"""HTTP core: the error taxonomy every network module shares, plus TLS.

Failures are classified three ways so callers can decide re-login vs retry vs
give-up: AuthError, TransientError, FatalError.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .version import USER_AGENT

_SNIPPET = 240


class ApiError(Exception):
    pass


class AuthError(ApiError):
    """Not authenticated: 401, or a redirect to a login page."""


class TransientError(ApiError):
    """Network failure or 5xx, safe to retry."""


class Cancelled(ApiError):
    """The user closed the sign-in window, pressed cancel, or stopped an upload.

    Lives here because every layer recognises it, including ones the engines import.
    """


class FatalError(ApiError):
    """Non-retryable: 4xx, undecodable body, HTML where JSON was expected."""


# Painter's bundled OpenSSL is compiled with CA paths from Adobe's build machine
# (/opt/alg/openssl-.../ssl/cert.pem), absent on user machines, so
# create_default_context() loads ZERO anchors and every request fails
# CERTIFICATE_VERIFY_FAILED. Windows is unaffected: it reads the system store.
_FALLBACK_CA_FILES = (
    "/etc/ssl/cert.pem",  # macOS
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
)


def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ctx.get_ca_certs():
        return ctx
    for path in _FALLBACK_CA_FILES:
        if os.path.isfile(path):
            try:
                ctx.load_verify_locations(path)
            except (ssl.SSLError, OSError):
                pass  # a verification failure later surfaces as TransientError
            break
    return ctx


SSL_CONTEXT = build_ssl_context()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx from the API means "not authenticated"; following it yields HTML."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=SSL_CONTEXT))


def detail_of(body: bytes) -> str:
    """The human-readable message out of an error envelope, if there is one."""
    try:
        raw = json.loads(body.decode("utf8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if isinstance(raw, dict):
        for key in ("detail", "error_description", "title", "message", "error"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def classify(status: int, body: bytes, url: str = "") -> ApiError:
    detail = detail_of(body)
    if status in (301, 302, 303, 307, 308):
        return AuthError(detail or "the session was not accepted")
    if status in (401, 403):
        return AuthError(detail or "the session was not accepted")
    if status == 429 or status >= 500:
        return TransientError(detail or f"the server returned {status}")
    return FatalError(detail or f"the server returned {status}")


def request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_body=None,
    query: dict | None = None,
    timeout: float = 30.0,
    tolerate: tuple[int, ...] = (),
) -> bytes:
    """`tolerate` names statuses whose body is an answer, not a failure: an
    RFC 8628 poll says "still waiting" with a 400."""
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
    sent = {"Accept": "application/json", "User-Agent": USER_AGENT}
    sent.update(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        sent["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=sent, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            if response.status in (301, 302, 303, 307, 308):
                raise AuthError("the session was not accepted")
            return response.read()
    except urllib.error.HTTPError as error:
        body = error.read()
        if error.code in tolerate:
            return body
        raise classify(error.code, body, url) from error
    except urllib.error.URLError as error:
        raise TransientError(f"could not reach {url}: {error.reason}") from error
    except TimeoutError as error:
        raise TransientError(f"{url} timed out") from error
    except OSError as error:
        # A dropped connection mid-request is the same class of fault as a 5xx.
        raise TransientError(str(error)) from error


def request_json(method: str, url: str, **kwargs) -> dict:
    body = request(method, url, **kwargs)
    try:
        return json.loads(body.decode("utf8"))
    except (ValueError, UnicodeDecodeError) as error:
        snippet = body[:_SNIPPET].decode("utf8", errors="replace")
        raise FatalError(f"{url} did not return JSON; body starts: {snippet}") from error


def friendly_error(
    exc: Exception, service: str, detail_reader=None, host: str | None = None
) -> str:
    """One sentence for a window. The full text still goes to the log.

    `detail_reader` turns a raw body into a sentence when one leaks through
    instead of a parsed message; each API has its own error envelope.
    """
    if isinstance(exc, AuthError):
        return f"{service} did not accept the session. Sign in again."
    if isinstance(exc, TransientError):
        return f"Could not reach {service}. Check your connection and try again."
    text = " ".join(str(exc).split())
    match = re.search(r"returned (\d{3})(?::\s*(.+))?", text)
    if match:
        status, detail = match.group(1), match.group(2) or ""
        # Naming the host is what makes "404" legible: the service exists, this
        # build is pointed at a deploy that does not have it.
        where = f"{service} at {urllib.parse.urlsplit(host).netloc}" if host else service
        if detail.startswith(("{", "[")):
            detail = detail_reader(detail.encode()) if detail_reader else ""
            if detail.startswith(("{", "[")):
                detail = ""
        if len(detail) > 140:
            detail = detail[:137].rstrip() + "…"
        if detail:
            return f"{where} refused the request ({status}): {detail}"
        hint = f" Check which {service} this build points at." if host and status == "404" else ""
        return f"{where} refused the request ({status}).{hint}"
    return text[:200] or f"Something went wrong with {service}."
