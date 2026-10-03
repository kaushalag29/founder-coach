---
status: accepted
---
# Books enter as private Chapters, parsed once (PDFium by default), positioned by page

PDF Books are a third Source type (ADR-0013). A Book is not a Document: each Chapter is, so
extraction, checkpoints, verification and Citations work at the same grain as a talk or an
article. Chapters are found from a manual `chapters:` list, then the PDF's outline (Parts opened into
their chapters), then the printed contents page matched against page openings, then the parser's
headings (bookmarks, numbering, font style), then fixed page windows; front and back matter are
skipped. PDFs that are encrypted, scanned or have a garbled text layer are refused. A Book is parsed once per file hash into a cached, text-only structure (paragraphs with
their page and Section; tables, figures and running headers dropped), and `clean` slices Chapters
from that cache.

A Chapter's position for each text unit is `page * 1000 + paragraph-on-page`: one increasing,
unique integer per unit as ADR-0013 requires, decodable to a page (`pos // 1000`) for the Locator
label ("p. 47", the printed page label when the PDF has one) and for one-page Moments.

Book Sources default to `distribute: false`: their items are indexed locally and used in the
maintainer's own coaching and evals, but `pack build` leaves them out unless asked
(`--include-private`), and `release.py` refuses a pack that contains private items. Summaries
and quotes of books the publishers sell are a different question from derived notes on free public
talks, and it is settled before any public release, not by default.

## Considered Options

- **The Book as one Document:** 60-100k words per Document breaks extraction windows, makes
  Citations coarse and makes one failed call redo a whole book.
- **pypdf / pypdfium2 / pdfplumber:** permissive but weak on reading order, running headers and
  headings. **PyMuPDF / PyMuPDF4LLM** (AGPL) and **Marker** (weights restricted above $5M revenue)
  have licence costs. **LiteParse** (Apache, model-free, very fast) stays the fallback behind the
  same parser interface.
- **Page number as the position:** several units share a page, which collides with Passage and
  Moment ids built from the position.
- **Git submodule for Docling:** it is a published MIT package; the `pdf` extra installs
  `docling-slim` with only the PDF parser and local layout models (torch is already in `index`).

## Consequences

`source_kind` gains `chapter` and `locator` gains `page` (given fields: no re-extraction). Search
caps results per Series as well as per Document, so one Book can't fill an answer. Books are never
OCRed and images never become Evidence (a model's description of a figure can't be verified).

## Amendment 2026-09-30: PDFium is the default parser; Docling is parked

A dry run on five owned books split all five correctly with the model-free PDFium parser (four from
bookmarks, one from its printed contents page), in about a second per book, with no model download and no
torch. Docling (layout models downloaded once from Hugging Face) is parked: its parser stays behind the
`BookParser` seam, unverified on real books, and is not installed by default. It is the first thing to try
for a PDF where PDFium's split or text is poor (multi-column pages, sidebars, tables).

## Amendment 2026-09-30 (P3): every source competes; privacy comes from the Source

- **Ranking is source-agnostic.** No boost, cap or penalty is keyed on a source kind. The planned
  "cap results per Series" (Consequences) is a candidate diversity rule for every Series alike
  (`PerSeriesCap` in `founder_coach.search`, eval config `full-series3`), adopted only if the Tuning
  set shows it doesn't lower nDCG@10 overall or for any source kind's questions.
- **Privacy comes from the Source's configuration** (`distribute: false`), read through
  `ytbrain.visibility.Visibility`, never from a Document's kind or id; unknown Documents count as private.
- **One benchmark, two homes.** Private Sources' Moments are pooled and graded like any other; their
  labels, and the questions written from Private Sources (`eval build --set dev-private`), go to the
  private overlay (`data/eval/private/`), scored with the released set and never released.
- **Dropped from P3:** a within-Book near-duplicate merge (one pair >= 0.95 among 1,203 book items;
  replaced by the source-agnostic `NearDuplicateCollapse` candidate), Section summaries (PDFium found no
  Sections inside chapters) and a Book summary (an unverified summary would break ADR-0004).
- Behaviour per kind lives in one `SourceKind` class each (`ytbrain/source_kinds.py`).

