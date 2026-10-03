# Commands by phase

Every command is resumable: Ctrl+C keeps finished work, and re-running continues. Install once:
`uv pip install -e ".[extract,index,graph,serve,asr,eval,pack,dev,pot,web]"` (everything; `graph` and
`asr` are optional), then `playwright install chromium` for JavaScript-only websites.

## The whole loop (docs/ops.md)

```bash
ytbrain ops                 # ingest -> eval when the index changed -> plugin when it passed; resumable
ytbrain ops --dry-run       # what is due
```

## Tests (run after any change)

```bash
for t in core eval pack coach plugin web books; do YTBRAIN_DOTENV=0 python tests/test_$t.py | tail -1; done
```

## Phase 1: corpus (sync → clean → extract → verify)

```bash
ytbrain discover                      # list the channel's playlists into sources.yaml candidates
ytbrain run                           # the whole pipeline: sync, clean, extract, verify, index
ytbrain sync --backfill               # retry caption downloads that hit HTTP 429, slowly
ytbrain sync --reconcile              # tombstone videos removed upstream (weekly)
ytbrain refresh                       # backfill Series and provenance without re-extracting
ytbrain clean ; ytbrain extract --workers 4 ; ytbrain verify     # one Step at a time
ytbrain status                        # per-Step counts
ytbrain report                        # quality gates
ytbrain sample --n 10                 # human review sheet in data/reports/ (fill the verdicts)
ytbrain pages                         # readable Markdown per talk in data/pages/
ytbrain invalidate extract --only-flagged   # re-run one Step for flagged records
ytbrain extract --retry-failed        # also retry talks parked after failing 3 runs in a row
                                      # (same flag on clean, verify and index)
```

## M1: knowledge index and search

```bash
ytbrain index                         # embed Verified items into LanceDB (part of `run`)
ytbrain search "how do I price a B2B pilot" --stage mvp
ytbrain search "..." --no-rerank --kind advice --json
```

## M2: eval (Tuning set)

```bash
ytbrain eval build --set dev --limit 5            # smoke test in data/eval/smoke
ytbrain eval build                                # every Tuning split (talks, articles, books, private), spend-capped per split
ytbrain eval build --top-up                       # grow each split to 20% of its Documents (at most)
ytbrain eval build --set dev-private              # one split
ytbrain eval status                               # each split's questions, target and spend
ytbrain eval run --set dev --save-baseline        # full setup = the baseline
ytbrain eval run --set dev --config no-rerank --compare full
ytbrain eval run --set dev --config full-no-passages --compare full
ytbrain eval run --set dev --config pack --compare full           # G1: within 0.03 nDCG@10
ytbrain eval run --set dev --config pack-no-rerank --compare full
ytbrain eval judge --set dev --config full --config pack   # grade Moments no judge has seen (pool extension)
ytbrain eval rescore --set dev --config full --save-baseline      # score a saved run again, no search
ytbrain eval rescore --set dev --config pack --compare full
ytbrain eval rescore --config full --refresh-baseline   # the baseline, re-scored on new labels
```

Exit codes: 0 pass, 1 regression, 3 inconclusive (read the verdict). After new Documents are
indexed: `pack build`, then `eval run` for full (`--save-baseline`) and pack, `eval judge` on both, and
`eval rescore` (full `--save-baseline`, then pack `--compare full`). `eval judge` and `eval run --config
pack` say so when a saved run or the pack is older than the index.

## M2d: Knowledge pack

```bash
ytbrain pack build                    # data/pack: knowledge.sqlite + pack.json (resumable)
ytbrain pack info                     # counts, models, checksum OK
ytbrain pack build --device coreml    # experimental: Apple GPU/Neural Engine for embedding (default cpu)
ytbrain search "..." --pack           # search the pack with the ONNX runtime path
ytbrain pack build --with-passages --out data/pack-passages       # beta-only variant (ADR-0009)
ytbrain pack build --embed-model snowflake/snowflake-arctic-embed-m --with-passages --out data/pack-arctic
ytbrain eval run --set dev --config pack-no-rerank --pack data/pack-arctic --label arctic --compare full
```

## M3a: coach runtime (founder-coach CLI and MCP server)

```bash
founder-coach warmup                              # download the ONNX query models
founder-coach status                              # pack, models, store integrity, Nudges
founder-coach serve --pack data/pack              # stdio MCP server (the host starts this)
npx @modelcontextprotocol/inspector founder-coach serve --pack data/pack   # try the 8 tools by hand
echo '{}' | founder-coach hook session-start      # prints SessionStart JSON only when something is due
founder-coach export [--out DIR]
founder-coach restore --list ; founder-coach restore            # put back the newest good backup
founder-coach forget [--confirm "<company>"] [--no-backup]
founder-coach feedback list ; founder-coach feedback export [--out DIR]   # the Founder's Feedback, as one file to send
founder-coach usage summary [--days 30] [--json] ; founder-coach usage export [--out DIR]
founder-coach usage clear --yes                  # the local usage log (docs/usage-log.md)
```

