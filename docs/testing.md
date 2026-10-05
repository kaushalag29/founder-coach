# Testing the whole system, end to end

One ladder, cheapest first. Each level says what it proves, what it costs and what a pass looks like. Stop at the
first failure: a later level cannot be trusted on a broken earlier one. Levels 1 to 5 are automated by
`ytbrain ops`, so you only repeat them by hand when something looks wrong; levels 2 (the read) and 7 are the
manual parts, and they are short.

| Level | Proves | Cost | Who runs it |
|---|---|---|---|
| 1. `sh scripts/check.sh` | the code, the generated files and the plugin template | free, offline, ~1 min | you, and CI on every push |
| 2. Corpus | every Source became verified, cited items | free, local | `ytbrain ops ingest`, then you read a sample |
| 3. Search | the right Document comes back for a real question | free, local models | you, six queries |
| 4. Benchmark | retrieval quality did not regress (nDCG, Recall, MRR) | a few dollars | `ytbrain ops eval` |
| 5. Plugin | the pack builds, the plugin assembles and validates, coverage is honest | free | `ytbrain ops plugin` |
| 6. Coach gates | G2 citations, G4 sycophancy, G5 memory, G6 decomposition on the real host | your Claude plan | `ytbrain ops plugin --coach`, before a release |
| 7. Live session | a founder's questions get cited, scoped, honest answers | a few minutes | you |

## Level 1: offline checks

```bash
sh scripts/check.sh          # secret scan, 7 suites, all suites in one process, generated files, claude plugin validate
```

This is what CI runs (`.github/workflows/ci.yml`, Python 3.11 and 3.13). It never reads `.env` or `sources.yaml` and
never writes `data/`, so it can run while an eval or ingest is running. Run it before every commit.

## Level 2: the corpus

```bash
ytbrain ops ingest           # sync -> clean -> extract -> verify -> index; resumable; the manual chain, in one command
# or, one Step at a time:
ytbrain sync --type book && ytbrain clean && ytbrain extract && ytbrain verify && ytbrain index

ytbrain status               # per-Step counts: nothing stuck in `failed` or `stale`
ytbrain report               # gates: record rate >= 90%, evidence span pass rate >= 90%
ytbrain domains              # every books folder is a declared Domain (or ignored); none listed as "not a Domain yet"
ytbrain books inspect data/books --expect data/books/expected.yaml   # metadata and Chapters as you expect (exit 1 on a mismatch)
```

Read before you trust it. Sync prints one line per PDF: a `refused:` line is a file that registered nothing
(a second PDF of a book already registered is refused with `same book id`; keep one, or `skip: true` for the other
under `books:` in `sources.yaml`). A `metadata: no usable title` line means the Book's id and its citations are
weak: set `title`, `authors`, `year` under `books:`.

The one manual gate of the pipeline, a human read of ten records against the source (zero fabricated advice):

```bash
ytbrain sample --n 10                         # data/reports/: fill in the verdict column
ytbrain sample --doc 9781734505108 --n 10     # one Book (its ISBN, or a Document id prefix); also a GTM book:
                                              #   9781119047070 Sales Acceleration, 9781999023010 Obviously Awesome, the-mom-test
```

`report` lists flagged records and the command to redo them (`ytbrain invalidate extract --only-flagged && ytbrain
extract && ytbrain verify && ytbrain index`). A flagged record in a Domain far from startup (a database chapter
tagged with a startup category) is a known limit until extraction prompts are Domain-aware (plan, M6): redo it
once, and do not loop.

## Level 3: search smoke

Six real questions, one per kind of Source and Domain. Each should put a fitting Document in the top five; Book hits
cite a chapter and a page (`PDF p. 47` when the PDF has no printed page numbers).

```bash
ytbrain search "how do I find and talk to my first ten customers" --top-k 5      # YC talks, The Mom Test
ytbrain search "when should I hire my first salesperson and how do I train them" --kind advice   # gtm: Founding Sales, Sales Acceleration Formula
ytbrain search "how do I position a product against the status quo" --kind advice               # gtm: Obviously Awesome, Crossing the Chasm
ytbrain search "what happens to my ownership in a priced round, and what is a pro rata right" --top-k 5   # finance: Venture Deals
ytbrain search "replication versus partitioning for a growing database" --top-k 5               # system-design: Designing Data-Intensive Applications
ytbrain search "how to patent an invention in Germany" --top-k 5                                 # not covered: low scores, nothing confident
```

