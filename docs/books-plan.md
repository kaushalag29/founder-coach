# PDF Books: implementation plan (final, 2026-09-30)

Decisions: ADR-0014 (+ amendment: PDFium default, Docling parked); evidence:
docs/research/book-indexing.md; terms: CONTEXT.md (Book, Chapter, Section, Private Source).
Goal: owned PDF Books through the existing Steps with the fewest changes to what works, no model
download, and nothing shipped to others.

## Settled

| Topic | Decision |
|---|---|
| Unit | Book = one PDF; Document = one Chapter; Series = the Book's title; Speaker = its author(s) |
| Parser | PDFium (model-free, ~1 s per book) by default; `DoclingParser` parked behind the same seam in an opt-in `pdf-docling` extra |
| Chapters | manual list > bookmarks (Parts opened) > printed contents page > headings > 15-page windows; front/back matter skipped |
| Positions | `page * 1000 + paragraph-on-page`; Locator label "p. 47" (printed label if the PDF has one, else the PDF page) |
| Refused | encrypted, scanned, garbled text layer; never OCR, never DRM removal; a few garbled lines are dropped and reported |
| Privacy | Books are a Private Source (`distribute: false`, the only value in v1): not in the pack unless `--include-private`, and `release.py` refuses a pack with private items |
| Book evals | a `dev-private` question set (at most 20 % of the Chapters) in `data/eval/private/` (git-ignored); the committed talk/article set is the regression gate |

## The books (data/books/startup/, owned copies) and what must be found

The golden file `data/books/expected.yaml` (local, git-ignored) holds the full expectations; check C1 runs it.

| File | Title | Authors | Year | ISBN (ebook) | Chapters | Found by |
|---|---|---|---|---|---|---|
| No Rules Rules… | No Rules Rules | Reed Hastings, Erin Meyer | 2020 | 9781984877871 | 13 | bookmarks (4 Sections opened) |
| Play Bigger… | Play Bigger | Al Ramadan, Dave Peterson, Christopher Lochhead, Kevin Maney | 2016 | 9780062407627 | 11 | bookmarks (3 Parts opened) |
| The Hard Thing… | The Hard Thing About Hard Things | Ben Horowitz | 2014 | 9780062273215 | 10 | bookmarks |
| The Lean Startup… | The Lean Startup | Eric Ries | 2011 | 9780307887917 | 15 | printed contents page |
| Zero to One… | Zero to One | Peter Thiel, Blake Masters (sources.yaml) | 2014 | 9780753550304 | 16 | bookmarks |

None has printed page labels (Citations use PDF pages). *The Lean Startup* has a garbled line on 171 pages.

## Metadata: where each given field comes from (first match wins; never the LLM)

1. `sources.yaml` (per file).
2. The copyright page, model-free (`books/metadata.py`): the page among the first 15 and last 8 with an
   ISBN. **ISBN:** the one labelled ebook/epub/eISBN, else the first there. **Year:** from that page's
   "Copyright © …" line; the year may follow the name ("© Peter Thiel 2014"); copyright lines on other
   pages (song and quote permissions) are ignored.
3. The PDF's metadata: **title** (a download site's suffix stripped; text after the first colon becomes
   the **subtitle**), **authors** ("Horowitz, Ben" -> "Ben Horowitz"; "A & B & C" -> three authors).
4. Open Library by ISBN, only with `lookup: openlibrary`: fills fields still empty; one cached call per book.

`sync` and `inspect` flag anything missing or suspect (junk title, no author, no year, no ISBN).

## Code plan

### P1 — Books become Documents (no LLM, no cost)

New:
- `books/metadata.py`: `BookMeta` (title, subtitle, authors, year, isbn, publisher, url) and
  `resolve(facts, overrides, lookup=None)`; `copyright_page`, `pick_isbn`, `copyright_year`,
  `split_title`, `split_authors` as pure functions. The probe keeps the copyright page's lines.
- `books/adapter.py`: `BookAdapter` (ADR-0013 `Adapter`, `type = "pdf_books"`, `doc_type = "book"`):
  - `sync`: every PDF of every enabled Source -> probe -> parse (cached) -> metadata -> chapters ->
    one manifest Document per Chapter (`doc_id = <book_id>__<chapter-slug>`, `book_id` = ISBN-13 or title
    slug), plus a per-book line: chapters, method, skipped, warnings. A PDF no longer present: its
    Chapters are tombstoned. A refused or failing PDF: reported, the others continue.
  - `clean`: one Chapter -> the shared transcript (`utterances` = paragraphs with `start_ms = page*1000+n`,
    `chapters` = its Sections when headings exist, `source_kind: chapter`, `locator: page`, series, speaker,
    year, isbn, `private: true`, page labels). Unchanged text (same hash) is not cleaned again.
