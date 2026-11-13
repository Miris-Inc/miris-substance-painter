"""Auth mechanisms, keyed by the name a catalog entry selects them with.

Each mechanism module registers itself on import. Import order only sets the
order in error messages.
"""

from __future__ import annotations

MECHANISMS: dict[str, type] = {}


def register(cls):
    MECHANISMS[cls.NAME] = cls
    return cls


from . import device_grant, oauth2_pkce  # noqa: E402, F401
