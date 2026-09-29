"""Moments: the fixed windows that Eval labels point at (docs/eval-spec.md §1).

Pure functions, no I/O. A talk's Moment starts on every whole minute and lasts two minutes, so
consecutive Moments overlap by one (the TREC Podcasts convention); id `<youtube id>_<start s>`.
An article's Locator is a paragraph number (ADR-0013): its Moment is ARTICLE_MOMENT_PARAS
paragraphs starting every ARTICLE_MOMENT_STEP, id `<doc id>_p<start paragraph>`. Labels name
Moments, never Knowledge items, so they survive re-extraction and any chunking can map onto them.
"""
from __future__ import annotations

from ..config import ARTICLE_MOMENT_PARAS, ARTICLE_MOMENT_STEP, MOMENT_S, MOMENT_STEP_S

def moment_id(youtube_id: str, start_s: int) -> str:
    return f"{youtube_id}_{int(start_s):05d}"


def is_article(doc_id: str) -> bool:
    return str(doc_id).startswith("w-")          # website Documents (ytbrain.web.urls.doc_id)


def is_article_moment(mid: str) -> bool:
    return mid.rsplit("_", 1)[-1].startswith("p")


def parse_moment_id(mid: str) -> tuple[str, int]:
    """(doc id, start): seconds for a talk, a paragraph number for an article. The start is
    after the last '_' (YouTube ids may contain '_' themselves)."""
    doc, tail = mid.rsplit("_", 1)
    return doc, int(tail[1:] if tail.startswith("p") else tail)


def moment_for(doc_id: str, locator: int | None) -> str | None:
    """The Moment a result with this Locator maps to: the latest Moment start at or before it.
    None without a Locator (Document summaries score at Document level only)."""
    if locator is None or locator < 0:
        return None
    if is_article(doc_id):
        n = max(1, int(locator))
        return f"{doc_id}_p{(n - 1) // ARTICLE_MOMENT_STEP * ARTICLE_MOMENT_STEP + 1:05d}"
    return moment_id(doc_id, (int(locator) // 1000 // MOMENT_STEP_S) * MOMENT_STEP_S)


def moment_url(mid: str, doc_url: str | None = None) -> str:
    """A link to the Moment: the talk at its second; an article's page (the caller knows its URL)."""
    doc, s = parse_moment_id(mid)
    if is_article_moment(mid):
        return doc_url or ""
    return f"https://www.youtube.com/watch?v={doc}&t={s}s"


def moment_text(utterances: list[dict], start_s: int, article: bool = False) -> str:
    """Text of one Moment: every unit overlapping its window (seconds for a talk, paragraph
    numbers for an article, whose units' start_ms/end_ms ARE paragraph numbers)."""
    if article:
        lo, hi = start_s, start_s + ARTICLE_MOMENT_PARAS
        return " ".join(u.get("text", "") for u in utterances if lo <= int(u.get("start_ms", 0)) < hi).strip()
    lo, hi = start_s * 1000, (start_s + MOMENT_S) * 1000
    return " ".join(u.get("text", "") for u in utterances
                    if int(u.get("start_ms", 0)) < hi and int(u.get("end_ms", u.get("start_ms", 0))) > lo
                    ).strip()


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
