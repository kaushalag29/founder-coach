"""What's due, computed from the store every time it's asked (a Nudge is never stored),
plus the context digest the coach reads first and the FOUNDER.md view."""
from __future__ import annotations

import datetime as dt

from . import domain as D
from . import product
from .store import FounderStore, week_start


def nudges(store: FounderStore) -> list[dict]:
    """[{kind, message, ids?}] most important first; empty when nothing is due."""
    out: list[dict] = []
    prof = store.profile()
    if any(f not in prof for f in D.REQUIRED):
        return [{"kind": "setup", "message": D.SETUP_NUDGE}]
    today, week = store.today(), store.this_week()
    last = store.checkins(1)
    last_day = (dt.datetime.fromisoformat(last[0]["created_at"]).astimezone(store.tz()).date() if last else None)
    started = store.created_at()
    day = D.WEEKDAYS.index(prof["checkin_day"]["value"]) if "checkin_day" in prof else None
    checked_this_week = bool(last) and last[0]["week"] == week
    overdue_days = None
    if last_day and (today - last_day).days >= D.CHECKIN_OVERDUE_DAYS:
        overdue_days = (today - last_day).days - 7
    elif day is not None and today.weekday() > day and not checked_this_week and (
            last_day or (started and (today - started.astimezone(store.tz()).date()).days >= 7)):
        overdue_days = today.weekday() - day
    elif not last_day and started and (today - started.astimezone(store.tz()).date()).days >= D.CHECKIN_OVERDUE_DAYS:
        overdue_days = (today - started.astimezone(store.tz()).date()).days - 7
    if overdue_days is not None:
        out.append({"kind": "checkin_overdue",
                    "message": f"Check-in overdue by {max(1, overdue_days)} day(s)"
                               + (f" (last one {last_day.isoformat()})" if last_day else " (none yet)")})
    old = store.overdue_commitments()
    if old:
        out.append({"kind": "open_past_commitments", "ids": [c["id"] for c in old],
                    "message": f"{len(old)} Commitment(s) from earlier weeks still open: review them as done, "
                               "dropped or carried"})
    stale = [f for f, v in prof.items() if v["stale"]]
    if stale:
        out.append({"kind": "stale_profile", "fields": stale,
                    "message": f"Profile facts not confirmed in {D.PROFILE_STALE_DAYS}+ days: {', '.join(stale)}; "
                               "ask the Founder whether they still hold"})
    late = [g for g in store.goals("active") if g.get("target_date") and g["target_date"] < today.isoformat()]
    if late:
        out.append({"kind": "goal_past_target", "ids": [g["id"] for g in late],
                    "message": f"{len(late)} active Goal(s) past their target date ("
                               + "; ".join(f"{g['text'][:60]} by {g['target_date']}" for g in late[:3])
                               + "): ask whether each was met, should be dropped, or gets a new date"})
    from . import product as _p
    if _p.has("holdings"):
        out += _holdings_nudges(store, prof, today)
    rev = store.decisions_to_revisit()
    if rev:
        out.append({"kind": "decision_revisit", "ids": [d["id"] for d in rev],
                    "message": f"{len(rev)} Decision(s) due for a revisit"})
    return out


def _holdings_nudges(store: FounderStore, prof: dict, today: dt.date) -> list[dict]:
    """The investor Pack (I4): Holdings older than the person's review interval want a fresh import before any
    review; a policy with targets but no Holdings yet wants a first one."""
    snaps, _ = store.holdings()
    if not snaps:
        if "targets" in prof:
            return [{"kind": "holdings_missing", "message": "No Holdings yet: import a positions CSV from each "
                     "account (coach_holdings) before a review"}]
        return []
    latest: dict[str, str] = {}
    for s in snaps:
        latest[s["account"]] = max(latest.get(s["account"], ""), s["as_of"])
    oldest = min(latest.values())
    days = prof.get("review_days", {}).get("value")
    if days and (today - dt.date.fromisoformat(oldest)).days > int(days):
        old = sorted(a for a, d in latest.items() if (today - dt.date.fromisoformat(d)).days > int(days))
        return [{"kind": "holdings_stale", "message": f"Holdings older than your {int(days)}-day review interval ("
                 + ", ".join(f"{a} as of {latest[a]}" for a in old) + "): re-import those positions before a review"}]
    return []


