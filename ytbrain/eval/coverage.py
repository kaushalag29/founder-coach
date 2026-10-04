"""Coverage evals (docs/library-and-packs-plan.md §2.4, §2.6): is `coverage` honest?

Two commands, no LLM:

  `ytbrain eval calibrate`  fits the curve that turns a hit's similarity into P(relevant) from your graded
                            labels (isotonic regression), checks it on held-out questions against the
                            provisional curve, and saves it for `pack build` (data/eval/calibration.json).
  `ytbrain eval gap`        measures the two ways coverage can be wrong, with the pack's own curve:
                            wrongful answers (a Gap question the coach would answer) and wrongful
                            refusals (an answerable question it would call a Gap), and sweeps where the
                            border sits so the thresholds can be tuned on the Gap questions.

Gap questions come from three places: the benchmark's own out-of-corpus questions (`answerable: false`),
the starter list shipped with ytbrain (gap_questions.txt) and your own (data/eval/gap-questions.txt, same
format: `question`, or `question | domain` for one that is a Gap only while the Library holds nothing in
that Domain). The search and the coverage rules are the server's (`founder_coach.coverage.for_search`),
so the numbers measured are the ones founders get.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from founder_coach import coverage as C
from founder_coach.search import search

STARTER = Path(__file__).with_name("gap_questions.txt")
MAX_WRONGFUL_ANSWERS = 0.10        # plan: Gap questions the coach answers
MAX_WRONGFUL_REFUSALS = 0.15       # plan: answerable questions the coach calls a Gap
MIN_GAP, MIN_ANSWERABLE = 20, 30   # fewer questions than this is a hint, not a verdict
SHIFTS = tuple(round(x / 100, 2) for x in range(-12, 13, 2))
TOP_K = 8                          # what a founder's search returns
EXIT_FAIL, EXIT_INCONCLUSIVE = 1, 3


@dataclass(frozen=True)
class Probe:
    qid: str
    text: str
    domain: str = ""               # a Gap question tagged with a Domain is a Gap only while the Library is empty there
    expected: frozenset = frozenset()   # an answerable question's Domains (its seed Document's)


def parse_gap_questions(text: str, prefix: str) -> list[Probe]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        q, _, dom = line.partition("|")
        if q.strip():
            out.append(Probe(f"{prefix}-{len(out) + 1:03d}", q.strip(), dom.strip()))
    return out


def gap_questions(extra: Path | None, queries: list[dict] | None = None) -> list[Probe]:
    """Starter + your own file + the benchmark's out-of-corpus questions, deduplicated by text."""
    probes = parse_gap_questions(STARTER.read_text(encoding="utf-8"), "starter") if STARTER.exists() else []
    if extra and extra.exists():
        try:                                       # your file may be any editor's: never a traceback over its bytes
            probes += parse_gap_questions(extra.read_text(encoding="utf-8", errors="replace"), "mine")
        except OSError as e:
            raise ValueError(f"cannot read {extra}: {e.strerror or e}") from e
    for q in queries or []:
        if q.get("answerable", True) is False:
            probes.append(Probe(q["_id"], q["text"]))
    seen, out = set(), []
    for p in probes:
        key = " ".join(p.text.lower().split())
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def active_gap(probes: list[Probe], have: set[str]) -> tuple[list[Probe], list[Probe]]:
    """(Gap questions that still are Gaps, those retired because their Domain now has items)."""
    keep = [p for p in probes if not p.domain or p.domain not in have]
    return keep, [p for p in probes if p not in keep]


# ------------------------------------------------------------------------------------------ observing
@dataclass
class Seen:
    rows: list[dict]
    info: dict


def _route(store):
    meta = getattr(store, "meta", None) or {}
    router = meta.get("router") or {}
    if router.get("enabled"):
        return {k: router[k] for k in ("margin", "max_domains", "top_n") if k in router}
    return False


