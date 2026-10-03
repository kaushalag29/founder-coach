# ytbrain — M3a/M3b status and handoff (updated 2026-09-28)

Builds on [phase3-plan.md](phase3-plan.md) §11. Written so any session can pick up from here.
Vocabulary: [CONTEXT.md](../CONTEXT.md). Roadmap: [phase2-plan.md](phase2-plan.md), [phase3-plan.md](phase3-plan.md) §0.

## M3a: done

Founder store (change log, idempotent `request_id`, migrations, backups, FOUNDER.md, carry streak,
confirm/staleness, time zones, export/forget); nudges and context; 7 tools, 4 prompts, 3 resources
+ 1 template with annotations, fix-it errors, untrusted-quote wrapping, citation checks; keyword
fallback while models load; ~0.6 s start-up; stderr-only logging; CLI (serve, hook, warmup,
status, export, forget, restore); schema snapshot, stdio under both protocols, 3 concurrent writers.
Since then: an 8th tool `coach_feedback`, and CLI `feedback list|export` and `usage summary|export|clear`.

The three gaps from the 2026-09-24 handoff were applied on 2026-09-25, each with a test:

1. **Pack checksum at start-up (§11.4).** The server opens the pack with `PackStore(path, verify=True)`
   (sha256 against `pack.json`). A mismatch or missing manifest makes the pack `unavailable: …` in
   `coach_corpus_status.pack_error`, and `coach_search` / `coach_read` errors say so; memory keeps
   working. `status` and `warmup` verify too. `find_pack` now lives in `founder_coach/pack.py`
   (re-exported by `server`), so the assembler's `--check` doesn't import MCP.
2. **A damaged store doesn't stop the server.** `store.open_store()` returns the store or
   `(None, why)`: it catches unreadable files (`StoreDamaged`) and runs `FounderStore.check()`
   (`PRAGMA integrity_check`). `State.store` may be None with `store_error`; memory tools and the
   `founder://` resources answer "Nothing has been deleted: run `founder-coach restore` …"; search
   keeps working. `founder-coach restore [--list] [BACKUP]` (`store.list_backups`, `store.restore`):
   picks the newest backup that passes its check (or the named one), copies it via the backup API
   into a temp file, checks it, moves the current `founder.db` + `-wal`/`-shm` to
   `before-restore-<time>/` (never deletes), renames the copy in, runs migrations and regenerates
   FOUNDER.md. `status` reports `integrity` (exit 1 when the store is unusable); the SessionStart
   hook tells the host about a damaged store (and stays silent on other errors).
3. **ONNX failure during a search** falls back to keyword results with `mode: keyword` and a note
   naming the error; `top_similarity` is cleared so the Gap flag isn't based on a half-run.

Also: `forget --confirm COMPANY` is a documented flag; without it and without a terminal, `forget`
refuses (exit 2) and deletes nothing.

Tests: `tests/test_coach.py` 22 (6 new). The tool-schema snapshot is unchanged.

## M3b: in the repo, waiting on one real session

- `plugin/`: `.claude-plugin/plugin.json`, `.mcp.json` (server `coach`, `--pack ${CLAUDE_PLUGIN_ROOT}/pack`),
  `hooks/hooks.json` (SessionStart `startup|resume|clear|compact`, 10 s, fail-open), `pyproject.toml`,
  8 skills: `founder-coach` (model only; the 8-rule contract, `references/stages.md`, `citations.md`),
  `ask`, `weekly-focus`, `check-in` (both), `setup`, `status`, `export`, `forget` (user only).
  CLI calls in skills use `uvx --from "${CLAUDE_SKILL_DIR}/../.." founder-coach …`.
  Since then (ADR-0012): the contract skill is `coach`, a 9th skill `feedback` exists, and skills say
  `{{id}}` where the product id goes (the assembler fills it in).
- `scripts/assemble_plugin.py`: regenerates `founder_coach/playbooks/*.md` from the skills (host
  syntax stripped), builds in a temp folder, `--check` imports the runtime and verifies the pack,
  swaps atomically and keeps `<out>.previous`.
- `tests/test_plugin.py`: 15 tests.

Reconciled on 2026-09-25:

- **Dependencies** (`plugin/pyproject.toml`) now match the runtime's imports: `mcp>=2.2.0,<3`,
  `pydantic>=2.9`, `numpy>=1.26`, `fastembed>=0.8`, `onnxruntime>=1.18`, `tzdata`
  (tokenizers and huggingface-hub come with fastembed).
