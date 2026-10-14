# Usage log (decided 2026-09-26)

What the coach records about its own use, on the Founder's machine, so the maintainers (and the
Founder) can see which tools run, how often they fail or come back empty, and how slow they are.
It answers product questions (is search used? how often is it a Gap? is keyword mode common?)
without collecting what the Founder asked or was told.

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Local only.** Nothing is sent anywhere. `founder-coach usage export` writes one JSONL file the Founder may choose to send, like Feedback. | Same promise as the rest of Founder data (ADR-0006); no server to run or pay for. |
| D2 | **No Founder text by default.** Each event holds the tool, time, run (one server process = one host session), duration, outcome (`ok`, `empty`, `gap`, `error`), coach version and per-tool shapes: search mode, hit count, the first 5 item ids, Stage used, top similarity, query *length*, filters used; the `ref` read; profile *field names*; record kind; update status; Feedback category; `replayed`; an error's class and message (first 160 characters). Search text only with `FOUNDER_COACH_USAGE_TEXT=1`. | The questions a Founder asks are the most sensitive thing they type; counts and corpus ids answer the product questions. |
| D3 | **A `usage` table in `founder.db`** (store schema v4), not a separate file. It is not coaching memory: it isn't in FOUNDER.md, adds no change-log row and never triggers a backup, but `forget` deletes it, `export` includes it and backups/restore carry it. | One file to back up, forget and restore; SQLite gives safe concurrent appends from several host sessions. |
| D4 | **Captured in the MCP server**, around every tool call (a wrapper that keeps each tool's signature, so the schemas the host sees are unchanged). Arguments rejected by schema validation never reach the tool and aren't logged. Hooks and skills aren't logged for now. | The server sees every call without touching the host; add hook events only if the data shows a need. |
| D5 | **Never costs an answer.** A usage write waits at most 250 ms for a busy store, never raises, and is skipped when the store is unavailable. | Telemetry is the least important thing the coach does. |
| D6 | **On by default, kept 90 days.** `FOUNDER_COACH_USAGE=0` turns it off; `FOUNDER_COACH_USAGE_DAYS` changes retention (pruned at each server start). `founder-coach status` says whether it's on. | Useful for beta testers without setup; bounded growth; one switch to stop it. |
| D7 | **CLI:** `founder-coach usage summary [--days 30] [--json]` (calls and sessions; per tool: calls, errors, empty, Gaps, keyword-mode searches, p50/p95 ms), `usage export [--out DIR]`, `usage clear --yes` (deletes only the usage log). | The Founder can see and delete exactly what's kept. |

## Follow-ups decided 2026-09-27

| # | Decision | Why |
|---|---|---|
| D8 | **`/setup` tells the Founder**, once, in its closing message: a local usage log exists, holds no words, is kept 90 days, and `<PREFIX>USAGE=0` turns it off. | It is on by default; a tester never reads the README. |
| D9 | **Testers send it through `/feedback`:** after exporting Feedback, the skill offers the usage export in the same message. No separate skill or MCP tool; export stays CLI-only (run by the skill with the Founder's yes). | No terminal needed, one moment to ask, no new surface. |
| D10 | **Question text only on the maintainer's machine** (`FOUNDER_COACH_USAGE_TEXT=1` while dogfooding); never asked of testers, whose Feedback carries the question when it matters. | The Gap and keyword-mode questions are worth seeing; testers' words stay theirs. |
| D11 | **Skills and hooks stay unlogged** until the first beta's data leaves a question tool calls can't answer (which Playbook ran is visible from its tool pattern). | No evidence yet that it's needed. |

## Follow-up decided 2026-10-14

| # | Decision | Why |
|---|---|---|
| D12 | **A search row also carries `coverage_by_domain`, `basis` and `stale`** (Domain names and levels, never words), and `ytbrain gaps` reads every Project's `usage` table, read-only and from a copy, to report strong / partial / none per coach and Domain. | `outcome: gap` only means *nothing close*; the Gaps founders see are mostly `partial`, so the log must say which Domain was thin to say what to ingest or tune. |
| D13 | **The question text for the report is the existing opt-in (D10)**, set on the maintainer's machines; the report says how to turn it on when weak searches have no text. | What was missing can't be read from counts. |
| D14 | **Not decided here:** how a skill reports the Gaps it names (a tool call from the skill). `coach_record` is coaching memory (founder-approved text, FOUNDER.md, change log) and is not the place. | Needs a deliberate tool-schema change or a server-side inference. |

## Acceptance criteria (tests/test_coach.py, `test_u*`)

- **U1** Every one of the 8 tools, successful or not, adds one event with the fields in D2; a replayed write says so.
- **U2** No Founder text in the log: the search query, goal text, profile values and Feedback wording never appear in it.
- **U3** A usage write that fails (raises, or the store is locked by another process) never breaks the tool call, and gives up within 2 s at most (250 ms busy wait).
- **U4** `FOUNDER_COACH_USAGE=0` records nothing; `FOUNDER_COACH_USAGE_TEXT=1` adds the search text.
- **U5** Events older than the retention window are pruned.
- **U6** A v3 store migrates to v4 with a backup first; `forget` deletes the log; `export` includes it; usage adds no change-log rows and doesn't show in FOUNDER.md.
- **U7** `usage summary` (text and JSON), `usage export` (usage only) and `usage clear` (needs `--yes`) work; `status` reports it.
- The MCP tool-schema snapshot is unchanged.
