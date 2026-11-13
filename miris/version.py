"""The plugin's version, from version.json beside this file.

Castellum bumps version.json from changesets; pyproject.toml defers to it.
"""

import json
from pathlib import Path

__version__ = json.loads(Path(__file__).with_name("version.json").read_text(encoding="utf-8"))[
    "version"
]
USER_AGENT = f"miris-substance-painter/{__version__}"
