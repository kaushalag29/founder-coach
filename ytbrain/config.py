"""Paths and tunables. Each constant is a decision; the comment says which."""
from __future__ import annotations
import os
import sys
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Read KEY=value lines from the repo's .env into os.environ.

    Stdlib only, and never overrides a variable already set in the shell, so
    `YTBRAIN_LLM_MODEL=x ytbrain extract` still wins for a one-off run. Accepts
    the `export KEY=value` form so the same file also works with `source .env`.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.removeprefix("export ").split("=", 1)
        val = val.strip()
        if val[:1] in "\"'" and val[:1] and val.endswith(val[0]):
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()     # inline comment
        key = key.strip()
        cur = os.environ.get(key)
        if cur is None:
            os.environ[key] = val
        elif cur != val:
            # The shell wins (so one-off overrides work), but a value left over
            # from an earlier `source .env` silently beating the edited file is
            # confusing -- say so. Secrets are never printed.
            shown = "" if "KEY" in key or "TOKEN" in key or "SECRET" in key else f" = {cur!r}"
            print(f"note: {key} from your shell{shown} overrides .env "
                  f"(run `unset {key}` or open a new terminal to use .env)", file=sys.stderr)


# YTBRAIN_DOTENV=0 skips it: the tests set that, so they behave the same on a machine with a
# configured .env (real backend, real keys) as on a clean CI runner, and never spend money
DOTENV = Path(__file__).resolve().parents[1] / ".env"   # also handed to founder-coach (`ytbrain claude`)
if os.environ.get("YTBRAIN_DOTENV") != "0":
    _load_dotenv(DOTENV)

ROOT = Path(os.environ.get("YTBRAIN_ROOT", Path(__file__).resolve().parents[1]))
DATA = ROOT / "data"
RAW = DATA / "raw"                   # yt-dlp: <id>.info.json, <id>.en.srt
TRANSCRIPTS = DATA / "transcripts"   # cleaned, utterance-merged
METADATA = DATA / "metadata"         # canonical JSON record per video
PAGES = DATA / "pages"               # markdown projection (human/agent readable)
REPORTS = DATA / "reports"           # acceptance samples + run status
EXTRACT_FAILURES = REPORTS / "extract-failures"   # call log of each video extract gave up on
MANIFEST_DB = DATA / "manifest.db"
ARCHIVE = DATA / "archive.txt"       # yt-dlp --download-archive
LOCKFILE = DATA / "ytbrain.lock"     # single writer: SQLite + scheduled runs
RUN_LOG = DATA / "run.log"

WEB_RAW = RAW / "web"                # website pages: <host>/<doc_id>.html + .meta.json

# --- websites (docs/web-sources-plan.md D3-D9; industry-standard polite defaults) ---
WEB_CONTACT = os.environ.get("YTBRAIN_WEB_CONTACT", "https://github.com/kaushalag29/founder-coach")
WEB_USER_AGENT = os.environ.get("YTBRAIN_WEB_USER_AGENT",
                                f"ytbrain/0.1 (+{WEB_CONTACT}; polite research crawler)")
WEB_ROBOTS_AGENT = "ytbrain"         # the token robots.txt groups are matched against
WEB_MIN_INTERVAL_S = 1.0             # one request per second per host, or the site's Crawl-delay if slower
WEB_MAX_CRAWL_DELAY_S = 60.0         # a Crawl-delay above this is capped (and logged)
WEB_TIMEOUT_S = 30.0                 # one HTTP request
WEB_RENDER_TIMEOUT_S = 60.0          # one page in the browser
WEB_RETRIES = 3                      # per request, on 429 / 5xx / network errors
WEB_BACKOFF_BASE_S = 2.0             # 2, 4, 8s (+ jitter), or Retry-After when the server says
WEB_RETRY_AFTER_MAX_S = 120.0        # a longer Retry-After gives up on the page for this run
WEB_MAX_BYTES = 15 * 2**20           # a bigger page is skipped (Googlebot reads the first 15 MB of HTML; JS frameworks inline big data)
WEB_HOST_FAILURE_CAP = 5             # pages failing in a row before the host is paused for the run
WEB_URL_FAILURE_CAP = 3              # runs in a row a page may fail before it's parked
WEB_DEFAULT_DEPTH = 3                # link hops from the start page
WEB_DEFAULT_MAX_PAGES = 10_000       # pages fetched per Source per run
WEB_RECHECK_DAYS = 7                 # a known page is re-checked (conditional GET) after this
WEB_MIN_ARTICLE_WORDS = 150          # main text below this isn't an Article (and triggers rendering)
WEB_MAX_SITEMAPS = 50                # sitemap files read per Source (index files included)
WEB_LANGUAGES = ("en",)              # D9: other languages are skipped, recorded as such

