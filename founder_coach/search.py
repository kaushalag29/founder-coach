"""Hybrid search over Knowledge items, shared by the ytbrain pipeline (LanceDB index)
and the coach runtime (Knowledge pack), so eval numbers describe what founders run.

vector top-N + full-text top-N -> reciprocal-rank fusion -> optional cross-encoder
rerank -> soft boosts (Stage [ADR-0005], kind, recency) -> a DiversityPolicy. Hard filters
only when the caller asks. Nothing here looks at a result's source kind: talks, articles and
book chapters compete on relevance alone (ADR-0014, amendment 2026-09-30).

A store is anything with `ready`, `vector_search(vector, filter, limit)` and
`text_search(query, filter, limit)` returning row dicts that carry `item_id` plus the
PUBLIC_FIELDS; `filter` is a `Filter` or None.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Callable, Protocol

SEARCH_CANDIDATES = 50               # per retriever, before fusion
RRF_K = 60
MAX_PER_DOCUMENT = 3                 # diversity: at most this many results from one Document
BOOST_STAGE_ITEM = 0.10              # item's own Stage matches the Founder's
BOOST_STAGE_DOCUMENT = 0.04          # only the Document's Stages match
KIND_PRIOR = {"advice": 0.05, "rule": 0.05, "fact": 0.04, "takeaway": 0.03, "summary": 0.02, "passage": 0.0}
RECENCY_WEIGHT = 0.03                # newest talks get up to this much; halves every ~5 years
ROUTED_WEIGHT = 0.5                  # a routed Domain's own result lists count this much beside the whole Library's
MIN_PER_DOMAIN = 2                   # when a question routes to several Domains, each gets at least this many results ...
DOMAIN_FLOOR = 0.5                   # ... if they are at least this share as relevant as the best result

DEFAULT_DOMAIN = "startup"           # the Domain of every item indexed before Domains existed

PUBLIC_FIELDS = ("item_id", "kind", "text", "evidence", "deep_link", "start_ms", "doc_id",
                 "title", "speaker", "series", "published_at", "stages", "stage_origin",
                 "topics", "provenance", "source_kind", "domains", "source_id")


@dataclass(frozen=True)
class Filter:
    """Hard filters. Empty fields mean "no restriction". `domains` match when the item is in ANY
    of them: a question that spans two Domains searches both in one call."""
    kinds: tuple[str, ...] = ()
    stage: str | None = None
    topics: tuple[str, ...] = ()
    doc_id: str | None = None
    domains: tuple[str, ...] = ()

    @classmethod
    def of(cls, kinds=None, stage=None, topics=None, doc_id=None, domains=None) -> "Filter | None":
        f = cls(tuple(kinds or ()), stage or None, tuple(topics or ()), doc_id or None,
                tuple(dict.fromkeys(domains or ())))
        return None if f.empty else f

    @property
    def empty(self) -> bool:
        return not (self.kinds or self.stage or self.topics or self.doc_id or self.domains)

    def matches(self, row: dict) -> bool:
        if self.kinds and row.get("kind") not in self.kinds:
            return False
        if self.stage and self.stage not in (row.get("stages") or []):
            return False
        if self.topics and not set(self.topics) & set(row.get("topics") or []):
            return False
        if self.doc_id and row.get("doc_id") != self.doc_id:
            return False
        if self.domains and not set(self.domains) & set(row.get("domains") or [DEFAULT_DOMAIN]):
            return False
        return True


class Store(Protocol):
    @property
    def ready(self) -> bool: ...
    def vector_search(self, vector, flt: Filter | None, limit: int) -> list[dict]: ...
    def text_search(self, query: str, flt: Filter | None, limit: int) -> list[dict]: ...


Embed = Callable[[list[str]], list]                   # query texts -> vectors
Reranker = Callable[[str, list[str]], list[float]]    # (query, texts) -> scores in 0..1


def rrf(rankings: list[list[str]], k: int = RRF_K, weights: list[float] | None = None) -> dict[str, float]:
    scores: dict[str, float] = {}
    for n, ranking in enumerate(rankings):
        w = 1.0 if weights is None else weights[n]
        for rank, iid in enumerate(ranking):
            scores[iid] = scores.get(iid, 0.0) + w / (k + rank + 1)
    return scores


def hybrid(store: Store, embed: Embed, query: str, limit: int,
           flt: Filter | None = None) -> list[dict]:
    """Dense + full-text results fused by RRF (no rerank, no boosts)."""
    dense = store.vector_search(embed([query])[0], flt, limit)
    sparse = store.text_search(query, flt, limit)
    rows = {r["item_id"]: r for r in dense + sparse}
    fused = rrf([[r["item_id"] for r in dense], [r["item_id"] for r in sparse]])
    return [rows[i] for i in sorted(fused, key=fused.get, reverse=True)]


def boosts(row: dict, stage: str | None, this_year: int) -> float:
    b = KIND_PRIOR.get(row.get("kind", ""), 0.0)
    if stage and stage in (row.get("stages") or []):
        b += BOOST_STAGE_ITEM if row.get("stage_origin") == "item" else BOOST_STAGE_DOCUMENT
    year = row.get("year") or 0
    if year:
        b += RECENCY_WEIGHT * math.pow(0.5, max(0, this_year - year) / 5)
    return b


# -- diversity -------------------------------------------------------------------------------
# A result list is kept readable by small rules, each deciding whether the next result (in rank
# order) joins the ones already kept. Rules are source-agnostic; a new one is a class with
# `admits`, and a policy is chosen by the eval (`ytbrain eval run --config full-<policy>`).

class DiversityRule(Protocol):
    def admits(self, row: dict, kept: list[dict]) -> bool: ...


def _quote(row: dict) -> str:
    return " ".join((row.get("evidence") or "").lower().split())


@dataclass(frozen=True)
class SameQuote:
    """One result per quote per Document: an advice item and a takeaway often cite the same sentence."""

    def admits(self, row, kept):
        q = _quote(row)
        return not q or not any(k["doc_id"] == row["doc_id"] and _quote(k) == q for k in kept)


@dataclass(frozen=True)
class PerDocumentCap:
    """At most `n` results from one Document."""
    n: int = MAX_PER_DOCUMENT

    def admits(self, row, kept):
        return sum(k["doc_id"] == row["doc_id"] for k in kept) < self.n


@dataclass(frozen=True)
class PerSeriesCap:
    """At most `n` of the first `window` results from one Series (a playlist, a blog, a book).
    Results without a Series are never capped; past the window nothing is."""
    n: int = 3
    window: int = 10

    def admits(self, row, kept):
        series = row.get("series")
        if not series or len(kept) >= self.window:
            return True
        return sum(k.get("series") == series for k in kept) < self.n


_WORD = re.compile(r"[a-z0-9']+")


def _words(row: dict) -> frozenset[str]:
    return frozenset(_WORD.findall((row.get("text") or "").lower()))


@dataclass(frozen=True)
class NearDuplicateCollapse:
    """Drop a result whose text says what a kept one already says (word-set Jaccard >= threshold),
    whichever Documents or sources they come from: the higher-ranked one stays."""
    threshold: float = 0.8

    def admits(self, row, kept):
        a = _words(row)
        if not a:
            return True
        for k in kept:
            b = _words(k)
            if b and len(a & b) / len(a | b) >= self.threshold:
                return False
        return True


@dataclass(frozen=True)
class DiversityPolicy:
    """Rules applied in order to a ranked list; a result is kept when every rule admits it."""
    rules: tuple = (SameQuote(), PerDocumentCap())

    def apply(self, rows: list[dict]) -> list[dict]:
        kept: list[dict] = []
        for r in rows:
            if all(rule.admits(r, kept) for rule in self.rules):
                kept.append(r)
        return kept

    def with_rule(self, rule) -> DiversityPolicy:
        return DiversityPolicy(self.rules + (rule,))


DEFAULT_POLICY = DiversityPolicy()
# Candidate policies, measured by the eval before any becomes the default (docs/eval-spec.md §7)
POLICIES = {
    "default": DEFAULT_POLICY,
    "series3": DEFAULT_POLICY.with_rule(PerSeriesCap(3, 10)),
    "neardup": DEFAULT_POLICY.with_rule(NearDuplicateCollapse(0.8)),
}


def diversify(rows: list[dict], per_doc: int = MAX_PER_DOCUMENT) -> list[dict]:
    """The default policy (one result per quote, at most `per_doc` per Document)."""
    return DiversityPolicy((SameQuote(), PerDocumentCap(per_doc))).apply(rows)


def _per_domain_floor(ranked: list[dict], routed: tuple[str, ...], top_k: int) -> list[dict]:
    """The first `top_k` of `ranked`, but with at least MIN_PER_DOMAIN results from each routed Domain
    that has results at least DOMAIN_FLOOR as relevant as the best one: a Domain with many items
    can't crowd out the other Domain the question also needs. Replaces the lowest-ranked result of a
    Domain that has more than its share; the list stays in rank order."""
    top = ranked[:top_k]
    if len(routed) < 2 or not ranked:
        return top
    bar = DOMAIN_FLOOR * (ranked[0].get("relevance") or 0.0)
    has = lambda r, d: d in (r.get("domains") or [DEFAULT_DOMAIN])
    for d in routed:
        need = MIN_PER_DOMAIN - sum(has(r, d) for r in top)
        for cand in (r for r in ranked[top_k:] if has(r, d) and (r.get("relevance") or 0.0) >= bar):
            if need <= 0:
                break
            drop = next((i for i in range(len(top) - 1, -1, -1)
                         if all(sum(has(x, e) for x in top) > MIN_PER_DOMAIN or not has(top[i], e) for e in routed)
                         and not has(top[i], d)), None)
            if drop is None:
                break
            top[drop:drop + 1] = []
            top.append(cand)
            need -= 1
    top.sort(key=lambda r: r["score"], reverse=True)
    return top


def search(store: Store, embed: Embed, query: str, *, stage: str | None = None,
           kinds: list[str] | None = None, topics: list[str] | None = None,
           domains: list[str] | None = None, route: bool | dict = False,
           require_stage: bool = False, top_k: int = 8, reranker: Reranker | None = None,
           candidates: int = SEARCH_CANDIDATES, info: dict | None = None,
           diversity: DiversityPolicy = DEFAULT_POLICY,
           routed_weight: float = ROUTED_WEIGHT) -> list[dict]:
    """Ranked Knowledge items for `query`, each with `score` and Citation fields. `domains` restricts
    the search to those Domains (any of them); when it names several, each gets its own ranking and a
    minimum share of the results, so a big Domain can't crowd a small one out. With `route` (True, or a dict of router settings such as
    {"margin": 0.05, "max_domains": 3}) and no `domains`, the router (founder_coach.router) picks the
    Domains the question touches and search favours them while still looking in the whole Library. If `info` is given, it receives `top_similarity` (the cosine
    similarity of the closest item, from stores that report it: an absolute signal for Gaps that
    reranking can't distort) and `routing` (the Routing, when one was made)."""
    if not store.ready:
        raise RuntimeError("knowledge index not built -- run `ytbrain index`")
    flt = Filter.of(kinds=kinds, topics=topics, domains=domains, stage=stage if require_stage else None)
    qv = embed([query])[0]
    routing = None
    if (route is True or isinstance(route, dict)) and not domains:
        from .router import route as route_query
        routing = route_query(store, qv, **(route if isinstance(route, dict) else {}))
        if info is not None:
            info["routing"] = routing
    dense = store.vector_search(qv, flt, candidates)
    sparse = store.text_search(query, flt, candidates)
    if info is not None:
        info["top_similarity"] = dense[0].get("_similarity") if dense else None
    rows = {r["item_id"]: r for r in dense + sparse}
    sims = {r["item_id"]: r["_similarity"] for r in dense if r.get("_similarity") is not None}
    rankings = [[r["item_id"] for r in dense], [r["item_id"] for r in sparse]]
    weights = [1.0, 1.0]
    focus = tuple(dict.fromkeys(domains)) if domains and len(set(domains)) > 1 else tuple(routing.domains if routing else ())
    for d in focus:         # backfill: the whole filtered set above, each named or routed Domain below
        dflt = Filter.of(kinds=kinds, topics=topics, domains=[d], stage=stage if require_stage else None)
        d_dense, d_sparse = store.vector_search(qv, dflt, candidates), store.text_search(query, dflt, candidates)
        rows.update({r["item_id"]: r for r in d_dense + d_sparse})
        for r in d_dense:
            if r.get("_similarity") is not None:
                sims.setdefault(r["item_id"], r["_similarity"])
        rankings += [[r["item_id"] for r in d_dense], [r["item_id"] for r in d_sparse]]
        weights += [routed_weight, routed_weight]
    fused = rrf(rankings, weights=weights)
    ranked = sorted(fused, key=fused.get, reverse=True)[:candidates]
    if reranker is not None and ranked:
        rel = dict(zip(ranked, reranker(query, [rows[i]["indexable"] for i in ranked])))
    else:
        top = max(fused.values(), default=1.0)
        rel = {i: fused[i] / top for i in ranked}
    this_year = dt.date.today().year
    scored = sorted(ranked, key=lambda i: rel[i] + boosts(rows[i], stage, this_year),
                    reverse=True)
    out = []
    for i in scored:
        r = rows[i]
        out.append({**{f: r.get(f) for f in PUBLIC_FIELDS},
                    "relevance": round(rel[i], 4),
                    "similarity": None if i not in sims else round(sims[i], 4),
                    "score": round(rel[i] + boosts(r, stage, this_year), 4)})
    kept = diversity.apply(out)
    return _per_domain_floor(kept, focus, top_k)
