"""PDF -> ParsedBook. `BookParser` is the seam: DoclingParser today, another library (LiteParse,
a future Docling release) tomorrow, without touching chapters, clean or anything later.

A parser yields `Block`s (label, text, page, heading level, layer) in reading order; `assemble`
turns blocks into a ParsedBook the same way for every parser: body text, list items and headings
are kept and cleaned, a paragraph split by a page break is joined back, and everything else
(running headers and footers, tables, figures, captions, footnotes, formulas, code, the contents
list) is only counted. Text inside figures is never read.

Concurrency: one book at a time. Docling parallelises inside a book (its own threads and the
layout model on MPS/CPU); two books in one process would double the models in memory for little
gain. PDFium (PdfiumParser) is not thread-safe at all."""
from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .ligatures import repair_book, repair_text
from .model import OutlineEntry, Paragraph, ParsedBook
from .normalize import clean_text, continues, garbled_line, join
from .probe import PdfFacts

KEEP = {"text": "text", "paragraph": "text", "list_item": "list", "section_header": "heading", "title": "heading"}
CONTENTS = "document_index"


class ParseError(RuntimeError):
    """A book the parser could not read completely. Never ingest a partial book."""


@dataclass(frozen=True)
class Block:
    label: str                      # a Docling DocItemLabel value, or the same words from another parser
    text: str
    page: int                       # 1-based PDF page
    level: int = 0                  # heading depth, when known
    furniture: bool = False         # page furniture: running header/footer, page number


class BookParser(Protocol):
    name: str

    def blocks(self, path: Path) -> Iterable[Block]: ...


def assemble(blocks: Iterable[Block], facts: PdfFacts, parser: str, seconds: float = 0.0) -> ParsedBook:
    paragraphs: list[Paragraph] = []
    dropped: dict[str, int] = {}
    section_starts = {o.page for o in facts.outline}          # a bookmark's page never continues the page before
    contents_pages: set[int] = set()
    for b in blocks:
        kind = KEEP.get(b.label)
        if b.furniture or kind is None:
            key = "running header/footer" if b.furniture else b.label
            dropped[key] = dropped.get(key, 0) + 1
            if b.label == CONTENTS:
                contents_pages.add(b.page)
            continue
        text = clean_text(b.text)
        if not text:
            continue
        prev = paragraphs[-1] if paragraphs else None
        if (kind == "text" and prev is not None and prev.kind == "text" and b.page == prev.page + 1
                and b.page not in section_starts and continues(prev.text, text)):
            paragraphs[-1] = Paragraph(join(prev.text, text), prev.page, "text")
            dropped["joined across a page break"] = dropped.get("joined across a page break", 0) + 1
            continue
        paragraphs.append(Paragraph(text, b.page, kind, max(1, b.level) if kind == "heading" else 0))
    # a font without ligature maps ("di@erent") is repaired once, for every parser, before chapters are
    # found, so titles, headings and body text agree
    fixed, repairs = repair_book([p.text for p in paragraphs])
    paragraphs = [Paragraph(t, p.page, p.kind, p.level) for t, p in zip(fixed, paragraphs, strict=True)]
    vocab = {w: n for w, n in _own(fixed).items()}
    outline = [OutlineEntry(repair_text(o.title, vocab, paragraph_start=True), o.page, o.level) for o in facts.outline]
    contents = [repair_text(e, vocab, paragraph_start=True) for e in facts.contents]
    return ParsedBook(path=facts.path, sha256=facts.sha256, pages=facts.pages, paragraphs=paragraphs,
                      outline=outline, page_labels=dict(facts.page_labels), meta=dict(facts.meta),
                      contents_pages=sorted(contents_pages), contents=contents,
                      contents_page=facts.contents_page, dropped=dropped, repaired=repairs.as_dict(),
                      parser=parser, seconds=round(seconds, 1))


def _own(texts: list[str]) -> dict[str, int]:
    """The vocabulary a Book's titles are repaired with: the bundled words and the book's own."""
    from .ligatures import english, own_words
    return {**english(), **own_words(texts)}


def parse(path: Path, facts: PdfFacts, parser: BookParser) -> ParsedBook:
    t0 = time.monotonic()
    blocks = list(parser.blocks(Path(path)))
    return assemble(blocks, facts, parser.name, time.monotonic() - t0)


