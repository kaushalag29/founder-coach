"""`ytbrain eval build`: build or resume one split of the benchmark (docs/eval-spec.md §6).

Steps, each resumable from the eval database:
  1 questions   dev: sample Advice/Takeaways and generate questions (parallel, LLM)
  2 rewrites    one query rewrite per question for the fifth pooling variant (parallel, LLM)
  3 pool        five retrieval variants -> candidate Moments (local models, main thread)
  4 judge       two judges grade every candidate, a third breaks ties (parallel, LLM)
  5 finalize    decide grades, drop questions whose seed isn't relevant, write eval/ files
A spend cap, a refused endpoint or Ctrl+C stops the build cleanly; re-running resumes.
"""
from __future__ import annotations

import json
import math
import re
import time
from functools import lru_cache

from ..config import (EVAL_DATA, EVAL_DIR, EVAL_GENERATOR, EVAL_JUDGE_BATCH,
                      EVAL_JUDGES, EVAL_MAX_COST, EVAL_MAX_RPM, EVAL_POOL_DEPTH, EVAL_TUNING_OVERSAMPLE,
                      EVAL_TUNING_SIZE, EVAL_WORKERS, TRANSCRIPTS)
from ..extract import runner
from . import files, generate, judge, pool
from .db import EvalDB
from .llm import Budget, ask, run_parallel
from .moments import is_article_moment, moment_text, parse_moment_id

EXIT_STOPPED = 2          # stopped early (budget / endpoint); re-run to resume


class Env:
    """Everything a build needs, created once; tests pass fakes."""

    def __init__(self, store=None, embed=None, reranker=None, ask_fn=ask, db: EvalDB | None = None,
                 root=EVAL_DIR, generator=EVAL_GENERATOR, judges=None, workers=EVAL_WORKERS,
                 max_cost=EVAL_MAX_COST, transcripts=TRANSCRIPTS, manifest=None):
        self.store, self.embed, self.reranker, self.ask = store, embed, reranker, ask_fn
        self.db = db or EvalDB(EVAL_DATA / "eval.db")
        self.root, self.generator = root, generator
        self.judges = judges or list(EVAL_JUDGES)
        self.workers, self.max_cost = workers, max_cost
        self.transcripts, self.manifest = transcripts, manifest

    @lru_cache(maxsize=256)
    def utterances(self, doc_id: str) -> tuple:
        p = self.transcripts / f"{doc_id}.json"
        return tuple(json.loads(p.read_text())["utterances"]) if p.exists() else ()

    def moment_text(self, mid: str) -> str:
        doc, start = parse_moment_id(mid)
        return moment_text(list(self.utterances(doc)), start, article=is_article_moment(mid))


def _say(msg: str) -> None:
    print(msg, flush=True)


class PaidFailure(ValueError):
    """A job that failed after its LLM calls were paid for: the cost still counts."""

    def __init__(self, msg: str, cost: float = 0.0):
        super().__init__(msg)
        self.cost = cost


def _paid(err) -> float:
    return float(getattr(err, "cost", 0.0) or 0.0)


def _spend(env: Env, budget: Budget, build: str, model: str, kind: str, cost: float) -> None:
    if cost:
        budget.add(cost)
        env.db.add_spend(build, model, kind, cost)


# --------------------------------------------------------------------------- dev questions

def seed_dev(env: Env, limit: int | None = None) -> int:
    """Step 1a: sample seeds once; later runs reuse them (the sample is part of the set)."""
    if env.db.questions("dev"):
        return 0
    rows = env.store.rows(kinds=["advice", "takeaway"],
                          columns=["item_id", "kind", "doc_id", "text", "evidence", "start_ms",
                                   "topics", "stages", "year", "speaker", "title"])
    caption = {}
    if env.manifest is not None:
        caption = {d["doc_id"]: d["caption_kind"] for d in env.manifest.documents()}
    n = limit or math.ceil(EVAL_TUNING_SIZE * EVAL_TUNING_OVERSAMPLE)
    picks = generate.sample_items(rows, n, caption)
    for i, row in enumerate(picks, 1):
        qid = f"dev-{i:04d}"
        seed = generate.seed_record(row, qid)
        env.db.upsert_question(qid, "dev", i, "seeded", seed, seed_moment=seed["seed_moment"])
    _say(f"  questions: sampled {len(picks)} seeds from {len(rows)} Advice/Takeaways")
    return len(picks)


