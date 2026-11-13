"""Authorization code with PKCE for any standard authorization server.

Every value comes from the catalog block and every mechanic from `pkce.py`.
"""

from __future__ import annotations

from typing import ClassVar

from .. import config, pkce
from . import register
from .base import AuthMechanism, NotAvailable


@register
class OAuth2Pkce(AuthMechanism):
    NAME: ClassVar[str] = "oauth2_pkce"
    PARAMS: ClassVar[dict] = {
        "endpoints": {"authorize": str, "token": str, "register": (str, "")},
        "scopes": [str],
        "redirect": {"loopback"},
        "client_id": str | None,
        "client_name": (str, "Miris for Adobe Substance 3D Painter"),
    }
    # OAuth has no liveness endpoint; the engine's probe covers it. Workspaces
    # are the engine's too.
    PROVIDES: ClassVar[frozenset] = frozenset()
    RUNTIME: ClassVar[frozenset] = frozenset()
    INTERACTIVE: ClassVar[bool] = True

    def __init__(self, block: dict, destination: str = "", title: str = "", store=None):
        super().__init__(block, destination, title)
        if block["redirect"] != "loopback":
            raise NotAvailable(
                f"redirect {block['redirect']!r} is not implemented; only loopback is"
            )
        endpoints = self.endpoints
        cfg = pkce.PkceConfig(
            authorize_url=config.join_url(self.host, endpoints["authorize"]),
            token_url=config.join_url(self.host, endpoints["token"]),
            register_url=(
                config.join_url(self.host, endpoints["register"])
                if endpoints.get("register")
                else ""
            ),
            scopes=tuple(block["scopes"]),
            client_name=block["client_name"],
            client_id=block["client_id"],
        )
        self.session = pkce.PkceSession(
            store or pkce.CredentialStore(pkce.store_path(self.destination)), cfg
        )

    def signed_in(self) -> bool:
        return self.session.signed_in()

    def sign_in(self, show_url, should_continue) -> None:
        self.session.sign_in(lambda url: show_url(url, ""))

    def cancel(self) -> None:
        self.session.cancel_sign_in()

    def sign_out(self) -> None:
        # No revocation: it is optional in the standard and Playbook offers none,
        # so the grant stays in the user's account until removed there.
        self.session.sign_out()

    def retire(self) -> None:
        self.session.retire()

    def authorization(self) -> dict:
        return self.session.authorization()

    def human_error(self, exc: Exception) -> str:
        return pkce.human_error(exc, self.title or "the service")
