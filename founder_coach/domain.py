"""The coaching vocabulary (CONTEXT.md) as data: Stages, Founder profile fields, record
statuses and the soft limits the Playbooks work to. No I/O here."""
from __future__ import annotations

import os
from pathlib import Path
from . import product

# Where a company is in its life (CONTEXT.md "Stage"). Must equal ytbrain.extract.schema.Stage;
# tests/test_coach.py fails if they drift apart.
STAGES = ("pre-idea", "idea", "mvp", "pmf", "growth", "fundraising", "scaling", "exit")

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Founder profile facts: field -> (kind, description)
PROFILE_FIELDS: dict[str, tuple[str, str]] = {
    "company": ("text", "company name"),
    "one_liner": ("text", "what the company does, in one sentence"),
    "customer": ("text", "who the customer is"),
    "stage": ("stage", "one of: " + ", ".join(STAGES)),
    "team_size": ("int", "number of people working on the company"),
    "key_metrics": ("metrics", "the few numbers that matter now, e.g. {\"MRR\": \"$4k\", \"design partners\": 3}"),
    "timezone": ("tz", "IANA time zone, e.g. Asia/Kolkata"),
    "checkin_day": ("weekday", "preferred Check-in day: " + ", ".join(WEEKDAYS)),
}

STATUSES = {
    "goal": ("active", "met", "dropped"),
    "commitment": ("open", "done", "dropped", "carried"),
}
TERMINAL = {"met", "dropped", "done", "carried"}

# fields coach_update may change, per record kind
EDITABLE = {
    "goal": ("text", "measure", "target_date", "note"),
    "commitment": ("action", "cue", "outcome", "goal_id", "result_note"),
    "decision": ("text", "reasoning", "revisit_on"),
    "checkin": ("summary", "wins", "blockers"),
}

ID_PREFIX = {"goal": "g", "commitment": "c", "decision": "d", "checkin": "k", "feedback": "f"}
KIND_OF_PREFIX = {v: k for k, v in ID_PREFIX.items() if k != "feedback"}   # records coach_update may touch

MAX_OPEN_COMMITMENTS_PER_WEEK = 3      # Focus is 1-3 Commitments (CONTEXT.md)
MAX_ACTIVE_GOALS = 3
PROFILE_STALE_DAYS = 30
CHECKIN_OVERDUE_DAYS = 8
TEXT_MAX = 1000
# Feedback (CONTEXT.md): a Founder's report that an answer or a saved record was wrong. The
# categories map to the coach's gates (G2 citations, G3 Gaps, G4 sycophancy, G5 memory).
FEEDBACK_CATEGORIES = {
    "wrong_citation": "a cited quote doesn't say what the answer claims",
    "missed_gap": "the coach answered where it should have said the talks don't cover it",
    "weak_advice": "the advice was generic, wrong for the Stage, or agreed with a weak plan",
    "wrong_memory": "the coach saved, recalled or updated something about the Founder wrongly",
    "other": "anything else",
}
FEEDBACK_QUESTION_MAX, FEEDBACK_ANSWER_MAX = 4_000, 20_000


def system_timezone() -> str:
    """Best-effort IANA name of this machine's zone (macOS/Linux symlink, else $TZ, else UTC)."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def home() -> Path:
    return Path(product.env("HOME") or product.default_home()).expanduser()