Use `--home /tmp/fc-test` on any of these to try them without touching your real store.

Settings (`FOUNDER_COACH_*`, listed in `.env.example`) come from the environment, then
`$FOUNDER_COACH_ENV_FILE`, `./.env` and `~/.founder-coach/.env`. Only `FOUNDER_COACH_*` lines are
read, never API keys; `founder-coach status` shows which files were used. Without any file,
nothing changes and every default applies.

## M3b: plugin, skills and hook

```bash
python scripts/assemble_plugin.py --pack data/pack --check [--zip]   # -> dist/plugin; --zip adds the Cowork upload file
claude plugin validate dist/plugin --strict
ytbrain claude -- --plugin-dir dist/plugin      # your Claude plan on Haiku (--openrouter: paid per token)
#   then in the session: /founder-coach:setup, /founder-coach:ask <question>,
#   /founder-coach:weekly-focus, /founder-coach:check-in, /founder-coach:status,
#   /founder-coach:feedback <what was wrong> (after an answer that missed), /founder-coach:export,
#   /founder-coach:forget (test data only)
#   and start a new session to see the SessionStart Nudge when something is due
```

## M3d: evaluating the coach's answers and memory

```bash
# fast, mocked behaviour checks of the skills (no pack, no Founder data)
python scripts/assemble_plugin.py --pack data/pack --with-evals --out dist/plugin-eval
ytbrain claude -- plugin eval dist/plugin-eval --runs 1 --threshold 0.8   # --runs 3 before a release
# `ytbrain claude [--model sonnet] [--openrouter] -- <claude args>` = the local Claude Code on your
# Claude plan with Haiku (--openrouter: through OpenRouter instead, paid per token)
# --ablation none skips the without-plugin comparison runs (half the usage) while iterating

# gates on the real host, server and pack: G2 citation support, G4 sycophancy,
# G5 multi-week memory, G6 decomposition. The host is the local Claude Code on your Claude plan,
# token-saving: Haiku, 12 turns per call, no repo context (judges spend a few cents, --max-cost)
python scripts/assemble_plugin.py --pack data/pack --check
ytbrain eval coach --limit 2                      # a quick end-to-end check first
ytbrain eval coach                                # all four gates; resumable per plugin build and model
ytbrain eval coach --gate g5                      # one gate (repeatable)
ytbrain eval coach --max-turns 12                 # turns per host call (the default)
ytbrain eval coach --model sonnet                 # sign a release off on the model founders use
ytbrain eval coach --host openrouter              # optional: through OpenRouter, paid per token
ytbrain claude -- --plugin-dir dist/plugin        # a live session with the plugin (your plan, Haiku)
```

Results: `data/eval/coach/<gate>-<build>[-<model>]-<harness>.jsonl` (one line per case, with answers, judge reasons
and the memory checks) and `report-<time>.json`. Don't reassemble `dist/plugin` while `eval coach` runs:
it stops rather than mixing two builds' results.


## Product name, repos, CI and releases (docs/release.md)

```bash
$EDITOR product.toml                                   # the product's id, display name, SEO description, repos
python scripts/assemble_plugin.py --pack data/pack     # regenerates founder_coach/product.json and the playbooks
python scripts/check_secrets.py                        # what CI and the pre-commit hook run
python scripts/release.py --pack data/pack --marketplace ../founder-coach-marketplace --version 0.1.1 [--push]
```

## Website Sources (docs/web-sources-plan.md)

```bash
uv pip install -e ".[web]" && playwright install chromium   # once; the browser is only for JavaScript sites
# add `- type: website` + `url:` entries to sources.yaml (see sources.example.yaml)
ytbrain sync                          # every enabled Source: playlists and websites
ytbrain sync --source <id> --limit 50 # one Source, at most 50 pages this run (the rest resume next run)
ytbrain sync --type website           # only websites (talks found on them still get their captions)
ytbrain sync --type book              # only PDF Books: one Document per Chapter (then `ytbrain clean`)
ytbrain sync --type youtube           # only YouTube playlists
ytbrain run                           # sync -> clean -> extract -> verify -> index, articles and talks alike
ytbrain invalidate clean --source <id> # re-run one Step for one Source
ytbrain drop --source <id>            # take a Source's Documents out of the knowledge (then `ytbrain index`)
```

## PDF Books (docs/books-plan.md)

```bash
ytbrain books inspect <pdf-or-folder> # refused or not, metadata, chapters found and how (no LLM; parse cached)
ytbrain books inspect data/books --expect data/books/expected.yaml   # exit 1 on any mismatch
ytbrain sync --type book && ytbrain clean
ytbrain extract --doc <ISBN>          # one Book's Chapters (pilot), then plain `ytbrain extract`
ytbrain verify --doc <ISBN> && ytbrain sample --doc <ISBN> --n 10
ytbrain index                         # Books join the index, marked private
ytbrain pack build --include-private --out data/pack-mine   # your own coach only; release refuses it
```
