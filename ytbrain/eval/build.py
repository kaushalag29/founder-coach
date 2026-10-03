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

from ..config import (
    EVAL_DATA,
    EVAL_DIR,
    EVAL_GENERATOR,
    EVAL_JUDGE_BATCH,
    EVAL_JUDGES,
    EVAL_MAX_COST,
    EVAL_MAX_RPM,
    EVAL_PRIVATE,
    EVAL_TUNING_OVERSAMPLE,
    EVAL_WORKERS,
    TRANSCRIPTS,
)
from ..extract import runner
from ..visibility import Visibility
from . import files, generate, judge, pool, splits
from .db import EvalDB
from .llm import Budget, ask, run_parallel
from .moments import moment_locator, moment_text, parse_moment_id

EXIT_STOPPED = 2          # stopped early (budget / endpoint); re-run to resume


class Env:
    """Everything a build needs, created once; tests pass fakes."""

    def __init__(self, store=None, embed=None, reranker=None, ask_fn=ask, db: EvalDB | None = None,
                 root=EVAL_DIR, generator=EVAL_GENERATOR, judges=None, workers=EVAL_WORKERS,
                 max_cost=EVAL_MAX_COST, transcripts=TRANSCRIPTS, manifest=None, visibility=None,
                 private=None):
        self.store, self.embed, self.reranker, self.ask = store, embed, reranker, ask_fn
        # which Documents are private (ADR-0014): their labels go to the overlay, never the release
        self.visibility = visibility or Visibility.everything_public()
        self.private = private if private is not None else EVAL_PRIVATE
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
        return moment_text(list(self.utterances(doc)), start, locator=moment_locator(mid))


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

# The Tuning set is one question set per kind of Source the questions are written from (eval/splits.py),
# each built, decided and versioned on its own: `dev` from Talks, `dev-articles` from Articles,
# `dev-chapters` from public Books (released in eval/), `dev-private` from your private Sources (the
# private overlay only, ADR-0014). Every question is graded over every Source you index, so a talk
# question can be answered by a book page; `eval run --set all` scores the sets together.
PRIVATE_SPLIT = splits.PRIVATE_SPLIT
SPLITS = splits.SPEC
PUBLIC_SPLITS = splits.PUBLIC_SPLITS
LIVE = ("seeded", "generated", "accepted", "spare")          # a question that holds (or will hold) a slot


def _seed_rows(env: Env) -> list[dict]:
    rows = env.store.rows(kinds=["advice", "takeaway"],
                          columns=["item_id", "kind", "doc_id", "text", "evidence", "start_ms",
                                   "topics", "stages", "year", "speaker", "title", "visibility",
                                   "deep_link"])
    for r in rows:                     # the Source's configuration decides, whatever the index says
        if env.visibility.is_private(r["doc_id"]):
            r["visibility"] = "private"
    return rows


def split_target(env: Env, split: str) -> int:
    """The split's size as last computed by seeding (`targets` table), else what it was built with
    (a split seeded before targets existed keeps every question it has)."""
    t = env.db.target(split)
    if t is not None:
        return t["target"]
    return splits.LEGACY_SIZE.get(split, sum(q["status"] in LIVE for q in env.db.questions(split)))


