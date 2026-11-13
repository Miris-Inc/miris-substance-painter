"""The About window: a static page rendered in Python, with no bridge.

details() is Qt-free and Painter-free so tests can read it.
"""

from __future__ import annotations

import datetime
import html
import os
import platform
import webbrowser
from pathlib import Path

from .. import config, destinations
from ..version import __version__

_WEB_DIR = Path(__file__).parent / "web"
_WINDOW_SIZE = (460, 430)
_PAGE_BACKGROUND = "#1c1c1f"

TITLE = "Miris for Substance 3D Painter"
DESCRIPTION = "Publishes the open project as a composed USD asset to Miris and partner platforms."
LICENCE = "Apache License 2.0"
COMPANY = "Miris, Inc."
WEBSITE = "https://miris.com"


def details() -> dict:
    miris = destinations.by_id("miris") or {}
    catalog_host = (miris.get("auth") or {}).get("host", "")
    from_env = bool(os.environ.get(config.HOST_ENV))
    return {
        "title": TITLE,
        "version": __version__,
        "description": DESCRIPTION,
        "host": config.resolve_host(catalog_host),
        "host_source": config.HOST_ENV if from_env else "catalog",
        "painter": painter_version(),
        "python": platform.python_version(),
        "qt": qt_version(),
        "platform": platform.platform(terse=True),
        "docs_url": miris.get("help_url") or WEBSITE,
        "website": WEBSITE,
        "licence": LICENCE,
        "copyright": f"© {datetime.date.today().year} {COMPANY}",
    }


def painter_version() -> str:
    """Painter's own version, or "unknown" when this is not running inside it."""
    try:
        import substance_painter.application as app
    except ImportError:
        return "unknown"
    for name in ("version", "version_info"):
        try:
            value = getattr(app, name)()
        except Exception:  # noqa: BLE001 (an About box must never fail on a lookup)
            continue
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (tuple, list)) and value:
            return ".".join(str(part) for part in value)
    return "unknown"


def qt_version() -> str:
    try:
        from PySide6 import QtCore

        return QtCore.qVersion()
    except ImportError:
        return "unknown"


def support_text(facts: dict) -> str:
    """What "Copy details" puts on the clipboard: the lines a bug report needs."""
    host = facts["host"]
    if facts["host_source"] != "catalog":
        host += f" (from {facts['host_source']})"
    return "\n".join(
        [
            f"{facts['title']} {facts['version']}",
            f"Miris host: {host}",
            f"Painter: {facts['painter']}",
            f"Python: {facts['python']}",
            f"Qt: {facts['qt']}",
            f"System: {facts['platform']}",
        ]
    )


def render(facts: dict) -> str:
    """Every value is escaped. The wordmark comes from the catalog so the logo
    exists once. Raises ValueError if a placeholder is left unfilled."""
    template = (_WEB_DIR / "about.html").read_text(encoding="utf-8")
    logo = ((destinations.by_id("miris") or {}).get("logo") or {}).get("svg", "")
    values = {key: html.escape(str(value)) for key, value in facts.items()}
    values["wordmark"] = logo.replace("<svg ", '<svg class="about-wordmark" ', 1)
    values["host_note"] = (
        "" if facts["host_source"] == "catalog" else f"set by {values['host_source']}"
    )
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    if "{{" in template:
        raise ValueError("about.html has a placeholder nothing fills")
    return template


class AboutDialog:
    """Imports Qt lazily so the module loads without it."""

    def __init__(self, parent=None):
        from PySide6 import QtCore, QtGui, QtWebEngineCore, QtWebEngineWidgets, QtWidgets

        from . import branding

        class ExternalLinks(QtWebEngineCore.QWebEnginePage):
            """Clicked links open in the system browser."""

            def acceptNavigationRequest(self, url, kind, is_main_frame):
                if kind == QtWebEngineCore.QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                    text = url.toString()
                    if text.startswith(("http://", "https://")):
                        webbrowser.open(text)
                    return False
                return True

        self.window = QtWidgets.QDialog(parent)
        self.window.setWindowTitle("About Miris")
        self.window.setWindowIcon(branding.mark_icon())
        self.window.setWindowModality(QtCore.Qt.ApplicationModal)
        self.window.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
        self.window.setFixedSize(*_WINDOW_SIZE)

        self.view = QtWebEngineWidgets.QWebEngineView(self.window)
        self.view.setPage(ExternalLinks(self.view))
        self.view.setContextMenuPolicy(QtCore.Qt.NoContextMenu)
        self.view.page().setBackgroundColor(QtGui.QColor(_PAGE_BACKGROUND))

        copy = QtWidgets.QPushButton("Copy details")
        copy.setAutoDefault(False)
        copy.clicked.connect(self._copy)
        close = QtWidgets.QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.window.close)
        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(14, 0, 14, 14)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)

        layout = QtWidgets.QVBoxLayout(self.window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view, 1)
        layout.addLayout(buttons)

        self._facts: dict = {}
        self._QtCore = QtCore
        self._QtWidgets = QtWidgets

    def open(self) -> None:
        # Re-rendered on every open: the host can change through the environment.
        self._facts = details()
        base = self._QtCore.QUrl.fromLocalFile(str(_WEB_DIR) + "/")
        self.view.setHtml(render(self._facts), base)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _copy(self) -> None:
        clipboard = self._QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(support_text(self._facts))

    def close(self) -> None:
        self.window.close()
        self.window.deleteLater()
