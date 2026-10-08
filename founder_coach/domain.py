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

# The founder Pack's profile facts: field -> (kind, description). A Pack declares its own in pack.toml
# ([[profile]], carried into product.json); this is the founder Pack's, for a product.json from before Packs.
# tests/test_plugin.py checks it equals packs/founder/pack.toml.
FOUNDER_PROFILE: dict[str, tuple[str, str]] = {
    "company": ("text", "company name"),
    "one_liner": ("text", "what the company does, in one sentence"),
    "customer": ("text", "who the customer is"),
    "stage": ("stage", "one of: " + ", ".join(STAGES)),
    "team_size": ("int", "number of people working on the company"),
    "key_metrics": ("metrics", "the few numbers that matter now, e.g. {\"MRR\": \"$4k\", \"design partners\": 3}"),
    "timezone": ("tz", "IANA time zone, e.g. Asia/Kolkata"),
    "checkin_day": ("weekday", "preferred Check-in day: " + ", ".join(WEEKDAYS)),
    "workspace": ("places", "where the Founder keeps their own data, in their words: {what: tool or place}, e.g. "
                            "{\"pipeline\": \"HubSpot\", \"metrics\": \"Google Sheet 'KPIs'\", "
                            "\"investor updates\": \"Drive folder 'Updates'\"}"),
    "name": ("text", "what to call the person"),
    "role": ("text", "the person's role, e.g. CEO, CTO, solo founder"),
    "answer_style": ("text", "how they like answers, e.g. 'short, bullet points first'"),
}
FOUNDER_NEVER_STALE = ("timezone", "checkin_day", "workspace", "name", "role", "answer_style")
# The Common profile (ADR-0016): facts about the person, not a Project, that one coach may share with the
# others on a yes. A Pack lists which of these it reads and offers to save there (pack.toml common_fields).
COMMON_FIELDS = ("timezone", "name", "role", "answer_style")
# ...and every Pack's profile has them, so a Project can keep its own value (a Pack may describe them itself)
PERSON_FIELDS: dict[str, tuple[str, str]] = {f: FOUNDER_PROFILE[f] for f in COMMON_FIELDS}


def _pack_profile() -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, ...]], tuple[str, ...]]:
    spec = product.PACK.get("profile")
    if not spec:
        return dict(FOUNDER_PROFILE), {}, FOUNDER_NEVER_STALE
    fields = {f["name"]: (f.get("kind", "text"), f["description"]) for f in spec}
    values = {f["name"]: tuple(f["values"]) for f in spec if f.get("kind") == "enum"}
    never = [f["name"] for f in spec if not f.get("stale", True)]
    for name, kd in PERSON_FIELDS.items():
        if name not in fields:
            fields[name] = kd
            never.append(name)
    return fields, values, tuple(never)


# This Pack's profile facts: field -> (kind, description); an enum field's values; facts that don't drift on
# their own, never flagged stale (the rest want confirming after PROFILE_STALE_DAYS)
PROFILE_FIELDS, FIELD_VALUES, NEVER_STALE = _pack_profile()
# setup is offered until these are saved
REQUIRED = tuple(product.PACK.get("required") or (("company", "stage") if "company" in PROFILE_FIELDS
                                                   else tuple(PROFILE_FIELDS)[:1]))
_RUNTIME = product.PACK.get("runtime") or {}
COACH_NAME = _RUNTIME.get("coach_name") or "Founder coach"
SETUP_NUDGE = _RUNTIME.get("setup_nudge") or (
    "No Founder profile yet: answer the Founder's question first, then offer the setup Playbook once (company, "
    "customer, Stage, one Goal, Check-in day).")

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

ID_PREFIX = {"goal": "g", "commitment": "c", "decision": "d", "checkin": "k", "feedback": "f", "holdings": "h"}
KIND_OF_PREFIX = {v: k for k, v in ID_PREFIX.items() if k not in ("feedback", "holdings")}   # records coach_update may touch

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


def pack_home() -> Path:
    """This coach's own folder, for what isn't a Project's memory (the pack memo, a pack copied by hand):
    <PREFIX>HOME when one data folder is pinned, else ~/.ytbrain/<pack id> (ADR-0016)."""
    if product.env("HOME"):
        return home()
    from .installed import engine_home
    return engine_home() / str(product.PACK.get("id") or "founder")
