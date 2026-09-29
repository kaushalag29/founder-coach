"""Candidate pooling (docs/eval-spec.md §6.2): the union of what five different retrieval
variants return, mapped to Moments, so labels don't just mirror one system."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..knowledge.search import hybrid, search
from .moments import results_to_moments

VARIANTS = ("bm25", "dense", "hybrid", "hybrid_rerank", "rewrite")


class Rewrite(BaseModel):
    query: str = Field(description="a concise search query, at most 20 words")


REWRITE_PROMPT = """Rewrite this founder's question as a concise search query (at most 20 words)
for a library of startup talks. Keep the underlying problem and key concepts; drop filler.

QUESTION: {question}
"""


def _hybrid(store, embed, query: str, limit: int) -> list[dict]:
    return hybrid(store, embed, query, limit)


def pool_question(store, embed, reranker, question: str, rewrite: str | None, depth: int,
                  seed_moment: str | None = None) -> dict[str, tuple[list[str], int]]:
    """{moment_id: (variants that returned it, best rank)}; the seed Moment is always in."""
    wide = depth * 3                         # items, before mapping several to one Moment
    ranked = {
        "bm25": store.text_search(question, None, wide),
        "dense": store.vector_search(embed([question])[0], None, wide),
        "hybrid": _hybrid(store, embed, question, wide),
    }
    if reranker is not None:
        ranked["hybrid_rerank"] = search(store, embed, question, top_k=wide, reranker=reranker,
                                         candidates=max(50, wide))
    if rewrite:
        ranked["rewrite"] = _hybrid(store, embed, rewrite, wide)
    out: dict[str, tuple[list[str], int]] = {}
    for name, results in ranked.items():
        for rank, m in enumerate(results_to_moments(results, depth), 1):
            variants, best = out.get(m, ([], rank))
            out[m] = (variants + [name], min(best, rank))
    if seed_moment and seed_moment not in out:
        out[seed_moment] = (["seed"], 999)
    elif seed_moment:
        out[seed_moment] = (out[seed_moment][0] + ["seed"], out[seed_moment][1])
    return out
