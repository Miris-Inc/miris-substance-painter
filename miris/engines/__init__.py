"""Upload engines, keyed by the name a catalog entry selects them with."""

from __future__ import annotations

ENGINES: dict[str, type] = {}


def register(cls):
    ENGINES[cls.NAME] = cls
    return cls


from . import miris_asset_api, playbook_api  # noqa: E402, F401
