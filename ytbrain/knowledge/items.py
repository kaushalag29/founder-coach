"""Knowledge items: the retrievable units built from one Document's record.

Pure functions, no model and no storage, so the item shape is unit-tested
offline. One Document yields:
  advice    each Verified Advice, with its Evidence and Locator
  takeaway  each Verified Takeaway, with its Evidence and Locator
  summary   the Document summary (Locator = start)
  passage   ~550-token transcript Passages (Locator = first utterance)
Unverified Advice/Takeaways are never items [ADR-0004].
"""
from __future__ import annotations

from .. import locators
from founder_coach.search import DEFAULT_DOMAIN
from ..config import EVIDENCE_MIN_JACCARD
from ..index import chunk_transcript

SOURCE_KIND = "talk"                 # a record's default Source kind [ADR-0003, ADR-0013]


def deep_link(url: str | None, doc_id: str, ms: int | None, locator: str = "time",
              quote_text: str | None = None) -> str:
    return locators.deep_link(url, doc_id, ms, locator, quote_text)


def _year(published_at: str | None) -> int:
    try:
        return int((published_at or "")[:4])
    except ValueError:
        return 0


def context_header(record: dict, chapter: str | None = None) -> str:
    """Deterministic header prepended before embedding and full-text indexing. `chapter` is the
    section at the item's position (a talk's chapter, an article's or a Book Chapter's heading)."""
    who = f" by {record['speaker']}" if record.get("speaker") else ""
    if record.get("source_kind") == "chapter":       # the Book, its authors, then the Chapter
        year = _year(record.get("published_at"))
        head = f"From the book \"{record.get('series') or ''}\"{who}" + (f" ({year})" if year else "")
        head += f", chapter \"{record.get('title_raw') or record.get('title_canonical') or ''}\""
        return head + (f" — {chapter}" if chapter else "")
    where = ", ".join(x for x in (record.get("series"), str(_year(record.get("published_at")) or ""))
                      if x)
    head = f"From \"{record.get('title_canonical') or record.get('title_raw') or ''}\"{who}"
    head += f" ({where})" if where else ""
    return head + (f" — {chapter}" if chapter else "")


def _chapter_at(record: dict, ms: int | None) -> str | None:
    best = None
    for ch in record.get("chapters") or []:
        if ms is not None and (ch.get("start_ms") or 0) <= ms:
            best = ch.get("title")
    return best


def _verified(item: dict, threshold: float) -> bool:
    score = item.get("match_score")
    return bool(item.get("evidence_span")) and score is not None and score >= threshold


def build_items(record: dict, transcript: dict | None, domains: list[str] | None = None,
                source_id: str = "") -> list[dict]:
    """Knowledge items for one verified record (no vectors yet). `domains` and `source_id` come
    from configuration (ytbrain/domains.py, the manifest), not from the record: re-tagging a Document
    never needs it re-extracted."""
    doc_id = record["doc_id"]
    ver = (record.get("extraction_meta") or {}).get("verification") or {}
    threshold = ver.get("threshold", EVIDENCE_MIN_JACCARD)
    doc_stages = sorted(record.get("stage_relevance") or [])
    topics = [record["category"]] if record.get("category") else []
    loc = locators.kind(record)
    base = {
        "doc_id": doc_id, "source_kind": record.get("source_kind") or SOURCE_KIND,
        "series": record.get("series") or "", "title": record.get("title_canonical")
        or record.get("title_raw") or "", "speaker": record.get("speaker") or "",
        "provenance": record.get("provenance") or "",
        "published_at": record.get("published_at") or "", "year": _year(record.get("published_at")),
        "topics": topics, "domains": list(domains or [DEFAULT_DOMAIN]), "source_id": source_id or "",
        # a Private Source's items never leave this machine (pack, released eval) [ADR-0014]
        "visibility": "private" if record.get("private") else "public",
    }

    def item(kind: str, iid: str, text: str, evidence: str, ms: int | None, end_ms: int | None,
             stages: list[str], origin: str) -> dict:
        header = context_header(record, _chapter_at(record, ms))
        indexable = f"{header}\n{text}" + (f"\nQuote: {evidence}" if evidence else "")
        return {**base, "item_id": iid, "kind": kind, "text": text, "evidence": evidence or "",
                "start_ms": int(ms) if ms is not None else -1,
                "end_ms": int(end_ms) if end_ms is not None else -1,
                "deep_link": deep_link(record.get("url"), doc_id, ms, loc, evidence or (text if kind == "passage" else None)),
                "stages": stages, "stage_origin": origin,
                "context_header": header, "indexable": indexable}

    out: list[dict] = []
    for a in record.get("advice_atoms") or []:
        if _verified(a, threshold):
            own = sorted(a.get("applies_to_stage") or [])
            out.append(item("advice", f"adv:{doc_id}:{a.get('atom_id')}", a["text"],
                            a["evidence_span"], a.get("timestamp_ms"), None,
                            own or doc_stages, "item" if own else "document"))
    for n, h in enumerate(record.get("highlights") or [], 1):
        if _verified(h, threshold):
            out.append(item("takeaway", f"tkw:{doc_id}:{n:02d}", h["text"], h["evidence_span"],
                            h.get("timestamp_ms"), None, doc_stages, "document"))
    if record.get("summary"):
        out.append(item("summary", f"sum:{doc_id}", record["summary"], "", 0, None,
                        doc_stages, "document"))
    if transcript and transcript.get("utterances"):
        for c in chunk_transcript({"doc_id": doc_id, "utterances": transcript["utterances"]}):
            out.append(item("passage", f"psg:{doc_id}:{c.start_ms}" + (f".{c.part}" if c.part else ""), c.text, "", c.start_ms,
                            c.end_ms, doc_stages, "document"))
    return out
