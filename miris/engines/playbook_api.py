"""Playbook's asset API: prepare a session, stream chunks, complete, tag, link."""

from __future__ import annotations

from typing import ClassVar

from .. import destinations, playbook
from . import register
from .base import Uploaded, UploadEngine, UploadRequest


@register
class PlaybookApiEngine(UploadEngine):
    NAME: ClassVar[str] = "playbook_api"
    PARAMS: ClassVar[dict] = {
        "endpoints": {
            "workspaces": str,
            "upload_prepare": str,
            "upload_complete": str,
            "tags": str,
            "share": str,
        },
        "options": {"chunk_bytes": (int, 8388608), "retries": (int, 4)},
    }
    PROVIDES: ClassVar[frozenset] = frozenset({"tags", "share_link", "workspaces"})
    RUNTIME: ClassVar[frozenset] = frozenset({"workspace", "asset_token"})
    PROBE: ClassVar[str | None] = "workspaces"
    # environments is read by the window, which packs the chosen lighting into
    # the one archive; with no skip_preview here, it never allows more than one.
    PUBLISH_OPTIONS: ClassVar[frozenset] = frozenset({"environments"})

    def __init__(self, block: dict, auth):
        super().__init__(block, auth)
        self.cfg = playbook.PlaybookConfig(
            api_host=self.host,
            chunk_bytes=self.options["chunk_bytes"],
            retries=self.options["retries"],
            workspaces_path=self.endpoints["workspaces"],
            upload_prepare_path=self.endpoints["upload_prepare"],
            upload_complete_path=self.endpoints["upload_complete"],
            tags_path=self.endpoints["tags"],
            share_path=self.endpoints["share"],
        )

    def _api(self) -> playbook.PlaybookApi:
        return playbook.PlaybookApi(self.auth.authorization, self.cfg)

    def workspaces(self) -> list:
        return [
            {"id": o.get("slug", ""), "name": o.get("name") or o.get("slug", "")}
            for o in self._api().organizations()
            if o.get("slug")  # the path template takes the slug; no slug, no target
        ]

    def probe(self) -> None:
        self._api().organizations()

    def upload(self, request: UploadRequest) -> Uploaded:
        api = self._api()
        target = self.workspace
        if not target:
            orgs = api.organizations()
            if not orgs:
                raise RuntimeError("this Playbook account has no workspace")
            target = orgs[0]["slug"]
            self.workspace = target

        request.report_stage("Uploading to Playbook…")
        total = request.archive.stat().st_size
        request.report_bytes(0, total)
        # The extension stays on the title: Playbook derives file_extension
        # from it and returns null without one.
        asset = api.upload(
            target,
            request.archive,
            title=f"{request.asset_name}.usdz",
            on_bytes=lambda sent: request.report_bytes(sent, total),
            should_continue=request.should_continue,
        )

        request.report_stage("Tagging…")
        warning = self._apply_tags(api, target, asset, request.tags)
        token = asset.get("token", "")
        if not token:
            return Uploaded("Published to Playbook.", warning=warning)

        # The file is already there: a failure here costs a link, not the asset.
        try:
            share_url = api.share_link(target, token) or ""
        except Exception as exc:  # noqa: BLE001 (the upload already succeeded)
            return Uploaded(
                "Published to Playbook.",
                warning=" ".join(
                    filter(None, [warning, f"No share link: {playbook.human_error(exc)}"])
                ),
            )
        if not share_url:
            return Uploaded("Published to Playbook. It returned no link to share.", warning=warning)
        return Uploaded("Published to Playbook.", share_url=share_url, warning=warning)

    def _apply_tags(self, api, target, asset, extra_tags) -> str:
        # Reported, not assumed: this API answers 200 to things it ignores.
        wanted = destinations.upload_tags(extra_tags)
        try:
            stuck = api.set_tags(target, asset["token"], wanted)
        except Exception as exc:  # noqa: BLE001 (the upload already succeeded)
            return f"Published, but tagging failed. {playbook.human_error(exc)}"
        missing = [t for t in wanted if t not in stuck]
        if not missing:
            return ""
        return (
            "Published, but Playbook did not keep "
            f"{'this tag' if len(missing) == 1 else 'these tags'}: {', '.join(missing)}."
        )

    def human_error(self, exc: Exception) -> str:
        return playbook.human_error(exc)
