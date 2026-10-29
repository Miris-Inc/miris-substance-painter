"""Reading and patching Painter's USD export as text.

With "Export USD asset" enabled, Painter writes a stitching layer with no
geometry of its own:

    subLayers = [
        @./<name>.material.usda@,                  <- relative, ships as-is
        @/Users/you/Desktop/<source>.usdz@         <- absolute: the source mesh
    ]

The materials layer is already portable: of 380 references in a real export,
379 were relative texture paths and the one absolute was `painterProject_spp`,
provenance in customData.
"""

from __future__ import annotations

import os.path
import re
from pathlib import Path

from .api import FatalError

_REF = re.compile(r"@([^@]+)@")
# Painter's .spp provenance in customData. Nothing resolves it.
_IGNORED_SUFFIXES = (".spp",)
_TEXT_LAYER_SUFFIXES = (".usda", ".usd")


def is_text_layer(path: Path) -> bool:
    if path.suffix.lower() not in _TEXT_LAYER_SUFFIXES:
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(5).startswith(b"#usda")
    except OSError:
        return False


def references(text: str) -> list[str]:
    """Every @...@ reference in a text USD layer, in order of appearance."""
    return [m.group(1) for m in _REF.finditer(text)]


def is_absolute(ref: str) -> bool:
    return ref.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", ref) is not None


def normalize(ref: str) -> str:
    """Strip the leading './' USD uses for same-directory references."""
    return ref[2:] if ref.startswith("./") else ref


def absolute_references_in(text: str) -> list[str]:
    """Absolute references in layer text, ignoring Painter's .spp provenance."""
    return [
        ref for ref in references(text) if is_absolute(ref) and not ref.endswith(_IGNORED_SUFFIXES)
    ]


def absolute_references(root: Path) -> list[str]:
    """Absolute references in a text layer file; [] for a binary one."""
    if not is_text_layer(root):
        return []
    return absolute_references_in(root.read_text(encoding="utf-8", errors="replace"))


def collect_layer_files(root: Path, seen: set[str] | None = None) -> list[str]:
    """Relative paths a text layer needs, following nested text layers.

    Skips absolute references and does not check that files exist; the caller
    handles both.
    """
    seen = set() if seen is None else seen
    base = root.parent.resolve()
    found: list[str] = []

    def visit(layer: Path) -> None:
        if not is_text_layer(layer):
            return
        try:
            text = layer.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise FatalError(f"could not read {layer.name}: {e}") from e
        for ref in references(text):
            if is_absolute(ref) or ref.endswith(_IGNORED_SUFFIXES):
                continue
            nested = (layer.parent / normalize(ref)).resolve()
            rel = Path(os.path.relpath(nested, base)).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            found.append(rel)
            visit(nested)

    visit(root)
    return found


# Painter's material outputs, and the one a path tracer actually asks for.
_ADOBE_PREVIEW_CONNECT = re.compile(
    r"^(\s*)token outputs:usdpreviewsurface:surface\.connect = (<[^>]+>)\s*$"
)
_UNIVERSAL_SURFACE = re.compile(r"^\s*token outputs:surface(\.connect)?\b")


def ensure_universal_surface(text: str) -> str:
    """Alias each material's UsdPreviewSurface output onto the universal outputs:surface.

    Painter 12 writes only adobe:, mtlx: and usdpreviewsurface: outputs. A
    renderer that resolves the universal context first finds no surface and
    draws black, which the pipeline's path-traced tier did to every Painter
    asset. Done as text because Painter's Python has no pxr.
    """
    lines = text.split("\n")
    out = []
    for index, line in enumerate(lines):
        out.append(line)
        match = _ADOBE_PREVIEW_CONNECT.match(line)
        if match and not _same_prim_has_universal(lines, index, match.group(1)):
            out.append(f"{match.group(1)}token outputs:surface.connect = {match.group(2)}")
    return "\n".join(out)


def _same_prim_has_universal(lines: list[str], index: int, indent: str) -> bool:
    """Whether the prim owning lines[index] has a universal outputs:surface.

    A prim's attributes share one indentation depth. Deeper lines belong to a
    nested NodeGraph, which the real Painter file has. Depth is measured rather
    than prefix-matched, because an `indent + " "` test read a tab-indented
    NodeGraph's output as the material's own. Mixed tabs and spaces fail safe.
    """
    depth = len(indent)

    def scan(indices, opens: str, closes: str) -> bool:
        # Scan direction decides which brace opens a child and which ends the
        # prim. Reading both as the end missed a binding past a nested NodeGraph.
        nested = 0
        for j in indices:
            line = lines[j]
            if not line.strip():
                continue
            here = len(line) - len(line.lstrip())
            if here < depth:
                return False  # left the prim
            if here > depth:
                continue
            brace = line.strip()
            if brace == opens:
                nested += 1
            elif brace == closes:
                if nested == 0:
                    return False  # the prim's own boundary
                nested -= 1
            elif nested == 0 and _UNIVERSAL_SURFACE.match(line):
                return True
        return False

    return scan(range(index - 1, -1, -1), "}", "{") or scan(
        range(index + 1, len(lines)), "{", "}"
    )



_METERS_PER_UNIT = re.compile(r"^(\s*)metersPerUnit\s*=\s*\S+\s*$", re.M)
_HEADER_OPEN = re.compile(r"^#usda[^\n]*\n\(", re.M)
_USDA_LINE = re.compile(r"^#usda[^\n]*\n")


def set_meters_per_unit(text: str, value: float) -> str:
    """Set metersPerUnit on a root layer, inserting it when absent.

    Verified against OpenUSD: a sublayer's metersPerUnit is ignored, and a root
    with none resolves to 0.01 rather than inheriting one.
    """
    # %g would write 0.001 as 1e-03, which USD parses and nobody reads.
    number = f"{value:.10f}".rstrip("0").rstrip(".") or "0"
    declaration = f"metersPerUnit = {number}"
    if _METERS_PER_UNIT.search(text):
        return _METERS_PER_UNIT.sub(lambda m: f"{m.group(1)}{declaration}", text, count=1)
    header = _HEADER_OPEN.search(text)
    if header:
        return f"{text[: header.end()]}\n    {declaration}{text[header.end() :]}"
    # No metadata block; open one after the #usda line.
    line = _USDA_LINE.match(text)
    if not line:
        raise FatalError("not a text USD layer: no #usda line to write the unit after")
    return f"{text[: line.end()]}(\n    {declaration}\n)\n{text[line.end() :]}"


_DOME_LIGHT_PRIM = 'def DomeLight "MirisEnvironment"'


def add_dome_light(text: str, texture_ref: str) -> str:
    """Light the stage with one HDRI, as a top-level prim on the root layer.

    Top level so it does not depend on Painter's root prim name. That works only
    because the pipeline opens the root layer and traverses the whole stage; a
    reference to this file would load the default prim and drop the light. The
    pipeline keeps a stage's own dome if it has a non-black colour and positive
    intensity. 1500 matches the pipeline's default EXR; nothing documents the
    scale.
    """
    if not _USDA_LINE.match(text):
        raise FatalError("not a text USD layer: cannot add a dome light")
    if _DOME_LIGHT_PRIM in text:
        return text
    prim = (
        f"\n{_DOME_LIGHT_PRIM}\n"
        "{\n"
        f"    asset inputs:texture:file = @{texture_ref}@\n"
        "    float inputs:intensity = 1500\n"
        "    color3f inputs:color = (1, 1, 1)\n"
        "    custom bool visibleInPrimaryRay = false\n"
        "}\n"
    )
    return text.rstrip("\n") + "\n" + prim
