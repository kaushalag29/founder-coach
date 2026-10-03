"""Retrieval metrics with trec_eval semantics, plus bootstrap intervals and a paired
randomization test (docs/eval-spec.md §7). Stdlib only: the definitions are short, and
the released qrels/run files are TREC-format, so ranx or trec_eval reproduce them.

qrels: {qid: {doc: grade}}   run: {qid: [doc, ...]} ranked best first.
`min_rel` is the lowest grade that counts as relevant (1 = default, 2 = strict "-l2").
"""
from __future__ import annotations

import math
import random


def _gain(grade: int, min_rel: int) -> int:
    return grade if grade >= min_rel else 0


def ndcg(qrel: dict[str, int], ranked: list[str], k: int = 10, min_rel: int = 1) -> float:
    """Linear-gain nDCG@k, as trec_eval's ndcg_cut (ranx `ndcg`)."""
    dcg = sum(_gain(qrel.get(d, 0), min_rel) / math.log2(i + 2) for i, d in enumerate(ranked[:k]))
    ideal = sorted((_gain(g, min_rel) for g in qrel.values()), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def recall(qrel: dict[str, int], ranked: list[str], k: int = 10, min_rel: int = 1) -> float:
    rel = {d for d, g in qrel.items() if g >= min_rel}
    return len(rel & set(ranked[:k])) / len(rel) if rel else 0.0


def mrr(qrel: dict[str, int], ranked: list[str], k: int = 10, min_rel: int = 1) -> float:
    for i, d in enumerate(ranked[:k]):
        if qrel.get(d, 0) >= min_rel:
            return 1.0 / (i + 1)
    return 0.0


def judged(qrel: dict[str, int], ranked: list[str], k: int = 10) -> float:
    """Share of the top k that has any judgment (grade 0 included): low values mean the
    system retrieves things the pool never saw, so its scores are underestimates."""
    top = ranked[:k]
    return sum(d in qrel for d in top) / len(top) if top else 0.0


def kind_mix(rankings: list[list[str]], k: int = 10) -> dict[str, float]:
    """{source kind: share of all top-k Moments}: what the answers are made of (a diagnostic;
    every source competes on relevance alone, so no share is a target)."""
    from .moments import moment_kind
    counts: dict[str, int] = {}
    for ranked in rankings:
        for m in ranked[:k]:
            kind = moment_kind(m)
            counts[kind] = counts.get(kind, 0) + 1
    total = sum(counts.values())
    return {kind: round(n / total, 4) for kind, n in sorted(counts.items())} if total else {}


METRICS = {
    "ndcg@10": lambda q, r: ndcg(q, r, 10),
    "ndcg@10-l2": lambda q, r: ndcg(q, r, 10, 2),
    "recall@10": lambda q, r: recall(q, r, 10),
    "recall@10-l2": lambda q, r: recall(q, r, 10, 2),
    "recall@50": lambda q, r: recall(q, r, 50),
    "mrr@10": lambda q, r: mrr(q, r, 10),
    "judged@10": lambda q, r: judged(q, r, 10),
    # nDCG@10 over the judged part of the ranking only (Sakai's condensed list): a check on
    # pool bias. When judged@10 is low, the true nDCG@10 lies between ndcg@10 and this.
    "ndcg@10-cond": lambda q, r: ndcg(q, [d for d in r if d in q], 10),
}


def per_query(qrels: dict[str, dict[str, int]], run: dict[str, list[str]],
              metrics: dict = METRICS) -> dict[str, dict[str, float]]:
    """{metric: {qid: value}} over the questions that have at least one relevant label."""
    scorable = [q for q, rel in qrels.items() if any(g >= 1 for g in rel.values())]
    return {name: {q: fn(qrels[q], run.get(q, [])) for q in scorable} for name, fn in metrics.items()}


def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def bootstrap_ci(values: list[float], n: int = 10_000, alpha: float = 0.05,
                 seed: int = 42) -> tuple[float, float]:
    """Percentile bootstrap over questions."""
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    k = len(values)
    means = sorted(sum(values[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return means[int(n * alpha / 2)], means[min(n - 1, int(n * (1 - alpha / 2)))]


def paired_randomization(a: dict[str, float], b: dict[str, float], n: int = 10_000,
                         seed: int = 42) -> float:
    """Two-sided p-value for mean(a) != mean(b) over the questions both scored
    (Fisher's randomization test: randomly swap each pair)."""
    qs = sorted(set(a) & set(b))
    if not qs:
        return 1.0
    diffs = [a[q] - b[q] for q in qs]
    observed = abs(sum(diffs))
    rng = random.Random(seed)
    hits = sum(abs(sum(d if rng.random() < 0.5 else -d for d in diffs)) >= observed - 1e-12
               for _ in range(n))
    return (hits + 1) / (n + 1)


def paired_bootstrap_ci(a: dict[str, float], b: dict[str, float], n: int = 10_000,
                        alpha: float = 0.05, seed: int = 42) -> tuple[float, float]:
    """95% percentile interval of mean(a - b) over the questions both scored: the effect
    size with its uncertainty, which says more than a p-value alone."""
    qs = sorted(set(a) & set(b))
    return bootstrap_ci([a[q] - b[q] for q in qs], n=n, alpha=alpha, seed=seed)


def ceiling(qrels: dict[str, dict[str, int]], k: int, min_rel: int = 1) -> float:
    """Best achievable mean recall@k: a question with more than k relevant Moments can't
    reach 1.0, so read recall@k against this, not against 1."""
    vals = []
    for rel in qrels.values():
        r = sum(g >= min_rel for g in rel.values())
        if any(g >= 1 for g in rel.values()):
            vals.append(min(k, r) / r if r else 0.0)
    return mean(vals)


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values."""
    order = sorted(pvalues, key=pvalues.get)
    out, running = {}, 0.0
    for i, key in enumerate(order):
        running = max(running, min(1.0, (len(order) - i) * pvalues[key]))
        out[key] = running
    return out
