"""Hybrid search over Knowledge items, shared by the ytbrain pipeline (LanceDB index)
and the coach runtime (Knowledge pack), so eval numbers describe what founders run.

vector top-N + full-text top-N -> reciprocal-rank fusion -> optional cross-encoder
rerank -> soft boosts (Stage [ADR-0005], kind, recency) -> diversity cap per Document.
Hard filters only when the caller asks.

A store is anything with `ready`, `vector_search(vector, filter, limit)` and
`text_search(query, filter, limit)` returning row dicts that carry `item_id` plus the
PUBLIC_FIELDS; `filter` is a `Filter` or None.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Callable, Protocol

SEARCH_CANDIDATES = 50               # per retriever, before fusion
RRF_K = 60
MAX_PER_DOCUMENT = 3                 # diversity: at most this many results from one Document
BOOST_STAGE_ITEM = 0.10              # item's own Stage matches the Founder's
BOOST_STAGE_DOCUMENT = 0.04          # only the Document's Stages match
KIND_PRIOR = {"advice": 0.05, "takeaway": 0.03, "summary": 0.02, "passage": 0.0}
RECENCY_WEIGHT = 0.03                # newest talks get up to this much; halves every ~5 years

PUBLIC_FIELDS = ("item_id", "kind", "text", "evidence", "deep_link", "start_ms", "doc_id",
                 "title", "speaker", "series", "published_at", "stages", "stage_origin",
                 "topics", "provenance", "source_kind")


@dataclass(frozen=True)
class Filter:
    """Hard filters. Empty fields mean "no restriction"."""
    kinds: tuple[str, ...] = ()
    stage: str | None = None
    topics: tuple[str, ...] = ()
    doc_id: str | None = None

    @classmethod
    def of(cls, kinds=None, stage=None, topics=None, doc_id=None) -> "Filter | None":
        f = cls(tuple(kinds or ()), stage or None, tuple(topics or ()), doc_id or None)
        return None if f.empty else f

    @property
    def empty(self) -> bool:
        return not (self.kinds or self.stage or self.topics or self.doc_id)

    def matches(self, row: dict) -> bool:
        if self.kinds and row.get("kind") not in self.kinds:
            return False
        if self.stage and self.stage not in (row.get("stages") or []):
            return False
        if self.topics and not set(self.topics) & set(row.get("topics") or []):
            return False
        if self.doc_id and row.get("doc_id") != self.doc_id:
            return False
        return True


class Store(Protocol):
    @property
    def ready(self) -> bool: ...
    def vector_search(self, vector, flt: Filter | None, limit: int) -> list[dict]: ...
    def text_search(self, query: str, flt: Filter | None, limit: int) -> list[dict]: ...


Embed = Callable[[list[str]], list]                   # query texts -> vectors
Reranker = Callable[[str, list[str]], list[float]]    # (query, texts) -> scores in 0..1


def rrf(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, iid in enumerate(ranking):
            scores[iid] = scores.get(iid, 0.0) + 1.0 / (k + rank + 1)
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


def diversify(rows: list[dict], per_doc: int = MAX_PER_DOCUMENT) -> list[dict]:
    """At most `per_doc` results per Document, and one per quote: an advice item and
    a takeaway often cite the same sentence, which read as a duplicate result."""
    seen: dict[str, int] = {}
    quotes: set[tuple[str, str]] = set()
    out = []
    for r in rows:
        q = " ".join((r.get("evidence") or "").lower().split())
        if q and (r["doc_id"], q) in quotes:
            continue
        n = seen.get(r["doc_id"], 0)
        if n < per_doc:
            out.append(r)
            seen[r["doc_id"]] = n + 1
            if q:
                quotes.add((r["doc_id"], q))
    return out


def search(store: Store, embed: Embed, query: str, *, stage: str | None = None,
           kinds: list[str] | None = None, topics: list[str] | None = None,
           require_stage: bool = False, top_k: int = 8, reranker: Reranker | None = None,
           candidates: int = SEARCH_CANDIDATES, info: dict | None = None) -> list[dict]:
    """Ranked Knowledge items for `query`, each with `score` and Citation fields. If `info`
    is given, it receives `top_similarity`: the cosine similarity of the closest item (from
    stores that report it), an absolute signal for Gaps that reranking can't distort."""
    if not store.ready:
        raise RuntimeError("knowledge index not built -- run `ytbrain index`")
    flt = Filter.of(kinds=kinds, topics=topics, stage=stage if require_stage else None)
    dense = store.vector_search(embed([query])[0], flt, candidates)
    sparse = store.text_search(query, flt, candidates)
    if info is not None:
        info["top_similarity"] = dense[0].get("_similarity") if dense else None
    rows = {r["item_id"]: r for r in dense + sparse}
    fused = rrf([[r["item_id"] for r in dense], [r["item_id"] for r in sparse]])
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
                    "score": round(rel[i] + boosts(r, stage, this_year), 4)})
    return diversify(out)[:top_k]
