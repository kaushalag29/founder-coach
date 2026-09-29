# Phase 3 plan — the founder coach as a product

Status: **accepted 2026-09-24; §0 sets the v1 scope.** It builds on [phase2-plan.md](phase2-plan.md) (M3 onward) and
replaces its §6 tool list and the packaging row of §10. Vocabulary: [CONTEXT.md](../CONTEXT.md).
Decisions: ADR-0002 (the server retrieves, the host generates), ADR-0004 (Verified only), ADR-0006
(local, founder-keyed state), ADR-0009 (Knowledge pack), ADR-0010 (one light runtime, every host).
Sources are cited inline; research dates are September 2026.

## 0. Ship path: what's required for v1 and what isn't (decided 2026-09-24)

**v1 = a private beta.** You and a handful of founders install the plugin in Claude Code (Cowork
if it loads the same plugin) from a private GitHub plugin marketplace. It is also the M4 dogfood.
Public release (PyPI, MCP Registry, Desktop bundle, plugin directory) comes after the beta and
needs the two decisions left in §10.

**Quality gates before any other founder uses it:**

| Gate | What must hold |
|---|---|
| G1 Retrieval | Tuning-set baseline recorded, and the lite pack within 0.03 nDCG@10 of it |
| G2 Citation support | at least 90 % of cited claims supported by their quote |
| G3 Gaps | at least 80 % correct declines on Holdout questions the corpus can't answer |
| G4 Sycophancy | at least 8 of 10 weak plans challenged, with a Citation |
| G5 Memory | 3 simulated founders × 3 weekly cycles end with the correct store state (one run each; pass^3 before public release) |
| G6 Decomposition | 6-8 multi-part questions: every part searched and cited |

**Required, in order (about 12 focused days to the beta):**

| # | Step | Days | Scope |
|---|---|---|---|
| 1 | Finish the Tuning set (running) | — | resume to the end; `eval run --set dev --save-baseline`; compare `no-rerank` and `stage-boost` once and keep the best. No parameter sweeps |
| 2 | Housekeeping | 0.5 | your 10-record read (M0, 30 min); a shared rate limit that adapts (halves on a 429 burst, climbs back after quiet minutes) — done 2026-09-24 |
| 3 | M2d core | 2 | `ytbrain pack build`, a search module shared by pipeline and runtime, one ONNX embedding model with and without the reranker, `eval run --config pack` → G1. Code done 2026-09-24 (bge-base-en-v1.5 + jina-reranker-v1-turbo-en, about 45 MB pack + 0.36 GB models); the measurement waits for the finished Tuning set |
| 4 | M3a | 4 | runtime package, founder store (migrations, backups, FOUNDER.md, export/forget), the 7 tools, prompts, resources, contract tests. Done 2026-09-24: `founder_coach/{domain,store,nudges,server,cli}.py`, 16 tests incl. stdio under both protocols and 3 concurrent writer processes |
| 5 | M3b | 2-3 | `founder-coach`, `ask` (host-side decomposition), `weekly-focus`, `check-in` and `setup` skills; `/coach:*` commands; SessionStart hook. In the repo 2026-09-25 (`plugin/`, `scripts/assemble_plugin.py`, 15 tests; `claude plugin validate --strict` passes); exit waits on one real session, see [m3-status.md](m3-status.md). M3a's three gaps (pack checksum, damaged store + `restore`, ONNX fallback) closed the same day |
| 6 | M2c lite | 1 | about 30 Startups Stack Exchange questions, a third outside the corpus, pooled and judged; no reference answers yet |
| 7 | M3d core | 2 | answers produced by `claude -p` with the plugin installed (the real host); judged with the eval's non-generator judges → G2-G6 |
| 8 | Beta packaging | 1 | the private GitHub repo (you run git) and the release script; see "Beta distribution" below; a fresh-machine test from install to first cited answer |
| 9 | Beta = M4 | 2 wks | at least 10 real sessions from you plus at least 3 other founders; every miss becomes an eval case |

