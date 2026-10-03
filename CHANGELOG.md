# Changelog

User-visible changes, newest first. Dates are when the change landed; plugin releases are
tagged `founder-coach--v<version>` in the marketplace repo ([docs/release.md](docs/release.md)).

## 2026-10-01 (`ytbrain ops` runs unattended: stop reasons, retries, notices, versions, references)

- **CI fix, second one:** `test_ac3_raising_depth_reopens_the_old_frontier` used a month page of bare links, which
  trafilatura 2.3.0 (what CI installs fresh) reads as 0 words and 2.2.0 as 2; at 0 words an unrenderable page is
  `failed` and retried each run, so the "finished pages wait for their re-check" assertion broke on CI only. The
  page now has a heading, a listing under every release. The local-server CLI test also uses a fresh article text
  each run, so a reused data root no longer skips it as a duplicate.
- **CI fix, and the gap behind it:** `ops` stopped on a missing `sources.yaml` (the CLI's exit is a `SystemExit`, which
  `except Exception` doesn't catch), a test that passed only where `sources.yaml` existed. Every suite now points
  `YTBRAIN_SOURCES_FILE` at a missing file before importing `ytbrain`, so it fails the same on your machine as on CI,
  however it is run (`python tests/test_x.py`, `pytest`, `scripts/check.sh`); a guard test checks it.
- **No more waiting on the last call:** an eval step (questions, rewrites, grading) that had one call hanging at the
  provider sat at `100/101` for up to 7 minutes until the deadline. A call slower than 5x the median (at least 30 s)
  is now asked again in parallel and the first answer wins; the deadline stays as the last resort.
  Not while the provider is rate-limiting us (429s): then the slowness is the backoff, and a repeat only adds load.
- **Stop reasons:** commands that stop early write why (`data/ops/last-stop.json`); `ops` retries network
  stops twice (1 and 5 min), goes on after a sync that keeps failing, stops with the fix for a refused
  endpoint or the spend cap, and leaves the coach eval pending (re-run alone) on a Claude plan limit.
- **Notices:** a macOS notification and `data/ops/last-run.md` on every stop and finished run (`--no-notify`).
- **Plugin version:** a new build asks patch / minor / skip (patch after 60 s or with no terminal; a version
  set by hand is kept; `--version` answers up front).
- **References:** before the gate moves the baseline it is kept as `full-<date>` (last 10), and as
  `full-before-<kind>` the first time a kind of Source is indexed.

## 2026-09-30 (`ytbrain ops`; question sets that grow with the Sources; book folders found by themselves)

- **`ytbrain ops [all|ingest|eval|plugin]`** (and `ops/*.sh`): the whole loop in one resumable command
  (docs/ops.md). Checkpoints in `data/ops/state.json`; fingerprints of the index, labels and plugin decide what
  runs; a FAIL or INCONCLUSIVE gate stops before the plugin and keeps the baseline; `--max-cost` caps the run's
  eval and coach-judge spend; flagged extractions are retried once each; `--dry-run` shows what is due.
- **Question sets sized by their Sources:** each split holds at most 20% of its Documents (no minimum), never
  fewer than it has. `eval build --top-up` writes the missing ones (new Documents first);
  `eval build` without `--set` builds every split; `eval status` shows targets. Questions whose seed Document
  was removed retire (and labels on removed Documents go) at the next build or judge.
- **Splits come from the source-kind registry:** `dev` (talks), `dev-articles`, `dev-chapters` (public books),
  plus `dev-private`; a new source kind gets its own split without eval changes.
- **`eval rescore --refresh-baseline`** re-scores a saved baseline on the current labels, keeping its questions.
- **Books:** `path: data/books` picks up every subfolder you add (startup/, leadership/, finance/ ...); the
  caches (parsed/, plans/, openlibrary/) and hidden or `_` folders are skipped.

## 2026-09-30 (Eval: one question set per source kind; fair baselines; source-neutral wording)

- **Separate Tuning sets per kind of Source:** `dev` (Talks, 150), `dev-articles` (public Articles, 30,
  released), `dev-private` (your private Sources such as Books, 30, overlay only). Each is built with
  `eval build --set <split>`, seeded only from its kind, and graded over every Source.
  `eval run|judge|rescore` default to `--set all`: every set in one run, one nDCG row per source kind.
  Private questions written by the previous release under `dev` move to `dev-private` at the next judge.
- **`eval judge` grades every set in the run.** It used to grade only `dev` questions: a private
  question's newly pooled Moments were never graded and the next judge stopped on them.
- **Baselines compare like with like:** a run records the questions it was asked; questions added after
  a baseline are left out of the comparison (and listed), not scored zero, and differences are paired.
- **Question generation knows the source:** the prompt says talk, article or book; an article or book
  seed links to its page, not to a YouTube URL. The build's closing lines report the set it built.
- **Wording:** `extract`/`verify`/`clean` counts no longer say "transcript"/"talk(s)" for every kind;
  `ytbrain sample` stratifies by source kind and tells the reviewer how to check each one.
- **Tests:** all suites also run together in one process (`check.sh`), which caught two leaks between them.
- **Coach eval checks Citations for every source kind:** G2 asks questions from each Tuning set (talks 70%,
  articles 15%, your books 15% when the plugin carries them); a link no search returned, or a book page no
  book hit had, counts as an unsupported claim without a judge; cited book hits reach the judges first;
  the judge prompts and the `ask` template name talks, articles and books.
- **Passages fit the 1024-token embedding window:** an article or book paragraph longer than it is cut into
  the fewest equal pieces that fit (same position, so Citations and eval Moments are unchanged); a short
  chunk is never carried whole into the next one (two Passages had one id). Only the ~80 affected
  Documents re-index on the next `ytbrain index`.

## 2026-09-30 (Books: several folders per Source, better chapter splits, a retry before verify flags)

- **Resume and next-step hints name the real command.** An interrupted run now prints the command as typed (`ytbrain eval build --set dev-private`, not a bare `eval build`); a finished `dev-private` build says to run `eval run --set dev --config full` and then `eval judge`; a `dev` build says when private Sources still have no questions; a stale baseline names the `eval rescore --run … --as …` that refreshes it.
- **`path` may list several folders or files** for one `pdf_books` Source (`path: [data/books/startup,
  data/books/leadership]`). Per-book corrections stay keyed by file name, so two PDFs with one name are refused.
- **Nested bookmarks:** a bookmark level that only groups Parts or numbered chapters is opened ("THE FORCE" >
  "PART 1" > "1. Protection from Above"): *Leaders Eat Last* splits into 27 chapters, not 4 of up to 31k words.
  Chapter levels whose children are sections are never skipped.
- **More back matter skipped:** Resources ("Additional Resources"), Photo Insert, Photographs, Illustrations,
  Plates. The "damaged words" warning counts only Chapter text (not Notes or Index links).
- **Self-check retry at the verify threshold:** extraction retries once with feedback whenever verification
  would flag the record (< 90 % of quotes grounded; it was < 60 %), keeping the better attempt; a misquote reused
  by several items is listed once. Thresholds live in `config.VERIFY_PASS_RATE` / `VERIFY_FAIL_RATE`.
- 6 new books checked (11/11 match `data/books/expected.yaml`). 4 new offline tests (272 in all).

## 2026-09-30 (P3: every source competes; one benchmark across all Sources)

- **Fix (privacy):** `eval judge --config pack` over a pack built with `--include-private` had released 48
  labels on Book pages (ids, titles, pages; no text) in labels v1.4.0. Privacy now comes from the Source
  (`distribute: false`, `ytbrain/visibility.py`; unknown Documents count as private): `files.write_split`
  refuses a label on a private Document, and those labels go to the **private overlay** instead
  (`data/eval/private/`, never released). Re-running `eval judge` ($0) moves the 48 there.
- **One benchmark:** private Moments are pooled and graded like any other; `eval run`/`rescore` score the
  released set plus the overlay. `eval build --set dev-private` writes 30 questions from your private Sources
  (your Books), graded over every Source, overlay only.
- **Every source kind measured and gated:** nDCG@10 by `seed_kind`; a kind with >= 10 questions fails the run
  when it drops > 0.03 significantly. `top-10 mix by source kind` is shown. **Fix:** `doc_ndcg@10` replaces
  `talk_ndcg@10`, which cut Document ids at 11 characters and so never counted an Article or a Chapter as a
  hit (Document-level nDCG@10 of `full` is 0.643, not 0.519).
- **`eval rescore --run FILE --as NAME`:** any saved run becomes a named baseline (e.g. the pre-books run).
- **`DiversityPolicy`** (`founder_coach/search.py`): composable, source-agnostic rules; the default is
  unchanged. Candidates `PerSeriesCap` and `NearDuplicateCollapse` run as eval configs `full-series3`,
  `full-neardup`, `pack-series3`, `pack-neardup`.
- **`SourceKind` registry** (`ytbrain/source_kinds.py`): Talk, Article and Book Chapter own their Locator and
  Moment rules; locators, moments, the eval files and the runner dispatch to them (behaviour unchanged).
- **Private plugin builds:** `assemble_plugin.py` warns when the pack holds private items and names the zip
  `<id>-<version>-private.plugin`.
- Dropped from the books plan: the Book-only Series cap, the within-Book near-duplicate merge, Section and
  Book summaries (ADR-0014 amendment).
- 7 new offline tests (268 in all).

## 2026-09-30 (PDF Books: damaged ligatures and hyphens repaired when a PDF is parsed)

- **Ligature repair:** some PDF fonts lose their ligatures (ff, fi, fl, ffi, ffl) and the text layer holds a
  stand-in ("di@erent", "e&orts", "signi%cant", "pro1t", "in3uenced", "diKcult"), which verification could never
  match. `ytbrain/books/ligatures.py` tries each stray symbol, digit or capital inside a word as every ligature
  and accepts a result only if it is a word the language really has (the Book's own correctly spelled words, plus
  `ytbrain/books/ligature_words.txt`, built from your transcripts by `scripts/ligature_words.py`). Brands,
  ordinals, "R&D" and URLs are never touched; anything no ligature explains is left as printed and reported.
  *The Lean Startup*: 603 damaged words repaired (310 distinct), 9-11 left (joined words like
  "systematically8guring"); the other four books needed none.
- **Hyphens:** a control character between letters is a hyphen in some fonts ("one\x02time"); it is now "-".
  This changes the text of all five Books slightly.
- `books inspect` shows the repairs and warns about the words it could not repair; the parse cache format is 3,
  so cached parses are redone. After `sync --type book && clean`, Chapter transcripts change, so `extract`
  treats them as new: re-run `extract`, `verify`, `index`. 8 new offline tests (261 in all).

## 2026-09-30 (PDF Books, P2: Chapters through extract, verify and index)

- **Chapter prompt:** book Chapters extract with the talk prompt reworded for a book (one source of truth, as for
  articles): "Chapter / Book … by <authors> / Sections", `[p. n]` page markers, the authors given (never
  generated), and a rule to keep the authors' own advice rather than lines they quote. No chaptering call: a
  Chapter's headings are its Sections. `extract`, `verify` and `sample` take `--doc PREFIX`, so
  `extract --doc <ISBN>` pilots one Book. The P1 hold on book Chapters is gone.
- **Page Citations:** `locator: page` reads `p. 47` (printed page) or `PDF p. 47`, with no link for a private
  PDF; pages render it bare, the coach returns `source_kind: chapter` with `page` and the book, and the citation
  guide shows the format. Moments for Chapters are one PDF page (`<doc>_b<page>`).
- **Book header:** a Chapter's items are indexed as `From the book "<Book>" by <authors> (<year>), chapter
  "<Chapter>" — <Section>`; talks and articles keep theirs (the unused legacy chunk header now uses the Locator
  label, so an Article no longer reads "at 00:00").
- **Private stays private (moved up from P3):** Knowledge items carry `visibility`; `pack build` leaves private
  items out unless `--include-private`, `pack.json` counts them, `scripts/release.py` refuses such a pack, and
  the released eval never seeds from or labels them. An index built before the column reads as public.
- 7 new offline tests (253 in all; the P1 extract-hold test is gone), incl. a Chapter end to end through extract (fake LLM), verify, pages, items
  and Moments.

## 2026-09-30 (PDF Books, P1: Chapters become Documents)

- **`type: pdf_books` Sources:** `ytbrain sync --type book` registers one Document per Chapter
  (`<ISBN-13>__<chapter-title>`), `ytbrain clean` writes their transcripts (paragraphs at `page * 1000 + n`,
  `source_kind: chapter`, `locator: page`, `private: true`, the Book's given fields). Unchanged Chapters aren't
  cleaned again; a removed book leaves the index; a refused or failing PDF never stops the others.
- **Metadata, never from the LLM:** sources.yaml, then the copyright page (ebook ISBN, the book's own copyright
  year), then the PDF's metadata (title/subtitle, "Last, First" and "A & B" authors), then Open Library by ISBN
  when asked. Missing or suspect fields are flagged (e.g. a copyright-line author missing from the authors).
- **`books inspect --expect FILE`** checks metadata and chapters against expected values; the five owned books
  match 5/5. Per-file `chapters:` corrections rename, add or drop single chapter starts.
- **PDFium is the default parser; Docling is parked** (`pdf-docling` extra), so books need no model download.
- `extract` holds book Chapters until P2 (chapter prompt, page Citations). 15 new offline tests (39 in the suite).

## 2026-09-29 (PDF Books, first slice)

- **`ytbrain books inspect <pdf-or-folder>`** shows what the pipeline would make of a PDF Book before any
  paid extraction: refused or not (encrypted, scanned), parse time, what was dropped (running headers and
  footers, tables, figures, captions, footnotes), which chapter-detection method won and why the others
  didn't, and each Chapter's printed pages and length. New `ytbrain/books/` package (probe, normalize, parse,
  chapters, inspect) behind a `BookParser` seam; Docling via the new `pdf` extra; ADR-0014,
  `docs/books-plan.md`, `docs/research/book-indexing.md`; glossary: Book, Chapter, Section. A seventh
  offline suite, `tests/test_books.py` (24 tests), runs on tiny fixture PDFs with a fake parser (`pypdfium2` joins `dev`).
- **Chapter detection** tries a manual list, the bookmarks (Parts and Sections opened into their chapters), the
  printed contents page (entries matched to page openings, in order), the parser's headings, then page windows.
- **`--parser pdfium`**: a model-free parser (about a second per book) for dry-runs and well-bookmarked PDFs.
- **Refusals and warnings:** encrypted, scanned and garbled text layers (fonts without a text map) are refused;
  a few garbled lines are dropped and reported. A Docling timeout or partial parse fails the book, never half of
  it; one failing book never stops the others. `YTBRAIN_BOOK_TIMEOUT_S`, `YTBRAIN_BOOK_THREADS`.
- **Dry-run on five books:** all five split correctly with the model-free parser.


- **`assemble_plugin.py --zip`** writes `dist/<id>-<version>.plugin`, the file Cowork's Plugins page uploads. The
  README gained "Use the coach": installing uv and Claude Code, the Claude Code and Cowork steps, first run.
  The setup skill no longer asks the Founder to run the model download (the server does it in the background).

- **`sh scripts/check.sh` runs the CI checks locally before a push:** secret scan, the six suites (a skipped
  test fails), generated files fresh, and `claude plugin validate`. Offline, free, no `.env`.
  `docs/release.md` shows how to run it as a git pre-commit hook (`--no-verify` skips it). CI actions moved to current versions
  (`checkout@v7`, `setup-node@v7`, `setup-uv@v9.0.0`).

- **One `.env` configures founder-coach too.** `founder-coach` reads its `FOUNDER_COACH_*` lines
  (never other keys) from:
  1. `$FOUNDER_COACH_ENV_FILE`;
  2. `./.env`, the folder Claude Code was started in (the repo during development);
  3. `~/.founder-coach/.env`, a Founder's own settings.

  The environment and `--home` still win, and `founder-coach status` names the files it used.
  `claude plugin eval` passes only `EVAL_*` variables into its sandbox, so `ytbrain claude` hands
  it the repo's `.env` as `EVAL_FOUNDER_COACH_ENV_FILE`; there, `HOME`, `MODELS` and `PACK` are
  skipped so the eval keeps its own data folder. `YTBRAIN_DOTENV=0` (the tests) or
  `FOUNDER_COACH_DOTENV=0` turns the files off.
- **Results on Haiku (build 20260928T040223Z):**
  - G2: 100 % of cited claims supported (20 cases).
  - G4: 9 of 10 weak plans challenged with a Citation.
  - G6: 7 of 7 multi-part questions answered part by part.
  - G5: 1 of 3 personas. The Check-in fixes below came after this run.
- **Smaller fixes:**
  - `founder-coach warmup` accepts `--home` like every other command.
  - The `pack build --rerank-model` help names the real default (`none`).
  - The wheel includes `ytbrain/eval/coach_cases/`, which `eval coach` needs.
  - After `eval judge` releases new labels, it suggests `eval rescore`, which doesn't search
    again, instead of `eval run`.
  - The setup skill says the model download is about 0.2 GB.
  - The Citation example uses the link form the tools return.
  - `forget` shows the store path and names the word to type when no company is saved.
  - `weekly-focus` passes each Commitment's Citations.
- **Docs brought up to date:**
  - The README, commands, eval spec, web plan, M3 status (with a new "Next, in order"), AGENTS,
    CONTEXT and SECURITY docs.
  - An amendment to ADR-0009 (the pack ships in the plugin, `--with-passages` is beta-only, there
    is no reranker, and the G1 numbers).
  - `.env.example` lists every setting with its default and whether it's required.
- Suites: core 72, eval 35, pack 11, coach 40, plugin 21, web 27.

- **The coach answers first:** it searches, judges and cites in its first reply, and asks at most one
  clarifying question after that answer. It also no longer holds a question back for setup.
  - In the first `eval coach` run on Haiku, 8 of 10 G4 plans were challenged without a single
    `coach_search`, so they had no Citation (20%).
  - Two G6 answers only asked the Founder to run setup first.
  - Two G5 personas never got their Commitments or Check-in recorded, because the coach kept
    asking questions instead of proposing.
  - The coaching contract changed in both the MCP instructions and the `coach` skill:
    - Rule 1: the restatement and the judgment come in one reply.
    - Rule 4: search before advising and back the position with a retrieved Citation.
    - Rule 6: answer for the stated default, then ask.
  - The setup Nudge and the SessionStart hook now say "answer first, then offer setup once".
  - `ask`, `weekly-focus` and `check-in` follow the same rule: a missing Goal is a stated
    assumption, and a Check-in that already says what happened gets every status proposed at once.
- **Check-ins finish the job:** the second G5 run (1 of 3 personas) recorded Check-ins while last
  week's Commitments stayed open, skipped recording one Check-in entirely, and turned a dropped
  Commitment's reason into a second Decision.
  - `coach_record` of a Check-in now warns with the ids of Commitments from earlier weeks that are
    still open, so the coach sets each one's status.
  - The `check-in` skill triggers when a Founder reports how the week went, calls a Check-in done
    only when both steps are, and keeps a drop's reason in its note.
  - The `coach` skill routes week reports to `check-in` and focus questions to `weekly-focus`.
- **`eval coach` stops if `dist/plugin` is reassembled mid-run** rather than recording the new
  build's answers under the old build's id.
- **The coach answers in fewer turns:** the `coach` and `ask` skills set a search budget. Independent
  searches go out together in one message, with at most one follow-up per part, and then whatever
  is still missing is named as a Gap. Most questions stay one part. On the first
  `claude plugin eval` run, Haiku re-searched until it ran out of turns (6 searches, no answer), so
  a weak plan got no pushback at all. Every extra call is also a turn the Founder waits through and
  tokens on their plan.
- **The Citation format is inline in the `coach` skill**, so an answer no longer needs a `Read` of
  `references/citations.md` (a turn, and denied in `claude plugin eval`, whose cases allow only Skill).
  In the second plugin eval run, cases spent 9-11 turns for 2 coach calls and one weak-plan answer
  cited without a link.
- **`challenges-a-weak-plan` mocks a fundraising-shaped search result** rather than the suite's
  generic pricing one, which invited re-searching.

## 2026-09-27 (backlog, cheaper coach eval)

- **`ytbrain eval coach` spends far less of a Claude plan's limits:** cases run on `--model haiku` by
  default (`--model sonnet` to sign a release off; `YTBRAIN_COACH_MODEL` changes the default;
  results are kept per model), at most `--max-turns 12` per call, and in a scratch folder outside the
  repo. From the repo, every case loaded its CLAUDE.md/AGENTS.md (thousands of tokens, and a coach
  that saw developer notes): all gates' harness versions are bumped. Each case records the host's
  tokens, and each gate prints its total.
- **Plugin development runs the local Claude Code on the Claude plan, token-saving by default:**
  `ytbrain eval coach` (default `--host claude`, Haiku) and the new `ytbrain claude -- <args>` (Haiku via
  `ANTHROPIC_MODEL`; `--model sonnet` to change) for live `--plugin-dir dist/plugin` sessions and
  `plugin eval`. OpenRouter is opt-in (`--host openrouter`, `ytbrain claude --openrouter`). README:
  "Claude Code for plugin development" and "What's in git".
- **Optional `ytbrain eval coach --host openrouter`** runs the host (Claude Code) through OpenRouter's
  Anthropic-compatible API, paid per token and counted in `--max-cost`, instead of on the Claude plan
  (key: `YTBRAIN_COACH_HOST_KEY`, or the OpenRouter `YTBRAIN_LLM_API_KEY`; `--model` is an OpenRouter
  id, default `anthropic/claude-haiku-4.5`). Results are cached apart per host and model.
- **`eval judge` / `eval run --config pack` say when they're stale:** a saved run older than the last
  `index`, or a pack built before it, is named with the command that refreshes it (judging an old run
  pools nothing new: it grades what that run retrieved).
- **Usage log follow-ups** (docs/usage-log.md D8-D11): `/setup` tells the Founder the log exists and how
  to turn it off; `/feedback` offers the usage export next to the Feedback export (also in the
  marketplace README); search text stays maintainer-only. New template placeholder `{{env_prefix}}`
  (the runtime's settings prefix), so skills never spell the product id.
- **Verifier keeps parenthetical prose.** It dropped every `( ... )` as a caption annotation, which
  erased Paul Graham's whole-paragraph parentheticals, so correct quotes from them were "unmatched"
  (8 essays flagged wrongly; no talk changes). Only short annotations ((laughs), [music], [3]) are
  dropped now. Re-verify with `ytbrain invalidate verify && ytbrain verify` (no LLM calls).
- **`ytbrain report`** breaks web pages down (articles cleaned; video and index pages skipped) and
  counts talks not fetched yet.
- **Crawler: pages whose links are followed are always fetched in full.** pmarchive.com's start
  page (saved as an article before the start-page rule) answered every re-check with 304, so its
  links were never read again and new posts couldn't be found. Only leaf pages (at the Source's
  depth) are re-checked conditionally now.
- **A YC Library talk YouTube has no captions for** (private, removed, none in English) is no longer
  lost: the next sync saves its page's transcript as an article.
- **A page reclassified as not an article after extraction** is tombstoned, so its items leave the
  index; it comes back if it's an article again.
- **`founder-coach --home X`** is the whole data folder: the models folder follows it.
- **Restore while a session is open:** a running coach notices a store renamed into place (restoring
  a damaged store) and reopens it, instead of writing to the file that was set aside.
- Suites: core 71, eval 31, pack 11, coach 39, plugin 20, web 26 (hermetic: pass on a clean checkout).

## 2026-09-26 (review fixes)

- **`ytbrain index` on an existing index** failed on the first Document once records carried
  `source_kind` ("field does not exist in table schema"); the index now gains missing columns on
  its next write (old rows read as talks). A Knowledge pack built before a column existed opens
  with the column's default instead of being reported as damaged.
- **Eval handles articles:** an article's Moment is 6 paragraphs starting every 3rd (`<doc>_p<n>`);
  talk Moment ids are unchanged. Released files give article spans in paragraphs and link the page.
- **`ytbrain report`** counts articles apart from talks (they were "no captions fetched" and
  inflated the talk record rate).
- **Crawler:** XHTML pages starting with `<?xml ... encoding=...?>` parsed as empty (silently
  skipped); pages declaring their charset only in `<meta>` were decoded as UTF-8; gzipped sitemaps
  are capped at 50 MB uncompressed; a page reopened for a higher `depth` is fetched in full (a 304
  has no links to follow).
- **Usage log:** quoted values in error messages (e.g. a timezone the Founder typed) are replaced
  by `'…'`.
- **Tests are hermetic:** `YTBRAIN_DOTENV=0` stops `config.py` reading `.env`, and every suite sets
  it; tests no longer read the repo's `sources.yaml`. On a clean checkout (as in CI) `test_core`
  died on the missing `sources.yaml` and `test_eval` failed without `YTBRAIN_LLM_BACKEND`; both pass
  now. Suites: core 71, eval 30, pack 11, coach 37, plugin 20, web 24.

## 2026-09-26 (usage log)

- **Local usage log** (docs/usage-log.md): one event per coach tool call (tool, time, session, duration,
  outcome, counts and item ids), no Founder text unless `FOUNDER_COACH_USAGE_TEXT=1`, kept 90 days,
  off with `FOUNDER_COACH_USAGE=0`. Stored in `founder.db` (schema v4, migrated with a backup first);
  never costs a tool call. `founder-coach usage summary|export|clear`; `status` reports it. Tool schemas
  are unchanged.

## 2026-09-26 (website Sources)

- **Any website can be a Source** (`type: website` + `url` in `sources.yaml`; optional `depth`,
  `render`, `max_pages`, `enabled`). Polite crawling (robots.txt, per-host pacing and
  `Crawl-delay`, backoff honouring `Retry-After`, 5 MB limit, no login, no evasion), scope by host
  and directory plus sitemaps, Playwright rendering only when a page's text is thin, page typing
  and metadata from JSON-LD/OpenGraph/meta tags and trafilatura, English only for now. Articles
  become Documents; listings contribute links; embedded YouTube talks go to YouTube sync.
  Plan and acceptance criteria: `docs/web-sources-plan.md`; design: ADR-0013.
- **Fix (first real crawl):** relative sitemap URLs (`Sitemap: /sitemap.xml` in robots.txt, relative
  `<loc>` in a sitemap index) now resolve against the file they came from, and a URL that can
  never work (no http(s) scheme or host) fails at once instead of being retried.
- **Fixes from the first 20 paulgraham.com pages:** paragraphs written as `text<br><br>text`, or
  returned as one block by trafilatura's table-layout fallback, are now separate paragraphs (they
  were one ¶ per essay); a `July 2023` byline at the top is the date (trafilatura guessed the year
  only, or a date from a link); a Source's `name` you wrote is the series, and a new optional
  `author` is the speaker when a page names none; saved-page paths are stored relative to
  `data/raw/web`. `clean` re-reads saved pages, so already-fetched pages pick all this up.
- `ytbrain sync --type website|youtube` syncs one Source type (combine with `--limit`; `--source` must match it).
- **Fixes from the first multi-site crawl:** a child sitemap listed as an ordinary `<url>` (YC does
  this for `/library/sitemap.xml`) is now read as a sitemap; raising a Source's `depth` re-opens the
  pages at the old limit on the next run (their links were never followed); the page cap is 15 MB
  (was 5 MB: YC's JavaScript pages inline large data); a start page that was skipped once is
  tried again every run.
- **Page types from the YC Library crawl:** a page that embeds a YouTube video and carries its
  transcript is a video page (the talk is ingested once, from YouTube, with timestamps); a start page
  crawled with `depth >= 1` is the Source's index; archive, category, tag, author and `/page/N`
  URLs are listings. `clean` applies the same rules to pages saved earlier (marked skipped, never
  extracted).
- **Adapter seam** (`ytbrain/sources.py`): each Source type owns `sync` and `clean`; everything
  after `clean` is shared. YouTube is the first adapter, unchanged. `enabled: false` works for
  every Source; `ytbrain sync --source ID`, `ytbrain invalidate <step> --source ID` and
  `ytbrain drop --source ID` are new.
- **Identity that survives moves:** page ids from the canonical URL's SHA-256 (clash refused), and
  the same text at a new URL is an alias, not a new Document. The crawl queue and page state live
  in the manifest database: interrupted crawls resume; re-checks after 7 days use conditional GET;
  404/410 tombstones a page.
- **Paragraph Locators:** records gain the given fields `source_kind` and `locator` (defaults match
  every existing talk record, so nothing is re-extracted; the generated schema is now pinned by a
  test). Article evidence is located to paragraph numbers, linked with text fragments
  (`#:~:text=`), shown as `¶n`. The coach's search hits gain `source_kind` (`start_s` is null for
  articles); the citation rules cover articles.
