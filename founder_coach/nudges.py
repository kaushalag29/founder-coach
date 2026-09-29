"""What's due, computed from the store every time it's asked (a Nudge is never stored),
plus the context digest the coach reads first and the FOUNDER.md view."""
from __future__ import annotations

import datetime as dt

from . import domain as D
from .store import FounderStore, week_start


def nudges(store: FounderStore) -> list[dict]:
    """[{kind, message, ids?}] most important first; empty when nothing is due."""
    out: list[dict] = []
    prof = store.profile()
    if "stage" not in prof or "company" not in prof:
        return [{"kind": "setup", "message": "No Founder profile yet: answer the Founder's question first, then "
                 "offer the setup Playbook once (company, customer, Stage, one Goal, Check-in day)."}]
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
    rev = store.decisions_to_revisit()
    if rev:
        out.append({"kind": "decision_revisit", "ids": [d["id"] for d in rev],
                    "message": f"{len(rev)} Decision(s) due for a revisit"})
    return out


def context(store: FounderStore, detailed: bool = False) -> dict:
    """Everything a Playbook needs to start, in one read."""
    week = store.this_week()
    prof = store.profile()
    ctx = {"today": store.today().isoformat(), "week": week,
           "week_starts": week_start(week).isoformat(),
           "timezone": str(store.tz().key),
           "profile": prof if detailed else {f: ({"value": v["value"], "stale": True} if v["stale"] else v["value"])
                                             for f, v in prof.items()},
           "goals": store.goals("active"),
           "this_week": store.commitments(week),
           "overdue": store.overdue_commitments(),
           "last_checkin": (store.checkins(1) or [None])[0],
           "recent_decisions": store.decisions(5),
           "nudges": nudges(store)}
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
    text = ("Founder coach: " + " · ".join(parts) +
            ". If the Founder raises startup work, answer it first, then offer the matching Playbook (check-in or "
            "setup) once; otherwise don't interrupt.")
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
    lines = ["# What the founder coach remembers", "",
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
    lines += [f"- `{g['id']}` {g['text']}" + (f" — measure: {g['measure']}" if g.get("measure") else "")
              + (f" — by {g['target_date']}" if g.get("target_date") else "") + _cite(store, g["citations"])
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
    lines += ["", "## Recent changes", ""]
    for ch in store.recent_changes(10):
        lines.append(f"- {ch['at'][:16].replace('T', ' ')} {ch['op']} {ch['entity']} `{ch['entity_id']}` ({ch['source']})")
    return "\n".join(lines) + "\n"