# A Nudge about a memory module the Pack doesn't keep is never shown (ADR-0016: modules per Pack)
NUDGE_MODULE = {"checkin_overdue": "checkins", "open_past_commitments": "commitments",
                "goal_past_target": "goals", "decision_revisit": "decisions", "holdings_stale": "holdings",
                "holdings_missing": "holdings"}


def context(store: FounderStore, detailed: bool = False) -> dict:
    """Everything a Playbook needs to start, in one read: only the memory modules this Pack keeps."""
    from . import product
    has = product.has
    week = store.this_week()
    prof = store.profile()
    ctx = {"today": store.today().isoformat(), "week": week,
           "week_starts": week_start(week).isoformat(),
           "timezone": str(store.tz().key),
           # a stale fact says when it was last confirmed, so the coach can ask "still true since <date>?"
           "profile": prof if detailed else {f: ({"value": v["value"], "stale": True,
                                                  "confirmed_on": v["confirmed_at"][:10]} if v["stale"] else v["value"])
                                             for f, v in prof.items()},
           "goals": store.goals("active") if has("goals") else [],
           "this_week": store.commitments(week) if has("commitments") else [],
           "overdue": store.overdue_commitments() if has("commitments") else [],
           "last_checkin": (store.checkins(1) or [None])[0] if has("checkins") else None,
           "recent_decisions": store.decisions(5) if has("decisions") else [],
           "nudges": [x for x in nudges(store) if x["kind"] not in NUDGE_MODULE or has(NUDGE_MODULE[x["kind"]])]}
    if not detailed:
        for c in ctx["this_week"] + ctx["overdue"]:
            c.pop("created_at", None)
            c.pop("closed_at", None)
    return ctx


def hook_text(store: FounderStore, limit: int = 1500) -> str:
    """One short paragraph for the SessionStart hook; empty when nothing is due."""
    items = nudges(store)
    if not items:
        return ""
    open_now = len(store.commitments(store.this_week(), "open"))
    parts = [n["message"].rstrip(".") for n in items]
    if open_now and not any(n["kind"] == "setup" for n in items):
        parts.append(f"{open_now} open Commitment(s) this week")
    text = (f"{D.COACH_NAME}: " + " · ".join(parts) + product.reword(
        ". If the Founder raises startup work, answer it first, then offer the matching Playbook (check-in or "
        "setup) once; otherwise don't interrupt."))
    return text[:limit]


def _cite(store: FounderStore, ids: list[str]) -> str:
    if not ids:
        return ""
    out = []
    for i in ids:
        info = store.cite_lookup(i) if store.cite_lookup else None
        out.append(f"[{info['title']}]({info['deep_link']})" if info else f"`{i}`")
    return " · sources: " + ", ".join(out)