- **Prompt argument:** the `ask` prompt fills `{arguments}` (generated) and still `{question}`.
- **`find_pack` / `PackStore(verify=True)`** match what `--check` calls.
- **Assembler fixes found while testing:** it copied the whole pack folder, including the build-only
  `embed-cache.db`, and a single-file pack without its `pack.json`; it now ships exactly
  `knowledge.sqlite` + `pack.json` and refuses a pack without a manifest. `--check` ran Python
  inside the build and left `__pycache__` in it; it now runs with `-B`.

Verified in a Linux sandbox (Python 3.10 with a `tomllib` shim; the Mac runs 3.11+):
all five suites green (core 61, eval 17, pack 9, coach 22, plugin 15); `assemble_plugin.py --check`
on a test pack; `claude plugin validate dist/plugin --strict` passes (Claude Code 2.1.281); a wheel
built from `dist/plugin` (hatchling) contains the runtime and playbooks, and served
setup → ask → weekly-focus → check-in over stdio with the right FOUNDER.md afterwards.
Not verified: a live `claude --plugin-dir` session with a model (needs the maintainer's account).

## Robustness audit (2026-09-25)

The pipeline, eval, pack and coach were audited for failure handling, resume, retries,
concurrency and progress; about 20 bugs were fixed, each with a test (CHANGELOG.md
2026-09-25). Suites: core 70, eval 20, pack 10, coach 26, plugin 15. Coach store is schema v2.
Decisions taken: forget/restore work in place on the live store; a Step failing 3 runs in a
row is parked until `--retry-failed`; two judges without a tie-breaker take the lower grade;
`top_similarity` keeps its name (the spec was corrected, not the schema).

## M3d: coach eval, first full run (2026-09-25, build 20260925T052515Z-451a18b01aa6)

- G2 citation support 93% (gate 90%) PASS over 20 Ask questions; G4 sycophancy 10/10 PASS.
- G5 memory: dental passed; idea-to-mvp passed every store check but saved the dictated profile
  on turn 1 ("Set me up. Here's my profile…"). Decision: a Founder who asks the coach to save
  values they dictated has said yes to those values; anything drafted, reworded or inferred still
  needs a yes (rule 8 reworded; the G5 turn-1 check allows only dictated values). fundraising and
  G6 (5 cases) errored on the Claude plan's session limit; the eval now stops at that limit.
- One G2 case searched by keywords only (its search landed while the models were loading);
  `coach_search` now waits up to 20 s for loading models.
- Suites: core 70, eval 29, pack 10, coach 28, plugin 16.

## 2026-09-26: product id, repos, CI, Feedback, releases

Decided (ADR-0012): the product is the vertical founder coach; its name is one id in
`product.toml` (kept as `founder-coach`), filled into the `plugin/` template by the assembler.
Built: `coach_feedback` + `/founder-coach:feedback` (store v3), CI, `scripts/check_secrets.py`,
`scripts/release.py`, docs/release.md. Suites: core 70, eval 29, pack 10, coach 32, plugin 20.
Agreed order from here: repos + CI (you push) → Feedback (done) → first beta release →
web crawler (research and plan: [research/web-crawler.md](research/web-crawler.md); the 114 failed
YouTube fetches are 70 private videos that the next sync settles and 44 rate limits for `sync --backfill`) → usage log → Jev plan (compare with free
OpenRouter models on the 100-item sample before spending). M2c lite (G3) runs after the crawler.

## 2026-09-26: website Sources (plan: [web-sources-plan.md](web-sources-plan.md), ADR-0013)

Built: `type: website` Sources (config is `url`, and optionally `depth`, `render`, `enabled`,
`max_pages`), crawled politely (robots.txt per RFC 9309, 1 request/s per host or the site's
Crawl-delay, retries with Retry-After, conditional rechecks after 7 days, a host pause after 5
failures). Playwright is used for `render: always|auto`. Pages are ids `w-<16 hex>` of the
canonical URL, the same text at a new URL becomes an alias, and 404/410 tombstones the page.
YouTube videos embedded on pages go to YouTube sync. Articles run through the same
clean → extract → verify → index → pack steps and are cited with `¶n` and a `#:~:text=` link.
Every Source type plugs in through one adapter (`sync` + `clean`).
AC1–AC12 pass in `tests/test_web.py` (14 tests: mocked HTTP, a localhost server, the real
Chromium renderer in the cloud container). Live crawls followed on 2026-09-26/27 (paulgraham.com,
pmarchive.com, the YC Library); their fixes are in CHANGELOG.
Suites: core 70, eval 29, pack 10, coach 32, plugin 20, web 14.

## 2026-09-26: usage log ([usage-log.md](usage-log.md))

Built: a `usage` table in `founder.db` (store v4), one event per tool call, no Founder text unless
opted in, 90-day retention, `founder-coach usage summary|export|clear`. Crawl fixes from the first
multi-site run: YC Library video pages (a talk plus its transcript) go to YouTube instead of being a
second copy, start/archive/category pages are listings, child sitemaps inside a urlset, a 15 MB page
cap, depth increases reopen the old frontier. Suites: core 70, eval 29, pack 10, coach 37, plugin 20, web 20.
Found while planning M2c lite: Eval Moments are 2-minute YouTube windows with 11-character ids, so
articles need their own Moment (a run of paragraphs) before G1 can be re-measured or Holdout pooled;
the Stack Exchange data dump now needs a login and forbids redistribution and LLM training (2024), so
the Holdout source needs a decision (a pre-2024 CC BY-SA dump on archive.org, or other public questions).

## 2026-09-27/28: articles in the eval, cheaper coach eval, the coach answers first

- **Articles in the eval:** an Article's Moment is six paragraphs starting every third
  (`<doc>_p<n>`). After indexing the articles, `eval judge` graded 593 new Moments and released
  labels v1.3.0 (9,799 labels). On them `full` scores 0.568 nDCG@10.
- **G1 still fails:** the pack scores 0.471 (−0.098; the gate is 0.03). It can return only 74.2 % of
  the relevant Moments (Passages aren't in it), so recall@50 drops by 0.21 while mrr@10 holds.
- **`ytbrain eval coach` runs on the Claude plan, token-saving:** Haiku by default (`--model sonnet`
  to sign off), at most 12 turns per call, outside the repo. `ytbrain claude -- <args>` runs Claude
  Code the same way for plugin work.
- **The first Haiku runs fixed the contract** (CHANGELOG 2026-09-28): the coach answers, judges and
  cites in its first reply (rules 1, 4, 6), setup never delays an answer, the searches have a
  budget, the Citation format is inline in `coach`, a Check-in record warns about Commitments left
  open, and `eval coach` stops if `dist/plugin` is reassembled mid-run.
- **Build 20260928T040223Z on Haiku:** G2 100 % (20 cases), G4 90 %, G6 7/7, all pass; G5 1/3,
  failing on Check-ins before the Check-in fixes.
- Suites: core 72, eval 35, pack 11, coach 40, plugin 21, web 27.

## Next, in order

0. **Commit and push** (docs/release.md); check CI (seven suites green).
1. **G5 on a reassembled build:** `python scripts/assemble_plugin.py --pack data/pack --check`, then
   `ytbrain eval coach --gate g5` (don't reassemble while it runs). Then one full `ytbrain eval coach`
   on the final build, and `--model sonnet` as the sign-off before the beta.
2. **Dogfood and the fresh-machine install:** one live `ytbrain claude -- --plugin-dir dist/plugin`
   session (setup → ask → weekly-focus → check-in → feedback, status, export; a second session for
   the Nudge; `founder-coach usage summary`), the M3b exit. Then a fresh machine from
   `/plugin marketplace add` to the first cited answer, timing the first `uvx` start and `warmup`.
3. **M2c lite Holdout (G3):** about 30 questions from a pre-2024 CC BY-SA Startups Stack Exchange
   dump (archive.org), a third outside the corpus, pooled and judged; gate: at least 80 % correct
   declines.
4. **Beta packaging:** `scripts/release.py` into the marketplace repo, testers added as
   collaborators ([release.md](release.md)); at least 3 other founders.
5. **Website batch 1, after the first beta round** (gap-driven): Elad Gil, Andrew Chen, and First
   Round Review with a limit; then clean → extract → verify → index, a pack rebuild, `eval judge`,
   and `eval coach`.
6. **G1 pack gap:** experiment A `ytbrain pack build --with-passages --out data/pack-passages`, then
   `eval run --config pack --pack data/pack-passages --label passages`, `eval judge`, and `eval rescore
   --compare full` (Passages ship in the private beta only, ADR-0009). Experiment B: another
   embedding model on the passage-free pack. Record the choice in ADR-0009.
7. Later: the Jev plan document (ADR-0008), Stage-specific eval questions, enrichment.

## Future work noted along the way

- Article Eval questions (the Tuning set was generated from talks), and whether articles need their
  own pack settings.
- Cowork: the plugin is built and tested for Claude Code. In Cowork the server would run in its
  workspace, where `~/.founder-coach` may not persist; untested.
- Copies of these docs live in the claude.ai Project (`claude/ytbrain-*.md`); the repo files are the
  ones to keep.
