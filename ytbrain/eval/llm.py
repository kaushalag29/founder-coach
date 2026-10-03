"""LLM calls for eval builds: typed JSON answers from a named model, run in parallel.

Reuses the extract runner's HTTP layer, so every call gets the same exponential backoff
(429/5xx/dropped connections/bad 200s), shared requests-per-minute pacing across threads,
and a clean stop on a bad key, model or exhausted credit (BackendUnavailable).

Parallelism: daemon worker threads make calls; the caller's thread receives results in
completion order and does all writing. Ctrl+C exits at once (daemon threads), a spend cap
stops handing out new work, and anything unfinished is simply redone by the next build.
"""
from __future__ import annotations

import json
import queue
import statistics
import threading
import time
from dataclasses import dataclass, field as dc_field
from typing import Callable

from pydantic import BaseModel, ValidationError

from ..config import EVAL_TIMEOUT_S
from ..extract import runner
from ..extract.runner import BackendUnavailable

REPAIR = """Your previous answer was not valid for this JSON schema:
{error}

Return ONLY the corrected JSON, complete.

Previous answer:
{previous}
"""


# Per-model request adjustments, filled by the build's preflight from the endpoint's
# model list (e.g. {"reasoning": None} for a model that doesn't accept `reasoning`).
MODEL_OVERRIDES: dict[str, dict] = {}


@dataclass
class Answer:
    value: BaseModel | None
    cost: float = 0.0
    calls: int = 0
    error: str | None = None
    info: dict = dc_field(default_factory=dict)


def ask(prompt: str, model_cls: type[BaseModel], model: str, repairs: int = 1) -> Answer:
    """One typed answer from `model` (OpenAI-compatible endpoint), with a repair retry."""
    schema = model_cls.model_json_schema()
    out = Answer(None)
    p = prompt
    for attempt in range(repairs + 1):
        try:
            c = runner._as_completion(runner.openai_compatible_json(
                p, schema, model=model, timeout=EVAL_TIMEOUT_S, overrides=MODEL_OVERRIDES.get(model)))
        except BackendUnavailable:
            raise
        except Exception as e:
            if out.cost:              # the first call was paid for: don't lose it with the repair's error
                e.cost = out.cost     # type: ignore[attr-defined]
            raise
        out.calls += 1
        out.cost += float(c.info.get("cost") or 0.0)
        out.info = c.info
        try:
            out.value = model_cls.model_validate_json(c.text.strip())
            out.error = None
            return out
        except (ValidationError, json.JSONDecodeError, ValueError) as e:
            out.error = f"{type(e).__name__}: {str(e)[:300]}"
            p = prompt + "\n\n" + REPAIR.format(error=str(e)[:800], previous=c.text[:6000])
    return out


@dataclass
class Budget:
    """Spend cap shared by all workers. `spent` starts from what the build already cost."""
    limit: float
    spent: float = 0.0
    _lock: threading.Lock = dc_field(default_factory=threading.Lock)

    def add(self, cost: float) -> None:
        with self._lock:
            self.spent += cost

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit        # --max-cost 0 means "spend nothing", not "no cap"


def _progress(done: int, total: int, started: float, budget: Budget | None, spent0: float) -> str:
    """'119/4528 · 58/min · ETA 76 min · $0.057 spent (~$2.17 at the end)'"""
    elapsed = max(1e-6, time.time() - started)
    rate = done / elapsed
    parts = [f"{done}/{total}"]
    if done:
        parts += [f"{rate * 60:.0f}/min", f"ETA {(total - done) / rate / 60:.1f} min"]
    else:
        parts.append("ETA -- (waiting for the first answers)")
    if budget:
        spent = f"${budget.spent:.3f} spent"
        if done:
            projected = budget.spent + (budget.spent - spent0) / done * (total - done)
            spent += f" (~${projected:.2f} at the end)" if projected >= 1 else f" (~${projected:.3f} at the end)"
        parts.append(spent)
    if runner.current_rpm() < runner.rpm_ceiling() - 0.5:        # slowed down by 429s
        parts.append(f"rate {runner.current_rpm():.0f}/{runner.rpm_ceiling():.0f} per min after 429s")
    return " · ".join(parts)


HEDGE_MIN_S = 30        # never ask again before a call has waited this long...
HEDGE_FACTOR = 5        # ...and not before it is this many times slower than the typical call


