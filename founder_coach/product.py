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
# The Pack this plugin is (ADR-0016): its Domains and the memory modules it keeps beyond the core (profile,
# Feedback, usage). A product.json from before Packs is the founder Pack with every module.
MODULES = ("goals", "commitments", "checkins", "decisions")     # a product.json from before Packs: the founder's
# `common_fields`: the Common profile facts it reads and offers to share (absent: all of them; [] none).
PACK: dict = {"id": "founder", "domains": [], "modules": list(MODULES), **(_DATA.get("pack") or {})}


def reword(text: str) -> str:
    """The shared runtime's text in this Pack's words: [runtime.replace] in pack.toml (longest phrase first,
    e.g. "Founder" -> "Engineer"). The founder Pack replaces nothing, so its text is exactly as written."""
    rep = (PACK.get("runtime") or {}).get("replace") or {}
    if not rep or not text:
        return text
    import re as _re
    keys = sorted(rep, key=len, reverse=True)
    return _re.sub("|".join(_re.escape(k) for k in keys), lambda m: rep[m.group(0)], text)


def has(module: str) -> bool:
    """This Pack keeps `module` (goals, commitments, checkins, decisions)."""
    return module in (PACK.get("modules") or ())


def env_name(name: str) -> str:
    """The full name of one setting, e.g. env_name("HOME") -> "FOUNDER_COACH_HOME"."""
    return ENV_PREFIX + name


def env(name: str, default: str | None = None) -> str | None:
    """Read one setting from the environment."""
    return os.environ.get(ENV_PREFIX + name, default)


def default_home() -> Path:
    """The Founder's data folder when the HOME setting isn't set: ~/.<id>."""
    return Path(f"~/.{ID}").expanduser()
