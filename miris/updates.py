"""Whether a newer release than this build is out."""

from __future__ import annotations

from . import api
from .version import __version__

# releases/latest skips prereleases, so a release still being finalized is never offered.
LATEST_URL = "https://api.github.com/repos/Miris-Inc/miris-substance-painter/releases/latest"
TAG_PREFIX = "substance-painter/v"


def newer_release() -> dict | None:
    release = api.request_json("GET", LATEST_URL, timeout=10)
    version = release["tag_name"].removeprefix(TAG_PREFIX)
    if not is_newer(version, __version__):
        return None
    return {"version": version, "url": release["html_url"]}


def is_newer(latest: str, current: str) -> bool:
    def parts(version):
        return tuple(int(n) for n in version.split("-")[0].split("."))

    return parts(latest) > parts(current)
