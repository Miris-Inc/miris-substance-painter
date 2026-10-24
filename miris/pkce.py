"""OAuth 2.0 authorization code with PKCE, for a native client.

Knows no destination: the config carries every URL, and the credential file is
named after the destination that asked.

* PKCE (RFC 7636), S256. The code travels through the user's browser, so it is
  bound to a secret only this process has.
* Loopback redirect (RFC 8252). A Python plugin inside someone else's
  application cannot get the OS to route a custom URL scheme to it.
* Dynamic client registration (RFC 7591). A public client with no secret, so
  the plugin can ship as a file anyone can read.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import http.server
import json
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, localstate
from .api import (
    SSL_CONTEXT,
    AuthError,
    FatalError,
    TransientError,
    friendly_error,
)
from .version import USER_AGENT

# Refresh this long before expiry, so a token cannot die mid-upload.
EXPIRY_SKEW_SECONDS = 120
DEFAULT_PORT = 8765
# RFC 8252 says servers should accept any loopback port, but providers that
# check the redirect against the registration do not, so all of these get
# registered and whichever is free gets used.
PORT_CANDIDATES = (8765, 8766, 8767, 8768, 8769)


@dataclasses.dataclass(frozen=True)
class PkceConfig:
    """One authorization server. Comes from the catalog, never hardcoded here."""

    authorize_url: str
    token_url: str
    register_url: str
    scopes: tuple
    client_name: str
    client_id: str | None = None


def store_path(destination: str) -> Path:
    """One file per destination, so PKCE destinations cannot overwrite each other."""
    return config.state_dir() / f"{destination}.json"


def redirect_uri(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}/callback"


def all_redirect_uris(ports=PORT_CANDIDATES) -> list[str]:
    return [redirect_uri(port) for port in ports]


def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge), S256."""
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def new_state() -> str:
    return secrets.token_urlsafe(16)


