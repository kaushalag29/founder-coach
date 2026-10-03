"""The pdf_books Source type's adapter (ADR-0013, ADR-0014).

sync   every PDF of every enabled pdf_books Source: probe, parse (cached per file hash), resolve the
       metadata, split into Chapters, then one manifest Document per Chapter
       (`<book_id>__<chapter-slug>`) and a plan file (data/books/plans/<book_id>.json) that `clean`
       reads. A refused PDF is reported and its Chapters leave the index; a PDF that fails to parse
       is reported and keeps its Chapters; either way the other books go on. Books that disappear
       from the folder are tombstoned.
clean  one Chapter -> the shared transcript: its paragraphs as text units at `page * 1000 + n`,
       its headings as Sections, and the Book's given fields (series = title, speaker = authors,
       year, ISBN, `private: true`). A Chapter whose text and metadata are unchanged is not
       cleaned again; a changed one sends its extraction back.

Books are a Private Source: records carry `private: true` and the pack leaves them out (P3)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import pages
from ..manifest import StageState
from .chapters import detect
from .inspect import cached_parse
from .metadata import OpenLibrary, resolve, slug
from .model import ParsedBook
from .parse import ParseError, parser_for
from .probe import probe, text_warnings

PLAN_FORMAT = 1


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def chapter_ids(book_id: str, titles: list[str]) -> list[str]:
    """`<book_id>__<title-slug>`, unique within the Book (a repeated title gets its ordinal).
    Never 11 characters long: that is a YouTube id's length, and Moments tell a Chapter from a
    talk by its id (eval/moments.py)."""
    out, seen = [], set()
    for n, t in enumerate(titles, 1):
        did = f"{book_id}__{slug(t, 48)}"
        if did in seen:
            did = f"{did}-{n}"
        if len(did) <= 11:
            did = f"{book_id}__chapter-{n:02d}-{slug(t, 48)}"
        seen.add(did)
        out.append(did)
    return out


class BookAdapter:
    type, doc_type = "pdf_books", "book"

    def __init__(self, parser=None, lookup=None, say=print, root: Path | None = None,
                 parsed_dir: Path | None = None, plans_dir: Path | None = None, transcripts: Path | None = None):
        from ..config import BOOKS_PARSED, BOOKS_PLANS, ROOT, TRANSCRIPTS
        self._parser, self._lookup, self.say = parser, lookup, say
        self.root = root or ROOT
        self.parsed_dir, self.plans_dir = parsed_dir or BOOKS_PARSED, plans_dir or BOOKS_PLANS
        self.transcripts = transcripts or TRANSCRIPTS
        self._parsers: dict[str, object] = {}

    # ------------------------------------------------------------------ sync
    def _parser_for(self, name: str):
        if self._parser is not None:
            return self._parser
        if name not in self._parsers:
            self._parsers[name] = parser_for(name)       # one instance per run: a model loads once
        return self._parsers[name]

    def _lookup_for(self, src: dict):
        if src.get("lookup") != "openlibrary":
            return None
        if self._lookup is not None:
            return self._lookup
        from ..config import BOOKS_LOOKUPS, WEB_USER_AGENT
        return OpenLibrary(BOOKS_LOOKUPS, WEB_USER_AGENT)

    def sync(self, m, sources: list[dict], args, say=None) -> dict:
        from ..sources import book_files
        say = say or self.say
        code, chapters = 0, 0
        for si, src in enumerate(sources, 1):
            files = book_files(src, self.root)
            say(f"[{si}/{len(sources)}] {src['id']}: {len(files)} PDF(s) in {src['path']} (private)")
            seen: set[str] = set()
            for fi, pdf in enumerate(files, 1):
                try:
                    res = self.sync_book(m, src, pdf)
                except RuntimeError as e:                 # the pdf extra is missing: nothing can work
                    say(f"sync: {e}")
                    return {"code": 2, "videos": 0}
                seen |= res["keep"]
                chapters += res["chapters"]
                code = code or (1 if res["status"] != "ok" else 0)
                say(f"  [{fi}/{len(files)}] {res['line']}")
                for w in res["notes"]:
                    say(f"        {w}")
            gone = m.tombstone_missing(src["id"], seen)
            if gone:
                say(f"  {len(gone)} Chapter(s) of books no longer in {src['path']} (or refused) leave the index")
        say(f"sync: {chapters} Chapter(s) registered from books")
        return {"code": code, "videos": 0}

    def sync_book(self, m, src: dict, pdf: Path) -> dict:
        """Register one PDF's Chapters. Returns {status, line, notes, keep (doc ids to keep), chapters}."""
        overrides = (src.get("books") or {}).get(pdf.name) or {}
        if overrides.get("skip"):
            return {"status": "ok", "line": f"{pdf.name}: skipped (skip: true in sources.yaml)", "notes": [],
                    "keep": set(), "chapters": 0}
        facts = probe(pdf)
        if not facts.ok:
            return {"status": "refused", "line": f"{pdf.name}: refused: {facts.reason}", "notes": [],
                    "keep": set(), "chapters": 0}
        meta = resolve(facts.meta, facts.edge_pages, overrides, self._lookup_for(src))   # before parsing: the ids need it
        parser = self._parser_for(src.get("parser") or "pdfium")
        try:
            facts, book, _cached = cached_parse(pdf, self.parsed_dir, parser, facts=facts)
        except ParseError as e:                          # a transient failure keeps what was there
            known = {d for d in m.known_ids(src["id"]) if d.startswith(f"{meta.book_id}__")}
            return {"status": "failed", "line": f"{pdf.name}: parse failed: {e} (its {len(known)} Chapters are kept)",
                    "notes": [], "keep": known, "chapters": 0}
        plan = detect(book, overrides)
        ids = chapter_ids(meta.book_id, [c.title for c in plan.chapters])
        rows = []
        for did, ch in zip(ids, plan.chapters):
            text = [p.text for p in book.paragraphs[ch.first:ch.last + 1]]
            rows.append({"doc_id": did, "ordinal": ch.ordinal, "title": ch.title, "first": ch.first, "last": ch.last,
                         "start_page": ch.start_page, "end_page": ch.end_page, "input_hash": _hash(text, ch.title, meta.as_dict())})
        doc = {"format": PLAN_FORMAT, "book_id": meta.book_id, "file": pdf.name, "path": str(pdf), "sha256": facts.sha256,
               "parser": book.parser, "source_id": src["id"], "meta": meta.as_dict(), "method": plan.method,
               "skipped": plan.skipped, "warnings": plan.warnings, "problems": meta.problems(), "chapters": rows}
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        pages.atomic_write_text(self.plans_dir / f"{meta.book_id}.json", json.dumps(doc, indent=1, ensure_ascii=False))
        for r in rows:
            existing = m.get_document(r["doc_id"])
            if existing is not None and existing["tombstoned_at"]:
                m.restore(r["doc_id"])
            m.upsert_document(r["doc_id"], src["id"], doc_type=self.doc_type, title=r["title"], series=meta.title,
                              url=meta.url, published_at=str(meta.year) if meta.year else None,
                              provenance=meta.publisher or "book", caption_kind="none")
            m.mark(StageState(r["doc_id"], "fetch", "ok", input_hash=facts.sha256[:16], output_hash=r["input_hash"]))
            prev = m.stage(r["doc_id"], "clean")
            if prev is not None and prev["status"] in ("ok", "skipped") and prev["input_hash"] != r["input_hash"]:
                m.mark(StageState(r["doc_id"], "clean", "stale", input_hash=prev["input_hash"],   # keep the old output hash:
                                  output_hash=prev["output_hash"],                                  # clean compares with it
                                  error="chapter text or book metadata changed"))
        who = meta.speaker or "?"
        line = (f"{meta.title or pdf.name} ({who}, {meta.year or '?'}): {len(rows)} chapters by {plan.method}"
                + (f", {len(plan.skipped)} skipped" if plan.skipped else ""))
        notes = [f"metadata: {p}" for p in meta.problems()] + [f"warning: {w}" for w in plan.warnings + text_warnings(facts)]
        return {"status": "ok", "line": line, "notes": notes, "keep": set(ids), "chapters": len(rows)}

    # ------------------------------------------------------------------ clean
    def _plan(self, doc_id: str) -> dict | None:
        path = self.plans_dir / f"{doc_id.split('__', 1)[0]}.json"
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return plan if plan.get("format") == PLAN_FORMAT else None

    def clean(self, m, doc_id: str, doc) -> str:
        from ..config import MIN_TRANSCRIPT_WORDS
        plan = self._plan(doc_id)
        row = next((r for r in (plan or {}).get("chapters", []) if r["doc_id"] == doc_id), None)
        book = self._parsed(plan) if plan else None
        if row is None or book is None:
            m.mark(StageState(doc_id, "fetch", "stale", error="book plan or parse missing: run sync"))
            print("BOOK PLAN OR PARSE MISSING -- run `ytbrain sync` again", flush=True)
            return "refetch"
        meta = plan["meta"]
        paras = book.paragraphs[row["first"]:row["last"] + 1]
        units, sections, labels, per_page = [], [], {}, {}
        for p in paras:
            n = per_page[p.page] = per_page.get(p.page, 0) + 1
            pos = p.page * 1000 + n
            if book.page_labels.get(p.page):
                labels[str(p.page)] = book.page_labels[p.page]
            if p.kind == "heading":
                if p is not paras[0] or not _is_title(p.text, row["title"]):
                    sections.append({"chapter_id": f"s{len(sections) + 1:02d}", "title": p.text[:120],
                                     "start_ms": pos, "source": "uploader"})
                continue
            units.append({"text": p.text, "start_ms": pos, "end_ms": pos})
        for i, s in enumerate(sections):
            s["end_ms"] = sections[i + 1]["start_ms"] if i + 1 < len(sections) else (units[-1]["end_ms"] if units else s["start_ms"])
        words = sum(len(u["text"].split()) for u in units)
        if words < MIN_TRANSCRIPT_WORDS:
            m.mark(StageState(doc_id, "clean", "skipped", input_hash=row["input_hash"], error=f"chapter too short ({words} words)"))
            print(f"TOO SHORT ({words} words) -- not extracted", flush=True)
            return "skipped"
        rec = {"doc_id": doc_id, "title": row["title"], "series": meta["title"],
               "published_at": str(meta["year"]) if meta.get("year") else None, "caption_kind": "none",
               "source_kind": "chapter", "locator": "page", "url": meta.get("url"), "speaker": meta.get("speaker"),
               "provenance": meta.get("publisher") or "book", "language": "en", "private": True,
               "book": {"book_id": plan["book_id"], "title": meta["title"], "subtitle": meta.get("subtitle"),
                        "authors": meta.get("authors") or [], "year": meta.get("year"), "isbn": meta.get("isbn"),
                        "chapter": row["ordinal"], "chapters": len(plan["chapters"]), "file": plan["file"]},
               "page_labels": labels, "chapters": sections,
               "quality": {"words": words, "paragraphs": len(units), "pages": [row["start_page"], row["end_page"]]},
               "utterances": units}
        body = pages.canonical_json(rec)
        out_hash = hashlib.sha256(body.encode()).hexdigest()[:16]
        prev = m.stage(doc_id, "clean")
        changed = prev is not None and prev["output_hash"] and prev["output_hash"] != out_hash
        if changed and m.stage_status(doc_id, "extract") is not None:
            m.mark(StageState(doc_id, "extract", "stale", error="chapter changed"))
        self.transcripts.mkdir(parents=True, exist_ok=True)
        pages.atomic_write_text(self.transcripts / f"{doc_id}.json", body)
        m.mark(StageState(doc_id, "clean", "ok", input_hash=row["input_hash"], output_hash=out_hash, attempts=1))
        m.succeeded(doc_id, "clean")
        print(f"chapter {row['ordinal']}/{len(plan['chapters'])} · pp. {row['start_page']}-{row['end_page']} · "
              f"{len(units)} paragraphs · {words} words" + (" · changed: re-extract queued" if changed else ""), flush=True)
        return "ok"

    def _parsed(self, plan: dict) -> ParsedBook | None:
        path = self.parsed_dir / f"{plan['sha256'][:16]}.{plan['parser']}.json"
        try:
            return ParsedBook.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError):
            return None


def _is_title(text: str, title: str) -> bool:
    from .chapters import _opens_with
    return _opens_with(text, title)
