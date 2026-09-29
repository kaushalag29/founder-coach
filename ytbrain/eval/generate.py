"""Tuning-set questions generated from our own corpus (docs/eval-spec.md §6.1).

One question per sampled Verified Advice or Takeaway. The generator sees the item's
text but never its quote, and a question that leans on the source's wording is rejected,
because copied wording makes full-text search look better than it is.
"""
from __future__ import annotations

import math
import random
import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from .moments import moment_for

STAGES = ("pre-idea", "idea", "mvp", "pmf", "growth", "fundraising", "scaling", "exit")
QUESTION_TYPES = ("how_to_advice", "conditional_stage", "single_fact", "multi_source_synthesis",
                  "comparison_disagreement", "time_sensitive", "false_premise", "out_of_corpus")
MAX_PER_SPEAKER = 3
MAX_LEXICAL_OVERLAP = 0.5
MAX_QUOTE_SIMILARITY = 0.85
MAX_QUESTION_SIMILARITY = 0.90

_STOP = set("""a an the and or but if then so to of in on at for from by with about as into over
under is are was were be been being am do does did doing have has had having i me my we our you
your he she it they them their this that these those what which who whom how why when where can
could should would will shall may might must not no yes than too very just really also any some
more most such own same other there here up down out off again once all each few both only get got
make made want need like know think""".split())


class GeneratedQuestion(BaseModel):
    question: str = Field(description="the founder's question, 8-35 words, first person")
    question_type: Literal["how_to_advice", "conditional_stage", "single_fact"]
    stage: list[Literal["pre-idea", "idea", "mvp", "pmf", "growth", "fundraising", "scaling",
                        "exit"]] = Field(default_factory=list)
    temporal: Literal["static", "slow_changing", "time_sensitive"] = "static"


PROMPT = """You write evaluation questions for a search engine that coaches early-stage startup founders.

Below is one piece of knowledge taken from a startup talk. Write ONE question that a real founder
might ask a coach, whose best answer is this knowledge.

Rules:
- Describe the founder's own situation or problem in their words, first person, 8-35 words.
- Do not restate the knowledge and do not reuse its distinctive words or phrases; a founder asking
  this would not yet know the answer.
- No names of the speaker, the talk, or companies mentioned in it.
- question_type: how_to_advice ("how do I..."), conditional_stage (advice for a specific situation
  or stage), or single_fact (what is true or typical).
- stage: the company stages the question is about (empty if any stage).
- temporal: time_sensitive if the answer depends on current programs, prices or rules;
  slow_changing if it depends on the current state of technology or markets; else static.

KNOWLEDGE ({kind}, topic: {topic}):
{text}
"""


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(w) > 2 and w not in _STOP}


def lexical_overlap(question: str, *sources: str) -> float:
    """Share of the question's content words that also appear in the source texts."""
    q = content_words(question)
    if not q:
        return 1.0
    src = set().union(*(content_words(s) for s in sources))
    return len(q & src) / len(q)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def sample_items(rows: list[dict], n: int, caption_kind: dict[str, str] | None = None,
                 seed: int = 42) -> list[dict]:
    """Stratified sample of Advice/Takeaway rows: Topics in proportion to the corpus (at least
    3 each while available), at most one item per Talk and MAX_PER_SPEAKER per speaker,
    round-robin over Topics so every prefix of the sample stays balanced. Deterministic."""
    rng = random.Random(seed)
    by_topic: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["item_id"]):
        if r.get("kind") in ("advice", "takeaway") and r.get("evidence") and r.get("start_ms", -1) >= 0:
            by_topic[(r.get("topics") or ["other"])[0]].append(r)
    for items in by_topic.values():
        rng.shuffle(items)
        # prefer Advice, and mix caption kinds / years so the order isn't all one Series
        items.sort(key=lambda r: (r["kind"] != "advice",
                                  (caption_kind or {}).get(r["doc_id"]) == "auto" and rng.random() < 0.5))
    total = sum(len(v) for v in by_topic.values()) or 1
    weight = {t: max(len(v) / total, 3 / max(1, n)) for t, v in by_topic.items()}
    talks: set[str] = set()
    speakers: dict[str, int] = defaultdict(int)
    cursor = {t: 0 for t in by_topic}
    got: dict[str, int] = defaultdict(int)
    open_topics = set(by_topic)
    picked: list[dict] = []
    while len(picked) < n and open_topics:
        t = min(open_topics, key=lambda t: (got[t] / weight[t], t))
        items = by_topic[t]
        while cursor[t] < len(items):
            r = items[cursor[t]]
            cursor[t] += 1
            spk = (r.get("speaker") or "").strip().lower()
            if r["doc_id"] in talks or (spk and speakers[spk] >= MAX_PER_SPEAKER):
                continue
            picked.append(r)
            talks.add(r["doc_id"])
            speakers[spk] += bool(spk)
            got[t] += 1
            break
        else:
            open_topics.discard(t)
    return picked


def seed_record(row: dict, qid: str) -> dict:
    return {"seed_item": row["item_id"], "seed_kind": row["kind"], "seed_text": row["text"],
            "seed_quote": row.get("evidence") or "", "doc_id": row["doc_id"],
            "start_ms": int(row["start_ms"]), "title": row.get("title") or "",
            "speaker": row.get("speaker") or "", "topic": list(row.get("topics") or []),
            "seed_moment": moment_for(row["doc_id"], row["start_ms"]),
            "seed_url": f"https://www.youtube.com/watch?v={row['doc_id']}&t={int(row['start_ms']) // 1000}s",
            "qid": qid}


def prompt_for(seed: dict) -> str:
    return PROMPT.format(kind=seed["seed_kind"], topic=", ".join(seed["topic"]) or "general",
                         text=seed["seed_text"])


def check_question(q: str, seed: dict, embed=None, accepted_vecs: list | None = None
                   ) -> tuple[bool, str, dict]:
    """(ok, reason, measurements) for one generated question against the leakage rules."""
    words = len(q.split())
    meas = {"lexical_overlap": round(lexical_overlap(q, seed["seed_text"], seed["seed_quote"]), 3)}
    if words < 6 or words > 45:
        return False, f"length {words} words", meas
    if meas["lexical_overlap"] > MAX_LEXICAL_OVERLAP:
        return False, "reuses the source's wording", meas
    if embed is not None:
        qv, sv = embed([q, seed["seed_quote"] or seed["seed_text"]])
        meas["embed_sim"] = round(cosine(qv, sv), 3)
        meas["_vec"] = qv
        if meas["embed_sim"] > MAX_QUOTE_SIMILARITY:
            return False, "too similar to the source quote", meas
        for v in accepted_vecs or []:
            if cosine(qv, v) > MAX_QUESTION_SIMILARITY:
                return False, "near-duplicate of another question", meas
    return True, "", meas
