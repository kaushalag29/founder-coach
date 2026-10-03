"""Markdown projection of the canonical JSON record.

The JSON is the source of truth; this is a pure, regenerable view of it
(decision Q12). It exists because phase 1 has no retrieval layer, so markdown
in git IS the query mechanism: greppable, diffable, readable, and directly
usable as agent context.

The property that makes that true is DETERMINISTIC SERIALIZATION. Without
stable key order, stable list order and fixed float precision, every re-run
produces a huge diff of reordered noise and the git history becomes worthless -
which would defeat the entire reason for choosing markdown.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import METADATA, PAGES


def canonical_json(record: dict) -> str:
    """Stable JSON: sorted keys, fixed separators, rounded floats."""
    return json.dumps(_round_floats(record), indent=2, sort_keys=True,
                      ensure_ascii=False) + "\n"


def _round_floats(obj, places: int = 3):
    if isinstance(obj, float):
        return round(obj, places)
    if isinstance(obj, dict):
        return {k: _round_floats(v, places) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, places) for v in obj]
    return obj


def atomic_write_text(path: Path, text: str) -> Path:
    """Write via a temp file + rename. A Ctrl+C mid-write leaves the old file
    (or none), never a truncated one that later crashes `pages`/`report`."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return path


def write_record(record: dict) -> Path:
    return atomic_write_text(METADATA / f"{record['doc_id']}.json", canonical_json(record))


def _ts(ms: int | None, locator: str = "time", record: dict | None = None) -> str:
    from .locators import label
    return label(ms, locator, (record or {}).get("page_labels"))


def _at(doc_id: str, ms: int | None, loc: str, record: dict, quote_text: str | None = None) -> str:
    """`[label](link)`, or the bare label when there is no link (a private Book's page)."""
    link = _link(doc_id, ms, record, quote_text)
    return f"[{_ts(ms, loc, record)}]({link})" if link else _ts(ms, loc, record)


def _link(doc_id: str, ms: int | None, record: dict | None = None, quote_text: str | None = None) -> str:
    from .locators import deep_link, kind
    if kind(record) == "time":           # talks: the video link from its id, exactly as before
        return f"https://www.youtube.com/watch?v={doc_id}&t={(int(ms or 0)) // 1000}s"
    return deep_link((record or {}).get("url"), doc_id, ms, kind(record), quote_text)


def _yaml_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace('"', '\\"')
    return f'"{s}"'


UNVERIFIED_MARK = "⚠ NOT FOUND IN TRANSCRIPT"


def _grounded(item: dict, threshold: float | None) -> bool:
    """True once verify found this item's quote in the transcript."""
    if threshold is None:                     # record not verified yet (the sample sheet marks
        return True                           # the whole record "Not yet verified"; `pages` skips it)
    score = item.get("match_score")
    return score is not None and score >= threshold


