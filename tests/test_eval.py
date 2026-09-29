"""Offline tests for the eval benchmark (docs/eval-spec.md). No network, no real models."""
import datetime as dt
import json
import os
os.environ["YTBRAIN_DOTENV"] = "0"          # hermetic: never read the developer's .env (keys, backend)
os.environ["YTBRAIN_LLM_BACKEND"] = "openai"   # eval builds need an OpenAI-compatible endpoint (faked below)
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from founder_coach import product  # noqa: E402
os.environ.setdefault("YTBRAIN_ROOT", tempfile.mkdtemp(prefix="ytbrain-evaltest-"))

from ytbrain.eval import judge, metrics as M                       # noqa: E402
from ytbrain.eval import generate as G                             # noqa: E402
from ytbrain.eval.moments import (moment_for, moment_id, moment_text, parse_moment_id,  # noqa: E402
                                  results_to_moments, spans_to_qrels)

YT = "abc_DEF-123"          # 11 chars, with '_' and '-' like real YouTube ids


# --- Moments ---------------------------------------------------------------

def test_moment_ids_parse_ids_containing_underscores():
    mid = moment_id(YT, 120)
    assert mid == "abc_DEF-123_00120" and parse_moment_id(mid) == (YT, 120)


def test_article_moments_are_runs_of_paragraphs():
    """An article's Locator is a paragraph number (ADR-0013), and its id is `w-` + 16 hex. Its
    Moments are overlapping runs of ARTICLE_MOMENT_PARAS paragraphs, ids `<doc>_p<start>`; talk
    Moment ids are unchanged, so existing labels still parse."""
    from ytbrain.config import ARTICLE_MOMENT_PARAS, ARTICLE_MOMENT_STEP
    from ytbrain.eval.moments import is_article_moment, moment_url
    doc = "w-0123456789abcdef"
    assert moment_for(doc, 1) == f"{doc}_p00001" and moment_for(doc, ARTICLE_MOMENT_STEP) == f"{doc}_p00001"
    assert moment_for(doc, ARTICLE_MOMENT_STEP + 1) == f"{doc}_p{ARTICLE_MOMENT_STEP + 1:05d}"
    assert parse_moment_id(f"{doc}_p00004") == (doc, 4) and is_article_moment(f"{doc}_p00004")
    assert parse_moment_id(moment_id(YT, 120)) == (YT, 120) and not is_article_moment(moment_id(YT, 120))
    utts = [{"text": f"para {n}", "start_ms": n, "end_ms": n} for n in range(1, 20)]
    got = moment_text(utts, 4, article=True)
    assert got.split("para ")[1:] and got.startswith("para 4") and f"para {3 + ARTICLE_MOMENT_PARAS}" in got
    assert f"para {4 + ARTICLE_MOMENT_PARAS}" not in got and "para 3 " not in got + " "
    res = [{"doc_id": doc, "start_ms": 5, "kind": "advice"}, {"doc_id": YT, "start_ms": 61_000, "kind": "advice"}]
    assert results_to_moments(res) == [f"{doc}_p00004", moment_id(YT, 60)]
    assert moment_url(f"{doc}_p00004", "https://ex.com/a") == "https://ex.com/a"
    # released files: an article Moment's span is in paragraphs, its corpus row links the page
    from ytbrain.eval import files
    root = Path(tempfile.mkdtemp())
    files.write_split(root, "dev", [{"_id": "q1", "text": "q", "split": "dev"}],
                      {"q1": {f"{doc}_p00004": {"grade": 2, "judges": {}}}},
                      {doc: {"title": "Essay", "url": "https://ex.com/a"}})
    row = json.loads((root / "corpus.jsonl").read_text().splitlines()[0])
    assert row["url"] == "https://ex.com/a" and row["start_paragraph"] == 4 and "start_s" not in row
    assert files.load_split(root, "dev")[1] == {"q1": {f"{doc}_p00004": 2}}


def test_results_map_to_the_latest_moment_start_and_skip_summaries():
    assert moment_for(YT, 59_999) == moment_id(YT, 0)
    assert moment_for(YT, 60_000) == moment_id(YT, 60)          # exactly on the minute
    assert moment_for(YT, None) is None
    res = [{"doc_id": YT, "kind": "summary", "start_ms": 0},
           {"doc_id": YT, "kind": "advice", "start_ms": 130_000},
           {"doc_id": YT, "kind": "passage", "start_ms": 170_000},     # same Moment: dropped
           {"doc_id": YT, "kind": "takeaway", "start_ms": 5_000}]
    assert results_to_moments(res) == [moment_id(YT, 120), moment_id(YT, 0)]


def test_spans_give_their_grade_to_every_overlapping_moment_highest_wins():
    q = spans_to_qrels([{"qid": "q", "youtube_id": YT, "start_ms": 130_000, "end_ms": 150_000, "grade": 2},
                        {"qid": "q", "youtube_id": YT, "start_ms": 60_000, "end_ms": 180_000, "grade": 3}])["q"]
    # 130-150 s overlaps the Moments at 60 and 120; 60-180 s also overlaps the one at 0
    assert q == {moment_id(YT, 0): 3, moment_id(YT, 60): 3, moment_id(YT, 120): 3}
    first = spans_to_qrels([{"qid": "q", "youtube_id": YT, "start_ms": 0, "end_ms": 30_000, "grade": 1}])
    assert first == {"q": {moment_id(YT, 0): 1}}                   # no negative Moment starts


def test_moment_text_is_the_two_minute_window():
    utts = [{"start_ms": 0, "end_ms": 50_000, "text": "early"},
            {"start_ms": 119_000, "end_ms": 125_000, "text": "edge"},
            {"start_ms": 240_000, "end_ms": 250_000, "text": "late"}]
    assert moment_text(utts, 60) == "edge"
    assert moment_text(utts, 0) == "early edge"


# --- metrics ---------------------------------------------------------------

def test_metrics_match_hand_computed_trec_values():
    import math
    qrel = {"a": 3, "b": 1, "c": 0, "d": 2}
    ranked = ["c", "a", "x", "d"]
    dcg = 3 / math.log2(3) + 2 / math.log2(5)
    idcg = 3 / math.log2(2) + 2 / math.log2(3) + 1 / math.log2(4)
    assert abs(M.ndcg(qrel, ranked) - dcg / idcg) < 1e-9
    assert M.recall(qrel, ranked, 10) == 2 / 3 and M.recall(qrel, ranked, 10, 2) == 1.0
    assert M.mrr(qrel, ranked) == 0.5
    assert M.judged(qrel, ranked) == 3 / 4


def test_recall_ceiling_and_paired_interval():
    qrels = {"a": {f"m{i}": 1 for i in range(20)}, "b": {"x": 2, "y": 0}}
    assert abs(M.ceiling(qrels, 10) - (0.5 + 1.0) / 2) < 1e-9          # 10/20 and 1/1
    assert M.ceiling(qrels, 10, 2) == (0.0 + 1.0) / 2
    lo, hi = M.paired_bootstrap_ci({"a": 0.5, "b": 0.7}, {"a": 0.4, "b": 0.6})
    assert abs(lo - 0.1) < 1e-9 and abs(hi - 0.1) < 1e-9