- New `web` extra (trafilatura, Playwright); CI installs it and runs `tests/test_web.py`
  (14 acceptance tests: mocked HTTP, a localhost server, a fake browser).

## 2026-09-26 (repo hygiene audit)

- **`.gitignore`:** a bare `feedback/` pattern was hiding `plugin/skills/feedback/` from git;
  the Founder-data patterns are now specific (`feedback-*.jsonl`, `founder.db-*`,
  `before-restore-*/`, `founder-export.json`), and common secret files (`*.p12`, `id_rsa*`,
  `.netrc`, `.pypirc`, `credentials.json`…) are ignored.
- **README** rewritten at the top for the product (what it is, status, quickstart for testers
  and developers, documentation map), with a Tests and CI table, a current roadmap, contributing
  rules and a Security and privacy section. The changelog moved here; `SECURITY.md` added;
  `PHASE2.md` archived to `docs/archive/phase2-parked.md`.
- **CI can't pass on skipped tests:** tests that used to return silently when an optional
  dependency was missing now report it, and with `REQUIRE_ALL_TESTS=1` (set in CI, which also
  installs LanceDB for the index tests) they fail.
- **Releases leave no trace on failure:** a failed validation puts the marketplace clone and the
  version files back; a failed push keeps the local commit and tag and says how to finish; the
  release warns when the build regenerated files that should be committed.
