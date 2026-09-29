"""Settings files: the runtime's own lines from `.env` files, so one file configures the coach
whether a shell, Claude Code, the plugin or an eval starts it.

Only lines named with the product's prefix (FOUNDER_COACH_* for id founder-coach) are read; every
other line, API keys included, is ignored, so a shared `.env` never hands the runtime a secret it
doesn't need. A variable already in the environment always wins. Files, first match wins:

1. `$<PREFIX>ENV_FILE`, a file named explicitly;
   or, inside a `claude plugin eval` sandbox (which passes only EVAL_* variables through),
   `$EVAL_<PREFIX>ENV_FILE` (`ytbrain claude` sets it to the repo's .env). There the location
   settings (HOME, MODELS, PACK) are skipped, so the eval stays in its sandbox;
2. `.env` in the current folder (a session started in the repo or a project folder);
3. `.env` in the data folder (`$<PREFIX>HOME` or ~/.<id>): a Founder's own settings.

`<PREFIX>DOTENV=0` (or YTBRAIN_DOTENV=0, which the tests set) skips all of them. Stdlib only.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import product

LOCATION = ("HOME", "MODELS", "PACK")       # where data lives: never taken from a file inside an eval sandbox
CONTROL = ("ENV_FILE", "DOTENV")            # these steer the loading itself, so a file can't set them
LOADED: list[dict] = []                     # [{path, count}] of this process's files, for `status`


def parse(path: Path) -> dict[str, str]:
    """NAME=value lines (an `export ` prefix, quotes and ` # comments` handled like ytbrain's .env)."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.removeprefix("export ").split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] in "\"'" and val.endswith(val[0]):
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        out[key.strip()] = val
    return out


def candidates(cwd: Path | None = None) -> list[tuple[Path, bool]]:
    """The files to try before the data folder's, each with whether it's an eval sandbox's."""
    files: list[tuple[Path, bool]] = []
    explicit = os.environ.get(product.env_name("ENV_FILE"))
    sandboxed = os.environ.get("EVAL_" + product.env_name("ENV_FILE"))
    if explicit:
        files.append((Path(explicit).expanduser(), False))
    elif sandboxed:
        files.append((Path(sandboxed).expanduser(), True))
    files.append(((cwd or Path.cwd()) / ".env", False))
    return files


def load(cwd: Path | None = None) -> list[dict]:
    """Apply the settings files to os.environ (never overriding it); returns what was read."""
    LOADED.clear()
    if os.environ.get(product.env_name("DOTENV")) == "0" or os.environ.get("YTBRAIN_DOTENV") == "0":
        return LOADED
    seen: set[Path] = set()

    def apply(path: Path, sandbox: bool) -> None:
        try:
            key = path.resolve()
            if key in seen or not path.is_file():
                return
            seen.add(key)
            values = parse(path)
        except (OSError, UnicodeDecodeError, ValueError):
            return                              # an unreadable file is skipped: settings are optional
        n = 0
        for name, value in values.items():
            if not name.startswith(product.ENV_PREFIX):
                continue
            short = name[len(product.ENV_PREFIX):]
            if short in CONTROL or (sandbox and short in LOCATION):
                continue
            if name not in os.environ:
                os.environ[name] = value
                n += 1
        LOADED.append({"path": str(path), "count": n})

    for path, sandbox in candidates(cwd):
        apply(path, sandbox)
    # the data folder may itself have been named by a file above
    home = Path(product.env("HOME") or product.default_home()).expanduser()
    apply(home / ".env", False)
    return LOADED
