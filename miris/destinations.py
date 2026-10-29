"""Where an upload can go, read from catalog.json.

The window renders whatever the catalog says, so adding a destination on a
known protocol is one JSON edit. A destination that is not available yet needs
only a name, a status and a reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import params

# Every asset this plugin uploads leads with this tag, whatever the destination,
# so one search finds them in any DAM.
PLUGIN_TAG = "substance-painter"

CATALOG_PATH = Path(__file__).with_name("catalog.json")
WEB_DIR = Path(__file__).parent / "ui" / "web"

SCHEMA_VERSION = 2
SCHEMA_DOC = "miris/catalog.schema.json"

# Only "available" can be chosen. "soon" and "disabled" show greyed, because a
# destination that vanishes after being offered reads as a bug. "hidden" is the
# exception: never offered, entry kept so it can be turned on in one word.
STATUSES = ("available", "soon", "disabled", "hidden")


def load(path: Path | None = None) -> list[dict]:
    """Every destination, in display order. Raises ValueError on a schema mismatch."""
    raw = json.loads((path or CATALOG_PATH).read_text())
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"catalog schema_version {raw.get('schema_version')!r} is not {SCHEMA_VERSION}; "
            f"see {SCHEMA_DOC}"
        )
    return raw["integrations"]


def by_id(destination: str, path: Path | None = None) -> dict | None:
    for entry in load(path):
        if entry["id"] == destination:
            return entry
    return None


def ids(path: Path | None = None) -> list[str]:
    return [entry["id"] for entry in load(path)]


def available(destination: str, path: Path | None = None) -> bool:
    entry = by_id(destination, path)
    return bool(entry) and entry["status"] == "available"


def mechanism_of(entry: dict) -> str:
    return (entry.get("auth") or {}).get("mechanism", "")


def engine_of(entry: dict) -> str:
    return (entry.get("upload") or {}).get("engine", "")


def signs_in(entry: dict) -> bool:
    """Whether this destination's mechanism signs the user in through a browser."""
    # Local import: engines import this module at module level.
    from .auth import MECHANISMS

    mechanism = MECHANISMS.get(mechanism_of(entry))
    return bool(mechanism and mechanism.INTERACTIVE)


def default_destination(path: Path | None = None) -> str:
    """The first destination that works, rather than a hardcoded name: a catalog
    that disables one should not leave the fallback pointing at it."""
    for entry in load(path):
        if entry["status"] == "available":
            return entry["id"]
    return ""


def parse_destination(raw: str, path: Path | None = None) -> str:
    """Anything unrecognised falls back to the default: by the time this is read
    the user has pressed Upload, and refusing would lose their work."""
    return raw if available(raw, path) else default_destination(path)