def generate_dev(env: Env, budget: Budget) -> str | None:
    """Step 1b: write a question for every seed not yet written."""
    todo = env.db.questions("dev", ("seeded",))
    if not todo:
        return None
    _say(f"  questions: generating {len(todo)} with {env.generator}")
    accepted_vecs = [q["record"]["_vec"] for q in env.db.questions("dev", ("generated", "accepted"))
                     if q["record"].get("_vec")]

    def work(q):
        return env.ask(generate.prompt_for(q["record"]), generate.GeneratedQuestion, env.generator)

    def on_result(q, ans, err):
        _spend(env, budget, "dev", env.generator, "generate", ans.cost if ans is not None else _paid(err))
        if err is not None or ans is None or ans.value is None:
            env.db.set_status(q["qid"], "seeded", f"generation failed: {err or (ans and ans.error)}")
            return
        g = ans.value
        ok, reason, meas = generate.check_question(g.question, q["record"], env.embed, accepted_vecs)
        rec = {**q["record"], "question": g.question, "question_type": g.question_type,
               "stage": sorted(set(g.stage), key=generate.STAGES.index), "temporal": g.temporal,
               "creation": {"generator_model": env.generator, "prompt_id": "gen-v1",
                            **{k: v for k, v in meas.items() if not k.startswith("_")}}}
        if "_vec" in meas:
            rec["_vec"] = meas["_vec"]
            if ok:
                accepted_vecs.append(meas["_vec"])
        env.db.upsert_question(q["qid"], "dev", q["sort_key"], "generated" if ok else "rejected",
                               rec, text=g.question, seed_moment=q["seed_moment"], reason=reason or None)

    return run_parallel(todo, work, on_result, workers=env.workers, budget=budget, label="generate")


# --------------------------------------------------------------------------- shared steps

def rewrites(env: Env, split: str, budget: Budget, statuses=("generated",)) -> str | None:
    todo = [q for q in env.db.questions(split, statuses) if env.db.rewrite(q["qid"]) is None]
    if not todo:
        return None
    _say(f"  rewrites: {len(todo)} questions")

    def work(q):
        return env.ask(pool.REWRITE_PROMPT.format(question=q["text"]), pool.Rewrite, env.generator)

    def on_result(q, ans, err):
        _spend(env, budget, split, env.generator, "rewrite", ans.cost if ans is not None else _paid(err))
        if ans is not None and ans.value is not None:
            env.db.save_rewrite(q["qid"], ans.value.query)
        else:
            env.db.rewrite_failed(q["qid"])

    return run_parallel(todo, work, on_result, workers=env.workers, budget=budget, label="rewrite")