def seed_dev(env: Env, limit: int | None = None, split: str = "dev", top_up: bool = False) -> int:
    """Step 1a: sample seeds. The first build samples the whole split; later builds reuse it (the
    sample is part of the set) unless `top_up`, which samples only the questions still missing to
    reach the split's target -- from Documents no question of the split was written from, the ones
    added since the last seeding first."""
    existing = env.db.questions(split)
    if existing and not top_up:
        return 0
    spec = SPLITS[split]
    eligible = generate.eligible(_seed_rows(env), private=spec["private"], kinds=spec["kinds"])
    docs = sorted({r["doc_id"] for r in eligible})
    have = sum(q["status"] in ("accepted", "spare") for q in existing)
    known = env.db.target(split)
    goal = limit or splits.target(len(docs), max(have, split_target(env, split) if existing else 0))
    env.db.set_target(split, goal, docs)
    live = sum(q["status"] in LIVE for q in existing)
    missing = goal - live
    if missing <= 0:
        if top_up:
            _say(f"  {split}: {live} question(s) for {len(docs)} Document(s): at its target ({goal})")
        return 0
    used = {q["record"].get("doc_id") for q in existing}
    fresh = [r for r in eligible if r["doc_id"] not in used]
    seen = set(known["docs"]) if known else set()
    caption = {}
    if env.manifest is not None:
        caption = {d["doc_id"]: d["caption_kind"] for d in env.manifest.documents()}
    n = missing if limit else math.ceil(missing * EVAL_TUNING_OVERSAMPLE)
    picks = generate.sample_items([r for r in fresh if r["doc_id"] not in seen], n, caption,
                                  private=spec["private"], kinds=spec["kinds"], seed=42 + len(existing))
    if len(picks) < n:                 # then any Document not used yet
        taken = {r["doc_id"] for r in picks}
        picks += generate.sample_items([r for r in fresh if r["doc_id"] not in taken], n - len(picks), caption,
                                       private=spec["private"], kinds=spec["kinds"], seed=43 + len(existing))
    start = max((q["sort_key"] for q in existing), default=0)
    for i, row in enumerate(picks, start + 1):
        qid = f"{spec['prefix']}-{i:04d}"
        seed = generate.seed_record(row, qid)
        env.db.upsert_question(qid, split, i, "seeded", seed, seed_moment=seed["seed_moment"])
    what = "private Sources only" if spec["private"] else f"{', '.join(spec['kinds'])} only"
    _say(f"  questions: sampled {len(picks)} seeds ({what}) for {missing} missing of {goal} "
         f"({len(docs)} Document(s), {len(fresh)} not used yet)")
    return len(picks)


