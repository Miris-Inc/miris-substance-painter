from __future__ import annotations

import base64
import gzip
import json
import os
import platform
import threading
import time
import uuid

from . import api, localstate
from .version import __version__

# CloudFront rejects URLs longer than 8,192 bytes.
_MAX_URL_BYTES = 8000
_MAX_PENDING = 50

_lock = threading.Lock()
_session_id = str(uuid.uuid4())
_origin = time.monotonic()
_pending: list[dict] = []
_next_id = 0


def track(entry_type: str, name: str, **data) -> None:
    """Queue one entry and send what is queued, off the calling thread."""
    # DO_NOT_TRACK (consoledonottrack.com) wins over the saved answer.
    if os.environ.get("DO_NOT_TRACK", "0") not in ("", "0"):
        return
    try:
        with _lock:
            _push(entry_type, name, "integration", data)
        threading.Thread(target=_flush, daemon=True).start()
    except Exception:  # noqa: BLE001 (telemetry must never break the plugin)
        pass


def _entry(entry_type: str, name: str, target_type: str, data: dict) -> dict:
    return {
        "name": name,
        "type": entry_type,
        "platform": "desktop",
        "startTime": round((time.monotonic() - _origin) * 1000, 3),
        "duration": 0,
        "sessionId": _session_id,
        "targetId": "substance-painter" if target_type == "integration" else "",
        "targetType": target_type,
        "data": data,
    }


def _push(entry_type: str, name: str, target_type: str, data: dict) -> None:
    _pending.append(_entry(entry_type, name, target_type, data))
    del _pending[:-_MAX_PENDING]


# Runs at the first send with analytics on, so where it is off no install id is made.
def _open_session() -> None:
    install_id = localstate.load_prefs().get("install_id")
    first_run = not isinstance(install_id, str)
    if first_run:
        install_id = str(uuid.uuid4())
        localstate.save_pref("install_id", install_id)
    opening = [
        _entry("session", "session", "session", {"absoluteStartTime": round(time.time() * 1000)}),
        _entry(
            "configuration",
            "configuration",
            "integration",
            {
                "integration": "substance-painter",
                "integrationVersion": __version__,
                "host": "substance-painter",
                "hostVersion": _HOST_VERSION,
                "os": platform.system().lower(),
                "installId": install_id,
            },
        ),
    ]
    if first_run:
        opening.append(_entry("lifecycle", "install", "integration", {}))
    _pending[:0] = opening


def _host_version() -> str:
    try:
        import substance_painter.application

        return substance_painter.application.version()
    except (ImportError, AttributeError):
        return ""


# Read on import, which is on Painter's main thread; sessions open on a send thread.
_HOST_VERSION = _host_version()


def _endpoint() -> str | None:
    """The analytics pixel, "" when analytics is off, None until Miris has said."""
    if not localstate.share_usage():
        return ""
    try:
        config = json.loads(api.miris_config)
        # Portal and API endpoints send the client settings; viewer endpoints send sdk ones.
        if config["k"] != "client":
            return None
        flags = config["flags"]
        return flags.get("analyticsEndpoint", "") if flags.get("isAnalyticsEnabled") else ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _flush() -> None:
    global _next_id
    endpoint = _endpoint()
    if endpoint is None:
        return
    with _lock:
        if endpoint and _next_id == 0:
            _open_session()
        batch = _pending[:]
        _pending.clear()
        if not endpoint:
            return
        # Numbered as they leave, so the session's own entries come first.
        for entry in batch:
            entry["entryId"] = _next_id
            _next_id += 1
    if batch:
        _send(endpoint, batch)


def _send(endpoint: str, batch: list[dict]) -> None:
    body = json.dumps(batch, separators=(",", ":")).encode()
    # gzip, then unpadded base64url, as the SDK's CDN transport does.
    data = base64.urlsafe_b64encode(gzip.compress(body)).rstrip(b"=").decode()
    separator = "&" if "?" in endpoint else "?"
    # k is the viewer key, which a plugin doesn't have.
    url = f"{endpoint}{separator}v=1&b={uuid.uuid4()}&k=&d={data}"
    if len(url) > _MAX_URL_BYTES and len(batch) > 1:
        middle = len(batch) // 2
        _send(endpoint, batch[:middle])
        _send(endpoint, batch[middle:])
        return
    if os.environ.get("MIRIS_TELEMETRY_DEBUG") == "1":
        try:
            import substance_painter.logging as sp_log

            sp_log.info(f"[miris] usage report: {body.decode()}")
        except ImportError:
            print(f"[miris] usage report: {body.decode()}")
    try:
        api.request("GET", url, timeout=10)
    except api.ApiError:
        pass  # a lost entry is not worth a line in the user's log
