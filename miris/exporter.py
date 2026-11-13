"""Export the open project as a USD asset Miris can ingest.

The textures go out with the "Document channels + Normal + AO (No Alpha)"
preset plus "exportUSD", a real export config key that Adobe's own round-trip
test passes. That adds a materials layer and a stage referencing the source
mesh, so nothing depends on the Output template dropdown.

The stage references the source mesh because Painter triangulates at import:
shipping the untouched file is the only way to keep subdivision. A source the
pipeline cannot open (FBX above all) falls back to exported USD geometry and
loses it. This replaced a glTF export, which cannot carry subdivision.
"""

from __future__ import annotations

from pathlib import Path

from .api import FatalError


class ExportCancelled(Exception):
    """The user pressed Cancel in Painter's own export progress dialog."""


def _check_status(status, export_status, what: str, message: str) -> None:
    """Warning still means the export completed; Cancelled is the user's choice."""
    if status == export_status.Cancelled:
        raise ExportCancelled(what)
    if status not in (export_status.Success, export_status.Warning):
        raise FatalError(f"{what} failed: {message}")


# What a conditioning pipeline can open. FBX is missing and is a common Painter
# source, so an unsupported source mesh must never be uploaded as-is.
_INGESTIBLE_MESH_EXTS = (
    ".usd",
    ".usda",
    ".usdc",
    ".usdz",
    ".obj",
    ".gltf",
    ".glb",
    ".stl",
)

# A .gltf needs its sibling .bin and textures, so it cannot ship alone. .glb
# embeds everything.
_MULTI_FILE_MESH_EXTS = (".gltf",)

# What can go inside the .usdz: narrower than what the pipeline can open.
_PACKABLE_MESH_EXTS = (".usd", ".usda", ".usdc", ".usdz")


def is_ingestible_mesh(path) -> bool:
    return Path(path).suffix.lower() in _INGESTIBLE_MESH_EXTS


def is_self_contained_source(path) -> bool:
    """Whether this file can ship as the geometry on its own, inside the .usdz.

    Stricter than is_ingestible_mesh: .obj, .stl and .glb are ingestible but
    cannot go in a .usdz, and offering them aborted the publish in the packer
    after the export had already run.
    """
    # Packable implies ingestible and single-file, so this one test covers all three.
    return Path(path).suffix.lower() in _PACKABLE_MESH_EXTS


def _log(message: str) -> None:
    """Log to Painter; a no-op outside it.

    The chosen preset is logged because it decides what an upload is, and it
    once went unlogged while every export silently came out as glTF.
    """
    try:
        import substance_painter.logging as sp_log

        sp_log.info(f"[miris] {message}")
    except ImportError:
        pass


def default_asset_name() -> str:
    import substance_painter.project

    if not substance_painter.project.is_open():
        return "Untitled"
    name = substance_painter.project.name()
    return name or "Untitled"


def source_mesh_path():
    """The mesh this project was created from, or None.

    Painter's USD stage references it by absolute path, so the upload has to
    ship it too.
    """
    from pathlib import Path as _Path

    try:
        import substance_painter.project

        raw = substance_painter.project.last_imported_mesh_path()
    except Exception:  # noqa: BLE001 (outside Painter / no project open)
        return None
    return _Path(raw) if raw else None


def stale_source_mesh(mesh_mtime: float, project_mtime: float) -> bool:
    """Whether the source file changed after the project was last saved.

    The stage references the file on disk, but the maps were painted against
    Painter's imported copy, so an edited file uploads with the textures
    sliding off the model. Saving without reloading the mesh hides the change,
    so this only warns.
    """
    return mesh_mtime > project_mtime


def source_mesh_warning() -> str | None:
    """A caution to show before uploading, or None when nothing looks wrong."""
    import os.path

    try:
        import substance_painter.project

        source = source_mesh_path()
        project_file = substance_painter.project.file_path()
        if source is None or not project_file:
            return None
        if not is_self_contained_source(source):
            # The fallback re-exports geometry with Painter's own UVs, so it
            # cannot disagree with the maps.
            return None
        if not stale_source_mesh(os.path.getmtime(source), os.path.getmtime(project_file)):
            return None
    except Exception:  # noqa: BLE001 (a warning must never block the upload)
        return None
    return (
        f"{source.name} has changed since this project was saved. The upload "
        "references it for geometry, so the textures may not line up. Reload "
        "the mesh in Painter and save before uploading."
    )