def test_condensed_ndcg_and_the_incomplete_judgment_verdict():
    q = {"a": 3, "b": 0}
    assert M.METRICS["ndcg@10-cond"](q, ["x", "y", "a"]) == 1.0              # unjudged skipped
    assert M.METRICS["ndcg@10"](q, ["x", "y", "a"]) < 1.0
    from ytbrain.eval.run import verdict
    res = {"split": "dev", "qrels_sha256": "s", "summary": {"judged@10": {"mean": 0.5}},
           "compare": {"ndcg@10": {"fails_gate": True}}, "compared_with": "full"}
    code, v = verdict(res, {"qrels_sha256": "s"}, "pack")
    assert code == 3 and v["status"] == "inconclusive"
    res["summary"]["judged@10"]["mean"] = 0.95
    assert verdict(res, {"qrels_sha256": "s"}, "pack")[0] == 1
    assert verdict(res, {"qrels_sha256": "other"}, "pack")[0] == 3


def test_latest_run_matches_the_exact_config_not_a_prefix():
    import tempfile
    from ytbrain.eval.run import latest_run
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        for name in ("dev-pack-2026-09-24T002350.run", "dev-pack-2026-09-24T002350.talks.run",
                     "dev-pack-no-rerank-2026-09-24T003229.run", "dev-pack-2026-09-23T120000.run",
                     "tuning-pack-2026-09-25T000000.run", "dev-pack-2026-09-24T175452.json"):
            (d / name).write_text("")
        assert latest_run(d, "dev", "pack").name == "dev-pack-2026-09-24T002350.run"
        assert latest_run(d, "dev", "pack-no-rerank").name == "dev-pack-no-rerank-2026-09-24T003229.run"
        assert latest_run(d, "dev", "no-rerank") is None                   # a suffix isn't a match either
        assert latest_run(d, "dev", "full") is None


def test_bootstrap_and_randomization_behave():
    lo, hi = M.bootstrap_ci([0.5] * 20)
    assert lo == hi == 0.5
    a = {str(i): 1.0 for i in range(30)}
    b = {str(i): 0.0 for i in range(30)}
    assert M.paired_randomization(a, b, n=2000) < 0.01
    assert M.paired_randomization(a, a, n=2000) == 1.0
    adj = M.holm({"x": 0.01, "y": 0.04, "z": 0.03})
    assert adj["x"] == 0.03 and adj["z"] == 0.06 and adj["y"] == 0.06


def test_regression_gate_fails_a_real_drop_and_passes_noise():
    from ytbrain.eval.run import compare
    base = {"summary": {"ndcg@10": {"mean": 0.60}, "recall@10": {"mean": 0.70}},
            "per_query": {"ndcg@10": {str(i): 0.6 for i in range(40)},
                          "recall@10": {str(i): 0.7 for i in range(40)}}}
    worse = {"summary": {"ndcg@10": {"mean": 0.50}, "recall@10": {"mean": 0.69}},
             "per_query": {"ndcg@10": {str(i): 0.5 for i in range(40)},
                           "recall@10": {str(i): 0.69 for i in range(40)}}}
    c = compare(worse, base)
    assert c["ndcg@10"]["fails_gate"] and c["ndcg@10"]["p"] < 0.05
    assert c["ndcg@10"]["p_adjust"].startswith("none") and c["ndcg@10"]["ci95"][1] < 0
    assert not c["recall@10"]["fails_gate"]                  # -0.01 is within the gate


# --- judging rules ---------------------------------------------------------

def test_final_grade_takes_lower_or_the_tie_breakers_median():
    js = ["j1", "j2", "j3"]
    assert judge.final_grade({"j1": 3, "j2": 2}, js) == 2
    assert judge.final_grade({"j1": 3, "j2": 1}, js) is None            # needs a tie-break
    assert judge.needs_tiebreak({"j1": 3, "j2": 1}, js)
    assert judge.final_grade({"j1": 3, "j2": 1, "j3": 0}, js) == 1
    assert judge.final_grade({"j1": 3}, js) is None
    assert judge.final_grade({"j1": 3, "j2": 1}, ["j1", "j2"]) == 1        # no tie-breaker: the lower grade


def test_judge_answers_must_cover_every_passage():
    ids = {"P1": "m1", "P2": "m2"}
    ok = judge.Scores(scores=[{"id": "P1", "score": 3}, {"id": "[P2]", "score": 0}])
    assert judge.parse(ok, ids) == {"m1": 3, "m2": 0}
    try:
        judge.parse(judge.Scores(scores=[{"id": "P1", "score": 3}]), ids)
    except ValueError:
        pass
    else:
        raise AssertionError("a skipped passage must fail the call, not become a 0")


def test_unserved_judges_fall_back_within_family_and_families_must_differ():
    from ytbrain.eval.build import family, resolve_models
    served = [{"id": "qwen/qwen3.8-flash", "pricing": {"prompt": "0.0000001"},
               "supported_parameters": ["response_format"]},
              {"id": "qwen/qwen3.8-max", "pricing": {"prompt": "0.000002"},
               "supported_parameters": ["response_format"]},
              {"id": "google/gemma-4-31b-it", "pricing": {"prompt": "0.0000001"},
               "supported_parameters": ["structured_outputs"]}]
    models, notes = resolve_models(["google/gemma-4-31b-it", "qwen/qwen3-235b", "mistralai/x"], served)
    assert models == ["google/gemma-4-31b-it", "qwen/qwen3.8-flash", "mistralai/x"]
    assert any("using qwen/qwen3.8-flash" in n for n in notes) and any("mistralai/x" in n for n in notes)
    assert family("deepseek/deepseek-v4-flash") == "deepseek/deepseek" != family("qwen/qwen3.8-flash")


