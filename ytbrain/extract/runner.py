"""Single-call-per-video extraction with an adaptive fallback.

Shape (decision Q9):
  1. chapters = uploader's, from info.json, when present  -> zero LLM calls
     otherwise one chapterization call
  2. one extraction call over the whole transcript, with the chapter list as
     context, unless the transcript is very long or validation fails twice - in
     which case fall back to per-chapter calls

Backend-agnostic: the contract is Pydantic, so swapping the local Ollama call
for a batch API is a config flag. Every record carries `model` and
`prompt_hash` so a backend or prompt change can be detected and re-run
selectively - and so quality numbers stay interpretable, which they are not if
half a corpus came from one model and half from another.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field as dc_field
from typing import Callable

from pydantic import BaseModel, ValidationError

from ..config import (EXTRACT_MAX_REPAIRS, GROUNDING_RETRY_BELOW, LLM_BACKEND, LLM_BASE_URL,
                      LLM_MODEL, MAX_ADVICE, MAX_HIGHLIGHTS, NO_ADVICE_RETRY_MIN_WORDS,
                      WINDOW_TOKENS,
                      LLM_BACKOFF_BASE_S, LLM_BACKOFF_MAX_S, LLM_MAX_RETRIES,
                      LLM_MAX_TOKENS, LLM_MIN_INTERVAL_S, LLM_NUM_CTX,
                      LLM_TIMEOUT_RETRIES, LLM_TIMEOUT_S,
                      RPM_CUT_COOLDOWN_S, RPM_FLOOR, RPM_RAISE_EVERY_S, RPM_RAISE_STEP,
                      LLM_SEED, LLM_TEMPERATURE, SCHEMA_VERSION,
                      SINGLE_CALL_MAX_TOKENS, TOKENS_PER_WORD,
                      WORDS_PER_ADVICE, WORDS_PER_HIGHLIGHT)
from .. import source_kinds
from . import prompts
from .schema import (AdviceAtom, Chapter, ChapterList, ExtractionMeta, Generated, Highlight,
                     Overview, VideoMetadata)


# Progress callback: each LLM call can take minutes on a laptop, so callers get
# a short label before each one ("chaptering", "repair 1", "ch3/8"). A silent
# multi-minute call is indistinguishable from a hang.
Step = Callable[[str], None] | None


def _step(on_step: Step, label: str) -> None:
    if on_step:
        on_step(label)


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

def ollama_json(prompt: str, schema: dict, model: str = LLM_MODEL,
                base_url: str = LLM_BASE_URL, timeout: float = LLM_TIMEOUT_S) -> "Completion":
    """Local structured generation via Ollama's JSON-Schema-constrained `format`."""
    return _post_with_backoff(
        f"{base_url}/api/generate",
        {"model": model, "prompt": prompt, "format": schema, "stream": False,
         "options": {"temperature": LLM_TEMPERATURE, "seed": LLM_SEED, "num_ctx": LLM_NUM_CTX}},
        timeout, {}, parse=_ollama_content)


