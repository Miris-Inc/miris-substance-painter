"""One connector for every destination: a mechanism plus an engine plus parameters.

Names no destination, mechanism or engine; they are looked up by the strings the
catalog carries.
"""

from __future__ import annotations

from . import destinations, localstate, params
from .api import AuthError
from .auth import MECHANISMS
from .engines import ENGINES
from .engines.base import Cancelled, Uploaded, UploadRequest  # re-exported for ui/

__all__ = [
    "Cancelled",
    "Connector",
    "UploadRequest",
    "Uploaded",
    "build",
]


class Connector:
    def __init__(self, entry: dict):
        self.id = entry["id"]
        self.title = entry.get("title") or self.id
        mechanism_cls = MECHANISMS[destinations.mechanism_of(entry)]
        engine_cls = ENGINES[destinations.engine_of(entry)]
        auth_block = params.with_defaults(entry["auth"], mechanism_cls.PARAMS)
        upload_block = params.with_defaults(entry["upload"], engine_cls.PARAMS)
        self.mechanism = mechanism_cls(auth_block, self.id, self.title)
        self.engine = engine_cls(upload_block, self.mechanism)
        self.engine.workspace = localstate.workspace(self.id)
        self._workspaces: list = []

    def signed_in(self) -> bool:
        return self.mechanism.signed_in()

    def sign_in(self, show_url, should_continue) -> None:
        """Runs on a worker. Calls show_url(url, code) once there is somewhere
        to send the user, then blocks until the grant lands or gives up."""
        self.mechanism.sign_in(show_url, should_continue)

    def cancel(self) -> None:
        self.mechanism.cancel()

    def sign_out(self):
        # Mechanism first: a retired session refuses to sign out, and a refused
        # sign-out must leave the persisted workspace exactly as it was.
        after = self.mechanism.sign_out()
        self._workspaces = []
        self.set_workspace("")
        return after

    def retire(self) -> None:
        """Invalidate in-flight work on plugin reload without signing out."""
        self.mechanism.retire()

    def verify(self) -> list:
        """Prove the stored credential still works and refresh the workspace list.

        The mechanism answers when it has a liveness endpoint; otherwise the
        engine's probe does. Either raises AuthError when the grant is gone.
        """
        lists_workspaces = "workspaces" in self.engine.PROVIDES
        if "verify" in self.mechanism.endpoints:
            self.mechanism.verify()
        elif self.engine.PROBE and not lists_workspaces:
            # Listing workspaces already proves the session, so probing too
            # would repeat the request.
            self.engine.probe()
        provider = None
        if lists_workspaces:
            provider = self.engine
        elif "workspaces" in self.mechanism.PROVIDES:
            provider = self.mechanism
        if provider is not None:
            self._workspaces = provider.workspaces()
            # A fixed workspace is whatever the credential says, every time.
            if self._workspaces and (self.workspace_fixed() or not self.engine.workspace):
                self.set_workspace(self._workspaces[0]["id"])
        return self._workspaces

    def workspaces(self) -> list:
        return self._workspaces if self.signed_in() else []

    def workspace(self) -> str:
        return self.engine.workspace

    def workspace_fixed(self) -> bool:
        return bool(getattr(self.mechanism, "WORKSPACE_FIXED", False))

    def set_workspace(self, value: str) -> None:
        self.engine.workspace = value
        localstate.set_workspace(self.id, value)

    def upload(self, request: UploadRequest) -> Uploaded:
        """Runs on a worker."""
        return self.engine.upload(request)

    def share_set(self, tag: str, name: str) -> str:
        """Runs on a worker. One link for a set of uploads sharing `tag`."""
        return self.engine.share_set(tag, name)

    def human_error(self, exc: Exception) -> str:
        """Sign-in failures, in the mechanism's terms."""
        if isinstance(exc, AuthError):
            return f"{self.title} did not accept the session. Sign in again."
        return self.mechanism.human_error(exc)

    def upload_error(self, exc: Exception) -> str:
        """Upload failures, in the engine's terms."""
        if isinstance(exc, AuthError):
            return f"{self.title} did not accept the session. Sign in again."
        return self.engine.human_error(exc)


def build() -> dict:
    """A connector for every catalog entry whose components exist."""
    return {
        e["id"]: Connector(e)
        for e in destinations.load()
        if destinations.mechanism_of(e) in MECHANISMS and destinations.engine_of(e) in ENGINES
    }