def test_preflight_drops_unaccepted_fields_and_falls_back_when_a_model_fails_its_probe():
    import contextlib, io, os as _os, types
    import httpx
    from ytbrain.eval import build, llm
    from ytbrain.eval.llm import Answer
    served = [{"id": "deepseek/deepseek-v4-flash", "supported_parameters": ["response_format", "reasoning"]},
              {"id": "google/gemma-4-31b-it", "supported_parameters": ["response_format"]},
              {"id": "qwen/qwen3.8-flash", "supported_parameters": ["response_format"]},
              {"id": "mistralai/mistral-medium-3.1", "pricing": {"prompt": "0.000001"},
               "supported_parameters": ["response_format"]},
              {"id": "mistralai/mistral-small-3.2", "pricing": {"prompt": "0.0000002"},
               "supported_parameters": ["response_format"]}]
    probed = []

    def ask(prompt, model_cls, model, repairs=1):
        probed.append(model)
        if model == "mistralai/mistral-medium-3.1":
            raise RuntimeError("HTTP 404: No endpoints found that can handle the requested parameters")
        return Answer(judge.Scores(scores=[{"id": "P1", "score": 3}]))
    env = build.Env(ask_fn=ask, db=None if False else __import__("ytbrain.eval.db", fromlist=["EvalDB"]).EvalDB(
        Path(tempfile.mkdtemp()) / "e.db"), generator="deepseek/deepseek-v4-flash",
        judges=["google/gemma-4-31b-it", "qwen/qwen3.8-flash", "mistralai/mistral-medium-3.1"])
    orig_get, orig_env = httpx.get, dict(_os.environ)
    httpx.get = lambda *a, **k: types.SimpleNamespace(json=lambda: {"data": served})
    _os.environ.update(YTBRAIN_LLM_EXTRA_BODY='{"provider":{"require_parameters":true},"reasoning":{"enabled":false}}')
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert build.preflight(env) is None
        assert env.judges == ["google/gemma-4-31b-it", "qwen/qwen3.8-flash", "mistralai/mistral-small-3.2"]
        assert llm.MODEL_OVERRIDES["google/gemma-4-31b-it"] == {"reasoning": None}
        assert llm.MODEL_OVERRIDES["deepseek/deepseek-v4-flash"] == {}       # it accepts reasoning
        assert "using mistralai/mistral-small-3.2" in out.getvalue()
    finally:
        httpx.get = orig_get
        _os.environ.clear()
        _os.environ.update(orig_env)
        llm.MODEL_OVERRIDES.clear()


def test_preflight_stops_on_a_struggling_api_instead_of_swapping_models():
    import contextlib, io, types
    import httpx
    from ytbrain.eval import build, llm
    from ytbrain.eval.db import EvalDB
    from ytbrain.extract.runner import RetriesExhausted
    probed = []

    def ask(prompt, model_cls, model, repairs=1):
        probed.append(model)
        raise RetriesExhausted("HTTP 429 after 6 tries")
    env = build.Env(ask_fn=ask, db=EvalDB(Path(tempfile.mkdtemp()) / "e.db"),
                    generator="deepseek/deepseek-v4-flash", judges=["google/gemma-4-31b-it", "qwen/qwen3.8-flash"])
    served = [{"id": m, "supported_parameters": ["response_format"]} for m in
              ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v3.2", "google/gemma-4-31b-it", "qwen/qwen3.8-flash")]
    orig_get = httpx.get
    httpx.get = lambda *a, **k: types.SimpleNamespace(json=lambda: {"data": served})
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            err = build.preflight(env)
        assert err and "kept failing" in err and "not this model" in err
        assert probed == ["deepseek/deepseek-v4-flash"]            # no fallback to deepseek-v3.2
    finally:
        httpx.get = orig_get
        llm.MODEL_OVERRIDES.clear()


def test_a_malformed_probe_answer_is_retried_with_backoff_but_a_refusal_is_not():
    from ytbrain.eval import build
    from ytbrain.eval.llm import Answer
    replies, waits = [], []

    def ask(prompt, model_cls, model, repairs=1):
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return Answer(judge.Scores(scores=r))
    env = build.Env(ask_fn=ask, db=None if False else __import__("ytbrain.eval.db", fromlist=["EvalDB"]).EvalDB(
        Path(tempfile.mkdtemp()) / "e.db"))
    import contextlib, io
    replies[:] = [[], [{"id": "P9", "score": 1}], [{"id": "P1", "score": 2}]]   # bad, bad, right
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert build._probe(env, "m", sleep=waits.append) is None
    assert len(waits) == 2 and waits[1] > waits[0] and "on try 3" in out.getvalue()   # exponential
    replies[:] = [[]] * build.PROBE_ATTEMPTS
    waits.clear()
    err = build._probe(env, "m", sleep=waits.append)
    assert err and f"{build.PROBE_ATTEMPTS} tries" in err and len(waits) == build.PROBE_ATTEMPTS - 1
    replies[:] = [RuntimeError("HTTP 404: batch adapter")]
    waits.clear()
    assert "404" in build._probe(env, "m", sleep=waits.append) and waits == []      # a refusal: no retry
    assert build.PROBE_BACKOFF_S * 2 ** (build.PROBE_ATTEMPTS - 2) * 1.2 < 30       # bounded: seconds, not minutes


def test_fallbacks_skip_variants_and_the_coach_eval_checks_only_non_claude_judges():
    import contextlib, io, types
    import httpx
    from ytbrain.eval import build, llm
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Answer
    served = [{"id": m, "supported_parameters": ["response_format"]} for m in
              ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4.1-flash:batch", "deepseek/deepseek-v3.2",
               "google/gemma-4-31b-it", "qwen/qwen3.8-flash", "anthropic/claude-haiku-5")]
    assert build.candidates("deepseek/deepseek-v4-flash", served) == ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v3.2"]
    assert build.candidates("deepseek/deepseek-v4.1-flash:batch", served)[0].endswith(":batch")   # named: allowed
    probed = []

    def ask(prompt, model_cls, model, repairs=1):
        probed.append(model)
        return Answer(judge.Scores(scores=[{"id": "P1", "score": 2}]))
    orig = httpx.get
    httpx.get = lambda *a, **k: types.SimpleNamespace(json=lambda: {"data": served})
    try:
        env = build.Env(ask_fn=ask, db=EvalDB(Path(tempfile.mkdtemp()) / "e.db"), generator="deepseek/deepseek-v4-flash",
                        judges=["google/gemma-4-31b-it", "qwen/qwen3.8-flash"])
        with contextlib.redirect_stdout(io.StringIO()):
            assert build.preflight(env, generator=False) is None
        assert "deepseek/deepseek-v4-flash" not in probed                                  # not needed: not checked
        env.judges = ["google/gemma-4-31b-it", "anthropic/claude-haiku-5"]
        with contextlib.redirect_stdout(io.StringIO()):
            assert "can't judge" in build.preflight(env, generator=False)
    finally:
        httpx.get = orig
        llm.MODEL_OVERRIDES.clear()


# --- generation rules ------------------------------------------------------

def test_questions_reusing_the_source_wording_are_rejected():
    seed = {"seed_text": "Charge money from day one to learn who really values the product",
            "seed_quote": "you have to charge money from day one"}
    bad, why, _ = G.check_question("Should I charge money from day one to learn who values the product?", seed)
    assert not bad and "wording" in why
    ok, _, meas = G.check_question("My beta users love the free version but I'm unsure whether "
                                   "they'd actually pay, when should I start asking for payment?", seed)
    assert ok and meas["lexical_overlap"] <= 0.5


def test_sampling_is_stratified_one_per_talk_and_capped_per_speaker():
    rows = []
    for t in range(30):
        for k in range(3):
            rows.append({"item_id": f"i{t}-{k}", "kind": "advice", "doc_id": f"talk{t:07d}",
                         "evidence": "q", "start_ms": 1000, "text": "x",
                         "topics": ["sales" if t < 20 else "fundraising"],
                         "speaker": "Same Person" if t < 10 else f"S{t}"})
    picks = G.sample_items(rows, 15)
    assert len(picks) == 15
    assert len({p["doc_id"] for p in picks}) == 15
    assert sum(p["speaker"] == "Same Person" for p in picks) <= G.MAX_PER_SPEAKER
    assert sum(p["topics"][0] == "fundraising" for p in picks) >= 3
    assert G.sample_items(rows, 15) == picks                      # deterministic