def openai_compatible_json(prompt: str, schema: dict, model: str = LLM_MODEL,
                           base_url: str | None = None, timeout: float = LLM_TIMEOUT_S,
                           overrides: dict | None = None) -> "Completion":
    """Any OpenAI-compatible endpoint (LM Studio, vLLM, llama.cpp, hosted APIs).

    How JSON is requested is set by YTBRAIN_LLM_JSON_MODE:
      response_format  (default) OpenAI `response_format: json_schema` -- LM Studio,
                       vLLM, OpenAI, and NVIDIA's current hosted models
      json_object      `response_format: json_object` -- any valid JSON; the schema
                       is still in the prompt and enforced by the repair loop
      nvext            legacy NIM `nvext.guided_json`; NVIDIA's hosted endpoints now
                       reject it (HTTP 400 "unknown field guided_json"), so opt-in only
    max_tokens is set explicitly: some hosted endpoints default to ~1k output
    tokens, which truncates a full record mid-JSON and burns the repair budget.
    Transient 429/5xx responses are retried with backoff instead of failing the video.
    """
    import os

    base = base_url or os.environ.get("YTBRAIN_LLM_BASE_URL", "http://localhost:8000/v1")
    key = os.environ.get("YTBRAIN_LLM_API_KEY", "not-needed")
    mode = os.environ.get("YTBRAIN_LLM_JSON_MODE") or "response_format"
    body = {"model": model, "temperature": LLM_TEMPERATURE, "max_tokens": LLM_MAX_TOKENS,
            "seed": LLM_SEED, "stream": False,
            "messages": [{"role": "user", "content": prompt}]}
    # Per-model knobs without code changes, e.g. switching a hybrid model's
    # thinking off: YTBRAIN_LLM_EXTRA_BODY='{"chat_template_kwargs":{"thinking":false}}'
    # (a thinking model under a JSON constraint can return content=null).
    extra = os.environ.get("YTBRAIN_LLM_EXTRA_BODY")
    if extra:
        body.update(json.loads(extra))
    # Per-model adjustments (eval judges): YTBRAIN_LLM_EXTRA_BODY is tuned for the extract
    # model; e.g. `reasoning` must be dropped for a model that doesn't accept it, or
    # OpenRouter's require_parameters routing finds no endpoint at all (HTTP 404).
    for k, v in (overrides or {}).items():
        if v is None:
            body.pop(k, None)
        else:
            body[k] = v
    if "openrouter.ai" in base:
        body.setdefault("usage", {"include": True})             # report cost per call
    avoid = getattr(_tls, "avoid_providers", None)
    if avoid and "openrouter.ai" in base:
        # OpenRouter serves one model from many providers; one that just looped
        # to the output cap is skipped for this video's retry.
        prov = dict(body.get("provider") or {})
        prov["ignore"] = sorted(set(prov.get("ignore") or []) | set(avoid))
        body["provider"] = prov
    if mode == "nvext":
        body["nvext"] = {"guided_json": schema}
    elif mode == "json_object":
        body["response_format"] = {"type": "json_object"}
    else:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "record", "schema": schema,
                                                   "strict": False}}
    return _post_with_backoff(f"{base}/chat/completions", body, timeout,
                              {"Authorization": f"Bearer {key}", "Accept": "application/json"})


# --- pacing shared by every worker thread ---------------------------------
# One schedule for the whole process: each call reserves the next free slot
# (LLM_MIN_INTERVAL_S apart), so N workers together still stay under
# LLM_MAX_RPM. A 429 on any worker pauses ALL of them (_pause_until) -- the
# limit is per account, so the other workers would only hit it too.
_pace_lock = threading.Lock()
_min_interval = [LLM_MIN_INTERVAL_S]   # set_max_rpm() changes it (eval runs faster than extract)
_next_slot = [0.0]
_pause_until = [0.0]
_rpm_ceiling = [60.0 / LLM_MIN_INTERVAL_S]   # what the user allowed; the working rate adapts below it
_rpm_changed = [0.0]                          # monotonic time of the last cut or raise
_rpm_cut = [0.0]         # when the rate was last CUT: a 429 burst counts once (raises don't count)
_tls = threading.local()          # per-thread progress callback, see _say()


def _say(msg: str) -> None:
    """Progress text from deep inside a call. Routed to the calling video's
    on_step when extract_video set one (so parallel workers don't interleave
    half-lines on the terminal), else printed inline."""
    cb = getattr(_tls, "on_step", None)
    if cb:
        cb(msg)
    else:
        print(f"{msg} · ", end="", flush=True)