def observe(store, embed, probes: list[Probe], say=print, reranker=None) -> dict[str, Seen]:
    """Search every probe the way the server does (same top_k, routing as the pack says)."""
    route, out = _route(store), {}
    for i, p in enumerate(probes, 1):
        info: dict = {}
        rows = search(store, embed, p.text, top_k=TOP_K, route=route, reranker=reranker, info=info)
        out[p.qid] = Seen(rows, info)
        if i % 50 == 0:
            say(f"    searched {i}/{len(probes)}")
    return out


def calibration_of(store) -> C.Calibration:
    return C.Calibration.from_meta(getattr(store, "meta", None) or {})


def level(seen: Seen, store, cal: C.Calibration, shift: float = 0.0) -> str:
    return C.for_search(seen.rows, seen.info, meta=getattr(store, "meta", None), calibration=cal.shifted(shift) if shift else cal,
                        semantic=True).level


# ------------------------------------------------------------------------------------------ the Gap eval
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def rates(levels_gap: list[str], levels_ans: list[str]) -> dict:
    ng, na = len(levels_gap), len(levels_ans)
    wa = sum(1 for lv in levels_gap if lv != "none")
    wr = sum(1 for lv in levels_ans if lv == "none")
    return {"gap_n": ng, "answered": wa, "confident": sum(1 for lv in levels_gap if lv == "strong"),
            "wrongful_answers": wa / ng if ng else None, "wrongful_answers_ci": wilson(wa, ng),
            "answerable_n": na, "refused": wr, "strong": sum(1 for lv in levels_ans if lv == "strong"),
            "wrongful_refusals": wr / na if na else None, "wrongful_refusals_ci": wilson(wr, na)}


def meets(r: dict) -> bool:
    return (r["wrongful_answers"] is not None and r["wrongful_answers"] <= MAX_WRONGFUL_ANSWERS and
            r["wrongful_refusals"] is not None and r["wrongful_refusals"] <= MAX_WRONGFUL_REFUSALS)


def recommend(sweep: dict[float, dict]) -> float | None:
    """Of the shifts meeting both targets, the one refusing the fewest answerable questions: a refusal is a hard
    failure, a `partial` on a Gap question is hedged by the host (plan decision #25). Ties: fewer wrongful answers,
    then the smaller move. None when no shift meets both."""
    ok = [s for s, r in sweep.items() if meets(r)]
    return min(ok, key=lambda s: (sweep[s]["wrongful_refusals"], sweep[s]["wrongful_answers"], abs(s))) if ok else None


def report(store, gap: list[Probe], answerable: list[Probe], seen_gap: dict, seen_ans: dict,
           retired: list[Probe] = ()) -> dict:
    cal = calibration_of(store)
    sweep = {}
    for s in SHIFTS:
        sweep[s] = rates([level(seen_gap[p.qid], store, cal, s) for p in gap],
                         [level(seen_ans[p.qid], store, cal, s) for p in answerable])
    now = sweep[0.0]
    by_domain: dict[str, dict] = {}
    for p in answerable:
        for d in sorted(p.expected):
            rec = by_domain.setdefault(d, {"n": 0, "refused": 0})
            rec["n"] += 1
            rec["refused"] += level(seen_ans[p.qid], store, cal) == "none"
    best = recommend(sweep)
    small = now["gap_n"] < MIN_GAP or now["answerable_n"] < MIN_ANSWERABLE
    status = "inconclusive" if small else ("pass" if meets(now) else "fail")
    leaks = [{"qid": p.qid, "text": p.text, "coverage": level(seen_gap[p.qid], store, cal),
              "top": [(h.get("title") or "")[:50] for h in seen_gap[p.qid].rows[:2]]}
             for p in gap if level(seen_gap[p.qid], store, cal) != "none"]
    return {"calibration": "provisional" if cal.provisional else "calibrated", "now": now, "sweep": sweep,
            "by_domain": by_domain, "recommended_shift": best, "status": status, "leaks": leaks,
            "retired": [p.text for p in retired]}