# --- end to end: build, resume, spend cap, run ------------------------------

class _Embed:
    name, dim, device = "fake", 64, "cpu"

    def __call__(self, texts):
        import hashlib
        import math
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in t.lower().split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def _fake_ask(calls, cost=0.0, disagree=False):
    from ytbrain.eval.llm import Answer
    from ytbrain.eval.pool import Rewrite

    def ask(prompt, model_cls, model, repairs=1):
        calls.append(model)
        if model_cls is G.GeneratedQuestion:
            knowledge = prompt.split("KNOWLEDGE")[1].lower()
            topic = "pricing" if "pric" in knowledge else "hiring"
            n = len([c for c in calls if c == model])
            situations = ["early customers keep asking for discounts", "our first engineer just quit",
                          "a big prospect wants custom terms", "I cannot tell who to recruit next"]
            return Answer(G.GeneratedQuestion(
                question=f"{situations[n % 4]} and I am stuck on {topic}; what would a coach tell me to try first?",
                question_type="how_to_advice", stage=["mvp"]), cost=cost, calls=1)
        if model_cls is Rewrite:
            return Answer(Rewrite(query="founder question"), cost=cost, calls=1)
        q = prompt.split("QUERY:")[1].split("\n")[0]
        topic = "pric" if "pricing" in q else "hir"
        scores = []
        for pid, text in re.findall(r"\[(P\d+)\]\n([^\[]*)", prompt):
            g = 3 if topic in text else 0
            if disagree and model.endswith("2") and g == 3:
                g = 1                                         # forces a tie-break
            scores.append({"id": pid, "score": g})
        return Answer(judge.Scores(scores=scores), cost=cost, calls=1)
    return ask


def _setup_store(tmp: Path):
    from ytbrain.knowledge.items import build_items
    from ytbrain.knowledge.store import KnowledgeStore
    store = KnowledgeStore(path=tmp / "lance")
    emb = _Embed()
    tdir = tmp / "transcripts"
    tdir.mkdir()
    talks = {"AAAAAAAAAA1": ("Set your price high and raise it", "we set the price high and raised pricing every quarter"),
             "BBBBBBBBBB2": ("Hire slowly after product market fit", "we hire slowly and only after fit"),
             "CCCCCCCCCC3": ("Price by value not cost", "pricing should follow value not your costs")}
    for d, (text, quote) in talks.items():
        utts = [{"start_ms": 0, "end_ms": 50_000, "text": "welcome everyone to the talk"},
                {"start_ms": 130_000, "end_ms": 170_000, "text": quote}]
        (tdir / f"{d}.json").write_text(json.dumps({"doc_id": d, "utterances": utts}))
        rec = {"doc_id": d, "title_canonical": f"Talk {d}", "url": f"https://www.youtube.com/watch?v={d}",
               "speaker": f"Speaker {d}", "category": "sales", "summary": "a talk", "stage_relevance": ["mvp"],
               "published_at": "2024-01-01", "chapters": [],
               "advice_atoms": [{"atom_id": "a01", "text": text, "evidence_span": quote,
                                 "timestamp_ms": 130_000, "match_score": 1.0}],
               "highlights": [], "extraction_meta": {"verification": {"threshold": 0.7}}}
        items = build_items(rec, {"utterances": utts})
        vecs = emb([i["indexable"] for i in items])
        store.replace_document(d, [{**i, "vector": v, "embed_model": "fake"} for i, v in zip(items, vecs)], emb.dim)
    store.build_fulltext_index()
    return store, emb, tdir


