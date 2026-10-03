"""`ytbrain books inspect`: what the pipeline would make of a PDF, before any paid extraction.

Probe (encrypted? scanned? outline? page labels?), parse once (cached by file hash under
data/books/parsed/), split into Chapters, and report: timing, what was dropped, which detection
method won and why the others didn't, each Chapter's pages and length, and warnings."""
from __future__ import annotations

import json
from pathlib import Path

from ..pages import atomic_write_text
from .chapters import chapter_words, detect
from .ligatures import still_damaged
from .metadata import resolve
from .model import ParsedBook
from .parse import BookParser, ParseError, parse, parser_for
from .probe import PdfFacts, probe, text_warnings


def cached_parse(path: Path, cache_dir: Path, parser: BookParser | None = None, reparse: bool = False,
                 facts: PdfFacts | None = None):
    """(facts, ParsedBook or None, cached?) for one PDF. A parse is reused while the file's hash,
    the parser and the parse format are unchanged; a damaged cache file is parsed again."""
    facts = facts or probe(path)
    if not facts.ok:
        return facts, None, False
    parser = parser or parser_for("pdfium")
    cache = cache_dir / f"{facts.sha256[:16]}.{parser.name}.json"
    if cache.exists() and not reparse:
        try:
            return facts, ParsedBook.from_dict(json.loads(cache.read_text(encoding="utf-8"))), True
        except (ValueError, TypeError, KeyError):
            pass
    book = parse(path, facts, parser)
    cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cache, json.dumps(book.to_dict(), ensure_ascii=False))
    return facts, book, False


def report(path: Path, cache_dir: Path, config: dict | None = None, parser: BookParser | None = None,
           reparse: bool = False) -> dict:
    """One PDF's report. A book that fails to parse is reported as refused; it never stops the
    other books in the same run. A missing `pdf` extra (RuntimeError) does stop the run."""
    facts, book, cached = probe(Path(path)), None, False
    try:
        facts, book, cached = cached_parse(Path(path), cache_dir, parser, reparse, facts)
    except ParseError as e:
        facts.ok, facts.reason = False, f"parse failed: {e}"
    except RuntimeError:
        raise                                                 # the pdf extra is missing: stop the run
    except Exception as e:                                    # noqa: BLE001 -- a parser bug on one book
        facts.ok, facts.reason = False, f"parse failed: {type(e).__name__}: {str(e)[:200]}"
    out = {"file": Path(path).name, "sha256": facts.sha256[:16], "pages": facts.pages,
           "text_page_share": facts.text_page_share, "ok": facts.ok, "reason": facts.reason}
    if book is None:
        return out
    meta = resolve(facts.meta, facts.edge_pages, config or {})
    plan = detect(book, config or {})
    plan.warnings += text_warnings(facts)
    words = [chapter_words(book, c) for c in plan.chapters]
    repaired = dict(book.repaired or {})
    if repaired:                 # warn only about text that becomes Chapters (not Notes, Index, URLs there)
        left = still_damaged(p.text for c in plan.chapters for p in book.paragraphs[c.first:c.last + 1])
        repaired.update(unresolved=sum(left.values()),
                        unresolved_examples=[k for k, _ in left.most_common(8)])
    out.update({
        "parser": book.parser, "parse_seconds": book.seconds, "cached": cached,
        "seconds_per_page": round(book.seconds / max(1, book.pages), 2),
        "metadata": {k: v for k, v in meta.as_dict().items() if k not in ("copyright_names",)},
        "metadata_problems": meta.problems(),
        "page_labels": bool(book.page_labels), "outline_entries": len(book.outline),
        "paragraphs": len(book.paragraphs), "dropped": book.dropped, "repaired": repaired,
        "contents_pages": book.contents_pages,
        "method": plan.method, "tried": plan.tried, "skipped": plan.skipped, "warnings": plan.warnings,
        "chapters": [{"n": c.ordinal, "title": c.title,
                      "pages": f"{book.label(c.start_page)}-{book.label(c.end_page)}", "words": w}
                     for c, w in zip(plan.chapters, words)],
        "words": sum(words),
    })
    return out


def render(r: dict) -> str:
    """The report as a few readable lines (no book text beyond chapter titles)."""
    lines = [f"{r['file']}  ({r['pages']} pages, sha {r['sha256']})"]
    if not r["ok"]:
        return "\n".join(lines + [f"  refused: {r['reason']}"])
    how = "cached parse" if r["cached"] else f"parsed in {r['parse_seconds']}s ({r['seconds_per_page']}s/page)"
    lines.append(f"  {r['parser']}: {how}; text on {r['text_page_share']:.0%} of pages; "
                 f"{r['paragraphs']} paragraphs, {r['words']} words in chapters")
    md = r["metadata"]
    lines.append(f"  book: {md['title']!r}" + (f" ({md['subtitle']})" if md.get("subtitle") else "")
                 + f" by {md.get('speaker') or '?'}, {md.get('year') or '?'}, ISBN {md.get('isbn') or '?'}"
                 + f"  [id {md['book_id']}; copyright page {md.get('copyright_page') or '?'}]")
    for p in r["metadata_problems"]:
        lines.append(f"  metadata: {p}  <- set it in sources.yaml (books: <file>: ...)")
    lines.append(f"  outline: {r['outline_entries']} entries; printed page labels: {'yes' if r['page_labels'] else 'no'}")
    if r["dropped"]:
        lines.append("  dropped: " + ", ".join(f"{k} {v}" for k, v in sorted(r["dropped"].items())))
    fix = r.get("repaired") or {}
    if fix.get("words") or fix.get("hyphens"):
        lines.append(f"  repaired: {fix['words']} damaged ligature words ({fix['distinct']} distinct, e.g. "
                     + ", ".join(fix["examples"][:3]) + f"), {fix['hyphens']} hyphens")
    lines.append(f"  chapters by {r['method']}:")
    for t in r["tried"]:
        lines.append(f"    tried {t}")
    for c in r["chapters"]:
        lines.append(f"    {c['n']:>2}. {c['title'][:60]:<60} pp. {c['pages']:<9} {c['words']:>6} words")
    if r["skipped"]:
        lines.append("  skipped: " + "; ".join(r["skipped"]))
    if fix.get("unresolved"):
        lines.append(f"  warning: {fix['unresolved']} words look damaged (a stray symbol, digit or capital inside "
                     f"the word) but no ligature explains them, e.g. {', '.join(fix['unresolved_examples'][:5])}")
    for w in r["warnings"]:
        lines.append(f"  warning: {w}")
    return "\n".join(lines)


EXPECT_FIELDS = ("title", "subtitle", "authors", "year", "isbn")


def compare(r: dict, want: dict) -> list[str]:
    """Mismatches between one report and its expected block (data/books/expected.yaml)."""
    if not r["ok"]:
        return [f"refused: {r['reason']}"]
    md, got = r["metadata"], []
    for f in EXPECT_FIELDS:
        if f in want and (str(md.get(f)) if f in ("year", "isbn") else md.get(f)) != (str(want[f]) if f in ("year", "isbn") else want[f]):
            got.append(f"{f}: expected {want[f]!r}, got {md.get(f)!r}")
    chapters = r["chapters"]
    checks = (("method", r["method"]), ("chapters", len(chapters)),
              ("first_chapter", chapters[0]["title"] if chapters else None),
              ("last_chapter", chapters[-1]["title"] if chapters else None))
    for f, have in checks:
        if f in want and have != want[f]:
            got.append(f"{f}: expected {want[f]!r}, got {have!r}")
    return got

