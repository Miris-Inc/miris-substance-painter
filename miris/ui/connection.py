"""The sign-in lifecycle, shared by every destination that signs in."""

from __future__ import annotations

from PySide6 import QtCore

from ..connectors import Cancelled


class ConnectionManager(QtCore.QObject):
    """Owns no widgets: the host builds the login window on the GUI thread,
    because `sign_in` runs on a worker."""

    # (destination, url, user_code)
    show_login = QtCore.Signal(str, str, str)
    # Sent however an attempt ends. A login window left open gets closed by
    # hand, which reports a cancellation over a sign-in that worked.
    hide_login = QtCore.Signal()
    changed = QtCore.Signal(str)

    def __init__(self, connectors: dict, spawn, log, parent=None):
        super().__init__(parent)
        self._connectors = connectors
        self._spawn = spawn
        self._log = log
        self._generation = dict.fromkeys(connectors, 0)
        self._connecting: set[str] = set()
        self._verifying: set[str] = set()
        # Signed out by the server, not the user: same control, different message.
        self._expired: set[str] = set()
        # destination -> (text, tone), tone "error" or "note".
        self._messages: dict[str, tuple[str, str]] = {}
        self._shutting_down = False

    def get(self, destination: str):
        return self._connectors.get(destination)

    def __contains__(self, destination: str) -> bool:
        return destination in self._connectors

    def signed_in(self, destination: str) -> bool:
        connector = self.get(destination)
        return bool(connector and connector.signed_in())

    def connecting(self, destination: str) -> bool:
        return destination in self._connecting

    def message(self, destination: str) -> str:
        return self._messages.get(destination, ("", "note"))[0]

    def clear_message(self, destination: str | None = None) -> None:
        if destination is None:
            self._messages.clear()
        else:
            self._messages.pop(destination, None)

    def _say(self, destination: str, text: str, tone: str = "note") -> None:
        if text:
            self._messages[destination] = (text, tone)
        else:
            self._messages.pop(destination, None)

    def expired(self, destination: str) -> bool:
        return destination in self._expired

    def snapshot(self) -> dict:
        return {
            name: {
                "signedIn": c.signed_in(),
                "connecting": name in self._connecting,
                "expired": name in self._expired,
                "workspaces": c.workspaces(),
                "workspace": c.workspace(),
                "workspaceFixed": c.workspace_fixed(),
                "message": self._messages.get(name, ("", "note"))[0],
                "tone": self._messages.get(name, ("", "note"))[1],
            }
            for name, c in self._connectors.items()
        }

    def sign_in(self, destination: str) -> None:
        connector = self.get(destination)
        if connector is None or destination in self._connecting:
            return
        self._generation[destination] += 1
        generation = self._generation[destination]
        self._verifying.discard(destination)
        self._expired.discard(destination)
        # Tracked here, not in the page, because only Python learns when it ends.
        self._connecting.add(destination)
        self._say(destination, "")
        self.changed.emit(destination)

        def current():
            return generation == self._generation[destination] and not self._shutting_down

        def show(url, code):
            if current():
                self.show_login.emit(destination, url, code)

        self._spawn(
            lambda: connector.sign_in(show, current),
            lambda _result: self._signed_in(destination, generation),
            lambda exc: self._failed(destination, generation, exc),
        )

    def cancel(self, destination: str) -> None:
        """The user closed the login window or pressed cancel."""
        connector = self.get(destination)
        if connector is None:
            return
        if connector.signed_in():
            # Closing the window after the grant landed is not a cancellation.
            self.hide_login.emit()
            return
        self._generation[destination] += 1
        self._connecting.discard(destination)
        connector.cancel()
        title = self._title(destination)
        self._log(f"{title} sign-in closed before it finished")
        self._say(destination, f"{title} sign-in cancelled.")
        self.changed.emit(destination)

    def sign_out(self, destination: str) -> None:
        connector = self.get(destination)
        if connector is None:
            return
        self._generation[destination] += 1
        self._connecting.discard(destination)
        self._verifying.discard(destination)
        self._expired.discard(destination)
        after = connector.sign_out()
        self.changed.emit(destination)
        if after is None:
            return
        # The local sign-out stands; a server that can't be told is only logged.
        title = self._title(destination)
        self._spawn(
            after,
            lambda _result: None,
            lambda exc: self._log(f"{title} sign-out: the server was not told: {exc}", error=True),
        )

    def set_workspace(self, destination: str, value: str) -> None:
        connector = self.get(destination)
        if connector is not None:
            connector.set_workspace(value)
            self.changed.emit(destination)

    def _signed_in(self, destination: str, generation: int) -> None:
        if generation != self._generation[destination]:
            return
        self._connecting.discard(destination)
        self._expired.discard(destination)
        self._say(destination, "")
        self.hide_login.emit()
        self._log(f"signed in to {self._title(destination)}")
        self.changed.emit(destination)
        self.verify(destination)

    def _failed(self, destination: str, generation: int, exc: Exception) -> None:
        if generation != self._generation[destination]:
            return
        self._connecting.discard(destination)
        self.hide_login.emit()
        if isinstance(exc, Cancelled):
            self._say(destination, f"{self._title(destination)} sign-in cancelled.")
        else:
            self._log(f"{self._title(destination)} sign-in failed: {exc}", error=True)
            self._say(destination, self.get(destination).human_error(exc), "error")
        self.changed.emit(destination)

    def verify(self, destination: str) -> None:
        """A credential revoked on the server leaves the local file intact, so
        ask the service before an export."""
        connector = self.get(destination)
        if connector is None or not connector.signed_in():
            return
        if destination in self._verifying or destination in self._connecting:
            return
        self._verifying.add(destination)
        generation = self._generation[destination]
        self._spawn(
            connector.verify,
            lambda _result: self._verified(destination, generation),
            lambda exc: self._verify_failed(destination, generation, exc),
        )

    def verify_all(self) -> None:
        for destination in self._connectors:
            self.verify(destination)

    def _verified(self, destination: str, generation: int) -> None:
        if generation != self._generation[destination]:
            return
        self._verifying.discard(destination)
        self.changed.emit(destination)

    def _verify_failed(self, destination: str, generation: int, exc: Exception) -> None:
        from ..api import AuthError

        if generation != self._generation[destination]:
            return
        self._verifying.discard(destination)
        if isinstance(exc, AuthError):
            title = self._title(destination)
            self._log(f"the stored {title} session is no longer valid")
            self._expired.add(destination)
            self._say(destination, f"Your {title} session expired. Sign in again to publish there.")
            self.get(destination).sign_out()
            self.changed.emit(destination)
            return
        # Offline is not signed out; an upload will report it if one is tried.
        self._log(f"could not check the {self._title(destination)} session: {exc}", error=True)

    def retire(self) -> None:
        self._shutting_down = True
        for destination, connector in self._connectors.items():
            self._generation[destination] += 1
            connector.retire()

    def _title(self, destination: str) -> str:
        from .. import destinations

        entry = destinations.by_id(destination) or {}
        return entry.get("title") or destination