def render(rep: dict) -> str:
    n = rep["now"]
    pct = lambda x: "n/a" if x is None else f"{x:.1%}"
    ci = lambda t: f"[{t[0]:.0%}, {t[1]:.0%}]"
    lines = [f"coverage eval · curve {rep['calibration']} · {n['gap_n']} Gap questions, {n['answerable_n']} answerable questions", "",
             f"  wrongful answers  {pct(n['wrongful_answers']):>6}  95% CI {ci(n['wrongful_answers_ci'])}   (target <= {MAX_WRONGFUL_ANSWERS:.0%}; "
             f"{n['answered']} of {n['gap_n']} Gap questions answered, {n['confident']} with strong coverage)",
             f"  wrongful refusals {pct(n['wrongful_refusals']):>6}  95% CI {ci(n['wrongful_refusals_ci'])}   (target <= {MAX_WRONGFUL_REFUSALS:.0%}; "
             f"{n['refused']} of {n['answerable_n']} answerable questions called a Gap, {n['strong']} strong)"]
    if rep["by_domain"]:
        lines.append("\n  wrongful refusals by the question's Domain: " + ", ".join(
            f"{d} {v['refused']}/{v['n']}" for d, v in sorted(rep["by_domain"].items())))
    lines.append("\n  moving the border (similarity shift: wrongful answers / wrongful refusals):")
    for s, r in rep["sweep"].items():
        mark = "  <- now" if s == 0.0 else ("  <- recommended" if s == rep["recommended_shift"] else "")
        flag = " ok" if meets(r) else ""
        lines.append(f"    {s:+.2f}   {pct(r['wrongful_answers']):>6} / {pct(r['wrongful_refusals']):>6}{flag}{mark}")
    if rep["recommended_shift"] is None:
        lines.append("    no shift meets both targets: the curve needs fitting (`ytbrain eval calibrate`) or the Library needs more material")
    elif rep["recommended_shift"] != 0.0:
        lines.append(f"    `ytbrain eval gap --tune` saves the {rep['recommended_shift']:+.2f} shift for the next `pack build`")
    if rep["leaks"]:
        lines.append("\n  Gap questions that were answered (is any of them really answerable? then fix the question, not the curve):")
        for lk in rep["leaks"][:12]:
            lines.append(f"    [{lk['coverage']}] {lk['text']}  <- {'; '.join(lk['top'])}")
    if rep["retired"]:
        lines.append(f"\n  retired (their Domain now has items): {len(rep['retired'])} question(s)")
    small = n["gap_n"] < MIN_GAP or n["answerable_n"] < MIN_ANSWERABLE
    lines.append(f"\n  verdict: {rep['status'].upper()}" + (f"  (needs >= {MIN_GAP} Gap and >= {MIN_ANSWERABLE} answerable questions)" if small else ""))
    return "\n".join(lines)


def exit_code(rep: dict) -> int:
    return {"pass": 0, "fail": EXIT_FAIL, "inconclusive": EXIT_INCONCLUSIVE}[rep["status"]]


# ------------------------------------------------------------------------------------------ calibration
def label(hit: dict, qrels_q: dict, doc_grades: dict) -> int | None:
    """1 if the hit's Moment (a Document summary: its Document) was judged relevant (grade >= 2), 0 if judged
    otherwise, None if nobody judged it."""
    from .moments import moment_for
    if hit.get("kind") == "summary":
        g = doc_grades.get(hit["doc_id"])
    else:
        m = moment_for(hit["doc_id"], hit.get("start_ms"))
        g = qrels_q.get(m) if m else None
    return None if g is None else int(g >= 2)


def pairs(store, embed, queries: list[dict], qrels: dict, k: int = 10, say=print, reranker=None) -> list[tuple[str, float, int]]:
    """(question id, similarity, relevant?) for every judged hit in the top `k` of each answerable question."""
    from .run import doc_qrels
    docs = doc_qrels(qrels)
    out = []
    todo = [q for q in queries if q.get("answerable", True) and q["_id"] in qrels]
    for i, q in enumerate(todo, 1):
        rows = search(store, embed, q["text"], top_k=k, reranker=reranker)
        for h in rows:
            lab = label(h, qrels[q["_id"]], docs.get(q["_id"], {}))
            if lab is not None and h.get("similarity") is not None:
                out.append((q["_id"], float(h["similarity"]), lab))
        if i % 50 == 0:
            say(f"    searched {i}/{len(todo)}")
    return out


