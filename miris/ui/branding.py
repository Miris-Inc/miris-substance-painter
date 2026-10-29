"""Miris brand assets and formatting helpers.

The window UI itself is the QtWebEngine page in ui/web/ (design tokens live in
app.css, sourced from the portal's tailwind ramp). This module keeps what the
Qt host still needs: the window icon and byte formatting. Qt imports stay inside
functions so the module imports without PySide6 (pytest gate).
"""

from __future__ import annotations

from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets"

ACCENT = "#ff3500"  # accent-500, kept as the canonical brand accent reference


def format_bytes(count: int) -> str:
    """Human-readable size for the upload byte counter (12.3 MB style)."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def mark_icon():
    """The Miris M glyph as a QIcon (dock/window icon)."""
    from PySide6 import QtGui

    return QtGui.QIcon(str(ASSETS_DIR / "miris-mark.svg"))
