"""Uploading a packed .usdz to Miris with a device-grant Bearer token.

The calls, with the file in the middle, which is why sigv4.py and s3.py exist:

    POST /v1/asset                 register it, and take temporary S3 credentials
    PUT  <s3>/<upload_path>/...    the file, signed with those
    PUT  /v1/asset/upload/{id}     tell the API the upload finished
    POST /v1/asset-viewer-key      mint a key that can stream one tag, for a set

Tags go in with the first call, so an asset is never in Miris untagged.

No ?workspace= is sent. The token is pinned to one workspace, which the
middleware applies when the parameter is absent and 401s on when it differs.
"""

from __future__ import annotations

import dataclasses
import json

from . import config
from .api import FatalError, request, request_json
from .params import fill


@dataclasses.dataclass(frozen=True)
class CreateAssetResult:
    """What POST /v1/asset hands back: an id and one-shot S3 credentials."""

    id: str
    endpoint_url: str
    region: str
    access_key_id: str
    secret_key: str
    session_token: str
    upload_path: str


_REQUIRED = ("id", "endpoint_url", "access_key_id", "secret_key", "session_token", "upload_path")


class MirisUpload:
    def __init__(self, auth, host: str, endpoints: dict, options: dict, timeout: float = 30.0):
        self.auth = auth
        self.host = host.rstrip("/")
        self.endpoints = endpoints
        self.options = options
        self.timeout = timeout

    def _url(self, name: str, **values) -> str:
        return fill(config.join_url(self.host, self.endpoints[name]), **values)

    def create_asset(
        self,
        *,
        name: str,
        tags: list,
        content_path: str,
        file_count: int,
        total_bytes: int,
        streamable: bool = False,
    ) -> CreateAssetResult:
        body = {
            "input_type": self.options["input_type"],
            "output_type": self.options["output_type"],
            "name": name,
            "tags": tags,
            "content_path": content_path,
            "file_count": file_count,
            "total_bytes": total_bytes,
        }
        # Only when asked: the flag bypasses the preview recipe and spends a
        # generation credit, so the default body must not carry it.
        if streamable:
            body["streamable"] = True
        raw = request_json(
            "POST",
            self._url("register"),
            headers=self.auth.authorization(),
            json_body=body,
            timeout=self.timeout,
        )
        missing = [k for k in _REQUIRED if not raw.get(k)]
        if missing:
            raise FatalError(f"the asset was created without {', '.join(missing)}")
        return CreateAssetResult(
            id=raw["id"],
            endpoint_url=raw["endpoint_url"],
            region=raw.get("region") or "us-east-1",
            access_key_id=raw["access_key_id"],
            secret_key=raw["secret_key"],
            session_token=raw["session_token"],
            upload_path=raw["upload_path"],
        )

    def finalize_upload(self, asset_id: str, ok: bool, *, manifest=None) -> dict:
        """Start the pipeline, or mark the asset failed so it does not wait forever.

        `manifest` lists what was PUT, as [{"file_name", "size"}]. The API checks
        it against storage, so a truncated or misplaced upload is refused here
        with a reason. Returns the API's view of the upload; see rejection().
        """
        body: dict = {"status": "completed" if ok else "failed"}
        if ok and manifest:
            body["manifest"] = list(manifest)
        raw = request(
            "PUT",
            self._url("finalize", asset_id=asset_id),
            headers=self.auth.authorization(),
            json_body=body,
            timeout=self.timeout,
        )
        try:
            detail = json.loads(raw.decode("utf8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            detail = {}
        return detail if isinstance(detail, dict) else {}

    def create_viewer_key(self, *, name: str, tags: list) -> str:
        """A key that streams every asset carrying one of `tags`. The body is
        returned exactly once; nothing can read it back later."""
        raw = request_json(
            "POST",
            self._url("viewer_key"),
            headers=self.auth.authorization(),
            json_body={"name": name, "tags": list(tags)},
            timeout=self.timeout,
        )
        key = raw.get("key")
        if not key:
            raise FatalError("the viewer key was created without its key")
        return str(key)


def rejection(detail: dict) -> str:
    """Why the API did not take the upload, from a finalize response, or "".

    A 2xx with a failed status or an errors list is still a refusal: the
    verification gate downgrades the request rather than erroring, and the
    asset it describes will never process.
    """
    status = str(detail.get("status") or "").lower()
    errors = detail.get("errors") or []
    if status not in ("failed", "canceled") and not errors:
        return ""
    reasons = []
    for error in errors if isinstance(errors, list) else []:
        if isinstance(error, dict):
            text = error.get("message") or error.get("detail") or error.get("title")
            reasons.append(str(text) if text else json.dumps(error, sort_keys=True))
        elif error:
            reasons.append(str(error))
    if not reasons:
        reasons.append(f"the upload is marked {status}")
    return "; ".join(reasons)


def human_error(exc: Exception) -> str:
    from . import api

    return api.friendly_error(exc, service="Miris")
