"""What every auth mechanism declares and does."""

from __future__ import annotations

from typing import ClassVar

from .. import config

# Re-exported: components read them from here, and the protocol modules from
# params, so neither side has to import the other's package.
from ..params import fill, placeholders  # noqa: F401


class NotAvailable(Exception):
    """The catalog asked a component for something it does not implement."""


class AuthMechanism:
    """One wire protocol for proving who the user is.

    Subclasses declare NAME, PARAMS, PROVIDES, RUNTIME and INTERACTIVE, and read
    every value from the catalog block they are constructed with.
    """

    NAME: ClassVar[str] = ""
    PARAMS: ClassVar[dict] = {}
    # Capabilities this component can back; the validator refuses a catalog
    # capability that neither the mechanism nor the engine provides.
    PROVIDES: ClassVar[frozenset] = frozenset()
    # Placeholders this component fills per request; any other in a URL is refused.
    RUNTIME: ClassVar[frozenset] = frozenset()
    # True sends the user to a browser. The validator refuses an available
    # destination whose mechanism is not, and every mechanism sets it itself.
    INTERACTIVE: ClassVar[bool] = False
    # True when the credential is pinned to one workspace: the picker shows it
    # and cannot change it; only a new sign-in can.
    WORKSPACE_FIXED: ClassVar[bool] = False

    # Shared by every destination on the protocol: `destination` keys what it
    # stores, and `title` is the name its messages use.
    def __init__(self, block: dict, destination: str = "", title: str = ""):
        self.block = block
        self.destination = destination
        self.title = title
        self.host = block["host"].rstrip("/")
        self.endpoints = dict(block.get("endpoints") or {})

    def url(self, name: str, **values) -> str:
        return fill(config.join_url(self.host, self.endpoints[name]), **values)

    def signed_in(self) -> bool:
        raise NotImplementedError

    def sign_in(self, show_url, should_continue) -> None:
        """Runs on a worker. Calls show_url(url, code) once there is somewhere
        to send the user, then blocks until the grant lands or gives up."""
        raise NotImplementedError

    def cancel(self) -> None:
        pass

    def sign_out(self):
        """Forget the local credential now. May return a callable that tells the
        server; the manager runs it off the GUI thread and only logs a failure."""
        raise NotImplementedError

    def retire(self) -> None:
        """Invalidate in-flight work on plugin reload without signing out."""

    def verify(self) -> None:
        """Prove the stored credential is still live, or raise AuthError.
        Mechanisms with no liveness endpoint leave this to the engine's probe."""

    def workspaces(self) -> list:
        """[{id, name}] when the credential itself decides the workspace; the
        engine lists them otherwise. Declared through PROVIDES like an engine."""
        return []

    def authorization(self) -> dict:
        """Headers that authenticate a request, refreshed first if needed."""
        raise NotImplementedError

    def human_error(self, exc: Exception) -> str:
        raise NotImplementedError