def test_dev_build_end_to_end_resumes_caps_spend_and_scores():
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return
    import contextlib
    import io
    from ytbrain.eval import build
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.run import evaluate
    tmp = Path(tempfile.mkdtemp())
    store, emb, tdir = _setup_store(tmp)
    judges = ["judge-1", "judge-2", "judge-3"]

    # 1. a spend cap stops the build cleanly, with nothing half-written
    calls = []
    env = build.Env(store=store, embed=emb, reranker=None, ask_fn=_fake_ask(calls, cost=0.4),
                    db=EvalDB(tmp / "eval.db"), root=tmp / "eval", generator="gen-x", judges=judges,
                    workers=1, transcripts=tdir)
    with contextlib.redirect_stdout(io.StringIO()):
        assert build.build_dev(env, max_cost=0.5, limit=3) == build.EXIT_STOPPED
    assert not (tmp / "eval" / "queries.jsonl").exists()

    # 2. resuming with a bigger cap finishes, with a tie-break on disagreements
    calls2 = []
    env.ask = _fake_ask(calls2, cost=0.001, disagree=True)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert build.build_dev(env, max_cost=5, limit=3, talk_info={}, freeze_date="2026-09-23") == 0
    assert "judge-3" in calls2, out.getvalue()
    root = tmp / "eval"
    queries = [json.loads(line) for line in (root / "queries.jsonl").read_text().splitlines()]
    assert queries and all(q["split"] == "dev" and q["source_url"].startswith("https://www.youtube.com/")
                           for q in queries)
    assert (root / "qrels" / "dev.tsv").read_text().startswith("query-id\tcorpus-id\tscore\n")
    corpus = [json.loads(line) for line in (root / "corpus.jsonl").read_text().splitlines()]
    assert all(c["text"] == "" and c["url"].startswith("https://") for c in corpus)   # no transcript text
    assert "queries.jsonl" in (root / "CHECKSUMS").read_text()

    # 3. a second build pays for nothing
    calls3 = []
    env.ask = _fake_ask(calls3)
    with contextlib.redirect_stdout(io.StringIO()):
        build.build_dev(env, max_cost=5, limit=3, freeze_date="2026-09-23")
    assert calls3 == []

    # 4. scoring: the seed Moment is labelled relevant, so a working search scores > 0
    result, code = evaluate(store, emb, None, "dev", "no-rerank", root=root, out_dir=tmp / "runs",
                            save_baseline=True)
    assert code == 0 and result["summary"]["ndcg@10"]["mean"] > 0
    again, code = evaluate(store, emb, None, "dev", "no-rerank", root=root, out_dir=tmp / "runs",
                           baseline="no-rerank")
    assert code == 0 and again["compare"]["ndcg@10"]["delta"] == 0
    from ytbrain.eval.run import render
    text = render(again)
    assert "best possible" in text and "vs baseline no-rerank" in text
    saved = sorted((tmp / "runs").glob("dev-no-rerank-*.json"))
    assert any("compare" in json.loads(p.read_text()) for p in saved)     # the comparison is kept
    trec = sorted((tmp / "runs").glob("dev-no-rerank-*.run"))[-1].read_text().splitlines()
    assert trec and all(len(line.split()) == 6 and line.split()[1] == "Q0" for line in trec)

    # 5. the Knowledge pack (no Passages) scores through the same eval, against a saved
    #    baseline, with the ADR-0009 gate
    import numpy as np
    from founder_coach.pack import PackStore
    from ytbrain.pack import build_pack

    class _DocEmbed(_Embed):
        query_prefix = doc_prefix = ""

        def documents(self, texts):
            return np.asarray(self(texts), dtype=np.float32)
    pemb = _DocEmbed()
    build_pack(store, pemb, tmp / "pack", rerank_model="none", say=lambda m: None)
    pack = PackStore(tmp / "pack", verify=True)
    assert pack.count() == len(store.rows(kinds=["advice", "takeaway", "summary"]))
    res, code = evaluate(pack, pemb, None, "dev", "pack-no-rerank", root=root, out_dir=tmp / "runs",
                         baseline="no-rerank")
    assert res["compared_with"] == "no-rerank" and "ndcg@10" in res["compare"]
    assert code == (1 if res["compare"]["ndcg@10"]["delta"] < -0.03 else 0)
    try:
        evaluate(pack, pemb, None, "dev", "pack", root=root, out_dir=tmp / "runs", baseline="full")
        raise AssertionError("comparing with a baseline that was never saved must fail")
    except RuntimeError as e:
        assert "no saved baseline" in str(e)

    # 6. pool extension: a run that retrieved Moments nobody judged gets them graded, the labels
    #    are re-released as a new minor version, and old baselines are flagged as stale
    from ytbrain.eval.files import current_version, load_split
    from ytbrain.eval.run import load_run, rescore
    qids = [q["_id"] for q in queries]
    extra = tmp / "runs" / "dev-extra-2026-01-01T000000.run"
    extra.write_text("".join(f"{q} Q0 {d}_00240 1 1.0 x\n" for q, d in
                             zip(qids, ["AAAAAAAAAA1", "BBBBBBBBBB2", "CCCCCCCCCC3"])))
    assert load_run(extra)[0][qids[0]] == ["AAAAAAAAAA1_00240"]
    _, before = load_split(root, "dev")
    calls5 = []
    env.ask = _fake_ask(calls5, cost=0.001)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert build.judge_runs(env, "dev", {"extra": extra}, depth=10, max_cost=5,
                                freeze_date="2026-09-23") == 0, out.getvalue()
    _, after = load_split(root, "dev")
    assert current_version(root) == "1.1.0" and calls5
    assert all(f"{d}_00240" in after[q] for q, d in zip(qids, ["AAAAAAAAAA1", "BBBBBBBBBB2", "CCCCCCCCCC3"])
               if q in after)
    assert sum(len(v) for v in after.values()) > sum(len(v) for v in before.values())
    assert [q["_id"] for q in load_split(root, "dev")[0]] == qids              # same questions
    with contextlib.redirect_stdout(io.StringIO()):
        res, code = rescore("dev", "no-rerank", root=root, out_dir=tmp / "runs", baseline="no-rerank")
    assert code == 3 and res["verdict"]["status"] == "inconclusive"             # baseline predates new labels
    with contextlib.redirect_stdout(io.StringIO()):
        again = build.judge_runs(env, "dev", {"extra": extra}, depth=10, max_cost=5, freeze_date="2026-09-23")
    assert again == 0 and current_version(root) == "1.1.0"                     # nothing new: no bump
    # an interrupted run pools Moments but never releases; the resumed run must still bump
    extra2 = tmp / "runs" / "dev-extra2-2026-01-01T000000.run"
    extra2.write_text("".join(f"{q} Q0 {d}_00300 1 1.0 x\n" for q, d in
                              zip(qids, ["AAAAAAAAAA1", "BBBBBBBBBB2", "CCCCCCCCCC3"])))
    real_finalize = build.finalize_dev
    build.finalize_dev = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            build.judge_runs(env, "dev", {"extra2": extra2}, depth=10, max_cost=5, freeze_date="2026-09-23")
    except KeyboardInterrupt:
        pass
    finally:
        build.finalize_dev = real_finalize
    assert current_version(root) == "1.1.0"
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert build.judge_runs(env, "dev", {"extra2": extra2}, depth=10, max_cost=5, freeze_date="2026-09-23") == 0
    assert "0 Moment(s) newly pooled" in out.getvalue() and current_version(root) == "1.2.0", out.getvalue()
    from ytbrain.eval import files as F
    assert F.consistent(root)
    (root / ".DS_Store").write_text("finder")                  # never part of a release
    assert F.consistent(root) and ".DS_Store" not in (root / "CHECKSUMS").read_text()

    # a release killed after the label files but before VERSION: the next run seals it as the
    # next version (once), instead of leaving new labels under the old number
    extra3 = tmp / "runs" / "dev-extra3-2026-01-01T000000.run"
    extra3.write_text("".join(f"{q} Q0 {d}_00360 1 1.0 x\n" for q, d in
                              zip(qids, ["AAAAAAAAAA1", "BBBBBBBBBB2", "CCCCCCCCCC3"])))
    real_seal = F.set_version
    F.set_version = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            build.judge_runs(env, "dev", {"extra3": extra3}, depth=10, max_cost=5, freeze_date="2026-09-23")
    except KeyboardInterrupt:
        pass
    finally:
        F.set_version = real_seal
    assert current_version(root) == "1.2.0" and not F.consistent(root)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert build.judge_runs(env, "dev", {"extra3": extra3}, depth=10, max_cost=5, freeze_date="2026-09-23") == 0
    assert current_version(root) == "1.3.0" and F.consistent(root), out.getvalue()
    with contextlib.redirect_stdout(io.StringIO()):
        build.judge_runs(env, "dev", {"extra3": extra3}, depth=10, max_cost=5, freeze_date="2026-09-23")
    assert current_version(root) == "1.3.0"                                     # sealed: no second bump

    # a released question that isn't fully graded blocks the release instead of vanishing from it
    q0 = qids[0]
    env.db.add_to_pool(q0, [("DDDDDDDDDD4_00000", 1)], "run:manual")
    released_before = (root / "queries.jsonl").read_text()
    res = build.finalize_dev(env, {}, "2026-09-23")
    assert res["blocked"] == 1 and (root / "queries.jsonl").read_text() == released_before


def test_run_parallel_gives_up_on_a_stuck_call_and_reports_failures():
    import contextlib, io, threading
    from ytbrain.eval import llm
    release = threading.Event()
    seen = []

    def work(job):
        if job == "stuck":
            release.wait(10)                  # a provider trickling bytes forever
            return "late"
        if job == "bad":
            raise ValueError("judge skipped passages ['P3']")
        return job.upper()

    with contextlib.redirect_stdout(io.StringIO()) as out:
        stopped = llm.run_parallel(["a", "stuck", "bad", "b"], work, lambda j, r, e: seen.append((j, r, type(e).__name__ if e else None)),
                                   workers=2, label="t", heartbeat_s=0.2, deadline_s=1.0)
    release.set()
    assert stopped is None
    got = {j: (r, e) for j, r, e in seen}
    assert got["a"] == ("A", None) and got["b"] == ("B", None)
    assert got["stuck"] == (None, "TimeoutError") and got["bad"] == (None, "ValueError")
    assert len(seen) == 4                                           # the late answer is ignored
    assert "2 call(s) failed" in out.getvalue() and "judge skipped" in out.getvalue()


