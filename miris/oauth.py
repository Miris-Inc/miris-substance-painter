"""Miris device-grant sign-in: start, poll, refresh, persist.

Not drop-in RFC 8628. The paths are bespoke JSON, not a form-encoded /token
with grant_type, and poll answers HTTP 200 with {"error": "..."} where the RFC
says 400. Only the status strings follow the spec.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from . import config, localstate
from .api import AuthError, FatalError, request, request_json

PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"
DENIED = "access_denied"
EXPIRED = "expired_token"

# The server floors polling at 5s and rejects faster callers with slow_down.
MIN_INTERVAL = 5

# Refresh this far before expiry so a slow upload does not 401 mid-flight.
_EXPIRY_SKEW = 120


@dataclasses.dataclass(frozen=True)
class DeviceStart:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclasses.dataclass(frozen=True)
class Tokens:
    access: str
    refresh: str
    expires_at: float

    @property
    def stale(self) -> bool:
        return time.time() >= self.expires_at - _EXPIRY_SKEW


def _tokens_from(raw: dict) -> Tokens:
    access, refresh = raw.get("access_token"), raw.get("refresh_token")
    if not access or not refresh:
        raise FatalError("the token response was missing a token")
    return Tokens(access, refresh, time.time() + int(raw.get("expires_in") or 3600))


class TokenStore:
    """{host, access, refresh, expires_at} at 0600. A corrupt file reads as
    signed out."""

    def __init__(self, path: Path | None = None):
        self.path = path or config.state_dir() / "miris_oauth.json"

    def load(self) -> tuple[str | None, Tokens | None]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None, None
        if not isinstance(raw, dict) or not raw.get("access_token"):
            return None, None
        return raw.get("host"), Tokens(
            raw["access_token"], raw.get("refresh_token", ""), float(raw.get("expires_at") or 0)
        )

    def load_for(self, host: str) -> Tokens | None:
        """Only tokens minted by `host`, so MIRIS_AUTH_HOST pointing elsewhere cannot leak them."""
        persisted, tokens = self.load()
        return tokens if tokens and persisted == host else None

    def save(self, host: str, tokens: Tokens) -> None:
        localstate.write_private(
            self.path,
            json.dumps(
                {
                    "host": host,
                    "access_token": tokens.access,
                    "refresh_token": tokens.refresh,
                    "expires_at": tokens.expires_at,
                }
            ),
        )

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass  # already gone, or not ours to remove


class DeviceFlow:
    """Presents nothing: start() runs on a worker thread, which cannot build widgets."""

    def __init__(self, host: str, endpoints: dict, scopes: list, client_label: str):
        self.host = host.rstrip("/")
        self.endpoints = endpoints
        self.scopes = list(scopes)
        self.client_label = client_label
        self.interval = MIN_INTERVAL
        self._device_code: str | None = None

    def _url(self, name: str) -> str:
        return config.join_url(self.host, self.endpoints[name])

    def start(self) -> DeviceStart:
        raw = request_json(
            "POST",
            self._url("start"),
            json_body={"client_label": self.client_label, "scopes": self.scopes},
        )
        start = DeviceStart(
            device_code=raw["device_code"],
            user_code=raw["user_code"],
            verification_uri=config.join_url(self.host, raw["verification_uri"]),
            expires_in=int(raw.get("expires_in") or 600),
            interval=max(int(raw.get("interval") or MIN_INTERVAL), MIN_INTERVAL),
        )
        self.interval = start.interval
        self._device_code = start.device_code
        return start

    def poll_once(self) -> Tokens | None:
        """None means keep waiting. Raises once the grant can never complete."""
        if self._device_code is None:
            raise FatalError("no pending device flow")
        # Sandbox answers a pending poll with HTTP 200 and the RFC 8628 error in
        # the body; dev sends the same body with the RFC's 400.
        raw = request_json(
            "POST",
            self._url("poll"),
            json_body={"device_code": self._device_code},
            tolerate=(400,),
        )
        error = raw.get("error")
        if error == PENDING:
            return None
        if error == SLOW_DOWN:
            self.interval += MIN_INTERVAL
            return None
        if error == DENIED:
            self.cancel()
            raise AuthError("the request was denied")
        if error == EXPIRED:
            self.cancel()
            raise AuthError("the sign-in request expired")
        if error:
            self.cancel()
            raise FatalError(error)
        tokens = _tokens_from(raw)
        self.cancel()
        return tokens

    def cancel(self) -> None:
        self._device_code = None


class MirisAuth:
    """The signed-in Miris user: holds the tokens and keeps them fresh."""

    def __init__(self, store, host: str, endpoints: dict, scopes: list, client_label: str):
        self.store = store
        self.host = host.rstrip("/")
        self.endpoints = endpoints
        self.scopes = list(scopes)
        self.client_label = client_label
        self.tokens = self.store.load_for(self.host)

    def _url(self, name: str) -> str:
        return config.join_url(self.host, self.endpoints[name])

    def signed_in(self) -> bool:
        return self.tokens is not None

    def adopt(self, tokens: Tokens) -> None:
        # Save before assigning. A failed write must leave the session on the
        # pair on disk: an unpersisted refresh token becomes a replay on next
        # start, and a replay revokes the whole family.
        self.store.save(self.host, tokens)
        self.tokens = tokens

    def forget(self) -> None:
        self.tokens = None
        self.store.clear()

    def revoke(self, refresh: str) -> None:
        """Revoke the whole token family on the server.

        Takes the token because sign-out forgets the local copy first and this
        runs afterwards, off the GUI thread. Unknown tokens also get a 204, so
        retrying is safe.
        """
        request("POST", self._url("revoke"), json_body={"refresh_token": refresh})

    def flow(self) -> DeviceFlow:
        return DeviceFlow(self.host, self.endpoints, self.scopes, self.client_label)

    def authorization(self) -> dict:
        """The Bearer header, refreshed first if the access token is stale."""
        if self.tokens is None:
            raise AuthError("not signed in")
        if self.tokens.stale:
            self.refresh()
        return {"Authorization": f"Bearer {self.tokens.access}"}

    def refresh(self) -> None:
        """Rotate the pair.

        The new refresh token is persisted before use: a replayed refresh revokes
        the whole family, and the 24h grace runs from supersession rather than
        last use, so losing it to a crash signs the user out for good.
        """
        if self.tokens is None or not self.tokens.refresh:
            raise AuthError("not signed in")
        raw = request_json(
            "POST",
            self._url("refresh"),
            json_body={"refresh_token": self.tokens.refresh},
            tolerate=(400,),
        )
        if raw.get("error"):
            # RFC shape: invalid_grant with a 400. The grant is gone either way.
            raise AuthError(raw.get("error_description") or raw["error"])
        self.adopt(_tokens_from(raw))

    def check(self) -> dict:
        """Ask the server whether this grant is still live, and whose it is.

        A revoke in Miris leaves the local file and expiry untouched, so only a
        server call can tell. Raises AuthError once the grant is gone.

        The catalog's "verify" endpoint (/auth/me) needs no scope, and its
        customer_id is the workspace this token is pinned to, hence the body.
        """
        return request_json("GET", self._url("verify"), headers=self.authorization())
