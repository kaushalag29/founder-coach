"""A fast, model-free look at a PDF before any parsing (pypdfium2, Apache/BSD): whether it can be
read at all, whether it has a text layer, and what it declares about itself (outline, printed
page labels, metadata). Refusals happen here, in well under a second, never after a slow parse."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .model import OutlineEntry
from .normalize import garbled_line

MIN_CHARS_PER_PAGE = 200         # a page with less than this is not text (a scan, a figure, a blank)
MIN_TEXT_PAGE_SHARE = 0.3        # below this share of pages with text, the PDF is a scan
MAX_GARBLED_LINE_SHARE = 0.15    # above this share of garbled lines the text layer is unusable (refused)
WARN_GARBLED_PAGE_SHARE = 0.02   # above this share of pages with a garbled line, the report warns


@dataclass
class PdfFacts:
    path: str
    sha256: str
    ok: bool
    reason: str = ""                                    # why it can't be ingested (when not ok)
    pages: int = 0
    text_page_share: float = 0.0
    edge_pages: dict[int, list[str]] = field(default_factory=dict)   # lines of the first and last pages (copyright page)
    contents: list[str] = field(default_factory=list)   # entries of the printed contents page(s), in order
    contents_page: int = 0
    garbled_lines: int = 0                              # lines whose font has no working text map (normalize.garbled_line)
    garbled_pages: int = 0
    judged_lines: int = 0
    outline: list[OutlineEntry] = field(default_factory=list)
    page_labels: dict[int, str] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


def text_warnings(facts: PdfFacts) -> list[str]:
    """What a usable PDF's text layer loses, for sync and inspect alike."""
    if facts.garbled_pages / max(1, facts.pages) > WARN_GARBLED_PAGE_SHARE:
        return [(f"{facts.garbled_pages} pages have garbled lines (fonts without a text map, {facts.garbled_lines} "
                 "lines): that text is lost; a better copy would fix it")]
    return []


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


EDGE_FRONT, EDGE_BACK = 15, 8     # where a copyright page sits: the first 15 or the last 8 pages
CONTENTS_SEARCH_PAGES = 25      # a printed contents page is looked for in the first pages only
_CONTENTS_TITLE = re.compile(r"^(table of )?contents$", re.IGNORECASE)
_LEADER_PAGE = re.compile(r"[\s.\u2026\u00b7_]*(\d{1,4}|[ivxlc]{1,6})$", re.IGNORECASE)   # "..... 47" at the end of an entry


def contents_entries(pages: list[list[str]]) -> tuple[int, list[str]]:
    """(1-based page, entries) of the printed contents: the lines after a "Contents" heading, and
    the next page's too when it is still a list of short lines. Page numbers and dot leaders go."""
    for i, lines in enumerate(pages):
        at = next((k for k, ln in enumerate(lines[:3]) if _CONTENTS_TITLE.match(ln)), None)
        if at is None:
            continue
        rows = lines[at + 1:]
        nxt = pages[i + 1] if i + 1 < len(pages) else []
        if len(nxt) >= 3 and sum(len(ln) < 70 for ln in nxt) >= 0.8 * len(nxt):
            rows += nxt
        entries = [e for e in (_LEADER_PAGE.sub("", ln).strip(" .") for ln in rows) if len(e) > 1]
        return i + 1, entries[:150]
    return 0, []


def probe(path: Path) -> PdfFacts:
    """What the file is, without a layout model. Never raises for a bad PDF: `ok` says."""
    path = Path(path)
    try:
        facts = PdfFacts(path=str(path), sha256=file_sha256(path), ok=False)
    except OSError as e:
        return PdfFacts(path=str(path), sha256="", ok=False, reason=f"can't read the file ({e.strerror or e})")
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise RuntimeError('books need the pdf extra: uv pip install -e ".[pdf]"')
    try:
        doc = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as e:
        msg = str(e)
        facts.reason = ("encrypted: the PDF needs a password (ytbrain never removes DRM or passwords)"
                        if "password" in msg.lower() else f"not a readable PDF ({msg})")
        return facts
    try:
        facts.pages = len(doc)
        if not facts.pages:
            facts.reason = "the PDF has no pages"
            return facts
        with_text = 0
        page_lines: list[list[str]] = []
        for i in range(facts.pages):                     # every page: ~1 ms each, so a 1,000-page book takes a second
            page = doc[i]
            tp = page.get_textpage()
            try:
                text = tp.get_text_bounded() or ""
            finally:
                tp.close()
                page.close()
            with_text += len(text.strip()) >= MIN_CHARS_PER_PAGE
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if i < CONTENTS_SEARCH_PAGES:
                page_lines.append(lines)
            if i < EDGE_FRONT or i >= facts.pages - EDGE_BACK:
                facts.edge_pages[i + 1] = lines
            verdicts = [v for v in map(garbled_line, text.splitlines()) if v is not None]
            facts.judged_lines += len(verdicts)
            facts.garbled_lines += sum(verdicts)
            facts.garbled_pages += any(verdicts)
        facts.text_page_share = round(with_text / facts.pages, 3)
        facts.contents_page, facts.contents = contents_entries(page_lines)
        facts.page_labels = {}
        for i in range(facts.pages):
            label = doc.get_page_label(i) if hasattr(doc, "get_page_label") else ""
            if label:
                facts.page_labels[i + 1] = label
        if all(facts.page_labels.get(i + 1) == str(i + 1) for i in range(facts.pages)):
            facts.page_labels = {}                      # labels that equal the PDF page numbers say nothing
        facts.outline = []
        for bm in doc.get_toc():
            dest = bm.get_dest()
            idx = dest.get_index() if dest is not None else None
            title = " ".join((bm.get_title() or "").split())
            if idx is not None and title:
                facts.outline.append(OutlineEntry(title=title, page=idx + 1, level=int(bm.level)))
        meta = doc.get_metadata_dict() or {}
        facts.meta = {k.lower(): " ".join(v.split()) for k, v in meta.items()
                      if k in ("Title", "Author", "Subject", "CreationDate") and v and v.strip()}
    finally:
        doc.close()
    if facts.text_page_share < MIN_TEXT_PAGE_SHARE:
        facts.reason = (f"scanned: only {facts.text_page_share:.0%} of pages have a text layer "
                        "(no OCR in v1)")
        return facts
    if facts.judged_lines and facts.garbled_lines / facts.judged_lines > MAX_GARBLED_LINE_SHARE:
        facts.reason = (f"garbled text layer: {facts.garbled_lines / facts.judged_lines:.0%} of lines come from fonts "
                        "without a text map; use another copy (no OCR in v1)")
        return facts
    facts.ok = True
    return facts