def is_local_endpoint(url: str) -> bool:
    """LM Studio / Ollama / llama.cpp on this machine: no account-level rate
    limit exists, so no pacing -- throttling there would only waste GPU time."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return (host in ("localhost", "0.0.0.0", "::1", "host.docker.internal")
            or host.startswith("127.") or host.endswith(".local"))


def _acquire_slot(url: str) -> None:
    if is_local_endpoint(url):
        return
    with _pace_lock:
        now = time.monotonic()
        t = max(now, _next_slot[0], _pause_until[0])
        _next_slot[0] = t + _min_interval[0]
    if t > now:
        time.sleep(t - now)


def set_max_rpm(rpm: float) -> None:
    """Change the shared requests-per-minute ceiling for this process (and start at it)."""
    with _pace_lock:
        _rpm_ceiling[0] = max(1.0, float(rpm))
        _min_interval[0] = 60.0 / _rpm_ceiling[0]
        _rpm_changed[0] = _rpm_cut[0] = 0.0


def current_rpm() -> float:
    """The working requests-per-minute rate right now (at most the ceiling)."""
    return 60.0 / _min_interval[0]


def rpm_ceiling() -> float:
    return _rpm_ceiling[0]


def _rate_limited(now: float | None = None) -> float | None:
    """A hosted API said 429: halve the working rate for every worker. Several workers
    usually get the same burst of 429s, so only one cut per RPM_CUT_COOLDOWN_S counts.
    Returns the new rate, or None when this 429 belongs to a burst already handled."""
    now = time.monotonic() if now is None else now
    with _pace_lock:
        # only a recent CUT makes this 429 part of a burst already handled; a recent raise
        # (the rate just went up) is exactly when a new 429 must cut again
        if _rpm_cut[0] and now - _rpm_cut[0] < RPM_CUT_COOLDOWN_S and current_rpm() < _rpm_ceiling[0]:
            return None
        rate = max(min(RPM_FLOOR, _rpm_ceiling[0]), current_rpm() / 2)
        _min_interval[0] = 60.0 / rate
        _rpm_changed[0] = _rpm_cut[0] = now
        return rate


def _went_through(now: float | None = None) -> float | None:
    """A call succeeded: after RPM_RAISE_EVERY_S with no 429, step the working rate back
    up by RPM_RAISE_STEP of the ceiling. Returns the new rate when it changed."""
    now = time.monotonic() if now is None else now
    with _pace_lock:
        rate, ceiling = current_rpm(), _rpm_ceiling[0]
        if rate >= ceiling or now - _rpm_changed[0] < RPM_RAISE_EVERY_S:
            return None
        rate = min(ceiling, rate + ceiling * RPM_RAISE_STEP)
        _min_interval[0] = 60.0 / rate
        _rpm_changed[0] = now
        return rate


class RetriesExhausted(RuntimeError):
    """Every retry of a transient failure (429, 5xx, timeout, dropped connection, bad 200)
    failed. The request itself may be fine -- try again later -- unlike a 4xx."""


class RequestRejected(RuntimeError):
    """HTTP 400/422: the request itself is wrong (too long, unsupported parameter). Sending
    it again gets the same answer, so it is never retried."""


class BackendUnavailable(RuntimeError):
    """Every remaining video would fail the same way: bad key or model
    (401/403/404/410), out of credits (402), or a daily quota spent. The run
    stops instead of marking hundreds of videos 'failed'."""


@dataclass
class Completion:
    """What a backend returns: the text, whether the model hit its output cap, and
    diagnostics for the call log (finish reason, token usage, provider, seconds).
    A truncated JSON answer cannot be repaired -- only asked for again, shorter."""
    text: str
    truncated: bool = False
    info: dict = dc_field(default_factory=dict)


class ExtractionFailed(RuntimeError):
    """No valid record after repairs and the windowed fallback. Carries the call
    log so the failure can be diagnosed without re-running it."""

    def __init__(self, msg: str, calls: list[dict]):
        super().__init__(msg)
        self.calls = calls


def _as_completion(c) -> "Completion":
    return c if isinstance(c, Completion) else Completion(str(c or ""))


def _openai_content(j: dict) -> tuple[str | None, bool]:
    choice = (j.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content"), choice.get("finish_reason") == "length"


def _ollama_content(j: dict) -> tuple[str | None, bool]:
    return j.get("response"), j.get("done_reason") == "length"


def _call_info(j: dict) -> dict:
    """Why a call ended and what it cost, from an OpenAI-style or Ollama response.
    `reasoning_tokens` > 0 means hidden thinking ate into the output cap."""
    info: dict = {}
    choice = (j.get("choices") or [{}])[0] if j.get("choices") else {}
    finish = choice.get("finish_reason") or j.get("done_reason")
    if finish:
        info["finish"] = finish
    native = choice.get("native_finish_reason")
    if native and native != finish:
        info["native_finish"] = native
    u = j.get("usage") or {}
    if u:
        info["in_tokens"] = u.get("prompt_tokens")
        info["out_tokens"] = u.get("completion_tokens")
        if u.get("cost") is not None:                          # OpenRouter usage accounting
            info["cost"] = u.get("cost")
        r = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if r:
            info["reasoning_tokens"] = r
    elif "eval_count" in j:                                   # Ollama
        info["in_tokens"], info["out_tokens"] = j.get("prompt_eval_count"), j.get("eval_count")
    reasoning = (choice.get("message") or {}).get("reasoning")
    if reasoning:
        info["reasoning_chars"] = len(reasoning)
    if j.get("provider"):                                     # OpenRouter's upstream
        info["provider"] = j["provider"]
    return info


def _post_with_backoff(url: str, body: dict, timeout: float, headers: dict,
                       parse: Callable[[dict], tuple] = _openai_content) -> Completion:
    """POST with exponential backoff + jitter for every failure a retry can fix.

    Retried (up to LLM_MAX_RETRIES, waits 10, 20, 40, 80, 160s +-20%):
      - HTTP 429 and 5xx (Retry-After honoured; a 429 on a hosted API also
        pauses every other worker and halves the shared rate, which climbs
        back after quiet minutes -- see _rate_limited / _went_through)
      - dropped / refused connections
      - HTTP 200 with a bad payload: not JSON, an `error` object instead of
        choices (OpenRouter does this when an upstream provider fails), or
        empty content
      - a read timeout, but only LLM_TIMEOUT_RETRIES times: each try already
        waited LLM_TIMEOUT_S
    Not retried -- raised as BackendUnavailable so the run stops: 401/403/404/410
    (key or model), 402 (credits), a spent daily quota. Anything else raises
    RuntimeError and the caller's whole-video retry decides.
    """
    import httpx
    local = is_local_endpoint(url)
    timeouts = 0
    last = "no attempt made"
    started = time.monotonic()
    for attempt in range(LLM_MAX_RETRIES + 1):
        final = attempt == LLM_MAX_RETRIES
        _acquire_slot(url)
        try:
            r = httpx.post(url, json=body, timeout=timeout, headers=headers)
        except httpx.TimeoutException as e:
            timeouts += 1
            if timeouts > LLM_TIMEOUT_RETRIES or final:
                raise RetriesExhausted(f"no response within {timeout:.0f}s ({timeouts} tries)") from e
            _backoff(attempt, None, f"timed out after {timeout:.0f}s")
            continue
        except httpx.TransportError as e:            # refused / reset / DNS blip
            last = f"connection error ({type(e).__name__})"
            if final:
                raise RetriesExhausted(f"{last} after {attempt + 1} tries: {e}") from e
            _backoff(attempt, None, last)
            continue

        if r.status_code == 429 and ("per-day" in r.text or "per day" in r.text.lower()):
            # OpenRouter's daily :free quota (50, or 1000 with >=$10 purchased):
            # retrying for minutes cannot help, it resets tomorrow.
            raise BackendUnavailable(f"daily request quota used up: {r.text[:200]}")
        if r.status_code == 402:
            raise BackendUnavailable(f"HTTP 402, out of credits / key limit reached: {r.text[:200]}")
        if r.status_code in (401, 403, 404, 410):
            raise BackendUnavailable(f"HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code in (408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529):
            last = f"HTTP {r.status_code}"
            if r.status_code == 429 and not local:
                slower = _rate_limited()
                if slower is not None:
                    _say(f"rate limited: slowing every worker to {slower:.0f} requests/min")
            if final:
                raise RetriesExhausted(f"{last} after {attempt + 1} tries: {r.text[:200]}")
            _backoff(attempt, r.headers.get("retry-after"), last,
                     pause_all=r.status_code == 429 and not local)
            continue
        if r.status_code >= 400:                       # 400/422: the request itself is wrong
            raise RequestRejected(f"HTTP {r.status_code} from {url}: {r.text[:300]}")

        try:
            j = r.json()
        except ValueError:
            j = None
        err = j.get("error") if isinstance(j, dict) else None
        content, truncated = parse(j) if isinstance(j, dict) and not err else (None, False)
        if content:
            if not local:
                _went_through()
            info = _call_info(j)
            info["seconds"] = round(time.monotonic() - started, 1)
            if attempt:
                info["http_retries"] = attempt
            return Completion(content, truncated, info)
        last = (f"provider error in 200 response: {str(err)[:120]}" if err
                else "empty response" if isinstance(j, dict) else "non-JSON response body")
        if final:
            raise RetriesExhausted(f"{last} after {attempt + 1} tries")
        _backoff(attempt, None, last)
    raise RetriesExhausted(last)


def _backoff(attempt: int, retry_after: str | None, why: str, pause_all: bool = False) -> None:
    import random
    try:
        wait = float(retry_after) if retry_after else None
    except ValueError:
        wait = None                                  # HTTP-date form: ignore, use ours
    if wait is not None:
        # honour the provider, but never let one header freeze every worker for an hour
        wait = min(max(wait, 0.0), LLM_BACKOFF_MAX_S)
    if wait is None:
        wait = min(LLM_BACKOFF_BASE_S * 2 ** attempt, LLM_BACKOFF_MAX_S)
        wait *= random.uniform(0.8, 1.2)             # jitter
    if pause_all:
        with _pace_lock:
            _pause_until[0] = max(_pause_until[0], time.monotonic() + wait)
    _say(f"{why}, retry {attempt + 1}/{LLM_MAX_RETRIES} in {wait:.0f}s")
    time.sleep(wait)


BACKENDS: dict[str, Callable[..., str]] = {
    "ollama": ollama_json,
    "openai": openai_compatible_json,
}


# --------------------------------------------------------------------------
# Validated generation with a repair loop
# --------------------------------------------------------------------------

def generate_validated(prompt: str, model_cls: type[BaseModel], backend: str = LLM_BACKEND,
                       max_repairs: int = EXTRACT_MAX_REPAIRS, on_step: Step = None,
                       calls: list[dict] | None = None, kind: str = "extract",
                       budget: tuple[int, int] = (MAX_HIGHLIGHTS, MAX_ADVICE)):
    """Generate, validate, and on failure re-prompt with the SPECIFIC validator
    errors. Returns (instance | None, errors); every call made is appended to
    `calls` ({kind, ok, truncated, error}) so failures are diagnosable later.

    A *truncated* answer (the model hit its output cap) is not repaired: the
    repair prompt would have to reproduce the same too-long answer. It is asked
    for once more with tighter size limits instead.
    """
    fn = BACKENDS[backend]
    schema = model_cls.model_json_schema()
    calls = calls if calls is not None else []
    errors: list[str] = []

    def call(p: str, k: str) -> Completion:
        c = _as_completion(fn(p, schema))
        calls.append({"kind": k, "ok": False, "truncated": c.truncated, "chars": len(c.text),
                      **c.info})
        return c

    def complete_anyway(c: Completion):
        """A 'truncated' answer can still be whole JSON: some models pad a finished
        answer with whitespace until the cap. Accept it rather than re-asking."""
        try:
            inst = model_cls.model_validate_json(c.text.strip())
        except (ValidationError, json.JSONDecodeError, ValueError):
            return None
        calls[-1].update(ok=True, note="cut off after a complete answer")
        return inst

    def evidence(c: Completion, why: str) -> None:
        calls[-1]["error"] = why[:200]
        calls[-1]["head"], calls[-1]["tail"] = c.text[:160], c.text[-160:]

    c = call(prompt, kind)
    if c.truncated and (inst := complete_anyway(c)) is not None:
        return inst, errors
    if c.truncated:
        evidence(c, "truncated")
        # Observed cause: a provider looping on one value ("idea", "idea", ...) until
        # the cap -- not a long answer. The retry goes to a different provider.
        if c.info.get("provider"):
            _tls.avoid_providers = (getattr(_tls, "avoid_providers", None) or set()) | {c.info["provider"]}
            calls[-1]["retry_avoids"] = c.info["provider"]
        _step(on_step, "output cut off; asking for a shorter answer")
        c = call(prompt + _P().CONCISE_SUFFIX.format(
            max_highlights=max(2, budget[0] * 2 // 3), max_advice=max(2, budget[1] * 2 // 3)),
            f"{kind}:concise")
        if c.truncated and (inst := complete_anyway(c)) is not None:
            return inst, errors
        if c.truncated:
            evidence(c, "truncated twice")
            return None, ["output truncated twice"]

    for attempt in range(max_repairs + 1):
        try:
            inst = model_cls.model_validate_json(c.text)
            calls[-1]["ok"] = True
            return inst, errors
        except (ValidationError, json.JSONDecodeError) as e:
            errors.append(f"attempt {attempt}: {str(e)[:600]}")
            evidence(c, str(e))
            if attempt == max_repairs:
                return None, errors
            _step(on_step, f"repair {attempt + 1}")
            c = call(_P().REPAIR_PROMPT.format(errors=str(e)[:1500], previous=c.text),
                     f"{kind}:repair")
            if c.truncated:
                evidence(c, "repair truncated")
                return None, errors
    return None, errors


# --------------------------------------------------------------------------
# Chapters
# --------------------------------------------------------------------------

def resolve_chapters(transcript: dict, uploader_chapters: list[dict],
                     backend: str = LLM_BACKEND,
                     on_step: Step = None) -> tuple[list[Chapter], str]:
    """Uploader chapters win. They are exact, human-authored and free."""
    if uploader_chapters:
        return [Chapter(**c) for c in uploader_chapters], "uploader"
    if _P().kind in ("article", "chapter"):
        return [], "none"       # their sections are their headings; none means one section
    _step(on_step, "chaptering")
    prompt = _P().CHAPTER_PROMPT.format(
        schema=json.dumps(ChapterList.model_json_schema(), indent=2),
        transcript=_P().format(transcript["utterances"], with_ms=True),
    )
    result, errs = generate_validated(prompt, ChapterList, backend, on_step=on_step,
                                      calls=_tls_calls(), kind="chapters")
    if result is None:
        return [], "none"       # not fatal: extraction proceeds without chapters
    return snap_chapters(result.chapters, transcript["utterances"]), "llm"


def snap_chapters(chapters: list[Chapter], utterances: list[dict]) -> list[Chapter]:
    """Snap each LLM-proposed boundary to the nearest real utterance start.

    Models copy the `[ms]` markers approximately at best. Snapping guarantees a
    chapter boundary is always a timestamp that actually exists in the video,
    which is what makes chapter deep links trustworthy.
    """
    starts = [int(u["start_ms"]) for u in utterances]
    if not starts:
        return chapters
    out = []
    for ch in sorted(chapters, key=lambda c: c.start_ms):
        nearest = min(starts, key=lambda s: abs(s - ch.start_ms))
        out.append(ch.model_copy(update={"start_ms": nearest}))
    for i in range(len(out) - 1):       # contiguous, non-overlapping
        out[i] = out[i].model_copy(update={"end_ms": out[i + 1].start_ms})
    if out:
        out[-1] = out[-1].model_copy(update={"end_ms": max(
            int(u["end_ms"]) for u in utterances)})
    return out


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def est_tokens(utterances: list[dict]) -> int:
    return int(sum(len(u.get("text", "").split()) for u in utterances) * TOKENS_PER_WORD)




def _P():
    """The prompts for the Document being extracted (its Source kind, ADR-0013)."""
    return getattr(_tls, "prompts", None) or prompts.for_kind("talk")


def _tls_calls() -> list[dict]:
    calls = getattr(_tls, "calls", None)
    return calls if calls is not None else []


def extract_video(transcript: dict, meta: dict, uploader_chaps: list[dict],
                  backend: str = LLM_BACKEND, on_step: Step = None) -> VideoMetadata:
    _tls.on_step = on_step            # retries/backoff report through the same callback
    _tls.calls = []                   # every LLM call for this video, kept in extraction_meta
    _tls.avoid_providers = None       # providers that looped on this video (OpenRouter)
    _tls.prompts = prompts.for_kind(meta.get("source_kind") or transcript.get("source_kind"))
    try:
        return _extract_video(transcript, meta, uploader_chaps, backend, on_step)
    finally:
        _tls.on_step = None
        _tls.calls = None
        _tls.avoid_providers = None
        _tls.prompts = None


def _extract_video(transcript: dict, meta: dict, uploader_chaps: list[dict],
                   backend: str, on_step: Step) -> VideoMetadata:
    calls = _tls.calls
    chapters, chapter_source = resolve_chapters(transcript, uploader_chaps, backend, on_step)
    utterances = transcript["utterances"]
    chapter_titles = ", ".join(c.title for c in chapters) or "(none)"
    tokens = est_tokens(utterances)
    _step(on_step, f"{len(chapters)} ch ({chapter_source})")
    _step(on_step, f"~{tokens / 1000:.1f}k tok")

    windows = 1
    single = tokens <= SINGLE_CALL_MAX_TOKENS
    gen = None
    if single:
        _step(on_step, "extracting")
        gen, _ = _extract_once(utterances, meta, chapter_titles, backend, on_step, calls)
    if gen is None:
        _step(on_step, "too long; extracting in windows" if not single
              else "invalid; extracting in windows")
        gen, windows = _extract_windowed(utterances, chapters, meta, chapter_titles, backend,
                                         on_step, calls)
    if gen is None:
        raise ExtractionFailed("extraction failed validation after repairs and windowed fallback",
                               list(calls))

    # Local self-check (no LLM): are the quotes really in the transcript, and did
    # a substantial talk yield any advice? If not, one retry with specific
    # feedback. Keeps whichever attempt grounds more items.
    found, checked = _grounding(gen, utterances)
    words = sum(len(u.get("text", "").split()) for u in utterances)
    problems = _problems(gen, utterances, found, checked, words)
    grounding = {"found": found, "checked": checked,
                 "rate": round(found / checked, 3) if checked else None, "retried": False}
    if problems and single:
        _step(on_step, f"self-check: {found}/{checked} quotes found; retrying with feedback")
        retry, _ = _extract_once(utterances, meta, chapter_titles, backend, on_step, calls,
                                 feedback=_P().GROUNDING_FEEDBACK.format(problems=problems),
                                 kind="extract:grounding-retry")
        grounding["retried"] = True
        if retry is not None:
            f2, c2 = _grounding(retry, utterances)
            if (f2, -abs(c2 - f2)) > (found, -abs(checked - found)):
                gen, found, checked = retry, f2, c2
                grounding.update(found=f2, checked=c2,
                                 rate=round(f2 / c2, 3) if c2 else None, kept="retry")
            else:
                grounding["kept"] = "first"

    kind = _P().kind
    talk = kind == "talk"
    fields = gen.model_dump()
    if not talk and meta.get("speaker"):
        fields["speaker"] = meta["speaker"]      # given (the page's author, the Book's authors) beats generated
    return VideoMetadata(
        **fields,
        doc_id=meta["doc_id"],
        title_raw=meta.get("title") or "",
        # a talk's link is its video; a private Book has no url, so its Citation is its page label
        url=meta.get("url") or (f"https://www.youtube.com/watch?v={meta['doc_id']}" if talk else ""),
        series=meta.get("series"),
        provenance=meta.get("provenance") or "yc-official",
        published_at=meta.get("published_at"),
        duration_s=meta.get("duration_s"),
        caption_kind=meta.get("caption_kind") or ("auto" if talk else "none"),
        source_kind=kind,
        locator=source_kinds.by_name(kind).locator,
        private=bool(meta.get("private")),
        page_labels=meta.get("page_labels") or {},
        chapters=chapters,
        extraction_meta=ExtractionMeta(
            model=LLM_MODEL, backend=backend, schema_version=SCHEMA_VERSION,
            prompt_hash=_P().hash(),
            passes=windows, chapter_source=chapter_source,
            processed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            calls=list(calls), grounding=grounding,
        ),
    )


def _grounding(gen: Generated, utterances: list[dict]) -> tuple[int, int]:
    """(quotes found in the transcript, quotes checked) — the same test `verify` applies."""
    from ..verify import find_evidence
    items = list(gen.highlights) + list(gen.advice_atoms)
    found = sum(1 for it in items if find_evidence(it.evidence_span or "", utterances).ok)
    return found, len(items)


def _problems(gen: Generated, utterances: list[dict], found: int, checked: int,
              words: int) -> str:
    """Feedback for the grounding retry, or "" when the result is good enough."""
    from ..verify import find_evidence
    out = []
    if checked and found / checked < GROUNDING_RETRY_BELOW:
        missing = list(dict.fromkeys(it.evidence_span for it in list(gen.highlights) + list(gen.advice_atoms)
                                     if not find_evidence(it.evidence_span or "", utterances).ok))[:8]
        out.append(f"- Only {found} of {checked} evidence quotes appear in the transcript. "
                   "These were NOT found (they read like paraphrases or quotes from memory):")
        out += [f'    "{q[:160]}"' for q in missing]
    if not checked:
        out.append("- You returned no highlights or advice.")
    if not gen.advice_atoms and words >= NO_ADVICE_RETRY_MIN_WORDS:
        out.append(f"- You returned no advice atoms. If the {_P().kind} recommends anything a founder "
                   "could act on, extract it as advice atoms (imperative, one idea each, with "
                   "its own quote); if it truly contains none, say so in unknowns_and_gaps.")
        if gen.highlights:
            out.append("  Several of your highlights read like advice; each one that tells a "
                       "founder what to do must also be an advice atom:")
            out += [f'    "{h.text[:140]}"' for h in gen.highlights[:6]]
    return "\n".join(out)


def _series(meta: dict) -> str:
    """The prompt's Series line; a Book's names its authors too ("Zero to One by Peter Thiel...")."""
    series = meta.get("series") or "(none)"
    if _P().kind == "chapter" and meta.get("speaker"):
        series += f" by {meta['speaker']}"
    return series


def _extract_once(utterances: list[dict], meta: dict, chapter_titles: str, backend: str,
                  on_step: Step = None, calls: list[dict] | None = None,
                  feedback: str = "", kind: str = "extract"):
    budget = item_budget(sum(len(u.get("text", "").split()) for u in utterances))
    prompt = _P().EXTRACT_PROMPT.format(
        title=meta.get("title", ""), series=_series(meta),
        chapter_titles=chapter_titles,
        max_highlights=budget[0], max_advice=budget[1],
        schema=json.dumps(Generated.model_json_schema(), indent=2),
        feedback=feedback,
        transcript=_P().format(utterances),
    )
    return generate_validated(prompt, Generated, backend, on_step=on_step, calls=calls, kind=kind,
                              budget=budget)


def item_budget(words: int) -> tuple[int, int]:
    """(highlights, advice) to ask for: scales with length, capped for long talks."""
    return (max(2, min(MAX_HIGHLIGHTS, round(words / WORDS_PER_HIGHLIGHT))),
            max(2, min(MAX_ADVICE, round(words / WORDS_PER_ADVICE))))


def _windows(utterances: list[dict], chapters: list[Chapter]) -> list[list[dict]]:
    """Consecutive utterances grouped into ~WINDOW_TOKENS windows, cut at chapter
    starts when one falls near the budget, else at an utterance boundary."""
    starts = {c.start_ms for c in chapters}
    out: list[list[dict]] = [[]]
    size = 0.0
    for u in utterances:
        n = len(u.get("text", "").split()) * TOKENS_PER_WORD
        at_chapter = u.get("start_ms") in starts
        if out[-1] and (size + n > WINDOW_TOKENS or (at_chapter and size > WINDOW_TOKENS * 0.6)):
            out.append([])
            size = 0.0
        out[-1].append(u)
        size += n
    return [w for w in out if w]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


def _round_robin(groups: list[list], cap: int) -> list:
    """Take items across windows in turn so a cap never drops a whole section."""
    out, i = [], 0
    while len(out) < cap and any(i < len(g) for g in groups):
        for g in groups:
            if i < len(g) and len(out) < cap:
                out.append(g[i])
        i += 1
    return out


def _extract_windowed(utterances: list[dict], chapters: list[Chapter], meta: dict,
                      chapter_titles: str, backend: str, on_step: Step = None,
                      calls: list[dict] | None = None) -> tuple[Generated | None, int]:
    """Fallback for very long or repeatedly-failing transcripts: a few ~8k-token
    windows instead of one call per chapter, merged with de-duplication and caps,
    then one short call to write talk-level fields over the whole talk."""
    wins = _windows(utterances, chapters)
    parts: list[Generated] = []
    for i, w in enumerate(wins):
        _step(on_step, f"window {i + 1}/{len(wins)}")
        part, _ = _extract_once(w, meta, chapter_titles, backend, on_step, calls,
                                kind=f"window {i + 1}/{len(wins)}")
        if part is not None:
            parts.append(part)
    if not parts:
        return None, len(wins)

    def dedup(items):
        seen, out = set(), []
        for it in items:
            k = _norm(it.text)
            if k and k not in seen:
                seen.add(k)
                out.append(it)
        return out

    highlights = _round_robin([dedup(p.highlights) for p in parts], int(MAX_HIGHLIGHTS * 1.5))
    advice = _round_robin([dedup(p.advice_atoms) for p in parts], int(MAX_ADVICE * 1.5))
    advice = [a.model_copy(update={"atom_id": f"a{n:02d}"}) for n, a in enumerate(dedup(advice), 1)]
    merged = parts[0].model_copy(update={"highlights": dedup(highlights), "advice_atoms": advice})
    for p in parts[1:]:
        merged.unknowns_and_gaps += [g for g in p.unknowns_and_gaps if g not in merged.unknowns_and_gaps]
        for field in ("people", "companies", "frameworks", "books", "yc_jargon"):
            seen = getattr(merged.entities, field)
            for v in getattr(p.entities, field):
                if v not in seen:
                    seen.append(v)

    if len(parts) > 1:
        _step(on_step, "overview")
        prompt = _P().OVERVIEW_PROMPT.format(
            title=meta.get("title", ""), series=_series(meta),
            summaries="\n".join(f"- {p.summary}" for p in parts),
            takeaways="\n".join(f"- {h.text}" for h in merged.highlights),
            schema=json.dumps(Overview.model_json_schema(), indent=2))
        ov, _ = generate_validated(prompt, Overview, backend, on_step=on_step, calls=calls,
                                   kind="overview")
        if ov is not None:
            merged = merged.model_copy(update=ov.model_dump())
    return merged, len(wins)
