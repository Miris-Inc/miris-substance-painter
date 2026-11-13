"""Miris's asset API: register, PUT to S3 with one-shot credentials, finalize.

Sends no ?workspace= to the API; the token is pinned (see mirisupload.py). The
portal link is the one place a workspace is named, so the browser lands in the
workspace the asset is in.
"""

from __future__ import annotations

import urllib.parse
from typing import ClassVar

from .. import config, destinations, mirisupload, s3
from ..api import FatalError
from . import register
from .base import Uploaded, UploadEngine, UploadRequest


@register
class MirisAssetApi(UploadEngine):
    NAME: ClassVar[str] = "miris_asset_api"
    PARAMS: ClassVar[dict] = {
        "endpoints": {
            "register": str,
            "finalize": str,
            "portal": str,
            "viewer_key": str,
            "player": str,
        },
        "options": {"input_type": str, "output_type": str},
    }
    PROVIDES: ClassVar[frozenset] = frozenset({"tags", "share_link"})
    RUNTIME: ClassVar[frozenset] = frozenset({"asset_id"})
    PROBE: ClassVar[str | None] = None  # the mechanism's verify endpoint covers liveness
    # skip_preview asks the API to go straight to streamable, which spends a
    # generation credit; the form says so beside the checkbox. environments is
    # read by the window, which publishes one copy per chosen lighting and, for
    # more than one, asks share_set() for their link.
    PUBLISH_OPTIONS: ClassVar[frozenset] = frozenset({"skip_preview", "environments"})

    def __init__(self, block: dict, auth):
        super().__init__(block, auth)
        # Same MIRIS_AUTH_HOST override as device_grant: a token minted on one
        # deploy is refused by another.
        self.catalog_host = self.host
        self.host = config.resolve_host(self.host)

    def upload(self, request: UploadRequest) -> Uploaded:
        client = mirisupload.MirisUpload(self.auth, self.host, self.endpoints, self.options)
        total = request.archive.stat().st_size

        request.report_stage("Creating in Miris…")
        asset = client.create_asset(
            name=request.asset_name,
            tags=destinations.upload_tags(request.tags),  # never exists untagged
            content_path=request.asset_name,
            file_count=1,
            total_bytes=total,
            streamable=bool(request.options.get("skip_preview")),
        )

        key = s3.join_key(s3.target_from_asset(asset)[1], request.archive.name)
        request.note(
            f"registered asset {asset.id} for {request.asset_name!r}: "
            f"{total} bytes -> {asset.upload_path}/{request.archive.name}"
        )
        # The destination now holds a record. If Painter quits before it is
        # final, the next open reports it.
        request.mark(f"Miris asset {asset.id} at {self.host}")

        # Cancelling or failing past this point would leave an asset registered
        # with no files, so the API is told the upload failed instead.
        try:
            request.stop_if_cancelled()
            request.report_stage("Uploading to Miris…")
            request.report_bytes(0, total)
            target, _prefix = s3.target_from_asset(asset)
            s3.put_file(
                target,
                key,
                request.archive,
                on_bytes=lambda sent: request.report_bytes(sent, total),
            )
            request.report_bytes(total, total)
            request.note(f"uploaded {total} bytes for asset {asset.id}")
        except Exception as exc:
            request.note(f"upload of asset {asset.id} stopped: {exc}")
            client.finalize_upload(asset.id, False)
            request.mark(None)
            raise

        request.report_stage("Finalising…")
        # The manifest makes the API verify the exact file before accepting.
        detail = client.finalize_upload(
            asset.id, True, manifest=[{"file_name": request.archive.name, "size": total}]
        )
        problem = mirisupload.rejection(detail)
        request.mark(None)
        if problem:
            request.note(f"Miris refused asset {asset.id}: {problem}")
            raise FatalError(f"Miris did not accept the upload. {problem}")
        request.note(
            f"finalised asset {asset.id}: status={detail.get('status', '?')} "
            f"processing={detail.get('process_status', '?')}"
        )
        return Uploaded("Published to Miris.", share_url=self.portal_link(asset.id))

    def portal_link(self, asset_id: str) -> str:
        """The asset's page in the portal. The portal is served from the API
        host, so a build aimed at another deploy links to that deploy."""
        url = self.url("portal", asset_id=asset_id)
        if self.workspace:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {"workspace": self.workspace}
            )
        return url

    def share_set(self, tag: str, name: str) -> str:
        """The player on every asset carrying `tag`, via a key that streams exactly
        those. The API returns the key once, so the link is its only copy."""
        client = mirisupload.MirisUpload(self.auth, self.host, self.endpoints, self.options)
        key = client.create_viewer_key(name=f"Substance 3D Painter: {name}", tags=[tag])
        # pending asks the player to list the variants still processing.
        query = urllib.parse.urlencode({"viewerKey": key, "tags": tag, "pending": 1})
        # The catalog names production's player; another deploy has its own at player.<host>.
        player = self.url("player")
        if self.host != self.catalog_host:
            parts = urllib.parse.urlsplit(self.host)
            player = parts._replace(netloc="player." + parts.netloc).geturl()
        return f"{player}?{query}"

    def human_error(self, exc: Exception) -> str:
        return mirisupload.human_error(exc)