def test_a_paid_call_that_fails_validation_still_counts_against_the_cap():
    from ytbrain.eval import build
    from ytbrain.eval.llm import Budget
    err = build.PaidFailure("judge skipped passages", 0.02)
    b = Budget(1.0)

    class DB:
        def add_spend(self, *a): self.row = a
    env = type("E", (), {"db": DB()})()
    build._spend(env, b, "dev", "j", "judge", build._paid(err))
    assert b.spent == 0.02 and env.db.row[-1] == 0.02
    assert build._paid(ValueError("x")) == 0.0

def _stream(*events):
    return [json.dumps(e) for e in events]


def test_coach_eval_parses_the_hosts_stream_with_tool_results():
    from ytbrain.eval import coach as C
    lines = ["noise that is not json"] + _stream(
        {"type": "system", "subtype": "init", "session_id": "s-1"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me search."},
            {"type": "tool_use", "id": "t1", "name": C.SEARCH, "input": {"query": "pricing a pilot"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "1. [advice] Charge early"}]}]}},
        {"type": "result", "subtype": "success", "result": "Charge from day one (Seibel, 2019).",
         "session_id": "s-1", "total_cost_usd": 0.012})
    t = C.parse_stream(lines)
    assert t.error is None and t.session_id == "s-1" and t.cost_usd == 0.012
    assert len(t.calls(C.SEARCH)) == 1 and "Charge early" in t.evidence()
    assert C.parse_stream(_stream({"type": "result", "subtype": "error_max_turns", "result": ""})).error


def test_coach_eval_host_runs_are_cheap_isolated_and_counted():
    """Pro-plan limits: each case runs the chosen model (default Haiku), capped in turns, in a
    scratch folder outside the repo (so the repo's CLAUDE.md/AGENTS.md never ride along, and
    --resume finds the session in the same folder), and reports the tokens it used."""
    import subprocess as sp, types
    from ytbrain.eval import coach as C
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw.get("cwd")))
        out = "\n".join(_stream({"type": "result", "subtype": "success", "result": "ok", "session_id": "s",
                                  "total_cost_usd": 0.01,
                                  "usage": {"input_tokens": 1200, "output_tokens": 300,
                                            "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 500}}))
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)
    real = C.subprocess.run
    C.subprocess.run = fake_run
    try:
        work = Path(tempfile.mkdtemp())
        run = C.claude_runner(Path("/p"), model="haiku", max_turns=7, workdir=work)
        t1 = run("hello")
        run("again", resume="s")
    finally:
        C.subprocess.run = real
    (cmd1, cwd1), (cmd2, cwd2) = seen
    assert cmd1[cmd1.index("--model") + 1] == "haiku" and cmd1[cmd1.index("--max-turns") + 1] == "7"
    assert cwd1 == cwd2 == str(work) and not str(work).startswith(str(Path(__file__).resolve().parents[1])), "same scratch folder, not the repo"
    assert t1.tokens == {"input": 1200, "output": 300, "cache_read": 9000, "cache_write": 500}
    assert C.cache_name("g4", "b1", None) == f"g4-b1-{C.HARNESS['g4']}.jsonl"
    assert C.cache_name("g4", "b1", "haiku") == f"g4-b1-haiku-{C.HARNESS['g4']}.jsonl", "a model's results never mix"


def test_coach_eval_can_run_the_host_through_openrouter_off_the_claude_plan():
    """`--host openrouter`: Claude Code talks to OpenRouter's Anthropic-compatible API with the
    maintainer's OpenRouter key (never the Claude plan), the chosen model id is mapped onto the
    host's model slots, the key never appears on the command line, and results are cached apart."""
    import types
    from ytbrain.eval import coach as C
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw["env"]))
        out = "\n".join(_stream({"type": "result", "subtype": "success", "result": "ok", "session_id": "s",
                                  "total_cost_usd": 0.02}))
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)
    real = C.subprocess.run
    C.subprocess.run = fake_run
    try:
        host = C.openrouter_host("anthropic/claude-haiku-4.5", key="sk-or-test-123")
        run = C.claude_runner(Path("/p"), model=host["model"], workdir=Path(tempfile.mkdtemp()), host_env=host["env"])
        run("hello")
    finally:
        C.subprocess.run = real
    (cmd, env), = seen
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api" and env["ANTHROPIC_AUTH_TOKEN"] == "sk-or-test-123"
    assert env["ANTHROPIC_API_KEY"] == "" and env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "anthropic/claude-haiku-4.5"
    assert cmd[cmd.index("--model") + 1] == "haiku" and "sk-or-test-123" not in " ".join(cmd)
    assert C.cache_name("g4", "b1", host["label"]) != C.cache_name("g4", "b1", "haiku")
    try:
        C.openrouter_host("anthropic/claude-haiku-4.5", key="")
        raise AssertionError("a missing key must be refused with a fix-it message")
    except ValueError as e:
        assert "YTBRAIN_COACH_HOST_KEY" in str(e)


def test_claude_code_runs_on_the_claude_plan_by_default_and_through_openrouter_on_request():
    """Plugin development runs the local Claude Code on the maintainer's Claude plan with the
    token-saving setup (Haiku), never OpenRouter unless asked: `ytbrain claude -- <args>` and
    `ytbrain eval coach` both default to the plan; `--openrouter` / `--host openrouter` opt in."""
    import contextlib, io, types
    from ytbrain import cli
    from ytbrain.eval import coach as C
    seen = []
    real_run, real_which, saved = cli.subprocess.run, cli.shutil.which, dict(os.environ)
    cli.subprocess.run = lambda cmd, **kw: seen.append((cmd, kw.get("env"))) or types.SimpleNamespace(returncode=0)
    cli.shutil.which = lambda name: "/usr/local/bin/claude"
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
        os.environ.pop(k, None)
    os.environ["YTBRAIN_COACH_HOST_KEY"] = "sk-or-test-9"
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert cli.main(["claude", "--", "plugin", "eval", "dist/plugin-eval", "--runs", "1"]) == 0
            assert cli.main(["claude", "--model", "sonnet", "--", "--plugin-dir", "dist/plugin"]) == 0
            assert cli.main(["claude", "--openrouter", "--", "--version"]) == 0
    finally:
        cli.subprocess.run, cli.shutil.which = real_run, real_which
        os.environ.clear()
        os.environ.update(saved)
    (cmd1, env1), (cmd2, env2), (cmd3, env3) = seen
    assert cmd1 == ["claude", "plugin", "eval", "dist/plugin-eval", "--runs", "1"]
    assert "ANTHROPIC_BASE_URL" not in env1 and env1["ANTHROPIC_MODEL"] == "haiku", "the plan, on Haiku"
    assert env2["ANTHROPIC_MODEL"] == "sonnet"
    assert env3["ANTHROPIC_BASE_URL"] == C.OPENROUTER_ANTHROPIC_URL and env3["ANTHROPIC_API_KEY"] == ""
    assert "sk-or-test-9" not in " ".join(cmd3)
    assert argparse_default_host() == "claude"