def pool_step(env: Env, split: str, statuses=("generated",)) -> int:
    depth = EVAL_POOL_DEPTH[split]
    todo = [q for q in env.db.questions(split, statuses) if not env.db.is_pooled(q["qid"])]
    # A question pooled without its rewrite would lose the fifth variant for good (a pool is
    # never redone), so it waits for the rewrite -- unless the rewrite has failed twice.
    waiting = [q for q in todo if env.db.rewrite(q["qid"]) is None and env.db.rewrite_failures(q["qid"]) < 2]
    if waiting:
        _say(f"  pool: {len(waiting)} question(s) wait for their query rewrite (re-run to retry it)")
        todo = [q for q in todo if q not in waiting]
    if todo:
        _say(f"  pool: {len(todo)} questions, top {depth} Moments from each of {len(pool.VARIANTS)} variants")
    started = time.time()
    every = max(1, min(25, len(todo) // 10 or 1))
    for i, q in enumerate(todo, 1):
        entries = pool.pool_question(env.store, env.embed, env.reranker, q["text"],
                                     env.db.rewrite(q["qid"]), depth, q["seed_moment"])
        env.db.save_pool(q["qid"], entries, depth)
        if i % every == 0 or i == len(todo):
            per = (time.time() - started) / i
            _say(f"    pool: {i}/{len(todo)}, {per:.1f}s/question, ETA {per * (len(todo) - i) / 60:.1f} min")
    return len(todo)


def judge_step(env: Env, split: str, budget: Budget, statuses=("generated",)) -> str | None:
    """Grade every pooled Moment with the first two judges, then tie-break where needed."""
    ver = judge.PROMPT_VERSION
    qs = [q for q in env.db.questions(split, statuses) if env.db.is_pooled(q["qid"])]
    for phase, who in (("grade", env.judges[:2]), ("tie-break", env.judges[2:3])):
        jobs = []
        for q in qs:
            have = env.db.grades(q["qid"], ver)
            for j in who:
                if phase == "grade":
                    need = [m for m in env.db.pool(q["qid"]) if j not in have.get(m, {})]
                else:
                    need = [m for m in env.db.pool(q["qid"])
                            if judge.needs_tiebreak(have.get(m, {}), env.judges)]
                for batch in judge.batches(q["qid"], need, EVAL_JUDGE_BATCH, j):
                    jobs.append((q, j, batch))
        if not jobs:
            continue
        _say(f"  judge ({phase}): {len(jobs)} calls over {len(qs)} questions")

        def work(job):
            q, j, batch = job
            ids = {f"P{i + 1}": m for i, m in enumerate(batch)}
            prompt = judge.render(q["text"], [(pid, env.moment_text(m)) for pid, m in ids.items()])
            ans = env.ask(prompt, judge.Scores, j)
            if ans.value is None:
                raise PaidFailure(ans.error or "no answer", ans.cost)
            try:
                return ans, judge.parse(ans.value, ids)
            except ValueError as e:                         # e.g. the judge skipped a passage
                raise PaidFailure(str(e), ans.cost) from e

        def on_result(job, result, err):
            q, j, _ = job
            if result is None:
                _spend(env, budget, split, j, "judge", _paid(err))   # a failed call is still paid
                return                                      # left ungraded: the next build retries it
            ans, grades = result
            _spend(env, budget, split, j, "judge", ans.cost)
            env.db.save_grades(q["qid"], j, ver, grades)

        stopped = run_parallel(jobs, work, on_result, workers=env.workers, budget=budget,
                               label=f"judge {phase}")
        if stopped:
            return stopped
    return None


def decided_labels(env: Env, qid: str) -> tuple[dict[str, dict], int]:
    """({moment: {"grade", "judges"}} for decided Moments, number still undecided)."""
    have = env.db.grades(qid, judge.PROMPT_VERSION)
    out, pending = {}, 0
    for m in env.db.pool(qid):
        g = judge.final_grade(have.get(m, {}), env.judges)
        if g is None:
            pending += 1
        else:
            out[m] = {"grade": g, "judges": have.get(m, {})}
    return out, pending


def popularity(labels: dict[str, dict]) -> str:
    n = sum(lab["grade"] >= 2 for lab in labels.values())
    return "head" if n >= 10 else "torso" if n >= 3 else "tail"


def finalize_dev(env: Env, talk_info: dict[str, dict], freeze_date: str,
                 bump_if_changed: bool = True) -> dict:
    """Decide every question and write the released files once. A question already released
    keeps its slot, and if any of them is still being graded nothing is written (releasing
    now would silently drop it). When the released questions or labels change -- or the last
    release was interrupted -- the new files go out under the next minor version, in the same
    write, so no crash can leave new labels under an old version."""
    kept, discarded, pending, blocked = [], 0, 0, []
    labels_by_q: dict[str, dict] = {}
    qs = env.db.questions("dev", ("generated", "accepted", "discarded", "spare"))
    qs.sort(key=lambda q: (q["status"] != "accepted", q["sort_key"]))   # released ones keep their slot
    for q in qs:
        labels, undecided = decided_labels(env, q["qid"])
        if undecided or not env.db.is_pooled(q["qid"]):
            pending += 1
            if q["status"] == "accepted":
                blocked.append(q["qid"])
            continue
        seed = labels.get(q["seed_moment"], {}).get("grade", 0)
        if seed < 2:
            env.db.set_status(q["qid"], "discarded", f"judges graded its seed Moment {seed}")
            discarded += 1
            continue
        if len(kept) >= EVAL_TUNING_SIZE:
            env.db.set_status(q["qid"], "spare")
            continue
        env.db.set_status(q["qid"], "accepted")
        kept.append(q)
        labels_by_q[q["qid"]] = labels
    out = {"accepted": len(kept), "discarded": discarded, "pending": pending, "blocked": len(blocked),
           "changed": False, "version": files.current_version(env.root)}
    if blocked:
        return out                     # a released question isn't fully graded: keep the old release
    records = [query_record(q, labels_by_q[q["qid"]], freeze_date, "dev") for q in kept]
    if records:
        old_q, old_labels = files.load_split(env.root, "dev")
        new_labels = {q: {m: lab["grade"] for m, lab in ms.items()} for q, ms in labels_by_q.items()}
        released = bool(old_q)
        changed = (new_labels != old_labels or sorted(old_q, key=lambda r: r["_id"]) != records
                   or not files.consistent(env.root))
        version = None
        if released and changed and bump_if_changed:
            version = files.bump_minor(files.current_version(env.root))
        if changed or not released:
            files.write_split(env.root, "dev", records, labels_by_q, talk_info, version=version)
        out.update(changed=changed and released, version=files.current_version(env.root))
    return out


def query_record(q: dict, labels: dict[str, dict], freeze_date: str, split: str) -> dict:
    r = q["record"]
    creation = dict(r.get("creation") or {})
    creation["seed_moment"] = q.get("seed_moment")
    return {"_id": q["qid"], "text": q["text"], "split": split,
            "question_type": r.get("question_type", "how_to_advice"),
            "stage": sorted(set(r.get("stage") or []), key=lambda x: (x not in generate.STAGES,
                                                                          generate.STAGES.index(x) if x in generate.STAGES else 0)),
            "topic": r.get("topic") or [],
            "temporal": r.get("temporal", "static"), "valid_as_of": freeze_date,
            "answerable": any(lab["grade"] >= 2 for lab in labels.values()),
            "popularity": popularity(labels), "origin": r.get("origin", "synthetic"),
            "source_url": r.get("source_url") or r.get("seed_url"),
            "source_author": r.get("source_author"), "source_author_url": r.get("source_author_url"),
            "license": r.get("license", "CC-BY-SA-4.0"), "creation": creation,
            "human_verified": False}


# --------------------------------------------------------------------------- entry point

def family(model_id: str) -> str:
    """'qwen/qwen3.8-flash' -> 'qwen/qwen'; 'google/gemma-4-31b-it' -> 'google/gemma'."""
    org, _, name = model_id.partition("/")
    m = re.match(r"[a-z]+", name.lower())
    return f"{org}/{m.group(0) if m else name}"


JSON_PARAMS = {"response_format", "structured_outputs"}


def _json_ok(m: dict) -> bool:
    return bool(JSON_PARAMS & set(m.get("supported_parameters") or []))


def _is_variant(model_id: str) -> bool:
    """OpenRouter variants (`:free`, `:batch`, `:flex`, `:online`, `:thinking`, ...) change
    rate limits, routing, endpoints or behaviour: a batch-only id can't even take chat requests.
    They're used only when named explicitly, never as a stand-in for another model."""
    return ":" in model_id


def candidates(wanted: str, served: list[dict]) -> list[str]:
    """The wanted model if it is served and accepts a JSON schema, then the other models of
    its family that do (plain ids only, no variants), cheapest first."""
    by_id = {m.get("id"): m for m in served}
    out = [wanted] if wanted in by_id and _json_ok(by_id[wanted]) else []
    same = [m for m in served if family(m.get("id", "")) == family(wanted) and m.get("id") != wanted
            and not _is_variant(m.get("id", "")) and _json_ok(m)]
    same.sort(key=lambda m: float((m.get("pricing") or {}).get("prompt") or 1e9))
    return out + [m["id"] for m in same]


def resolve_models(wanted: list[str], served: list[dict]) -> tuple[list[str], list[str]]:
    """First candidate per wanted model (see `candidates`). Returns (models, notes)."""
    notes, out = [], []
    for w in wanted:
        cands = candidates(w, served)
        if not cands:
            out.append(w)
            notes.append(f"{w} is not usable here and no {family(w)} model with JSON output is served")
        else:
            out.append(cands[0])
            if cands[0] != w:
                notes.append(f"{w} is not usable here; using {cands[0]} (same family, cheapest)")
    return out, notes


def request_overrides(model: dict | None, extra_body: dict) -> dict:
    """Drop request fields from YTBRAIN_LLM_EXTRA_BODY that this model doesn't accept.
    With OpenRouter's `require_parameters`, one unsupported field (typically `reasoning`
    on a non-reasoning model) leaves no endpoint and every call fails with HTTP 404."""
    params = set((model or {}).get("supported_parameters") or [])
    if model and "reasoning" in extra_body and not ({"reasoning", "include_reasoning"} & params):
        return {"reasoning": None}
    return {}


PROBE_QUERY = "How should I price my first product?"
PROBE_PASSAGE = "we raised our prices every quarter until some customers complained"


PROBE_ATTEMPTS = 4            # a malformed answer is asked again: 2, 4, 8 s apart (+-20 % jitter)
PROBE_BACKOFF_S = 2.0


def _probe(env: Env, model: str, sleep=time.sleep) -> str | None:
    """A tiny typed call (a single-passage grade): None once the model answers correctly.

    Transport problems (429, 5xx, timeouts) are already retried with backoff inside the call,
    and a struggling API stops the preflight (RetriesExhausted). What's retried here is a
    *bad answer* (invalid JSON, a skipped passage): OpenRouter may route one request to a
    provider that answers badly, and one such answer mustn't disqualify the model. The bar is
    unchanged -- the model must still return a correct grade -- and a refused request (404
    route, 400 bad parameter, bad key) is never retried: that model can't be used as asked."""
    import random
    last = "no valid answer"
    for attempt in range(PROBE_ATTEMPTS):
        try:
            ans = env.ask(judge.render(PROBE_QUERY, [("P1", PROBE_PASSAGE)]), judge.Scores, model)
        except runner.RetriesExhausted:
            raise                                     # the API is struggling, not this model
        except Exception as e:                        # 404 routing, 400 bad request, ...
            return f"{type(e).__name__}: {str(e)[:160]}"
        if ans.value is None:
            last = ans.error or "no valid answer"
        else:
            try:
                judge.parse(ans.value, {"P1": "probe"})
                if attempt:
                    _say(f"  note: {model} answered the check correctly on try {attempt + 1} "
                         f"(an earlier answer was malformed)")
                return None
            except ValueError as e:
                last = str(e)
        if attempt + 1 < PROBE_ATTEMPTS:
            sleep(PROBE_BACKOFF_S * 2 ** attempt * random.uniform(0.8, 1.2))
    return f"{last} ({PROBE_ATTEMPTS} tries)"


HOST_FAMILY = "anthropic/claude"     # the coach's answers come from Claude: never its own judge


def preflight(env: Env, generator: bool = True) -> str | None:
    """Before spending anything, make sure every model the build needs works *with the
    request it will actually receive*: listed, accepts a JSON schema, and answers a tiny
    typed probe. A model that fails is replaced by the next of its family; the judges
    must come from families other than the generator's and from each other.

    generator=False (the coach eval): only the judges are checked -- the question generator
    isn't used -- and they must not be Claude, whose answers they grade."""
    if runner.LLM_BACKEND != "openai":
        return "eval builds need an OpenAI-compatible endpoint (YTBRAIN_LLM_BACKEND=openai, e.g. OpenRouter)"
    runner.set_max_rpm(EVAL_MAX_RPM)                  # probes are paced like the build
    if len(env.judges) < 2:
        return "YTBRAIN_EVAL_JUDGES needs at least two judge models (a third breaks ties)"
    import os
    import httpx
    from . import llm
    base = os.environ.get("YTBRAIN_LLM_BASE_URL", "")
    extra = json.loads(os.environ.get("YTBRAIN_LLM_EXTRA_BODY") or "{}")
    try:
        r = httpx.get(f"{base}/models", timeout=30,
                      headers={"Authorization": f"Bearer {os.environ.get('YTBRAIN_LLM_API_KEY', '')}"})
        served = r.json().get("data", []) or []
    except Exception:
        served = []                                   # can't list: probe what is configured
    by_id = {m.get("id"): m for m in served}
    chosen = []
    wanted = [env.generator, *env.judges] if generator else list(env.judges)
    for want in wanted:
        tries = candidates(want, served)[:3] if served else [want]
        picked, why = None, []
        for model in tries:
            llm.MODEL_OVERRIDES[model] = request_overrides(by_id.get(model), extra)
            try:
                err = _probe(env, model)
            except runner.RetriesExhausted as e:
                return (f"the API kept failing while checking {model} ({e}); it's rate-limiting "
                        f"or down, not this model. Nothing was spent on the build -- try again "
                        f"in a few minutes")
            if err is None:
                picked = model
                break
            why.append(f"{model}: {err}")
        if picked is None:
            detail = "; ".join(why) or "not listed, or no JSON-schema support"
            return (f"no working model for {want} ({detail}). Set YTBRAIN_EVAL_GENERATOR / "
                    f"YTBRAIN_EVAL_JUDGES; check candidates with `python ops/probe_models.py <id>`")
        if picked != want:
            _say(f"  note: {want} doesn't work here; using {picked} (same family)")
        if llm.MODEL_OVERRIDES.get(picked):
            _say(f"  note: {picked}: not sending {', '.join(llm.MODEL_OVERRIDES[picked])} "
                 "(the model doesn't accept it)")
        chosen.append(picked)
    if generator:
        env.generator, env.judges = chosen[0], chosen[1:]
    else:
        env.judges = chosen
    fams = [family(m) for m in env.judges]
    if len(set(fams)) != len(fams):
        return f"judges must come from different model families; got {', '.join(env.judges)}"
    if generator and family(env.generator) in fams:
        return (f"judges must come from different model families than the generator "
                f"({env.generator}); got {', '.join(env.judges)}")
    if not generator and HOST_FAMILY in fams:
        return f"a Claude model can't judge the coach's (Claude's) answers; got {', '.join(env.judges)}"
    return None


def build_dev(env: Env, max_cost: float | None = None, limit: int | None = None,
              talk_info: dict | None = None, freeze_date: str = "") -> int:
    runner.set_max_rpm(EVAL_MAX_RPM)
    budget = Budget(max_cost if max_cost is not None else env.max_cost, env.db.spent("dev"))
    _say(f"eval build dev: generator {env.generator}; judges {', '.join(env.judges)}; "
         f"{env.workers} workers; spent so far ${budget.spent:.3f} of ${budget.limit:g}")
    seed_dev(env, limit)
    for step in (lambda: generate_dev(env, budget), lambda: rewrites(env, "dev", budget)):
        stopped = step()
        if stopped:
            return _stopped(stopped, budget)
    pool_step(env, "dev")
    stopped = judge_step(env, "dev", budget)
    if stopped:
        return _stopped(stopped, budget)
    res = finalize_dev(env, talk_info or {}, freeze_date)
    _say(f"eval build dev: {res['accepted']} questions accepted, {res['discarded']} discarded "
         f"(seed not relevant), {res['pending']} incomplete; spent ${budget.spent:.3f}"
         + (f"; released as v{res['version']}" if res["changed"] else ""))
    if res["blocked"]:
        _say(f"  {res['blocked']} released question(s) still have ungraded Moments: the released files "
             f"are unchanged until they're graded (re-run to finish)")
        return EXIT_STOPPED
    if res["pending"]:
        _say("  re-run `ytbrain eval build --set dev` to finish the incomplete ones")
    if res["accepted"] < EVAL_TUNING_SIZE and not res["pending"] and not limit:
        _say(f"  note: fewer than {EVAL_TUNING_SIZE} questions survived; raise "
             "EVAL_TUNING_OVERSAMPLE or add seeds")
    return 0


def judge_runs(env: Env, split: str, run_paths: dict, depth: int = 10, max_cost: float | None = None,
               talk_info: dict | None = None, freeze_date: str = "") -> int:
    """Pool extension (TREC practice for systems built after the pool): grade the top-`depth`
    Moments each saved run retrieved that no judge has seen, with the same judges and prompt,
    then re-release the labels as a new minor version. Resumable and spend-capped like a build."""
    from .run import load_run
    if split != "dev":
        _say("eval judge: only the dev split exists so far")
        return 1
    runner.set_max_rpm(EVAL_MAX_RPM)
    budget = Budget(max_cost if max_cost is not None else env.max_cost, env.db.spent(split))
    accepted = env.db.questions(split, ("accepted",))
    if not accepted:
        _say("eval judge: no accepted questions -- build the split first")
        return 1
    added = 0
    for config, path in run_paths.items():
        moments, _talks = load_run(path)
        for q in accepted:
            ranked = [(m, r) for r, m in enumerate(moments.get(q["qid"], [])[:depth], 1)]
            added += env.db.add_to_pool(q["qid"], ranked, f"run:{config}")
    ver = judge.PROMPT_VERSION
    todo = sum(1 for q in accepted for m in env.db.pool(q["qid"])
               if len(env.db.grades(q["qid"], ver).get(m, {})) < 2)
    _say(f"eval judge: {added} Moment(s) newly pooled from {', '.join(run_paths)} (top {depth}); "
         f"{todo} still to grade · judges {', '.join(env.judges)} · spent so far ${budget.spent:.3f} of ${budget.limit:g}")
    stopped = judge_step(env, split, budget, statuses=("accepted",))
    if stopped:
        return _stopped(stopped, budget)
    undecided = [q["qid"] for q in accepted if decided_labels(env, q["qid"])[1]]
    if undecided:
        # finalizing now would drop these questions from the set: finish grading first
        _say(f"eval judge: {len(undecided)} question(s) still have ungraded Moments (failed calls); "
             f"re-run the same command to finish -- the released files are unchanged")
        return EXIT_STOPPED
    # finalize compares with what's released (not this run's additions): a resumed run that
    # finishes grades an interrupted run pooled still changes the labels, and bumps
    res = finalize_dev(env, talk_info or {}, freeze_date)
    changed = res["changed"]
    n_labels = sum(len(v) for v in files.load_split(env.root, split)[1].values())
    _say(f"eval judge: labels {'released as v' + res['version'] if changed else 'unchanged'} "
         f"({res['accepted']} questions, {n_labels} labels); spent ${budget.spent:.3f}")
    if changed:
        _say(f"  scores against the old labels are stale: `ytbrain eval rescore --set {split} --config full "
             f"--save-baseline` (no new search), then `ytbrain eval rescore --set {split} --config <c> --compare full` "
             f"for the others")
    return 0


def _stopped(why: str, budget: Budget) -> int:
    if why == "budget":
        _say(f"eval build: stopped at the spend cap (${budget.spent:.3f} of ${budget.limit:g}). "
             "Re-run with a higher --max-cost to continue; nothing is lost.")
    else:
        _say(f"eval build: stopped -- {why}. Fix it and re-run; finished work is kept.")
    return EXIT_STOPPED