def _uses_udims() -> bool | None:
    """Whether any texture set is UDIM-tiled, or None when it cannot be told."""
    try:
        import substance_painter.textureset

        return any(ts.has_uv_tiles() for ts in substance_painter.textureset.all_texture_sets())
    except Exception:  # noqa: BLE001 (outside Painter / API unavailable)
        return None


def export_mesh_as_usd(destination: Path) -> None:
    """Write the project's geometry to a .usd, for a source the pipeline cannot open.

    FBX above all, which the Miris pipeline omits pending a license assessment.
    The result is always triangles.
    """
    import substance_painter.export as sp_export
    import substance_painter.project

    if not substance_painter.project.is_open():
        raise FatalError("no project is open in Painter")

    # BaseMesh rather than Painter's documented triangulating example, though
    # neither keeps quads: Painter triangulates at IMPORT. A six-quad cube.fbx
    # (24 PolygonVertexIndex entries) exports as 12 triangles either way.
    option = sp_export.MeshExportOption.BaseMesh
    if sp_export.scene_has_tessellation():
        option = sp_export.MeshExportOption.TessellationNormalsBaseMesh

    destination.parent.mkdir(parents=True, exist_ok=True)
    result = sp_export.export_mesh(str(destination), option)
    _check_status(result.status, sp_export.ExportStatus, "the mesh export", result.message)


def export_usd_asset(export_dir: Path) -> None:
    """Export the project as a composed USD asset. Main thread only.

    Writes the texture maps, a materials layer binding them (MaterialX/OpenPBR
    with a UsdPreviewSurface fallback) and a stage referencing the source mesh.
    Adobe's test_usd_openpbr_roundtrip.py passes "exportUSD" the same way.
    """
    import substance_painter.export
    import substance_painter.project
    import substance_painter.textureset

    if not substance_painter.project.is_open():
        raise FatalError("no project is open in Painter")

    presets = _available_presets()
    chosen = _pick_material_preset([name for name, _ in presets])
    if chosen is None:
        raise FatalError("no usable texture export preset found in this Painter version")
    preset_url = next(url for name, url in presets if name == chosen)
    _log(f"export preset: {chosen}")

    export_list = [{"rootPath": ts.name} for ts in substance_painter.textureset.all_texture_sets()]
    if not export_list:
        raise FatalError("the project has no texture sets, so there is nothing to export")
    result = substance_painter.export.export_project_textures(
        {
            "defaultExportPreset": preset_url,
            "exportList": export_list,
            "exportMeshTessellation": False,
            "exportParameters": [
                {
                    "filter": {},
                    "parameters": {"dilationDistance": 16, "paddingAlgorithm": "infinite"},
                }
            ],
            "exportPath": str(export_dir),
            "exportPresets": [],
            "exportShaderParams": False,
            "exportUSD": True,
        }
    )
    _check_status(
        result.status, substance_painter.export.ExportStatus, "Painter's export", result.message
    )

    # A generator preset silently wins over "exportUSD": the glTF preset wrote a
    # .glb and no stage, and the publisher uploaded it. Only this layer can tell
    # that from an intended glTF upload.
    if not any(p.suffix.lower() in _USD_SUFFIXES for p in export_dir.rglob("*")):
        raise FatalError(
            f"the export produced no USD stage (preset: {chosen}). "
            "This preset composes its own output and ignores the USD request"
        )
    maps = exported_maps(export_dir)
    if not maps:
        raise FatalError(
            f"the export produced no texture maps (preset: {chosen}); "
            "the asset would upload with untextured materials"
        )
    # Here rather than in the packer, which would refuse deep into the publish.
    unpackable = unpackable_maps(maps)
    if unpackable:
        raise FatalError(
            f"the export wrote {', '.join(unpackable)} textures (preset: {chosen}), "
            "which a .usdz cannot hold. The plugin picks this preset itself, so there is "
            "nothing to change in the publish window; please report it"
        )


