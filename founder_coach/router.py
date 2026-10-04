"""Which Domains does a question touch? No LLM, no extra model: the question's own vector decides.

A Domain is scored by how close its best items are to the question: the mean cosine of its `top_n`
closest items. Scoring each Domain on its own (rather than counting votes in one global top-N) means
a small Domain, a few leadership books, is not drowned by a large one, thousands of startup talks.
The Domains within `margin` of the best score are routed, at most `max_domains`: one for a plain
question, two or three when a question spans subjects. Search then favours the routed Domains and
still looks everywhere (route-and-backfill, docs/library-and-packs-plan.md §2.4), so a wrong route
costs a little ranking, never recall.

With one Domain in the Library there is nothing to choose between and nothing is routed.
The numbers are tuned with `ytbrain eval route`.
"""
from __future__ import annotations

from dataclasses import dataclass

MARGIN = 0.05            # a Domain is routed when its score is within this of the best Domain's
MAX_DOMAINS = 3
TOP_N = 3                # a Domain's score: the mean cosine of its TOP_N closest items


@dataclass(frozen=True)
class Routing:
    scores: tuple[tuple[str, float], ...] = ()       # every Domain with items, best first
    domains: tuple[str, ...] = ()                    # the routed ones, best first; empty: search everything

    @property
    def routed(self) -> bool:
        return bool(self.domains)

    def as_list(self) -> list[dict]:
        """The routed Domains with their scores, for a tool response."""
        by = dict(self.scores)
        return [{"domain": d, "score": round(by[d], 3)} for d in self.domains]


def choose(scores: dict[str, float], margin: float = MARGIN, max_domains: int = MAX_DOMAINS) -> Routing:
    ranked = tuple(sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])))
    if len(ranked) < 2:
        return Routing(ranked, ())
    best = ranked[0][1]
    return Routing(ranked, tuple(d for d, s in ranked if s >= best - margin)[:max_domains])


def route(store, vector, margin: float = MARGIN, max_domains: int = MAX_DOMAINS, top_n: int = TOP_N,
          **ignored) -> Routing:
    """Route a question vector over `store` (anything with `domain_scores(vector, top_n)`)."""
    scorer = getattr(store, "domain_scores", None)
    if scorer is None:
        return Routing()
    return choose(scorer(vector, top_n), margin, max_domains)
