"""The grammar a mechanism or engine declares its PARAMS in, and the walker.

  str / int / bool     required, of that type
  (type, default)      optional, with that default
  type | None          required to be present, may be null
  {"a", "b"}           required, one of those strings
  [type]               required, non-empty list of that type
  {key: spec, ...}     nested object, walked recursively
  {"*": type}          map of any names to that type

An unknown key is an error, so the catalog cannot carry a key the component
will not read.
"""

from __future__ import annotations

import re
import types

_MISSING = object()


def check(block, spec: dict, where: str) -> list[str]:
    """Every way `block` fails `spec`, as readable lines. Empty means good."""
    if not isinstance(block, dict):
        return [f"{where}: must be an object"]
    if set(spec) == {"*"}:
        return [p for k, v in block.items() for p in _leaf(v, spec["*"], f"{where}.{k}")]

    problems = [f"{where}.{key}: unknown key" for key in block if key not in spec]
    for key, sub in spec.items():
        value = block.get(key, _MISSING)
        here = f"{where}.{key}"
        if isinstance(sub, tuple):
            if value is not _MISSING:
                problems += _leaf(value, sub[0], here)
        elif value is _MISSING:
            problems.append(f"{here}: required")
        elif isinstance(sub, dict):
            problems += check(value, sub, here)
        else:
            problems += _leaf(value, sub, here)
    return problems


def _leaf(value, sub, where: str) -> list[str]:
    if isinstance(sub, list):
        if not isinstance(value, list) or not value:
            return [f"{where}: must be a non-empty list"]
        return [p for i, v in enumerate(value) for p in _leaf(v, sub[0], f"{where}[{i}]")]
    if isinstance(sub, (set, frozenset)):
        return [] if value in sub else [f"{where}: must be one of {sorted(sub)}, got {value!r}"]
    if isinstance(sub, types.UnionType):
        if value is None:
            return []
        inner = next(a for a in sub.__args__ if a is not type(None))
        return _leaf(value, inner, where)
    # bool is an int to Python; a catalog saying 8388608 must not accept true.
    if isinstance(value, bool) and sub is not bool:
        return [f"{where}: must be {sub.__name__}"]
    if not isinstance(value, sub):
        return [f"{where}: must be {sub.__name__}"]
    return []


def with_defaults(block: dict, spec: dict) -> dict:
    """`block` with every optional key filled from its default."""
    out = dict(block)
    for key, sub in spec.items():
        if isinstance(sub, tuple):
            out.setdefault(key, sub[1])
        elif isinstance(sub, dict) and set(sub) != {"*"}:
            out[key] = with_defaults(out.get(key) or {}, sub)
    return out


_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text or ""))


def fill(template: str, **values) -> str:
    """Raises ValueError on a leftover placeholder, which would send a literal "{asset_id}"."""
    missing = placeholders(template) - set(values)
    if missing:
        raise ValueError(f"unresolved placeholder(s) {sorted(missing)} in {template!r}")
    return template.format(**values)