# --- caption cleanup ---
MIN_TRANSCRIPT_WORDS = 60            # below this (music-only intros, 2-cue captions) there is
                                     # nothing to ground advice in: skipped, not extracted
UTTERANCE_MAX_MS = 25_000            # merge cues into ~10-25s utterances
UTTERANCE_GAP_MS = 1_200             # a pause this long forces a break

# --- extraction (decision Q9: one call per video) ---
LLM_BACKEND = os.environ.get("YTBRAIN_LLM_BACKEND", "ollama")
LLM_MODEL = os.environ.get("YTBRAIN_LLM_MODEL", "qwen3:30b-a3b")
LLM_BASE_URL = os.environ.get("YTBRAIN_LLM_BASE_URL", "http://localhost:11434")
LLM_NUM_CTX = int(os.environ.get("YTBRAIN_LLM_NUM_CTX", "32768"))  # Ollama silently truncates
                                     # prompts past its default window (2-4k); a 20k-token transcript needs this
LLM_MAX_TOKENS = int(os.environ.get("YTBRAIN_LLM_MAX_TOKENS", "8192"))  # output cap, OpenAI-style backends
LLM_TEMPERATURE = 0.1                # low: prompt changes should be the only variable
LLM_SEED = 7                         # pinned where the backend honours it
EXTRACT_MAX_REPAIRS = 2              # then flag for review rather than loop
# Above this transcript size, extract in windows. Local 32k-context models need
# ~20k; hosted long-context models (DeepSeek, Gemini) can take 60k+ in one call,
# which keeps a long talk's summary and advice coherent.
SINGLE_CALL_MAX_TOKENS = int(os.environ.get("YTBRAIN_SINGLE_CALL_MAX_TOKENS", "20000"))
WINDOW_TOKENS = 8_000                # fallback windows: few large windows, not one call per chapter
MAX_HIGHLIGHTS = 10                  # per call; also the cap after merging windows (x1.5)
MAX_ADVICE = 12
# Short clips got the same 10 + 12 budget as hour-long talks: a 150-word recruiting
# video yielded ~8 near-duplicate items. The budget now scales with length (one item
# per this many words, at least 2) up to the caps above.
WORDS_PER_HIGHLIGHT = 150
WORDS_PER_ADVICE = 120
# Self-check after extraction (local, no LLM): retry once with feedback when the
# quotes mostly aren't in the transcript, or a substantial talk yields no advice.
GROUNDING_RETRY_BELOW = 0.60
NO_ADVICE_RETRY_MIN_WORDS = 1_000   # "YC Founders Made These Fundraising Mistakes" (1.4k words) got none
TOKENS_PER_WORD = 1.33               # estimator; avoids a tokenizer dependency

# --- retries / backoff ---
LLM_MAX_RETRIES = int(os.environ.get("YTBRAIN_LLM_MAX_RETRIES", "5"))   # 429/5xx/connection drops/bad payloads
LLM_TIMEOUT_S = float(os.environ.get("YTBRAIN_LLM_TIMEOUT_S", "900"))   # one request, end to end
LLM_TIMEOUT_RETRIES = 1              # a timed-out request is retried once: each try can take LLM_TIMEOUT_S
EXTRACT_VIDEO_RETRIES = int(os.environ.get("YTBRAIN_EXTRACT_VIDEO_RETRIES", "2"))  # whole-video retries
STEP_FAILURE_CAP = 3               # a talk failing a Step this many runs in a row is parked
                                     # ('parked'): skipped until `ytbrain <step> --retry-failed`
EXTRACT_VIDEO_BACKOFF_S = 30         # 30, 60s (+ jitter) between whole-video attempts
LLM_BACKOFF_BASE_S = 10              # 10, 20, 40, 80, 160s (+ jitter), capped below
LLM_BACKOFF_MAX_S = 300
# Requests-per-minute ceiling shared by ALL extract workers (calls are spaced
# 60/RPM seconds apart). OpenRouter ":free" models allow 20 RPM per account;
# paid models have no published per-key RPM, so 30 is a polite default.
LLM_MAX_RPM = float(os.environ.get("YTBRAIN_LLM_MAX_RPM") or
                    (18 if os.environ.get("YTBRAIN_LLM_MODEL", "").endswith(":free") else 30))
