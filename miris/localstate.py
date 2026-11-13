"""UI preferences, export folders, owner-only writes."""

from __future__ import annotations

import json
from pathlib import Path

from . import config


def prefs_path() -> Path:
    return config.state_dir() / "ui_prefs.json"


def load_prefs(path: Path | None = None) -> dict:
    try:
        raw = json.loads((path or prefs_path()).read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_pref(key: str, value, path: Path | None = None) -> dict:
    p = path or prefs_path()
    prefs = load_prefs(p)
    prefs[key] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prefs))
    return prefs


def workspace(destination: str, path: Path | None = None) -> str:
    value = load_prefs(path).get(f"workspace.{destination}")
    return value if isinstance(value, str) else ""


def set_workspace(destination: str, value: str, path: Path | None = None) -> None:
    save_pref(f"workspace.{destination}", value, path)


def pending_publish(path: Path | None = None) -> dict | None:
    """An upload a destination registered but never finalized, or None.

    One left over means Painter quit or the plugin reloaded mid-upload, with no
    window left to report it.
    """
    value = load_prefs(path).get("pending_publish")
    return value if isinstance(value, dict) and value else None


def set_pending_publish(record: dict | None, path: Path | None = None) -> None:
    save_pref("pending_publish", record, path)


def chosen_environments(path: Path | None = None) -> list[str]:
    """HDRIs the user picked from disk, most recent first."""
    value = load_prefs(path).get("chosen_environments")
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def remember_environment(file: str, path: Path | None = None) -> None:
    kept = [file, *(f for f in chosen_environments(path) if f != file)]
    save_pref("chosen_environments", kept[:12], path)


def forget_environment(file: str, path: Path | None = None) -> None:
    save_pref("chosen_environments", [f for f in chosen_environments(path) if f != file], path)


def keep_exports(path: Path | None = None) -> bool:
    """Debugging aid: keep each run's export, full-resolution textures included."""
    return bool(load_prefs(path).get("keep_exports"))


def reveal_exports(path: Path | None = None) -> bool:
    """Open the kept folder after a publish. On by default so kept exports get found."""
    value = load_prefs(path).get("reveal_exports")
    return True if value is None else bool(value)


EXPORTS_FOLDER = "Miris"
PAINTER_FOLDER = ("Adobe", "Adobe Substance 3D Painter")
PAINTER_EXPORT_FOLDER = "export"


def painter_export_dir() -> Path:
    """A subfolder of Painter's export folder, where artists already look."""
    return documents_dir().joinpath(*PAINTER_FOLDER, PAINTER_EXPORT_FOLDER, EXPORTS_FOLDER)


def export_root(path: Path | None = None) -> Path:
    """Falls back to the default when the saved folder is unusable: it may be on
    an unplugged drive, and this is read after Upload, too late to refuse."""
    saved = load_prefs(path).get("export_root")
    if isinstance(saved, str) and saved:
        candidate = Path(saved)
        parent = candidate if candidate.is_dir() else candidate.parent
        if parent.is_dir():
            return candidate
    return painter_export_dir()


def set_export_root(folder: Path | str | None, path: Path | None = None) -> dict:
    return save_pref("export_root", str(folder) if folder else "", path)


# Everything else is dropped rather than substituted: "v2/final" should read as
# "v2 final", not "v2_final", and must not become two path components.
_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_.")


def safe_folder_name(asset_name: str, fallback: str = "asset") -> str:
    """Truncated because macOS caps a component at 255 bytes and a timestamp
    follows. Trailing dots and spaces go because Windows strips them."""
    cleaned = "".join(c if c in _NAME_OK else " " for c in asset_name)
    cleaned = " ".join(cleaned.split())[:60].strip(" .")
    return cleaned or fallback


def documents_dir() -> Path:
    """QStandardPaths, not ~/Documents: on Windows the folder is routinely
    redirected into OneDrive."""
    try:
        from PySide6 import QtCore

        raw = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.DocumentsLocation
        )
        if raw:
            return Path(raw)
    except Exception:  # noqa: BLE001 (outside Qt, or no such location)
        pass
    return Path.home() / "Documents"


def export_folder(asset_name: str, when: str, root: Path | None = None) -> Path:
    """Always a new folder: the same asset is often uploaded twice a minute, and
    a merged folder hides what was sent."""
    base = root if root is not None else export_root()
    stem = f"{safe_folder_name(asset_name)} {when}".strip()
    candidate = base / stem
    suffix = 2
    while candidate.exists():
        candidate = base / f"{stem} ({suffix})"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def write_private(path: Path, text: str) -> None:
    """Owner-only, since the default 0644 exposes tokens to other accounts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass  # a pre-existing directory we cannot chmod is not worth failing over
    path.write_text(text)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows and some network shares ignore POSIX modes