# Wider than what a .usdz accepts on purpose: calling a .tga export "untextured"
# would name the wrong problem. Not exhaustive; add a suffix when a preset is
# seen writing it.
_MAP_SUFFIXES = (".png", ".jpg", ".jpeg", ".exr", ".avif", ".tif", ".tiff", ".tga")

# The subset a .usdz may hold; the spec does not list tga or tiff.
_PACKABLE_MAP_EXTS = (".png", ".jpg", ".jpeg", ".exr", ".avif")


def exported_maps(export_dir: Path) -> list[Path]:
    return sorted(p for p in export_dir.rglob("*") if p.suffix.lower() in _MAP_SUFFIXES)


# Painter works in centimeters whatever the source used, and the project's unit
# scale never reaches the export, so the artist has to pick the unit.
UNITS = (
    ("cm", "Centimeters", 0.01),
    ("mm", "Millimeters", 0.001),
    ("dm", "Decimeters", 0.1),
    ("m", "Meters", 1.0),
    ("in", "Inches", 0.0254),
)
PAINTER_UNIT = "cm"


def unit_choices():
    return [{"id": key, "label": label} for key, label, _mpu in UNITS]


def meters_per_unit_for(unit_id: str) -> float:
    for key, _label, mpu in UNITS:
        if key == unit_id:
            return mpu
    return meters_per_unit_for(PAINTER_UNIT)


def partial_displacement(with_height, all_sets) -> str | None:
    """A caution when only some texture sets have height, which is a seam, not a choice."""
    if not with_height or len(with_height) == len(all_sets):
        return None
    flat = [s for s in all_sets if s not in with_height]
    return (
        f"Height is painted on {len(with_height)} of {len(all_sets)} texture sets; "
        f"{', '.join(flat)} will not displace. Add height there or remove it everywhere."
    )


def displacement_warning() -> str | None:
    try:
        import substance_painter.textureset as ts_api

        kinds = (ts_api.ChannelType.Height, ts_api.ChannelType.Displacement)
        sets = ts_api.all_texture_sets()
        with_height = [
            ts.name
            for ts in sets
            if any(stack.has_channel(k) for stack in ts.all_stacks() for k in kinds)
        ]
        return partial_displacement(with_height, [ts.name for ts in sets])
    except Exception:  # noqa: BLE001 (a warning must never block the upload)
        return None


def dropped_channels(labels_by_set, map_stems) -> list[str]:
    """Custom channels that produced no texture, one line each.

    Judged from what the export wrote, since Painter does not document how
    presets treat user channels.
    """
    stems = [s.lower() for s in map_stems]
    return [
        f"custom channel {label} on {set_name} was not exported"
        for set_name, labels in labels_by_set.items()
        for label in labels
        if not any(s.startswith(set_name.lower()) and label.lower() in s for s in stems)
    ]


def dropped_channel_notes(export_dir: Path) -> list[str]:
    try:
        import substance_painter.textureset as ts_api

        labels_by_set = {
            ts.name: [
                ch.label() or ch.type().name
                for stack in ts.all_stacks()
                for kind, ch in stack.all_channels().items()
                if kind.name.startswith("User")
            ]
            for ts in ts_api.all_texture_sets()
        }
        return dropped_channels(labels_by_set, [p.stem for p in exported_maps(export_dir)])
    except Exception:  # noqa: BLE001 (outside Painter, or an older API)
        return []


def substitute_reason(source: Path) -> str:
    """Why this source is being re-exported instead of shipped, for the log."""
    if source.suffix.lower() in _MULTI_FILE_MESH_EXTS:
        return "depends on sibling files"
    if is_ingestible_mesh(source):
        return "cannot go inside a .usdz"
    return "is not a format the pipeline opens"


def unpackable_maps(maps: list[Path]) -> list[str]:
    """The suffixes among these textures that a .usdz cannot hold."""
    return sorted({p.suffix.lower() for p in maps} - set(_PACKABLE_MAP_EXTS))


_USD_SUFFIXES = (".usd", ".usda", ".usdc", ".usdz")