LLM_MIN_INTERVAL_S = 60.0 / LLM_MAX_RPM
# The ceiling above adapts on hosted APIs: a 429 halves the working rate (at most once per
# RPM_CUT_COOLDOWN_S, so one burst of 429s counts once, never below RPM_FLOOR), and every
# RPM_RAISE_EVERY_S without a 429 raises it by RPM_RAISE_STEP of the ceiling, back up to it.
RPM_FLOOR = 6.0
RPM_CUT_COOLDOWN_S = 20.0
RPM_RAISE_EVERY_S = 60.0
RPM_RAISE_STEP = 0.10
LLM_WORKERS = int(os.environ.get("YTBRAIN_LLM_WORKERS", "1"))   # default for `extract --workers`
YT_COOLDOWN_BASE_S = 30              # YouTube 429s: 30, 60, 120, 240s pause before the next video
YT_COOLDOWN_MAX_S = 600
YT_MAX_429_STREAK = 5                # then stop the sync: the IP is throttled, more requests only extend it
YT_VIDEO_RETRIES = int(os.environ.get("YTBRAIN_YT_VIDEO_RETRIES", "2"))  # same video, after the cooldown
YT_ERROR_BACKOFF_S = 10              # other transient yt-dlp errors: 10, 20s before retrying the video
YT_ENUM_RETRIES = 2                  # playlist listing: retried after 30, 60s
# Pause before EACH subtitle file (a video usually has two: en + en-orig), on top of
# sleep_requests between every request. yt-dlp issue #13831 reports 60s ending
# auto-caption 429s, but that was without a JS runtime or PO tokens; with both,
# shorter pauses are enough. `sync --backfill` retries rate-limited videos a bit
# more cautiously, at most YT_BACKFILL_PER_HOUR. Raise these if 429s return.
YT_SLEEP_SUBTITLES = float(os.environ.get("YTBRAIN_YT_SLEEP_SUBTITLES", "10"))
YT_BACKFILL_SLEEP_SUBTITLES = 15
YT_BACKFILL_PER_HOUR = 60            # YouTube allows guests ~300 videos/hour; stay well below
# Optional: passed to yt-dlp --proxy (http://user:pass@host:port or socks5://...).
# ytbrain adds no rotation of its own; never printed, since it may hold credentials.
YTDLP_PROXY = os.environ.get("YTBRAIN_YTDLP_PROXY", "").strip()
# Optional: where a bgutil PO-token provider server listens (default 127.0.0.1:4416).
POT_PROVIDER_URL = os.environ.get("YTBRAIN_POT_PROVIDER_URL", "http://127.0.0.1:4416").strip()
# ... or no server at all: the plugin's script mode runs the provider per token with
# Node/Deno from a checkout (default ~/bgutil-ytdlp-pot-provider/server).
POT_SCRIPT_HOME = os.path.expanduser(os.environ.get(
    "YTBRAIN_POT_SCRIPT_HOME", "~/bgutil-ytdlp-pot-provider/server")).strip()

# --- grounding verification ---
EVIDENCE_MIN_JACCARD = 0.70          # fuzzy, NOT exact: ASR text has no punctuation
# Second test for quotes the model tidied (dropped repeated words, "like", "you know"):
# Jaccard over a quote-sized window punishes the words it left out. A quote passes
# when >= 90 % of its words appear within a window up to 1.5x its length. Invented
# words still fail it; 0 of 480 cross-talk quotes passed in testing, 20 of 80
# genuinely tidied quotes did. Only for quotes long enough to be distinctive.
EVIDENCE_MIN_CONTAINMENT = 0.90
EVIDENCE_CONTAINMENT_MIN_WORDS = 8
EVIDENCE_WINDOW_SCALES = (1.15, 1.3, 1.5)
# A record is flagged for another look (and picked up by `invalidate extract
# --only-flagged`) when a real talk yields nothing: no items at all from this many
# words, or no advice from a practical talk this long.
EMPTY_RECORD_MIN_WORDS = 1_000   # "Closing Remarks" (560 words) may rightly be empty
NO_ADVICE_FLAG_MIN_WORDS = 1_000
NARRATIVE_CATEGORIES = ("ai-and-tech-trends", "founder-story", "other")

# --- acceptance thresholds (decision Q10) ---
ACCEPT_MIN_RECORD_RATE = 0.90        # captioned videos that produced a record
ACCEPT_MIN_SPAN_PASS_RATE = 0.90     # evidence spans that verify
ACCEPT_SAMPLE_SEED = 42              # fixed so the human read is reproducible

# --- phase 2 (parked; see PHASE2.md) ---
CHUNK_TARGET_TOKENS = 550
CHUNK_OVERLAP_RATIO = 0.18
PARENT_TARGET_TOKENS = 2000
GRAPH_DB = DATA / "graph.duckdb"
LANCE_DIR = DATA / "lancedb"