def render_markdown(record: dict, show_unverified: bool = False) -> str:
    """Frontmatter carries the filterable scalars; the body carries the prose.

    File is named by doc_id, never by title - titles get edited upstream and
    would produce phantom renames in git.

    Takeaways and advice whose evidence quote verify could not find in the
    transcript are left OUT of the page: these pages are what the founder-coach
    agent reads, and an ungrounded claim there is indistinguishable from a
    real one. They stay in the JSON record (with match_score), and the page
    says how many were withheld. `show_unverified=True` (the human review
    sheet) shows them instead, flagged, so a reviewer can judge them.
    """
    doc_id = record["doc_id"]
    meta = record.get("extraction_meta") or {}
    ver = meta.get("verification") or {}
    threshold = ver.get("threshold") if ver else None

    def keep(items: list[dict]) -> tuple[list[tuple[dict, bool]], int]:
        rows = [(it, _grounded(it, threshold)) for it in items]
        hidden = sum(1 for _, ok in rows if not ok)
        return (rows if show_unverified else [r for r in rows if r[1]]), hidden

    highlights, hidden_h = keep(record.get("highlights") or [])
    atoms, hidden_a = keep(record.get("advice_atoms") or [])
    hidden = hidden_h + hidden_a
    loc = record.get("locator") or "time"
    kind = record.get("source_kind") or "talk"
    article = kind != "talk"
    fm = {
        **({"doc_id": doc_id, "source_kind": kind} if article else {"video_id": doc_id}),
        **({"private": True} if record.get("private") else {}),
        "title": record.get("title_canonical") or record.get("title_raw"),
        "title_raw": record.get("title_raw"),
        "url": record.get("url"),
        "series": record.get("series"),
        "provenance": record.get("provenance"),
        "speaker": record.get("speaker"),
        "category": record.get("category"),
        "published_at": record.get("published_at"),
        **({} if article else {"duration_s": record.get("duration_s"),
                               "caption_kind": record.get("caption_kind")}),
        "validation_status": meta.get("validation_status"),
        "chapter_source": meta.get("chapter_source"),
        "schema_version": meta.get("schema_version"),
        "generated": True,
        "verified": bool(ver),
        "withheld_unverified": hidden,
    }

    out = ["---"]
    for k in sorted(fm):                       # sorted: stable diffs
        out.append(f"{k}: {_yaml_scalar(fm[k])}")
    stages = sorted(record.get("stage_relevance") or [])
    out.append("stage_relevance: [" + ", ".join(stages) + "]")
    out += ["---", ""]

    out += [f"# {fm['title']}", "",
            f"> Generated from `data/metadata/{doc_id}.json` — do not edit by hand.", ""]
    if not ver:
        out += ["> **Not yet verified** — evidence quotes have not been checked against "
                "the transcript. Run `ytbrain verify`.", ""]

    if record.get("summary"):
        out += ["## Summary", "", record["summary"], ""]

    chapters = record.get("chapters") or []
    if chapters:
        out += ["## Chapters", ""]
        for ch in chapters:
            out.append(f"- {_at(doc_id, ch.get('start_ms'), loc, record)} — {ch.get('title')}")
        out.append("")

    if highlights:
        out += ["## Key takeaways", ""]
        for h, ok in highlights:
            where = (f"({_at(doc_id, h.get('timestamp_ms'), loc, record, h.get('evidence_span'))})"
                     if ok else f"**{UNVERIFIED_MARK}**")
            out.append(f"- **{h.get('text')}** {where}")
            if h.get("evidence_span"):
                out.append(f"  > {h['evidence_span']}")
        out.append("")

    if atoms:
        out += ["## Advice", ""]
        for a, ok in atoms:
            stages = ", ".join(sorted(a.get("applies_to_stage") or [])) or "any stage"
            where = (f"({_at(doc_id, a.get('timestamp_ms'), loc, record, a.get('evidence_span'))})"
                     if ok else f"**{UNVERIFIED_MARK}** — quote: “{a.get('evidence_span', '')}”")
            out.append(f"- {a.get('text')} _({stages})_ {where}")
        out.append("")

    ents = record.get("entities") or {}
    if any(ents.get(k) for k in ents):
        out += ["## Mentioned", ""]
        for key in sorted(ents):
            vals = sorted(ents.get(key) or [])
            if vals:
                out.append(f"- **{key.replace('_', ' ')}**: {', '.join(vals)}")
        out.append("")

    gaps = record.get("unknowns_and_gaps") or []
    if gaps:
        out += ["## Gaps", "",
                *[f"- {g}" for g in gaps], ""]

    if ver:
        out += ["---", "",
                f"_Verification: {ver.get('checked', 0)} spans checked, "
                f"{ver.get('failed', 0)} unmatched, "
                f"pass rate {ver.get('pass_rate', 0)}._"]
        if hidden and not show_unverified:
            out.append(f"_{hidden} claim(s) withheld from this page because their quote was not "
                       f"found in the transcript; they remain in the JSON record for review._")
        out.append("")

    return "\n".join(out)


def write_page(record: dict) -> Path:
    return atomic_write_text(PAGES / f"{record['doc_id']}.md", render_markdown(record))