- `founder_coach/product.json` missing or damaged gives a fix-it message; `export` and
  `feedback export` report an unwritable folder instead of a traceback.

## 2026-09-26 (product id, repos and CI, Feedback, beta releases)

- **One product id** (ADR-0012): `product.toml` holds the id (`founder-coach`), display name,
  SEO description, keywords and the two repo names. `plugin/` is now a template (`{{id}}`);
  the assembler fills it in and writes `founder_coach/product.json`, which the runtime reads
  through `founder_coach.product` (CLI name, `~/.<id>`, `<ID>_*` settings). A rename is one
  edit plus a rebuild; tests fail on a spelled-out id. The contract skill is now `coach`.
- **Feedback** (CONTEXT.md): `/founder-coach:feedback` shows a record of a wrong answer and, on
  a yes, saves it with the new 8th tool `coach_feedback` (store schema v3, migrated with a
  backup first; `forget` deletes it; not part of FOUNDER.md). `founder-coach feedback list|export`
  writes only the Feedback to one JSONL file to send. New plugin eval case
  `feedback-shows-before-saving`.
- **Repos and CI** (docs/release.md): `.gitignore` updated, `scripts/check_secrets.py` (CI and a
  pre-commit hook), `.github/workflows/ci.yml` (Linux, Python 3.11 and 3.13: secret scan, all
  five suites with skips treated as failures, an assembled plugin with `--check`, generated
  files committed, `claude plugin validate --strict`).