def render_markdown(store: FounderStore) -> str:
    """FOUNDER.md: what the coach believes, for the Founder to read and correct."""
    prof = store.profile()
    week = store.this_week()
    lines = [f"# What the {D.COACH_NAME.lower()} remembers", "",
             f"_Updated {store.now().astimezone(store.tz()).strftime('%Y-%m-%d %H:%M %Z')}. This file is "
             "regenerated after every change; edit through the coach (\"my stage is now MVP\"), not here._", ""]
    for n in nudges(store):
        lines.append(f"> **Due:** {n['message']}")
    if lines[-1].startswith(">"):
        lines.append("")
    lines += ["## Profile", ""]
    if not prof:
        lines.append("_Nothing yet: run setup._")
    for f, v in prof.items():
        val = v["value"]
        if isinstance(val, dict):
            val = ", ".join(f"{k}: {x}" for k, x in val.items())
        lines.append(f"- **{f.replace('_', ' ')}:** {val}" + ("  _(not confirmed in 30+ days)_" if v["stale"] else ""))
    lines += ["", "## Goals", ""]
    goals = store.goals("active")
    today = store.today().isoformat()
    lines += [f"- `{g['id']}` {g['text']}" + (f" — measure: {g['measure']}" if g.get("measure") else "")
              + (f" — by {g['target_date']}" if g.get("target_date") else "")
              + ("  _(past its date: met, dropped or a new date?)_" if g.get("target_date") and g["target_date"] < today else "")
              + _cite(store, g["citations"])
              for g in goals] or ["_None active._"]
    lines += ["", f"## This week ({week})", ""]
    cs = store.commitments(week)
    for c in cs:
        box = {"open": "[ ]", "done": "[x]", "dropped": "[-]", "carried": "[>]"}[c["status"]]
        when = f"{c['cue']}: " if c.get("cue") else ""
        carried = f" _(carried {c['carried_weeks']} week(s))_" if c.get("carried_weeks") else ""
        lines.append(f"- {box} `{c['id']}` {when}{c['action']} → {c['outcome']}{carried}"
                     + (f" — {c['result_note']}" if c.get("result_note") else "") + _cite(store, c["citations"]))
    if not cs:
        lines.append("_No Commitments yet this week._")
    over = store.overdue_commitments()
    if over:
        lines += ["", "### Still open from earlier weeks", ""]
        lines += [f"- [ ] `{c['id']}` ({c['week']}) {c['action']} → {c['outcome']}" for c in over]
    lines += ["", "## Recent Check-ins", ""]
    ks = store.checkins(3)
    for k in ks:
        lines.append(f"- **{k['week']}** {k['summary']}"
                     + (f" — wins: {'; '.join(k['wins'])}" if k["wins"] else "")
                     + (f" — blockers: {'; '.join(k['blockers'])}" if k["blockers"] else ""))
    if not ks:
        lines.append("_None yet._")
    lines += ["", "## Decisions", ""]
    ds = store.decisions(10)
    for d in ds:
        lines.append(f"- `{d['id']}` {d['decided_at'][:10]}: {d['text']} — why: {d['reasoning']}"
                     + (f" (revisit {d['revisit_on']})" if d.get("revisit_on") else "") + _cite(store, d["citations"]))
    if not ds:
        lines.append("_None yet._")
    from . import product as _p
    if _p.has("holdings"):
        snaps, _ = store.holdings()
        lines += ["", "## Holdings", ""]
        latest = {}
        for s in snaps:
            latest[s["account"]] = s
        from .invest import money
        lines += [f"- {a}: {money(s['total_cents'])} as of {s['as_of']} ({s['broker']} export)"
                  for a, s in sorted(latest.items())] or ["_None imported yet._"]
    lines += ["", "## Recent changes", ""]
    for ch in store.recent_changes(10):
        lines.append(f"- {ch['at'][:16].replace('T', ' ')} {ch['op']} {ch['entity']} `{ch['entity_id']}` ({ch['source']})")
    return "\n".join(lines) + "\n"


def render_common_markdown(store: FounderStore) -> str:
    """YOU.md: the Common profile every coach on this machine may read (only the fields its Pack shares)."""
    prof = store.profile()
    lines = ["# What your coaches share about you", "",
             f"_Updated {store.now().astimezone(store.tz()).strftime('%Y-%m-%d %H:%M %Z')}. Each coach reads only "
             "the fields its Pack shares; nothing about a Project is kept here. Edit through a coach, not here._", ""]
    for f, v in prof.items():
        lines.append(f"- **{f.replace('_', ' ')}:** {v['value']}")
    if not prof:
        lines.append("_Nothing yet._")
    return "\n".join(lines) + "\n"