def generate_dev(env: Env, budget: Budget, split: str = "dev") -> str | None:
    """Step 1b: write a question for every seed not yet written."""
    todo = env.db.questions(split, ("seeded",))
    if not todo:
        return None
    _say(f"  questions: generating {len(todo)} with {env.generator}")
    accepted_vecs = [q["record"]["_vec"] for s in SPLITS for q in env.db.questions(s, ("generated", "accepted"))
                     if q["record"].get("_vec")]

    def work(q):
        return env.ask(generate.prompt_for(q["record"]), generate.GeneratedQuestion, env.generator)

    def on_result(q, ans, err):
        _spend(env, budget, split, env.generator, "generate", ans.cost if ans is not None else _paid(err))
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
        env.db.upsert_question(q["qid"], split, q["sort_key"], "generated" if ok else "rejected",
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
    depth = splits.pool_depth(split)
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


def judge_step(env: Env, split: str, budget: Budget, statuses=("generated",),
               questions: list[dict] | None = None) -> str | None:
    """Grade every pooled Moment with the first two judges, then tie-break where needed.
    `questions` (from several splits, e.g. a pool extension) overrides the split's own;
    spend is booked to `split` either way."""
    ver = judge.PROMPT_VERSION
    qs = [q for q in (questions if questions is not None else env.db.questions(split, statuses))
          if env.db.is_pooled(q["qid"])]
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


def _decide(env: Env, split: str) -> dict:
    """Decide one split's questions: keep those whose seed the judges found relevant, up to the
    split's size; a question already released keeps its slot."""
    kept, discarded, pending, blocked, retired = [], 0, 0, [], 0
    labels_by_q: dict[str, dict] = {}
    gone = removed_documents(env)
    size = split_target(env, split)
    qs = env.db.questions(split, ("generated", "accepted", "discarded", "spare"))
    qs.sort(key=lambda q: (q["status"] != "accepted", q["sort_key"]))   # released ones keep their slot
    for q in qs:
        if q["status"] != "discarded" and q["seed_moment"] and parse_moment_id(q["seed_moment"])[0] in gone:
            # its seed Document was removed: the "its source answers it" guarantee is gone
            env.db.set_status(q["qid"], "retired", "its seed Document was removed")
            retired += 1
            continue
        labels, undecided = decided_labels(env, q["qid"])
        labels = {m: lab for m, lab in labels.items() if parse_moment_id(m)[0] not in gone}
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
        if len(kept) >= size:
            env.db.set_status(q["qid"], "spare")
            continue
        env.db.set_status(q["qid"], "accepted")
        kept.append(q)
        labels_by_q[q["qid"]] = labels
    return {"kept": kept, "labels": labels_by_q, "discarded": discarded, "pending": pending,
            "blocked": blocked, "retired": retired, "target": size}


def removed_documents(env: Env) -> set[str]:
    """Documents removed from their Source (tombstoned): their questions retire, their labels go."""
    if env.manifest is None or not hasattr(env.manifest, "tombstoned_ids"):
        return set()
    return env.manifest.tombstoned_ids()


def finalize_dev(env: Env, talk_info: dict[str, dict], freeze_date: str,
                 bump_if_changed: bool = True) -> dict:
    """Decide every question of every Tuning split and write the released files once. A
    question already released keeps its slot, and if any of them is still being graded nothing
    is written (releasing now would silently drop it). When any public split's questions or
    labels change -- or the last release was interrupted -- the new files go out under the next
    minor version (one bump for all splits), so no crash leaves new labels under an old version.
    Returns totals plus `splits`: {split: {accepted, discarded, pending, blocked, changed}}."""
    decided = {s: _decide(env, s) for s in SPLITS}
    per = {s: {"accepted": len(d["kept"]), "discarded": d["discarded"], "pending": d["pending"],
               "blocked": len(d["blocked"]), "retired": d["retired"], "target": d["target"], "changed": False}
           for s, d in decided.items()}
    blocked = sum(len(decided[s]["blocked"]) for s in SPLITS)
    out = {"accepted": per["dev"]["accepted"], "discarded": per["dev"]["discarded"],
           "pending": per["dev"]["pending"], "blocked": blocked, "changed": False,
           "version": files.current_version(env.root), "splits": per}
    if blocked:
        return out                     # a released question isn't fully graded: keep the old release
    # One benchmark, two homes (ADR-0014): labels on private Documents go to the overlay
    hidden: dict[str, dict] = {}
    writes = []
    for split in PUBLIC_SPLITS:
        d = decided[split]
        public, private = split_by_visibility(d["labels"], env.visibility)
        hidden[split] = private
        records = [query_record(q, public[q["qid"]], freeze_date, split) for q in d["kept"]]
        if not records:
            continue
        old_q, old_labels = files.load_split(env.root, split)
        new_labels = {q: {m: lab["grade"] for m, lab in ms.items()} for q, ms in public.items()}
        changed = (new_labels != old_labels or sorted(old_q, key=lambda r: r["_id"]) != records
                   or not files.consistent(env.root))
        released = bool(old_q)
        per[split]["changed"] = changed and released
        if changed or not released:
            writes.append((split, records, public))
    version = None
    if bump_if_changed and any(per[s]["changed"] for s in PUBLIC_SPLITS):
        version = files.bump_minor(files.current_version(env.root))
    for split, records, public in writes:
        files.write_split(env.root, split, records, public, talk_info, version=version,
                          visibility=env.visibility)
    out.update(changed=any(per[s]["changed"] for s in PUBLIC_SPLITS), version=files.current_version(env.root))
    out.update(release_overlay(env, hidden, decided[PRIVATE_SPLIT], freeze_date))
    return out


def release_overlay(env: Env, hidden: dict[str, dict], private: dict, freeze_date: str) -> dict:
    """Write the private overlay, per split: labels on private Documents for each public split's
    questions (`hidden`), and the questions written from private Sources (`dev-private`) with all
    their labels (every Source). Never released."""
    changed = False
    n_labels = 0
    for split in PUBLIC_SPLITS:
        labels = hidden.get(split, {})
        n_labels += sum(len(v) for v in labels.values())
        changed |= files.write_private(env.private, split, labels, [])
    labels = private["labels"]
    records = [{**query_record(q, labels[q["qid"]], freeze_date, PRIVATE_SPLIT), "private": True}
               for q in private["kept"]]
    n_labels += sum(len(v) for v in labels.values())
    changed |= files.write_private(env.private, PRIVATE_SPLIT, labels, records)
    return {"private_labels": n_labels, "private_questions": len(private["kept"]),
            "private_changed": changed}


def split_by_visibility(labels: dict[str, dict[str, dict]], visibility) -> tuple[dict, dict]:
    """({qid: public labels}, {qid: private labels}): every question keeps a (maybe empty) public set."""
    public: dict[str, dict] = {}
    private: dict[str, dict] = {}
    for q, ms in labels.items():
        public[q] = {m: lab for m, lab in ms.items() if not visibility.is_private_moment(m)}
        hidden = {m: lab for m, lab in ms.items() if visibility.is_private_moment(m)}
        if hidden:
            private[q] = hidden
    return public, private


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


def build_all(env: Env, max_cost: float | None = None, talk_info: dict | None = None,
              freeze_date: str = "", top_up: bool = False) -> int:
    """Build, resume or (`top_up`) grow every Tuning split in turn; a split with no Documents to
    write from is skipped. Each split keeps its own spend cap."""
    worst = 0
    for split in splits.TUNING_SPLITS:
        if SPLITS[split]["private"] and not env.visibility.private_docs:
            continue
        code = build_dev(env, max_cost=max_cost, talk_info=talk_info, freeze_date=freeze_date, split=split,
                         top_up=top_up, quiet_if_empty=True)
        worst = max(worst, code)
        if code and getattr(env, "stopped_by", None) not in (None, "budget"):
            return code                # the endpoint refused: every later split would stop the same way
    return worst


def build_dev(env: Env, max_cost: float | None = None, limit: int | None = None,
              talk_info: dict | None = None, freeze_date: str = "", split: str = "dev",
              top_up: bool = False, quiet_if_empty: bool = False) -> int:
    """Build or resume one Tuning split; `top_up` first grows it to its target."""
    runner.set_max_rpm(EVAL_MAX_RPM)
    budget = Budget(max_cost if max_cost is not None else env.max_cost, env.db.spent(split))
    seed_dev(env, limit, split, top_up=top_up)
    if not env.db.questions(split):
        if not quiet_if_empty:
            _say(f"eval build {split}: no Documents of this kind to write questions from")
        return 0
    _say(f"eval build {split}: generator {env.generator}; judges {', '.join(env.judges)}; "
         f"{env.workers} workers; spent so far ${budget.spent:.3f} of ${budget.limit:g}")
    for step in (lambda: generate_dev(env, budget, split), lambda: rewrites(env, split, budget)):
        stopped = step()
        if stopped:
            return _stopped(stopped, budget, split, env)
    pool_step(env, split)
    stopped = judge_step(env, split, budget)
    if stopped:
        return _stopped(stopped, budget, split, env)
    res = finalize_dev(env, talk_info or {}, freeze_date)
    mine = res["splits"][split]
    _say(f"eval build {split}: {mine['accepted']} questions accepted, {mine['discarded']} discarded "
         f"(seed not relevant), {mine['pending']} incomplete; spent ${budget.spent:.3f}"
         + (f"; released as v{res['version']}" if mine["changed"] else "")
         + ("; private overlay only, never released" if SPLITS[split]["private"] else ""))
    if res.get("private_questions") or res.get("private_labels"):
        _say(f"  private overlay: {res.get('private_questions', 0)} question(s) from private Sources, "
             f"{res.get('private_labels', 0)} private label(s) ({env.private}; never released)")
    if res["blocked"]:
        _say(f"  {res['blocked']} released question(s) still have ungraded Moments: the released files "
             f"are unchanged until they're graded (re-run to finish)")
        return EXIT_STOPPED
    if mine["pending"]:
        _say(f"  re-run `ytbrain eval build --set {split}` to finish the incomplete ones")
    else:
        for line in next_steps(env, split, res):
            _say(line)
    if mine["retired"]:
        _say(f"  {mine['retired']} question(s) retired: their seed Document was removed")
    if mine["accepted"] < mine["target"] and not mine["pending"] and not limit:
        _say(f"  {mine['accepted']} of {mine['target']} questions survived: `ytbrain eval build --set {split} "
             f"--top-up` writes more")
    return 0


def next_steps(env: Env, split: str, res: dict) -> list[str]:
    """What to run after a finished build: new questions only count once a run has searched them
    and the judge has graded what they retrieve. A build also says when some split was never
    built, so a split name is never guessed."""
    per = res.get("splits") or {}
    out = []
    if per.get(split, {}).get("accepted"):
        out += ["  next: `ytbrain eval run --config full`, then `ytbrain eval judge --config full`",
                "        (`--set all` is the default: every Tuning split, one row per source kind)"]
    elif split == PRIVATE_SPLIT:
        out.append("  no question survived from the private Sources; check `ytbrain eval status`")
    never = [s for s in splits.TUNING_SPLITS if s != split and env.db.target(s) is None
             and (not SPLITS[s]["private"] or env.visibility.private_docs)]
    if never:
        out.append(f"  never built: {', '.join(never)} -- `ytbrain eval build` builds every split "
                   f"(a kind with no Documents is skipped)")
    return out


def judge_runs(env: Env, split: str, run_paths: dict, depth: int = 10, max_cost: float | None = None,
               talk_info: dict | None = None, freeze_date: str = "") -> int:
    """Pool extension (TREC practice for systems built after the pool): grade the top-`depth`
    Moments each saved run retrieved that no judge has seen, with the same judges and prompt,
    then re-release the labels as a new minor version. Resumable and spend-capped like a build."""
    from .run import load_run
    if split not in ("all", *SPLITS):
        _say(f"eval judge: no {split} questions to judge yet (the Tuning splits: {', '.join(SPLITS)})")
        return 1
    runner.set_max_rpm(EVAL_MAX_RPM)
    budget = Budget(max_cost if max_cost is not None else env.max_cost, env.db.spent(split))
    splits = list(SPLITS) if split == "all" else [split]
    accepted = [q for s in splits for q in env.db.questions(s, ("accepted",))]
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
    stopped = judge_step(env, split, budget, questions=accepted)
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
    n_labels = sum(len(v) for s in PUBLIC_SPLITS for v in files.load_split(env.root, s)[1].values())
    _say(f"eval judge: labels {'released as v' + res['version'] if changed else 'unchanged'} "
         f"({sum(res['splits'][s]['accepted'] for s in PUBLIC_SPLITS)} released questions, {n_labels} labels); "
         f"spent ${budget.spent:.3f}")
    if res.get("private_labels"):
        _say(f"  private overlay: {res['private_labels']} label(s), {res.get('private_questions', 0)} private question(s) "
             f"({'updated' if res.get('private_changed') else 'unchanged'}; {env.private}, never released)")
    if changed or res.get("private_changed"):
        _say(f"  scores against the old labels are stale: `ytbrain eval rescore --set {split} --config full "
             f"--save-baseline` (no new search), then `ytbrain eval rescore --set {split} --config <c> --compare full` "
             f"for the others")
    return 0


def _stopped(why: str, budget: Budget, split: str = "", env: Env | None = None) -> int:
    from .. import runstatus
    name = f"eval build {split}".strip()
    runstatus.record("budget" if why == "budget" else runstatus.classify(why) if runstatus.classify(why) != "unknown"
                     else "endpoint", why)
    if env is not None:
        env.stopped_by = why
    if why == "budget":
        _say(f"{name}: stopped at the spend cap (${budget.spent:.3f} of ${budget.limit:g}). "
             "Re-run with a higher --max-cost to continue; nothing is lost.")
    else:
        _say(f"{name}: stopped -- {why}. Fix it and re-run; finished work is kept.")
    return EXIT_STOPPED
