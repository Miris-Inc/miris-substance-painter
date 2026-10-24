"""Environments a lit publish can pack, from Painter's shelves, with Painter's own previews."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

from .api import FatalError

# No HDRI: the pipeline lights the asset with its own default.
DEFAULT_ID = "miris_default"
DEFAULT_LABEL = "Miris default"
YOUR_FILES = "Your files"
_EXR_MAGIC = b"\x76\x2f\x31\x01"


@dataclass(frozen=True)
class Environment:
    id: str
    label: str
    group: str
    path: Path

    @property
    def meta(self) -> Path:
        """Painter's sidecar for this file, which carries its preview."""
        return self.path.parent / ".alg_meta" / self.path.name


def shelves() -> list[tuple[str, Path]]:
    """Main thread only."""
    import substance_painter.resource as sp_resource

    return [(shelf.name(), Path(shelf.path())) for shelf in sp_resource.Shelves.all()]


def find(shelf_roots: list[tuple[str, Path]]) -> list[Environment]:
    """Every EXR under each shelf's environments folder, in shelf and path order.

    Only EXR: a .usdz may not hold an .hdr.
    """
    found = []
    for name, root in shelf_roots:
        base = root / "environments"
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.exr")):
            if ".alg_meta" in path.parts:
                continue
            group = path.parent.name if path.parent != base else "Environments"
            rel = path.relative_to(root).as_posix()
            found.append(Environment(f"{name}/{rel}", path.stem, group, path))
    return found


def from_file(path: Path) -> Environment:
    """An EXR the user chose, outside any shelf. Raises FatalError if it is not one."""
    if path.suffix.lower() != ".exr":
        raise FatalError(f"{path.name} is not an EXR; a .usdz can only carry EXR lighting")
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError as e:
        raise FatalError(f"{path.name} could not be read: {e}") from e
    if magic != _EXR_MAGIC:
        raise FatalError(f"{path.name} is not a readable EXR image")
    return Environment(f"file:{path}", path.stem, YOUR_FILES, path)


def preview(meta: Path) -> bytes | None:
    """The WebP Painter embeds in a sidecar, or None if there is none to read."""
    try:
        data = meta.read_bytes()
    except OSError:
        return None
    start = data.find(b"RIFF")
    if start < 0 or data[start + 8 : start + 12] != b"WEBP":
        return None
    (size,) = struct.unpack("<I", data[start + 4 : start + 8])
    end = start + 8 + size
    return data[start:end] if end <= len(data) else None


def cached_preview(env: Environment, cache_dir: Path) -> Path | None:
    """Painter's preview as a file the page can load, written once per version of the HDRI."""
    try:
        stamp = f"{env.path}:{env.path.stat().st_mtime_ns}"
    except OSError:
        return None
    target = cache_dir / f"{hashlib.sha1(stamp.encode()).hexdigest()[:16]}.webp"
    if target.is_file():
        return target
    image = preview(env.meta)
    if image is None:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image)
    return target


def resolve(ids: list[str], environments: list[Environment]) -> list[tuple[str, Path | None]]:
    """(label, HDRI) per chosen id, in order; the Miris default has no HDRI.

    Ids come from the page, so each must name an environment this plugin listed.
    """
    by_id = {e.id: e for e in environments}
    resolved = []
    for env_id in ids:
        if env_id == DEFAULT_ID:
            resolved.append((DEFAULT_LABEL, None))
            continue
        env = by_id.get(env_id)
        if env is None or not env.path.is_file():
            raise FatalError(f"the lighting {env.label if env else env_id} can no longer be found")
        resolved.append((env.label, env.path))
    return resolved