def authorize_url(
    cfg: PkceConfig, client_id: str, redirect: str, state: str, challenge: str
) -> str:
    return (
        cfg.authorize_url
        + "?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "scope": " ".join(cfg.scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )


def register_client(cfg: PkceConfig, redirect_uris) -> str:
    """Register this installation as a public client and return its client_id.

    No secret is issued, and a lost id is re-registered rather than recovered.
    Every loopback redirect goes in, since the server checks the redirect
    against the registration.
    """
    data = _request(
        "POST",
        cfg.register_url,
        json_body={
            "client_name": cfg.client_name,
            "redirect_uris": (
                [redirect_uris] if isinstance(redirect_uris, str) else list(redirect_uris)
            ),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(cfg.scopes),
            "application_type": "native",
        },
    )
    client_id = data.get("client_id")
    if not client_id:
        raise FatalError("registration returned no client_id")
    return client_id


def exchange_code(cfg: PkceConfig, client_id: str, code: str, redirect: str, verifier: str) -> dict:
    """Authorization code -> {access_token, refresh_token, expires_in, scope}."""
    return _token_request(
        cfg,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


def refresh_tokens(cfg: PkceConfig, client_id: str, refresh_token: str) -> dict:
    return _token_request(
        cfg,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


def _token_request(cfg: PkceConfig, fields: dict) -> dict:
    data = _request(
        "POST",
        cfg.token_url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not data.get("access_token"):
        raise AuthError("the authorization server returned no access token")
    return data


@dataclasses.dataclass
class Credentials:
    client_id: str
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    # A client_id only works with the redirects it was registered with, so one
    # that predates a port has to be registered again.
    redirect_uris: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> Credentials | None:
        if not isinstance(raw, dict):
            return None
        raw.pop("workspace", None)  # older files on disk may still carry this
        if any(
            not isinstance(raw.get(key), str) or not raw[key]
            for key in ("client_id", "access_token")
        ):
            return None
        uris = raw.get("redirect_uris", ())
        if not isinstance(uris, (list, tuple)) or not all(isinstance(u, str) for u in uris):
            return None
        try:
            expires_at = float(raw.get("expires_at", 0.0))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(expires_at):
            return None
        return cls(
            client_id=str(raw["client_id"]),
            access_token=str(raw["access_token"]),
            refresh_token=str(raw.get("refresh_token") or ""),
            expires_at=expires_at,
            redirect_uris=tuple(u for u in uris if isinstance(u, str)),
        )

    @classmethod
    def from_token_response(
        cls,
        client_id: str,
        payload: dict,
        now: float,
        redirect_uris: tuple[str, ...] = (),
    ) -> Credentials:
        return cls(
            client_id=client_id,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token") or "",
            expires_at=now + float(payload.get("expires_in") or 0),
            redirect_uris=tuple(redirect_uris),
        )


def is_expired(creds: Credentials, now: float, skew: float = EXPIRY_SKEW_SECONDS) -> bool:
    """An unknown expiry (0.0) counts as expired: one wasted refresh beats a 401 mid-upload."""
    return creds.expires_at - skew <= now


class CredentialStore:
    """Absent, unreadable and malformed files all read as signed out.

    Written owner-only: a refresh token is a long-lived credential.
    """

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Credentials | None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return Credentials.from_dict(raw)

    def save(self, creds: Credentials) -> None:
        localstate.write_private(self.path, json.dumps(creds.as_dict()))

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class LoopbackReceiver:
    """Catches the OAuth redirect on 127.0.0.1 and yields the code."""

    def __init__(self, state: str, ports=PORT_CANDIDATES):
        self.state = state
        self._ports = (ports,) if isinstance(ports, int) else tuple(ports)
        self.port = self._ports[0]
        self._captured: dict[str, str] = {}
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def redirect_uri(self) -> str:
        return redirect_uri(self.port)

    def __enter__(self) -> LoopbackReceiver:
        captured = self._captured

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.urlparse(self.path).query
                found = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
                if "code" in found or "error" in found:
                    captured.update(found)
                message = (
                    "Sign-in failed. Go back to Substance Painter."
                    if found.get("error")
                    else "Signed in. You can close this tab."
                )
                body = (
                    b"<!doctype html><meta charset=utf-8>"
                    b"<body style='font:14px system-ui;padding:3em'>" + message.encode()
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # the plugin's log is not a web server access log

        # Any one port may already be taken, and "address already in use" is
        # not something the user can fix.
        last: OSError | None = None
        for port in self._ports:
            try:
                # Browsers can preconnect without sending a request. A serial
                # server would wait on that socket and never handle the redirect.
                self._server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            except OSError as e:
                last = e
                continue
            self.port = self._server.server_address[1]
            break
        if self._server is None:
            raise AuthError(
                "every port the sign-in can use is busy "
                f"({', '.join(str(p) for p in self._ports)}). "
                f"Close whatever is using them and try again ({last})"
            )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False

    def wait_for_code(
        self,
        timeout: float = 300.0,
        interval: float = 0.25,
        now=time.monotonic,
        sleep=time.sleep,
        should_continue=None,
    ) -> str:
        """Block until the redirect lands.

        `state` is checked before the code is used, or anything on loopback could
        hand us a code from another authorization.
        """
        deadline = now() + timeout
        while now() < deadline:
            if should_continue is not None and not should_continue():
                raise AuthError("sign-in cancelled")
            if self._captured:
                break
            sleep(interval)
        if not self._captured:
            raise AuthError("sign-in timed out waiting for the browser")
        if self._captured.get("error"):
            raise AuthError(f"the sign-in was refused: {self._captured['error']}")
        if self._captured.get("state") != self.state:
            raise AuthError("the sign-in reply did not match this request")
        code = self._captured.get("code")
        if not code:
            raise AuthError("the authorization server returned no code")
        return code


class PkceSession:
    """A signed-in user for one destination. Qt-free: sign_in takes a callable to open a URL."""

    def __init__(self, store: CredentialStore, cfg: PkceConfig, clock=time.time):
        self.store = store
        self.cfg = cfg
        self._clock = clock
        self._state_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._generation = 0
        self._retired = False
        self._sign_in_cancelled = threading.Event()

    def credentials(self) -> Credentials | None:
        with self._state_lock:
            return self.store.load()

    def signed_in(self) -> bool:
        return self.credentials() is not None

    def sign_out(self) -> None:
        with self._state_lock:
            self._require_active()
            self._generation += 1
            self._sign_in_cancelled.set()
            self.store.clear()

    def retire(self) -> None:
        """Invalidate this plugin generation without signing the user out.

        Reload leaves worker threads alive until their network calls return, and
        they must not write credentials or start work once a new plugin
        instance shares the store.
        """
        with self._state_lock:
            self._retired = True
            self._generation += 1
            self._sign_in_cancelled.set()

    def _require_active(self) -> None:
        if self._retired:
            raise AuthError("the session was retired")

    def cancel_sign_in(self) -> None:
        """Stop the pending browser flow without blocking on its worker."""
        with self._state_lock:
            self._sign_in_cancelled.set()

    def _clear_generation(self, generation: int) -> None:
        with self._state_lock:
            if not self._retired and generation == self._generation:
                self.sign_out()

    def access_token(self) -> str:
        # Only worker API calls wait for refresh. Sign-out uses the short state
        # lock, so the UI never waits for a network timeout.
        with self._refresh_lock:
            with self._state_lock:
                self._require_active()
                creds = self.credentials()
                generation = self._generation
            if creds is None:
                raise AuthError("not signed in")
            if is_expired(creds, self._clock()):
                creds = self._refresh(creds, generation)
            return creds.access_token

    def authorization(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token()}"}

    def _refresh(self, creds: Credentials, generation: int) -> Credentials:
        if not creds.refresh_token:
            self._clear_generation(generation)
            raise AuthError("the session expired; sign in again")
        try:
            payload = refresh_tokens(self.cfg, creds.client_id, creds.refresh_token)
        except AuthError:
            # A refresh token the server rejects would fail every call until
            # cleared by hand.
            self._clear_generation(generation)
            raise
        refreshed = Credentials.from_token_response(
            creds.client_id, payload, self._clock(), redirect_uris=creds.redirect_uris
        )
        if not refreshed.refresh_token:
            refreshed.refresh_token = creds.refresh_token  # not always reissued
        with self._state_lock:
            if generation != self._generation:
                raise AuthError("the session changed during refresh")
            self.store.save(refreshed)
        return refreshed

    def sign_in(self, open_url, *, ports=PORT_CANDIDATES, timeout: float = 300.0) -> Credentials:
        """Send the user to consent and store the result.

        A stored client_id is reused so returning users do not pile up registrations.
        """
        with self._state_lock:
            self._require_active()
            self._sign_in_cancelled.set()
            cancelled = threading.Event()
            self._sign_in_cancelled = cancelled
            self._generation += 1
            existing = self.credentials()
        wanted = tuple(all_redirect_uris(ports))
        verifier, challenge = pkce_pair()
        state = new_state()

        with LoopbackReceiver(state, ports) as receiver:
            uri = receiver.redirect_uri
            if existing:
                client_id = existing.client_id
                # The server checks the redirect against the registration, so
                # an id registered before this port existed is refused at consent.
                if uri not in existing.redirect_uris:
                    client_id = ""
            else:
                # A catalog-supplied id is used only when nothing is stored yet.
                client_id = self.cfg.client_id or ""
            if not client_id:
                client_id = register_client(self.cfg, list(wanted))
            if cancelled.is_set():
                raise AuthError("sign-in cancelled")
            open_url(authorize_url(self.cfg, client_id, uri, state, challenge))
            code = receiver.wait_for_code(
                timeout=timeout, should_continue=lambda: not cancelled.is_set()
            )

        if cancelled.is_set():
            raise AuthError("sign-in cancelled")
        payload = exchange_code(self.cfg, client_id, code, uri, verifier)
        creds = Credentials.from_token_response(
            client_id, payload, self._clock(), redirect_uris=wanted
        )
        with self._state_lock:
            if cancelled.is_set():
                raise AuthError("sign-in cancelled")
            self.store.save(creds)
        return creds


# Separate from api.py's transport because OAuth errors (RFC 6749 section 5.2)
# carry their meaning in a 400 body.
_GRANT_ERRORS = ("invalid_grant", "invalid_client", "unauthorized_client")


def oauth_error(body: bytes) -> str:
    """The message out of an OAuth error body, if there is one."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    described = payload.get("error_description") or payload.get("error") or ""
    return described if isinstance(described, str) else ""


def human_error(exc: Exception, service: str) -> str:
    """One sentence about a sign-in failure, for a window rather than a log."""
    return friendly_error(exc, service, detail_reader=oauth_error)


def _request(method: str, url: str, *, json_body=None, data=None, headers=None, timeout=60.0):
    """Decoded JSON object from the authorization server, {} if it sent none."""
    sent = {
        # Some providers' CDNs reject urllib's default agent with a 403 that
        # looks exactly like a permissions failure.
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        **(headers or {}),
    }
    body = data
    if json_body is not None:
        body = json.dumps(json_body).encode()
        sent.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=sent, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        payload = e.read()
        message = oauth_error(payload) or payload[:240].decode("utf-8", errors="replace")
        # OAuth answers an expired or revoked grant with 400, where an ordinary
        # API would use 401. Both mean the same thing to the caller.
        if e.code == 400 and oauth_error(payload) and _is_grant_error(payload):
            raise AuthError("the sign-in credentials were rejected") from e
        if e.code == 401:
            raise AuthError(f"the credentials were rejected: {message}") from e
        if e.code == 429 or e.code >= 500:
            raise TransientError(f"{_where(url)} returned {e.code}: {message}") from e
        raise FatalError(f"{_where(url)} returned {e.code}: {message}") from e
    except TimeoutError as e:
        raise TransientError(f"request to {_where(url)} timed out") from e
    except OSError as e:
        raise TransientError(f"request to {_where(url)} failed: {getattr(e, 'reason', e)}") from e
    if not payload:
        return {}
    try:
        decoded = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as e:
        raise FatalError(f"{_where(url)} did not return JSON") from e
    return decoded if isinstance(decoded, dict) else {}


def _is_grant_error(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("error") in _GRANT_ERRORS


def _where(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