- **Beta releases:** `scripts/release.py` builds with `--check`, scans the build, checks GitHub's
  file limits, writes `marketplace.json` (renames for a changed id) and a tester README into
  the `founder-coach-marketplace` clone, validates, commits, tags `<id>--v<version>` and
  optionally pushes; it refuses a version that is already released.

## 2026-09-25 (first full coach eval: save rule, usage limits, model warm-up)

- Contract rule 8 now says what counts as a yes: a Founder who asks the coach to save values
  they dictated ("set me up: company Acme, stage MVP") has agreed to those values; anything the
  coach drafted, reworded or inferred still needs a yes. Same wording in the founder-coach skill,
  the setup skill (and its generated prompt) and the server instructions.
- `coach_search` waits up to 20 s (`FOUNDER_COACH_SEARCH_WAIT_S`) for models that are still
  loading, instead of answering the session's first question by keywords.
- `ytbrain eval coach` stops at the first usage-limit refusal from Claude (exit 2) instead of
  marking every remaining case ERROR; finished cases are kept for the re-run.
- G5: the turn-1 check allows only the profile values the Founder dictated (tolerant of normal
  forms like `fri`); a drafted Goal, an extra field or a changed value fails it. Harness `g5` is
  now `h4`, so G5 re-runs; G2, G4 and G6 results for the current build are kept.

