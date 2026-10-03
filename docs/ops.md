# `ytbrain ops`: the whole loop in one command

```bash
ytbrain ops                 # or sh ops/all.sh: ingest, then eval if the index changed, then the plugin if it passed
ytbrain ops ingest          # sh ops/ingest.sh
ytbrain ops eval            # sh ops/eval.sh
ytbrain ops plugin          # sh ops/plugin.sh
ytbrain ops --dry-run       # what is due and every step, nothing run
```

Flags: `--max-cost USD` (this run's eval and coach-judge spend, default 5), `--max-extract N` (extract at
most N Documents), `--no-sync`, `--skip-coach`, `--restart` (don't resume), `--force` (run eval and the
plugin even when nothing changed or the last verdict wasn't a pass), `--version patch|minor|skip` (answer
the version question up front), `--no-notify`.

Nothing is manual: references, versions, retries and notices are handled by the run (below). It runs
when you start it; it isn't scheduled (the 03:15 launchd job still runs only `ytbrain run`).

## What each plan runs

| Plan | Steps (each an ordinary `ytbrain` command, with its own resume) | When |
|---|---|---|
| ingest | `sync` → `clean` → `extract` → `verify` → one more extract + verify for Documents verify newly flagged → `index` | every run (sync is how new talks, pages and books are found) |
| eval | `eval build --set <split> --top-up` for every split with Documents → `eval run --config full` → `eval judge --config full` → gate | the index or the labels changed since the last eval, or its verdict wasn't a pass |
| plugin | `pack build` → assemble `dist/plugin` (+ zip) → with private Sources, `pack build --include-private` → `dist/plugin-private` (+ zip) → `claude plugin validate` → `eval coach` | the last eval passed on the current index, and the index or the plugin's code changed; the coach eval once per new build |

The gate re-scores the saved baseline (`eval rescore --refresh-baseline`) on the labels the judge just
released, compares the new run with it, and saves the new run as the baseline only on PASS. FAIL or
INCONCLUSIVE stops before the plugin and keeps the baseline. The first eval becomes the baseline.

## Resume and change detection

`data/ops/state.json` records each finished step; an interrupted or stopped run (Ctrl+C, spend cap, a
refused endpoint, a failed gate) resumes at the first step not done. It also keeps fingerprints: of the
index (every indexed Document with the input it was indexed from), the labels (released version and the
private overlay), and the plugin (index + `plugin/`, `founder_coach/`, the assembler). What changed decides
what runs. Flagged Documents are retried once each (the list is kept), never on every run.

## When something stops

A command that stops early writes why (`data/ops/last-stop.json`: budget, endpoint, network, plan_limit,
interrupted); the runner acts on it:

| Reason | What happens |
|---|---|
| network (sync, a slow provider) | the step is retried twice, after 1 and 5 minutes; a sync that still fails is skipped and the run goes on with what is fetched |
| endpoint refused (key, credits, model, quota) | stop; the notice says to fix `.env` and re-run |
| spend cap | stop; the notice says to raise `--max-cost` |
| gate FAIL or INCONCLUSIVE | stop before the plugin; the baseline is kept |
| Claude plan limit (coach eval) | the plugin is built; the coach eval is pending and runs alone on the next `ytbrain ops` |
| no `claude` on PATH | validate and the coach eval are skipped (pending) |
| interrupted, another ops running | resume with the same command / exit |

Every stop and every finished run sends a macOS notification and writes `data/ops/last-run.md`: what
happened and the one command to run next.

## Plugin version

A new build gets a new version so Cowork installs it as an update. The run asks in the terminal: patch
(default), minor or skip; with no answer in 60 s, or no terminal, it takes patch (a notification says the
question is waiting). A version you set by hand since the last build is kept. It is written to
plugin.json, plugin/pyproject.toml and founder_coach (the same helper `scripts/release.py` uses).

## References to compare with

Before the gate moves the baseline, the run keeps it as `full-<date>` (the last 10), and the first time a
kind of Source is indexed, as `full-before-<kind>` (what `full-prebooks` was for Books). Compare any time,
free: `ytbrain eval rescore --config full --compare full-2026-10-01` (after new labels, refresh it first:
`ytbrain eval rescore --config full-2026-10-01 --refresh-baseline`).

## Questions as Sources grow

Each Tuning split holds at most 20% of the Documents it is written from (one question per Document at
most), never fewer than it has (eval/splits.py, `EVAL_SPLIT_RATIO`). `eval build
--top-up` (which `ops eval` runs) writes the missing ones from Documents added since the split was last
seeded first. A question whose seed Document was removed is retired at the next build or judge.
`ytbrain eval status` shows each split's target.

## Spend

`--max-cost` caps what this run spends on eval questions, judging and the coach eval's judges: each paid
step gets what is left. Extraction is billed by your LLM endpoint and isn't metered: the runner prints how
many Documents it will extract; `--max-extract` limits them. The coach eval runs on your Claude plan.

The scheduled job (`ops/com.ytbrain.sync.plist`, `ytbrain run`) stays as it is: it never writes eval
questions or builds a plugin.

Before installing a new private plugin in Cowork as an update, bump `version` in
`plugin/.claude-plugin/plugin.json`; the private zip is `dist/founder-coach-<version>-private.plugin`.