def brier(cal: C.Calibration, rows: list[tuple[float, int]]) -> float:
    return sum((cal.p(x) - y) ** 2 for x, y in rows) / len(rows) if rows else float("nan")


def calibrate(rows: list[tuple[str, float, int]]) -> dict:
    """Fit on all judged hits; check on held-out questions (two folds by question) against the provisional
    curve. Returns {"curve", "brier_fitted", "brier_provisional", "better", "table", "n", "questions"}."""
    pts = [(x, y) for _, x, y in rows]
    curve = C.fit(pts)
    qs = sorted({q for q, _, _ in rows})
    folds = [{q for i, q in enumerate(qs) if i % 2 == f} for f in (0, 1)]
    held_fit, held_prov = [], []
    for f in (0, 1):
        train = [(x, y) for q, x, y in rows if q not in folds[f]]
        test = [(x, y) for q, x, y in rows if q in folds[f]]
        try:
            cal = C.fit(train)
        except ValueError:
            continue
        held_fit.append(brier(cal, test) * len(test))
        held_prov.append(brier(C.Calibration(), test) * len(test))
    n_held = sum(1 for q, _, _ in rows)
    bf, bp = sum(held_fit) / n_held if held_fit else float("nan"), sum(held_prov) / n_held if held_prov else float("nan")
    table, srt = [], sorted(pts)
    bins = min(8, max(2, len(srt) // 25))
    for b in range(bins):
        chunk = srt[round(b * len(srt) / bins):round((b + 1) * len(srt) / bins)]
        if chunk:
            xm = sum(x for x, _ in chunk) / len(chunk)
            table.append({"similarity": round(xm, 3), "predicted": round(curve.p(xm), 3),
                          "observed": round(sum(y for _, y in chunk) / len(chunk), 3), "n": len(chunk)})
    ceiling = max(y for _, y in curve.points)
    unreachable = [t for t, r in C.RULES.items() if ceiling < r.strong]     # tiers whose "strong" no hit could ever reach
    return {"curve": curve, "brier_fitted": bf, "brier_provisional": bp, "better": bf < bp, "table": table,
            "n": len(rows), "questions": len(qs), "ceiling": round(ceiling, 3), "unreachable": unreachable}


def render_calibration(res: dict, saved: Path | None) -> str:
    lines = [f"calibration: {res['n']} judged hits from {res['questions']} questions", "",
             "  similarity  predicted  observed   n"]
    lines += [f"    {t['similarity']:.3f}      {t['predicted']:.2f}      {t['observed']:.2f}   {t['n']}" for t in res["table"]]
    lines += ["", f"  held-out Brier score: fitted {res['brier_fitted']:.4f} vs provisional {res['brier_provisional']:.4f} (lower is better)"]
    if res.get("unreachable"):
        need = ", ".join(f"{t} {C.RULES[t].strong:.2f}" for t in res["unreachable"])
        lines.append(f"  NOT SAVED: the fitted curve tops out at p={res['ceiling']:.2f}, so no hit can reach `strong` in the "
                     f"{'/'.join(res['unreachable'])} tier(s) (they need {need}); the judged labels are stricter than the "
                     "thresholds assume. Re-base the thresholds in founder_coach/coverage.py RULES, or --force to save anyway.")
    elif res["better"]:
        lines.append("  the fitted curve predicts relevance better than the provisional one" + (f"; saved to {saved}" if saved else ""))
    else:
        lines.append("  the fitted curve is NOT better than the provisional one on held-out questions: " +
                     ("not saved" if not saved else f"saved anyway to {saved}") + " (add judged questions first)")
    return "\n".join(lines)