## 2026-09-25 (plugin in the repo, M3b; coach hardening, M3a)

- **The Claude Code plugin is in the repo** (`plugin/`, `scripts/assemble_plugin.py`,
  `tests/test_plugin.py`): 8 skills, the SessionStart hook and the MCP config. The MCP prompts in
  `founder_coach/playbooks/` are now generated from the skills. `claude plugin validate --strict`
  passes on an assembled build, and a wheel built from it serves setup → ask → weekly-focus →
  check-in over stdio.
- **The pack's checksum is checked at start-up.** A mismatch makes the pack "unavailable" in
  `coach_corpus_status` and in `coach_search` errors; memory keeps working. `status` and
  `warmup` check it too.
- **A damaged founder store no longer stops the server.** Search keeps working; memory tools
  say nothing has been deleted and point to the new **`founder-coach restore [--list] [BACKUP]`**,
  which sets the damaged files aside and restores a checked backup. `status` shows the store's
  integrity check, and the SessionStart hook mentions a damaged store.
- **An ONNX error during a search falls back to keyword results** with a note.
- **`forget --confirm COMPANY` is documented,** and `forget` refuses to run without a terminal
  unless it's given.
- **Assembler fixes:** ships only `knowledge.sqlite` + `pack.json` (not the embedding cache),
  leaves no `__pycache__` in the build after `--check`, and refuses a pack without its manifest.
  `find_pack` moved to `founder_coach/pack.py` (still importable from `server`).
