"""What every upload engine declares and does."""

from __future__ import annotations

import dataclasses
from typing import ClassVar

from .. import config

# Re-exported: the connector and the window import it from here, and the
# protocol modules from api, so neither side has to import the other's package.
from ..api import Cancelled
from ..params import fill, placeholders


@dataclasses.dataclass(frozen=True)
class Uploaded:
    """What the window shows when an upload lands.

    `warning` is for something that failed after the file was safely there, like
    a tag that would not stick. It must not be reported as a failed upload.
    """

    message: str
    share_url: str = ""
    warning: str = ""


@dataclasses.dataclass(frozen=True)
class UploadRequest:
    archive: object
    asset_name: str
    tags: list
    report_stage: object
    report_bytes: object
    should_continue: object
    # Per-publish choices from the form, keyed by publish_options id; an engine
    # reads only the ones it declares in PUBLISH_OPTIONS.
    options: dict = dataclasses.field(default_factory=dict)
    # One line per step for Painter's log, with ids, keys and sizes, so
    # "nothing arrived" can be diagnosed from the log alone.
    log: object = None
    # Called with a short description once the destination holds a record of
    # this upload, and with None once that record is final either way. The
    # window persists it, so an upload cut off by a quit is reported next time.
    checkpoint: object = None

    def stop_if_cancelled(self) -> None:
        if not self.should_continue():
            raise Cancelled("upload cancelled by the user")

    def note(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def mark(self, description) -> None:
        if self.checkpoint is not None:
            self.checkpoint(description)


class UploadEngine:
    """One DAM's asset API. Named by API family, not by blob transport.

    Constructed with its catalog block and the mechanism that authenticates it.
    Reads every URL, path and option from the block.
    """

    NAME: ClassVar[str] = ""
    PARAMS: ClassVar[dict] = {}
    # Capabilities this engine can back, as for the mechanism's PROVIDES.
    PROVIDES: ClassVar[frozenset] = frozenset()
    # Placeholders this engine fills per request, such as {asset_id}.
    RUNTIME: ClassVar[frozenset] = frozenset()
    # Ids of publish_options this engine reads from UploadRequest.options.
    PUBLISH_OPTIONS: ClassVar[frozenset] = frozenset()
    # Which endpoint proves a stored session still works, when the mechanism
    # has no liveness endpoint of its own. None means the mechanism covers it.
    PROBE: ClassVar[str | None] = None

    def __init__(self, block: dict, auth):
        self.block = block
        self.auth = auth
        self.host = block["host"].rstrip("/")
        self.endpoints = dict(block.get("endpoints") or {})
        self.options = {k: self._option(k, v) for k, v in (block.get("options") or {}).items()}
        self.workspace = ""

    def _option(self, key: str, value):
        """Options are sent as they are, so a placeholder in one would reach the
        wire as literal braces."""
        if not isinstance(value, str):
            return value
        stranded = placeholders(value)
        if stranded:
            raise ValueError(
                f"option {key!r} uses placeholder(s) {sorted(stranded)}, "
                "which nothing fills: options are sent as they are"
            )
        return value

    def url(self, name: str, **values) -> str:
        return fill(config.join_url(self.host, self.endpoints[name]), **values)

    def upload(self, request: UploadRequest) -> Uploaded:
        """Runs on a worker."""
        raise NotImplementedError

    def workspaces(self) -> list:
        """[{id, name}] the user may upload to. Empty when the API has none."""
        return []

    def share_set(self, tag: str, name: str) -> str:
        """One link for every asset carrying `tag`. Only an engine that declares
        the environments publish option is ever asked."""
        raise NotImplementedError

    def probe(self) -> None:
        """Hit PROBE to confirm the session, or raise AuthError."""

    def human_error(self, exc: Exception) -> str:
        raise NotImplementedError
