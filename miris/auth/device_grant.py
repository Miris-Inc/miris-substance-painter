"""The device grant as the Miris backend implements it. Not drop-in RFC 8628; see oauth.py."""

from __future__ import annotations

import time
from typing import ClassVar

from .. import config, oauth
from ..api import AuthError
from ..engines.base import Cancelled
from . import register
from .base import AuthMechanism


@register
class DeviceGrant(AuthMechanism):
    NAME: ClassVar[str] = "device_grant"
    PARAMS: ClassVar[dict] = {
        "endpoints": {"start": str, "poll": str, "refresh": str, "verify": str, "revoke": str},
        "scopes": [str],
        "client_label": (str, "Substance 3D Painter"),
    }
    # The token is pinned to the workspace chosen at consent. It is provided so
    # the window can show it; only a new sign-in can pick another.
    PROVIDES: ClassVar[frozenset] = frozenset({"workspaces"})
    RUNTIME: ClassVar[frozenset] = frozenset()
    INTERACTIVE: ClassVar[bool] = True
    WORKSPACE_FIXED: ClassVar[bool] = True

    def __init__(
        self,
        block: dict,
        destination: str = "",
        title: str = "",
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        super().__init__(block, destination, title)
        # MIRIS_AUTH_HOST beats the catalog host so a build can target another
        # deploy. Tokens are stored against the host that minted them.
        self.host = config.resolve_host(self.host)
        self.auth = oauth.MirisAuth(
            oauth.TokenStore(),
            self.host,
            self.endpoints,
            block["scopes"],
            block["client_label"],
        )
        self._sleep = sleep
        self._clock = clock
        self._flow: oauth.DeviceFlow | None = None
        self._workspace: dict | None = None

    def signed_in(self) -> bool:
        return self.auth.signed_in()

    def sign_in(self, show_url, should_continue) -> None:
        flow = self.auth.flow()
        self._flow = flow
        start = flow.start()
        show_url(start.verification_uri, start.user_code)

        deadline = self._clock() + start.expires_in
        while self._clock() < deadline:
            if not should_continue():
                raise Cancelled("sign-in cancelled")
            # Sleep first: the user has not had time to consent yet, and the
            # server rejects anything faster than its interval with slow_down.
            self._sleep(flow.interval)
            if not should_continue():
                raise Cancelled("sign-in cancelled")
            tokens = flow.poll_once()
            if tokens is not None:
                self.auth.adopt(tokens)
                return
        raise AuthError("the sign-in request expired")

    def cancel(self) -> None:
        if self._flow is not None:
            self._flow.cancel()

    def sign_out(self):
        tokens = self.auth.tokens
        self.auth.forget()
        self._workspace = None
        if tokens is None or not tokens.refresh:
            return None
        # Captured before forget(): the server call runs later and needs it.
        return lambda: self.auth.revoke(tokens.refresh)

    def verify(self) -> None:
        # Always a round trip. A token revoked in Miris stays unexpired locally
        # for up to an hour, so the clock cannot answer this.
        me = self.auth.check() or {}
        self._workspace = _pinned_workspace(me)

    def workspaces(self) -> list:
        return [self._workspace] if self._workspace else []

    def authorization(self) -> dict:
        return self.auth.authorization()

    def human_error(self, exc: Exception) -> str:
        from .. import api

        return api.friendly_error(exc, service=self.title or "Miris", host=self.host)


def _pinned_workspace(me: dict) -> dict | None:
    """/auth/me's top-level customer_id is the workspace a bearer session runs
    as; the name comes from the membership list, or the id when it is absent."""
    pinned = me.get("customer_id") or ""
    if not pinned:
        return None
    for w in me.get("workspaces") or []:
        if w.get("customer_id") == pinned:
            return {"id": pinned, "name": w.get("name") or pinned}
    return {"id": pinned, "name": pinned}