- **Plugin dependencies** match the runtime's imports: mcp, pydantic, numpy, fastembed,
  onnxruntime, tzdata.
- **Tests:** 6 new in `test_coach.py` (22), 15 in `test_plugin.py`.

## 2026-09-25 (pack experiments toward G1)

- **The pack ships without a reranker by default**: on labels v1.2.0, jina-reranker-v1-turbo
  lowered nDCG@10 from 0.479 to 0.457 and slowed every search (`--rerank-model` still sets one).
- **`ytbrain pack build --with-passages`** adds the 11.5k transcript Passages (private beta only,
  ADR-0009). The coach shows a Passage only as the speaker's quoted words (the untrusted
  channel), trimmed to 120 words (400 in detailed mode), and `coach_read` of a talk leaves its
  Passages out unless one was asked for by id.
- **The model check before an eval is sturdier:** a malformed test answer (OpenRouter can route
  one request to a provider that answers badly) is asked again up to 4 times with exponential
  backoff (2, 4, 8 s, jittered); a refused request (404/400/bad key) still fails at once, and
  the model must still answer correctly. Fallbacks never pick OpenRouter variants such as
  `:batch` or `:free`. `eval coach` checks only its judges (not the question generator) and
  refuses a Claude judge, since Claude writes the answers being judged.
- **`ytbrain eval run --label NAME`** scores a pack variant under its own name
  (`pack-no-rerank-NAME`), so several variants can be compared and rescored side by side.

## 2026-09-25 (M3d: evaluating the coach)

- **`ytbrain eval coach`** runs gates G2 (citation support), G4 (sycophancy), G5 (multi-week
  memory) and G6 (decomposition) through the real host: `claude -p` with the assembled plugin,
  the real server and pack, graded by the eval's judges; G5 is graded on the Founder store's
  final state after three scripted weeks (a fake clock, `FOUNDER_COACH_FAKE_NOW`, moves the
  weeks). Resumable per plugin build, spend-capped, progress with ETA. Cases live in
  `ytbrain/eval/coach_cases/`.
- **`plugin/evals/`**: a mocked `claude plugin eval` suite (triggering, near-miss, citations
  only from results, no save without a yes, pushback on a weak plan). The assembler leaves it
  out of what ships unless `--with-evals`.

## 2026-09-25 (robustness audit: every Step, eval, pack and the coach)

Pipeline
- **One bad talk no longer stops a Step.** clean, verify, index and pages record the error for
  that talk and carry on. A talk that fails a Step 3 runs in a row is **parked** (skipped until
  `ytbrain <step> --retry-failed`); a rejected request (HTTP 400/422) or an answer that fails
  validation even after repairs parks at once instead of being paid for on every run.
- **sync:** a yt-dlp run that hangs past 15 min is a retryable failure, not a crash of the whole
  sync; a caption track that's listed but never written is retried instead of settled as "no
  captions"; plain sync shows an ETA.
- **New captions flow through:** a re-fetched transcript that changed sends the talk back to
  extract (and so to verify and index).
- **extract** marks verify stale *before* saving a new record, so a kill in between can't leave
  an unverified record marked verified.
- **pages** are written only for verified records.
- **index** rebuilds the keyword (full-text) index if its last build was interrupted, and search
  says so once instead of silently dropping keyword results.
- **Rate limiter:** a 429 right after a speed-up cuts the rate again; `Retry-After` is honoured
  but capped at 5 min.
- `run` shows elapsed time per Step and in total; runs killed without warning are recorded as
  `abandoned` by the next run.

Eval and pack
- **Releases are sealed:** CHECKSUMS lists the new VERSION before VERSION is written, so an
  interrupted release is detected and finished as the next version, never left as new labels
  under an old number. Dotfiles (`.DS_Store`) are no longer part of a release.
- `eval build` keeps released questions in their slots, refuses to release while one of them
  is still being graded, and keeps the set's `valid_as_of` date.
- A failed query rewrite is retried before its question is pooled (twice at most).
- With only two judges, a 2-grade disagreement takes the lower grade instead of waiting forever.
- Paid calls whose answers fail validation count against the spend cap; failed calls are
  summarised at the end of each step.
- A call still running after 3× its timeout is given up on and redone next run.
- `eval status` and `eval rescore` run while another eval command is running.
- `--max-cost 0` now means "spend nothing" (it used to mean "no cap").
- `eval run` saves the search results before scoring, so Ctrl+C during scoring keeps them.
- `pack build` can't leave a pack that fails its checksum when killed between writing the pack
  and its manifest.

Coach
- **`forget` and `restore` work while the coach is running:** forget empties the store in
  place (and scrubs freed pages), restore copies the backup into the live file; a running
  server sees either at once. The replaced store is still kept aside.
- Retrying a write with the same `request_id` replays it (a retried carry no longer fails),
  and two hosts can't carry the same Commitment twice. Reusing a `request_id` for a different
  write is refused (store schema v2 adds the write's fingerprint; migrated automatically,
  after a backup).
- A malformed `pack.json` or pack file makes the pack "unavailable" instead of stopping the
  server; a busy, full or failing disk gives a "nothing was saved, retry with the same
  request_id" message; a store that was busy at start-up is reopened on the next call.
- The pack's full checksum runs once per pack version, not on every start.
- `founder-coach warmup/status/serve` find the pack next to the plugin or in the repo's
  `data/pack` without `--pack`; `status` says whether the models are downloaded.
- Tool descriptions are cleaned before they're sent, so the schemas (and the golden test) are
  the same on Python 3.10-3.13.

## 2026-09-24 (eval calls no longer hang)

- **Eval calls now time out after 120 s** (`YTBRAIN_EVAL_TIMEOUT_S`), not the 900 s sized for
  long extractions. One hung provider call could hold `eval judge` or `eval build` near 100%
  for up to 30 minutes.
- **The heartbeat says what it's waiting for:** "1 call(s) in flight, oldest 240s".
- **An interrupted `ytbrain run` is recorded as `interrupted`,** not left `running` forever in the
  manifest's run history (the two old runs stay `running`; they're harmless).