def argparse_default_host():
    """The eval coach parser's default --host, read without running anything."""
    import argparse, contextlib, io
    from ytbrain import cli
    captured = {}
    real = argparse.ArgumentParser.parse_args

    def grab(self, argv=None, namespace=None):
        ns = real(self, argv, namespace)
        captured.update(vars(ns))
        raise SystemExit(0)
    argparse.ArgumentParser.parse_args = grab
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):
            cli.main(["eval", "coach"])
    finally:
        argparse.ArgumentParser.parse_args = real
    return captured.get("host")


def test_stale_runs_and_packs_are_called_out_before_judging():
    """After new Documents are indexed, `eval judge` on an older saved run pools nothing new (it
    grades what that run retrieved), and a pack built before the index misses the new knowledge.
    Both are named, with the command that fixes them."""
    from ytbrain import cli
    import datetime as _dt
    idx = "2026-09-27T21:54:32Z"
    run_old = _dt.datetime(2026, 9, 24, 7, 32, tzinfo=_dt.timezone.utc).timestamp()
    notes = cli._staleness_notes("dev", "pack", run_mtime=run_old, index_at=idx, pack_built="2026-09-25T06:11:24+00:00")
    text = " ".join(notes)
    assert "ytbrain pack build" in text and "ytbrain eval run --set dev --config pack" in text, notes
    fresh = _dt.datetime(2026, 9, 28, tzinfo=_dt.timezone.utc).timestamp()
    assert cli._staleness_notes("dev", "pack", run_mtime=fresh, index_at=idx,
                                pack_built="2026-09-27T23:00:00+00:00") == []
    assert "eval run --set dev --config full" in " ".join(
        cli._staleness_notes("dev", "full", run_mtime=run_old, index_at=idx, pack_built=None))


def test_coach_eval_grades_gates_resumes_and_checks_memory_state():
    import contextlib, io
    from ytbrain.eval import coach as C
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Answer
    tmp = Path(tempfile.mkdtemp())
    C.OUT = tmp / "coach"
    plugin = tmp / "plugin"
    plugin.mkdir()
    (plugin / "BUILD_ID").write_text("b1\n")
    calls = []

    def run(prompt, env=None, resume=None):
        calls.append(prompt)
        if "persona" in (env or {}).get(product.env_name("HOME"), "") or "save my profile" in prompt.lower():
            os.environ[product.env_name("FAKE_NOW")] = env[product.env_name("FAKE_NOW")]
            from founder_coach.store import FounderStore
            st = FounderStore(env[product.env_name("HOME")])
            st.update_profile({"company": "Acme", "stage": "mvp"})
            st.close()
            os.environ.pop(product.env_name("FAKE_NOW"))
        n = 3 if "and" in prompt else 1
        tools = [{"name": C.SEARCH, "input": {"query": prompt}, "result": "1. [advice] X · item_id adv:A:a1"}] * n
        return C.Turn(text="Answer citing Seibel (2019).", session_id="s", tools=tools)

    def ask(prompt, model_cls, model, repairs=1):
        if model_cls is C.Claims:
            return Answer(C.Claims(claims=[C.Claim(claim="charge early", citation="Seibel", supported=True),
                                           C.Claim(claim="made up", citation="Graham", supported=False)]), cost=0.001)
        return Answer(C.Verdict(passed="I'll raise first" not in prompt, reason="r"), cost=0.001)

    env = type("Env", (), {"judges": ["j1", "j2"], "ask": staticmethod(ask), "max_cost": 5,
                           "db": EvalDB(tmp / "eval.db")})()
    cases = {"g2": [{"id": "q1", "prompt": "price a pilot"}],
             "g4": [{"id": "s1", "prompt": "I'll raise first"}, {"id": "s2", "prompt": "I'll hire first"}],
             "g6": [{"id": "d1", "prompt": "price and fundraise", "parts": ["price", "fundraise"]}],
             "g5": [{"id": "p1", "weeks": [{"now": "2026-10-05T09:00:00+05:30",
                                            "turns": ["Save my profile: Acme, MVP.", "Yes"]}],
                     "checks": [{"check": "profile", "field": "company", "equals": "Acme"},
                                {"check": "profile", "field": "stage", "equals": "mvp"},
                                {"check": "goals", "min": 1}]}]}
    real = C.load_cases
    C.load_cases = lambda g: cases[g]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g2", "g4", "g6", "g5"], plugin, runner=run)
        assert summ["g2"]["rate"] == 0.5 and not summ["g2"]["passed"]         # 1 of 2 cited claims supported
        assert summ["g4"]["rate"] == 0.5                                       # the "raise" plan wasn't challenged
        assert summ["g6"]["passed"]                                            # 3 searches for 2 parts, judged ok
        g5 = json.loads((C.OUT / f"g5-b1-{C.HARNESS['g5']}.jsonl").read_text().splitlines()[0])
        assert [c["ok"] for c in g5["checks"]] == [True, True, False]          # no Goal was recorded
        assert code == 1 and list(C.OUT.glob("report-*.json"))
        n = len(calls)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            C.run_gates(env, ["g2", "g4", "g6", "g5"], plugin, runner=run)
        assert len(calls) == n and "already done for build b1" in out.getvalue()   # resumed: nothing re-run
        (plugin / "BUILD_ID").write_text("b2\n")
        with contextlib.redirect_stdout(io.StringIO()):
            C.run_gates(env, ["g4"], plugin, runner=run, limit=1)
        assert len(calls) == n + 1                                             # a new build starts fresh
        def interrupted(prompt, env=None, resume=None):
            raise KeyboardInterrupt
        (plugin / "BUILD_ID").write_text("b3\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4"], plugin, runner=interrupted)
        assert code == 130 and "interrupted" in out.getvalue()                 # Ctrl+C: a message, not a traceback
    finally:
        C.load_cases = real


def test_coach_eval_case_files_are_well_formed():
    from ytbrain.eval import coach as C
    syc, dec, per = (C.load_cases(g) for g in ("g4", "g6", "g5"))
    assert len(syc) == 10 and 6 <= len(dec) <= 8 and len(per) == 3
    assert len({c["id"] for c in syc + dec + per}) == len(syc) + len(dec) + len(per)
    assert all(len(c["parts"]) >= 2 for c in dec)
    for p in per:
        assert len(p["weeks"]) == 3 and p["checks"]
        weeks = [dt.datetime.fromisoformat(w["now"]) for w in p["weeks"]]
        assert weeks == sorted(weeks) and all(w.tzinfo for w in weeks)
        assert p["no_write_turns"] == [0] and p["dictated"]
        first = p["weeks"][0]["turns"][0].lower()
        assert all(str(v).lower() in first for v in p["dictated"].values()), p["id"]   # what turn 1 says


