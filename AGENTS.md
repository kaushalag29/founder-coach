# AGENTS.md

ytbrain turns public startup-advice content (today: YouTube talks and website articles) into verified, citable
knowledge, and is growing into a founder-coach served over MCP.

## Before you change anything

- **Vocabulary:** read `CONTEXT.md` when naming anything or writing docs; use its terms
  (Document, Advice, Evidence, Verified, Stage, Step …) and add a term there when you
  introduce a new domain concept.
- **Decisions:** read `docs/adr/` before changing architecture, storage, the MCP surface,
  or what reaches the coach; record a new ADR when a choice is hard to reverse.
- **Roadmap:** `docs/phase2-plan.md` (milestones, compatibility guarantees, backlog) and
  `docs/phase3-plan.md` §0 (v1 scope and quality gates); the current state and next steps are in
  `docs/m3-status.md`. Work is scoped to the current milestone.

## Commands

- Setup: `uv venv --python 3.12 && source .venv/bin/activate && uv pip install -e ".[serve,pack,dev,web]" "lancedb>=0.39.0"` (what CI installs; with less, suites skip tests). Everything: `".[extract,index,graph,serve,asr,eval,pack,dev,pot,web]"`
- Before a commit: `sh scripts/check.sh` runs what CI runs (secret scan, the six suites with skips failing, generated files fresh, `claude plugin validate`), offline and free; the maintainer's pre-commit hook runs it (`docs/release.md`).
- Tests: `for s in core eval pack coach plugin web; do YTBRAIN_DOTENV=0 python tests/test_$s.py || break; done` — offline and fast; run before and after every change. `tests/golden/coach_tools.json` pins the MCP tool schemas: after an intended change, review the diff and regenerate with `UPDATE_GOLDEN=1 python tests/test_coach.py`.
- CLI reference: `ytbrain --help` and `README.md`.
- Plugin: edit `plugin/skills/*/SKILL.md`, never `founder_coach/playbooks/*.md` or `founder_coach/product.json` (generated); `python scripts/assemble_plugin.py --pack data/pack --check` builds `dist/plugin`. `plugin/` is a template: load `dist/plugin`, not `plugin/`.
- Repos, CI and releases: `docs/release.md` (`.github/workflows/ci.yml`, `scripts/release.py`, `scripts/check_secrets.py`).

## Invariants

- **Runtime boundary:** `founder_coach/` is what founders install; it never imports `ytbrain`, torch, LanceDB or yt-dlp (ADR-0010). Ranking changes go in `founder_coach/search.py` so the index and the pack stay identical.
- **Git:** the maintainer runs every git command (including `scripts/release.py`); agents leave the repository state alone.
- **Product id:** the product's name lives only in `product.toml` (ADR-0012). Shipped files say `{{id}}` (plugin/) or use `founder_coach.product` (runtime: `product.ID`, `product.env("HOME")`); a test fails on a spelled-out id.
- **Founder store schema:** a change to its tables is a new numbered migration in `founder_coach/store.py` (bump `SCHEMA_VERSION`); migrations are forward-only and take a backup first (ADR-0010, ADR-0011).
- **Secrets:** `.env` holds API keys — leave it unread; configuration you need is documented
  in `.env.example` and `README.md`. Pass dummy values when a test needs a key.
- **Checkpoints:** a Step marks a Document done only after its output file is complete, so an
  interrupted run redoes exactly the unfinished Document. Keep this ordering in every new Step.
- **Atomic writes:** write files with `pages.atomic_write_text` (temp file + rename), never in place.
- **Damaged input goes upstream:** a missing or corrupt input sends the Document back to the Step
  that produces it (mark that Step `stale`), rather than skipping it.
- **Schema:** any change to what the model generates (`Generated`, `Overview`, `ChapterList` in
  `ytbrain/extract/schema.py`) bumps `SCHEMA_VERSION` in `config.py`, which re-extracts older
  records; `tests/golden/generated_schema.json` pins it. Given fields (url, series, source_kind…)
  may be added with defaults that match existing records, without a bump (ADR-0013).
- **Source types:** a new one is an adapter (`sync` + `clean` to the shared text-units transcript)
  registered in `ytbrain/sources.py`; nothing after `clean` may depend on the Source type (ADR-0013).
- **Crawling:** obey robots.txt, pace per host, never log in, never rotate IPs or spoof a browser
  fingerprint, never solve CAPTCHAs (docs/web-sources-plan.md D6).
- **Persisted names:** `data/manifest.db` keeps its `stage_state` table and `stage` column
  (ADR-0007); new code calls pipeline units Steps.
- **Verified only:** only Verified Advice/Takeaways are indexed, returned by tools or shown on
  pages (ADR-0004).
- **Given vs generated fields:** Series, provenance, title and dates come from Sources, yt-dlp
  or the page's own metadata, never from the model; change them with `ytbrain refresh`, not re-extraction.
- **Tests stay offline and hermetic:** stub yt-dlp, LLM calls and HTTP (httpx.MockTransport, a localhost server, a fake browser); tests use `YTBRAIN_ROOT` set to a temp dir, set `YTBRAIN_DOTENV=0` before importing `ytbrain` (so a developer's `.env` never changes their behaviour or spends money; it also stops `founder_coach/settings.py` reading `.env` files) and never read the repo's `sources.yaml` (patch `cli.SOURCES` to a temp file). A clean checkout must pass exactly like a configured machine (CI runs one).

## Data

- `data/` is rebuildable pipeline output (git-ignored): safe to regenerate, never commit.
- The Founder store (`~/.founder-coach/`, i.e. `~/.<product id>`) is private and not rebuildable: back it up before any
  migration and never send its contents to a Judge or any service other than the host model.