- **`eval rescore` and `eval judge` pick the run of exactly the configuration you name.** They
  matched by prefix, so `--config pack` silently used the newer `pack-no-rerank` run: its rescore
  showed pack-no-rerank's numbers, and its judging pooled the wrong Moments. Re-run
  `eval judge --config pack`, then `eval rescore --config pack`.
- **`eval judge` bumps the label version whenever the released labels change,** including
  when a resumed run finishes grades that an interrupted run started. Before, it only bumped
  when the current run pooled new Moments.

## 2026-09-24 (coach runtime, M3a)

- **New `founder_coach` package and `founder-coach` command:**
  - the MCP server: 7 tools, 4 prompts, 4 resources;
  - the founder store (ADR-0011): a change log, safe retries, daily backups, FOUNDER.md;
  - Nudges, the SessionStart hook, warmup, status, export and forget.
- **Behaviour:**
  - start-up takes about 0.6 s with the full pack;
  - semantic search switches on when the models finish loading in the background, with
    keyword results until then;
  - citations are checked against the pack, and quotes are marked as untrusted text.
- **Tests:** 16 new ones, among them a schema snapshot of the tools, a stdio start-up under both
  MCP protocol versions, and three processes writing at once. That last test found and fixed a
  race in the daily backup.

## 2026-09-24 (fair comparisons for new configurations)

- **The first pack run scored 0.336 nDCG@10 against 0.568 for `full`, but most of that gap
  was measurement.** 43% of the pack's top-10 Moments had never been judged: the labels came
  from the full index's systems, so anything new counted as irrelevant. Scoring only the judged
  part gives 0.501. Separately, the pack can only return 70% of the relevant Moments at all,
  because some of what the judges liked lives only in transcript Passages.
- **`eval judge --config C`** grades a saved run's unjudged Moments with the same judges (pool
  extension, standard TREC practice) and releases the labels as a new minor version.
- **`eval rescore`** re-scores saved runs in seconds against the current labels.
- **Comparisons now give a verdict:**
  - *inconclusive* (exit code 3) when the labels differ from the baseline's, or less than 90%
    of the top 10 was judged;
  - a failed gate (exit code 1) only when the comparison is sound.
- **Reports add `ndcg@10-cond` and each configuration's reach.**
- **The new `full-no-passages` configuration** separates the cost of dropping Passages from
  the cost of smaller models.

## 2026-09-24 (faster pack builds)

- **`pack build` batches texts of similar length together.** Each batch is padded to its
  longest text, so mixing 500-token summaries with 40-token advice wasted most of the work.
  The vectors are the same.