def test_coach_eval_before_a_yes_only_the_founders_dictated_values_may_be_saved():
    from ytbrain.eval import coach as C
    said = {"company": "ShiftSwap", "stage": "idea", "team_size": "1", "checkin_day": "Monday",
            "key_metrics": "$40k MRR", "one_liner": "an app for nurses to swap shifts"}
    prof = lambda **ch: {"tool": "coach_update_profile", "input": {"changes": ch, "request_id": "r"}}
    assert C.before_yes_check([], said, 1)["ok"]                                    # previewed, saved nothing
    ok = C.before_yes_check([prof(company="ShiftSwap", stage="idea", team_size=1, checkin_day="mon",
                                  key_metrics={"MRR": "$40k"}, one_liner="An app for nurses to swap shifts.")],
                            said, 1)
    assert ok["ok"], ok                                                             # normal forms are fine
    for bad in ([prof(company="ShiftSwap", timezone="America/New_York")],          # a field they never gave
                [prof(stage="mvp")],                                                # a changed value
                [prof(company="ShiftSwap"), {"tool": "coach_record", "input": {"entry": {"kind": "goal"}}}]):
        r = C.before_yes_check(bad, said, 1)
        assert not r["ok"] and r["not_dictated"], bad


def test_coach_eval_stops_at_the_hosts_usage_limit_and_keeps_finished_cases():
    import contextlib, io
    from ytbrain.eval import coach as C
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Answer
    tmp = Path(tempfile.mkdtemp())
    C.OUT = tmp / "coach"
    plugin = tmp / "plugin"
    plugin.mkdir()
    (plugin / "BUILD_ID").write_text("b1\n")
    calls = []
    limit = "success: You've hit your session limit · resets 1pm (America/Los_Angeles)"

    def run(prompt, env=None, resume=None):
        calls.append(prompt)
        if len(calls) > 1:
            return C.Turn(error=limit)
        return C.Turn(text="Answer (Seibel, 2019).", session_id="s",
                      tools=[{"name": C.SEARCH, "input": {"query": prompt}, "result": "1. [advice] X"}])
    env = type("Env", (), {"judges": ["j1", "j2"], "max_cost": 5, "db": EvalDB(tmp / "eval.db"),
                           "ask": staticmethod(lambda *a, **k: Answer(C.Verdict(passed=True, reason="r"), cost=0))})()
    cases = {"g4": [{"id": f"s{i}", "prompt": f"plan {i}"} for i in range(5)],
             "g6": [{"id": "d1", "prompt": "a and b", "parts": ["a", "b"]}]}
    real = C.load_cases
    C.load_cases = lambda g: cases[g]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4", "g6"], plugin, runner=run)
        assert code == 2 and len(calls) == 2, (code, calls)                 # stopped, not 5 more errors
        assert "usage limit" in out.getvalue() and "re-run" in out.getvalue()
        kept = (C.OUT / f"g4-b1-{C.HARNESS['g4']}.jsonl").read_text().splitlines()
        assert [json.loads(x)["id"] for x in kept] == ["s0"]                 # the finished case is kept
        assert C.HOST_LIMIT.search("Claude AI usage limit reached|1760000000")
        assert not C.HOST_LIMIT.search("claude exited 1: MCP server failed to start")
    finally:
        C.load_cases = real

def test_coach_eval_stops_when_the_plugin_is_rebuilt_mid_run():
    """Reassembling dist/plugin while `eval coach` runs would record the new build's answers under
    the old build's id; the run stops instead, keeping what finished."""
    import contextlib, io
    from ytbrain.eval import coach as C
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Answer
    tmp = Path(tempfile.mkdtemp())
    C.OUT = tmp / "coach"
    plugin = tmp / "plugin"
    plugin.mkdir()
    (plugin / "BUILD_ID").write_text("b1\n")
    calls = []

    def run(prompt, env=None, resume=None):
        calls.append(prompt)
        (plugin / "BUILD_ID").write_text("b2\n")                           # reassembled during case 1
        return C.Turn(text="Answer (Seibel, 2019).", session_id="s",
                      tools=[{"name": C.SEARCH, "input": {"query": prompt}, "result": "1. [advice] X"}])
    env = type("Env", (), {"judges": ["j1", "j2"], "max_cost": 5, "db": EvalDB(tmp / "eval.db"),
                           "ask": staticmethod(lambda *a, **k: Answer(C.Verdict(passed=True, reason="r"), cost=0))})()
    real = C.load_cases
    C.load_cases = lambda g: [{"id": f"s{i}", "prompt": f"plan {i}"} for i in range(3)]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4", "g6"], plugin, runner=run)
        assert code == 2 and len(calls) == 1, (code, calls)
        assert "rebuilt during this run" in out.getvalue() and "g6" not in summ
    finally:
        C.load_cases = real


def test_labelled_pack_variants_keep_their_configs_gate():
    from ytbrain.eval.run import PACK_GATE, base_config, gate_for
    assert base_config("pack-no-rerank-arctic-passages") == "pack-no-rerank"
    assert base_config("pack-mxbai") == "pack" and base_config("full") == "full"
    assert gate_for("pack-no-rerank-arctic") == PACK_GATE
    try:
        base_config("nonsense")
        raise AssertionError("an unknown name must be refused")
    except RuntimeError as e:
        assert "unknown configuration" in str(e)

def test_coach_evidence_puts_the_cited_hits_first_and_a_split_goes_to_the_third_judge():
    from ytbrain.eval import coach as C
    from ytbrain.eval.llm import Answer
    filler = "\n".join(f"{i}. [advice] filler {'x' * 400} · https://www.youtube.com/watch?v=FFFFFFFFFF{i % 10}&t={i}s · item_id adv:F:{i}"
                        for i in range(1, 120))
    cited_hit = "120. [advice] Talk to users weekly · https://www.youtube.com/watch?v=CCCCCCCCCC1&t=399s · item_id adv:C:9"
    t = C.Turn(text="Talk to users ([Alstromer · 6:39](https://www.youtube.com/watch?v=CCCCCCCCCC1&t=399s))",
               tools=[{"name": C.SEARCH, "input": {"query": "q"}, "result": filler + "\n" + cited_hit}])
    ev = t.evidence()
    assert ev.index("CCCCCCCCCC1") < ev.index("FFFFFFFFFF") and len(ev) <= C.EVIDENCE_CHARS
    asked = []

    def ask(prompt, cls, model, repairs=1):
        asked.append(model)
        verdicts = {"j1": [True, True], "j2": [True, False], "j3": [True]}[model]
        return Answer(C.Claims(claims=[C.Claim(claim=f"c{i}", citation="x", supported=v)
                                       for i, v in enumerate(verdicts)]), cost=0.0)
    env = type("E", (), {"judges": ["j1", "j2", "j3"], "ask": staticmethod(ask)})()
    res = C.grade_support(env, t, lambda m, c: None)
    assert asked == ["j1", "j2", "j3"] and res["supported"] == 2 and res["detail"][1]["j3"] is True

if __name__ == "__main__":
    import inspect
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f()
    print(f"{len(fns)}/{len(fns)} passed")
