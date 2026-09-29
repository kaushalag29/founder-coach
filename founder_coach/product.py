"""The product's identity at run time: its id, display name and the names derived from it.

Generated from the repo's product.toml into product.json by scripts/assemble_plugin.py
(ADR-0012), so a rename is one edit plus a rebuild. Everything user-visible that carries the
name reads it from here: the CLI name in messages, the data folder (~/.<id>) and the settings
prefix (<ID>_*, e.g. FOUNDER_COACH_HOME for id founder-coach).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_FILE = Path(__file__).with_name("product.json")
try:
    _DATA = json.loads(_FILE.read_text(encoding="utf-8"))
    ID: str = _DATA["id"]
    DISPLAY_NAME: str = _DATA["display_name"]
except (OSError, ValueError, KeyError, TypeError) as e:
    raise RuntimeError(f"{_FILE} is missing or damaged ({type(e).__name__}: {e}); it is generated from "
                       "product.toml by scripts/assemble_plugin.py: rebuild or reinstall the plugin") from None
if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", ID):
    raise RuntimeError(f"product.json: id {ID!r} must be lowercase letters, digits and hyphens")
ENV_PREFIX: str = ID.upper().replace("-", "_") + "_"


def env_name(name: str) -> str:
    """The full name of one setting, e.g. env_name("HOME") -> "FOUNDER_COACH_HOME"."""
    return ENV_PREFIX + name


def env(name: str, default: str | None = None) -> str | None:
    """Read one setting from the environment."""
    return os.environ.get(ENV_PREFIX + name, default)


def default_home() -> Path:
    """The Founder's data folder when the HOME setting isn't set: ~/.<id>."""
    return Path(f"~/.{ID}").expanduser()