**Beta distribution** (verified in step 8, replaces §6's release job for now): one private GitHub
repo holding the plugin, the runtime source and the pack. The plugin starts the server with
`uvx --from ${CLAUDE_PLUGIN_ROOT} founder-coach`, so testers need git access only: no PyPI, no
separate download, and the pack never becomes public. The dev repo stays separate; a script
publishes each release to the beta repo.

**Conditional, decided by numbers:**
- **Stage-specific eval questions:** the dev set has only 3 of 150 (the generator picked "how do
  I…" 146 times). Before the enrichment decision, add about 40 questions where the Founder's
  Stage is stated (about $0.50, alongside step 6), and give generation fixed type quotas from
  then on (`gen-v2`). Until then the Stage boost stays as it is: soft, and measured harmless on dev.
- **Enrichment (M1.5 lite):** after step 4. Tag each Advice item's Stage and Topic with the LLM
  backend (DeepSeek, about $1-2). Keep the tags only if nDCG@10 doesn't drop and the Stage-specific
  Tuning questions improve. Audience, kind and the support score wait.
- **`citation-auditor` subagent:** becomes required if G2 fails.
- **Holdout reference answers:** only once we score key-fact coverage.

**Optional (after the beta or never):** public release (M3c: PyPI, `.mcpb`, MCP Registry, CI),
dataset publishing (data card, Croissant, `eval hydrate`, v1.0.0 tag), Jev (ADR-0008, parked),
the Judge interface, the scheduled weekly nudge, YC Office Hours collector, M5 principles and
graph, M6 other hosts, the remote connector, YouTube backfill, persona pass^3.

## 1. The product in one paragraph

A founder installs one plugin in Claude Code or Claude Desktop, with no API key, no pipeline run
and no configuration. From then on the host agent can:
- **coach** with Verified YC knowledge, every recommendation cited to the second of a talk;
- **remember** the Founder's Stage, Goals, Commitments and Decisions week to week, in a local
  store the Founder can read, correct, export and delete;
- **run three Playbooks:** Ask, Weekly focus and Check-in;
- **nudge** gently when a Check-in is overdue.

The same server and skills later install in Cursor, Kiro, Codex, VS Code/Copilot and Gemini CLI
through thin per-host manifests.

## 2. Architecture

```
 host agent (Claude Code / Desktop / Cursor / Kiro / Codex / Copilot / Gemini CLI)
   │  skills (SKILL.md) · slash commands · SessionStart hook · optional subagent
   │  MCP: tools · prompts · resources
   ▼
 founder-coach runtime (Python, launched with `uvx`, no torch, no yt-dlp)   ← this phase
   ├─ knowledge: Knowledge pack (one SQLite file: items + FTS5 + vectors) + ONNX query models
   └─ founder store: ~/.founder-coach/founder.db (+ FOUNDER.md, backups)
 ytbrain pipeline (maintainer only: sync → … → index → eval → pack build)  ← phases 1-2
```

**Two packages from one repo** (ADR-0010):
- **Runtime** (`founder_coach`): MCP server, pack reader, search, store. Light dependencies:
  `mcp>=2.2`, numpy, onnxruntime-based embeddings (e.g. fastembed), platformdirs. About 40 MB of
  pack data ships inside the wheel, so nothing is downloaded at install time except the small
  query models on first use.
- **Pipeline** (`ytbrain`): everything built so far, plus `ytbrain pack build`, which produces the
  pack the runtime ships.

**Search is identical in both.** The runtime's pack search implements the same interface as the
LanceDB store: RRF fusion, reranking, boosts and the diversity cap in one shared module. M2d
measures exactly the runtime path (lite ONNX models, no Passages) against the full setup, and the
plugin ships the lite setup unless it loses > 0.03 nDCG@10 (ADR-0009).

## 3. MCP surface (replaces phase2-plan §6; build details in §11.3)

Following Anthropic's tool-writing guidance: a few workflow-shaped tools, one name prefix, compact
responses with a `response_format` switch, `outputSchema` plus the same JSON as text, and errors
that say what to do next ([writing tools](https://www.anthropic.com/engineering/writing-tools-for-agents),
[MCP tools spec](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)). The
server targets MCP 2026-07-28, which is stateless: every state handle goes in the arguments, and
there is no sampling ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)).

**Tools (7, stable order):**

| tool | does | notes |
|---|---|---|
| `coach_search(query, stage?, kinds?, topics?, top_k=8, response_format)` | ranked Knowledge items with structured Citations | returns `retrieval_confidence` and `gap_suspected` from the Gap threshold calibrated in M2; stays under ~3k tokens by default |
| `coach_read(ref, expand=false)` | one Document (summary, chapters, Advice) or the context around a Citation | the pack has no Passages: returns the Evidence quote and deep link instead |
| `coach_get_context()` | profile + active Goals + open Commitments + last Check-in + what's due or stale | one call at the start of every Playbook |
| `coach_update_profile(patch)` | Stage, one-liner, customer, team, metrics | every change is versioned; nothing is overwritten |
| `coach_record(entry)` | a new Goal, Commitment(s), Decision or Check-in | one typed union; Commitments are if-then plans with a measurable outcome |
| `coach_update(id, status?, fields?, note?)` | status (done / dropped / carried / met) or a correction | the note is the evidence ("shipped pricing page, 3 calls booked"); every change logged (§11.2) |
| `coach_corpus_status()` | what the corpus covers (Talks, Series, years, freshness) | lets the coach state Gaps honestly |

**Prompts** (show up as `/` commands in hosts without skills): `ask`, `weekly-focus`, `check-in`,
`setup`.

**Resources** (can be @-mentioned): `founder://profile`, `founder://this-week`, `corpus://status`.

**Response rules:**
- Citations are data (talk, speaker, date, start/end seconds, deep link, quote), never prose.
- Only retrieved items can be cited.
- Nothing returns more than ~10k tokens (Claude Code warns there).

## 4. Skills, commands, hooks, subagents (Claude first)

| Piece | Name | What it does |
|---|---|---|
| **Skill** | `founder-coach` | The coaching method, loaded when a founder asks for startup advice. Contents: the answer contract (phase2-plan §1: cite everything, date old advice, name Gaps, show disagreements), anti-sycophancy rules (challenge a weak plan, name one thing the founder may be avoiding, never praise without evidence), and "ask one clarifying question when the profile can't answer it" |
| **Skill** | `ask` | Split multi-part questions (decided 2026-09-23), one `coach_search` per part, read the top items, answer with Citations, list Gaps |
| **Skill** | `weekly-focus` | Read context, propose 1-3 Focus items as **if-then plans with a measurable outcome** (goal-setting evidence: [CHI 2026](https://arxiv.org/html/2602.08636)), cite each, record the accepted ones |
| **Skill** | `check-in` | Review each Commitment with evidence (done / dropped / carried), capture Decisions and blockers, update Stage and metrics if they changed, then hand off to weekly-focus. Recorded progress tracking is the strongest effect in the literature ([meta-analysis](https://www.eurekalert.org/news-releases/598512)) |
| **Skill** | `setup` | First-run interview (company, customer, Stage, one Goal, preferred check-in day); offers the weekly scheduled task |
| **Commands** | the skills themselves (`/<plugin>:ask`, `:weekly-focus`, `:check-in`, `:setup`) plus user-only `status`, `export`, `forget` skills | In Claude Code, slash commands are skills; no `commands/` folder (§11.6) |
| **Hook** | `SessionStart` → `founder-coach hook session-start` | Local and instant, no LLM: adds one line of context when something is due ("Check-in overdue by 3 days · 2 open Commitments · profile last confirmed 34 days ago"), silent otherwise ([hooks](https://code.claude.com/docs/en/hooks)) |
| **Nudge** | weekly Desktop scheduled task (opt-in during setup) | Runs `/coach:checkin` on the chosen day. Desktop tasks run locally and can read the store; cloud routines can't, so they're not used ([scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks)) |
| **Subagent** (v1.1) | `citation-auditor` | Re-reads each cited item with `coach_read` and flags claims their Evidence doesn't support, before the answer is shown. Worth it because 50-90 % of LLM answers in one study weren't fully supported by their citations ([Nature Comms 2025](https://www.nature.com/articles/s41467-025-58551-6)) |
| **Output style** (later) | `coach` | Only if dogfooding shows the tone needs it |

**Rules for the pieces:**
- Skills carry the method; tools stay thin.
- The skill text is written once and reused by every host (Agent Skills is an open standard:
  [agentskills.io](https://agentskills.io)).
- MCP prompts are generated from the same files for hosts without skills.

## 5. Founder store (refines phase2-plan §5; schema and rules in §11.2, ADR-0011)

- **Location:** `~/.founder-coach/founder.db`, shared by every host on the machine
  so one Founder has one brain. `FOUNDER_COACH_HOME` overrides it.
- **Typed, temporal records.**
  - Every row has `created_at`, `source` (which Playbook or tool wrote it) and, for
    profile facts, `valid_from` / `superseded_by`. A change never overwrites; the previous
    value is kept ([Zep bi-temporal](https://arxiv.org/abs/2501.13956)).
  - Staleness: profile facts older than 30 days are flagged in `coach_get_context` and in the
    hook, so the coach asks the Founder to confirm them rather than trust them.
- **The Founder stays in control:**
  - `FOUNDER.md` is regenerated after every write, for the Founder to read.
  - `founder-coach export` produces JSON + Markdown; `founder-coach forget` does a full delete after
    a typed confirmation and a final backup. Neither is an MCP tool (§11.5).
  - No telemetry.
- **Robustness:**
  - Schema migrations are numbered, one version table, forward-only.
  - WAL mode; every write in a transaction.
  - A daily rotating backup (7 kept) on the first write of the day.
  - Writes go through the tools only.
- **Privacy:**
  - Founder data goes only to the host model the Founder chose. It never reaches a Judge
    (ADR-0006/0008), a log or the pack.
  - The README says plainly which model sees it.

## 6. Knowledge pack and release (ADR-0009)

- **`ytbrain pack build`** writes one SQLite file:
  - Advice, Takeaways and summaries (Verified only, no Passages), each with its Citation;
  - an FTS5 table and float32 vectors from the lite embedding model;
  - a manifest with the schema version, items version, model ids, counts, date and a sha256.
  - Size: about 13k items, roughly 30-40 MB.
- **Search in the runtime:** brute-force cosine similarity over ~13k vectors takes milliseconds in
  numpy; no vector database is needed at this size.
- **Shipped inside the runtime wheel,** so pack and code are versioned together. A future larger
  corpus moves to a checksummed download to `~/.founder-coach/packs/` (atomic swap; the old pack
  is kept until the new one verifies).
- **One CI release job** (GitHub Actions, macOS + Linux) produces:
  - the PyPI wheel;
  - the `.mcpb` bundle for Claude Desktop (`server.type: "uv"`; the host manages Python —
    [MCPB](https://github.com/modelcontextprotocol/mcpb));
  - the Claude plugin marketplace entry;
  - the Agent Plugins manifest;
  - an MCP Registry `server.json` ([registry](https://modelcontextprotocol.io/registry/package-types)).

  Every manifest's version is bumped from one source.

## 7. Hosts and packaging

| Host | How it installs | What ships | When |
|---|---|---|---|
| **Claude Code** | `/plugin marketplace add <org>/<repo>` then `/plugin install founder-coach@…` | `.claude-plugin/plugin.json` (inline `mcpServers` → `uvx founder-coach@X.Y.Z`), `skills/`, `commands/`, `hooks/hooks.json` ([plugins](https://code.claude.com/docs/en/plugins-reference)) | M3 |
| **Claude Desktop** | Double-click the `.mcpb` or install from the directory | MCPB bundle (tools + prompts). Desktop scheduled tasks for the nudge | M3 |
| **Cursor, Kiro, Codex, VS Code/Copilot** | Their plugin/marketplace commands | Root `plugin.json` + `mcp.json` in the Agent Plugins 1.0 format ([spec](https://agent-plugins.org/specification)) + the same `skills/` | M6 |
| **Gemini CLI** | `gemini extensions install <repo>` | `gemini-extension.json` + the same `skills/` + `GEMINI.md` pointer | M6 |
| **Everything else** | `npx add-mcp` / `npx skills add` | The PyPI server + skills | M6 |
| **claude.ai web (remote connector)** | Settings → Connectors | A hosted Streamable-HTTP server with OAuth and per-founder storage; needs a hosting and licence decision | later |

**Rules:**
- Start the server only as `uvx founder-coach@<pinned>`. This avoids three different
  plugin-root variables.
- `uv` is the one prerequisite outside Desktop's uv bundle: `setup` checks for it and prints
  the one-line install.

## 8. Evaluating the coach (M3 exit, M4, then every release)

Most of these run as `claude plugin eval` suites with mocked tools; the multi-week persona check stays a custom harness (§11.9).

Retrieval is already measured (eval-spec). Answers are measured on top of it:

| Check | How | Gate |
|---|---|---|
| **Citation support** | Every sentence with a Citation checked against its Evidence by the judges (TREC RAG support style) | ≥ 90 % supported |
| **Key-fact coverage** | Holdout reference answers' key facts (eval-spec §5), vital facts covered | reported; must not drop between releases |
| **Gaps** | Out-of-corpus Holdout questions must be declined, not answered from memory | ≥ 80 % correct abstention |
| **Sycophancy probes** | 10 founders with a weak plan ("I'll raise before talking to users") must be challenged, with a Citation | ≥ 8/10 |
| **Multi-week personas** | 5 simulated founders × 3 weekly cycles, driven headless (`claude -p` with the plugin installed); graded on the final state of the store (τ-bench style: right Commitments recorded, statuses updated) plus a rubric, run 3 times each (pass^3) ([evals guide](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)) | all pass^3 on state; rubric ≥ 4/5 |
| **Tool contract tests** | Every tool against the MCP inspector and the Python SDK client; response size ≤ 10k tokens | in CI |

**Setup:**
- These run on the maintainer's machine; persona runs use the maintainer's Claude account.
- Judges are the same non-generator families as eval-spec.
- Every dogfooding miss becomes a new case.

## 9. Milestones (continues phase2-plan §12)

| # | Milestone | Done when |
|---|---|---|
| M2c-d | Holdout set, lite/pack configurations (measuring the **ONNX runtime path**), eval release v1.0.0 | plugin setup chosen by numbers |
| M1.5 | Judge + enrich (per-item Stage/Topic/audience/kind) | as in phase2-plan |
| **M3a** | Runtime package: pack format + `ytbrain pack build`, shared search module, founder store (migrations, backups, FOUNDER.md), MCP server with the 7 tools, prompts and resources; contract tests | tools pass contract tests; `eval run --config pack` matches the M2d numbers |
| **M3b** | Skills, commands, SessionStart hook, setup flow and scheduled-task offer | the three Playbooks work end to end in Claude Code from a local plugin install |
| **M3c** | Packaging + CI release: plugin marketplace, `.mcpb`, PyPI wheel, MCP Registry | fresh-machine test: install to first cited answer in < 5 minutes, no configuration |
| **M3d** | Coach evaluation harness (§8) + baseline | gates met |
| M4 | Dogfood 2 weeks (≥ 10 Check-ins/Asks); each miss becomes an eval case | top 5 issues fixed |
| M5 | Principles (consolidated Advice with dissent) + thin graph | eval gain on synthesis questions |
| **M6** | Other hosts: Agent Plugins manifest, Gemini extension, `add-mcp`/`skills` install docs; `citation-auditor` subagent | install + one Playbook verified in each host |
| later | Remote connector for claude.ai; more Sources (YC essays, PG essays, podcasts); new Playbooks (YC application review, investor updates, customer-interview prep); memory compaction of old Check-ins | — |

## 10. Decisions reserved for the maintainer

Both are needed only for the public release (§0); the beta doesn't depend on them.

1. **Public name** for the runtime package, plugin and registry entry (`founder-coach` is a
   placeholder; `ytbrain` stays the pipeline's name).
2. **Short quotes in the public pack.** Each is attributed and deep-linked (ADR-0009).

Decided 2026-09-24: the coach evaluation drives the real host, `claude -p` on the maintainer's
Claude account (§0 step 7).

## 11. M3a/M3b implementation spec (decided 2026-09-24)

This section is the build spec. Where §3-§5 differ, it wins.

> **Changed since (2026-09-25 to 09-28; the code and CHANGELOG are current):**
> - an 8th tool, `coach_feedback`, and a 9th skill, `feedback`;
> - the contract skill is `coach` (ADR-0012), and its wording lives in
>   `plugin/skills/coach/SKILL.md`: rule 8 changed on 09-25 (dictated values count as a yes), and
>   rules 1, 4 and 6 on 09-28 (answer and judge in the first reply, search before advising, answer
>   for the default and then ask);
> - a local usage log with no Founder text, kept 90 days ([usage-log.md](usage-log.md)); nothing
>   leaves the machine;
> - `coach_search` waits up to 20 s (`FOUNDER_COACH_SEARCH_WAIT_S`) for models that are still
>   loading before it answers keyword-only;
> - CLI `restore`, `feedback list|export`, `usage summary|export|clear`, and
>   `forget --confirm COMPANY`;
> - the pack is looked for in `$FOUNDER_COACH_PACK`, then the plugin's `pack/`, then the repo's
>   `data/pack`, then `~/.founder-coach/pack`.

It follows the 2026 guidance on:
- Claude Code plugins, skills, hooks and plugin evals: [plugins](https://code.claude.com/docs/en/plugins-reference), [skills](https://code.claude.com/docs/en/skills), [hooks](https://code.claude.com/docs/en/hooks), [plugin evals](https://code.claude.com/docs/en/plugin-evals);
- the MCP 2026-07-28 spec and Python SDK 2.2: [changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog), [tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), [SDK v2](https://py.sdk.modelcontextprotocol.io/whats-new/);
- the Agent Skills standard: [spec](https://agentskills.io/specification), [authoring](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices);
- Anthropic's posts on [writing tools](https://www.anthropic.com/engineering/writing-tools-for-agents) and [evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).

### 11.1 Layout

```
founder_coach/            runtime package; never imports ytbrain (ADR-0010)
  search.py pack.py models.py        done in M2d
  store.py                founder store: schema, migrations, change log, backups, FOUNDER.md
  nudges.py               what's due, computed from the store, never stored
  server.py               MCPServer: 7 tools, 4 prompts, 3 resources + 1 template
  cli.py                  founder-coach serve | hook session-start | warmup | status | export | forget
plugin/                   what a founder installs (plus the runtime and the pack, added by the assembler)
  .claude-plugin/plugin.json
  .mcp.json               uvx --from ${CLAUDE_PLUGIN_ROOT} founder-coach serve
  pyproject.toml          runtime deps only: mcp>=2.2,<3, numpy, fastembed, pydantic
  skills/<name>/SKILL.md  (+ references/, assets/)
  hooks/hooks.json
  evals/                  `claude plugin eval` cases + mocks (M3d)
scripts/assemble_plugin.py   plugin/ + founder_coach/ + pack -> dist/plugin (dev: `claude --plugin-dir dist/plugin`;
                             beta: the same folder is published to the private beta repo)
tests/test_coach.py
```

There's no `commands/` folder. In Claude Code, slash commands are skills.

### 11.2 Founder store (ADR-0011)

`~/.founder-coach/founder.db` (`FOUNDER_COACH_HOME` overrides the folder). Every row has `founder_id`, `'me'` by default (ADR-0006).

| Table | Holds |
|---|---|
| `profile_facts` | One row per value of a fact: `field` (company, one_liner, customer, stage, team_size, key_metrics, timezone, checkin_day), `value`, `valid_from`, `superseded_at`, `confirmed_at`, `source`. The current value is the row with no `superseded_at` |
| `goals` | `text`, `measure`, `target_date`, `status` (active / met / dropped), `citations`, `created_at`, `closed_at` |
| `commitments` | `week` (ISO, in the Founder's time zone), `action`, `cue` (the "if/when" of the if-then plan), `outcome` (measurable), `goal_id`, `citations`, `status` (open / done / dropped / carried), `carried_from`, `result_note`, timestamps |
| `decisions` | `text`, `reasoning`, `citations`, `decided_at`, `revisit_on` |
| `checkins` | `week`, `summary`, `wins`, `blockers`, `created_at` |
| `changes` | Append-only log: `seq`, `at`, `entity`, `entity_id`, `op` (create / update / confirm / status), `before`, `after`, `source`. Retries go through a separate `requests` table: `request_id` (unique) → the first result plus a fingerprint of the write, so a retry of the same write replays it and reusing the id for a different write is refused |

**Rules:**
- **Ids** are short and readable (`g-7f3a`, `c-…`, `d-…`, `k-…`).
- **Writes:** each write is one `BEGIN IMMEDIATE` transaction that includes its `changes` row.
- **Concurrency:** WAL, `busy_timeout` 5 s. Several hosts' servers and the hook share the file safely.
- **Migrations** are numbered, forward-only, and run under an exclusive lock after a backup.
- **Backups:** the SQLite backup API, on the first write of each day (7 kept), before every migration and before `forget`.
- **FOUNDER.md** is regenerated atomically after every write, including a "recent changes" section.
- **Carrying a Commitment** closes it as `carried` and opens a linked copy in the next week. The response reports how many weeks in a row it has been carried.
- **Confirming a fact:** re-stating the same value logs a `confirm` and resets that fact's 30-day staleness clock.
- **Time:** weeks are ISO weeks in the profile's time zone (the system's, captured at setup); timestamps are UTC.
- **Nudges** are computed on read:
  - a Check-in is overdue (the preferred day has passed with no Check-in this week, or none in 8 days);
  - profile facts are stale;
  - Commitments from earlier weeks are still open;
  - a Decision is due for a revisit;
  - no profile yet, so setup is needed.
- **Soft limits:** at most 3 open Commitments per week and 3 active Goals. A write over the limit still happens and returns a `warnings` entry that the skill acts on.

### 11.3 Tools

All tools:
- are prefixed `coach_`, in this fixed order;
- have static descriptions and a cache hint;
- return compact text plus `structuredContent`;
- stop well under Claude Code's 10k-token warning.

Errors are `ToolError`s that say how to fix the call.

| # | Tool | Annotations | Arguments → returns |
|---|---|---|---|
| 1 | `coach_search` | read-only, idempotent, closed-world | `query`, `stage?`, `kinds?`, `topics?`, `top_k` 5 (1-15), `response_format` concise/detailed → hits (item_id, kind, text, quote, talk, speaker, year, deep_link, start_s, relevance), `top_similarity` (named so in the shipped schema), `gap_suspected`, `mode` semantic/keyword |
| 2 | `coach_read` | read-only, idempotent | `ref` (an item_id or doc_id) → the talk's summary and all its Verified items, with quotes and links |
| 3 | `coach_get_context` | read-only | `response_format` → today, week, current profile (stale facts flagged), active Goals, this week's and overdue Commitments, last Check-in, 5 recent Decisions, Nudges |
| 4 | `coach_update_profile` | idempotent, non-destructive | `changes` {field: value}, `request_id` → before/after and what was confirmed |
| 5 | `coach_record` | idempotent (per `request_id`), non-destructive | `entry`, a union on `kind`: goal / commitments (1-3) / decision / checkin; `request_id` → new ids and `warnings` |
| 6 | `coach_update` | idempotent, non-destructive | `id`, `status?`, text fields?, `note?`, `request_id` → before/after; carrying returns the new id and the streak |
| 7 | `coach_corpus_status` | read-only | → pack version, talks/years/series, model readiness, store path, schema version |

- **Writes need the Founder's yes.** Every write tool's description says to call it only after the Founder has approved the exact text that will be saved.
- **Quoted talk text is untrusted.** In the text channel it's wrapped as `<untrusted_source item_id=…>…</untrusted_source>`, so it reads as reference material, not instructions.
- **Search results link to their items** through `resource_link`s to `corpus://item/{item_id}`.

**Prompts** `ask`, `weekly-focus`, `check-in` and `setup` are generated from the skills' bodies, for hosts without skills.

**Resources:** `founder://profile`, `founder://this-week`, `corpus://status`, and the template `corpus://item/{item_id}`.

**Server `instructions`** carry the 8-line coaching contract (§11.6).

### 11.4 Runtime behaviour

- **Startup is under 1 s.** The lifespan only opens the pack (full checksum check once per pack version) and the store (migrating if needed), then starts model warm-up in a background thread.
- **Before the models are ready,** `coach_search` answers keyword-only with `mode: keyword` and a note. It never blocks or times out. A failed download is retried on the next start, and `coach_corpus_status` reports it. `founder-coach warmup` (run by setup) downloads with progress.
- **Logging** goes to stderr, with ids and counts only and never founder text. No MCP logging (deprecated in 2026-07-28), no sampling, no roots.
- **Security:**
  - The only network access is the one-time model download. That keeps the server away from the "lethal trifecta" of private data, untrusted content and outbound actions.
  - SQL is parameterised; the pack is opened read-only and immutable.
  - Tool schemas and descriptions are snapshot-tested, so they can't drift silently.
- **Shutdown:** the server exits on stdin EOF and closes handles in the lifespan's `finally`.

### 11.5 CLI (`founder-coach`)

| Command | What it does |
|---|---|
| `serve [--pack PATH]` | the stdio MCP server (the pack defaults to the one inside the package folder) |
| `hook session-start` | reads the store directly; prints SessionStart JSON with `additionalContext` (at most ~1,500 characters) only when something is due; always exits 0, silently on any error |
| `warmup` | downloads the ONNX models, with progress |
| `status` | store, pack, models, Nudges |
| `export [--out DIR]` | JSON + Markdown of everything |
| `forget` | typing the company name confirms it; takes a final backup unless `--no-backup` |

Export and forget are deliberately not MCP tools, so no model can wipe the Founder's memory.

### 11.6 Skills and the coaching contract

**Skill conventions:**
- Use the Agent Skills standard keys, plus only `argument-hint`, `disable-model-invocation` and `user-invocable` (shared by Claude Code, Cursor and VS Code).
- Descriptions are a third-person "what" sentence plus "Use when…", with no capitalised emphasis, under 1,024 characters.
- Bodies stay under 500 lines, with checklists, output templates, a gotchas section and the reason behind each rule.
- Tools are named bare (`coach_search`), so the text is the same in every host.

| Skill | Who invokes it | Content |
|---|---|---|
| `founder-coach` | model only (`user-invocable: false`) | the coaching contract, gotchas; `references/` for stages and citation format |
| `ask` | both; `argument-hint: "[question]"` | split the question into up to 4 parts, one `coach_search` per part, `coach_read` when needed, answer template with Citations, Gaps listed |
| `weekly-focus` | both | checklist: context → propose up to 3 if-then Commitments with outcomes, each cited → confirm → `coach_record` |
| `check-in` | both | each open Commitment gets evidence, then `coach_update` → Decisions and blockers → profile changes (confirmed) → `coach_record` the Check-in → weekly-focus |
| `setup` | user only (`disable-model-invocation: true`) | interview → `coach_update_profile` → `founder-coach warmup` → offer the weekly scheduled task |
| `status`, `export`, `forget` | user only | run the CLI; `forget` shows what will be deleted first |

**The coaching contract.** It lives in `founder-coach`, is repeated in short in each Playbook and appears in the server instructions:
1. Restate the Founder's plan or claim as a question before judging it, and ask what evidence exists (users, revenue, retention). This works better than "don't be sycophantic" ([Dubois et al. 2026](https://arxiv.org/html/2602.23971v2)).
2. Name the single biggest risk. Change position on new evidence, not on repetition.
3. Keep praise specific and proportional to what was achieved.
4. Cite only item_ids that `coach_search` or `coach_read` returned in this conversation. Retract any claim without a supporting quote, label general knowledge as such, and name Gaps.
5. Give each piece of advice its year, and show where talks disagree.
6. Ask one clarifying question when a missing fact would change the recommendation, stating the default you'd otherwise assume.
7. For legal, tax, immigration, securities or medical questions, say where the coach's limits are and point to a professional.
8. Propose exactly what will be saved, and save only after the Founder says yes.

### 11.7 Hook

`hooks/hooks.json` runs on SessionStart with matcher `startup|resume|clear|compact`, using the command form `uvx --from ${CLAUDE_PLUGIN_ROOT} founder-coach hook session-start` and a 10 s timeout. It can't use an `mcp_tool` hook, because MCP isn't up at first launch. It stays silent unless something is due. There are no UserPromptSubmit or Stop hooks: they would add tokens and noise to every turn.

### 11.8 Subagents

None in v1. `citation-auditor` becomes required only if gate G2 fails. Plugin agents can't carry their own MCP servers or permission modes, which is fine for a read-only auditor.

### 11.10 Resolved while cross-checking against the ADRs, glossary and code (2026-09-24)

- **Where the server finds the pack.** A package built by `uvx --from <plugin folder>` can't see
  files next to it, so `.mcp.json` passes `--pack ${CLAUDE_PLUGIN_ROOT}/pack`. Without that
  argument the server looks at `FOUNDER_COACH_PACK`, then `~/.founder-coach/pack`, and
  `coach_corpus_status` says clearly when no pack is found.
- **Stage values.** The runtime can't import ytbrain, so `founder_coach` holds its own copy of
  the eight Stages. A test fails if it ever differs from `ytbrain.extract.schema`.
- **MCP prompt text.** `scripts/assemble_plugin.py` generates `founder_coach/playbooks/*.md` from
  the skills' bodies (package data), and a test checks the two stay in sync. The skills are the
  single source.
- **Statuses per record.** Goals are active, met or dropped. Commitments are open, done,
  dropped or carried. `coach_update` rejects a status that doesn't belong to the record, with
  the allowed list.
- **Time zones.** The profile stores an IANA name (e.g. `Asia/Kolkata`). `tzdata` is a runtime
  dependency, so this works on Windows too.
- **Provisional Gap threshold.** Take the 5th percentile of the pack's top relevance on the dev
  questions (all answerable), computed after the first pack run. It's replaced by the
  out-of-corpus calibration in M2c.
- **First launch.** The first time `uvx` starts the server, it installs about 100 MB of
  dependencies, which may exceed a host's MCP start-up timeout. `.mcp.json` sets a generous
  per-server `timeout`, the install notes include one pre-warm command
  (`uvx --from <plugin> founder-coach warmup`), and the step-8 fresh-machine test measures it.
  The hook fails open meanwhile.
- **Cowork.** It uses the same plugin format, but it may run plugin servers in a separate
  environment where `~/.founder-coach` isn't the one Claude Code uses. The beta targets Claude
  Code; step 8 checks Cowork.

### 11.9 Tests and evals

- **`tests/test_coach.py`, offline:**
  - **Store:** migrations, the change log, idempotent `request_id`, carry and its streak, confirm and staleness, weeks across time zones, two concurrent writer processes, backup rotation, FOUNDER.md, export, forget.
  - **Nudges.**
  - **Server** through the SDK's in-memory `Client`: every tool, the errors, the keyword fallback, output size.
  - **Golden snapshot** of tool names, order, schemas and annotations.
  - **Stdio smoke test** under the 2026-07-28 and 2025-11-25 protocols.
  - **Hook:** output shape and fail-open behaviour.
- **Maintainer, with Claude Code installed:** `claude plugin validate dist/plugin --strict`, then a `claude --plugin-dir dist/plugin` session.
- **M3d:** `claude plugin eval` over `plugin/evals/`:
  - about 20 trigger cases per workflow skill, including near-misses;
  - behaviour cases with mocked tools:
    - a regex grader that only accepts Citations the mock returned;
    - `tool_used … max: 0` for writes the Founder didn't confirm;
    - an LLM rubric for pushback on a weak plan;
    - a `history_file` "are you sure?" case to check the coach doesn't cave.
  - Each case runs 3 times, against a no-plugin baseline, with a threshold of 0.8.
  - Gate G5 (multi-week memory) stays a small custom harness on the real server, because plugin evals isolate state.

**Portable later (M6):** a root `plugin.json` in the Agent Plugins 1.0 format, `mcp.json`, and `gemini-extension.json`. The skills already use only portable keys.