# Painter 12's preset, lowercased, then the hint for a renamed sibling. It
# exports every channel the project defines, plus baked normal and AO.
_CHANNELS_PRESET = "document channels + normal + ao (no alpha)"
_CHANNELS_HINT = "document channels"
# Fallback for a Painter without it.
_MATERIAL_PRESET_HINTS = ("pbr metallic roughness", "pbr metal roughness")

# Presets whose generator runs instead of the USD export and writes no stage.
_GENERATOR_HINTS = ("gltf", "glb", "usdz")


def _available_presets() -> list[tuple[str, str]]:
    """(name, url) for every output template, shelf presets first.

    list_predefined_export_presets() only has presets embedded in Painter; "PBR
    Metallic Roughness" is a shelf resource. Reading only the embedded list made
    every upload a triangulated .glb via "glTF PBR Metal Roughness".
    """
    import substance_painter.export as sp_export

    presets: list[tuple[str, str]] = []
    for preset in sp_export.list_resource_export_presets():
        presets.append((preset.resource_id.name, preset.resource_id.url()))
    for preset in sp_export.list_predefined_export_presets():
        presets.append((preset.name, preset.url))
    return presets


def _pick_material_preset(names: list[str]) -> str | None:
    """The texture preset whose maps the USD materials will bind.

    Specular/glossiness and generator presets are never picked, even as a last
    resort: they give a wrong-looking asset rather than a visible failure.
    """
    lowered = [(n, n.lower()) for n in names]
    usable = [
        (name, low)
        for name, low in lowered
        if "gloss" not in low and not any(h in low for h in _GENERATOR_HINTS)
    ]
    for name, low in usable:
        if low == _CHANNELS_PRESET:
            return name
    for name, low in usable:
        if _CHANNELS_HINT in low:
            return name
    for name, low in usable:
        if low in _MATERIAL_PRESET_HINTS:
            return name
    for name, low in usable:
        if "pbr" in low and "metal" in low:
            return name
    return usable[0][0] if usable else None


def stage_is_self_contained(export_dir: Path) -> bool:
    """Whether every reference in the exported stage is relative.

    Raises on a binary stage: absolute_references() only reads text, so a crate
    stage looked self-contained and shipped a reference to the artist's machine.
    """
    from . import usdlayer, usdz

    root = usdz.find_root_layer(export_dir)
    if not usdlayer.is_text_layer(root):
        raise FatalError(
            f"{root.name} is a binary USD stage, which this plugin cannot read. "
            "Re-export with a .usda stage"
        )
    return not usdlayer.absolute_references(root)


def export_uploadable_asset(export_dir: Path, *, note=None) -> Path | None:
    """Export into export_dir and return the mesh to ship with the stage, or None.

    Every entry point goes through here. They once differed, one using a glTF
    preset and one exporting USD, which meant two upload paths failing two ways.
    `note` receives progress lines for the Painter log.
    """
    export_usd_asset(export_dir)
    if note is not None:
        for line in dropped_channel_notes(export_dir):
            note(line)

    source = source_mesh_path()

    # Ask the stage, not the source format. Painter 12 embeds geometry for a
    # non-USD source (./<project>.geo.usd), and deciding by format shipped a
    # byte-identical 40MB duplicate as an orphan member.
    if stage_is_self_contained(export_dir):
        if note is not None and source is not None and source.suffix.lower() not in _USD_SUFFIXES:
            # Painter's own message about this ("not supported", "lossy") is
            # easy to read as a failure; say what it means for the upload.
            note(
                f"geometry is Painter's export of the {source.suffix} source, triangulated; "
                "the source file itself is not referenced"
            )
        return None

    if source is None:
        # Nothing to ship; the packer names the reference if it cannot resolve.
        return None
    if is_self_contained_source(source):
        return source

    # The routes trade opposites. Referencing the source keeps subdivision but
    # trusts it to still carry the painted UVs (see source_mesh_warning).
    # Re-exporting triangulates but carries Painter's own UVs.
    if note is not None:
        note(
            f"source mesh {source.suffix} {substitute_reason(source)}; "
            "exporting geometry as USD instead"
        )
    substitute = export_dir / f"{source.stem}.geo.usd"
    export_mesh_as_usd(substitute)
    return substitute
