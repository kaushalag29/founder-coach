"""Moments: the fixed windows that Eval labels point at (docs/eval-spec.md §1).

Pure functions, no I/O. A talk's Moment starts on every whole minute and lasts two minutes, so
consecutive Moments overlap by one (the TREC Podcasts convention); id `<youtube id>_<start s>`.
An article's Locator is a paragraph number (ADR-0013): its Moment is ARTICLE_MOMENT_PARAS
paragraphs starting every ARTICLE_MOMENT_STEP, id `<doc id>_p<start paragraph>`. A Book
Chapter's Locator is page * 1000 + paragraph (ADR-0014): its Moment is one PDF page, id
`<doc id>_b<page>` (a page is ~300 words, about a two-minute read). Labels name
Moments, never Knowledge items, so they survive re-extraction and any chunking can map onto them.
Each rule lives in its kind's class (ytbrain/source_kinds.py); these functions dispatch to it.
"""
from __future__ import annotations

from .. import source_kinds as K
from ..config import MOMENT_S, MOMENT_STEP_S


def moment_id(youtube_id: str, start_s: int) -> str:
    return K.TALK.moment_id(youtube_id, start_s)


def is_article(doc_id: str) -> bool:
    return K.ARTICLE.owns_doc(doc_id)


def is_book(doc_id: str) -> bool:
    """A Book Chapter: `<book id>__<chapter>`, never 11 characters (a YouTube id's length)."""
    return K.BOOK_CHAPTER.owns_doc(doc_id)


def moment_locator(mid: str) -> str:
    """What a Moment's start counts: 'time' (seconds), 'paragraph' or 'page'."""
    return K.for_moment(mid).locator


def moment_kind(mid: str) -> str:
    """The source kind of a Moment: 'talk', 'article', 'chapter', ..."""
    return K.for_moment(mid).name


def is_article_moment(mid: str) -> bool:
    return K.for_moment(mid) is K.ARTICLE


def parse_moment_id(mid: str) -> tuple[str, int]:
    """(doc id, start): seconds for a talk, a paragraph number for an article, a page for a Book
    Chapter. The start is after the last '_' (YouTube ids may contain '_' themselves)."""
    doc, tail = mid.rsplit("_", 1)
    tag = K.for_moment(mid).moment_tag
    return doc, int(tail[len(tag):])


def moment_for(doc_id: str, locator: int | None) -> str | None:
    """The Moment a result with this Locator maps to: the latest Moment start at or before it.
    None without a Locator (Document summaries score at Document level only)."""
    if locator is None or locator < 0:
        return None
    k = K.for_doc(doc_id)
    return k.moment_id(doc_id, k.moment_start(locator))


def moment_url(mid: str, doc_url: str | None = None) -> str:
    """A link to the Moment: the talk at its second; an article's page (the caller knows its URL)."""
    doc, start = parse_moment_id(mid)
    return K.for_moment(mid).moment_url(doc, start, doc_url)


def moment_text(utterances: list[dict], start_s: int, article: bool = False,
                locator: str | None = None) -> str:
    """Text of one Moment from its Document's units. `locator` is `moment_locator(mid)`;
    `article=True` means 'paragraph'."""
    return K.by_locator("paragraph" if article else locator).moment_text(utterances, start_s)


def results_to_moments(results: list[dict], k: int | None = None) -> list[str]:
    """Ranked Knowledge items -> ranked Moment ids: each result maps to its Moment,
    repeats are dropped (first rank wins), summaries are skipped."""
    out: list[str] = []
    seen: set[str] = set()
    for r in results:
        if r.get("kind") == "summary":
            continue
        m = moment_for(r["doc_id"], r.get("start_ms"))
        if m and m not in seen:
            seen.add(m)
            out.append(m)
            if k and len(out) >= k:
                break
    return out


def results_to_talks(results: list[dict], k: int | None = None) -> list[str]:
    """Ranked results -> ranked talk ids (summaries count here)."""
    out: list[str] = []
    for r in results:
        if r["doc_id"] not in out:
            out.append(r["doc_id"])
            if k and len(out) >= k:
                break
    return out


def spans_to_qrels(spans: list[dict]) -> dict[str, dict[str, int]]:
    """Graded spans {qid, youtube_id, start_ms, end_ms, grade} -> {qid: {moment_id: grade}}.
    A span gives its grade to every Moment it overlaps; a Moment hit by several spans
    keeps the highest grade. For our own labels each span is exactly one Moment."""
    out: dict[str, dict[str, int]] = {}
    for sp in spans:
        lo, hi = int(sp["start_ms"]) // 1000, int(sp["end_ms"]) // 1000
        first = max(0, ((lo - MOMENT_S) // MOMENT_STEP_S + 1) * MOMENT_STEP_S)
        s = first
        while s < hi:
            if s + MOMENT_S > lo:
                mid = moment_id(sp["youtube_id"], s)
                q = out.setdefault(sp["qid"], {})
                q[mid] = max(q.get(mid, 0), int(sp["grade"]))
            s += MOMENT_STEP_S
    return out
