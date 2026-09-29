# Phase 2 plan — from verified corpus to founder coach

Status: **final — accepted 2026-09-23. M0 done (715 records, 99.4 % Evidence verified; sample pre-read found no fabrication, maintainer spot-check pending); M1 done (25 256 items indexed). M2 in progress: M2a (Moments, metrics, `eval run`) and M2b (Tuning-set build) built; M2c (Holdout) and M2d (lite/pack configs, release) next.** (Superseded as a status: M2d, M3a and M3b are built; the current state is in [m3-status.md](m3-status.md).) Vocabulary follows [CONTEXT.md](../CONTEXT.md);
decisions with lasting consequences are in [docs/adr/](adr/). [archive/phase2-parked.md](archive/phase2-parked.md) describes the parked
modules this plan builds on.

## 1. What v1 is

A coach that runs **inside Claude (Desktop or Code) through an MCP server and three Playbooks**,
used daily by one Founder (us first). It answers from Verified YC knowledge only, cites every
claim with a deep link, says what it doesn't know, and keeps track of the Founder's Stage,
Goals, Commitments and Decisions from week to week.

| Playbook | Founder says | Coach does |
|---|---|---|
| **Ask** | "How should I price a B2B pilot?" | Searches knowledge (boosted to the Founder's Stage), answers with Citations, lists Gaps |
| **Weekly focus** | "What should I focus on this week?" | Reads Founder profile + Goals + open Commitments, retrieves Stage-relevant Advice/Principles, proposes 1-3 Focus items with Citations, records accepted ones as Commitments |
| **Check-in** | "Weekly check-in" | Reviews last week's Commitments (done / dropped / carried), captures what happened and any Decisions, updates Stage/metrics if they changed, hands off to Weekly focus |

**Not in v1** (see §13): other Sources, Stage diagnosis, artifact review, web app, multiple Founders,
scheduled proactive nudges, Principle consolidation.

### Answer contract (every Playbook, checked by the eval)
1. Every recommendation carries at least one Citation to Verified Evidence (title, speaker, date, deep link).
2. Advice older than the Founder's context warrants is dated ("2014 — pre-AI tooling").
3. Anything the corpus doesn't cover is stated as a Gap, never filled from the model's own memory without saying so.
4. Conflicting Advice is shown as a disagreement, not averaged away.

## 2. Architecture

```
 Sources ─► ingestion Steps (sync·clean·extract·verify) ─► Document records (data/metadata)
                                                                 │
                                   index Step (new) ─────────────┤
                                                                 ▼
               Knowledge index (LanceDB: vector + full-text)   Thin graph (SQLite, projected)
                                         ╲                        ╱
                                          MCP server (retrieval + Founder state, no LLM)  ◄── Founder store (local, private)
                                                         │
                                         Host agent (Claude) + Playbooks  ◄──►  Founder
```

Package layout — **new code goes into new subpackages only; existing modules stay exactly
where they are** (moving them would break the tests, the launchd job, `ops/probe_models.py`
and the `ytbrain` entry point):

```
ytbrain/*.py        phase-1 ingestion (unchanged: cli, fetch, captions, verify, pages, manifest ...)
ytbrain/knowledge/  item builder, index Step, search (hybrid + rerank + boosts), graph   (new)
ytbrain/coach/      Founder store, Playbook definitions                                 (new)
ytbrain/serve/      MCP server (replaces the parked ytbrain/mcp_server.py)               (new)
ytbrain/judge/      Judge interface: typed judgements (Jev · LLM) [ADR-0008]             (new)
ytbrain/sources/    Source adapters, introduced with the second Source [ADR-0003]        (later)
skills/             Playbooks as Claude skill files                                     (new)
```

## 3. Knowledge items and the index Step

One row per Knowledge item [ADR-0003], only if Verified [ADR-0004]:

| field | notes |
|---|---|
| item_id | `adv:<doc>:<atom_id>`, `tkw:<doc>:<n>`, `psg:<doc>:<start>`, `sum:<doc>` |
| kind | advice · takeaway · passage · summary |
| text | the Advice/Takeaway text, Passage text, or Document summary |
| evidence | Verified quote (advice/takeaway) |
| locator | `{type: timestamp, ms}` for Talks; `{type: paragraph, n}` / `{type: page, n}` later |
| deep_link | URL that opens the Document at the Locator |
| doc_id, source_kind, series, title, speaker, published_at | Document context, denormalised for filtering and display |
| stages, stage_origin, stage_probs | Advice's Stages from the `enrich` Step with probabilities (`origin=judge`), else its own extracted Stages, else inherited from the Document (`origin=document`) [ADR-0005, ADR-0008] |
| topics | Advice/Document Topic(s); Document Category until enrichment adds per-item Topics |
| context_header | deterministic "From <title> by <speaker> (<series>, <year>) — <chapter>" |
| vector, indexable | embedding of `context_header + text`; full-text index on the same |
| embed_model, schema_version | reindex is triggered when either changes |

- **Step integration:** `index` becomes a checkpointed Step after `verify` (manifest already reserves it);
  `ytbrain run` gains it; `invalidate index` reindexes without touching extraction.
- **Embeddings:** local `sentence-transformers`, default `BAAI/bge-m3` (Apple-silicon MPS),
  configurable via `YTBRAIN_EMBED_MODEL`. Local = free, private, reproducible. Swap only on eval evidence.
- **Search:** vector top-50 + full-text top-50 → reciprocal-rank fusion → optional local cross-encoder
  rerank (`BAAI/bge-reranker-v2-m3`, on by default if installed) → soft boosts: Stage match,
  kind prior (Advice > Takeaway > Passage for coaching), light recency → diversity cap (≤3 items per
  Document). Hard filters only when asked (kind, Topic, Series, date range).
- **CLI:** `ytbrain index`, `ytbrain search "query" [--stage mvp] [--kind advice]` for debugging and eval.

## 4. Thin graph (projected, zero LLM) [ADR-0001]

Nodes: Document, Advice, Speaker, Company, Series, Stage, Topic. Edges: has_advice, by_speaker,
mentions (people/companies), in_series, applies_to_stage, about_topic. Used for "more from this
Talk/speaker", "everything mentioning Stripe", and later Principle consolidation. Rebuilt by the
index Step. Fixes needed in the parked `graph.py`: companies typed as Company (not Document),
books ignored, `graph.duckdb` name for a SQLite file, `contradictions()` ignores invalidated edges.

## 5. Founder store [ADR-0006]

> Superseded for M3 by phase3-plan §11.2 and ADR-0011 (location `~/.founder-coach/`, a change log, if-then Commitments). Kept for history.

Local SQLite at `~/.ytbrain/founder.db` (outside `data/`, override `YTBRAIN_FOUNDER_DB`), every row
keyed by `founder_id` (default `me`):

| table | fields |
|---|---|
| founder_profile | founder_id, company_name, one_liner, customer, stage, team, key_metrics (json), updated_at |
| goals | goal_id, founder_id, text, target_date, status (active/met/dropped), created_at |
| commitments | commitment_id, founder_id, week (ISO), text, goal_id?, status (open/done/dropped/carried), citations (json), created_at, closed_at |
| decisions | decision_id, founder_id, text, reasoning, citations (json), decided_at |
| checkins | checkin_id, founder_id, week, summary, wins, blockers, created_at |

A human-readable `~/.ytbrain/FOUNDER.md` is regenerated after every write so the Founder can read
and correct what the coach believes.

## 6. MCP tools v1 (small, stable surface)

| tool | purpose |
|---|---|
| `search_knowledge(query, stage?, kinds?, topics?, top_k=8)` | ranked Knowledge items with Citations |
| `get_document(doc_id)` | summary, chapters, Takeaways, Advice of one Document |
| `get_passage(item_id, expand=false)` | the Passage around a Citation (expand = neighbouring Passages) |
| `get_founder_context()` | profile + active Goals + open Commitments + last Check-in, in one call |
| `update_founder_profile(patch)` | Stage, metrics, one-liner, team |
| `set_goal(text, target_date?)` / `update_goal(goal_id, status)` | Goals |
| `add_commitments(week, items[])` / `update_commitment(id, status, note?)` | Commitments |
| `log_decision(text, reasoning, citations[])` | Decisions |
| `record_checkin(week, summary, wins, blockers)` | Check-ins |
| `corpus_status()` | what the corpus covers (Documents, Series, date range) — lets the coach state Gaps honestly |

**Query decomposition belongs to the host agent** (decided 2026-09-23): the Ask Playbook tells the
agent to split a multi-part question and call `search_knowledge` once per part. The server stays
one-query-in, ranked-list-out and model-free [ADR-0002]; a "rewrite only when retrieval is
weak" option can be tried later, measured on the Tuning set.

Playbooks ship twice from one source: as **MCP prompts** (portable to any MCP host) and as
**Claude skill files** in `skills/`. Graph tools stay internal until a Playbook needs them.

## 7. Eval (built before tuning anything) — decided 2026-09-23

> **The benchmark format is specified in [docs/eval-spec.md](eval-spec.md)** and takes precedence over the details
> below. The spec adds: labels on two-minute Moments, BEIR/TREC files, a third judge that settles
> disagreements (instead of dropping them), a depth-50 pool for Holdout questions, reference answers
> with nuggets, per-record licences, and a data card.

Two sets with different jobs, so that tuning can't overfit the numbers we report. Neither trains
a model; they choose search settings and catch regressions. **Both are built without manual
question writing or labelling**; every question and every expected answer carries a Citation.

| | **Tuning set** | **Holdout set** |
|---|---|---|
| Purpose | Choose settings: weights, candidate counts, embedder/reranker config, Gap threshold | Measure honestly at milestone ends; never used to choose anything |
| Questions | 150 generated from our corpus: one founder-style question per sampled Advice/Takeaway, stratified by Category, Stage, year and caption kind, at most one per Talk | At most 50 real questions from outside sources: YC Office Hours / partner Q&A posts, and the Startups + OnStartups Stack Exchange dumps (CC BY-SA 4.0). An LLM keeps only questions an early-stage founder would ask a coach, deduplicated and spread across Stages and Topics |
| Question citation | Deep link to the source item's Evidence | URL of the original post (author attributed for Stack Exchange) |
| Answer citations | Every item graded >= 2: Talk title, deep link to `start_ms` | Same |
| Out-of-corpus | — | Holdout questions whose pooled judging finds nothing graded >= 2 are kept as Gap questions (expected answer: a Gap) |
| Cost | ~$1 of LLM | ~$0.5 of LLM |
| Run | Every change (`ytbrain eval run --set tuning`) | Milestone ends only (`--set holdout`) |

**Generation rules (Tuning set).** The model writes the question from the item *without seeing the
quote*, as a founder would phrase the problem. A question sharing more than half its content words
with the item text or quote is rejected (copied wording flatters full-text search).

**Labels (both sets, automatic).** Pool the top 20 from five variants (full-text only, vector only,
hybrid, hybrid + rerank, and hybrid on an LLM rewrite of the question), ~30-60 candidates per
question. Two judges grade each candidate 0-3 with the UMBRELA prompt (TREC RAG 2024): the extract
model and a second model from a different family (Jev once M1.5 builds the Judge interface). The
grade is the lower of the two; candidates where they differ by 2 or more are dropped from scoring,
not guessed. Using two judges and taking the minimum limits one model grading its own retrieval
favourably. A label is (`doc_id`, `start_ms` ± 30 s, grade), so it survives re-extraction; a
Document summary counts by `doc_id`. A 15-minute human spot-check of 20 random labels is optional
and reported when done; nothing waits on it.

**Metrics.** Recall@10, nDCG@10 and MRR per set with `ranx` (paired significance tests).
Gap questions are scored separately: correct when the best reranker score falls below the Gap
threshold, which is calibrated on the Tuning set and later used by the coach. Gate: a change that
drops Tuning-set recall@10 by more than 0.05 fails. 50 Holdout questions detect ~0.07 nDCG@10
changes, not smaller ones; the set grows from real misses during dogfooding (M4).

**Configurations measured.** The current full config (bge-m3 + bge-reranker-v2-m3), no reranker,
the Knowledge pack without Passages, and at least one *plugin-lite* config (< ~500 MB of downloads),
to choose what the Claude plugin ships (ADR-0009).

**Files.** BEIR layout under `eval/`: `tuning/` and `holdout/`, each with `queries.jsonl` (question,
type, Stage, Topic, citation) and `qrels.tsv` (graded labels), plus `answers.jsonl` (the cited items
per question, readable). Committed to git. Run results stay in `data/eval/`.

**Not used:**
- **Ask HN:** HN's terms prohibit scraping, and the two sources above are enough.
- **The Hugging Face sets** `lucas-w/fa-startupschool` and `Glavin001/startup-interviews`: they cover
  ~35 Talks, are model-generated with the passage in view, and are unlicensed or non-commercial.
- **Multi-part questions:** they test the agent's decomposition, so they move to the M3 Playbook
  checks.

## 8. Compatibility guarantees (nothing that works today may break)

| Today | Guarantee |
|---|---|
| `ytbrain sync/clean/extract/verify/pages/report/sample/status/invalidate/run` | Same commands, flags and outputs. `run` gains an `index` Step at the end; if indexing fails (e.g. extra not installed) `run` still records sync→verify results and reports the index failure separately. |
| Core install `uv pip install -e ".[dev]"` | Stays light. Phase-2 deps (lancedb, sentence-transformers, mcp) stay in the optional `index` / `serve` extras; commands that need them say which extra to install. |
| `data/manifest.db` | No renames or destructive migrations. New Steps are new rows. The table/column names `stage_state`/`stage` stay [ADR-0007]. |
| `data/metadata/*.json`, `data/pages/*.md` | Schema only grows (additive fields); the Series/provenance backfill rewrites records atomically and is idempotent. |
| `.env`, `sources.yaml` | Existing keys keep their meaning; new keys have safe defaults. |
| Tests | Every milestone keeps all existing tests green and adds its own. |
| launchd job (`python -m ytbrain.cli run`) | Unchanged entry point. |

## 9. Failure handling and resume (phase-2 components)

Same rules as phase 1: mark done only after output is complete, write atomically, send damaged
input back upstream, stop cleanly on unrecoverable backend errors.

| Component | Failure | Handling |
|---|---|---|
| index Step | Ctrl+C / crash mid-Document | Per-Document checkpoint in the manifest; a Document's rows are replaced (delete + add) and marked indexed only afterwards, so a rerun redoes just that Document. LanceDB table versions keep readers on the last complete version. |
| index Step | Embedding model not downloaded / offline / out of memory | Preflight loads the model once before any Document; clear message naming the model and size; nothing marked failed. |
| index Step | Record changed after indexing (re-extract, backfill) | Input hash (record hash + embed model + schema version) stored per Document; mismatch makes it pending again. |
| search / MCP tools | Index missing, stale or being rebuilt | Tools return a structured error ("index not built — run `ytbrain index`") or serve the last complete table version; never a stack trace. |
| MCP server | Bad tool input | Validated with Pydantic; error payload explains the fix. Tool schemas are versioned; breaking changes bump the version. |
| Founder store | Concurrent writes, crash mid-write | SQLite transactions (WAL); every tool call is one transaction. |
| Founder store | Schema change | `schema_version` table + forward-only migrations run at startup. |
| Founder store | Data loss (it is NOT rebuildable) | SQLite backup API to `~/.founder-coach/backups/`: daily on first write (7 kept), before every migration and before `forget` (phase3-plan §11.2). |
| Pipeline lock vs MCP server | `ytbrain run` rebuilding while the coach is in use | The server only reads the corpus; writes go to the separate Founder store, so the pipeline lock never blocks the coach. |
| eval | Interrupted, LLM-judge failures | Retrieval metrics are deterministic and rerunnable; LLM-judge verdicts cached per (question, answer hash), retried with the existing backoff. |
| Judge (enrich Step) | Provider down / quota / API change | Same backoff and clean stop as extract; results checkpointed per Document with judge name + version; switching backend (e.g. Jev → LLM) re-runs only `enrich`. |

## 10. Agent files, skills, hooks and plugin packaging

| File | Purpose | When |
|---|---|---|
| `AGENTS.md` | Canonical instructions for coding agents (Claude Code, Codex, Cursor): setup, test command, never read `.env`, bump `SCHEMA_VERSION` on schema edits, write atomically, glossary in `CONTEXT.md`, decisions in `docs/adr/` | M0 |
| `CLAUDE.md` | One line importing `AGENTS.md` (`@AGENTS.md`) plus Claude-only notes, so both ecosystems share one source | M0 |
| `CONTEXT.md` | Domain glossary (done) | kept current in every milestone |
| `docs/adr/` | Decisions (0001-0010) | as decisions arise |
| `skills/{ask,weekly-focus,check-in}/SKILL.md` | The Playbooks for Claude hosts | M3 |
| MCP prompts | Same Playbooks for non-Claude MCP hosts, generated from the skill files | M3 |
| `.claude-plugin/plugin.json` + `.mcp.json` | **Package the coach as a Claude plugin** (skills + MCP server config) so any founder installs it in one step — the distribution form this project is aiming for. Minimum configuration: `.mcp.json` starts the server with `uvx`; on first use it downloads the Knowledge pack and the query models to `~/.ytbrain/`; no API key (the host model writes answers, ADR-0002); the Founder store is created on first write (ADR-0006). Running the pipeline stays optional, for maintainers and for private Sources | M3 |
| Hooks | None in v1. A SessionStart hook that injects Founder context is tempting, but an explicit `get_founder_context` call is portable to every MCP host and visible to the Founder. Revisit after dogfooding. | revisit M4 |
| CI | GitHub Actions: tests on macOS + Linux for every PR | before public release |

## 11. Eval set: feasibility and effort

| Part | Work | Human time |
|---|---|---|
| Tooling | `ytbrain eval build --set tuning|holdout` (sample or collect, generate/filter, pool, two-judge grading), `ytbrain eval run` (ranx) | ~1 day of build |
| Tuning set | 150 generated questions, graded automatically | none |
| Holdout set | <= 50 collected questions, graded automatically | none (optional 15-min spot-check) |
| **Total** | | **0 required human time, ~1 day build, ~$1.5 of LLM** |

Prerequisite (met): `verify` has run on the final 2.2.0 extraction.

## 12. Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | **Finish phase 1** (open: schema 2.2.0 re-extraction + sample read) — current extract run, Series + provenance fix and backfill, `verify` on all records, `report`, human sample read; `AGENTS.md` + `CLAUDE.md` | report gates pass; 10-record read finds no fabrication |
| M1 | **Knowledge index** — item builder, `index` Step, embeddings, hybrid search + rerank + boosts, `ytbrain search` (graph fixes moved to M5, the first milestone that uses the graph) | `ytbrain search` returns sensible cited items for 10 ad-hoc questions; index rebuild < 15 min on M2 |
| M2 | **Eval** (moved before M1.5, 2026-09-23; v1 scope trimmed 2026-09-24 in phase3-plan §0: Tuning set, pack configs, a 30-question Stack Exchange Holdout; dataset publishing after the beta) — Tuning and Holdout sets (§7), `ytbrain eval`, baselines for the full and plugin-lite configs | baselines recorded; search tuned once on the Tuning set; Holdout reported |
| M1.5 | **Judge + enrichment** [ADR-0008] (reshaped 2026-09-24: an LLM-only Stage/Topic tag, conditional on eval numbers, after M3a; see phase3-plan §0) — Judge interface with Jev and LLM (DeepSeek) backends; `enrich` Step giving every Advice item its own Stage(s) and Topic(s) (multi-label, with probabilities), its **audience** (founder / investor-or-mentor / employee / engineer) and its **kind** (actionable / anecdote / call to apply); evidence-support check stored as a score; ranking uses these as boosts, nothing is deleted | Stage coverage > 90%; Tuning-set nDCG@10 not worse, Stage-specific questions better; winning Judge per task chosen on a 100-item labelled sample |
| M3 | **Coach v1** (detailed in [phase3-plan.md](phase3-plan.md), which replaces §6 and the packaging row of §10) — Founder store (migrations, backups), MCP server with the §6 tools, three Playbooks (Ask splits multi-part questions and searches once per part), answer contract, packaged as a Claude plugin with a prebuilt Knowledge pack (ADR-0009) | Ask / Weekly focus / Check-in work end-to-end in Claude Code and Claude Desktop after one plugin install and no configuration: no API key, no pipeline run |
| M4 | **Dogfood 2 weeks** on our own startup; every miss logged as an eval case or backlog item | ≥ 10 real Check-in/Ask sessions; top 5 issues fixed |
| every | Tests green + new tests, README, `.env.example`, CHANGELOG and `CONTEXT.md` updated | reviewed before the milestone is closed |
| M5 | **Principles** — thin-graph fixes (§4), then consolidate Advice across Documents with support and dissent (Judge: "same recommendation?" / "contradicts?"); measure a query-time Judge (rerank) against the local cross-encoder | eval shows a gain on synthesis questions and no regression |

## 13. Future backlog (kept here so scope stays honest)

**Sources** (each = a Source adapter producing Documents + Passages + Locators, ADR-0003)
- YC Library essays, Requests for Startups, Paul Graham essays (web: Crawl4AI or Trafilatura)
- YC podcasts / other channels (same YouTube adapter; audio-only needs ASR — faster-whisper)
- Books the Founder owns (PDF/EPUB via Docling), quoted sparingly, private use
- The Founder's own material: meeting notes, investor updates, customer interviews — a private Source, never mixed into shared indexes
- Freshness policy per Topic (e.g. AI-era tooling advice decays faster than cofounder advice)

**Use cases** (each = a Playbook over the same tools)
- Stage diagnosis; YC application / pitch deck / cold email review; fundraising pipeline + investor update drafting;
  hiring plan; customer-discovery interview prep and synthesis; board/advisor meeting prep; weekly investor-update writer

**Knowledge quality**
- LLM contextual headers (Contextual Retrieval) if eval shows need
- Principle consolidation, contradiction/refinement edges, temporal validity (advice superseded over time)
- Speaker/company entity resolution (aliases), Topic taxonomy review from the `other` bucket
- Punctuation restoration for auto-captions; human-caption preference already in place

**Platform**
- Scheduled Check-in nudges (launchd / Claude scheduled tasks); hooks once Playbooks settle; web app with its own agent loop over the same tools;
  multi-Founder hosting (auth, per-founder stores — ADR-0006 makes this a storage change);
  optional gbrain as a Founder's personal memory; query-time Judge uses (routing, rerank) once measured [ADR-0008]
- Package rename once Sources go beyond YouTube (`ytbrain` → a neutral name); licence and public release (see §14)
- YouTube cookies (opt-in, throwaway account) for higher sync rate limits

## 14. Decisions reserved for the maintainer (none block M0-M3)

- ~~Licence~~ decided: Business Source License 1.1, converting to Apache-2.0 on 2030-01-01 (see `LICENSE`). Public project name still open.
- ~~Monthly LLM budget~~ decided: $10/month for extraction, enrichment and eval (actual spend so far is a few dollars).
- Whether the public Knowledge pack may include short Evidence quotes (ADR-0009; attribution + deep link each).
