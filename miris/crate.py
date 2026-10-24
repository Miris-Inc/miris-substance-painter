"""Read the file references out of a binary USD layer (.usdc, "crate") without pxr.

Painter's Python has no USD library, and the packer used to copy a binary
source mesh into the .usdz untouched. A source exported from Blender carries
its own materials and their texture paths, so the archive reached Miris with a
list of files it did not contain ("referenced file(s) could not be found").

A crate file keeps every string it uses, asset paths included, in one TOKENS
section (asset paths are stored as token indices; see crateFile.cpp). Reading
that table is enough to know what the layer depends on. The table is
LZ4-compressed from format 0.4.0 on, framed by TfFastCompression, so a byte
search of the file finds only fragments.

Layout, all little-endian:

    bootstrap   ident[8] "PXR-USDC", version[8] (major, minor, patch, unused),
                int64 tocOffset, int64 reserved[8]
    TOC         uint64 count, then count x (char name[16], int64 start, int64 size)
    TOKENS      uint64 numTokens, then
                  < 0.4.0: uint64 numBytes, bytes
                  >= 0.4.0: uint64 uncompressedSize, uint64 compressedSize, bytes
                the bytes are numTokens NUL-terminated strings
    compression uint8 nChunks; 0 means one LZ4 block follows, otherwise
                nChunks x (int32 size, LZ4 block)
"""

from __future__ import annotations

import posixpath
import struct
from pathlib import Path

MAGIC = b"PXR-USDC"

_SECTION = struct.Struct("<16sqq")


class CrateError(ValueError):
    """The file is not a crate layer this reader understands."""


def is_crate(path: Path) -> bool:
    try:
        with Path(path).open("rb") as handle:
            return handle.read(8) == MAGIC
    except OSError:
        return False


def tokens(path: Path) -> list[str]:
    """Every string in the layer's token table, in file order.

    Raises CrateError for anything the reader cannot make sense of, a
    truncated file included: USD itself would not open that either.
    """
    try:
        return _tokens(Path(path).read_bytes(), Path(path).name)
    except (struct.error, IndexError) as e:
        raise CrateError(f"{Path(path).name} is truncated or not a binary USD layer") from e


def _tokens(data: bytes, name: str) -> list[str]:
    if data[:8] != MAGIC:
        raise CrateError(f"{name} is not a binary USD layer")
    version = tuple(data[8:11])
    (toc_offset,) = struct.unpack_from("<q", data, 16)
    (count,) = struct.unpack_from("<Q", data, toc_offset)
    sections = {}
    for index in range(count):
        label, start, size = _SECTION.unpack_from(data, toc_offset + 8 + index * _SECTION.size)
        sections[label.split(b"\0", 1)[0].decode("ascii", "replace")] = (start, size)
    if "TOKENS" not in sections:
        raise CrateError(f"{name} has no token table")
    start, _size = sections["TOKENS"]
    (num_tokens,) = struct.unpack_from("<Q", data, start)
    if version < (0, 4, 0):
        (num_bytes,) = struct.unpack_from("<Q", data, start + 8)
        chars = data[start + 16 : start + 16 + num_bytes]
    else:
        uncompressed, compressed = struct.unpack_from("<QQ", data, start + 8)
        chars = _decompress(data[start + 24 : start + 24 + compressed], uncompressed)
    # NUL-terminated strings, so the last byte is a terminator, not a token.
    if not chars.endswith(b"\0"):
        raise CrateError(f"{name} has an unterminated token table")
    found = chars[:-1].split(b"\0")
    if len(found) != num_tokens:
        raise CrateError(f"{name} claims {num_tokens} tokens, found {len(found)}")
    return [token.decode("utf-8", "replace") for token in found]


def _decompress(buffer: bytes, size: int) -> bytes:
    """TfFastCompression's framing around LZ4 blocks."""
    chunks = buffer[0]
    if chunks == 0:
        out = _lz4_block(buffer[1:], size)
    else:
        out = bytearray()
        offset = 1
        for _ in range(chunks):
            (chunk_size,) = struct.unpack_from("<i", buffer, offset)
            offset += 4
            out += _lz4_block(buffer[offset : offset + chunk_size], size - len(out))
            offset += chunk_size
        out = bytes(out)
    if len(out) != size:
        raise CrateError(f"token table decompressed to {len(out)} bytes, expected {size}")
    return out


def _lz4_block(src: bytes, limit: int) -> bytes:
    """One raw LZ4 block: a token byte, literals, then a back-reference, repeated."""
    out = bytearray()
    i = 0
    end = len(src)
    while i < end:
        token = src[i]
        i += 1
        literal = token >> 4
        if literal == 15:
            while True:
                extra = src[i]
                i += 1
                literal += extra
                if extra != 255:
                    break
        out += src[i : i + literal]
        i += literal
        if i >= end:
            break  # the last sequence carries literals only
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise CrateError("corrupt LZ4 block in token table")
        length = (token & 0x0F) + 4
        if length == 19:
            while True:
                extra = src[i]
                i += 1
                length += extra
                if extra != 255:
                    break
        start = len(out) - offset
        if offset >= length:
            out += out[start : start + length]
        else:
            for k in range(length):  # overlapping copy, byte by byte
                out.append(out[start + k])
        if len(out) > limit:
            raise CrateError("LZ4 block overruns the token table")
    return bytes(out)


# Suffixes that make a token read as a file the layer depends on. The packer
# decides which of these can go into the archive.
FILE_SUFFIXES = frozenset(
    {
        ".usd",
        ".usda",
        ".usdc",
        ".usdz",
        ".png",
        ".jpg",
        ".jpeg",
        ".exr",
        ".avif",
        ".tif",
        ".tiff",
        ".tga",
        ".bmp",
        ".hdr",
        ".webp",
        ".m4a",
        ".mp3",
        ".wav",
    }
)


def file_references(path: Path) -> list[str]:
    """The tokens that name a file, as written in the layer, without duplicates.

    Any string with a file suffix counts: a prim or property name never ends in
    one, and a value that does is a path whether or not it is an asset attribute.
    """
    seen: dict[str, None] = {}
    for token in tokens(path):
        if "\0" in token or not token.strip():
            continue
        suffix = posixpath.splitext(token.replace("\\", "/").rsplit("[", 1)[0])[1].lower()
        if suffix in FILE_SUFFIXES:
            seen.setdefault(token, None)
    return list(seen)
