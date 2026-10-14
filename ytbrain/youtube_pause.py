"""The YouTube pause: after an HTTP 429, later runs leave YouTube alone for a while.

YouTube's 429 is a throttle on this IP, and every further request extends it. A sync already stops
YouTube at its first 429; this module carries that across runs, so an unattended `ytbrain ops` doesn't
ask again an hour later. The first 429 pauses YouTube for YT_PAUSE_HOURS; a 429 on the first run after
a pause doubles it, up to YT_PAUSE_MAX_HOURS. A run that fetches captions without a 429 clears it.

State lives in data/ops/youtube-pause.json, written atomically. Websites and Books are never paused.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .config import DATA, YT_PAUSE_HOURS, YT_PAUSE_MAX_HOURS
from .pages import atomic_write_text

PAUSE_FILE = DATA / "ops" / "youtube-pause.json"


def _now() -> datetime:
    return datetime.now().astimezone()


def _read() -> dict:
    try:
        return json.loads(PAUSE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def active(now: datetime | None = None) -> dict | None:
    """The pause in force ({"until", "hours", "reason", "at"}), or None."""
    state = _read()
    try:
        until = datetime.fromisoformat(state["until"])
    except (KeyError, TypeError, ValueError):
        return None
    return state if (now or _now()) < until else None


def record(reason: str, now: datetime | None = None) -> dict | None:
    """A 429 ended this run's YouTube sync: pause later runs. Returns the new pause, or None when
    YT_PAUSE_HOURS is 0 (pausing turned off)."""
    if YT_PAUSE_HOURS <= 0:
        return None
    now = now or _now()
    last = _read().get("hours")
    # a 429 right after a pause means the throttle outlasted it: wait twice as long this time
    hours = min(float(last) * 2, YT_PAUSE_MAX_HOURS) if isinstance(last, (int, float)) and last > 0 \
        else YT_PAUSE_HOURS
    state = {"until": (now + timedelta(hours=hours)).isoformat(timespec="seconds"), "hours": hours,
             "reason": reason[:200], "at": now.isoformat(timespec="seconds")}
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(PAUSE_FILE, json.dumps(state, indent=1, sort_keys=True))
    return state


def clear() -> bool:
    """Captions came through without a 429: YouTube is fine again. True if a pause was recorded."""
    if not PAUSE_FILE.exists():
        return False
    atomic_write_text(PAUSE_FILE, json.dumps({}))
    return True


def describe(state: dict) -> str:
    """"until Tue 14 Oct 09:30 (24h)" for messages and the ops summary."""
    until = datetime.fromisoformat(state["until"])
    return f"until {until.strftime('%a %d %b %H:%M')} ({state['hours']:g}h)"