# --- eval benchmark (docs/eval-spec.md) ---
EVAL_DIR = ROOT / "eval"                      # released benchmark files (committed)
EVAL_DATA = DATA / "eval"                     # working state: eval.db, raw downloads, run results
# Knowledge pack [ADR-0009]: what the coach plugin ships (built by `ytbrain pack build`)
from founder_coach.models import DEFAULT_EMBED_MODEL as _PACK_EMBED  # noqa: E402
from founder_coach.models import DEFAULT_RERANK_MODEL as _PACK_RERANK  # noqa: E402
PACK_DIR = DATA / "pack"                      # knowledge.sqlite + pack.json + embed-cache.db
PACK_EMBED_MODEL = os.environ.get("YTBRAIN_PACK_EMBED_MODEL", _PACK_EMBED)
# No reranker by default: measured on labels v1.2.0, jina-reranker-v1-turbo lowered the pack's
# nDCG@10 by 0.022 (0.457 vs 0.479) and made every search slower. `--rerank-model` still sets one.
PACK_RERANK_MODEL = os.environ.get("YTBRAIN_PACK_RERANK_MODEL", "none")
MOMENT_S = 120                                # a Moment is a two-minute window...
MOMENT_STEP_S = 60                            # ...starting on every whole minute (TREC Podcasts)
ARTICLE_MOMENT_PARAS = 6                      # an article's Moment: six paragraphs (~a two-minute read)...
ARTICLE_MOMENT_STEP = 3                       # ...starting every third paragraph (the same 50% overlap)
EVAL_WORKERS = int(os.environ.get("YTBRAIN_EVAL_WORKERS", "8"))
EVAL_MAX_RPM = float(os.environ.get("YTBRAIN_EVAL_MAX_RPM", "60"))
# One eval call (a question, a rewrite, a 5-Moment grade) answers in seconds; a provider that
# hangs shouldn't hold the build for LLM_TIMEOUT_S (900 s, sized for long extractions).
EVAL_TIMEOUT_S = float(os.environ.get("YTBRAIN_EVAL_TIMEOUT_S", "120"))
EVAL_MAX_COST = float(os.environ.get("YTBRAIN_EVAL_MAX_COST", "5"))     # USD per build, hard stop
EVAL_GENERATOR = os.environ.get("YTBRAIN_EVAL_GENERATOR") or os.environ.get(
    "YTBRAIN_LLM_MODEL", "deepseek/deepseek-v4-flash")
# two judges from other model families + a tie-breaker (comma-separated, in that order).
# A model that isn't served is replaced by the cheapest served one of its family (preflight).
EVAL_JUDGES = [m.strip() for m in os.environ.get(
    "YTBRAIN_EVAL_JUDGES",
    "google/gemma-4-31b-it,qwen/qwen3.8-flash,mistralai/mistral-medium-3.1").split(",")
    if m.strip()]
EVAL_JUDGE_BATCH = 5                          # Moments graded per judge call, each scored on its own
EVAL_TUNING_SIZE = 150
EVAL_TUNING_OVERSAMPLE = 1.4                  # candidates generated per kept question (filters, self-check)
EVAL_HOLDOUT_MAX = 50
EVAL_POOL_DEPTH = {"dev": 20, "test": 50}

# --- knowledge index (phase 2, M1) [ADR-0001, ADR-0003] ---
KNOWLEDGE_TABLE = "knowledge"
ITEMS_VERSION = "1"                  # bump when the Knowledge-item shape changes -> reindex
# Local embeddings: free, private, reproducible. Changing the model reindexes.
EMBED_MODEL = os.environ.get("YTBRAIN_EMBED_MODEL", "BAAI/bge-m3")
EMBED_MAX_SEQ = 1024                 # tokens per text; passages are ~550
# PyTorch device for embedding/reranking: auto (mps > cuda > cpu), mps (Apple
# silicon GPU), cpu or cuda. `--device` on index/search/run overrides it.
TORCH_DEVICE = os.environ.get("YTBRAIN_DEVICE", "auto").lower()
# Cross-encoder reranker over fused candidates; "none" disables.
RERANK_MODEL = os.environ.get("YTBRAIN_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
# Fusion, diversity and soft boosts [ADR-0005] are defined once, in the search module the
# coach runtime shares (ADR-0010); re-exported here for existing imports.
from founder_coach.search import (BOOST_STAGE_DOCUMENT, BOOST_STAGE_ITEM, KIND_PRIOR,  # noqa: E402,F401
                                  MAX_PER_DOCUMENT, RECENCY_WEIGHT, RRF_K, SEARCH_CANDIDATES)

SCHEMA_VERSION = "2.2.0"             # bump on ANY change to what the model generates -> auto-invalidates
                                     # extract. Given fields (url, series, source_kind...) may be added
                                     # with defaults that match existing records (ADR-0013); a test pins
                                     # the generated schema to this version.
                                     # 2.1.0: categories ai-and-tech-trends, founder-story
                                     # 2.2.0: extraction diagnostics, item caps, windowed fallback

for _p in (RAW, WEB_RAW, TRANSCRIPTS, METADATA, PAGES, REPORTS, EXTRACT_FAILURES):
    _p.mkdir(parents=True, exist_ok=True)
