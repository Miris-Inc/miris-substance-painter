"""Pack a Painter USD export into a single .usdz.

USDZ is a zip with three rules, none needing a library:

  * every member is STORED, never deflated;
  * each member's data begins on a 64-byte boundary, padded through the local
    header's `extra` field as Apple's own usdz files do;
  * only layers, images and audio may be inside, so no nested .usdz, which a
    Painter source mesh often is.

A plain USD source mesh goes in as it stands. A .usdz source is unpacked into
the archive and the stage repointed at its inner layer. Anything else must be
converted first (exporter.export_mesh_as_usd).
"""

from __future__ import annotations

import posixpath
import re
import struct
import zipfile
from pathlib import Path

from . import crate, usdlayer
from .api import FatalError

ALIGNMENT = 64

# What the USDZ spec permits inside the archive.
ALLOWED_SUFFIXES = frozenset(
    {
        ".usd",
        ".usda",
        ".usdc",
        ".png",
        ".jpg",
        ".jpeg",
        ".exr",
        ".avif",
        ".m4a",
        ".mp3",
        ".wav",
    }
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._/-]+")


def safe_member_name(name: str) -> str:
    """A member name without the characters that trip fussy USD readers.

    Painter names textures like "fender_stratocaster (1)_BaseColor.png". Spaces
    and parentheses are legal, but they are the first suspect when a consumer
    fails, and renaming is free because references are rewritten in one pass.
    """
    cleaned = _UNSAFE.sub("_", name).lstrip("/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".", "..")]
    return "/".join(re.sub(r"_{2,}", "_", p).strip("_") or "file" for p in parts)


def _check_suffix(arcname: str) -> None:
    suffix = posixpath.splitext(arcname)[1].lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise FatalError(
            f"{arcname} cannot go inside a .usdz (allowed: {', '.join(sorted(ALLOWED_SUFFIXES))})"
        )


def padding_for(offset: int, arcname: str, alignment: int = ALIGNMENT) -> int:
    """Bytes of `extra` needed so this member's data lands on a boundary.

    A local header is 30 bytes plus the name, so the data offset is known
    before the header is written.
    """
    header = 30 + len(arcname.encode("utf-8"))
    return -(offset + header) % alignment


def write(destination: Path, members: list[tuple[str, bytes]]) -> Path:
    """Write members in order. The first must be the stage: it is the default layer."""
    if not members:
        raise FatalError("nothing to pack into a .usdz")
    root_suffix = posixpath.splitext(members[0][0])[1].lower()
    if root_suffix not in (".usd", ".usda", ".usdc"):
        raise FatalError(f"the first member of a .usdz must be a USD layer, got {members[0][0]}")
    seen: set[str] = set()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for arcname, payload in members:
            _check_suffix(arcname)
            if arcname in seen:
                raise FatalError(f"{arcname} would appear twice in the archive")
            seen.add(arcname)
            info = zipfile.ZipInfo(arcname)
            info.compress_type = zipfile.ZIP_STORED
            # A no-op 0x0000 extra record whose length does the aligning. Its
            # own header is 4 bytes, so a shorter pad borrows a whole block.
            pad = padding_for(archive.fp.tell(), arcname)
            if pad:
                if pad < 4:
                    pad += ALIGNMENT
                info.extra = struct.pack("<HH", 0, pad - 4) + b"\0" * (pad - 4)
            archive.writestr(info, payload)
    return destination


def _usdz_members(source: Path) -> list[tuple[str, bytes]]:
    """The members of a .usdz source mesh, root layer first."""
    try:
        with zipfile.ZipFile(source) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
            if not entries:
                raise FatalError(f"{source.name} is an empty .usdz")
            if Path(entries[0].filename).suffix.lower() not in (".usd", ".usda", ".usdc"):
                raise FatalError(f"{source.name} does not start with a USD layer")
            return [(entry.filename, archive.read(entry)) for entry in entries]
    except zipfile.BadZipFile as e:
        raise FatalError(f"{source.name} is not a readable .usdz") from e


def package(
    root_layer: Path,
    *,
    source_mesh: Path | None = None,
    stands_in_for: str | None = None,
    prefix: str = "mesh",
    meters_per_unit: float | None = None,
    dome_texture: Path | None = None,
    note=None,
) -> list[tuple[str, bytes]]:
    """Members for a .usdz built from a Painter export, root layer first.

    Text-layer references are rewritten relative to where they land. Binary
    layers go in untouched, so what they reference is read from their token
    table (see crate.py) and packed at the same relative path beside them; a
    reference that cannot land there stops the publish, because Miris would
    refuse the archive for it after the upload. A .usdz source keeps its
    internal names and bytes.

    `note` receives one line per binary layer whose dependencies were packed,
    for Painter's log.

    `meters_per_unit` goes on the root layer only; None keeps Painter's
    centimeters.

    `stands_in_for` is the name the stage uses for the mesh when it differs
    from source_mesh's, as when an FBX is re-exported to <stem>.geo.usd.
    Without it the archive shipped a reference to the artist's machine and an
    orphan substitute.

    `dome_texture` is stored under env/ and lit by a DomeLight on the root layer.
    """
    payloads: dict[str, bytes] = {}
    packed: dict[Path, str] = {}
    root_layer = root_layer.resolve()
    known_mesh_names = {source_mesh.name, stands_in_for} if source_mesh else set()

    def store(member: str, payload: bytes) -> None:
        _check_suffix(member)
        if member in payloads and payloads[member] != payload:
            raise FatalError(
                f"two different files would both pack as {member}; rename one of them and re-export"
            )
        payloads[member] = payload

    def lift(source: Path) -> str:
        inner = _usdz_members(source)
        for name, payload in inner:
            # Binary layers can reference any of these names, so without pxr the
            # whole internal namespace has to stay.
            if (
                usdlayer.is_absolute(name)
                or "\\" in name
                or any(part in ("", ".", "..") for part in name.split("/"))
            ):
                raise FatalError(f"{source.name} contains an invalid member path: {name}")
            store(f"{safe_member_name(prefix)}/{name}", payload)
        return f"{safe_member_name(prefix)}/{inner[0][0]}"

    def add_file(path: Path, member: str) -> str:
        path = path.resolve()
        if path in packed:
            return packed[path]
        if path.suffix.lower() == ".usdz":
            packed[path] = lift(path)
            return packed[path]
        if not path.is_file():
            raise FatalError(f"{root_layer.name} references {path}, which is missing")
        _check_suffix(member)
        packed[path] = member
        payload = path.read_bytes()
        if usdlayer.is_text_layer(path):
            text = payload.decode("utf-8")
            renames: dict[str, str] = {}
            for ref in usdlayer.references(text):
                if ref.endswith(".spp"):
                    # Never resolved, but verbatim it put the artist's home
                    # directory in every archive. Keep only the filename.
                    renames[ref] = _ref_basename(ref)
                    continue
                if path == root_layer and _ref_basename(ref) in known_mesh_names:
                    if source_mesh.suffix.lower() not in (".usd", ".usda", ".usdc", ".usdz"):
                        raise FatalError(
                            f"{source_mesh.name} has to be USD before it can be packed; "
                            "export the geometry as USD first"
                        )
                    target = add_file(source_mesh, safe_member_name(source_mesh.name))
                else:
                    dependency = path.parent / ref
                    target_name = (
                        f"{prefix}/{_ref_basename(ref)}"
                        if usdlayer.is_absolute(ref)
                        else posixpath.normpath(posixpath.join(posixpath.dirname(member), ref))
                    )
                    if "<UDIM>" in ref:
                        target = add_tiles(dependency, target_name)
                    else:
                        target = add_file(dependency, safe_member_name(target_name))
                relative = posixpath.relpath(target, posixpath.dirname(member) or ".")
                renames[ref] = relative if relative.startswith("../") else f"./{relative}"
            payload = _rewritten(payload, renames)
            payload = _healed(payload)
            if path == root_layer and meters_per_unit is not None:
                payload = usdlayer.set_meters_per_unit(
                    payload.decode("utf-8"), meters_per_unit
                ).encode("utf-8")
            if path == root_layer and dome_member is not None:
                payload = usdlayer.add_dome_light(
                    payload.decode("utf-8"), f"./{dome_member}"
                ).encode("utf-8")
        elif crate.is_crate(path):
            add_crate_dependencies(path, member)
        store(member, payload)
        return member

    def add_crate_dependencies(layer: Path, member: str) -> None:
        """Pack what a binary layer references, exactly where it looks for it.

        A Blender export (every Poly Haven download) carries its own materials
        and texture paths. The layer cannot be rewritten, so each file must
        land at its written relative path next to the member, with no
        renaming, and an absolute path or a missing file has nowhere to go.
        """
        base = posixpath.dirname(member)
        unresolved: list[str] = []
        shipped: list[str] = []
        try:
            references = crate.file_references(layer)
        except crate.CrateError as e:
            # USD would not open it either, so Miris would fail it after the upload.
            raise FatalError(f"{e}; re-export the mesh and try again") from e
        for ref in references:
            if usdlayer.is_absolute(ref) or "\\" in ref or "[" in ref:
                unresolved.append(ref)
                continue
            target = posixpath.normpath(posixpath.join(base, ref))
            if target.startswith("../"):
                unresolved.append(ref)  # above the archive root
                continue
            dependency = layer.parent / usdlayer.normalize(ref)
            if "<UDIM>" in ref:
                if not dependency.parent.is_dir() or add_tiles(dependency, target) != target:
                    unresolved.append(ref)
                    continue
            elif not dependency.is_file():
                unresolved.append(ref)
                continue
            else:
                actual = add_file(dependency, target)
                if actual != target:
                    # Already packed under another name for a text layer; this
                    # layer still needs it at the written path.
                    store(target, payloads[actual])
            shipped.append(ref)
        if unresolved:
            listed = ", ".join(unresolved[:10])
            more = f" and {len(unresolved) - 10} more" if len(unresolved) > 10 else ""
            raise FatalError(
                f"{layer.name} is a binary USD layer that references {len(unresolved)} file(s) "
                f"the archive cannot carry: {listed}{more}. Miris would refuse the upload "
                "for them (\"referenced file(s) could not be found\"). Put each file at that "
                f"path next to {layer.name}, or re-export the mesh without its own materials"
            )
        if shipped and note is not None:
            note(f"packed {len(shipped)} file(s) referenced by {layer.name}: {', '.join(shipped)}")

    def add_tiles(pattern: Path, target_name: str) -> str:
        if "<UDIM>" in str(pattern.parent) or pattern.name.count("<UDIM>") != 1:
            raise FatalError(f"unsupported UDIM path: {pattern}; use one token in the filename")
        marker = "MIRISUDIMTOKEN"
        while marker in target_name:
            marker += "X"
        target = safe_member_name(target_name.replace("<UDIM>", marker)).replace(marker, "<UDIM>")
        matcher = re.compile(re.escape(pattern.name).replace("<UDIM>", r"([0-9]{4})"))
        matched = []
        if pattern.parent.is_dir():
            for tile in sorted(pattern.parent.iterdir()):
                match = matcher.fullmatch(tile.name)
                if match and tile.is_file():
                    matched.append((tile, match.group(1)))
        if not matched:
            raise FatalError(f"{root_layer.name} references {pattern}, which has no UDIM tiles")
        for tile, number in matched:
            tile_member = target.replace("<UDIM>", number)
            actual_member = add_file(tile, tile_member)
            if actual_member != tile_member:
                # An earlier direct reference packed this tile elsewhere; the
                # pattern still needs it here.
                store(tile_member, payloads[actual_member])
        return target

    dome_member = None
    if dome_texture is not None:
        if not dome_texture.is_file():
            raise FatalError(f"environment {dome_texture} is missing")
        dome_member = f"env/{safe_member_name(dome_texture.name)}"
        store(dome_member, dome_texture.read_bytes())

    root_name = add_file(root_layer, safe_member_name(root_layer.name))
    return [(root_name, payloads[root_name])] + [
        (name, payloads[name]) for name in sorted(payloads) if name != root_name
    ]


def _healed(payload: bytes) -> bytes:
    """Fix a text layer's Painter materials so they do not render black.

    See usdlayer.ensure_universal_surface. Binary layers pass through.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload  # crate
    if not text.startswith("#usda"):
        return payload
    text = usdlayer.ensure_universal_surface(text)
    return text.encode("utf-8")


def _ref_basename(ref: str) -> str:
    """The filename part of a reference, whichever separator wrote it."""
    return ref.replace("\\", "/").rsplit("/", 1)[-1]


def _rewritten(payload: bytes, renames: dict[str, str]) -> bytes:
    """Rewrite references once, so a renamed path cannot match another old name."""
    text = payload.decode("utf-8")
    return re.sub(
        r"@([^@]+)@", lambda match: f"@{renames.get(match.group(1), match.group(1))}@", text
    ).encode("utf-8")


def find_root_layer(export_dir: Path, exclude=()) -> Path:
    """The stage in a Painter export: the top-level layer nothing references.

    Found by structure because Painter's naming has changed before. `exclude`
    is for layers the caller wrote, like the substitute geometry, which is
    unreferenced too: the tiebreak once picked Grinder.geo.usd over
    MiniGrinder.usd (same length, G first) and the upload failed with "would
    appear twice in the archive".
    """
    excluded = {p.resolve() for p in exclude}
    candidates = sorted(
        p
        for p in export_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in (".usd", ".usda", ".usdc")
        and p.resolve() not in excluded
    )
    if not candidates:
        raise FatalError(f"no USD layer in {export_dir.name} to pack")
    referenced: set[str] = set()
    for layer in candidates:
        if not usdlayer.is_text_layer(layer):
            continue
        for ref in usdlayer.collect_layer_files(layer):
            referenced.add(posixpath.basename(ref))
    roots = [p for p in candidates if p.name not in referenced]
    if not roots:
        raise FatalError(
            f"every layer in {export_dir.name} is referenced by another; there is no stage to pack"
        )
    # Last resort for several stages: the shorter name, which only works when
    # the names share a stem.
    return min(roots, key=lambda p: (len(p.name), p.name))
