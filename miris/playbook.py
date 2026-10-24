"""Playbook (playbook.com) as an upload destination.

A partner DAM. Uploading a USDZ there produces a stream on their side.

Not in Playbook's documentation:

1. Cloudflare answers the default urllib User-Agent with 1010 "Access denied"
   on both api. and be.playbook.com, which reads as an auth failure. Every
   request here carries USER_AGENT.
2. The documented "PUT the bytes to upload_url" does not work for the gcs
   provider. It is a resumable upload: POST an empty body with the x-goog-*
   headers, then PUT to the session URL that comes back in Location.
3. A 3D asset never sets is_skeleton or display_url, even fully processed.
   Polling for `is_skeleton is False`, as the docs prescribe, waits forever on
   a healthy USDZ. Thumbnails are the signal that works for images and 3D.

Sign-in is standard OAuth 2.0 with PKCE and lives in `pkce.py`. Qt-free.
"""

from __future__ import annotations

import dataclasses
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .api import (
    SSL_CONTEXT,
    AuthError,
    Cancelled,
    FatalError,
    TransientError,
    friendly_error,
)
from .params import fill
from .version import USER_AGENT


@dataclasses.dataclass(frozen=True)
class PlaybookConfig:
    """One Playbook deployment's API, from the catalog. No authorization server:
    signing in is `pkce.py`'s job."""

    api_host: str
    chunk_bytes: int
    retries: int
    workspaces_path: str
    upload_prepare_path: str
    upload_complete_path: str
    tags_path: str
    share_path: str

    def api_url(self, template: str, **values) -> str:
        return self.api_host.rstrip("/") + fill(template, **values)


# What Playbook stores a USD archive as; it echoes this back on the asset.
USDZ_MEDIA_TYPE = "model/vnd.usdz+zip"

