"""Relevance grading with the UMBRELA 0-3 scale (docs/eval-spec.md §6.3).

Two judges from model families other than the question generator grade every pooled
Moment; when they differ by 2 or more a third judge breaks the tie and the median is
kept, otherwise the lower grade is. Each call grades a small batch of Moments, each on
its own, in a shuffled order. Judges see the Moment's transcript text, not our
extracted items, so labels don't depend on the extraction being evaluated.
"""
from __future__ import annotations

import hashlib
import random

from pydantic import BaseModel, Field

PROMPT = """Given a query and several passages, score EACH passage on its own on an integer scale of 0 to 3:
0 = the passage has nothing to do with the query.
1 = the passage seems related to the query but does not answer it.
2 = the passage has some answer for the query, but the answer may be a bit unclear, or hidden
    amongst extraneous information.
3 = the passage is dedicated to the query and contains the exact answer.

Consider the underlying intent of the query, measure how well the passage matches that intent and
how trustworthy it is, and decide each final score independently of the other passages. Passages
are spoken transcripts of startup talks and may contain transcription errors.

QUERY: {query}

{passages}

Return one score for every passage id."""

PROMPT_VERSION = "umbrela-batch-v1-" + hashlib.sha256(PROMPT.encode()).hexdigest()[:8]
MAX_PASSAGE_WORDS = 450


class Score(BaseModel):
    id: str
    score: int = Field(ge=0, le=3)


class Scores(BaseModel):
    scores: list[Score]


def render(query: str, passages: list[tuple[str, str]]) -> str:
    body = "\n\n".join(f"[{pid}]\n{' '.join(text.split()[:MAX_PASSAGE_WORDS])}" for pid, text in passages)
    return PROMPT.format(query=query, passages=body)


def batches(qid: str, moments: list[str], size: int, judge: str) -> list[list[str]]:
    """Deterministic shuffled batches, a different order per judge (limits position bias)."""
    order = sorted(moments)
    random.Random(f"{qid}|{judge}").shuffle(order)
    return [order[i:i + size] for i in range(0, len(order), size)]


def parse(result: Scores, ids: dict[str, str]) -> dict[str, int]:
    """Map passage ids back to Moments; every passage must be scored exactly once."""
    got = {s.id.strip("[] "): s.score for s in result.scores}
    missing = [pid for pid in ids if pid not in got]
    if missing:
        raise ValueError(f"judge skipped passages {missing}")
    return {ids[pid]: got[pid] for pid in ids}


def final_grade(grades: dict[str, int], judges: list[str]) -> int | None:
    """None while undecided (a judge missing, or a tie-break still needed)."""
    a, b = grades.get(judges[0]), grades.get(judges[1])
    if a is None or b is None:
        return None
    if abs(a - b) < 2:
        return min(a, b)
    if len(judges) < 3:
        return min(a, b)            # no tie-breaker configured: the conservative (lower) grade
    if grades.get(judges[2]) is None:
        return None
    return sorted((a, b, grades[judges[2]]))[1]


def needs_tiebreak(grades: dict[str, int], judges: list[str]) -> bool:
    a, b = grades.get(judges[0]), grades.get(judges[1])
    return (a is not None and b is not None and abs(a - b) >= 2 and len(judges) > 2
            and grades.get(judges[2]) is None)
