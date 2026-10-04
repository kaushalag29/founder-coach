"""Why the last command stopped, for `ytbrain ops` (docs/ops.md): a command that stops early writes one
line -- the reason and its detail -- so the runner can retry a network failure, wait out a plan limit,
or stop and say what to fix, instead of guessing from an exit code."""
from __future__ import annotations

import json
import re
import sys
import time

from .config import DATA
from .pages import atomic_write_text

STATUS = DATA / "ops" / "last-stop.json"
REASONS = ("budget", "endpoint", "network", "plan_limit", "auth", "interrupted", "config", "books", "unknown")
_NETWORK = re.compile(r"timed? ?out|timeout|connection|network|unreachable|temporar|reset by peer|"
                      r"name resolution|\b5\d\d\b|\b429\b|rate.?limit|too many requests|slow provider", re.IGNORECASE)
_ENDPOINT = re.compile(r"\b40[0-3]\b|credit|api key|\bkey\b|quota|refused|forbidden|unauthori[sz]ed|"
                       r"not a valid model|no such model|model .*not (found|served)", re.IGNORECASE)


def classify(text: str) -> str:
    """A stop message's reason: a refused endpoint (credits, key, model) needs you; a network
    problem may pass on its own."""
    text = str(text or "")
    if text == "budget":
        return "budget"
    if _ENDPOINT.search(text) and not re.search(r"\b429\b|rate.?limit", text, re.IGNORECASE):
        return "endpoint"
    if _NETWORK.search(text):
        return "network"
    return "unknown"


def record(reason: str, detail: str = "") -> None:
    """Best effort: a failure to write the status never hides the stop itself."""
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(STATUS, json.dumps({"reason": reason if reason in REASONS else "unknown",
                                              "detail": str(detail)[:500], "argv": sys.argv[1:],
                                              "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
    except OSError:
        pass


def read() -> dict | None:
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return None


def clear() -> None:
    try:
        STATUS.unlink()
    except OSError:
        pass
