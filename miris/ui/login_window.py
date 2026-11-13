"""Sign-in in a window inside Painter, handing off to the system browser.

Some providers, Google in particular, refuse to serve login to an embedded
view. The refusal is an ordinary page, so it is recognised by its text; a page
that fails to load hands off too. The flow keeps polling either way.
"""

from __future__ import annotations

import webbrowser

from PySide6 import QtCore, QtWebEngineWidgets, QtWidgets

from . import branding

# Matched case-insensitively against the rendered page.
_REFUSAL_MARKERS = (
    "browser or app may not be secure",
    "disallowed_useragent",
    "couldn't sign you in",
)

_WINDOW_SIZE = (520, 660)


class LoginWindow(QtWidgets.QDialog):
    cancelled = QtCore.Signal()

    def __init__(self, parent=None, title="Sign in to Miris"):
        super().__init__(parent)
        # Titled by the caller: a title naming the wrong provider is exactly what
        # an address bar exists to catch.
        self.setWindowTitle(title)
        self.setWindowIcon(branding.mark_icon())
        # Application-modal like Adobe's dialogs, so it stacks above the upload window.
        self.setWindowModality(QtCore.Qt.ApplicationModal)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
        self.resize(*_WINDOW_SIZE)

        self._url = ""
        self._handed_off = False
        self._done = False

        self.view = QtWebEngineWidgets.QWebEngineView(self)
        self.view.loadFinished.connect(self._on_load_finished)

        # No buttons. A QPushButton in a QDialog is auto-default, so Return
        # opened the browser instead of submitting the login form.
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view, 1)

    def present(self, url: str) -> None:
        self._url = url
        self._handed_off = False
        self._done = False
        self.view.load(QtCore.QUrl(url))
        self.show()
        self.raise_()
        self.activateWindow()
        # So the first keystroke lands in the page.
        self.view.setFocus()

    def finish(self) -> None:
        """Sign-in completed (the poller saw it); close without cancelling."""
        self._done = True
        self.view.stop()
        # Don't leave the provider's session UI rendered in a hidden window.
        self.view.setUrl(QtCore.QUrl("about:blank"))
        self.hide()

    def _hand_off(self, reason: str) -> None:
        if self._handed_off or self._done or not self._url:
            return
        self._handed_off = True
        _log(f"handing sign-in to the system browser ({reason})")
        webbrowser.open(self._url)
        self.finish()
        self._done = False  # polling continues; the flow is not finished yet

    def _on_load_finished(self, ok: bool) -> None:
        if self._done or self._handed_off:
            return
        if not ok:
            self._hand_off("the sign-in page did not load")
            return
        # toPlainText is asynchronous, so the check cannot be inline.
        self.view.page().toPlainText(self._check_for_refusal)

    def _check_for_refusal(self, text: str) -> None:
        if self._done or self._handed_off:
            return
        low = (text or "").lower()
        if any(marker in low for marker in _REFUSAL_MARKERS):
            self._hand_off("the provider refused the embedded view")

    def _cancel_if_active(self):
        # After a hand-off or a completed login, sign-in is still live elsewhere.
        if not self._done and not self._handed_off:
            self._done = True
            self.view.stop()
            self.cancelled.emit()

    def reject(self):
        # Escape bypasses closeEvent in QDialog.
        self._cancel_if_active()
        super().reject()

    def closeEvent(self, event):
        self._cancel_if_active()
        super().closeEvent(event)

    def shutdown(self) -> None:
        self._done = True
        self.view.stop()
        self.view.setUrl(QtCore.QUrl("about:blank"))
        self.hide()


def _log(message: str) -> None:
    try:
        import substance_painter.logging as sp_log

        sp_log.info(f"[miris] {message}")
    except ImportError:
        pass