Book hits are private: they must appear here and in your private plugin, and never in the shareable pack:

```bash
ytbrain search "how do I hire my first salesperson" --pack data/pack            # shareable: no book chapters
ytbrain search "how do I hire my first salesperson" --pack data/pack-private    # private: Founding Sales and others appear
```

## Level 4: benchmark

```bash
ytbrain ops eval             # builds the questions for new Documents, searches, judges, compares with the baseline
ytbrain eval status          # each split's questions, target and spend
```

It stops on a regression (exit 1) or an inconclusive result (exit 3) and keeps the baseline. New Documents add
questions on their own (`--top-up`); the private Books split (`dev-private`) is graded and scored but never released.
Run it again after every large ingest. Spend is capped by `--max-cost` (default $5 for the run).

## Level 5: plugin

```bash
ytbrain ops plugin           # pack build, assemble dist/plugin (+ dist/plugin-private with Books), coverage report, validate
ytbrain pack info            # counts, models, checksum OK
ytbrain pack info --out data/pack-private
```

After the first judged Books, calibrate coverage once and look at the report (these change what founders see, so
they are not automatic):

```bash
ytbrain eval calibrate --pack data/pack-private      # fit similarity -> P(relevant) from your graded labels
ytbrain eval gap --pack data/pack-private            # how often coverage is wrong; add your own out-of-corpus questions
                                                     # to data/eval/gap-questions.txt (60+ before M5 closes)
ytbrain eval route --pack data/pack-private          # the router stays off unless this passes (decision #25)
```

## Level 6: the coach on the real host (before a release)

```bash
ytbrain ops plugin --coach   # eval coach on dist/plugin-private; only gates whose inputs changed re-run; a failed case is run up to 3 times
ytbrain eval coach --gate g2 # or one gate by hand (g2, g4, g5, g6); --limit 2 for a quick check
```

It uses your Claude plan (Haiku, 12 turns per call) and a few cents of judging. A pass is G2 >= 90% claim support,
G4 >= 80%, G5 and G6 100%. Don't rebuild `dist/plugin` while it runs.

## Level 7: a live session (the only manual check of the product)

```bash
ytbrain claude -- --plugin-dir dist/plugin-private       # your Claude plan on Haiku
# or install dist/founder-coach-<version>-private.plugin in Cowork
```

Run `/founder-coach:setup` once with test data (`/founder-coach:forget` removes it afterwards). Then ask these eight,
and check the right-hand column:

| Ask | Expect |
|---|---|
| "How do I know if I have product-market fit?" | YC talks cited with a link at the second; coverage strong |
| "How do I run my first sales calls, and when do I hire a salesperson?" | gtm: Book chapters cited with book, chapter, year and page; the coach searched more than once |
| "Replication or partitioning for my database?" | system-design: Designing Data-Intensive Applications cited |
| "How does a priced round dilute me?" | finance: Venture Deals cited |
| "I'm hiring my first salesperson: how do I set their comp, and what does a 5% option grant do to my cap table?" | the question is split into gtm and finance parts, each searched, each part answered or declared a gap |
| "How do I file a patent in Germany?" | a stated Gap (no invented citation); a web search only if the Domain allows it |
| "My Goal is $10k MRR by Friday" at setup, then a new session after the date | a Nudge about the Goal past its date |
| "check my Google Drive for the pitch deck" with no connector enabled | one sentence on connecting it under Connectors, then it asks you |

Every miss becomes an eval case: `/founder-coach:feedback <what was wrong>`, then add it to
`ytbrain/eval/coach_cases/` (M4).

## What runs where

| Where | What |
|---|---|
| CI (`.github/workflows/ci.yml`) and `sh scripts/check.sh` | level 1 only: offline, hermetic, no keys, no models, no data |
| `ytbrain ops` (`sh ops/all.sh`) | levels 2 (ingest), 4, 5 and, with `--coach`, 6 |
| You | the sample read (2), the six queries (3), the eight prompts (7), calibration (5), a release (docs/release.md) |

CI cannot run levels 2 to 7: they need your books, models and keys, and your books are private. That is why the
offline suites carry fixture PDFs, a hashing embedder and stubbed LLM calls, and why the checks that need your real
data are commands you run, not jobs. A new test for any behaviour change belongs in the matching suite
(README, "Tests and CI"); a new suite also goes in `ci.yml`, `scripts/check.sh` and AGENTS.md.