_MEDIA_TYPES = {
    ".usdz": USDZ_MEDIA_TYPE,
    ".usd": "model/vnd.usd",
    ".usda": "model/vnd.usd",
    ".usdc": "model/vnd.usd",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def media_type_for(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def human_error(exc: Exception) -> str:
    """One sentence about a Playbook failure, for a window rather than a log."""
    return friendly_error(exc, "Playbook", detail_reader=_error_message)


class PlaybookApi:
    """Authenticated calls against one Playbook deployment."""

    def __init__(self, headers, cfg: PlaybookConfig, timeout: float = 60.0):
        # A callable, not a token: the mechanism refreshes on demand, so every
        # request gets a header that is valid at the moment it is sent.
        self._auth_headers = headers
        self.cfg = cfg
        self.timeout = timeout

    def organizations(self) -> list[dict]:
        """Workspaces this user can upload to."""
        return self._json("GET", self.cfg.api_url(self.cfg.workspaces_path)).get("data") or []

    def asset(self, slug: str, token: str) -> dict:
        url = self.cfg.api_url(self.cfg.tags_path, workspace=slug, asset_token=token)
        return self._json("GET", url).get("data") or {}

    def upload(
        self,
        slug: str,
        file_path: Path,
        *,
        title: str | None = None,
        description: str | None = None,
        board_token: str | None = None,
        on_bytes=None,
        should_continue=None,
    ) -> dict:
        """Upload one file and return the created asset.

        Playbook stores single files, so a USD export must already be one USDZ.
        `should_continue` is polled between chunks, the one point where stopping
        is clean; False raises Cancelled.
        """
        payload = file_path.read_bytes()
        title = title or file_path.name
        media_type = media_type_for(file_path)

        prepared = (
            self._json(
                "POST",
                self.cfg.api_url(self.cfg.upload_prepare_path, workspace=slug),
                json_body={
                    "asset": {"title": title, "media_type": media_type, "size": len(payload)}
                },
            ).get("data")
            or {}
        )

        provider = prepared.get("storage_provider")
        if prepared.get("parts"):
            self._put_parts(prepared, payload, on_bytes, should_continue)
        elif provider == "gcs":
            self._put_resumable(prepared, payload, media_type, on_bytes, should_continue)
        else:
            # Backblaze single-part: the signed URL covers these headers, so
            # they are not optional the way they look.
            _request(
                "PUT",
                prepared["upload_url"],
                data=payload,
                headers={
                    "Content-Type": media_type,
                    "x-amz-meta-encrypted-organization-metadata": prepared.get(
                        "encrypted_organization_metadata"
                    )
                    or "",
                    "x-amz-meta-extension": prepared.get("file_extension") or "",
                },
                timeout=self.timeout,
            )
        completion = {
            "signed_gcs_id": prepared["signed_gcs_id"],
            "title": title,
            "media_type": media_type,
            "size": len(payload),
        }
        if prepared.get("multipart_upload_id"):
            completion["multipart_upload_id"] = prepared["multipart_upload_id"]
        if description:
            completion["description"] = description
        if board_token:
            completion["board_token"] = board_token
        return (
            self._json(
                "POST",
                self.cfg.api_url(self.cfg.upload_complete_path, workspace=slug),
                json_body={"asset": completion},
            ).get("data")
            or {}
        )

    def share_link(self, slug: str, token: str) -> str:
        """A public URL for an asset, or "" if none came back.

        `permalink` is null on creation and stays null, so a link has to be
        asked for. Not add_permalinks: it mints links on permalinks.playbook.com,
        which serves an img.playbook.com certificate that expired in November
        2022 and every browser refuses. The share endpoint returns a
        www.playbook.com URL that loads.
        """
        url = self.cfg.api_url(self.cfg.share_path, workspace=slug, asset_token=token)
        reply = self._json("POST", url, json_body={})
        data = reply.get("data")
        if isinstance(data, dict) and data.get("url"):
            return data["url"]
        return reply.get("url") or ""

    def set_tags(self, slug: str, token: str, tags: list[str]) -> list[str]:
        """Replace an asset's tags and return what stuck.

        Not change_tags: it answers 200 and discards its input. The update
        endpoint applies tags as an ordinary attribute, but only wrapped in
        {"asset": ...}; a bare {"tags": [...]} gets a 406. Read back because a
        200 here proves nothing.
        """
        url = self.cfg.api_url(self.cfg.tags_path, workspace=slug, asset_token=token)
        self._json("PATCH", url, json_body={"asset": {"tags": tags}})
        return self.asset(slug, token).get("tags") or []

    def _put_resumable(
        self, prepared: dict, payload: bytes, media_type: str, on_bytes=None, should_continue=None
    ) -> None:
        """Send the body to a resumable session, a chunk at a time.

        A single PUT of a 24 MB archive died with a broken pipe from inside
        Painter; something in front of the storage proxy drops the connection.
        Each chunk is answered 308 with a Range saying what landed, so a drop
        resumes from there instead of starting over.
        """
        status, headers, _ = _request(
            "POST",
            prepared["upload_url"],
            data=b"",
            headers={
                "Content-Type": media_type,
                "x-goog-resumable": "start",
                "x-goog-meta-encrypted-organization-metadata": prepared.get(
                    "encrypted_organization_metadata"
                )
                or "",
                "x-goog-meta-extension": prepared.get("file_extension") or "",
            },
            timeout=self.timeout,
        )
        session = headers.get("location")
        if not session:
            raise FatalError(f"storage opened no upload session (HTTP {status}); cannot upload")

        total = len(payload)
        sent = 0
        attempts = 0
        needs_status = False
        while sent < total:
            if should_continue is not None and not should_continue():
                raise Cancelled("upload cancelled by the user")
            end = min(sent + self.cfg.chunk_bytes, total) - 1
            try:
                if needs_status:
                    committed = self._session_offset(session, total)
                else:
                    status, headers, _ = _request(
                        "PUT",
                        session,
                        data=payload[sent : end + 1],
                        headers={
                            "Content-Type": media_type,
                            "Content-Range": f"bytes {sent}-{end}/{total}",
                        },
                        timeout=self.timeout,
                    )
                    committed = _upload_offset(status, headers, total)
            except TransientError:
                # Whether any of that chunk landed is the server's to say, so ask.
                attempts += 1
                if attempts > self.cfg.retries:
                    raise
                needs_status = True
                continue
            # Only acknowledged progress resets the budget. A reachable server
            # repeatedly accepting no bytes must not keep the worker alive forever.
            attempts = 0 if committed > sent else attempts + 1
            if attempts > self.cfg.retries:
                raise TransientError("storage made no progress receiving the upload")
            sent = committed
            needs_status = False
            if on_bytes:
                on_bytes(sent)

    def _session_offset(self, session: str, total: int) -> int:
        """How many bytes the session already holds, per its Range header."""
        status, headers, _ = _request(
            "PUT",
            session,
            data=b"",
            headers={"Content-Range": f"bytes */{total}"},
            timeout=self.timeout,
        )
        return _upload_offset(status, headers, total)

    def _put_parts(
        self, prepared: dict, payload: bytes, on_bytes=None, should_continue=None
    ) -> None:
        """Backblaze multipart: one PUT per part, in order, no extra headers."""
        part_size = prepared.get("part_size") or len(payload)
        sent = 0
        for part in prepared["parts"]:
            if should_continue is not None and not should_continue():
                raise Cancelled("upload cancelled by the user")
            number = part.get("part_number", 1)
            chunk = payload[(number - 1) * part_size : number * part_size]
            _request("PUT", part["url"], data=chunk, timeout=self.timeout)
            sent += len(chunk)
            if on_bytes:
                on_bytes(sent)

    def _headers(self) -> dict:
        return {**self._auth_headers(), "Accept": "application/json"}

    def _request_checked(self, method, url, *, json_body=None):
        return _request(
            method,
            url,
            json_body=json_body,
            headers=self._headers(),
            timeout=self.timeout,
            is_api=True,
        )

    def _json(self, method, url, *, json_body=None) -> dict:
        # Every caller builds its URL through cfg.api_url, so a path template
        # cannot reach the wire with a {placeholder} still in it.
        _, _, body = self._request_checked(method, url, json_body=json_body)
        return _decode(body, url)


def _upload_offset(status: int, headers: dict, total: int) -> int:
    if status in (200, 201):
        return total
    if status != 308:
        raise FatalError(f"storage returned unexpected upload status {status}")
    committed = headers.get("range", "")
    if not committed:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", committed)
    if match and int(match[1]) < total:
        return int(match[1]) + 1
    raise FatalError("storage returned an invalid upload range")


def _decode(body: bytes, url: str) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise FatalError(f"{url} returned a body that is not JSON") from e
    return data if isinstance(data, dict) else {"data": data}


def _error_message(body: bytes) -> str:
    """Playbook wraps errors as {errors:[{message, extensions:{code}}]}."""
    try:
        payload = json.loads(body)
        errors = payload.get("errors") or []
        if errors:
            first = errors[0]
            code = (first.get("extensions") or {}).get("code")
            return f"{first.get('message', '')}{f' ({code})' if code else ''}"
    except (ValueError, AttributeError, UnicodeDecodeError):
        pass
    return body[:240].decode("utf-8", errors="replace")


def _request(method, url, *, json_body=None, data=None, headers=None, timeout=60.0, is_api=False):
    """Returns (status, lowercase headers, body).

    `is_api` marks a Playbook endpoint, whose URL and error body are safe to
    log. Storage URLs are signed, so by default they are redacted.
    """
    parsed = urllib.parse.urlsplit(url)
    # Storage session URLs authorize uploads by themselves, and errors reach
    # the log and exported diagnostics. Drop the path, query, echoed body and
    # any chained exception that could repeat them.
    target = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path if is_api else "", "", "")
    )
    sent = {
        # Cloudflare 1010-rejects urllib's default agent before Playbook sees
        # the request, as a 403 that looks like a permissions problem.
        "User-Agent": USER_AGENT,
        **(headers or {}),
    }
    body = data
    if json_body is not None:
        body = json.dumps(json_body).encode()
        sent.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=sent, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            return (resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as e:
        payload = e.read()
        # 308 is a resumable session saying "keep going". urllib raises it as
        # an unfollowed redirect, so hand it back as a result.
        if e.code == 308:
            return 308, {k.lower(): v for k, v in e.headers.items()}, payload
        message = _error_message(payload) if is_api else "storage refused the upload"
        cause = e if is_api else None
        if e.code == 400 and is_api:
            # OAuth rejects expired or revoked refresh tokens with 400, not 401.
            try:
                token_error = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                token_error = {}
            if isinstance(token_error, dict) and token_error.get("error") in (
                "invalid_grant",
                "invalid_client",
                "unauthorized_client",
            ):
                raise AuthError("Playbook rejected the sign-in credentials") from e
        if e.code == 401:
            raise AuthError(f"Playbook rejected the credentials: {message}") from cause
        if e.code == 402:
            raise FatalError(f"Playbook quota exhausted: {message}") from cause
        if e.code == 429 or e.code >= 500:
            raise TransientError(f"{target} returned {e.code}: {message}") from cause
        raise FatalError(f"{target} returned {e.code}: {message}") from cause
    except TimeoutError as e:
        raise TransientError(f"request to {target} timed out") from (e if is_api else None)
    except OSError as e:
        # A connection dropped mid-body can arrive as a bare BrokenPipeError or
        # SSLEOFError rather than URLError.
        reason = getattr(e, "reason", e) if is_api else type(e).__name__
        raise TransientError(f"request to {target} failed: {reason}") from (e if is_api else None)