- **`pack build --device coreml`** (experimental) runs the ONNX embedder on Apple's GPU /
  Neural Engine through ONNX Runtime's CoreML provider, falling back to the CPU for
  unsupported operations. It's opt-in: quantized models don't always run faster there, so time
  a small build first. The default stays `cpu`, and the plugin's search always runs on the CPU,
  where one query takes milliseconds. (`--device auto|mps` elsewhere is PyTorch, which the
  pack doesn't use.)

## 2026-09-24 (eval run reporting)

- **An independent recomputation matched the first real dev results:** every mean,
  bootstrap interval and p-value. The metric code is unchanged.
- **Comparisons show the size of a difference, not just its significance:**
  - each metric's paired difference comes with a 95% interval;
  - nDCG@10 (the primary metric) is tested on its own;
  - the other seven metrics are Holm-corrected among themselves.

  Before, nDCG@10 was corrected together with them and looked less certain than it is.
- **Recall is shown against its ceiling.** Recall@k is printed next to the best possible value
  (dev: Recall@10 can reach at most 0.342, because questions have 33 relevant Moments on median).
- **More breakdowns:** by popularity (head / torso / tail), Stage and topic; small groups are marked.
- **Every run writes a TREC run file,** and the saved JSON now includes the comparison.
- **`eval run --config no-rerank`** no longer loads the 2 GB reranker it doesn't use.

## 2026-09-24 (Knowledge pack, M2d core)

- **Validated end to end in the simulator**, and fixed along the way:
  - `eval build`'s model check no longer mistakes a burst of 429s for a broken model. It used
    to swap in another model of the family or refuse to start with "no working model"; now it
    stops with "the API kept failing ... try again in a few minutes". A 404 still falls back
    within the family as before.
  - The model check is paced at the eval rate.
  - `index` prints per-talk time and an ETA, and `sync --backfill` says how many videos are
    left and how long they'll take.
  - `pack build` progress is flushed immediately, which matters in logs and pipes, and shows
    after the first batch.
  - A second `pack build` into the same folder says so clearly.
  - Cost projections under $1 show three decimals.

- **`ytbrain pack build`** writes the Knowledge pack the coach plugin will ship:
  - 13,717 Verified items (no Passages) from 715 talks in one SQLite file;
  - vectors from a small ONNX model (`BAAI/bge-base-en-v1.5` by default, no torch);
  - a checksummed `pack.json`.
  - An embedding cache makes rebuilds and interrupted builds redo only what's missing. The
    new pack replaces the old one only after it has been opened and searched.
- **`search --pack`** and **`eval run --config pack|pack-no-rerank`** search the pack. The
  pack configs are compared with `--compare full` under ADR-0009's 0.03 nDCG@10 gate.
  `eval run --compare` now fails clearly when that baseline was never saved.
- **Search code moved to `founder_coach/search.py`,** the start of the coach runtime package.
  The ytbrain imports are unchanged, and ranking is unchanged.
- **Re-run `uv pip install -e ".[extract,dev,index,pot,pack]"`** (or your extras + `pack`)
  so the new package is installed. Until then ytbrain finds it in the checkout.

## 2026-09-24 (rate limits that adapt)

- **The shared request rate now adapts to 429s** on hosted APIs, for extract and eval alike. A
  burst of 429s halves the rate for every worker (counted once per 20 s, never below 6/min),
  and each quiet minute raises it by 10 % of the ceiling until it's back at
  `YTBRAIN_LLM_MAX_RPM` / `YTBRAIN_EVAL_MAX_RPM`. Eval progress lines show the slowed rate
  while it's below the ceiling. Local servers are never paced.
- **Scope for v1** is written down in `docs/phase3-plan.md` §0: a private beta, its quality
  gates, what's required and what's optional. ADR-0008 is amended (Jev parked, LLM backend
  first).

## 2026-09-24 (eval preflight)
- `eval build` probes every model with a tiny typed call before spending anything. A model that
  fails (e.g. OpenRouter's "No endpoints found that can handle the requested parameters") is
  replaced by the next model of its family.
- Request fields from `YTBRAIN_LLM_EXTRA_BODY` that a model doesn't accept (typically
  `reasoning` on non-reasoning judges) are dropped for that model only; the extract model's
  requests are unchanged.

## 2026-09-23 (validation fixes)
- Lock: the single-run lock is now an OS file lock, released automatically when a process dies.
  The old PID check could treat a crashed run as alive when its PID was reused, and then
  block every later command.
- `extract`'s start-up check retries a 429 or 5xx with backoff (honouring Retry-After) instead
  of aborting the run.
- Clearer messages: `clean` says skipped means "no captions or too short"; `verify` counts talks
  "without a record yet"; an interrupt names the exact command to resume, e.g.
  `ytbrain eval build`; `eval status` also shows smoke builds.

## 2026-09-23 (eval, M2a + M2b)
- `ytbrain eval build --set dev` builds the Tuning set, as specified in
  [docs/eval-spec.md](docs/eval-spec.md):
  - 150 questions generated from sampled advice across the talks, with checks against reused wording;
  - five retrieval variants pooled into two-minute Moments;
  - two judges grade each Moment on the UMBRELA 0–3 scale, and a third breaks ties;
  - output in BEIR, TREC and Moment files, with checksums.
- The build resumes after any interruption, runs LLM calls in parallel with backoff, and stops
  cleanly at a spend cap.
- `ytbrain eval run` scores a search configuration (nDCG@10, recall, MRR, judged@10, 95 %
  bootstrap intervals, breakdowns). `--compare` adds paired randomization tests with Holm
  correction and the regression gate.

## 2026-09-23 (search)
- Search shows one result per quote per talk; an advice item and a takeaway citing the same
  sentence no longer both appear.
- `--topic` accepts only valid categories, so a typo is an error instead of zero results.

## 2026-09-23 (looping providers)
- A reply cut off at the output cap is retried on a different OpenRouter provider
  (`provider.ignore`). The call log showed the cause: one provider repeating `"idea"`
  until the cap, not a long answer.
- The `YTBRAIN_LLM_MAX_TOKENS=16384` recommendation is withdrawn; 8192 is plenty for a
  2–3k-token record.

## 2026-09-23 (YouTube rate limits)
- `sync` checks yt-dlp's setup before starting: the `yt-dlp-ejs` package, a JavaScript
  runtime (Node, Bun or QuickJS are switched on when Deno is absent) and, if installed,
  the PO-token provider server.
- The dependency is now `yt-dlp[default]`.
- Pause before each subtitle file: 10 s by default, 15 s for `--backfill`; the backfill
  pace is 60 videos an hour. These were first set to 30 s, 60 s and 20 an hour, which
  proved more than needed once Deno and PO tokens were in place.
- `sync --backfill [--per-hour N]` slowly retries only the videos that failed for
  retryable reasons.
- Optional `YTBRAIN_YTDLP_PROXY` pass-through; there is no rotation, by design.
- New optional `pot` extra installs the bgutil PO-token plugin. Script mode (no server)
  is detected via `YTBRAIN_POT_SCRIPT_HOME`.

## 2026-09-23 (after the first full 2.2.0 run: 99.4 % records, 98.5 % quotes verified)
- Verify: tidied quotes pass on a word-containment test (≥ 90 % of ≥ 8 distinct words in
  a window up to 1.5× long). On the corpus this takes quote verification from 98.5 % to
  99.1 %, with 0 of 480 cross-talk quotes passing.
- Verify: records that yielded nothing (or no advice from a practical talk) are *flagged*
  with a reason instead of passing with a 100 % rate.
- Extract:
  - Item budget scales with talk length (a 150-word clip asks for 2 + 2, not 10 + 12).
  - The prompt keeps quotes verbatim (fillers, repeats, caption typos) and never uses a
    title as a quote.
  - Advice phrased as a highlight must also become an advice atom.
  - Q&A sessions are no longer told they may return nothing.
  - The no-advice retry names the highlights to convert and starts at 1 000 words.
- Extract diagnostics:
  - Every call logs its finish reason, token usage (including reasoning tokens),
    provider and seconds.
  - A cut-off answer that is already complete JSON is kept instead of re-asked.
  - A video extract gives up on leaves a call log in `data/reports/extract-failures/`.
- `report` breaks down missing transcripts and lists flagged records and extract failures.
- Index: transformers no longer starts a hidden background download of a second 2.3 GB
  copy of bge-m3 (which also kept `ytbrain index` from exiting); set
  `DISABLE_SAFETENSORS_CONVERSION=0` to allow it again. `index --limit` now reports how many
  talks are left for a later run instead of counting them as current.
- Licensed under BUSL-1.1 (Apache-2.0 from 2030-01-01); disclaimer added.
- M1: `ytbrain index` (Knowledge items, local bge-m3 embeddings, LanceDB, per-Document checkpoints, rebuilt when records change) and `ytbrain search` (vector + full-text fusion, bge-reranker, Stage/kind/recency boosts, diversity cap); `--device auto|mps|cpu|cuda`; `run` ends with the index Step.
- Fix: videos without captions were re-sent to sync on every clean; 'skipped' is now final for a Step until its input changes.
- Extraction hardening (schema 2.2.0, re-extracts older records): truncated answers are re-asked shorter instead of "repaired"; repairs see the whole previous answer; a local self-check retries once with feedback when quotes aren't in the transcript or a substantial talk yields no advice; long talks use a few ~8k-token windows plus one overview call instead of one call per chapter; size caps per call; every LLM call is logged in `extraction_meta.calls`.
- Fix: Series was never set. It now comes from the Source name (a catch-all assigns none) and provenance from the publisher recorded by yt-dlp; `ytbrain refresh` backfills both without re-extraction.
- `AGENTS.md` / `CLAUDE.md` for coding agents.
- Phase-2 design: `CONTEXT.md` glossary, ADRs 0001-0008, `docs/phase2-plan.md`.
- `sources.yaml` is now personal (git-ignored); template in `sources.example.yaml`.
- Extract: parallel `--workers`, shared rate pacing for hosted endpoints only,
  exponential-backoff retries (per request and per video), clean stop on 401/402/403/404/410
  and exhausted quotas; progress heartbeat for slow videos.
- Backends: OpenRouter, NVIDIA NIM and LM Studio via the OpenAI-compatible backend;
  `YTBRAIN_LLM_JSON_MODE`, `YTBRAIN_LLM_EXTRA_BODY`; `.env` loaded automatically;
  `ops/probe_models.py`.
- Sync: same-video retries with 429 cooldowns, yt-dlp retry sleeps, playlist-listing
  retries, private videos marked unavailable, exact English caption tracks only.
- Verify: filler words ignored, `...`-joined quotes checked piecewise; unverified claims
  withheld from pages; `invalidate --only-flagged`.
- Schema 2.1.0: categories `ai-and-tech-trends` and `founder-story`; schema bumps now
  re-extract automatically.
- Resume hardening: atomic writes everywhere, SIGTERM/SIGHUP handled, partial-file sweep,
  damaged inputs sent back upstream, transcripts under 60 words skipped.