- `books/overrides.py` (or in metadata): the per-book `sources.yaml` block, including a partial
  `chapters:` list that renames or moves only the entries it names.

Changed:
- `sources.py`: type `pdf_books` (`path`, `distribute`, `lookup`, `books:` per file), `DOC_TYPE`, `adapter()`.
- `cli.py`: `sync` dispatches `pdf_books` like `website`; `books inspect --expect FILE` compares with
  the golden file (exit 1 on a mismatch).
- `pyproject.toml`: `pdf = ["pypdfium2>=4.30"]`, `pdf-docling = [docling-slim...]`.
- `sources.example.yaml`: a commented `pdf_books` entry.

### P2 — Through the existing pipeline (pilot first) — code done 2026-09-30, pilot pending

- `extract/schema.py`: `source_kind` + `chapter`, `locator` + `page` (given fields; no SCHEMA_VERSION bump).
- `extract/prompts.py`: chapter wording ("a chapter of the book <title> by <authors>"), like the article one.
- `locators.py`: `page` label ("p. 47") and link (the Book's `url`, else none).
- `knowledge/items.py`: a Chapter's items get a Book header (`From the book "<Book>" by <authors> (<year>),
  chapter "<Chapter>" — <Section>`); talk and article headers unchanged. (The "at 00:00" header was the unused
  legacy `index.build_context_header`; it now uses `locators.label`.)
- `eval/moments.py`: one-page Moments for Chapters (`<doc>_b<page>`).
- Pilot: `extract --doc 9780753550304` -> `verify --doc …` on *Zero to One* only; read 10 records
  (`ytbrain sample --doc 9780753550304`); then the other four; then `index`.

### P2b — Text repair at parse time (2026-09-30)

- `books/ligatures.py`: damaged ligatures ("di@erent", "signi%cant", "pro1t") repaired only when the result is a
  word of the Book itself or of `books/ligature_words.txt` (from `scripts/ligature_words.py`); control characters
  between letters become hyphens. Applied to paragraphs, outline titles and contents entries in `assemble()`;
  `ParsedBook.repaired` and `books inspect` report it; parse cache format 3.
- Real data: *The Lean Startup* 603 words repaired, 9-11 unresolved (joined words such as "systematically8guring");
  the other four books: no ligature repairs. Every Chapter transcript changes (hyphens), so re-run
  `sync --type book && clean`, then `extract`, `verify`, `index`.

### P2 results (2026-09-30, on the Mac)

- Extract + verify: 65/65 Chapters; 2 flagged (*A Circle of Feedback* 17/22: the model's paraphrases, rejected
  as it should be; *Start: How to Discover a Category* 0 items twice: queued for `invalidate extract
  --only-flagged`). After the ligature fix *Join the Movement* verifies 16/16. Index: +2,349 items (35,975).
- Books vs talks on the dev set (labels v1.3.0, before any Book label existed), `full`: nDCG@10 0.5684 ->
  0.5629 (PASS). (The "talk nDCG@10" quoted then, 0.5233 -> 0.5211, was the old Document-level metric that
  never counted an Article or a Chapter; P3 replaced it with `doc_ndcg@10`.) Pre-books run:
  `dev-full-2026-09-27T152650.run`; its old baseline file is kept as `baseline-dev-full-prebooks-labels-v1.3.0.json`.
- Chapters in the top-10 mix: `full` 1.4 % (17 of 150 questions have one), `full-no-passages` 1.7 %, pack 2.6 %
  (24 questions). All 150 released questions are written from Talks.
- Pack gap to `full` (labels v1.4.0): `full` 0.565, `full-no-passages` 0.516, pack 0.464 -- about half the gap
  is Passages, half the smaller models. The jina reranker lowers the pack again (0.438 vs 0.464, same labels):
  it stays off (ADR-0009). Per-config baselines saved for `full` and `pack`.
- **Privacy slip, fixed in P3:** `eval judge --config pack` over the private pack released 48 labels on Book pages
  (ids/titles/pages) in labels v1.4.0. Don't commit `eval/` until step 1 of "Next" has moved them to the overlay.

### P3 — Every source competes; one benchmark (code done 2026-09-30)

Settled with the maintainer: nothing is Book-only. Ranking is source-agnostic, the benchmark covers every
question and every Source, and the design stays extensible (ADR-0014, amendment 2026-09-30).

- **`SourceKind` registry** (`ytbrain/source_kinds.py`): `Talk`, `Article`, `BookChapter` own their Locator
  labels and links, Moment ids, text and spans; locators, moments, the eval files and the runner dispatch to them.
- **`Visibility`** (`ytbrain/visibility.py`): private = the Source says `distribute: false`; unknown Documents
  count as private. Replaces the Book-page guard.
- **Private overlay** (`data/eval/private/`): private Moments are pooled and graded; their labels, and the
  questions written from private Sources (`eval build --set dev-private`, at most 20 % of the private Chapters), are scored with the released
  set and never released. The 48 Book labels graded on 2026-09-29 move there on the next `eval judge`.
- **Per-kind measurement and gate:** nDCG@10 by the seed's source kind; a kind with >= 10 questions fails when
  it drops > 0.03 significantly. `doc_ndcg@10` replaces `talk_ndcg@10` (which never counted an Article or a
  Chapter as a hit). `eval rescore --run FILE --as NAME` makes any saved run a named baseline.
- **`DiversityPolicy`** (`founder_coach/search.py`): today's rules (one per quote, 3 per Document) plus two
  source-agnostic candidates measured as eval configs: `PerSeriesCap(3 of 10)` (`full-series3`) and
  `NearDuplicateCollapse(0.8)` (`full-neardup`). Neither is the default until it passes both gates.
- Dropped: the within-Book near-duplicate merge (1 pair >= 0.95 among 1,203 book items), Section summaries
  (0 Sections found) and a Book summary (unverified, ADR-0004).
- Gap found: all 150 released dev questions are written from Talks, so Articles have no questions of their
  own yet (see "Next").

### More books (2026-09-30)

Six more: *Small Giants*, *Financial Intelligence for HR Professionals*, *The High Performance Entrepreneur*
(startup/) and *Good to Great*, *Leaders Eat Last*, *The Ride of a Lifetime* (leadership/). One Source lists both
folders. Fixes they needed, all generic: grouping bookmark levels opened (Leaders Eat Last: 27 chapters),
Photo Insert / Resources skipped, the damaged-word warning scoped to chapters; corrections in sources.yaml
(Financial Intelligence: year, ebook ISBN, authors; the High Performance Entrepreneur's title; subtitles).
11/11 match `data/books/expected.yaml`.

### Results with eleven books (2026-09-30)

- Index: 116 Documents re-indexed, 39,596 items. Talk questions vs the pre-books run (labels v1.5.0): nDCG@10
  +0.002 (p=0.45), recall@50 -0.017 (book Passages push some talk Moments out of ranks 11-50); Chapters are
  2.9 % of the top 10. `dev-private`: 21 of 33 book questions accepted ($0.26).

### P3b — One question set per source kind (code done 2026-09-30)

- Tuning splits `dev` (Talks, 150), `dev-articles` (public Articles, at most 20 % of them, released) and `dev-private` (private
  Sources, at most 20 % of their Chapters, overlay only), each seeded from its own kind; `eval run|judge|rescore --set all` (default)
  scores them together, one nDCG row per kind. `eval judge` grades every split in the run (it graded only
  `dev`, so private questions' new Moments were never graded). Runs record the questions they were asked:
  questions added after a baseline are left out of the comparison, never scored zero.
- Coach eval G2 asks questions from every split (books only when the plugin carries them) and checks
  Citations without a judge: a link no search returned, or a book page no hit had, is an unsupported claim.
- Passages: a unit longer than the 1024-token window is cut into the fewest pieces that fit (never smaller);
  a short chunk is never carried whole into the next (duplicate ids). ~80 Documents re-index once.

### Ops and growing question sets (code done 2026-09-30)

`ytbrain ops` runs ingest -> eval -> plugin in one resumable command, only what changed (docs/ops.md).
Question sets hold at most 20% of their Documents: talks 150 (kept), articles 72, private 36 today;
`eval build --top-up` fills them. Books: `path: data/books`, every subfolder found by itself.

### Next (runs on the Mac)

1. Before books are compared again, freeze the pre-books reference once (free):
   `eval rescore --config full --run dev-full-2026-09-27T152650.run --as full-prebooks --save-baseline`.
2. `ytbrain ops --dry-run`, then `ytbrain ops` (re-indexes ~80 Documents for the Passage split, tops up the
   article and private questions, runs, judges, gates; builds both plugins and runs the coach eval on a PASS).
3. Bump `plugin/.claude-plugin/plugin.json` before installing `dist/founder-coach-<version>-private.plugin`.

## Tests

Offline suite `tests/test_books.py` (CI), on synthetic text and the fixture PDFs only; the real books never
enter git or CI.

| # | Covers | Test |
|---|---|---|
| M1 | ISBN: ebook-labelled wins over hardcover/paperback; unlabelled -> first on the copyright page; ISBN-10 and hyphens normalised | `test_isbn_prefers_the_ebook_edition` |
| M2 | Year: "© 2014 by X", "© X 2014", "Copyright © 2020 by Netflix, Inc."; permission pages ignored | `test_year_comes_from_the_copyright_page_only` |
| M3 | Title/subtitle split; site suffix stripped; junk titles flagged | `test_title_and_subtitle` |
| M4 | Authors: "Last, First", "A & B & C", "A, B and C"; a single name kept | `test_authors_are_split_and_ordered` |
| M5 | Precedence: sources.yaml > copyright page > PDF metadata > Open Library (fake, fills blanks only, one call, cached) | `test_metadata_precedence` |
| M6 | Missing fields are flagged, not guessed | `test_missing_metadata_is_flagged` |
| S1 | `pdf_books` Source: folder, one file, per-book overrides, bad config rejected | `test_pdf_books_source_config` |
| S2 | sync registers one Document per Chapter with stable ids; re-sync is a no-op; a removed PDF tombstones its Chapters; a refused PDF doesn't stop the others | `test_book_sync_*` |
| S3 | clean writes the shared transcript (positions, locator, Sections, private flag); unchanged Chapters are skipped | `test_book_clean_*` |
| S4 | A partial `chapters:` override renames one entry and keeps the rest | `test_partial_chapter_override` |
| P1..P13 | The P0 criteria below (parsing, chapters, refusals, cache) | existing |

Local check C1 (not in CI; the books stay on your Mac):
`ytbrain books inspect data/books --expect data/books/expected.yaml` must report every field and chapter
count as expected for all five books. It is the acceptance test for metadata and chapter detection, and the
template for every book added later: add a file, add its expected block, run the check.

## P0 criteria (`ytbrain books inspect`, done)

| # | Path | Criterion | Test (`tests/test_books.py`) |
|---|---|---|---|
| A1 | happy | Bookmarked PDF: chapters by outline, front/back matter skipped, printed page labels used | `test_outline_wins_...`, `test_inspect_reports_the_fixture_book_...` |
| A2 | happy | Parts/Sections open into their chapters and bound them | `test_parts_in_the_outline_...`, `test_outline_parts_and_sections_open_...` |
| A3 | happy | No bookmarks: the printed contents page finds the chapters | `test_a_printed_contents_page_finds_chapters_...` |
| A4 | happy | No outline or contents: headings, then page windows with a warning | `test_headings_then_windows_...` |
| A5 | happy | Manual list with printed labels; two chapters on one page | `test_a_manual_list_uses_printed_page_labels_...` |
| A6 | happy | Model-free parser drops running headers/page numbers and splits the fixture | `test_pdfium_parser_drops_running_headers_...` |
| A7 | happy | A book is parsed once per file hash and parser; a changed file or damaged cache re-parses | `test_the_cache_follows_the_file_hash_...` |
| F1 | failure | Encrypted, scanned, garbled, non-PDF, missing file: refused with a reason, exit 1 | `test_probe_reads_outline_...`, `test_one_failing_book_...`, `test_cli_books_inspect_...` |
| F2 | failure | A parser error on one book is reported; the other books still run | `test_one_failing_book_is_reported_...` |
| F3 | failure | Docling partial success (timeout, failed pages) is a failure, never half a book | `test_docling_items_map_to_blocks_...` |
| F4 | failure | Missing `pdf` extra: clear message, exit 2 | `test_cli_stops_with_a_clear_message_...` |
| F5 | failure | Implausible split (one chapter holds most of the book) is rejected | `test_a_split_that_puts_most_...` |
| R1 | regression | A short title mentioned in body text never starts a chapter | `test_a_short_title_inside_body_text_...` |

## Phases and gates

| Phase | Done when |
|---|---|
| P0 (done) | `books inspect`; 24 offline tests; dry-run splits all five books |
| P1 (done 2026-09-30) | M1-M6 and S1-S4 pass (39 tests); C1: 5/5 books match; `sync --type book` + `clean` produced 65 Chapter transcripts (dry run on a local copy of the data); `extract` holds Chapters until P2 |
| P2 (code done 2026-09-30; pilot pending on the Mac) | Code: chapter prompt, page Locator, Book header, one-page Moments, `--doc`, private pack/release/eval guard; 51 book tests, 261 in all (incl. the ligature/hyphen repair, `ytbrain/books/ligatures.py`; *The Lean Startup* needed it, ~600 damaged words). Still to do: pilot book >= 95 % of Evidence quotes verified, 10 sampled records read and accepted; then all five indexed and searchable |
| P3 | Series cap, dedup, Book summary in place; book evals baselined; regression gate holds |

Parked: Docling (hard PDFs), OCR (scanned books), EPUB input, tables and figure text.
