"""Routing eval (docs/library-and-packs-plan.md §2.6): does the router find the Domains a question belongs to?

Every answerable question was written from a seed Document, so its expected Domains are that
Document's. No new labels are needed. Two kinds of prompt are scored:

  single    the question as written: is the best-scoring Domain one of the expected ones, and is the
            routed set exactly the expected set?
  composed  two questions from different Domains joined into one prompt ("... Also, ..."), as a founder
            asks about a fundraise and a hard conversation in one breath: is every expected Domain routed
            (recall), and how many routed Domains were wrong (precision)?

Each question's per-Domain scores are computed once; the router's settings (margin, max Domains) are then
swept offline over them, so tuning costs no further embedding. The numbers say whether to turn routing on
(`ytbrain pack build --route`) and with which margin; the answer-side check is
`ytbrain eval run --config pack-route --compare pack-no-rerank`.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from founder_coach.router import MARGIN, MAX_DOMAINS, TOP_N, choose
from .moments import parse_moment_id


@dataclass(frozen=True)
class Item:
    qid: str
    text: str
    expected: frozenset


def doc_domains(store) -> dict[str, frozenset]:
    """doc_id -> its Domains, from the store's items."""
    out: dict[str, set] = {}
    for r in store.rows(columns=["doc_id", "domains"]):
        out.setdefault(r["doc_id"], set()).update(r.get("domains") or [])
    return {d: frozenset(v) for d, v in out.items()}


def questions(queries: list[dict], domains_of: dict[str, frozenset]) -> list[Item]:
    """The answerable questions whose seed Document the store knows."""
    out = []
    for q in queries:
        if not q.get("answerable", True):
            continue
        seed = (q.get("creation") or {}).get("seed_moment")
        if not seed:
            continue
        exp = domains_of.get(parse_moment_id(seed)[0])
        if exp:
            out.append(Item(q["_id"], q["text"], exp))
    return sorted(out, key=lambda i: i.qid)


def compose(items: list[Item], n: int, seed: int = 17) -> list[Item]:
    """Up to `n` prompts that join two questions with disjoint expected Domains (deterministic)."""
    rng = random.Random(seed)
    pool = list(items)
    rng.shuffle(pool)
    out, used = [], set()
    for i, a in enumerate(pool):
        if len(out) >= n:
            break
        b = next((x for x in pool[i + 1:] if not (x.expected & a.expected) and x.qid not in used), None)
        if b is None or a.qid in used:
            continue
        used |= {a.qid, b.qid}
        out.append(Item(f"{a.qid}+{b.qid}", f"{a.text.strip()} Also, {b.text.strip()}", a.expected | b.expected))
    return out


def collect(store, embed, items: list[Item], top_n: int = TOP_N, every: int = 25, say=print) -> list[dict]:
    """Per-Domain scores of each item's text."""
    out = []
    for n, it in enumerate(items, 1):
        out.append(store.domain_scores(embed([it.text])[0], top_n))
        if n % every == 0:
            say(f"    route: {n}/{len(items)}")
    return out


def score(items: list[Item], scores: list[dict], margin: float, max_domains: int) -> dict:
    n = len(items) or 1
    top1 = exact = recall = 0
    prec = 0.0
    for it, sc in zip(items, scores):
        r = choose(sc, margin, max_domains)
        routed = set(r.domains) or set(sc)             # nothing routed = the whole Library
        best = max(sc, key=sc.get) if sc else None
        top1 += best in it.expected
        exact += routed == set(it.expected)
        recall += set(it.expected) <= routed
        prec += len(routed & set(it.expected)) / len(routed) if routed else 0.0
    return {"n": len(items), "top1": top1 / n, "exact": exact / n, "recall": recall / n, "precision": prec / n}


MARGINS = (0.0, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2)


def report(single: list[Item], single_scores: list[dict], composed: list[Item], composed_scores: list[dict],
           margins=MARGINS, max_domains: int = MAX_DOMAINS) -> dict:
    by_domain: dict[str, int] = {}
    for it in single:
        for d in it.expected:
            by_domain[d] = by_domain.get(d, 0) + 1
    return {"questions_by_domain": dict(sorted(by_domain.items())), "max_domains": max_domains,
            "sweep": [{"margin": m, "single": score(single, single_scores, m, max_domains),
                       "composed": score(composed, composed_scores, m, max_domains)} for m in margins]}


def render(rep: dict) -> str:
    lines = ["routing eval: questions per expected Domain: " + ", ".join(f"{d} {n}" for d, n in rep["questions_by_domain"].items()),
             f"  (the default margin is {MARGIN}; at most {rep['max_domains']} Domains)", "",
             "  margin | single: best in expected | exact set | composed: recall | precision   (n single / n composed)"]
    for row in rep["sweep"]:
        s, c = row["single"], row["composed"]
        mark = "  <- default" if abs(row["margin"] - MARGIN) < 1e-9 else ""
        lines.append(f"  {row['margin']:>6.2f} | {s['top1']:>24.0%} | {s['exact']:>9.0%} | {c['recall']:>16.0%} | {c['precision']:>9.0%}"
                     f"   ({s['n']} / {c['n']}){mark}")
    lines += ["", "  Pick the smallest margin whose composed recall is >= 90 % while single top-1 stays >= 90 %."]
    return "\n".join(lines)
