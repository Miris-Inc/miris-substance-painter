"""Where the plugin keeps state on disk, and which host it talks to."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

HOST_ENV = "MIRIS_AUTH_HOST"


STATE_DIR_ENV = "MIRIS_STATE_DIR"


def state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".miris" / "substance-painter"


def resolve_host(fallback: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """MIRIS_AUTH_HOST if set, so QA can aim a build at another deploy, else `fallback`."""
    source = os.environ if env is None else env
    return (source.get(HOST_ENV) or fallback or "").rstrip("/")


def join_url(host: str, path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return host.rstrip("/") + "/" + path_or_url.lstrip("/")
