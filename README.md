# Founder Coach

[![ci](https://github.com/kaushalag29/founder-coach/actions/workflows/ci.yml/badge.svg)](https://github.com/kaushalag29/founder-coach/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-BUSL--1.1-lightgrey)
![status](https://img.shields.io/badge/status-private%20beta-orange)

**An AI startup coach personalized to your company, grounded in Y Combinator talks.** It
answers with advice cited to the exact second of the talk it came from, sets a weekly Focus of
up to three Commitments, and runs Check-ins that remember what you decided. It installs as a
Claude Code plugin and runs locally: your data stays on your machine.

This repository holds both halves:

| Part | What it is | For |
|---|---|---|
| **The coach** (`founder_coach/`, `plugin/`) | A light MCP server with a searchable Knowledge pack and a private local Founder store, plus the skills (Playbooks), hook and manifest of a Claude Code plugin | Founders; installed from a private plugin marketplace |
| **ytbrain** (`ytbrain/`) | The pipeline that turns YouTube playlists into Verified, citable knowledge: captions only, an LLM extracts structured advice, and every quote is checked against the transcript before anything can be cited | The maintainer; builds the Knowledge pack |

```
 sources.yaml ─► sync ─► clean ─► extract ─► verify ─► index ─► pack build ─► plugin ─► Claude Code
 (playlists)    yt-dlp   dedup    LLM per    every     local    ONNX pack    skills +    /founder-coach:ask
               captions  + cues   video      quote     search   (no torch)   MCP server  /founder-coach:check-in
                                             located
```

**Status (2026-09-28):** private beta in preparation ([docs/m3-status.md](docs/m3-status.md)).

| | State |
|---|---|
| Corpus | 1,083 records: 721 Y Combinator talks and 362 website articles (Paul Graham, YC Library, Sam Altman and others); 33.6k items indexed; 222 talks still without captions |
| Coach runtime and plugin (M3a, M3b) | Done: 8 MCP tools, 9 skills, SessionStart hook; waiting on one live session |
| Quality gates ([phase3-plan §0](docs/phase3-plan.md)) | On Haiku (2026-09-28): G2 citation support 100 % ✓ · G4 sycophancy 9/10 ✓ · G6 decomposition 7/7 ✓ · G5 memory: re-run after the Check-in fixes · G1 retrieval: the pack trails the full index (0.471 vs 0.568 nDCG@10; experiments pending) · G3 Gaps: the Holdout is next |
| Next | G5 re-run · dogfood and a fresh-machine install · M2c lite Holdout (G3) · first beta release ([docs/m3-status.md](docs/m3-status.md)) |

## Quickstart

**Beta testers** (you need read access to the private marketplace repo, `git` and
[`uv`](https://docs.astral.sh/uv/)), in Claude Code:

```
/plugin marketplace add kaushalag29/founder-coach-marketplace
/plugin install founder-coach@founder-coach-marketplace
/founder-coach:setup
```

Then `/founder-coach:ask <question>`, `/founder-coach:weekly-focus`, `/founder-coach:check-in`,
and `/founder-coach:feedback <what was wrong>` when an answer misses.

**Developers:**

```bash
git clone git@github.com:kaushalag29/founder-coach.git && cd founder-coach
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[serve,pack,dev,web]" "lancedb>=0.39.0"   # as CI; everything: ".[extract,index,graph,serve,asr,eval,pack,dev,pot,web]"
for s in core eval pack coach plugin web; do YTBRAIN_DOTENV=0 python tests/test_$s.py || break; done   # offline, a few minutes
```

Building the corpus needs an LLM key and YouTube access ([Install from scratch](#install-from-scratch)).
Building and releasing the plugin: [The coach plugin](#the-coach-plugin-m3b) and
[docs/release.md](docs/release.md).

## Documentation

| Doc | What's in it |
|---|---|
| [CONTEXT.md](CONTEXT.md) | The glossary: Advice, Evidence, Verified, Stage, Commitment, Check-in, Feedback… |
| [docs/adr/](docs/adr/) | Architecture decisions (13), e.g. [retrieval, not generation](docs/adr/0002-mcp-server-retrieves-host-agent-generates.md), [only Verified knowledge](docs/adr/0004-only-verified-knowledge-reaches-the-coach.md), [one product id](docs/adr/0012-vertical-founder-coach-with-a-build-time-product-id.md) |
| [docs/phase3-plan.md](docs/phase3-plan.md) | The coach: v1 scope, quality gates, MCP surface, skills, store (§11 is the implementation spec) |
| [docs/phase2-plan.md](docs/phase2-plan.md) | Milestones M0–M6 and the pipeline design |
| [docs/eval-spec.md](docs/eval-spec.md) | The retrieval benchmark (Tuning and Holdout sets, judges, metrics) |
| [docs/commands.md](docs/commands.md) | Every command, by phase |
| [docs/release.md](docs/release.md) | Repos, CI, beta releases, what testers do, renaming |
| [docs/m3-status.md](docs/m3-status.md) | Where the coach work stands and what's next |
| [docs/research/](docs/research/) | Research behind upcoming work, e.g. [the web crawler](docs/research/web-crawler.md) |
| [AGENTS.md](AGENTS.md) | Instructions and invariants for coding agents |
| [CHANGELOG.md](CHANGELOG.md) · [SECURITY.md](SECURITY.md) | Changes; how secrets and Founder data are handled |

---

## Contents

- Pipeline: [Tested setup](#tested-setup) · [Responsible use](#responsible-use) · [Prerequisites](#prerequisites) · [Install from scratch](#install-from-scratch) · [Configure an LLM backend](#configure-an-llm-backend-env) · [Configure sources](#configure-sources-sourcesyaml) · [Run the pipeline](#run-the-pipeline)
- Coach: [The Knowledge pack](#the-knowledge-pack-what-the-coach-plugin-ships) · [The coach plugin](#the-coach-plugin-m3b) · [The coach runtime](#the-coach-runtime-founder-coach-m3a)
- Operating it: [Choosing and switching models](#choosing-and-switching-models) · [Parallelism, rate limits and retries](#parallelism-rate-limits-and-retries) · [Interrupting and resuming](#interrupting-and-resuming) · [Quality checks](#quality-checks) · [Output format](#output-format) · [Scheduling](#scheduling-macos-launchd) · [Configuration reference](#configuration-reference) · [Troubleshooting](#troubleshooting)
- Project: [Layout and design decisions](#project-layout-and-design-decisions) · [Tests and CI](#tests-and-ci) · [Roadmap](#roadmap) · [Contributing](#contributing) · [Security and privacy](#security-and-privacy) · [License](#license) · [Disclaimer](#disclaimer) · [Changelog](#changelog)

---

## Tested setup

Developed and tested on a **MacBook Air M2, 16 GB RAM, macOS**, Python 3.11+, yt-dlp
2026.08.19, against the Y Combinator channel (56 playlists, ~860 videos, ~710 with
English captions).

| Backend | Model | Result on our test videos |
|---|---|---|
| **OpenRouter** (paid, pennies) | `deepseek/deepseek-v4-flash` | **Recommended.** 4 workers: ~12–17 s per video overall; 10-video sample: 96 % of evidence quotes verified, 9/10 records pass. Whole ~700-video corpus ≈ $0.40–0.80. |
| OpenRouter | `google/gemma-4-31b-it` | 5-video sample: 51/51 quotes verified; ≈ $1.10 for the corpus. Slower providers. |
| **LM Studio** (local, free) | `gemma-4-e4b-it` (MLX, 4-bit), 32k context | Works on 16 GB. ~1 m 54 s per video → ~22 h for ~700 videos. Weaker on people/category extraction. |
| NVIDIA NIM (free tier) | `nvidia/nemotron-3.5-lightning-30b-a3b` | Works, but free-tier latency varied from 1 s to 100 s per request and several listed models were not callable for our account. Fine for trying things, not for a full run. |
| Ollama | any (default `qwen3:30b-a3b`) | Supported and covered by tests; not benchmarked by us on the M2. |

**Full-corpus runs** with DeepSeek V4 Flash on OpenRouter, 5 workers:

| Run | Records | Evidence quotes verified | Pass / flagged / failed |
|---|---|---|---|
| Schema 2.1 | 710 of ~712 (99.7 %) | 95.6 % | 659 / 29 / 22 |
| Schema 2.2.0 | 708 of 712 (99.4 %) | 98.5 % | 686 / 21 / 1 |
| + current verifier | 708 of 712 (99.4 %) | 99.1 % | 688 / 19 / 1 |

With 2.2.0, a normal record is 2–3k output tokens. The few replies that hit the output cap
were a provider stuck in a loop, now retried on a different provider (see
[extract](#extract)).

**Knowledge index** on the M2 Air: 708 talks produced 24 814 items in 53 min.
`BAAI/bge-m3` runs on the Apple GPU (`mps`) at about 5 s per talk. The two local models
(embedding + reranker) are ~2.3 GB each, downloaded once to `~/.cache/huggingface`.

**YouTube** rate-limits anonymous caption downloads per IP. Our full sync lost about 50
talks to HTTP 429. With Deno, `yt-dlp-ejs` and a PO-token server, `sync --backfill` could
fetch some of them, but rapid restarts triggered 429s again.

Prices and free-tier limits are as of September 2026 — check your provider.

## Responsible use

- **Captions are the uploader's content.** `data/` (raw captions, transcripts) is
  git-ignored on purpose: use it as ingestion material, and publish only what you derive
  (summaries, taxonomy, advice) with links back to the original video. Respect the
  YouTube Terms of Service and the uploader's rights.
- **Be gentle with YouTube.** ytbrain spaces requests (`sleep_requests`), backs off on
  rate limits and stops when throttled. Don't lower those defaults to go faster.
- **No IP rotation.** Rotating proxies to get around YouTube's rate limits is
  circumvention, which its terms prohibit. ytbrain only passes one proxy through
  (`YTBRAIN_YTDLP_PROXY`, e.g. a company network). The supported answer to 429s is
  slower pacing: `sync --backfill`.
- **Run it from a normal (residential) connection.** YouTube blocks or heavily limits
  cloud/datacenter IPs, and sandboxes that proxy traffic (`Tunnel connection failed: 403`).
- **ytbrain never logs in to YouTube.** It uses no cookies or Google account. (yt-dlp can,
  but its authors warn that accounts used this way can be banned.)

## Prerequisites

| Need | Why | Check |
|---|---|---|
| macOS or Linux | Tested on macOS; Linux works the same. Windows untested. | |
| Python **3.11+** | Language features used throughout | `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | Creates the virtualenv, installs dependencies, can install Python itself | `uv --version` |
| git | To clone the repo | `git --version` |
| [Deno](https://deno.com) (or Node ≥ 20) | yt-dlp needs a JavaScript runtime for full YouTube support | `deno --version` |
| Docker *(optional)* | Easiest way to run the PO-token server; Deno works too | `docker --version` |
| An LLM backend | For the `extract` stage only — one of LM Studio, Ollama, OpenRouter, NVIDIA NIM or any OpenAI-compatible API | see below |

You do **not** need ffmpeg, a YouTube API key or a Google account. yt-dlp is installed as
a Python dependency.

## Install from scratch

```bash
# 1. Install uv (pick one)
curl -LsSf https://astral.sh/uv/install.sh | sh          # official installer
# brew install uv                                        # or Homebrew

# 2. Get the code
git clone git@github.com:kaushalag29/founder-coach.git   # private during the beta
cd founder-coach

# 3. Create a virtualenv (uv downloads Python 3.12 if you don't have it)
uv venv --python 3.12
source .venv/bin/activate

# 4. Install ytbrain (editable) with test tools
uv pip install -e ".[dev]"
#    everything we use: extraction extras, Knowledge index + search (LanceDB,
#    sentence-transformers, PyTorch), the PO-token yt-dlp plugin, and the coach
#    runtime with the pack reader
uv pip install -e ".[extract,dev,index,pot,serve,pack,web]"
brew install deno                    # JavaScript runtime for yt-dlp
playwright install chromium          # the headless browser for JavaScript websites (web extra)

# 5. Create your config files from the templates (both are git-ignored)
cp sources.example.yaml sources.yaml
cp .env.example .env          # then fill in, see "Configure an LLM backend"

# 6. Check it works
ytbrain --help
for s in core eval pack coach plugin web; do python tests/test_$s.py || break; done   # no network needed
```

Every new terminal needs `source .venv/bin/activate` before `ytbrain` is on your PATH.

**Keep yt-dlp current.** YouTube changes often and yt-dlp follows within days. If fetching
suddenly breaks, update it first:

```bash
uv pip install -U "yt-dlp[default]"
```

**Give yt-dlp a JavaScript runtime.** Since yt-dlp 2025.11.12, full YouTube support needs
two things: the `yt-dlp-ejs` package, which `yt-dlp[default]` installs, and a JavaScript
runtime on your PATH. Without them yt-dlp falls back to fewer clients and hits more rate
limits. `ytbrain sync` checks this at the start and tells you what is missing.

```bash
brew install deno          # recommended; node >= 20, bun or quickjs also work
```

**Optional: a PO-token provider.** YouTube wants a proof-of-origin token on caption
requests from the web client. The [bgutil provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
generates one, and yt-dlp uses it automatically. It is a yt-dlp plugin plus a small local
server:

```bash
uv pip install -e ".[pot]"      # the yt-dlp plugin (or add pot to your other extras); same version as the server

# the provider, pick one:
# (a) Docker
docker run --name bgutil-provider -d --init -p 127.0.0.1:4416:4416 brainicism/bgutil-ytdlp-pot-provider

# (b) no Docker: run the server with Deno (leave this terminal open)
git clone --single-branch --branch 2.0.0 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git ~/bgutil-ytdlp-pot-provider
cd ~/bgutil-ytdlp-pot-provider/server
deno install --allow-scripts=npm:canvas --frozen
cd node_modules && deno run --allow-env --allow-net --allow-ffi=. --allow-read=. ../src/main.ts

# (c) no server: script mode. Keep the checkout from (b) in ~/bgutil-ytdlp-pot-provider
#     (or set YTBRAIN_POT_SCRIPT_HOME); the plugin then runs the provider per token.
```

`sync` reports which provider it found, or warns when the plugin is installed but
neither a server nor a script-mode checkout is there.

**Data location.** Everything ytbrain produces goes to `data/` inside the repo. If the repo
lives in a synced folder (iCloud, Dropbox, OneDrive), point ytbrain at a local folder —
SQLite needs real file locking:

```bash
export YTBRAIN_ROOT=~/ytbrain-data        # or put it in .env
```

---

## Configure an LLM backend (`.env`)

Only the `extract` stage uses an LLM. ytbrain reads a **`.env` file in the repo root
automatically** — no need to `source` it. `.env` is git-ignored; never commit API keys.

Precedence: a variable already set in your shell **overrides** `.env` (handy for one-off
runs: `YTBRAIN_LLM_MODEL=other ytbrain extract --limit 3`). When that happens ytbrain
prints a `note:` line so a leftover value can't silently win — open a new terminal or
`unset` it to go back to `.env`.

Start from `cp .env.example .env` (OpenRouter), or pick a template below and fill in the placeholders.

<details open>
<summary><b>OpenRouter</b> — recommended: fast, reliable, ≈ $1 for ~700 videos</summary>

1. Create an account and API key at [openrouter.ai](https://openrouter.ai) and add a few
   dollars of credit. Consider setting a credit limit on the key (e.g. $5).
2. `.env`:

```bash
export YTBRAIN_LLM_BACKEND=openai
export YTBRAIN_LLM_BASE_URL=https://openrouter.ai/api/v1
export YTBRAIN_LLM_API_KEY=sk-or-v1-...your-key...
export YTBRAIN_LLM_MODEL=deepseek/deepseek-v4-flash
export YTBRAIN_LLM_JSON_MODE=response_format
# only use providers that support JSON-schema output; no hidden "thinking"
export YTBRAIN_LLM_EXTRA_BODY='{"provider":{"require_parameters":true},"reasoning":{"enabled":false}}'
export YTBRAIN_LLM_WORKERS=5
export YTBRAIN_LLM_MAX_TOKENS=8192            # per-reply cap; a record is 2-3k tokens
export YTBRAIN_SINGLE_CALL_MAX_TOKENS=60000   # long-context model: whole talk in one call
```

Notes
- Keep `YTBRAIN_LLM_MAX_TOKENS` at 8192, the default. A complete record is 2–3k
  tokens. Replies that hit the cap were a provider looping on one value, e.g. `"idea",
  "idea", ...`, not a long answer. A higher cap only makes each loop take longer: 500 s
  and 16k tokens at 16384. ytbrain retries such a video with a shorter answer and, on
  OpenRouter, on a different provider.
- Paid models have no published per-key rate limit; ytbrain still caps itself at 30
  requests/min across all workers (`YTBRAIN_LLM_MAX_RPM`). 5–8 workers is a good range.
- **Free models** (`...:free`) are limited to 20 requests/min and **50 per day** (1 000 per
  day once you have bought ≥ $10 of credit). ytbrain automatically lowers its cap to 18/min
  for `:free` models and stops cleanly when the daily quota is used up.
- Out of credit (HTTP 402) stops the run; nothing is marked failed.

</details>

<details>
<summary><b>LM Studio</b> — local, free, private (tested on M2 Air 16 GB)</summary>

1. Install [LM Studio](https://lmstudio.ai) and download **Gemma 4 E4B** (MLX, 4-bit;
   id `gemma-4-e4b-it`). On 16 GB RAM, models much larger than ~8B will not fit.
2. Load the model with **Context Length 32768** (model loader → advanced settings, or
   `lms load gemma-4-e4b-it --context-length 32768`). The default context is far too small
   for a 10–20k-token transcript.
3. Developer tab → **Start Server** (default `http://localhost:1234/v1`).
4. `.env`:

```bash
export YTBRAIN_LLM_BACKEND=openai
export YTBRAIN_LLM_BASE_URL=http://localhost:1234/v1
export YTBRAIN_LLM_MODEL=gemma-4-e4b-it
export YTBRAIN_LLM_JSON_MODE=response_format
```

Notes
- No API key needed. Local endpoints are never rate-paced.
- `curl http://localhost:1234/v1/models` lists the exact model ids; if yours differs,
  `ytbrain extract` prints the valid ids and stops.
- To use `--workers N`, set **Max Concurrent Predictions ≥ N** in the model's load
  settings, otherwise requests just queue. On a fanless Air the gain is modest.
- Long runs: `caffeinate -i ytbrain extract` keeps the Mac awake; keep it plugged in.

</details>

<details>
<summary><b>Ollama</b> — local, free</summary>

1. Install [Ollama](https://ollama.com), then:

```bash
ollama serve                         # in its own terminal, if not already running
ollama pull qwen3:30b-a3b            # needs ~32 GB RAM; on 16 GB use qwen3:8b
```

2. `.env`:

```bash
export YTBRAIN_LLM_BACKEND=ollama
export YTBRAIN_LLM_BASE_URL=http://localhost:11434
export YTBRAIN_LLM_MODEL=qwen3:30b-a3b          # or qwen3:8b on 16 GB machines
export YTBRAIN_LLM_NUM_CTX=32768                # Ollama silently truncates beyond its default window
```

Notes
- ytbrain checks up front that Ollama is running and the model is pulled.
- JSON output is constrained with Ollama's native `format` (JSON Schema); no
  `YTBRAIN_LLM_JSON_MODE` needed.

</details>

<details>
<summary><b>NVIDIA NIM</b> — free tier (build.nvidia.com); good for experiments</summary>

1. Get an API key (`nvapi-...`) at [build.nvidia.com](https://build.nvidia.com).
2. Find a model that actually answers for your account — the catalog lists models that
   return 404 ("Function not found for account") or queue for minutes on the free tier:

```bash
python ops/probe_models.py                         # tests a list of candidates
python ops/probe_models.py nvidia/nemotron-3.5-lightning-30b-a3b
```

   The script prints the `.env` lines for the best working model.
3. `.env` (the combination that worked for us):

```bash
export YTBRAIN_LLM_BACKEND=openai
export YTBRAIN_LLM_BASE_URL=https://integrate.api.nvidia.com/v1
export YTBRAIN_LLM_API_KEY=nvapi-...your-key...
export YTBRAIN_LLM_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
export YTBRAIN_LLM_JSON_MODE=json_object
export YTBRAIN_LLM_EXTRA_BODY='{"chat_template_kwargs":{"enable_thinking":false}}'
```

Notes
- NVIDIA's hosted models reject the legacy `nvext.guided_json`; `json_schema` requests
  timed out for Nemotron, `json_object` worked. The schema is still enforced by ytbrain's
  own validation + repair loop.
- Free-tier capacity is shared and unpredictable (see NVIDIA developer-forum threads from
  Sep 2026). Models are retired without much notice (HTTP 410) — ytbrain stops cleanly
  instead of failing every video.

</details>

<details>
<summary><b>Any other OpenAI-compatible API</b> (vLLM, llama.cpp server, Gemini, hosted APIs)</summary>

```bash
export YTBRAIN_LLM_BACKEND=openai
export YTBRAIN_LLM_BASE_URL=https://your-endpoint/v1
export YTBRAIN_LLM_API_KEY=...
export YTBRAIN_LLM_MODEL=...
export YTBRAIN_LLM_JSON_MODE=response_format    # or json_object if json_schema is rejected
# per-model request fields, e.g. switching a model's "thinking" off:
# export YTBRAIN_LLM_EXTRA_BODY='{"chat_template_kwargs":{"enable_thinking":false}}'
```

Requirements: `POST {base}/chat/completions` and ideally `GET {base}/models`. Test with
`python ops/probe_models.py <model-id>` first.

</details>

**Before a long run, always probe and test on a few videos:**

```bash
python ops/probe_models.py <model-id>                       # expect "valid-json"
ytbrain extract --limit 5 && ytbrain verify && ytbrain report
```

### Claude Code for plugin development (your Claude plan, token-saving)

Everything that runs Claude Code while you develop the plugin (`ytbrain eval coach`,
`claude plugin eval`, a live session with `dist/plugin`) runs the **local `claude` CLI on your
Claude plan by default**, set up to use as little of its limits as possible:

- **Haiku** unless you ask for another model (`--model sonnet`; `YTBRAIN_COACH_MODEL` changes the default);
- `eval coach` caps each call at 12 turns (`--max-turns`) and runs every case in a scratch folder
  outside the repo, so the repo's CLAUDE.md/AGENTS.md never ride along; each gate prints the tokens it used;
- `claude plugin eval` with `--runs 1 --ablation none` while iterating (3 runs, with the
  without-plugin comparison, before a release).

```bash
ytbrain claude -- --plugin-dir dist/plugin                    # a live session with the plugin (Haiku)
python scripts/assemble_plugin.py --pack data/pack --with-evals --out dist/plugin-eval   # the eval build (never shipped)
ytbrain claude -- plugin eval dist/plugin-eval --runs 1 --ablation none   # the mocked skill checks
ytbrain eval coach                                            # the gates (Haiku, on your plan)
ytbrain eval coach --model sonnet                             # sign a release off on the model founders use
```

**Optional, OpenRouter instead of the plan** (paid per token, e.g. when the plan's limit is
reached): `ytbrain claude --openrouter -- …` or `ytbrain eval coach --host openrouter` with
`YTBRAIN_COACH_HOST_KEY` in `.env` (default model `anthropic/claude-haiku-4.5`; host cost counts in
`--max-cost`). Claude Code is built for Anthropic models; a non-Anthropic model is for smoke tests only.

---

## Configure sources (`sources.yaml`)

`sources.yaml` lists the Sources to ingest: YouTube playlists and websites (next section). It
is git-ignored (your list stays private);
start from `cp sources.example.yaml sources.yaml`, then build it interactively with
`discover` or edit it by hand. `discover` creates it if it doesn't exist.

### Discover playlists with yt-dlp (no API key)

```bash
ytbrain discover --method ytdlp --channel @ycombinator      # a channel: lists its playlists
ytbrain discover --method ytdlp --channel PLxxxxxxxxxxxx     # a single playlist id or URL
```

For a channel it lists every playlist on the `/playlists` tab; you choose which to keep
(`1,3,5-9`, `all`, or blank), name them, optionally add the channel's **uploads
catch-all** (`UU…`), and add extra playlists from other channels as
`Series Name|PLxxxx`. Re-running `discover` merges into the existing file without
creating duplicates.

`ytbrain discover --method api` instead prints YouTube Data API v3 calls for you to run
with your own key.

### Format

```yaml
defaults:                      # applied to every source unless the source overrides it
  sub_langs: en,en-orig,en-US,en-GB   # exact English tracks (en.* also pulls machine translations)
  sub_format: srt/best
  sleep_requests: 5            # seconds between yt-dlp requests; 5+ avoids most YouTube 429s
sources:
- id: startup_school_2026      # stable slug, unique
  name: Startup School 2026
  kind: playlist
  playlist_id: PLxxxxxxxxxxxx  # must be a PLAYLIST id (PL…/UU…), never a channel id (UC…)
  fallback: false
  verify: false
- id: yc_uploads
  name: YC uploads (catch-all)
  kind: playlist
  playlist_id: UUxxxxxxxxxxxx  # the channel's uploads playlist
  fallback: true               # catch-all: never overrides a video's specific series
  verify: false
```

A video that appears in both a specific playlist and the catch-all keeps the specific
playlist's series. Put `enabled: false` on a source to skip it.

### Websites

Any website can be a Source: give the URL, and optionally how far to follow links and whether
to use a browser. Nothing about the site's structure is configured; each page says what it is.

```yaml
- type: website
  url: https://paulgraham.com/articles.html
  depth: 1          # link hops from the start page (default 3)
  render: auto      # auto: a headless browser only when a page's text comes back thin | always | never
  enabled: true     # false: stop fetching (Documents stay until `ytbrain drop --source ID`)
  author: Paul Graham   # optional: the speaker when pages name no author
  id: paulgraham    # optional; default: the host + a short hash of the URL (used by --source)
  name: PG essays   # optional: the Series (default: the host)
  max_pages: 10000  # pages fetched per run (default 10 000)
```

```bash
uv pip install -e ".[web]"       # trafilatura (main text) + Playwright
playwright install chromium      # once, only needed for JavaScript sites
ytbrain sync                     # playlists and websites; then clean/extract/verify/index as usual
ytbrain sync --type website      # websites only (talks embedded on them still get their captions)
ytbrain sync --source ID --limit 20   # one Source, a small batch first
```

Point `url` at the page that lists what you want (an archive or index page) and keep `depth`
small; a trailing slash matters (`…/blog/` keeps the crawl inside `/blog/`, `…/blog` doesn't).

How a site is read (plan and acceptance tests: [docs/web-sources-plan.md](docs/web-sources-plan.md),
design: [ADR-0013](docs/adr/0013-sources-plug-in-through-adapters.md)):

- **Politely:** robots.txt obeyed (unreachable robots.txt means nothing is fetched that run), one
  request per second per host or the site's `Crawl-delay`, retries with backoff that honour
  `Retry-After`, an honest User-Agent, a 15 MB page limit, never logs in, no IP rotation, no
  browser-fingerprint tricks, no CAPTCHA solving.
- **Scope:** the start URL's host and directory; sitemaps seed the queue; `depth` link hops.
- **Each page** is typed from what it declares (schema.org JSON-LD, OpenGraph, meta tags,
  `rel=canonical`, `lang`) and its main text (trafilatura): an **article** becomes a Document
  (title, author as Speaker, date, site as provenance, paragraphs with their headings);
  a **listing** only contributes links; a page embedding a **YouTube talk** hands the video to
  YouTube sync (if YouTube has no captions for it, the page's own transcript is kept as an
  article); non-English pages are skipped for now.
- **Identity:** a page's id is a hash of its canonical URL (`w-` + 16 hex; the full hash is kept
  and a clash is refused, never merged). The same text at a new URL, even on a new domain, is
  the same Document: no second extraction.
- **Incremental and resumable:** the crawl queue lives in `data/manifest.db`, so an interrupted
  crawl continues; known pages are re-checked after 7 days (leaf pages at the Source's `depth`
  with a conditional GET, pages whose links are followed in full), and only a
  changed text goes through clean and extract again; a page that answers 404/410 leaves the
  index at the next `index`.
- **Same pipeline after `clean`:** articles are extracted with an article-worded prompt into the
  same record as talks, verified against their paragraphs, and cited as
  "Author, Title, year" linked to the page with a text fragment that opens at the quoted words
  (`https://…/essay.html#:~:text=…`), and shown as `¶4` in pages instead of `mm:ss`.

---

## Run the pipeline

### First run

```bash
ytbrain sync --limit 5                         # fetch captions for 5 videos per playlist (try it)
ytbrain sync                                   # then everything (long; resumable)
ytbrain clean                                  # captions → transcripts (seconds)
caffeinate -i ytbrain extract --workers 5      # LLM extraction (the long step)
ytbrain verify                                 # check every quote, write the pages
ytbrain report                                 # acceptance numbers
ytbrain sample --n 10                          # review sheet for a human read
```

(`caffeinate -i` is macOS-only and just keeps the machine awake; omit it on Linux.)

Then, with the `index` extra installed:

```bash
caffeinate -i ytbrain index                    # embed Verified knowledge (~1 h on an M2 Air)
ytbrain search "how do I find product-market fit" --stage idea
```

### Every run after that

```bash
caffeinate -i sh -c 'ytbrain run; ytbrain report'
```

`run` = sync → clean → extract → verify → index. Every Step only processes what is new,
changed or previously failed, so a daily run over an unchanged corpus finishes in seconds.

- **Index:** skipped with a note if the optional `index` extra isn't installed.
- **Sync failure:** if sync fails for every source (YouTube throttling your IP), `run` stops
  there and nothing downstream runs.
- **Refused LLM:** if the endpoint refuses the requests (key, credit, quota), `run` still
  verifies what was extracted, then stops and records the run as *failed*.

### Re-extracting without touching YouTube

After a schema bump or a model switch, extraction must be redone. Sync has nothing to do
then, and it could stop the run if YouTube throttles you, so skip it:

```bash
caffeinate -i sh -c 'ytbrain extract && ytbrain verify && ytbrain index; ytbrain report'
```

`extract` returns an error only when the LLM endpoint refuses the requests. Videos that
fail one by one don't break the chain, and `report` runs either way. `index` then re-embeds
only the talks whose records changed.

### Commands

| Command | Does | Useful flags |
|---|---|---|
| `discover` | Build/extend `sources.yaml` | `--method ytdlp\|api`, `--channel` |
| `sync` | List playlists and download captions + `info.json` (no video); crawl websites | `--type website\|youtube`, `--source ID`, `--limit N` (per Source), `--force` (re-fetch), `--reconcile` (mark removed videos), `--backfill` (slowly retry rate-limited videos only), `--per-hour N`, `--sleep-subtitles S` |
| `clean` | Caption file → deduplicated, ~10–25 s timestamped utterances; web page → numbered paragraphs | `--limit N`, `--retry-failed` |
| `extract` | One LLM call per Document → structured record | `--limit N`, `--workers N`, `--retry-failed` |
| `verify` | Locate every evidence quote in the transcript; write pages | `--limit N`, `--retry-failed` |
| `index` | Build the Knowledge index (advice, takeaways, summaries, transcript passages) with local embeddings; only Verified knowledge | `--limit N`, `--device auto\|mps\|cpu\|cuda`, `--retry-failed` |
| `search "question"` | Hybrid search (vector + full-text, reranked) with deep-link citations | `--stage`, `--require-stage`, `--kind`, `--topic`, `--top-k`, `--no-rerank`, `--device`, `--json`, `--pack [PATH]` |
| `eval build` | Build or resume the eval benchmark ([docs/eval-spec.md](docs/eval-spec.md)): generated Tuning questions, pooled candidates, two-judge grading; resumable and spend-capped | `--set dev\|test`, `--max-cost`, `--workers`, `--limit`, `--device` |
| `eval run` | Score a search configuration: nDCG@10, recall, MRR with 95 % intervals; compare with a baseline and apply the regression gate (0.03 nDCG@10 for pack configs) | `--set`, `--config full\|no-rerank\|stage-boost\|pack\|pack-no-rerank\|full-no-passages`, `--save-baseline`, `--compare CONFIG`, `--pack PATH`, `--label NAME` (a variant saved as `<config>-NAME`), `--smoke`, `--device` |
| `pack build` | Build the Knowledge pack the coach plugin ships: every Verified advice/takeaway/summary (Passages only with `--with-passages`, private beta) embedded with a small ONNX model into one SQLite file; resumable, re-embeds only new or changed items | `--embed-model`, `--rerank-model` (`none` = no reranker, the default), `--batch`, `--device cpu\|coreml\|cuda`, `--with-passages`, `--out DIR` |
| `pack info` | Describe the pack and verify its checksum | `--out DIR` |
| `eval judge` | Grade the Moments a saved run retrieved that no judge has seen (pool extension), then re-release the labels as a new minor version; resumable and spend-capped | `--config C` (repeatable), `--set`, `--depth`, `--max-cost`, `--workers` |
| `eval rescore` | Score a saved run again against the current labels, without searching (seconds) | `--config`, `--set`, `--compare`, `--save-baseline` |
| `eval status` | Progress and LLM spend per split | |
| `pages` | Re-render all markdown pages from the JSON records | |
| `refresh` | Re-apply Series and provenance from `sources.yaml` and yt-dlp metadata to documents, records and pages (no network, no LLM) | |
| `run` | sync → clean → extract → verify → index | `--limit` (per playlist for sync, per Step after), `--workers`, `--device`, `--force`, `--reconcile`, `--source ID`, `--type website\|youtube` |
| `report` | Acceptance numbers + last three runs | |
| `sample` | Stratified review sheet in `data/reports/` | `--n`, `--seed` |
| `status` | Count of finished items per stage | |
| `invalidate <stage>` | Mark `fetch`, `clean`, `extract`, `verify` or `index` for redo | `--only-flagged`, `--source ID`, `--reason` |
| `drop --source ID` | Take a Source's Documents out of the knowledge (then `index`) | |
| `eval coach` | Gates G2, G4, G5, G6 on the real host: the local Claude Code on your plan (Haiku, token-saving); stops if the plugin is reassembled mid-run | `--gate`, `--limit`, `--model`, `--max-turns`, `--host claude\|openrouter`, `--max-cost`, `--plugin DIR` (default `dist/plugin`) |
| `claude [--model M] [--openrouter] -- <args>` | The local Claude Code for plugin work, on your plan with Haiku | `--model`, `--openrouter` (before `--`) |

`--limit` counts Documents per Step (for `sync`, per Source). A Document that fails a Step 3 runs in a row is parked;
`--retry-failed` tries it again.

### From corpus to plugin: the whole flow

```bash
ytbrain run                                        # sync -> clean -> extract -> verify -> index
ytbrain report && ytbrain sample --n 10            # quality gates + a human read
ytbrain pack build                                 # the Knowledge pack (data/pack)
python scripts/assemble_plugin.py --pack data/pack --check    # -> dist/plugin
ytbrain eval coach --limit 2 && ytbrain eval coach # the gates (your Claude plan, Haiku)
ytbrain claude -- --plugin-dir dist/plugin         # a live session: /founder-coach:setup
python scripts/release.py …                        # a beta release (docs/release.md)
```

### Search the knowledge (phase 2, M1)

```bash
uv pip install -e ".[index]"          # LanceDB + sentence-transformers + PyTorch (MPS on Apple silicon)
python -c "import torch; print(torch.backends.mps.is_available())"   # True on Apple silicon
ytbrain index --limit 5               # try it; first run downloads the embedding model (~2.3 GB)
ytbrain index                         # the rest; resumable, re-embeds only changed talks
ytbrain search "how should I price a B2B pilot" --stage mvp
ytbrain search "cofounder conflict" --kind advice --device cpu --no-rerank
```

What gets indexed are **Knowledge items**, and only Verified ones:

| Kind | One item per | Why |
|---|---|---|
| `advice` | advice atom with a verified quote | the coach's main currency |
| `takeaway` | verified highlight | broader lessons |
| `summary` | talk | "which talk covers X" |
| `passage` | ~550-token (2–3 min) transcript window, slightly overlapping | anything the extraction missed |

Each item is embedded together with a short header (talk title, speaker, series) and its quote.
Every result links to the exact second in the video.

How `search` ranks results:

1. **Candidates.** It takes the top 50 from vector search and the top 50 from full-text
   search, and merges them (reciprocal-rank fusion).
2. **Rerank.** The cross-encoder re-scores the merged list.
3. **Boosts.** Small boosts apply for a matching `--stage` (Stage ranks results, it never
   filters them, unless you add `--require-stage`), for the kind of item (advice first),
   and for recent talks.
4. **Diversity.** At most 3 results come from one talk, and only one per quote (an advice
   item and a takeaway often cite the same sentence).

- **First `search`:** downloads the reranker (~2.3 GB) once; `--no-rerank` skips it.
- **`--json`:** prints results for scripts and agents.
- **Longer items:** a note is printed if an item may exceed the 1024-token embedding window.
  On the YC corpus none do; the longest passage is ~870 tokens.

### The Knowledge pack (what the coach plugin ships)

The plugin can't ask founders to install PyTorch or download 4.6 GB of models, so it ships
a **Knowledge pack** instead of the index ([ADR-0009](docs/adr/0009-plugin-ships-a-knowledge-pack-without-passages.md)):

- **What's in it:** every Verified advice, takeaway and summary (transcript Passages only with
  `--with-passages`, for the private beta) with its citation, a full-text index and one vector per
  item. One SQLite file of about 60 MB with the default model.
- **Models:** a small ONNX embedder through fastembed, with no torch. The default is
  `BAAI/bge-base-en-v1.5` (0.21 GB) with no reranker: on labels v1.2.0,
  `jinaai/jina-reranker-v1-turbo-en` lowered the pack's nDCG@10 (0.457 vs 0.479) and slowed every
  search (`pack build --rerank-model` still sets one). Models download once to
  `~/.founder-coach/models`.
- **Same ranking code:** search, fusion, boosts and diversity are the same code the index
  uses (`founder_coach/search.py`), so `eval run` measures exactly what founders get.

```bash
uv pip install -e ".[pack]"            # fastembed + onnxruntime (already in the eval extra)
ytbrain pack build                     # a few minutes on a laptop CPU; resumable; writes data/pack/
ytbrain pack info                      # items, talks, models, size; verifies the checksum
ytbrain search "how should I price a B2B pilot" --pack
ytbrain eval run --set dev --config pack --compare full            # ADR-0009 gate: within 0.03 nDCG@10
```

To compare a variant, build it into its own folder and score it under its own name:
`ytbrain pack build --embed-model BAAI/bge-small-en-v1.5 --out data/pack-small`, then
`ytbrain eval run --config pack --pack data/pack-small --label small --compare full`, then
`ytbrain eval judge --config pack-small` and `ytbrain eval rescore --config pack-small --compare full`
(a variant returns Moments no judge has seen yet).

### The coach plugin (M3b)

`plugin/` is the Claude Code plugin **as a template**: its files say `{{id}}` where the product
id goes, and `scripts/assemble_plugin.py` fills it in from `product.toml` (ADR-0012). It holds
the manifest, the MCP server config (`coach`, started with
`uvx --from ${CLAUDE_PLUGIN_ROOT} founder-coach serve --pack ${CLAUDE_PLUGIN_ROOT}/pack`), a fail-open SessionStart hook and 9
skills: `coach` (the coaching contract, model-only); `ask`, `weekly-focus`, `check-in`
(Playbooks); `setup`, `status`, `export`, `forget`, `feedback` (typed by the Founder). The skills
are the single source for the MCP prompts: the assembler regenerates `founder_coach/playbooks/*.md`
from them, and `founder_coach/product.json` from `product.toml`.

```bash
python scripts/assemble_plugin.py --pack data/pack --check   # -> dist/plugin (previous build kept)
claude plugin validate dist/plugin --strict                  # no model call
ytbrain claude -- --plugin-dir dist/plugin                   # then: /founder-coach:setup (your plan, Haiku)
```

Load `dist/plugin`, never `plugin/` itself. `--check` imports the assembled runtime and verifies
the pack's checksum before swapping the build in, so a broken build never replaces a working
one. Only `knowledge.sqlite` and `pack.json` ship; the embedding cache stays behind. Beta
releases go through `scripts/release.py` into the marketplace repo ([docs/release.md](docs/release.md)).

### The coach runtime (`founder-coach`, M3a)

The plugin runs `founder_coach`, a small package that never imports ytbrain, torch or LanceDB.
It serves the Knowledge pack and remembers the Founder, over MCP.

```bash
uv pip install -e ".[serve,pack]"
founder-coach warmup --pack data/pack        # download the search models once
founder-coach status --json                  # the same as JSON; every command takes --home DIR (the whole data folder)
founder-coach serve --pack data/pack         # the stdio MCP server (what the plugin starts)
founder-coach status --pack data/pack        # pack, models, profile, what's due
founder-coach hook session-start             # what the SessionStart hook prints (silent when nothing is due)
founder-coach export                         # everything remembered, as JSON + Markdown
founder-coach forget                         # delete it all (type the company name; one backup is kept)
founder-coach forget --confirm "Acme"        # the same without a prompt (required when there's no terminal)
founder-coach restore --list                 # backups, newest first, each with its integrity check
founder-coach restore [LABEL]                # restore a backup (default: newest good one); old file set aside
founder-coach feedback list|export [--out D] # the Founder's Feedback; export writes only that, to send
founder-coach usage summary|export|clear     # the local usage log (docs/usage-log.md); clear needs --yes
founder-coach usage summary --days 30 --json # a window, as JSON; forget --no-backup also deletes the backups
```

- **8 MCP tools, in this order:**
  - `coach_search`: cited advice; keyword-only while the models load (a search waits up to 20 s
    first); flags likely Gaps;
  - `coach_read`: a whole talk's items;
  - `coach_get_context`: profile, Goals, Commitments, last Check-in, Decisions, Nudges;
  - `coach_update_profile`, `coach_record`, `coach_update`: every write needs a `request_id`
    and is logged;
  - `coach_corpus_status`;
  - `coach_feedback`: a Founder's report of a wrong answer, saved only after they approve it.
- **Also served:** 4 prompts (`ask`, `weekly-focus`, `check-in`, `setup`) and the resources
  `founder://profile`, `founder://this-week`, `corpus://status` and `corpus://item/{id}`.
- **Founder data** lives in `~/.founder-coach/`:
  - `founder.db`, with a change log of every write;
  - `FOUNDER.md`, regenerated after every write;
  - `backups/`, daily (7 kept), before migrations and before forget;
  - Feedback, inside `founder.db` (since schema v3); `forget` deletes it too, and it isn't part of FOUNDER.md;
  - the usage log, also inside `founder.db`: one event per tool call (tool, time, duration, outcome,
    counts and item ids, never the Founder's words unless `FOUNDER_COACH_USAGE_TEXT=1`), kept 90 days,
    off with `FOUNDER_COACH_USAGE=0` ([docs/usage-log.md](docs/usage-log.md)). `/founder-coach:setup` tells
    the Founder; `/founder-coach:feedback` offers its export. While dogfooding, the maintainer can
    `export FOUNDER_COACH_USAGE_TEXT=1` to also see the search text (never asked of testers).

  Several host sessions can use the store at once.
- **Failures stay contained.** The pack and the store open independently:
  - a pack that is missing or fails its sha256 check (against `pack.json`) is reported as
    unavailable by `coach_corpus_status` and `coach_search`, and memory keeps working;
  - a store that can't be read or fails `PRAGMA integrity_check` leaves search working, and the
    memory tools answer "nothing has been deleted: run `founder-coach restore`". Restore moves
    the damaged `founder.db` (and `-wal`/`-shm`) to `before-restore-<time>/`, never deletes it;
  - an ONNX error during one search answers with keyword matches (`mode: keyword`, with a note).
- **Settings** (the prefix follows the product id): `FOUNDER_COACH_HOME`, `FOUNDER_COACH_PACK`,
  `FOUNDER_COACH_MODELS`, `FOUNDER_COACH_RERANK` (`0`/`1`, default: what the pack says),
  `FOUNDER_COACH_GAP_SIMILARITY` (provisional 0.60), `FOUNDER_COACH_SEARCH_WAIT_S` (how long the
  first search waits for loading models, default 20), `FOUNDER_COACH_LOG` (log level, stderr),
  `FOUNDER_COACH_WARMUP=0` (don't load models; tests), `FOUNDER_COACH_USAGE` (`0` turns the usage
  log off), `FOUNDER_COACH_USAGE_TEXT` (`1` also logs search text), `FOUNDER_COACH_USAGE_DAYS`
  (retention, default 90). The pack is looked for in `--pack`, `$FOUNDER_COACH_PACK`, the plugin's
  `pack/`, the repo's `data/pack`, then `~/.founder-coach/pack`. All of them are listed in `.env.example`.
  They come from the environment, then `$FOUNDER_COACH_ENV_FILE`, `./.env` (the folder Claude Code was
  started in) and `~/.founder-coach/.env`; only `FOUNDER_COACH_*` lines are read, never keys, and
  `founder-coach status` shows which files were used. `ytbrain claude` hands the repo's `.env` to
  `claude plugin eval` too (its sandbox passes only `EVAL_*` variables), minus the location settings.

### What each stage writes

| Stage | Output |
|---|---|
| sync | `data/raw/<id>.info.json`, `data/raw/<id>.en.srt` (+ `en-orig` etc.) |
| clean | `data/transcripts/<id>.json` |
| extract | `data/metadata/<id>.json` |
| extract (failures) | `data/reports/extract-failures/<id>.json` — the call log of a video extract gave up on; removed once it succeeds |
| verify | updates `data/metadata/<id>.json` (match scores, timestamps) and writes `data/pages/<id>.md` |
| index | `data/lancedb/` — one `knowledge` table of Knowledge items with vectors and a full-text index |
| pack build | `data/pack/knowledge.sqlite` + `pack.json` (sha256, models, corpus stats) + `embed-cache.db` |
| all | `data/manifest.db` — SQLite: one row per video, per-stage status, run history |

Progress at any time:

```bash
sqlite3 data/manifest.db "SELECT stage, status, COUNT(*) FROM stage_state GROUP BY 1,2;"
```

---

## Choosing and switching models

1. `python ops/probe_models.py <model-a> <model-b>` — which models answer, with valid JSON, how fast.
2. Try each on the same 5 videos (extract always processes the newest videos first):

```bash
ytbrain invalidate extract --reason "try model A"
YTBRAIN_LLM_MODEL=<model-a> ytbrain extract --limit 5 && ytbrain verify && ytbrain report
```

3. Pick on evidence pass rate, category/people quality in `data/pages/`, speed and cost.
4. **Use one model for the whole corpus.** Every record stores its model and prompt hash;
   mixed models make the quality numbers uninterpretable. To switch after a full run:
   `ytbrain invalidate extract` then `ytbrain extract`.

---

## Parallelism, rate limits and retries

### extract

- `--workers N` (or `YTBRAIN_LLM_WORKERS`) runs N LLM calls in parallel. Results are still
  saved one at a time, so resume works exactly as with one worker. With several workers a
  video's line prints when it finishes, and a *still working on …* line appears if nothing
  finishes for 60 s.
- **Rate pacing only for hosted endpoints** (anything not `localhost`/`127.*`/`*.local`/Ollama):
  all workers share one budget of `YTBRAIN_LLM_MAX_RPM` requests/min (default 30; 18 for
  OpenRouter `:free` models). A 429 on any worker pauses all of them.
- **Per request**, exponential backoff with jitter (10, 20, 40, 80, 160 s; the server's
  `Retry-After` wins) for: 429 and 5xx, dropped connections, and HTTP 200 responses that
  carry an error or no content. A timed-out request (default 900 s) is retried once.
- **Per video**, if a video still fails it is retried twice more (30 s, 60 s).
  If the model cannot produce valid JSON even after two repair prompts, the video is marked
  failed and not retried in that run (same prompt, same answer).
- **Per answer** (schema 2.2.0), a few retries target specific problems:
  - A reply cut off at the token cap is asked again with a "be concise" instruction instead
    of being repaired. On OpenRouter the retry skips the provider that produced it. In
    practice these were providers looping on one value, not long answers. A cut-off reply
    that is already complete JSON is simply kept.
  - A repair prompt includes the whole previous answer and the transcript.
  - If fewer than 60 % of quotes are found in the transcript, or a talk of 1 000+ words
    yields no advice, the video is retried once with that feedback, and the better attempt
    is kept.
  - Transcripts over `YTBRAIN_SINGLE_CALL_MAX_TOKENS` are split into a few ~8k-token
    windows, plus one overview call for title, speaker, category and
    summary.
- Every LLM call (kind, tokens, seconds, outcome) is logged in the record's
  `extraction_meta.calls`.
- **Stops the run** (without marking anything failed) on a bad key or model
  (401/403/404/410), no credit (402) or an exhausted daily quota.

### sync (YouTube)

- ytbrain is anonymous; YouTube allows guests roughly 300 videos / ~1 000 requests per
  hour per IP (yt-dlp wiki). `sleep_requests: 5` keeps you under that.
- **Two kinds of pause:**
  - `sleep_requests` (in `sources.yaml`, default 5 s) is yt-dlp's pause between *every*
    request: the watch page, player API calls and each caption file. A video needs about
    5–8 requests, so this caps you at roughly 720 requests an hour, under the guest
    limit of about 1 000.
  - The subtitle pause (`YTBRAIN_YT_SLEEP_SUBTITLES`, default 10 s; `--sleep-subtitles`)
    is an extra wait before each caption file. A video usually has two (`en` +
    `en-orig`).
  - yt-dlp issue #13831 reports 60 s ending auto-caption 429s, but without a JavaScript
    runtime or PO tokens. With both, 10–15 s has been enough. Raise it if 429s come back.
- **`ytbrain sync --backfill`** retries only videos whose caption fetch failed for a
  reason a retry can fix, such as HTTP 429.
  - It doesn't list playlists, pauses 15 s before each subtitle file, and paces itself
    to 60 videos an hour (`--per-hour N`).
  - Leave it running (`caffeinate -i ytbrain sync --backfill`). It stops on its own if
    YouTube keeps refusing.
- yt-dlp itself retries 5xx and connection errors, with exponential sleeps configured by
  ytbrain (`--retry-sleep http:exp=2:60`, `extractor:exp=5:120`).
- yt-dlp does **not** retry a caption HTTP 429, so ytbrain does: cooldown 30 → 60 → 120 →
  240 s and retry the same video (up to `YTBRAIN_YT_VIDEO_RETRIES`, default 2). Other
  transient errors: 10 s, 20 s.
- **5 rate limits in a row stop the sync** — the IP is throttled and more requests only
  extend it. Wait an hour or two and run again.
- Private, removed and members-only videos are recorded as *unavailable* and never
  retried; videos without English captions as *no captions*.
- Playlist listing is retried after 30 s and 60 s.
- **Proxy (optional):** `YTBRAIN_YTDLP_PROXY` is passed to yt-dlp's `--proxy`, for
  example behind a company network. ytbrain adds no IP rotation of its own. Rotating IPs
  to get around YouTube's rate limits is circumvention, which YouTube's terms prohibit.
  Slower pacing and `--backfill` are the supported answer.

---

## Interrupting and resuming

Stop any command at any time — **Ctrl+C**, closing the terminal, `kill`, a launchd stop,
even a crash or power loss — and run the same command again. It continues where it stopped.

- A video is marked done only **after** its output file is complete. The one in flight
  was never marked, so it is simply redone.
- Every file is written to a temporary name and renamed (yt-dlp does the same), so no
  output is ever half-written. Leftover temp files are cleaned up on the next run.
- A single run at a time is enforced with an OS file lock (`flock`) on `data/ytbrain.lock`.
  The operating system releases it the moment the holding process exits, even on `kill -9` or a
  crash, so a dead run can never block the next one (a reused PID can't fool it either).
- Damaged or missing inputs are **sent back to the stage that produces them, never
  silently skipped**: a missing caption file goes back to `sync`, a corrupt transcript to
  `clean`, a corrupt record to `extract`.
- `report` counts only current records; files left by an interrupted re-extraction are
  shown as *outdated on disk* and excluded.
- `index` checkpoints per talk. An interrupted index run leaves every finished talk in
  place and redoes the one in flight. A talk is re-embedded when its record or transcript
  changes, or when the embedding model changes.

---

## Quality checks

- **`verify`** fuzzy-matches every evidence quote and derives the timestamp from the
  transcript; the model never emits timestamps. A quote passes if either test holds:
  - word-level Jaccard ≥ 0.70 against a quote-sized window, ignoring "um/uh";
  - ≥ 90 % of its words (at least 8 distinct ones) appear within a window up to 1.5× its
    length. This accepts quotes the model tidied (dropped "like", "you know", repeated
    words) while still rejecting invented words and repetition loops.

  Each part of a `...`-joined quote is checked separately.
- **Empty records are flagged, not passed.** A talk of 1 000+ words that yields no items,
  or a practical (non-story, non-trends) talk that yields no advice, is marked *flagged*
  with the reason, so `invalidate extract --only-flagged` picks it up.
- **Pages withhold unverified claims.** A takeaway or advice item whose quote is not in
  the transcript is left out of `data/pages/*.md` (kept in the JSON with its score); the
  page says how many were withheld (`withheld_unverified` in the front matter).
  Typical cause: the model "remembers" a famous quote shown on a slide but never spoken.
- **`report`** gates: record rate ≥ 90 % (captioned videos with a record) and evidence
  pass rate ≥ 90 %. It also lists:
  - why videos have no transcript: private, no English captions, or rate-limited and due
    for another `sync`;
  - every flagged record with its reason;
  - every failed extraction, with a call log under `data/reports/extract-failures/`
    (each LLM call's finish reason, token usage including hidden reasoning tokens,
    provider, seconds, and the start and end of any answer that failed).
- **`sample --n 10`** writes a stratified review sheet (unverified claims included and
  flagged) — read it against the videos and confirm nothing is fabricated.
- **Redo weak records** with a stronger model:

```bash
ytbrain invalidate extract --only-flagged
YTBRAIN_LLM_MODEL=<stronger-model> ytbrain extract && ytbrain verify && ytbrain report
```

- **Re-check only** (after a verifier change, no LLM cost): `ytbrain invalidate verify && ytbrain verify`.

---

## Output format

Each video gets `data/metadata/<video_id>.json` (canonical) and `data/pages/<video_id>.md`
(readable projection, regenerated — don't edit by hand). A record contains:

- `title_canonical`, `speaker`, `summary` (~150 words)
- `category` — exactly one of: `ideation-problem-selection`, `customer-discovery`,
  `product-mvp`, `product-market-fit`, `growth-distribution`, `marketing-gtm`, `sales`,
  `fundraising`, `finance-metrics`, `hiring-team`, `cofounder-dynamics`, `legal-equity`,
  `operations-execution`, `founder-psychology`, `ai-and-tech-trends`, `founder-story`, `other`
- `stage_relevance` — any of `pre-idea`, `idea`, `mvp`, `pmf`, `growth`, `fundraising`,
  `scaling`, `exit`
- `highlights` and `advice_atoms`, each with `evidence_span`, `timestamp_ms`, `match_score`
- `entities` (people, companies, frameworks, books, YC jargon), `unknowns_and_gaps`
- `chapters` (uploader's when present, otherwise LLM-generated and snapped to real timestamps)
- `extraction_meta`: model, backend, schema version, prompt hash, verification result

To change the category list: edit `Category` in `ytbrain/extract/schema.py` and bump
`SCHEMA_VERSION` in `ytbrain/config.py` — the next `extract` re-extracts older records
automatically.

---

## Scheduling (macOS launchd)

`ops/com.ytbrain.sync.plist` runs `ytbrain run` daily at 03:15. launchd (not cron) runs a
missed job when the Mac wakes. Edit the absolute paths inside, then:

```bash
cp ops/com.ytbrain.sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ytbrain.sync.plist
launchctl list | grep ytbrain
```

`ops/logrotate-run-log.sh` rotates `data/run.log` past 5 MB. `ytbrain report` shows
whether recent runs succeeded (a refused LLM endpoint marks the run *failed*).

---

## Configuration reference

All settings are environment variables (`.env` or shell). Defaults in parentheses.

| Variable | Purpose |
|---|---|
| `YTBRAIN_ROOT` (repo dir) | Where `data/` lives |
| `YTBRAIN_LLM_BACKEND` (`ollama`) | `ollama` or `openai` (any OpenAI-compatible API) |
| `YTBRAIN_LLM_BASE_URL` (`http://localhost:11434`) | Endpoint base URL |
| `YTBRAIN_LLM_API_KEY` (none) | API key for hosted providers |
| `YTBRAIN_LLM_MODEL` (`qwen3:30b-a3b`) | Model id |
| `YTBRAIN_LLM_JSON_MODE` (`response_format`) | `response_format` (JSON schema), `json_object`, or legacy `nvext` |
| `YTBRAIN_LLM_EXTRA_BODY` (none) | JSON merged into every request, e.g. provider routing or thinking switches |
| `YTBRAIN_LLM_WORKERS` (1) | Default for `--workers` |
| `YTBRAIN_LLM_MAX_RPM` (30; 18 for `:free`) | Shared requests/min cap for hosted endpoints |
| `YTBRAIN_LLM_MAX_TOKENS` (8192) | Output token cap per request |
| `YTBRAIN_LLM_NUM_CTX` (32768) | Ollama context window |
| `YTBRAIN_LLM_TIMEOUT_S` (900) | Per-request timeout |
| `YTBRAIN_LLM_MAX_RETRIES` (5) | Per-request retries |
| `YTBRAIN_LLM_PREFLIGHT_S` (20) | Timeout per try of the start-up "model responds?" check |
| `YTBRAIN_SINGLE_CALL_MAX_TOKENS` (20000) | Transcripts larger than this are extracted in ~8k-token windows; raise it (e.g. 60000) for long-context hosted models |
| `YTBRAIN_EXTRACT_VIDEO_RETRIES` (2) | Whole-video retries in extract |
| `YTBRAIN_YT_VIDEO_RETRIES` (2) | Same-video retries in sync |
| `YTBRAIN_YT_SLEEP_SUBTITLES` (10) | Seconds yt-dlp waits before each subtitle file (`--backfill` uses at least 15) |
| `YTBRAIN_YTDLP_PROXY` (none) | Passed to yt-dlp `--proxy`; credentials are never printed |
| `YTBRAIN_POT_PROVIDER_URL` (`http://127.0.0.1:4416`) | Where the optional bgutil PO-token server listens |
| `YTBRAIN_POT_SCRIPT_HOME` (`~/bgutil-ytdlp-pot-provider/server`) | bgutil checkout for script mode (no server) |
| `YTBRAIN_EVAL_GENERATOR` (`YTBRAIN_LLM_MODEL`, else `deepseek/deepseek-v4-flash`) | Writes the eval questions |
| `YTBRAIN_EVAL_JUDGES` (`google/gemma-4-31b-it,qwen/qwen3.8-flash,mistralai/mistral-medium-3.1`) | Two judges and a tie-breaker from other model families; an unlisted model falls back to the cheapest of its family |
| `YTBRAIN_EVAL_WORKERS` (8) · `YTBRAIN_EVAL_MAX_RPM` (60) · `YTBRAIN_EVAL_MAX_COST` (5) · `YTBRAIN_EVAL_TIMEOUT_S` (120) | Eval parallelism, the request-rate ceiling (the working rate halves on 429s and climbs back), the per-split USD spend cap, and how long one eval call may take before it's retried |
| `YTBRAIN_EMBED_MODEL` (`BAAI/bge-m3`) | Local embedding model for the Knowledge index; changing it rebuilds the index |
| `YTBRAIN_RERANK_MODEL` (`BAAI/bge-reranker-v2-m3`) | Cross-encoder reranker for `search`; `none` disables |
| `YTBRAIN_PACK_EMBED_MODEL` (`BAAI/bge-base-en-v1.5`) · `YTBRAIN_PACK_RERANK_MODEL` (`none`) | ONNX models for `pack build` (fastembed names); the pack records them, so its searches always use the same ones |
| `FOUNDER_COACH_HOME` (`~/.founder-coach`) · `FOUNDER_COACH_MODELS` (`$FOUNDER_COACH_HOME/models`) | Where the coach runtime keeps its data and downloaded ONNX models; the other `FOUNDER_COACH_*` settings are under [The coach runtime](#the-coach-runtime-founder-coach-m3a) |
| `YTBRAIN_COACH_MODEL` (`haiku`) | Default host model for `eval coach` and `ytbrain claude` |
| `YTBRAIN_COACH_HOST_KEY` (none) | OpenRouter key for `--host openrouter` / `--openrouter`; falls back to `YTBRAIN_LLM_API_KEY` when `YTBRAIN_LLM_BASE_URL` is OpenRouter |
| `YTBRAIN_WEB_CONTACT` (the repo URL) · `YTBRAIN_WEB_USER_AGENT` (`ytbrain/0.1 (+<contact>; polite research crawler)`) | The crawler's honest User-Agent; set your own contact before crawling |
| `YTBRAIN_DOTENV` (on) | `0` skips loading `.env` (the tests set it) |
| `YTBRAIN_DEVICE` (`auto`) | PyTorch device: `auto` (mps > cuda > cpu), `mps`, `cpu`, `cuda`; an unavailable explicit device is an error, not a silent fallback |

Tunables without an environment variable (thresholds, backoff bases, minimum transcript
length) are documented in `ytbrain/config.py`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ytbrain: command not found` | Activate the venv: `source .venv/bin/activate` |
| `HTTP Error 429: Too Many Requests` in sync | YouTube is rate-limiting your IP. First fix any `WARNING` lines `sync` prints (missing yt-dlp-ejs, JavaScript runtime or PO-token server). Keep `sleep_requests` ≥ 5. If sync stops after 5 in a row, wait 1–2 h, then run `caffeinate -i ytbrain sync --backfill` |
| `Sign in to confirm you're not a bot` / PO-token errors | YouTube anti-bot checks. `uv pip install -U yt-dlp`, run from a home connection; see the [yt-dlp wiki](https://github.com/yt-dlp/yt-dlp/wiki/Extractors) |
| `UNAVAILABLE (private video)` | The playlist lists a private/removed video; nothing to do |
| `Tunnel connection failed: 403` | You are behind a proxy/sandbox that blocks YouTube; run on your own machine |
| `note: X from your shell … overrides .env` | An old value is exported in this terminal; open a new one or `unset X` |
| `model '…' not served … Set YTBRAIN_LLM_MODEL to one of: …` | Wrong model id; copy one from the list |
| `… is listed but not callable: HTTP 404` | Provider lists the model but your account can't use it; pick another (`ops/probe_models.py`) |
| `queued or down right now` | Hosted free tier is congested; retry later or switch backend |
| `extract failures` in `report` | Open the call log it names. `"finish": "length"` with a `tail` that repeats one value is a provider loop, retried elsewhere automatically. With large `reasoning_tokens`, hidden thinking used up the cap: check that `YTBRAIN_LLM_EXTRA_BODY` turns reasoning off |
| Every extract shows `repair 1` | Model struggles with the JSON schema; try `YTBRAIN_LLM_JSON_MODE=response_format` or a stronger model |
| LM Studio output truncated or nonsensical on long talks | Context length too small; load the model with 32768 |
| `another ytbrain run is active (pid N)` | Another command is running; only one at a time. A dead PID is cleared automatically |
| `sqlite3: no such table` | The database is `data/manifest.db`, not `manifest.db` in the repo root |
| `the knowledge index needs the optional "index" extra` | `uv pip install -e ".[index]"` **inside the activated `.venv`** (check with `which python`) |
| `search` / `index` pauses on a big download | First use of each model (~2.3 GB each), cached afterwards in `~/.cache/huggingface` |
| `device 'mps' requested but not available` / MPS errors | Use `--device cpu` (slower, same results); `auto` falls back on its own |
| `search` slow or memory-tight on 16 GB | `--no-rerank` skips the second model |

---

## Project layout and design decisions

```
README.md, CHANGELOG.md, SECURITY.md, LICENSE
AGENTS.md, CLAUDE.md      instructions for coding agents (CLAUDE.md imports AGENTS.md)
CONTEXT.md, docs/         glossary, ADRs, plans, eval spec, commands, release runbook, status
product.toml              the product's id, display name, SEO description and repos (ADR-0012)
.env.example              template for .env (copy, fill in; .env is git-ignored)
sources.example.yaml      template for sources.yaml (your playlist list; git-ignored)
.github/workflows/ci.yml  tests, secret scan, assembled plugin, claude plugin validate
eval/                     the released retrieval benchmark (queries, qrels, moments; CHECKSUMS)
ops/                      launchd plist, log rotation, probe_models.py
ytbrain/                  the pipeline
  cli.py                  all commands
  sources.py              sources.yaml entries and the adapter per Source type (ADR-0013)
  web/                    the website adapter: urls (ids, scope), http (robots, pacing, retries),
                          render (Playwright), page (metadata, main text, page type), state, crawl
  locators.py             positions -> labels and links (mm:ss / ¶n, &t= / #:~:text=)
  config.py               every tunable, annotated with the reason for its value; loads .env
  manifest.py             SQLite per-Step checkpoints, parking, run history
  fetch.py                yt-dlp wrapper; info.json decides human vs auto captions
  captions.py             srt → dedup → utterances
  verify.py               fuzzy grounding; derives timestamps from the transcript
  pages.py                canonical JSON + deterministic markdown; atomic writes
  lock.py                 single-writer lock
  extract/                schema.py (Pydantic), prompts.py, runner.py (backends, retries, windows)
  knowledge/              items.py (Knowledge items), embed.py, store.py (LanceDB), search.py
  pack.py                 `pack build`: the Knowledge pack writer (embed cache, checks, manifest)
  index.py                transcript passages (used by knowledge/items.py)
  eval/                   the benchmark build, judges, runs, and `eval coach` (gates G2, G4, G5, G6)
  graph.py, mcp_server.py parked for later milestones (docs/archive/phase2-parked.md)
founder_coach/            the light runtime founders install (ADR-0010): server.py (MCP), store.py
                          (Founder store), search.py (shared ranking), pack.py, models.py (ONNX),
                          product.py (the product id at run time); never imports ytbrain
plugin/                   the Claude Code plugin template: manifest, .mcp.json, hooks, skills, evals
scripts/                  assemble_plugin.py (-> dist/plugin), release.py, check_secrets.py
tests/                    six offline suites; golden/ pins the MCP tool schemas and the generated schema
```

### What's in git

Commit everything `git status` lists; `.gitignore` keeps out the rest. The repository holds:

| Tracked | What |
|---|---|
| `README.md`, `CHANGELOG.md`, `SECURITY.md`, `LICENSE`, `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md` | project docs and agent instructions |
| `.gitignore`, `.github/workflows/ci.yml` | what stays out; CI |
| `.env.example`, `sources.example.yaml`, `product.toml`, `pyproject.toml` | templates and configuration |
| `docs/` (`adr/`, `research/`, `archive/`, plans, eval spec, commands, release, status) | design record |
| `ytbrain/` | the pipeline |
| `founder_coach/`, incl. the generated `product.json` and `playbooks/` | the runtime founders install (CI checks the generated files are current) |
| `plugin/` | the plugin template (skills, hooks, manifest, evals) |
| `scripts/`, `ops/` | assembler, release, secret scan; launchd and helpers |
| `tests/`, incl. `tests/golden/` | the six suites and their snapshots |
| `eval/` | the released retrieval benchmark (no transcript text: ids, titles, timestamps, grades) |

| Never in git | Why |
|---|---|
| `.env` | API keys |
| `sources.yaml` | your own Source list |
| `data/` (and a stray `manifest.db`) | pipeline output: downloads, transcripts, records, index, pack, eval runs; rebuildable |
| `dist/` | assembled plugins and release builds |
| `~/.founder-coach/` (never inside the repo) | a Founder's private store |
| `.venv/`, caches, `*.egg-info`, `.DS_Store`, `*.orig`/`*.rej` | local tooling |
| `docs/ytbrain-docs/` | copies of plan docs made for a Claude Project |

Decisions worth knowing:

- **Captions only, `srt` requested natively.** No audio, no ASR. YouTube's native srt
  avoids the rolling-caption duplication of VTT; a dedup pass stays as a safety net.
- **`info.json` decides human vs auto captions,** not the file name (yt-dlp writes both as
  `<id>.en.srt`). Human tracks are preferred; plain `en` wins over variants.
- **The model never emits timestamps.** It quotes; `verify` finds the quote and derives
  the time. That is what makes one call per video safe.
- **Checkpoints are per stage.** Changing the schema or model re-runs `extract` onward
  and nothing else; a new verifier re-runs only `verify`.
- **Hosted and local backends behind one seam** (`extract/runner.py`), with the model and
  prompt hash recorded on every record.
- **Only Verified knowledge is indexed** ([ADR-0004](docs/adr/0004-only-verified-knowledge-reaches-the-coach.md)). The coach can't cite a quote
  the verifier didn't find.
- **Stage ranks, it doesn't filter** ([ADR-0005](docs/adr/0005-stage-is-a-ranking-signal-not-a-filter.md)). An idea-stage founder still
  sees the best answer even if it was tagged `mvp`.
- **Retrieval, not generation** ([ADR-0002](docs/adr/0002-mcp-server-retrieves-host-agent-generates.md)). ytbrain returns cited items, and the
  host agent (Claude, etc.) writes the answer.

## Tests and CI

```bash
for s in core eval pack coach plugin web; do YTBRAIN_DOTENV=0 python tests/test_$s.py || break; done   # 206 tests, no network
pytest tests/                                                                        # the same under pytest
```

| Suite | Guards |
|---|---|
| `test_core.py` | caption dedup, human-over-auto captions, evidence grounding both ways, per-Step checkpoints and parking, schema-version invalidation, atomic writes, rate pacing, extraction retries and windows |
| `test_eval.py` | benchmark build and release sealing, judges, metrics and gates, run labels, the coach eval harness (resume, usage-limit stop, persona checks) |
| `test_pack.py` | the Knowledge pack format, checksums, incremental rebuilds, shared search |
| `test_coach.py` | the Founder store (history, idempotent writes, migrations, backups, forget/restore, Feedback, the usage log, Check-in warnings), Nudges, all 8 MCP tools through the SDK client, the tool-schema snapshot, stdio under both protocol versions, concurrent writers |
| `test_plugin.py` | manifest, skills and their invocation, the assembler (template filling, atomic swap, `--check`), the product id living only in `product.toml`, releases (tags, version checks, renames) |
| `test_web.py` | website Sources against the acceptance criteria in docs/web-sources-plan.md: config, ids, scope and depth, robots.txt, page types, rendering, retries and host pausing, conditional re-checks, aliases, resume, articles through clean → extract → verify → items → pages, the CLI over a localhost server |

The tests use fakes (a hashing embedder, stubbed yt-dlp and LLM calls), so they need no keys,
models or YouTube access, and they are hermetic: every suite sets `YTBRAIN_DOTENV=0` (your `.env`
is never read) and never reads your `sources.yaml`, so a clean checkout passes exactly like your
machine. CI (`.github/workflows/ci.yml`) runs on every push and pull request: a secret scan,
then the suites on Python 3.11 and 3.13 (`serve,pack,dev,web` plus LanceDB), failing on any skipped
test; then it assembles a plugin from a test pack, fails if the generated files are stale, and
runs `claude plugin validate --strict`.
Answer quality is measured separately, on the real host (the local Claude Code on your plan,
Haiku by default): `ytbrain eval coach` (gates G2, G4, G5, G6) and `ytbrain claude -- plugin eval …` (mocked skill
behaviour); see [docs/commands.md](docs/commands.md).

## Roadmap

The v1 scope (a private beta) and its quality gates are in [docs/phase3-plan.md §0](docs/phase3-plan.md);
milestones in [docs/phase2-plan.md](docs/phase2-plan.md).

| Milestone | What | State |
|---|---|---|
| M0 | Extraction hardening (schema 2.2.0), Series fix, `refresh` | **done** |
| M1 | Knowledge index + `ytbrain search` | **done** |
| M2 | Evaluation: a Tuning set (150 generated questions, labels v1.3.0 with article Moments) and a Holdout set | Tuning set **done**; Holdout (M2c lite, G3) next |
| M2d | The Knowledge pack (ONNX, no torch) | **done**; pack experiments toward G1 pending |
| M3a/b | The coach runtime (8 MCP tools, local Founder store) and the Claude Code plugin (9 skills, hook) | **done**; one live session left |
| M3d | Coach evaluation on the real host (G2, G4, G5, G6) | on Haiku: G2, G4, G6 pass; G5 re-run after the Check-in fixes |
| — | Repos, CI, Feedback, beta releases | **done** (2026-09-26) |
| — | Website Sources: generic crawler for any listed site (robots.txt, pacing, Playwright when needed), articles through the same Steps | **done** (2026-09-26); first crawls (paulgraham.com, pmarchive.com, YC Library) done, fixes in CHANGELOG |
| — | Local usage log | **done** (2026-09-26) |
| — | Coach answers first, search budget, Check-in warnings, cheaper `eval coach` (Haiku, turn cap) | **done** (2026-09-28) |
| — | Eval support for articles (article Moments), review fixes (index/pack upgrades, hermetic tests) | **done** (2026-09-26) |
| next | M2c lite (Holdout, G3); Jev (typed judgements) plan | planned |
| M4 | Dogfood with real founders; every miss becomes an eval case | after the first beta release |
| M5, M6 | Principles and a thin graph; other hosts (Cursor, Codex, Gemini CLI…) | later |

## Contributing

The repository is private during the beta. Changes follow [AGENTS.md](AGENTS.md):

1. Run the six suites before and after a change, and add a test for any behaviour change,
   especially anything touching resume, retries, verification or the Founder store.
2. Keep the invariants: bump `SCHEMA_VERSION` in `config.py` for any change to
   `extract/schema.py`; a Founder-store table change is a new numbered migration; never spell
   the product id out in `plugin/` or `founder_coach/` (use `{{id}}` / `founder_coach.product`).
3. After an intended MCP tool change, review and regenerate the snapshot:
   `UPDATE_GOLDEN=1 python tests/test_coach.py`.
4. Record a hard-to-reverse choice as an ADR in `docs/adr/`, a new domain term in `CONTEXT.md`,
   and user-visible changes in `CHANGELOG.md`.
5. `python scripts/check_secrets.py` must pass (CI runs it; `docs/release.md` shows the
   pre-commit hook).

## Security and privacy

- **Secrets** live only in `.env` (git-ignored); CI and the pre-commit hook scan every tracked
  file for API keys. See [SECURITY.md](SECURITY.md).
- **Founder data never leaves the Founder's machine**: the store, FOUNDER.md, backups and
  Feedback live in `~/.founder-coach/`. Only public corpus text is ever sent to an LLM judge;
  Feedback and the usage log leave only when the Founder exports and sends the file.
- **Only Verified knowledge is cited** (ADR-0004), and quoted talk text reaches the host model
  wrapped as untrusted reference material, never as instructions.

## License

This repository (ytbrain and Founder Coach) is **source-available** under the [Business Source License 1.1](LICENSE) (`BUSL-1.1`),
which converts to the **Apache License 2.0 on 2030-01-01**.

- **Free, including production use:** personal use, education, research, open-source
  projects, and a founder or company using ytbrain to coach itself and its own team.
- **Needs a commercial license:** offering ytbrain (or something built from it) to others for
  a fee or commercial advantage — as a hosted service, app, API or agent, selling a knowledge
  base/index/coaching service built with it, or embedding it in a product you sell.
  Ask via the repository.

BUSL is not an OSI-approved open-source license until the change date; after it, every
version released before then is available under Apache-2.0.

## Disclaimer

- **Not professional advice.** ytbrain summarises public talks. Its output is educational
  and may be wrong, outdated or taken out of context; it is not legal, financial,
  investment or tax advice. Always check the cited source.
- **Your content, your responsibility.** You are responsible for having the right to
  download, process and use any content you ingest, and for complying with the YouTube
  Terms of Service, copyright law and the terms of any LLM provider you use. Don't publish
  or redistribute raw captions or transcripts.
- **No warranty.** The software is provided "as is" (see `LICENSE`).

## Changelog

User-visible changes are in [CHANGELOG.md](CHANGELOG.md), newest first.