class DoclingParser:
    """Docling's standard PDF pipeline (MIT): a layout model finds body text, headings, running
    headers and footers, tables and figures, in reading order. OCR, table structure and image
    rendering are off (text only, ADR-0014); heading levels come from the PDF's bookmarks, then
    numbering, then font style. The layout models download once from Hugging Face."""
    name = "docling"

    def __init__(self, threads: int | None = None, timeout_s: float | None = None):
        from ..config import BOOK_PARSE_TIMEOUT_S, BOOK_THREADS
        self.threads = threads or BOOK_THREADS or max(1, min(8, (os.cpu_count() or 2) - 1))
        self.timeout_s = timeout_s or BOOK_PARSE_TIMEOUT_S
        self._converter = None                    # built once, reused for every book (models load once)

    def _make(self):
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                HeadingHierarchyOptions,
                PdfPipelineOptions,
            )
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as e:
            raise RuntimeError('the Docling parser is parked; it needs: uv pip install -e ".[pdf-docling]"') from e
        opts = PdfPipelineOptions(
            do_ocr=False, do_table_structure=False, generate_page_images=False,
            generate_picture_images=False, generate_parsed_pages=True,      # parsed pages: font-style heading levels
            heading_hierarchy_options=HeadingHierarchyOptions(enabled=True),
            document_timeout=self.timeout_s)
        from docling.datamodel.accelerator_options import AcceleratorOptions
        opts.accelerator_options = AcceleratorOptions(num_threads=self.threads)   # device: auto (MPS on Apple silicon)
        return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})

    def blocks(self, path: Path) -> Iterable[Block]:
        if self._converter is None:
            self._converter = self._make()
        res = self._converter.convert(str(path), raises_on_error=False)
        status = _value(res.status)
        if status != "success":                  # partial_success (a timeout, failed pages) is a failure: never half a book
            why = "; ".join(str(getattr(e, "error_message", e)) for e in (getattr(res, "errors", None) or [])[:3])
            raise ParseError(f"docling {status}: {why or 'no detail'}")
        try:                                     # the real enum: Enum hashes by name, so plain strings won't match in a set
            from docling_core.types.doc import ContentLayer
            layers = {ContentLayer.BODY, ContentLayer.FURNITURE}
        except ImportError:                      # a stand-in converter in the tests
            layers = {"body", "furniture"}
        for item, _depth in res.document.iterate_items(included_content_layers=layers, traverse_pictures=False):
            prov = getattr(item, "prov", None) or []
            if not prov:
                continue
            yield Block(label=_value(getattr(item, "label", "")), text=getattr(item, "text", "") or "",
                        page=int(prov[0].page_no), level=int(getattr(item, "level", 0) or 0),
                        furniture=_value(getattr(item, "content_layer", "body")) == "furniture")


class PdfiumParser:
    """Model-free and fast (pypdfium2, about a second per book): text lines per page, running
    headers and footers found by repetition, paragraphs from line length and end punctuation. It
    finds no headings, so Chapters come from the PDF's outline or a manual list; use it for
    dry-runs and books with good bookmarks, Docling for the rest."""
    name = "pdfium"
    EDGE = 2                  # lines at the top and bottom of a page that can be a running header/footer
    REPEAT_SHARE = 0.2        # an edge line (digits ignored) on this share of pages is furniture

    def blocks(self, path: Path) -> Iterable[Block]:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        try:
            pages = []
            for i in range(len(doc)):
                page = doc[i]
                tp = page.get_textpage()
                try:
                    pages.append([ln.strip() for ln in (tp.get_text_bounded() or "").splitlines() if ln.strip()])
                finally:
                    tp.close()
                    page.close()
        finally:
            doc.close()
        furniture = self._furniture(pages)
        for n, lines in enumerate(pages, 1):
            body = []
            for k, ln in enumerate(lines):
                edge = k < self.EDGE or k >= len(lines) - self.EDGE
                if edge and (self._key(ln) in furniture or _PAGE_NO.fullmatch(ln)):
                    yield Block("page_footer" if k >= len(lines) - self.EDGE else "page_header", ln, n, furniture=True)
                elif garbled_line(ln):
                    yield Block("garbled line", ln, n)            # counted, never kept: it can't be quoted
                else:
                    body.append(ln)
            yield from (Block("text", para, n) for para in self._paragraphs(body))

    @staticmethod
    def _key(line: str) -> str:
        return _DIGITS.sub("#", line.lower())

    def _furniture(self, pages: list[list[str]]) -> set[str]:
        seen: dict[str, int] = {}
        for lines in pages:
            for key in {self._key(ln) for ln in lines[:self.EDGE] + lines[-self.EDGE:]}:
                seen[key] = seen.get(key, 0) + 1
        need = max(3, int(len(pages) * self.REPEAT_SHARE))
        return {k for k, v in seen.items() if v >= need}

    @staticmethod
    def _paragraphs(lines: list[str]) -> list[str]:
        """A paragraph ends on a line that ends a sentence and falls short of the page's width."""
        if not lines:
            return []
        width = sorted(len(ln) for ln in lines)[int(len(lines) * 0.9)] if len(lines) > 3 else max(map(len, lines))
        out, buf = [], []
        for ln in lines:
            buf.append(ln)
            if ln[-1] in '.!?"\')' and len(ln) < 0.85 * width:
                out.append("\n".join(buf))
                buf = []
        if buf:
            out.append("\n".join(buf))
        return out


def _value(x) -> str:
    """An enum's value or the string itself."""
    return str(getattr(x, "value", x) or "")


_DIGITS = re.compile(r"\d+")
_PAGE_NO = re.compile(r"(\d{1,4}|[ivxlcdm]{1,7})", re.IGNORECASE)

PARSERS = {"docling": DoclingParser, "pdfium": PdfiumParser}


def parser_for(name: str = "pdfium") -> BookParser:
    try:
        return PARSERS[name]()
    except KeyError:
        raise ValueError(f"unknown book parser {name!r}; one of {', '.join(PARSERS)}") from None