def run_parallel(jobs: list, work: Callable, on_result: Callable, *, workers: int,
                 budget: Budget | None = None, label: str = "", heartbeat_s: float = 30,
                 deadline_s: float | None = None, hedge_s: float | None = None) -> str | None:
    """Run `work(job)` for each job on daemon threads; call `on_result(job, result, error)`
    on this thread as each finishes. Returns None when all jobs ran, or why it stopped
    early ("budget" / "backend: ..."). Unfinished jobs are left for the next run.

    A call much slower than its siblings (`hedge_s`; default: 5x the median call, at least 30 s)
    is asked again in parallel and the first answer wins (never while the provider is rate-limiting us,
    when waiting is the right answer): a provider that hangs on one request
    answers the repeat in seconds, so the last job of a batch no longer waits for the deadline.
    A call still running after `deadline_s` (default: 3x the per-call timeout, which a
    provider trickling bytes never trips) is given up on: reported as failed, its worker
    replaced, and its late answer ignored, so one stuck request can't hold the whole step."""
    if not jobs:
        return None
    deadline_s = deadline_s if deadline_s is not None else 3 * EVAL_TIMEOUT_S + 60
    todo: queue.Queue = queue.Queue()
    done: queue.Queue = queue.Queue()
    for i, j in enumerate(jobs):
        todo.put((i, j))
    stop = threading.Event()
    inflight: dict[str, tuple[float, int]] = {}          # thread -> (call started, job index)
    abandoned: set[int] = set()
    hedged: set[int] = set()                             # jobs asked a second time
    completed: set[int] = set()                          # jobs whose first answer was taken
    durations: list[float] = []                          # seconds taken by calls that answered
    lock = threading.Lock()

    def worker() -> None:
        me = threading.current_thread().name
        while not stop.is_set():
            try:
                i, job = todo.get_nowait()
            except queue.Empty:
                return
            if budget and budget.exhausted:
                stop.set()
                return
            t0 = time.time()
            with lock:
                inflight[me] = (t0, i)
            result, err = None, None
            try:
                result = work(job)
            except BackendUnavailable as e:
                stop.set()
                err = e
            except Exception as e:                       # one job's failure never stops the rest
                err = e
            with lock:                                   # leave `inflight` before reporting, so the
                inflight.pop(me, None)                   # loop below sees only the OTHER attempts
                if err is None:
                    durations.append(time.time() - t0)
            done.put((i, job, result, err))
            if i in abandoned:                           # replaced while stuck: let the new one work
                return

    n_threads = max(1, min(workers, len(jobs)))
    spawned = [0]

    def spawn() -> threading.Thread:
        spawned[0] += 1
        t = threading.Thread(target=worker, daemon=True, name=f"eval-{spawned[0]}")
        t.start()
        return t

    threads = [spawn() for _ in range(n_threads)]
    finished, stopped, last = 0, None, time.time()
    started = time.time()
    spent0 = budget.spent if budget else 0.0
    errors: dict[str, int] = {}
    while finished < len(jobs):
        try:
            i, job, result, err = done.get(timeout=0.5)
        except queue.Empty:
            if not any(t.is_alive() for t in threads) and done.empty():
                break
            with lock:
                calls = list(inflight.items())
            now = time.time()
            for name, (t0, i) in calls:
                if now - t0 > deadline_s and i not in abandoned:
                    abandoned.add(i)
                    finished += 1
                    msg = f"no answer after {now - t0:.0f}s (gave up; redone next run)"
                    errors[msg[:60]] = errors.get(msg[:60], 0) + 1
                    on_result(jobs[i], None, TimeoutError(msg))
                    threads.append(spawn())
            with lock:
                typical = statistics.median(durations) if len(durations) >= 5 else None
            hedge_after = hedge_s if hedge_s is not None else (
                max(HEDGE_MIN_S, HEDGE_FACTOR * typical) if typical is not None else None)
            # Slow because the provider is rate-limiting us (429s, backoff, a halved request rate) is not
            # a hung call: a repeat would only add load and cost, so wait instead.
            rate_limited = runner.current_rpm() < runner.rpm_ceiling() - 0.5
            if hedge_after is not None and not rate_limited:
                for name, (t0, i) in calls:
                    if now - t0 > hedge_after and i not in hedged and i not in abandoned and i not in completed:
                        hedged.add(i)
                        todo.put((i, jobs[i]))
                        threads.append(spawn())
                        print(f"    {label}: a call has waited {now - t0:.0f}s"
                              + (f" (typical {typical:.0f}s)" if typical is not None else "")
                              + "; asking again in parallel, the first answer wins", flush=True)
            if now - last >= heartbeat_s:
                last = now
                waiting = ""
                starts = [t0 for t0, _ in (v for _, v in calls)]
                if starts:
                    oldest = now - min(starts)
                    waiting = (f" · {len(starts)} call(s) in flight, oldest {oldest:.0f}s"
                               + (" (a slow provider; it's retried or given up on at the deadline, and "
                                  "anything unfinished is redone on the next run)" if oldest > 60 else ""))
                print(f"    {label}: {_progress(finished, len(jobs), started, budget, spent0)}{waiting}", flush=True)
            continue
        if i in abandoned or i in completed:
            continue                                     # a late answer for a job already settled
        if err is not None and not isinstance(err, BackendUnavailable) and i in hedged:
            with lock:
                other_running = any(idx == i for _, idx in inflight.values())
            if other_running:
                continue                                 # the repeat of this call may still answer
        completed.add(i)
        finished += 1
        if isinstance(err, BackendUnavailable):
            stopped = f"backend: {err}"
        elif err is not None:
            key = f"{type(err).__name__}: {str(err)[:80]}"
            errors[key] = errors.get(key, 0) + 1
        on_result(job, result, err)
        if finished % max(1, len(jobs) // 10) == 0 or finished == len(jobs):
            print(f"    {label}: {_progress(finished, len(jobs), started, budget, spent0)}", flush=True)
            last = time.time()
    stop.set()
    if errors:
        top = sorted(errors.items(), key=lambda kv: -kv[1])[:3]
        print(f"    {label}: {sum(errors.values())} call(s) failed and are redone on the next run; "
              + "; ".join(f"{n}x {k}" for k, n in top), flush=True)
    if stopped:
        return stopped
    if budget and budget.exhausted and finished < len(jobs):
        return "budget"
    return None
