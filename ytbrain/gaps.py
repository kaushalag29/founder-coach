"""`ytbrain gaps`: what the dogfooded coaches could not answer well, from their local usage logs.

Every coach tool call leaves one row in the `usage` table of its Project's store
(`~/.ytbrain/<pack>/projects/<id>/founder.db`; docs/usage-log.md). This reads those tables and nothing else:
no goals, decisions or profile, and it never opens a store through the coach's own code (no migration, no
backup, no write). A search is rated `strong`, `partial` or `none` (its coverage); the report counts them per
coach and Domain, shows where the border between strong and partial sits in similarity, and, when the question
text was recorded (`<PREFIX>USAGE_TEXT=1`, your own machine only), lists the questions behind the weak ones.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import statistics
import tempfile
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = ("strong", "partial", "none", "empty", "unrated")     # unrated: a search from before coverage was logged
DB_NAME = "founder.db"
ASK_GAP_S = 120          # searches of one session closer than this belong to one ask
ROUND_GAP_S = 5          # ... and closer than this to one round (a round's searches go out together)
SEARCH_GUARD = 12        # the skills' cap on searches per ask (0.1.7)


def engine_home() -> Path:
    return Path(os.environ.get("YTBRAIN_HOME") or "~/.ytbrain").expanduser()


@dataclass
class Project:
    pack: str                     # founder, coding, investor
    project_id: str
    name: str
    db: Path
    rows: list[dict] = field(default_factory=list)
    note: str = ""                # why it has no rows (no usage table, unreadable)


def _product_id(home: Path, pack: str) -> str:
    """The coach's product id for a Pack folder (its env prefix is this, upper-cased), from the registry
    each coach writes when it starts; `<pack>-coach` if it never registered."""
    try:
        for f in (home / "installed").glob("*.json"):
            d = json.loads(f.read_text())
            if d.get("pack") == pack and d.get("product_id"):
                return str(d["product_id"])
    except (OSError, ValueError):
        pass
    return f"{pack}-coach"


def env_prefix(home: Path, pack: str) -> str:
    return _product_id(home, pack).upper().replace("-", "_") + "_"


def discover(home: Path | None = None, pack: str | None = None) -> list[Project]:
    """Every Project store under the engine home: `<home>/<pack>/projects/<id>/founder.db`."""
    home = home or engine_home()
    out: list[Project] = []
    if not home.is_dir():
        return out
    for pdir in sorted(p for p in home.iterdir() if (p / "projects").is_dir()):
        if pack and pdir.name != pack:
            continue
        for d in sorted(x for x in (pdir / "projects").iterdir() if (x / DB_NAME).is_file()):
            name = d.name
            try:
                name = json.loads((d / "project.json").read_text()).get("name") or d.name
            except (OSError, ValueError):
                pass
            out.append(Project(pack=pdir.name, project_id=d.name, name=str(name), db=d / DB_NAME))
    return out


def _read_usage(db: Path) -> tuple[list[dict], str]:
    """The usage rows of one store. The store is in WAL mode and a running coach keeps its newest rows in the
    -wal file, so the file and its -wal are copied to a temporary folder and read there: the original is never
    opened, so nothing (not even a -shm file) appears next to it."""
    tmp = tempfile.mkdtemp(prefix="ytbrain-gaps-")
    try:
        for suffix in ("", "-wal"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, Path(tmp) / (DB_NAME + suffix))
        c = sqlite3.connect(Path(tmp) / DB_NAME, timeout=1)
        try:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute("SELECT at, run, tool, outcome, duration_ms, version, detail "
                                               "FROM usage ORDER BY at, id")]
            return rows, ""
        finally:
            c.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return [], "no usage log yet (the store predates it, or the coach has not run)"
        return [], f"unreadable ({e})"
    except (sqlite3.Error, OSError) as e:
        return [], f"unreadable ({type(e).__name__})"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load(home: Path | None = None, pack: str | None = None, days: int | None = 30,
         now: dt.datetime | None = None) -> list[Project]:
    projects = discover(home, pack)
    cutoff = None
    if days is not None:
        cutoff = ((now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    for p in projects:
        rows, p.note = _read_usage(p.db)
        for r in rows:
            try:
                r["detail"] = json.loads(r.get("detail") or "{}")
            except ValueError:
                r["detail"] = {}
        p.rows = [r for r in rows if cutoff is None or r["at"] >= cutoff]
    return projects


def level_of(row: dict) -> str:
    """strong / partial / none / empty / unrated for one coach_search row."""
    d = row["detail"]
    if row["outcome"] == "empty":
        return "empty"
    if row["outcome"] == "gap":
        return "none"
    return d.get("coverage") if d.get("coverage") in ("strong", "partial", "none") else "unrated"


class HitDomains:
    """item_id -> Domains, read from the Knowledge pack a coach registered under <home>/installed/ (its pack_path),
    opened read-only and immutable: a search that named no Domain is attributed to the Domain most of its logged
    hits belong to."""

    def __init__(self, pack_file: Path | None):
        self.path = pack_file if pack_file and pack_file.is_file() else None
        self._cache: dict[str, list[str]] = {}

    @classmethod
    def for_pack(cls, home: Path, pack: str) -> "HitDomains":
        try:
            for f in (home / "installed").glob("*.json"):
                d = json.loads(f.read_text())
                if d.get("pack") == pack and d.get("pack_path"):
                    return cls(Path(d["pack_path"]) / "knowledge.sqlite")
        except (OSError, ValueError):
            pass
        return cls(None)

    def domains(self, item_ids: list[str]) -> list[list[str]]:
        want = [i for i in item_ids if i not in self._cache]
        if want and self.path:
            try:
                c = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True)
                try:
                    q = f"SELECT item_id, domains FROM items WHERE item_id IN ({','.join('?' * len(want))})"
                    for iid, doms in c.execute(q, want):
                        self._cache[iid] = json.loads(doms or "[]")
                finally:
                    c.close()
            except (sqlite3.Error, ValueError):
                self.path = None                     # unreadable: stop trying, the report says how many were unknown
        return [self._cache[i] for i in item_ids if i in self._cache]


def domain_levels(row: dict, hits: HitDomains | None = None) -> dict[str, str]:
    """Domain -> level for one search: its per-Domain coverage when logged; else the overall level for each
    Domain it named; else (no Domain named: the whole Library) for the Domain most of its hits belong to.
    The last two are approximate."""
    d = row["detail"]
    by = d.get("coverage_by_domain")
    if isinstance(by, dict) and by:
        return {str(k): str(v) for k, v in by.items()}
    lv = level_of(row)
    if lv not in ("strong", "partial", "none"):
        return {}
    if d.get("domains_searched"):
        return {str(k): lv for k in d["domains_searched"]}
    if hits is None:
        return {}
    counts: dict[str, int] = {}
    for doms in hits.domains([str(i) for i in (d.get("items") or [])]):
        for dom in doms:
            counts[dom] = counts.get(dom, 0) + 1
    if not counts:
        return {}
    top = max(counts.values())
    return {dom: lv for dom, n in counts.items() if n == top}


def _ts(at: str) -> float:
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(at).timestamp()
    except ValueError:
        return 0.0


def asks_of(searches: list[dict]) -> list[list[list[dict]]]:
    """Searches grouped into asks (same session, under ASK_GAP_S apart), each ask into rounds (under ROUND_GAP_S
    apart). Timestamps only: an approximation the report labels as such."""
    asks: list[list[list[dict]]] = []
    prev = None
    for r in sorted(searches, key=lambda r: (r.get("run") or "", r["at"])):
        t = _ts(r["at"])
        if prev is None or r.get("run") != prev[0] or t - prev[1] > ASK_GAP_S:
            asks.append([[r]])
        elif t - prev[1] > ROUND_GAP_S:
            asks[-1].append([r])
        else:
            asks[-1][-1].append(r)
        prev = (r.get("run"), t)
    return asks


def _sims(rows: list[dict]) -> dict | None:
    xs = [r["detail"]["top_similarity"] for r in rows if isinstance(r["detail"].get("top_similarity"), (int, float))]
    if not xs:
        return None
    return {"n": len(xs), "min": round(min(xs), 3), "median": round(statistics.median(xs), 3), "max": round(max(xs), 3)}


def summarize(p: Project, questions: int = 15, hits: HitDomains | None = None) -> dict:
    searches = [r for r in p.rows if r["tool"] == "coach_search"]
    by_level = {lv: sum(level_of(r) == lv for r in searches) for lv in LEVELS}
    domains: dict[str, dict[str, int]] = {}
    for r in searches:
        for dom, lv in domain_levels(r, hits).items():
            domains.setdefault(dom, {"strong": 0, "partial": 0, "none": 0})
            if lv in domains[dom]:
                domains[dom][lv] += 1
    by_hits = sum(1 for r in searches if not r["detail"].get("coverage_by_domain") and not r["detail"].get("domains_searched")
                  and domain_levels(r, hits))
    asks = asks_of(searches)
    per_ask = [sum(len(rd) for rd in a) for a in asks]
    rounds = [len(a) for a in asks]
    routed = [r["detail"].get("routing") for r in searches if r["detail"].get("routing")]
    weak = [r for r in searches if level_of(r) in ("partial", "none", "empty")]
    with_text = [r for r in searches if r["detail"].get("query")]
    weak_text = [r for r in weak if r["detail"].get("query")]
    return {
        "pack": p.pack, "project": p.name, "project_id": p.project_id, "note": p.note,
        "calls": len(p.rows), "searches": len(searches), "first": p.rows[0]["at"] if p.rows else None,
        "last": p.rows[-1]["at"] if p.rows else None, "levels": by_level,
        "similarity": {lv: _sims([r for r in searches if level_of(r) == lv]) for lv in ("strong", "partial", "none")},
        "domains": dict(sorted(domains.items())), "domains_from_hits": by_hits,
        "asks": {"n": len(asks), "searches_avg": round(sum(per_ask) / len(asks), 1) if asks else None,
                 "searches_max": max(per_ask, default=None), "rounds_avg": round(sum(rounds) / len(asks), 1) if asks else None,
                 "rounds_max": max(rounds, default=None), "at_guard": sum(n >= SEARCH_GUARD for n in per_ask)},
        "explicit_domains": {"n": sum(x == "explicit" for x in routed), "of": len(routed)},
        "errors": sum(r["outcome"] == "error" for r in p.rows),
        "searches_with_text": len(with_text),
        "weak_questions": [{"at": r["at"][:19], "level": level_of(r), "top_similarity": r["detail"].get("top_similarity"),
                            "domains": sorted(domain_levels(r, hits)) or r["detail"].get("domains_searched") or [],
                            "query": r["detail"]["query"]}
                           for r in reversed(weak_text)][:questions],
    }


def report(projects: list[Project], home: Path | None = None, days: int | None = 30, questions: int = 15) -> dict:
    home = home or engine_home()
    return {"home": str(home), "days": days,
            "projects": [summarize(p, questions, HitDomains.for_pack(home, p.pack)) for p in projects]}


def render(rep: dict, home: Path | None = None) -> str:
    home = home or Path(rep["home"])
    lines: list[str] = []
    if not rep["projects"]:
        return f"gaps: no coach Projects under {rep['home']} (use a coach in a Project first; `/setup` makes one)"
    span = f"last {rep['days']} days" if rep["days"] else "all time"
    for s in rep["projects"]:
        lines.append(f"{s['pack']} coach · Project {s['project']} ({s['project_id']}) · {span}")
        if s["note"]:
            lines.append(f"  {s['note']}")
            lines.append("")
            continue
        n, lv = s["searches"], s["levels"]
        if not n:
            lines.append(f"  {s['calls']} tool call(s), no searches in this window")
            lines.append("")
            continue
        rated = n - lv["unrated"]                      # percentages are of the searches that carry a coverage level
        pct = lambda k: f"{lv[k]} ({round(100 * lv[k] / rated)}%)" if rated else str(lv[k])    # noqa: E731
        lines.append(f"  searches {n} ({rated} rated, {lv['unrated']} from before coverage was logged): strong "
                     f"{pct('strong')} · partial {pct('partial')} · none {pct('none')} · empty {lv['empty']}   "
                     f"({s['first'][:10]} to {s['last'][:10]})")
        sim = s["similarity"]
        bits = [f"{k} {v['min']}-{v['max']} (median {v['median']})" for k, v in sim.items() if v]
        if bits:
            lines.append("  top similarity: " + " · ".join(bits))
        a = s["asks"]
        if a["n"]:
            lines.append(f"  asks (grouped by time, approximate) {a['n']}: searches per ask {a['searches_avg']} (max "
                         f"{a['searches_max']}), rounds per ask {a['rounds_avg']} (max {a['rounds_max']}); "
                         f"{a['at_guard']} at the {SEARCH_GUARD}-search guard")
        e = s["explicit_domains"]
        if e["of"]:
            lines.append(f"  Domains named by the host in {e['n']} of {e['of']} searches "
                         f"({round(100 * e['n'] / e['of'])}%); the rest searched the whole Library")
        if s["domains"]:
            note = (f", {s['domains_from_hits']} search(es) attributed by their hits' Domain"
                    if s["domains_from_hits"] else "")
            lines.append(f"  by Domain (strong / partial / none){note}:")
            for dom, c in s["domains"].items():
                lines.append(f"    {dom:14} {c['strong']:3} / {c['partial']:3} / {c['none']:3}")
        if s["errors"]:
            lines.append(f"  {s['errors']} tool error(s): `founder-coach usage summary` lists them")
        weak = lv["partial"] + lv["none"] + lv["empty"]
        if weak and not s["searches_with_text"]:
            lines.append(f"  {weak} weak search(es) but no question text was recorded, so what was missing is unknown. "
                         f"On this machine set {env_prefix(home, s['pack'])}USAGE_TEXT=1 for the {s['pack']} coach "
                         f"(local only; the text never leaves your store).")
        elif s["weak_questions"]:
            lines.append(f"  weak searches (newest first, {len(s['weak_questions'])} of {weak}):")
            for q in s["weak_questions"]:
                sim_s = "" if q["top_similarity"] is None else f" sim {q['top_similarity']}"
                lines.append(f"    {q['at']} {q['level']:7}{sim_s} [{', '.join(q['domains'])}] {q['query']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