def upload_tags(extra_tags=None) -> list[str]:
    tags = [PLUGIN_TAG]
    for tag in extra_tags or []:
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def validate(catalog: dict) -> list[str]:
    """Everything wrong with a catalog, one line each. Empty means valid."""
    problems: list[str] = []

    def bad(where: str, message: str) -> None:
        # One mistake, one line, whichever rules overlap on it.
        line = f"{where}: {message}"
        if line not in problems:
            problems.append(line)

    if catalog.get("schema_version") != SCHEMA_VERSION:
        bad("catalog", f"schema_version must be {SCHEMA_VERSION}")

    entries = catalog.get("integrations")
    if not isinstance(entries, list) or not entries:
        bad("catalog", "integrations must be a non-empty list")
        return problems

    # Local import: the engines import this module at module level, so a
    # top-level import here would run against a partially initialised package.
    from .auth import MECHANISMS
    from .engines import ENGINES

    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"integrations[{index}]"
        if not isinstance(entry, dict):
            bad(where, "must be an object")
            continue

        entry_id = entry.get("id")
        where = entry_id or where
        if not isinstance(entry_id, str) or not entry_id:
            bad(where, "id is required")
        elif entry_id in seen:
            # Ids key stored credentials, so a duplicate means two destinations
            # sharing one sign-in.
            bad(where, "duplicate id")
        else:
            seen.add(entry_id)

        if not entry.get("title"):
            bad(where, "title is required")

        if entry.get("status") not in STATUSES:
            bad(where, f"status must be one of {STATUSES}, got {entry.get('status')!r}")
        # A greyed card with no reason is what generates support mail. "hidden"
        # is exempt: nothing shows it, so there is nobody to explain it to.
        if entry.get("status") in ("soon", "disabled") and not entry.get("status_message"):
            bad(where, "status_message is required when the status is not selectable")

        logo = entry.get("logo")
        if logo is not None and not isinstance(logo, dict):
            bad(where, "logo must be an object or null")
        elif logo is not None:
            if logo.get("lockup") not in ("horizontal", "stacked"):
                bad(where, "logo.lockup must be horizontal or stacked")
            if not isinstance(logo.get("aspect"), (int, float)) or logo.get("aspect") <= 0:
                bad(where, "logo.aspect must be a positive number")
            svg, file = logo.get("svg"), logo.get("file")
            if bool(svg) == bool(file):
                bad(where, "logo needs exactly one of svg (inline) or file (under ui/web/logos)")
            elif svg:
                if not str(svg).startswith("<svg"):
                    bad(where, "logo.svg must be inline SVG markup")
                elif "viewBox" not in svg:
                    # Without one it cannot scale to a CSS height: the card shows
                    # the top-left corner of one letter.
                    bad(where, "logo.svg needs a viewBox or it cannot be scaled")
            elif not re.fullmatch(r"logos/[A-Za-z0-9._-]+\.(png|svg)", str(file)):
                bad(where, "logo.file must be a png or svg under logos/")
            elif not (WEB_DIR / file).is_file():
                bad(where, f"logo.file does not exist: {file}")

        help_url = entry.get("help_url")
        if help_url is not None and not str(help_url).startswith("https://"):
            bad(where, "help_url must be https")

        if entry.get("status") != "available":
            continue

        if not entry.get("description"):
            bad(where, "description is required")

        if mechanism_of(entry) not in MECHANISMS:
            bad(
                where,
                f"auth.mechanism must be one of {sorted(MECHANISMS)}, got {mechanism_of(entry)!r}",
            )
        elif not signs_in(entry):
            bad(where, "an available destination must sign in")
        if engine_of(entry) not in ENGINES:
            bad(where, f"upload.engine must be one of {sorted(ENGINES)}, got {engine_of(entry)!r}")

        mechanism = MECHANISMS.get(mechanism_of(entry))
        engine = ENGINES.get(engine_of(entry))

        if mechanism is not None:
            auth = dict(entry.get("auth") or {})
            for envelope_key in ("mechanism", "host"):
                auth.pop(envelope_key, None)
            if not isinstance(entry["auth"].get("host"), str):
                bad(where, "auth.host is required")
            problems.extend(params.check(auth, mechanism.PARAMS, f"{where}.auth"))
        if engine is not None:
            upload = dict(entry.get("upload") or {})
            for envelope_key in ("engine", "host"):
                upload.pop(envelope_key, None)
            if not isinstance(entry["upload"].get("host"), str):
                bad(where, "upload.host is required")
            problems.extend(params.check(upload, engine.PARAMS, f"{where}.upload"))

        if mechanism is not None and engine is not None:
            runtime = set(mechanism.RUNTIME) | set(engine.RUNTIME)
            for block_name in ("auth", "upload"):
                for path, name in _placeholders_in(entry.get(block_name), block_name):
                    if name not in runtime:
                        bad(where, f"{path} uses placeholder {{{name}}} that nothing supplies")
            provided = set(mechanism.PROVIDES) | set(engine.PROVIDES)
            for cap, on in (entry.get("capabilities") or {}).items():
                if on and cap not in provided:
                    bad(
                        where,
                        f"capabilities.{cap} is true but neither {mechanism.NAME} nor "
                        f"{engine.NAME} can provide it",
                    )

        formats = entry.get("formats")
        if not isinstance(formats, list) or not formats:
            bad(where, "formats must be a non-empty list, best first")

        # The packager's release gate checks packaged hosts against these, so a
        # staging host cannot ship.
        production = entry.get("production_hosts")
        if production is None:
            if entry.get("status") == "available":
                bad(where, "production_hosts is required for an available destination")
        elif not isinstance(production, dict) or set(production) != {"auth", "upload"}:
            bad(where, "production_hosts must name exactly auth and upload")
        else:
            for block_name, host in production.items():
                if not isinstance(host, str) or not host.startswith("https://"):
                    bad(where, f"production_hosts.{block_name} must be an https URL")

        for option in entry.get("publish_options") or []:
            option_where = f"{where}.publish_options[{option.get('id', '?')}]"
            for key in ("id", "label", "help"):
                if not option.get(key):
                    bad(option_where, f"{key} is required")
            if option.get("type") not in ("checkbox", "environments"):
                bad(option_where, "type must be checkbox or environments")
            if not isinstance(option.get("default", False), bool):
                bad(option_where, "default must be true or false")
            requires = option.get("requires")
            if requires is not None:
                siblings = {o.get("id") for o in entry.get("publish_options") or []}
                if requires == option.get("id") or requires not in siblings:
                    bad(option_where, "requires must name another publish option on this entry")
            if "caution" in option and not option.get("caution"):
                bad(option_where, "caution must be a non-empty string when present")
            if not isinstance(option.get("advanced", False), bool):
                bad(option_where, "advanced must be true or false")
            # An option the engine never reads is a checkbox that does nothing.
            if engine is not None and option.get("id") not in engine.PUBLISH_OPTIONS:
                bad(option_where, f"{engine.NAME} does not read this publish option")

    return problems


def _placeholders_in(block, path: str) -> list[tuple[str, str]]:
    """(path, name) for every placeholder in a block, the path for error messages."""
    from .params import placeholders

    if isinstance(block, dict):
        return [p for k, v in block.items() for p in _placeholders_in(v, f"{path}.{k}")]
    if isinstance(block, list):
        return [p for i, v in enumerate(block) for p in _placeholders_in(v, f"{path}[{i}]")]
    if isinstance(block, str):
        return [(path, name) for name in sorted(placeholders(block))]
    return []
