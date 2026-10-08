"""Offline tests for the eval benchmark (docs/eval-spec.md). No network, no real models."""
import datetime as dt
import json
import os
os.environ["YTBRAIN_DOTENV"] = "0"          # hermetic: never read the developer's .env (keys, backend)
os.environ.setdefault("YTBRAIN_SOURCES_FILE", os.path.join(__import__("tempfile").mkdtemp(prefix="ytbrain-nosources-"), "sources.yaml"))   # hermetic: never read your sources.yaml (the file does not exist)
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from founder_coach import product  # noqa: E402
os.environ.setdefault("YTBRAIN_ROOT", tempfile.mkdtemp(prefix="ytbrain-evaltest-"))

from ytbrain.eval import judge, metrics as M                       # noqa: E402
from ytbrain.extract import runner as _runner                   # noqa: E402
# eval builds need an OpenAI-compatible endpoint (faked below). Set on the module that reads
# it, not in os.environ: config is read once, so another suite imported first would win.
_runner.LLM_BACKEND = "openai"
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


def test_private_moments_are_graded_but_released_only_to_the_private_overlay():
    """ADR-0014: privacy comes from the Source (Visibility), never from a Document's kind or id."""
    from ytbrain.eval import files
    from ytbrain.eval.build import split_by_visibility
    from ytbrain.eval.db import EvalDB
    from ytbrain.visibility import Visibility
    book, essay = "9780753550304__secrets_b00012", "w-abc_p00004"
    vis = Visibility.from_sources([{"doc_id": "9780753550304__secrets", "source_id": "books"},
                                   {"doc_id": YT, "source_id": "yc"}, {"doc_id": "w-abc", "source_id": "notes"}],
                                  [{"id": "books", "distribute": False}, {"id": "yc"}, {"id": "notes", "distribute": False}])
    assert vis.is_private_moment(book) and vis.is_private_moment(essay), "a private Source of any kind"
    assert not vis.is_private(YT) and vis.is_private("never-seen"), "fail closed on unknown Documents"
    db = EvalDB(Path(tempfile.mkdtemp()) / "e.db")
    db.save_pool("q1", {moment_id(YT, 60): (["full"], 1)}, 10)
    assert db.add_to_pool("q1", [(book, 1)], "run:pack") == 1, "private Moments are pooled and graded"
    labels = {"q1": {moment_id(YT, 60): {"grade": 2, "judges": {}}, book: {"grade": 3, "judges": {}}}}
    public, private = split_by_visibility(labels, vis)
    assert list(public["q1"]) == [moment_id(YT, 60)] and list(private["q1"]) == [book]
    root, overlay = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (root / "corpus.jsonl").write_text(json.dumps({"_id": book, "title": "Secrets", "text": ""}) + "\n")
    try:
        files.write_split(root, "dev", [{"_id": "q1", "text": "q", "split": "dev"}], labels, {}, visibility=vis)
        raise AssertionError("a private label must be refused by the release")
    except ValueError as e:
        assert "private Documents" in str(e)
    files.write_split(root, "dev", [{"_id": "q1", "text": "q", "split": "dev"}], public, {}, visibility=vis)
    assert "secrets" not in (root / "corpus.jsonl").read_text(), "an old private row leaves the corpus"
    assert files.write_private(overlay, "dev", private) and not files.write_private(overlay, "dev", private)
    assert files.load_split(root, "dev")[1] == {"q1": {moment_id(YT, 60): 2}}
    assert files.load_split(root, "dev", overlay)[1] == {"q1": {moment_id(YT, 60): 2, book: 3}}, \
        "one benchmark: the overlay adds the private labels when you score"


def test_questions_from_private_sources_join_the_benchmark_only_in_the_overlay():
    """One benchmark: a question written from a Book is scored with the released ones, graded over
    every Source, and lives only in the private overlay; the released files never see it."""
    from ytbrain.eval import build, files, judge
    from ytbrain.eval.db import EvalDB
    from ytbrain.visibility import Visibility
    book_doc = "9780753550304__secrets"
    book = f"{book_doc}_b00012"
    vis = Visibility.from_sources([{"doc_id": book_doc, "source_id": "books"}, {"doc_id": YT, "source_id": "yc"}],
                                  [{"id": "books", "distribute": False}, {"id": "yc"}])
    root, overlay = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    env = build.Env(ask_fn=None, db=EvalDB(Path(tempfile.mkdtemp()) / "e.db"), root=root, visibility=vis,
                    private=overlay, judges=["j1", "j2"])
    rec = {"question": "How do I find a secret?", "stage": [], "creation": {}}
    env.db.upsert_question("devp-0001", build.PRIVATE_SPLIT, 1, "generated", rec, text=rec["question"], seed_moment=book)
    env.db.save_pool("devp-0001", {book: (["seed"], 1), moment_id(YT, 60): (["hybrid"], 2)}, 20)
    env.db.save_grades("devp-0001", "j1", judge.PROMPT_VERSION, {book: 3, moment_id(YT, 60): 2})
    env.db.save_grades("devp-0001", "j2", judge.PROMPT_VERSION, {book: 3, moment_id(YT, 60): 1})
    # an older overlay kept private questions under `dev`: the release moves them to their own set
    files.write_private(overlay, "dev", {"devp-0001": {book: {"grade": 3, "judges": {}}}},
                        [{"_id": "devp-0001", "split": "dev", "private": True}])
    res = build.finalize_dev(env, {}, "2026-09-30")
    assert res["private_questions"] == 1 and res["private_changed"]
    assert res["splits"][build.PRIVATE_SPLIT]["accepted"] == 1
    queries, qrels = files.load_split(root, build.PRIVATE_SPLIT, overlay)
    assert [q["_id"] for q in queries] == ["devp-0001"] and queries[0]["private"] is True
    assert queries[0]["split"] == build.PRIVATE_SPLIT
    assert qrels["devp-0001"] == {book: 3, moment_id(YT, 60): 1}, "graded over every Source (lower grade wins)"
    assert files.load_split(root, "dev", overlay) == ([], {}), "the talk set no longer holds book questions"
    assert files.load_set(root, "all", overlay)[0] == queries, "`all` scores every set together"
    assert files.load_split(root, build.PRIVATE_SPLIT) == ([], {}), "never in the released files"
    assert not build.finalize_dev(env, {}, "2026-09-30")["private_changed"], "idempotent"


def test_document_level_ndcg_counts_every_source_kind():
    from ytbrain.eval.run import DOC_NDCG, doc_qrels, score
    q = doc_qrels({"q1": {"w-abcdefghijklm_p00004": 3, moment_id(YT, 60): 1, moment_id(YT, 120): 2}})
    assert q == {"q1": {"w-abcdefghijklm": 3, YT: 2}}, "ids are parsed, never cut at 11 characters"
    runs = {"q1": {"moments": ["w-abcdefghijklm_p00004"], "talks": ["w-abcdefghijklm", YT]}}
    r = score([{"_id": "q1"}], {"q1": {"w-abcdefghijklm_p00004": 3, moment_id(YT, 60): 1}}, runs)
    assert r["summary"][DOC_NDCG]["mean"] == 1.0, "an article ranked first is a hit at Document level"


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


def test_a_stale_baseline_names_the_command_that_refreshes_it():
    from ytbrain.eval.run import verdict
    res = {"split": "dev", "qrels_sha256": "new", "summary": {"judged@10": {"mean": 1.0}},
           "compare": {}, "compared_with": "full-prebooks"}
    named = {"qrels_sha256": "old", "rescored_from": "dev-full-2026-09-27T152650.run"}
    code, v = verdict(res, named, "full")
    assert code == 3
    assert ("`ytbrain eval rescore --set dev --config full --run dev-full-2026-09-27T152650.run "
            "--as full-prebooks --save-baseline`") in v["reasons"][0]
    res["compared_with"] = "full"
    _, v = verdict(res, {"qrels_sha256": "old"}, "full")
    assert "`ytbrain eval rescore --set dev --config full --save-baseline`" in v["reasons"][0]
    assert "eval run" not in v["reasons"][0], "a re-score needs no new search"


def test_build_and_interrupt_hints_name_the_command_to_run():
    from types import SimpleNamespace
    from ytbrain.cli import rerun_command
    from ytbrain.eval.build import next_steps
    from ytbrain.visibility import Visibility
    assert rerun_command(["eval", "build", "--set", "dev-private"]) == "ytbrain eval build --set dev-private"
    assert rerun_command(["eval", "run", "--config", "full", "--compare", "full-prebooks"]) == \
        "ytbrain eval run --config full --compare full-prebooks"
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.splits import TUNING_SPLITS
    db = EvalDB(Path(tempfile.mkdtemp()) / "e.db")
    private = SimpleNamespace(visibility=Visibility(private_docs={"9780307887917__x"}, known_docs=set()), db=db)
    for s in TUNING_SPLITS:
        if s != "dev-articles":
            db.set_target(s, 30, [])
    every = {"dev": {"accepted": 150}, "dev-articles": {"accepted": 0}, "dev-private": {"accepted": 21}}
    built = next_steps(private, "dev-private", {"splits": every})
    assert "eval run --config full" in built[0] and "eval judge --config full" in built[0]
    assert "--set dev " not in " ".join(built), "the default set (all) includes the new questions"
    assert "never built: dev-articles -- `ytbrain eval build`" in built[-1]
    db.set_target("dev-articles", 73, [])
    assert len(next_steps(private, "dev-private", {"splits": every})) == 2, "nothing left to build"
    none = {**every, "dev-private": {"accepted": 0}}
    assert "no question survived" in next_steps(private, "dev-private", {"splits": none})[0]
    fresh = EvalDB(Path(tempfile.mkdtemp()) / "e.db")
    fresh.set_target("dev", 150, [])
    assert not any("dev-private" in line for line in next_steps(SimpleNamespace(
        visibility=Visibility.everything_public(), db=fresh), "dev", {"splits": {"dev": {"accepted": 150}}})), \
        "no private Sources: no private set to build"


def test_every_source_kind_is_measured_and_gated_on_its_own_questions():
    from ytbrain.eval.run import compare_kinds, score, verdict
    assert M.kind_mix([["9780753550304__secrets_b00012", "abcdefghijk_00060"], ["w-x_p00001", "abcdefghijk_00120"]]) \
        == {"article": 0.25, "chapter": 0.25, "talk": 0.5}
    assert M.kind_mix([]) == {}
    qs = [{"_id": f"t{i}", "creation": {"seed_moment": moment_id(YT, 60)}} for i in range(12)] + \
         [{"_id": "b1", "creation": {"seed_moment": "9780753550304__secrets_b00012"}}]
    qrels = {q["_id"]: {moment_id(YT, 60): 3} for q in qs}
    good = {q["_id"]: {"moments": [moment_id(YT, 60)], "talks": []} for q in qs}
    bad = {q["_id"]: {"moments": ["x_00000"] * 3 + [moment_id(YT, 60)], "talks": []} for q in qs}
    base, cur = score(qs, qrels, good), score(qs, qrels, bad)
    assert cur["by"]["seed_kind"]["talk"]["n"] == 12 and cur["by"]["seed_kind"]["chapter"]["n"] == 1
    assert cur["summary"]["_mix@10"]["talk"] > 0 and "_mix@10" not in cur["per_query"]
    kinds = compare_kinds(cur, base)
    assert kinds["talk"]["fails_gate"] and kinds["talk"]["gated"], "12 talk questions dropped: gated"
    assert not kinds["chapter"]["gated"], "one question is too few to gate"
    res = {"split": "dev", "qrels_sha256": "s", "summary": {"judged@10": {"mean": 1.0}},
           "compare": {"ndcg@10": {"fails_gate": False}}, "compare_kinds": kinds, "compared_with": "full"}
    assert verdict(res, {"qrels_sha256": "s"}, "full")[0] == 1, "a source kind's drop fails the run"


def test_any_saved_run_can_become_a_named_baseline():
    from ytbrain.eval import files
    from ytbrain.eval.run import rescore, write_trec_run
    root, out = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    files.write_split(root, "dev", [{"_id": "q1", "text": "q", "split": "dev"}],
                      {"q1": {moment_id(YT, 60): {"grade": 3, "judges": {}},
                              moment_id(YT, 600): {"grade": 0, "judges": {}}}}, {})
    write_trec_run(out / "dev-full-2026-09-27T152650.run", {"q1": {"moments": [moment_id(YT, 60)],
                                                                     "scores": [1.0], "talks": [YT]}}, "old")
    write_trec_run(out / "dev-full-2026-09-29T195100.run", {"q1": {"moments": [moment_id(YT, 600)],
                                                                     "scores": [1.0], "talks": [YT]}}, "new")
    r, _ = rescore("dev", "full", root=root, out_dir=out, private=None, save_baseline=True,
                   run=Path("dev-full-2026-09-27T152650.run"), name="full-prebooks")
    assert r["config"] == "full-prebooks" and r["summary"]["ndcg@10"]["mean"] == 1.0
    assert (out / "baseline-dev-full-prebooks.json").exists() and not (out / "baseline-dev-full.json").exists()
    r, code = rescore("dev", "full", root=root, out_dir=out, private=None, baseline="full-prebooks")
    assert r["summary"]["ndcg@10"]["mean"] == 0.0 and code == 1, "the newest run regressed against it"


def test_questions_added_after_a_baseline_are_not_scored_as_zero():
    """A book question written after the pre-books run is outside the comparison, not a zero:
    otherwise every new question looks like a gain and hides a drop on the old ones."""
    from ytbrain.eval import files
    from ytbrain.eval.run import evaluate, render, rescore, write_trec_run
    root, out = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    good, bad = moment_id(YT, 60), moment_id(YT, 600)
    labels = {f"q{i}": {good: {"grade": 3, "judges": {}}, bad: {"grade": 0, "judges": {}}} for i in range(4)}
    files.write_split(root, "dev", [{"_id": f"q{i}", "text": "q", "split": "dev"} for i in range(4)], labels, {})
    # the old run was asked q0, q1 only (q2, q3 were written later); both found the good Moment
    write_trec_run(out / "dev-full-2026-09-27T152650.run",
                   {q: {"moments": [good], "scores": [1.0], "talks": [YT]} for q in ("q0", "q1")}, "old")
    r, _ = rescore("dev", "full", root=root, out_dir=out, private=None, save_baseline=True,
                   run=Path("dev-full-2026-09-27T152650.run"), name="prebooks")
    assert r["n_questions"] == 2 and r["summary"]["ndcg@10"]["mean"] == 1.0, "not asked is not zero"

    class Store:            # the new run: q0 now worse, q1 same, q2/q3 new and perfect
        def search(self, *a, **k):
            return []
    runs = {"q0": {"moments": [bad], "scores": [1.0], "talks": [YT]}}
    runs.update({q: {"moments": [good], "scores": [1.0], "talks": [YT]} for q in ("q1", "q2", "q3")})
    import ytbrain.eval.run as R
    real = R.run_config
    R.run_config = lambda *a, **k: runs
    try:
        res, _ = evaluate(Store(), None, None, "dev", "full", root=root, out_dir=out,
                          baseline="prebooks", private=None)
    finally:
        R.run_config = real
    assert res["overlap"] == {"shared": 2, "new": 2, "retired": 0, "new_by_kind": {"(none)": 2}}
    assert res["compare"]["ndcg@10"]["delta"] == -0.5, "a drop on the shared questions, not +0.25"
    text = render(res)
    assert "compared on the 2 question(s) both were asked; 2 new since the baseline" in text
    saved = sorted(out.glob("dev-full-*.asked"))
    assert saved and saved[-1].read_text().split() == ["q0", "q1", "q2", "q3"]


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
    # Prompt prices in dollars per token, as OpenRouter lists them (2026-10: flash $0.15/M, max $2/M, gemma ~$0.09/M).
    # The dearer model is listed first, so only a real sort by price picks flash (a stable sort keeps list order).
    served = [{"id": "qwen/qwen3.8-max", "pricing": {"prompt": "0.000002"},
               "supported_parameters": ["response_format"]},
              {"id": "qwen/qwen3.8-flash", "pricing": {"prompt": "0.00000015"},
               "supported_parameters": ["response_format"]},
              {"id": "google/gemma-4-31b-it", "pricing": {"prompt": "0.00000009"},
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


def test_each_tuning_split_is_seeded_from_its_own_kind_of_source():
    rows = [{"item_id": f"t{i}", "kind": "advice", "doc_id": f"talk{i:07d}", "evidence": "q", "start_ms": 1000,
             "text": "x", "topics": ["sales"], "speaker": f"S{i}"} for i in range(6)]
    rows += [{"item_id": f"a{i}", "kind": "advice", "doc_id": f"w-site{i:09d}", "evidence": "q", "start_ms": 4,
              "text": "x", "topics": ["sales"], "speaker": f"A{i}", "deep_link": f"https://site/{i}#p4"}
             for i in range(6)]
    arts = G.sample_items(rows, 4, kinds=("article",))
    assert len(arts) == 4 and all(r["doc_id"].startswith("w-") for r in arts)
    assert all(not r["doc_id"].startswith("w-") for r in G.sample_items(rows, 4, kinds=("talk",)))
    seed = G.seed_record(arts[0], "deva-0001")
    assert seed["seed_url"].startswith("https://site/"), "an article seed links to its page, not to YouTube"
    assert "taken from a startup essay or article" in G.prompt_for(seed)
    talk = G.seed_record(G.sample_items(rows, 1, kinds=("talk",))[0], "dev-0001")
    assert talk["seed_url"].startswith("https://www.youtube.com/watch?v=")
    assert "taken from a startup talk" in G.prompt_for(talk)
    book = {"seed_kind": "advice", "topic": [], "seed_text": "x", "doc_id": "9780753550304__secrets"}
    assert "taken from a chapter of a business book" in G.prompt_for(book) and "the book," in G.prompt_for(book)


def test_splits_grow_to_20_percent_of_their_documents_and_retire_questions_of_removed_ones():
    import contextlib
    import io
    from types import SimpleNamespace
    from ytbrain.eval import build, splits
    from ytbrain.eval.db import EvalDB
    from ytbrain.visibility import Visibility
    assert splits.target(721) == 144 and splits.target(362) == 72 and splits.target(181) == 36
    assert splits.target(721, 150) == 150, "released questions are never dropped to fit"
    assert splits.target(12) == 2 and splits.target(4) == 0 and splits.target(0) == 0, "at most 20%, no floor"
    assert {"dev", "dev-articles", "dev-chapters", "dev-private"} == set(splits.TUNING_SPLITS)

    def rows(n, start=0):
        return [{"item_id": f"a{i}", "kind": "advice", "doc_id": f"w-site{i:09d}", "evidence": "q", "start_ms": 4,
                 "text": f"t{i}", "topics": ["sales"], "speaker": f"A{i}", "deep_link": f"https://s/{i}"}
                for i in range(start, n)]
    store = SimpleNamespace(rows=lambda **k: rows(150))
    gone: set[str] = set()
    manifest = SimpleNamespace(documents=lambda: [], tombstoned_ids=lambda: gone)
    env = build.Env(ask_fn=None, store=store, db=EvalDB(Path(tempfile.mkdtemp()) / "e.db"), manifest=manifest,
                    root=Path(tempfile.mkdtemp()), private=Path(tempfile.mkdtemp()),
                    visibility=Visibility.everything_public(), judges=["j1", "j2"])
    with contextlib.redirect_stdout(io.StringIO()):
        n = build.seed_dev(env, split="dev-articles")
    assert env.db.target("dev-articles")["target"] == 30 and n == 42, "20% of 150, oversampled"
    with contextlib.redirect_stdout(io.StringIO()):
        assert build.seed_dev(env, split="dev-articles") == 0, "a rebuild never samples again"
    for q in env.db.questions("dev-articles")[:25]:
        env.db.set_status(q["qid"], "accepted")
    for q in env.db.questions("dev-articles")[25:]:
        env.db.set_status(q["qid"], "discarded")
    store.rows = lambda **k: rows(300)                        # 150 new articles indexed
    with contextlib.redirect_stdout(io.StringIO()) as out:
        added = build.seed_dev(env, split="dev-articles", top_up=True)
    qs = env.db.questions("dev-articles")
    assert env.db.target("dev-articles")["target"] == 60 and added == 49, out.getvalue()   # 35 missing x 1.4
    new = [q for q in qs if q["status"] == "seeded"]
    assert all(int(q["record"]["doc_id"][-9:]) >= 150 for q in new), "the Documents added since go first"
    assert len({q["record"]["doc_id"] for q in qs}) == len(qs), "never two questions from one Document"
    assert new[0]["qid"] == "deva-0043", "ids continue"
    # a removed Document retires its question at the next decision
    first = env.db.questions("dev-articles", ("accepted",))[0]
    gone.add(first["record"]["doc_id"])
    env.db.save_pool(first["qid"], {first["seed_moment"]: (["seed"], 1)}, 20)
    res = build._decide(env, "dev-articles")
    assert res["retired"] == 1 and env.db.questions("dev-articles", ("retired",))[0]["qid"] == first["qid"]


def test_ops_runs_only_what_changed_resumes_and_stops_at_a_failed_gate():
    import contextlib
    import io
    from ytbrain import ops
    from ytbrain.config import EVAL_DATA
    if ops.STATE.exists():
        ops.STATE.unlink()
    base = EVAL_DATA / "runs" / "baseline-all-full.json"
    if base.exists():
        base.unlink()
    calls, fail = [], {}

    def call(argv):
        calls.append(" ".join(a for a in argv if not a.replace(".", "").isdigit()))
        for key, code in fail.items():
            if key in calls[-1]:
                return code
        return 0

    def run(**kw):
        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = ops.Ops(ops.Options(**{"version": "skip", "coach": True, **kw}), call=call, say=print).run()
        return code, out.getvalue()

    code, out = run()
    assert code == 0, out
    assert calls[:4] == ["sync", "clean", "extract", "verify"] and "index" in calls
    assert "eval run --config full" in calls and "eval judge --config full --max-cost" in calls
    assert "eval rescore --config full --save-baseline" in calls, "the first eval becomes the baseline"
    assert any(c.startswith("script scripts/assemble_plugin.py") for c in calls)
    assert "eval coach --plugin dist/plugin --max-cost" in calls, "a new build gets its coach eval"
    assert any(c.startswith("eval gap --pack") for c in calls), "the shipped pack's coverage is reported"
    assert calls.index(next(c for c in calls if c.startswith("eval gap"))) < calls.index("eval coach --plugin dist/plugin --max-cost")

    code, out = run()                                    # nothing changed
    assert code == 0 and "eval skipped: nothing changed" in out and "plugin skipped: nothing changed" in out
    assert not any(c.startswith("eval") or c.startswith("pack") for c in calls), calls

    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("{}")
    fail["eval judge"] = 2                               # stopped mid-eval (spend cap, endpoint)
    code, out = run(plan="eval", force=True)
    assert code == 2 and "resumes here" in out
    fail.clear()
    code, out = run(plan="eval")                          # resumes at the judge, not the questions
    assert "resuming the eval run" in out and calls[0] == "eval judge --config full --max-cost", calls
    assert calls[1:] == ["eval rescore --config full --refresh-baseline",
                         "eval rescore --config full --compare full",
                         "eval rescore --config full --save-baseline"]
    fail["--compare full"] = 1                           # a real regression
    code, out = run(plan="all", force=True)
    assert code == 1 and "FAIL" in out and not any(c.startswith("pack") for c in calls), "no plugin after a FAIL"
    assert "eval rescore --config full --save-baseline" not in calls, "the baseline is kept"
    fail.clear()
    code, out = run(plan="plugin")
    assert "plugin skipped: the current index has no passing eval" in out
    st = json.loads(ops.STATE.read_text())               # a pass, but on an index that has since changed
    st["eval"] = {**(st.get("eval") or {}), "verdict": "pass", "index": "an-older-index"}
    ops.STATE.write_text(json.dumps(st))
    code, out = run(plan="plugin")
    assert "plugin skipped: the current index has no passing eval" in out and "eval pass (on an older index)" in out, out
    fail["eval gap"] = 3                                 # coverage targets missed: reported, never a stop
    ops.STATE.unlink()
    run(plan="eval", force=True)
    code, out = run(plan="plugin", force=True)
    assert code == 0 and "eval gap exit 3 (report only)" in out and "eval coach --plugin dist/plugin --max-cost" in calls
    fail.clear()
    # the default: the plugin is built and validated, and the coach eval waits for --coach (before a release)
    code, out = run(plan="plugin", force=True, coach=False)
    assert code == 0 and not any(c.startswith("eval coach") for c in calls), calls
    assert any(c.startswith("script scripts/assemble_plugin.py") for c in calls)
    assert "coach eval not run (`ytbrain ops plugin --coach` before a release)" in out, out
    assert ops.Options().coach is False


def test_ops_carries_on_when_a_step_crashes_on_exit_after_writing_output_that_verifies():
    """macOS: a native library (ONNX) can abort while Python shuts down, after `pack build` wrote a good pack.
    ops then trusts the output, never the bare exit code: only a crash signal is checked, and only output this
    run wrote and that matches its checksum passes; Ctrl+C, kill and ordinary failures stop as before."""
    import contextlib
    import io
    import json as _json
    import time as _time
    from ytbrain import ops
    assert ops.crash_signal(-6) == "SIGABRT" and ops.crash_signal(134) == "SIGABRT" and ops.crash_signal(-11) == "SIGSEGV"
    assert ops.crash_signal(-2) is None and ops.crash_signal(130) is None and ops.crash_signal(-15) is None
    assert ops.crash_signal(1) is None and ops.crash_signal(2) is None and ops.crash_signal(0) is None

    import test_pack as TP
    out = Path(tempfile.mkdtemp()) / "pack"
    before = _time.time()
    TP._build(out)
    assert ops.pack_written(out, before) is None, "a fresh pack that matches its manifest"
    assert "older than this run" in ops.pack_written(out, _time.time() + 60)
    nxt = out / "pack.json.next"
    nxt.write_text("{}")
    assert "between writing the pack and its manifest" in ops.pack_written(out, before)
    nxt.unlink()
    m = _json.loads((out / "pack.json").read_text())
    (out / "pack.json").write_text(_json.dumps({**m, "sha256": "0" * 64}))
    assert "doesn't match" in ops.pack_written(out, before)
    assert "missing or unreadable" in ops.pack_written(Path(tempfile.mkdtemp()), before)

    seen = []
    o = ops.Ops(ops.Options(version="skip"), call=lambda argv: seen.append(argv) or code[0], say=print)
    verdict = [None]
    step = ops.Step("plugin:pack", ["pack", "build"], check=lambda since: verdict[0])
    for code, why, want in (([-6], None, 0), ([134], None, 0), ([-6], "pack.json is older", -6),
                            ([-2], None, -2), ([1], None, 1)):
        verdict[0] = why
        with contextlib.redirect_stdout(io.StringIO()) as said:
            assert o.child(step) == want, (code, why)
        text = said.getvalue()
        if want == 0:
            assert "crashed on exit (SIGABRT) after writing its output" in text
        elif why:
            assert "its output doesn't verify: pack.json is older" in text
        else:
            assert "crashed" not in text, "Ctrl+C and ordinary failures are never second-guessed"
    unchecked = ops.Step("eval:search", ["eval", "run"])
    code = [-6]
    with contextlib.redirect_stdout(io.StringIO()):
        assert o.child(unchecked) == -6, "a step with no check stops on any crash"
    steps = {s.name: s for s in o.plugin_steps()}
    assert steps["plugin:pack"].check is not None
    assert all(s.check is None for n, s in steps.items() if n not in ("plugin:pack", "plugin:pack-private"))


def test_ops_handles_each_stop_reason_asks_for_a_version_and_keeps_references():
    import contextlib
    import io
    import json as _json
    from ytbrain import ops, runstatus
    from ytbrain.config import EVAL_DATA
    for f in (ops.STATE, ops.LAST_RUN, EVAL_DATA / "runs" / "baseline-all-full.json"):
        if f.exists():
            f.unlink()
    assert runstatus.classify("HTTP 402: insufficient credits") == "endpoint"
    assert runstatus.classify("Connection reset by peer") == "network"
    assert runstatus.classify("HTTP 429 rate limit") == "network" and runstatus.classify("budget") == "budget"
    calls, script, slept = [], {}, []

    def call(argv):
        calls.append(" ".join(a for a in argv if not a.replace(".", "").isdigit()))
        for key, todo in script.items():
            if key in calls[-1] and todo:
                reason, code = todo.pop(0)
                runstatus.record(reason, f"{reason} detail")
                return code
        return 0

    versions = {"v": "0.1.0"}
    fake = type("R", (), {"current_version": staticmethod(lambda root: versions["v"]),
                          "set_version": staticmethod(lambda root, new: versions.update(v=new))})
    real_release = ops._release
    ops._release = lambda: fake
    answers = []

    def run(**kw):
        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            o = ops.Ops(ops.Options(**{"coach": True, **kw}), call=call, say=print, sleep=slept.append,
                        ask=lambda q, t: answers.pop(0) if answers else None)
            code = o.run()
        return code, out.getvalue()
    try:
        # a refused PDF is not the network: no retry, and the run carries on with the Books that registered
        script["sync"] = [("books", 1)]
        code, out = run(plan="ingest")
        assert code == 0 and calls.count("sync") == 1 and slept == [], out
        assert "carrying on with the Books that did register" in out and "continuing with what is already" not in out
        # a configuration problem (a book folder that is not a Domain) stops at once, with no retry
        script["sync"] = [("config", 1)]
        code, out = run(plan="ingest", restart=True)
        assert code == 1 and calls == ["sync"] and slept == [], out
        assert "configuration problem" in out
        # the network: sync retried twice, then the run goes on with what is fetched
        script["sync"] = [("network", 2)] * 3
        code, out = run(plan="ingest", restart=True)
        assert code == 0 and calls.count("sync") == 3 and slept == [60, 300], out
        assert "continuing with what is already fetched" in out
        # a refused endpoint stops at once, says what to fix, and resumes there
        script["extract"] = [("endpoint", 1)]
        code, out = run(plan="ingest", sync=False)
        assert code == 1 and calls.count("extract") == 1 and "LLM endpoint refused" in out
        assert "Resume: ytbrain ops ingest" in ops.LAST_RUN.read_text()
        code, out = run(plan="ingest", sync=False)
        assert code == 0 and calls[0] == "extract", "resumes at the extract"
        # the first eval: a baseline; the plugin asks for a version (minor), the coach hits the plan limit
        answers.append("m")
        script["eval coach"] = [("plan_limit", 2)]
        code, out = run(plan="all", sync=False)
        assert code == 0 and versions["v"] == "0.2.0", out
        assert "coach eval pending" in ops.LAST_RUN.read_text(), "the plugin is built; the coach waits for the reset"
        # the Claude login expired on the resume: ops stops with the fix, and the next run does only the coach eval
        script["eval coach"] = [("auth", 2)]
        code, out = run(plan="plugin")
        assert code == 2 and "/login" in out and "Resume: ytbrain ops plugin" in ops.LAST_RUN.read_text(), out
        assert _json.loads(ops.STATE.read_text())["coach"]["pending"] == "login needed"
        code, out = run(plan="plugin")
        assert "eval coach --plugin" in " ".join(calls) and not any(c.startswith("pack") for c in calls), \
            "only the pending coach eval runs again"
        # a failed coach gate is reported again by the next run (not forgotten as "already ran"), until it passes
        coach_run = lambda: sum(c.startswith("eval coach --plugin") for c in calls)         # noqa: E731
        st = _json.loads(ops.STATE.read_text())
        st["coach"] = {"build": None}                    # as for a freshly built plugin
        ops.STATE.write_text(_json.dumps(st))
        script["eval coach"] = [("unknown", 1)]
        code, out = run(plan="plugin")
        assert code == 1 and coach_run() == 1, out
        code, out = run(plan="plugin")
        assert code == 0 and coach_run() == 1 and not any(c.startswith("pack") for c in calls), out
        code, out = run(plan="plugin")
        assert coach_run() == 0 and "nothing changed" in out, out
        # a hand-set version is kept; no answer means patch
        versions["v"] = "0.5.0"
        code, out = run(plan="plugin", force=True)
        assert versions["v"] == "0.5.0" and "set since the last build" in out
        code, out = run(plan="plugin", force=True)
        assert versions["v"] == "0.5.1" and "no answer: patch 0.5.1" in out
        # references: the baseline in force before a change is kept, dated
        base = EVAL_DATA / "runs" / "baseline-all-full.json"
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(_json.dumps({"at": "2026-09-30T10:00:00", "config": "full"}))
        state = _json.loads(ops.STATE.read_text())
        state["kinds"] = ["talk"]                     # as if chapters had never been indexed
        ops.STATE.write_text(_json.dumps(state))
        o = ops.Ops(ops.Options(), call=call, say=lambda m: None)
        o.load()
        o.indexed_kinds = lambda: {"talk", "chapter"}
        o.save_references(base)
        assert (base.parent / "baseline-all-full-2026-09-30.json").exists()
        assert (base.parent / "baseline-all-full-before-chapter.json").exists()
    finally:
        ops._release = real_release
        runstatus.clear()


def test_ops_offers_to_declare_a_stray_book_folder_and_stops_when_no_one_answers():
    import contextlib
    import io
    from ytbrain import domains as D
    from ytbrain import ops, runstatus
    tmp = Path(tempfile.mkdtemp())
    f = tmp / "domains.yaml"
    f.write_text("default: startup\ndomains:\n  startup:\n    description: Startups.\n    risk_tier: medium\n")
    real = (D.DOMAINS_FILE, runstatus.STATUS, ops.STATE, ops.LAST_RUN)
    D.DOMAINS_FILE, runstatus.STATUS, ops.STATE, ops.LAST_RUN = f, tmp / "stop.json", tmp / "state.json", tmp / "last.md"
    syncs = []

    def call(argv):
        if argv[0] == "sync":
            syncs.append(argv)
            if "legal" not in D.load().names and "Legal" not in D.load().ignore_folders:
                runstatus.record("config", "book folder Legal/ is not a declared Domain")
                return 1
        return 0
    try:
        for answers, expect_code, expect_syncs, declared in (
                ([None], 1, 1, False),                                   # no one at the terminal: stop as before
                (["", "x"], 1, 1, False),                                # Enter: stop
                (["h", ""], 1, 1, False),                                # a tier but no description: stop
                (["h", "Law for founders: incorporation, contracts."], 0, 2, True)):
            f.write_text("default: startup\ndomains:\n  startup:\n    description: Startups.\n    risk_tier: medium\n")
            ops.STATE.unlink(missing_ok=True)
            syncs.clear()
            q = list(answers)
            o = ops.Ops(ops.Options(plan="ingest", notify=False), call=call, say=print,
                        ask=lambda prompt, t: q.pop(0) if q else None, stray=lambda: {"Legal": 2})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = o.run()
            assert code == expect_code and len(syncs) == expect_syncs, (answers, code, syncs, out.getvalue())
            assert ("legal" in D.load().names) == declared
            if declared:
                assert D.load().get("legal").risk_tier == "high" and "syncing again" in out.getvalue()
            else:
                assert "ytbrain domains add" in out.getvalue(), out.getvalue()
        f.write_text("default: startup\ndomains:\n  startup:\n    description: Startups.\n    risk_tier: medium\n")
        ops.STATE.unlink(missing_ok=True)
        syncs.clear()
        q = ["i"]
        o = ops.Ops(ops.Options(plan="ingest", notify=False), call=call, say=print,
                    ask=lambda prompt, t: q.pop(0) if q else None, stray=lambda: {"Legal": 2})
        with contextlib.redirect_stdout(io.StringIO()):
            assert o.run() == 0 and len(syncs) == 2 and D.load().ignore_folders == ("Legal",)
    finally:
        D.DOMAINS_FILE, runstatus.STATUS, ops.STATE, ops.LAST_RUN = real


def test_run_files_of_a_set_with_a_dash_are_found_under_that_set():
    from ytbrain.eval.run import RUN_NAME, latest_run
    m = RUN_NAME.fullmatch("dev-articles-full-series3-2026-10-01T101010.run")
    assert m["split"] == "dev-articles" and m["config"] == "full-series3"
    assert RUN_NAME.fullmatch("all-full-2026-10-01T101010.run")["split"] == "all"
    assert RUN_NAME.fullmatch("dev-full-2026-10-01T101010.run")["config"] == "full"
    with tempfile.TemporaryDirectory() as d:
        for name in ("dev-full-2026-10-01T101010.run", "dev-articles-full-2026-10-02T101010.run"):
            (Path(d) / name).write_text("")
        assert latest_run(Path(d), "dev", "full").name == "dev-full-2026-10-01T101010.run"
        assert latest_run(Path(d), "dev-articles", "full").name == "dev-articles-full-2026-10-02T101010.run"


def test_a_pool_extension_grades_the_new_moments_of_every_split_in_the_run():
    """`eval judge` used to grade only `dev` questions, so a private question's new Moments were
    pooled but never graded and the next judge stopped on them forever."""
    from ytbrain.eval import build
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Budget
    env = build.Env(ask_fn=_fake_ask([]), db=EvalDB(Path(tempfile.mkdtemp()) / "e.db"),
                    root=Path(tempfile.mkdtemp()), private=Path(tempfile.mkdtemp()), judges=["j1", "j2", "j3"])
    env.moment_text = lambda m: "pricing advice"
    qs = []
    for qid, split in (("dev-0001", "dev"), ("devp-0001", build.PRIVATE_SPLIT)):
        rec = {"question": "How should I do pricing?"}
        env.db.upsert_question(qid, split, 1, "accepted", rec, text=rec["question"], seed_moment=moment_id(YT, 60))
        env.db.save_pool(qid, {moment_id(YT, 60): (["seed"], 1)}, 20)
        qs += env.db.questions(split, ("accepted",))
    assert build.judge_step(env, "all", Budget(5, 0), questions=qs) is None
    assert all(not build.decided_labels(env, q["qid"])[1] for q in qs), "both splits' Moments are graded"


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


def test_run_parallel_asks_a_slow_call_again_and_takes_the_first_answer():
    import contextlib, io, threading
    from ytbrain.eval import llm
    release = threading.Event()
    calls, seen, lock = {}, [], threading.Lock()

    def work(job):
        with lock:
            calls[job] = calls.get(job, 0) + 1
            n = calls[job]
        if job == "slow" and n == 1:
            release.wait(20)                  # the provider hangs on this request, not on the repeat
        return job.upper()

    with contextlib.redirect_stdout(io.StringIO()) as out:
        stopped = llm.run_parallel(["a", "slow", "b", "c"], work,
                                   lambda j, r, e: seen.append((j, r, type(e).__name__ if e else None)),
                                   workers=4, label="t", heartbeat_s=0.2, deadline_s=15.0, hedge_s=0.3)
    release.set()
    assert stopped is None
    assert sorted(seen) == [("a", "A", None), ("b", "B", None), ("c", "C", None), ("slow", "SLOW", None)]
    assert calls["slow"] == 2 and calls["a"] == 1                  # only the slow one was repeated
    assert "asking again in parallel" in out.getvalue()


def test_run_parallel_does_not_ask_again_while_the_provider_is_rate_limiting():
    import contextlib, io, threading, time
    from unittest import mock
    from ytbrain.eval import llm
    calls, lock = {}, threading.Lock()

    def work(job):
        with lock:
            calls[job] = calls.get(job, 0) + 1
        if job == "slow":
            time.sleep(1.0)                   # waiting out 429s, not hung
        return job

    with contextlib.redirect_stdout(io.StringIO()) as out, \
            mock.patch.object(llm.runner, "current_rpm", return_value=6.0), \
            mock.patch.object(llm.runner, "rpm_ceiling", return_value=60.0):
        llm.run_parallel(["a", "slow"], work, lambda j, r, e: None,
                         workers=2, label="t", heartbeat_s=0.2, deadline_s=15.0, hedge_s=0.2)
    assert calls == {"a": 1, "slow": 1}
    assert "asking again" not in out.getvalue()


def test_run_parallel_reports_a_hedged_job_once_when_both_attempts_fail():
    import contextlib, io, threading, time
    from ytbrain.eval import llm
    seen, lock, n = [], threading.Lock(), [0]

    def work(job):
        if job != "bad":
            return job
        with lock:
            n[0] += 1
        time.sleep(1.2)                   # long enough for the loop to ask again before this fails
        raise ValueError("provider said no")

    with contextlib.redirect_stdout(io.StringIO()):
        llm.run_parallel(["ok", "bad"], work, lambda j, r, e: seen.append((j, type(e).__name__ if e else None)),
                         workers=2, label="t", heartbeat_s=0.2, deadline_s=15.0, hedge_s=0.2)
    assert sorted(seen) == [("bad", "ValueError"), ("ok", None)]   # settled once, not twice
    assert n[0] == 2


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


def test_coach_eval_stream_parser_survives_events_whose_message_is_plain_text():
    """A host notice (retry, overload) can carry `message` as a string: it must not fail the case."""
    from ytbrain.eval import coach as C
    t = C.parse_stream(_stream(
        {"type": "system", "subtype": "api_retry", "message": "Overloaded, retrying in 2s"},
        {"type": "assistant", "message": "not a dict either"},
        {"type": "user", "message": ["nor", "a", "list", "of", "blocks"]},
        {"type": "result", "subtype": "success", "result": "Answer.", "session_id": "s-2"}))
    assert t.error is None and t.text == "Answer." and t.session_id == "s-2"


def test_an_unexpected_case_error_says_where_it_was_raised():
    from ytbrain.eval import coach as C
    try:
        {}["x"].get("y")
    except KeyError as e:
        assert "test_eval.py:" in C._where(e)
    assert C._where(ValueError("no traceback")) == ""


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
    C.load_cases = lambda g, plugin=None: cases[g]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g2", "g4", "g6", "g5"], plugin, runner=run)
        assert summ["g2"]["rate"] == 0.5 and not summ["g2"]["passed"]         # 1 of 2 cited claims supported
        assert summ["g4"]["rate"] == 0.5                                       # the "raise" plan wasn't challenged
        assert summ["g6"]["passed"]                                            # 3 searches for 2 parts, judged ok
        g5 = json.loads(next(C.OUT.glob(f"g5-*-{C.HARNESS['g5']}.jsonl")).read_text().splitlines()[0])
        assert [c["ok"] for c in g5["checks"]] == [True, True, False]          # no Goal was recorded
        assert code == 1 and list(C.OUT.glob("report-*.json"))
        n = len(calls)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            C.run_gates(env, ["g2", "g4", "g6", "g5"], plugin, runner=run)
        assert len(calls) == n and "already done for these inputs" in out.getvalue()   # resumed: nothing re-run
        (plugin / "BUILD_ID").write_text("b2\n")
        (plugin / "server.py").write_text("V = 2\n")                         # a build that changed the runtime
        with contextlib.redirect_stdout(io.StringIO()):
            C.run_gates(env, ["g4"], plugin, runner=run, limit=1)
        assert len(calls) == n + 2      # starts fresh; that unchallenged plan fails twice, which decides it
        def interrupted(prompt, env=None, resume=None):
            raise KeyboardInterrupt
        (plugin / "BUILD_ID").write_text("b3\n")
        (plugin / "server.py").write_text("V = 3\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4"], plugin, runner=interrupted)
        assert code == 130 and "interrupted" in out.getvalue()                 # Ctrl+C: a message, not a traceback
    finally:
        C.load_cases = real


def test_coach_eval_case_files_are_well_formed():
    from ytbrain.eval import coach as C
    syc, dec, per = (C.load_cases(g) for g in ("g4", "g6", "g5"))
    assert len(syc) == 10 and 6 <= len(dec) <= 12 and len(per) == 3
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
    C.load_cases = lambda g, plugin=None: cases[g]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4", "g6"], plugin, runner=run)
        assert code == 2 and len(calls) == 2, (code, calls)                 # stopped, not 5 more errors
        assert "usage limit" in out.getvalue() and "re-run" in out.getvalue()
        kept = next(C.OUT.glob(f"g4-*-{C.HARNESS['g4']}.jsonl")).read_text().splitlines()
        assert [json.loads(x)["id"] for x in kept] == ["s0"]                 # the finished case is kept
        assert C.HOST_LIMIT.search("Claude AI usage limit reached|1760000000")
        assert not C.HOST_LIMIT.search("claude exited 1: MCP server failed to start")
    finally:
        C.load_cases = real


def test_coach_gate_with_only_errored_cases_missing_is_incomplete_not_failed():
    from ytbrain.eval import coach as C
    # 19 of 20 cases finished at 92 % (gate 90 %), one errored: not a FAIL, an incomplete run to retry
    done = [{"id": f"c{i}", "claims": 5, "supported": 5 if i else 3} for i in range(19)]
    s = C.score("g2", done + [{"id": "c19", "error": "error_max_turns"}])
    assert s["errors"] == 1 and s["rate"] >= s["threshold"] and not s["passed"]
    # a rate under the gate is a FAIL whatever the errors
    bad = [{"id": f"c{i}", "claims": 5, "supported": 2} for i in range(19)]
    s2 = C.score("g2", bad + [{"id": "c19", "error": "x"}])
    assert s2["rate"] < s2["threshold"] and not s2["passed"]


def test_coach_eval_with_one_errored_case_is_an_incomplete_run_and_a_rerun_does_only_that_case():
    import contextlib
    import io
    from ytbrain import runstatus
    from ytbrain.eval import coach as C
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Answer
    tmp = Path(tempfile.mkdtemp())
    C.OUT = tmp / "coach"
    plugin = tmp / "plugin"
    plugin.mkdir()
    (plugin / "BUILD_ID").write_text("b3\n")
    calls, broken = [], {"s3": True}

    def run(prompt, env=None, resume=None):
        calls.append(prompt)
        if prompt == "plan 3" and broken["s3"]:
            return C.Turn(error="error_max_turns", tools=[
                {"name": C.SEARCH, "input": {"query": "q"}, "result": ""},
                {"name": C.SEARCH, "input": {"query": "x" * 500}, "result": ""}])
        return C.Turn(text="Answer (Seibel, 2019).", session_id="s",
                      tools=[{"name": C.SEARCH, "input": {"query": prompt}, "result": "1. [advice] X"}])
    env = type("Env", (), {"judges": ["j1", "j2"], "max_cost": 5, "db": EvalDB(tmp / "eval.db"),
                           "ask": staticmethod(lambda *a, **k: Answer(C.Verdict(passed=True, reason="r"), cost=0))})()
    cases = {"g4": [{"id": f"s{i}", "prompt": f"plan {i}"} for i in range(10)]}
    real = C.load_cases
    C.load_cases = lambda g, plugin=None: cases[g]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4"], plugin, runner=run)
        assert code == 2 and "incomplete" in out.getvalue() and "Re-run" in out.getvalue(), (code, out.getvalue())
        assert "-> INCOMPLETE" in out.getvalue() and "-> FAIL" not in out.getvalue(), out.getvalue()
        assert len(calls) == 10
        # the errored run's tool calls are kept for diagnosis, next to the cache and never read as a result
        assert "2 tool call(s): 2 coach_search" in out.getvalue() and ".errors.jsonl" in out.getvalue(), out.getvalue()
        errs = list((tmp / "coach").glob("*.errors.jsonl"))
        assert len(errs) == 1, errs
        rec = json.loads(errs[0].read_text().splitlines()[0])
        assert rec["id"] == "s3" and rec["gate"] == "g4" and rec["error"] == "error_max_turns"
        assert [c["tool"] for c in rec["trace"]] == ["coach_search", "coach_search"]
        assert len(rec["trace"][1]["input"]) <= 203, "long inputs are cut short"
        broken["s3"] = False
        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            summ, code = C.run_gates(env, ["g4"], plugin, runner=run)
        assert code == 0 and calls == ["plan 3"], (code, calls)        # only the errored case runs again
    finally:
        C.load_cases = real


def _fake_plugin(root: Path, *, version="0.1.0", checkin="Check-in body", checkin_desc="Runs a Check-in",
                 content="c1", file_sha="f1") -> Path:
    import json as _json
    p = root / "plugin"
    if p.exists():
        shutil.rmtree(p)
    for name, desc, body in (("ask", "Answers questions", "Ask body"), ("coach", "Coaching method", "Contract"),
                             ("check-in", checkin_desc, checkin)):
        (p / "skills" / name).mkdir(parents=True)
        (p / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n")
    (p / "skills" / "check-in" / "references").mkdir()
    (p / "skills" / "check-in" / "references" / "x.md").write_text(checkin)
    (p / "founder_coach" / "playbooks").mkdir(parents=True)
    (p / "founder_coach" / "server.py").write_text("SERVER = 1\n")
    (p / "founder_coach" / "__init__.py").write_text(f'"""rt"""\n__version__ = "{version}"\n')
    (p / "founder_coach" / "playbooks" / "check-in.md").write_text(checkin)
    (p / ".claude-plugin").mkdir()
    (p / ".claude-plugin" / "plugin.json").write_text(_json.dumps({"name": "x", "version": version}))
    (p / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    (p / "pack").mkdir()
    (p / "pack" / "pack.json").write_text(_json.dumps({"sha256": file_sha, "content_sha256": content,
                                                       "built_at": file_sha}))
    (p / "pack" / "knowledge.sqlite").write_text(file_sha)
    (p / "BUILD_ID").write_text(f"build-{file_sha}-{version}\n")
    return p


def test_every_domain_a_decomposition_case_expects_is_declared():
    from ytbrain import domains as D
    from ytbrain.eval import coach as C
    names = set(D.load().names)
    cases = C.load_cases("g6")
    multi = [c for c in cases if c.get("domains")]
    assert len(multi) >= 4, "multi-Domain questions are part of G6"
    for c in multi:
        assert len(c["domains"]) == len(c["parts"]), c["id"]
        assert all(set(d) <= names for d in c["domains"]), (c["id"], c["domains"])
    assert len({c["id"] for c in cases}) == len(cases)


def test_a_part_reaches_its_domain_by_naming_it_or_by_searching_everything():
    from ytbrain.eval import coach as C
    r = C.domain_parts([["gtm", "startup"], ["leadership"]], [{"query": "a", "domains": ["startup"]},
                                                             {"query": "b", "domains": ["finance"]}])
    assert r["domain_parts_reached"] == 1 and r["domains_searched"] == [["startup"], ["finance"]]
    r = C.domain_parts([["gtm"], ["leadership"]], [{"query": "a"}, {"query": "b", "domains": []}])
    assert r["domain_parts_reached"] == 2 and r["domains_searched"] == [["*"], ["*"]], "no domains = the whole Library"


def test_each_coach_gate_is_cached_on_what_it_depends_on_not_the_whole_build():
    from ytbrain.eval import coach as C
    tmp = Path(tempfile.mkdtemp())
    cases = [{"id": "a", "prompt": "q"}]

    def keys(**kw):
        p = _fake_plugin(tmp, **kw)
        return {g: C.gate_inputs(p, g, cases) for g in ("g2", "g4", "g5", "g6")}
    base = keys()
    assert keys(version="0.2.0", file_sha="f2") == base, "a new version and a rebuilt pack file with the same items"
    edited = keys(checkin="Check-in body, reworded")
    assert edited["g5"] != base["g5"], "G5 uses the check-in skill"
    assert all(edited[g] == base[g] for g in ("g2", "g4", "g6")), "the other gates don't read its text"
    described = keys(checkin_desc="Runs the weekly Check-in")
    assert all(described[g] != base[g] for g in base), "a description is in every gate's context"
    assert all(v != base[g] for g, v in keys(content="c2").items()), "new pack content re-runs every gate"
    p = _fake_plugin(tmp)
    assert C.gate_inputs(p, "g2", cases) != C.gate_inputs(p, "g2", cases + [{"id": "b", "prompt": "r"}])
    (p / "founder_coach" / "server.py").write_text("SERVER = 2\n")
    assert C.gate_inputs(p, "g4", cases) != base["g4"], "the runtime is every gate's input"
    import json as _json
    (p / "pack" / "pack.json").write_text(_json.dumps({"sha256": "f9"}))      # an older pack: the file hash
    assert C.pack_id(p) == "f9"
    (p / "pack" / "pack.json").unlink()
    assert C.pack_id(p) == "none"


def test_a_failed_case_is_run_again_and_a_majority_of_three_decides():
    from ytbrain.eval import coach as C

    def runner(outcomes):
        seq = iter(outcomes)
        n = {"runs": 0}

        def one():
            n["runs"] += 1
            o = next(seq)
            if o == "err":
                return {"id": "x", "error": "error_max_turns"}
            return {"id": "x", "passed": o, "host_tokens": {"input": 10}, "host_cost_usd": 0.5, "reason": str(o)}
        return one, n
    one, n = runner([True])
    assert C.run_trials("g4", {"id": "x"}, one, say=lambda m: None)["passed"] and n["runs"] == 1, "a pass costs one run"
    one, n = runner([False, False])
    r = C.run_trials("g4", {"id": "x"}, one, say=lambda m: None)
    assert not r["passed"] and r["trials"] == [False, False] and n["runs"] == 2, "two failures decide it"
    one, n = runner([False, True, True])
    said = []
    r = C.run_trials("g5", {"id": "x"}, one, say=said.append)
    assert "0 of 1 run(s) passed; run 2 of up to 3" in said[0] and "1 of 2 run(s) passed; run 3 of up to 3" in said[1], \
        "the retry line says how many runs passed so far, never 'failed' after a pass"
    assert not any("failed" in m for m in said), said
    assert r["passed"] and r["trials"] == [False, True, True] and r["reason"] == "True"
    assert r["host_tokens"] == {"input": 30} and r["host_cost_usd"] == 1.5, "every run's tokens are counted"
    one, n = runner([False, True, False])
    r = C.run_trials("g6", {"id": "x"}, one, say=lambda m: None)
    assert not r["passed"] and r["reason"] == "False", "a failure keeps the failing run's details"
    one, n = runner([False, "err"])
    assert "error" in C.run_trials("g4", {"id": "x"}, one, say=lambda m: None), "an errored run isn't a verdict"
    one, n = runner([False])
    assert not C.run_trials("g2", {"id": "x"}, one, say=lambda m: None)["passed"] and n["runs"] == 1, "G2 runs once"


def test_coach_eval_reuses_a_gates_results_across_a_rebuild_that_changed_nothing_it_uses():
    import contextlib
    import io
    from ytbrain.eval import coach as C
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Answer
    tmp = Path(tempfile.mkdtemp())
    C.OUT = tmp / "coach"
    calls = []

    def run(prompt, env=None, resume=None):
        calls.append(prompt)
        return C.Turn(text="Answer (Seibel, 2019).", session_id="s",
                      tools=[{"name": C.SEARCH, "input": {"query": prompt}, "result": "1. [advice] X"}])
    env = type("Env", (), {"judges": ["j1", "j2"], "max_cost": 5, "db": EvalDB(tmp / "eval.db"),
                           "ask": staticmethod(lambda *a, **k: Answer(C.Verdict(passed=True, reason="r"), cost=0))})()
    cases = {"g4": [{"id": f"s{i}", "prompt": f"plan {i}"} for i in range(3)]}
    real = C.load_cases
    C.load_cases = lambda g, plugin=None: cases[g]
    try:
        plugin = _fake_plugin(tmp)
        with contextlib.redirect_stdout(io.StringIO()):
            assert C.run_gates(env, ["g4"], plugin, runner=run)[1] == 0 and len(calls) == 3
        calls.clear()
        plugin = _fake_plugin(tmp, version="0.1.1", file_sha="f2", checkin="reworded")   # G4 doesn't read check-in
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert C.run_gates(env, ["g4"], plugin, runner=run)[1] == 0
        assert calls == [] and "3 already done" in out.getvalue(), out.getvalue()
    finally:
        C.load_cases = real


def test_coach_eval_stops_when_the_host_started_without_the_plugin_and_records_nothing():
    """Cases that ran with no coach tools (a plugin the host did not load) fail for that reason, not for the coach's
    answers: the run stops at the first one and caches nothing; a host that reports the tools runs as usual."""
    import contextlib
    import io
    from ytbrain.eval import coach as C
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Answer
    lines = ['{"type":"system","subtype":"init","session_id":"s","tools":["Skill","mcp__plugin_other_coach__coach_search"],'
             '"mcp_servers":[{"name":"plugin:other:coach","status":"connected"}],"plugins":[{"name":"other"}]}',
             '{"type":"result","subtype":"success","result":"hi","session_id":"s"}']
    parsed = C.parse_stream(lines)
    assert parsed.init["tools"][0] == "Skill" and parsed.text == "hi"
    tmp = Path(tempfile.mkdtemp())
    C.OUT = tmp / "coach"
    plugin = tmp / "plugin"
    plugin.mkdir()
    (plugin / "BUILD_ID").write_text("b3\n")
    calls = []
    me = f"mcp__plugin_{C.TARGET['id']}_coach__coach_search"
    loaded = {"tools": ["Skill", me]}

    def run(prompt, env=None, resume=None):
        calls.append(prompt)
        return C.Turn(text="Challenged, citing Seibel (2019).", session_id="s", init=parsed.init, tools=[
            {"name": C.SEARCH, "input": {"query": prompt}, "result": "1. [advice] X · item_id adv:A:a1"}])

    env = type("Env", (), {"judges": ["j1", "j2"], "max_cost": 5, "db": EvalDB(tmp / "eval.db"),
                           "ask": staticmethod(lambda *a, **k: Answer(C.Verdict(passed=True, reason="r"), cost=0))})()
    cases = {"g4": [{"id": f"s{i}", "prompt": f"plan {i}"} for i in range(4)]}
    real_cases = C.load_cases
    C.load_cases = lambda g, plugin=None: cases[g]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4"], plugin, runner=run)
        assert code == 2 and len(calls) == 1, (code, calls)
        text = out.getvalue()
        assert "did not load the coach tools of" in text and "plugin:other:coach: connected" in text and "--plugin-dir" in text, text
        assert not list(C.OUT.glob(f"g4-*-{C.HARNESS['g4']}.jsonl")), "nothing was recorded as a result"
        calls.clear()
        run2 = lambda prompt, env=None, resume=None: (calls.append(prompt), C.Turn(     # noqa: E731
            text="Challenged, citing Seibel (2019).", session_id="s", init=loaded, tools=[
                {"name": C.SEARCH, "input": {"query": prompt}, "result": "1. [advice] X · item_id adv:A:a1"}]))[1]
        with contextlib.redirect_stdout(io.StringIO()):
            summ, code = C.run_gates(env, ["g4"], plugin, runner=run2)
        assert len(calls) == 4 and code == 0, (code, calls)
    finally:
        C.load_cases = real_cases


def test_the_plugin_choice_gate_stops_when_the_host_did_not_load_every_plugin():
    import contextlib
    import io
    from ytbrain.eval import coach as C
    tmp = Path(tempfile.mkdtemp())
    C.OUT = tmp / "coach"
    plugins = []
    for name in ("a-coach", "b-coach"):
        (tmp / name).mkdir()
        plugins.append(tmp / name)
    calls = []

    def run(prompt, env=None, resume=None):
        calls.append(prompt)
        return C.Turn(text="x", session_id="s", init={"tools": ["Skill", "mcp__plugin_a-coach_coach__coach_search"],
                                                       "mcp_servers": [{"name": "plugin:b-coach:coach", "status": "failed"}]})
    cases = [{"id": f"c{i}", "prompt": f"p{i}", "expect": "a-coach"} for i in range(3)]
    with contextlib.redirect_stdout(io.StringIO()) as out:
        summary, code = C.run_choice(plugins, runner=run, cases=cases)
    assert code == 2 and summary["passed"] is False and len(calls) == 1, (code, calls)
    assert "b-coach" in out.getvalue() and "plugin:b-coach:coach: failed" in out.getvalue(), out.getvalue()
    assert not list(C.OUT.glob("choice-*.jsonl")) or not any(f.read_text().strip() for f in C.OUT.glob("choice-*.jsonl"))


def test_warm_plugin_builds_the_runtime_once_with_the_plugins_own_command_and_reports_a_failure():
    import sys
    from ytbrain.eval import coach as C
    tmp = Path(tempfile.mkdtemp())
    plugin = tmp / "p-coach"
    plugin.mkdir()
    marker = tmp / "ran.txt"

    def write(code):
        (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {"coach": {"command": sys.executable, "args": [
            "-c", code, "${CLAUDE_PLUGIN_ROOT}", "serve", "--pack", "${CLAUDE_PLUGIN_ROOT}/pack"]}}}))
    write(f"import sys; open({str(marker)!r}, 'w').write(' '.join(sys.argv[1:]))")
    lines = []
    C.warm_plugin(plugin, say=lines.append)
    assert marker.read_text() == f"{plugin.resolve()} --help", "`serve` and what follows are replaced by --help, the root is filled in"
    assert any("ready" in x for x in lines), lines
    write("import sys; sys.exit('no module named x')")
    try:
        C.warm_plugin(plugin, say=lines.append)
        raise AssertionError("a runtime that does not build should stop the run")
    except C.HostSetup as e:
        assert "does not build" in str(e) and "no module named x" in str(e), str(e)
    (plugin / ".mcp.json").unlink()
    C.warm_plugin(plugin, say=lines.append)                      # nothing to build: the host reports what is wrong


def test_the_host_run_leaves_out_a_synced_founder_coach_unless_it_is_the_plugin_under_test():
    from unittest import mock
    from ytbrain.eval import coach as C
    tmp = Path(tempfile.mkdtemp())
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return type("P", (), {"stdout": "", "stderr": "", "returncode": 0})()
    for name, expect in (("coding-coach", True), ("founder-coach", False)):
        d = tmp / name
        (d / "founder_coach").mkdir(parents=True)
        (d / "founder_coach" / "product.json").write_text(json.dumps({"id": name}))
        with mock.patch.object(C.subprocess, "run", fake_run):
            C.claude_runner(d, workdir=tmp)("hi")
        denied = seen[-1][seen[-1].index("--disallowedTools"):]
        assert ("mcp__plugin_founder-coach_coach" in denied) is expect, (name, denied)
    # the host runs from its own scratch folder: a relative --plugin path would point nowhere there
    here = os.getcwd()
    os.chdir(tmp)
    try:
        with mock.patch.object(C.subprocess, "run", fake_run):
            C.claude_runner(Path("coding-coach"), workdir=tmp)("hi")
    finally:
        os.chdir(here)
    sent = seen[-1][seen[-1].index("--plugin-dir") + 1]
    assert Path(sent).is_absolute() and Path(sent).name == "coding-coach", sent


def test_a_pending_server_is_not_a_missing_one_and_the_investor_pack_asks_its_own_g2_questions():
    from unittest import mock
    from ytbrain.eval import coach as C
    starting = C.Turn(text="x", init={"tools": ["Skill"], "mcp_servers": [{"name": f"plugin:{C.TARGET['id']}:coach", "status": "pending"}]})
    C._check_host(starting)                                   # no error: its tools arrive during the turn
    failed = C.Turn(text="x", init={"tools": ["Skill"], "mcp_servers": [{"name": f"plugin:{C.TARGET['id']}:coach", "status": "failed"}]})
    try:
        C._check_host(failed)
        raise AssertionError("a failed server should stop the run")
    except C.HostSetup:
        pass
    with mock.patch.dict(C.TARGET, {"pack": "investor"}):
        qs = C.load_cases("g2")
    assert len(qs) >= 15 and all(q["id"].startswith("ia-") and q["prompt"].endswith("?") for q in qs)
    assert len({q["id"] for q in qs}) == len(qs)


def test_a_host_call_that_started_before_the_server_connected_is_repeated_but_a_used_or_refused_one_is_not():
    from ytbrain.eval import coach as C
    me = C.TARGET["id"]
    pending = lambda: {"mcp_servers": [{"name": f"plugin:{me}:coach", "status": "pending"}], "tools": ["Skill"]}   # noqa: E731
    connected = {"mcp_servers": [{"name": f"plugin:{me}:coach", "status": "connected"}], "tools": ["Skill"]}
    used_tool = [{"name": f"mcp__plugin_{me}_coach__coach_search", "input": {}, "result": ""}]

    def run_with(turns):
        calls = []

        def run(prompt, env=None, resume=None):
            calls.append((prompt, resume))
            return turns[min(len(calls), len(turns)) - 1]
        return C.ready_runner(run, retries=2, wait=0), calls
    r, calls = run_with([C.Turn(text="no tools", init=pending()), C.Turn(text="with tools", init=connected, tools=used_tool)])
    assert r("q", resume="s1").text == "with tools" and calls == [("q", "s1"), ("q", "s1")], "same prompt, same session"
    r, calls = run_with([C.Turn(text="a", init=pending())])
    assert r("q").text == "a" and len(calls) == 3, "gives up after the retries and returns the last one"
    r, calls = run_with([C.Turn(text="a", init=pending(), tools=used_tool)])
    r("q"); assert len(calls) == 1, "it used a coach tool: not repeated"
    r, calls = run_with([C.Turn(text="a", init=connected)])
    r("q"); assert len(calls) == 1, "the server was connected: the answer is the coach's own"
    r, calls = run_with([C.Turn(error="success: Failed to authenticate: OAuth session expired", init=pending())])
    r("q"); assert len(calls) == 1, "a login problem is not a slow start"


def test_coach_eval_stops_at_the_first_case_when_the_host_is_not_signed_in():
    import contextlib
    import io
    from ytbrain import runstatus
    from ytbrain.eval import coach as C
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Answer
    for text in ("success: Failed to authenticate: OAuth session expired and could not be refreshed",
                 "authentication_error: invalid x-api-key", "Please run /login"):
        assert C.HOST_AUTH.search(text), text
    assert not C.HOST_AUTH.search("claude exited 1: MCP server failed to start")
    assert not C.HOST_AUTH.search("You've hit your session limit · resets 1pm")
    tmp = Path(tempfile.mkdtemp())
    C.OUT = tmp / "coach"
    plugin = tmp / "plugin"
    plugin.mkdir()
    (plugin / "BUILD_ID").write_text("b2\n")
    calls = []
    run = lambda prompt, env=None, resume=None: (calls.append(prompt), C.Turn(error="success: Failed to authenticate: OAuth session expired"))[1]   # noqa: E731
    env = type("Env", (), {"judges": ["j1", "j2"], "max_cost": 5, "db": EvalDB(tmp / "eval.db"),
                           "ask": staticmethod(lambda *a, **k: Answer(C.Verdict(passed=True, reason="r"), cost=0))})()
    cases = {"g4": [{"id": f"s{i}", "prompt": f"plan {i}"} for i in range(5)]}
    real_cases, real_status = C.load_cases, runstatus.STATUS
    C.load_cases, runstatus.STATUS = (lambda g, plugin=None: cases[g]), tmp / "stop.json"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4"], plugin, runner=run)
        assert code == 2 and len(calls) == 1, (code, calls)                    # the first case, not all five
        assert "/login" in out.getvalue() and "not signed in" in out.getvalue()
        assert (runstatus.read() or {}).get("reason") == "auth"
        assert not list(C.OUT.glob(f"g4-*-{C.HARNESS['g4']}.jsonl")), "nothing was recorded as a result"
    finally:
        C.load_cases, runstatus.STATUS = real_cases, real_status

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

    change = {"code": True}

    def run(prompt, env=None, resume=None):
        calls.append(prompt)
        (plugin / "BUILD_ID").write_text(f"b{len(calls) + 1}\n")            # reassembled during each case...
        if change["code"]:
            (plugin / "server.py").write_text(f"V = {len(calls)}\n")         # ...with a change every gate uses
        return C.Turn(text="Answer (Seibel, 2019).", session_id="s",
                      tools=[{"name": C.SEARCH, "input": {"query": prompt}, "result": "1. [advice] X"}])
    env = type("Env", (), {"judges": ["j1", "j2"], "max_cost": 5, "db": EvalDB(tmp / "eval.db"),
                           "ask": staticmethod(lambda *a, **k: Answer(C.Verdict(passed=True, reason="r"), cost=0))})()
    real = C.load_cases
    C.load_cases = lambda g, plugin=None: [{"id": f"s{i}", "prompt": f"plan {i}"} for i in range(3)]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4", "g6"], plugin, runner=run)
        assert code == 2 and len(calls) == 1, (code, calls)
        assert "rebuilt during this run" in out.getvalue() and "g6" not in summ
        # a rebuild that only bumps the build id (a new version, the same files) changes nothing the gates use
        change["code"], C.OUT = False, tmp / "coach2"
        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summ, code = C.run_gates(env, ["g4"], plugin, runner=run)
        assert code == 0 and len(calls) == 3 and "rebuilt" not in out.getvalue(), (code, calls, out.getvalue())
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

def test_coach_citations_are_checked_for_every_source_kind_without_a_judge():
    from ytbrain.eval import coach as C
    t = C.Turn(text="")
    t.tools = [{"name": C.SEARCH, "input": {"query": "people"}, "result":
                '1. [advice] Keep good people\n   — Jim Collins, "First Who" in Good to Great, PDF p. 47 (2001) · item_id b1\n'
                '2. [advice] Charge early\n   — Seibel, "Pricing" (2019) · https://www.youtube.com/watch?v=abcdefghijk&t=760s · item_id t1\n'
                '3. [advice] Write it down\n   — PG, "Essay" (2009) · https://paulgraham.com/x.html#:~:text=write · item_id a1'}]
    t.text = ("Hire carefully (Jim Collins, Good to Great, PDF p. 47). Charge early "
              "([Seibel, \"Pricing\", 2019 · 12:40](https://www.youtube.com/watch?v=abcdefghijk&t=760s)). Write it "
              "down ([PG, Essay](https://paulgraham.com/x.html#:~:text=write)).")
    assert C.citation_problems(t) == []
    t.text += " Also ([made up](https://www.youtube.com/watch?v=abcdefghijk&t=999s)) and PDF p. 300."
    assert C.citation_problems(t) == ["link not returned by a search: https://www.youtube.com/watch?v=abcdefghijk&t=999s",
                                      "book page not retrieved: PDF p. 300"]
    assert t.evidence().startswith("[coach_search") and "PDF p. 47" in t.evidence().split("\n\n")[0], \
        "a cited book hit goes first, like a cited talk"


def test_the_g2_sample_asks_questions_from_every_kind_of_source():
    from ytbrain.eval import coach as C
    from ytbrain.eval import files
    root, overlay = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    lab = lambda qs: {q: {moment_id(YT, 60): {"grade": 3, "judges": {}}} for q in qs}
    talks = [f"dev-{i:04d}" for i in range(40)]
    arts = [f"deva-{i:04d}" for i in range(10)]
    files.write_split(root, "dev", [{"_id": q, "text": q, "split": "dev"} for q in talks], lab(talks), {})
    files.write_split(root, "dev-articles", [{"_id": q, "text": q, "split": "dev-articles"} for q in arts], lab(arts), {})
    books = [f"devp-{i:04d}" for i in range(10)]
    files.write_private(overlay, "dev-private", lab(books), [{"_id": q, "text": q, "split": "dev-private"} for q in books])
    public = C.sample_ask_questions(20, private=False, root=root, overlay=overlay)
    assert [sum(c["split"] == s for c in public) for s in ("dev", "dev-articles", "dev-private")] == [17, 3, 0]
    mine = C.sample_ask_questions(20, private=True, root=root, overlay=overlay)
    assert [sum(c["split"] == s for c in mine) for s in ("dev", "dev-articles", "dev-private")] == [14, 3, 3]
    assert len({c["id"] for c in mine}) == 20


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

# --- the routing eval (M5d) ---------------------------------------------------------------------
def _route_fixture():
    from ytbrain.eval import route as R
    docs = {"AAAAAAAAAA1": frozenset({"startup"}), "BBBBBBBBBB2": frozenset({"startup"}),
            "LLLLLLLLLL3": frozenset({"leadership"}), "MMMMMMMMMM4": frozenset({"leadership", "startup"})}
    q = lambda i, doc, **kw: {"_id": f"q{i}", "text": f"Question {i}?", "answerable": True,
                              "creation": {"seed_moment": f"{doc}_00120"}, **kw}
    queries = [q(1, "AAAAAAAAAA1"), q(2, "BBBBBBBBBB2"), q(3, "LLLLLLLLLL3"), q(4, "MMMMMMMMMM4"),
               q(5, "UNKNOWNDOC5"), q(6, "AAAAAAAAAA1", answerable=False), {"_id": "q7", "text": "no seed", "answerable": True}]
    return R, docs, queries


def test_routing_questions_expect_the_domains_of_their_seed_document():
    R, docs, queries = _route_fixture()
    items = R.questions(queries, docs)
    assert [(i.qid, set(i.expected)) for i in items] == [
        ("q1", {"startup"}), ("q2", {"startup"}), ("q3", {"leadership"}), ("q4", {"leadership", "startup"})], \
        "unanswerable, seedless and unknown-Document questions are left out"

    class Store:
        def rows(self, columns=None):
            return [{"doc_id": "D1", "domains": ["startup"]}, {"doc_id": "D1", "domains": ["startup"]},
                    {"doc_id": "D2", "domains": ["leadership", "startup"]}, {"doc_id": "D3", "domains": None}]
    assert R.doc_domains(Store()) == {"D1": frozenset({"startup"}), "D2": frozenset({"leadership", "startup"}), "D3": frozenset()}


def test_composed_prompts_join_questions_from_different_domains_and_are_deterministic():
    R, docs, queries = _route_fixture()
    items = R.questions(queries, docs) + [R.Item(f"s{i}", f"Startup question {i}?", frozenset({"startup"})) for i in range(6)] \
        + [R.Item(f"l{i}", f"Leadership question {i}?", frozenset({"leadership"})) for i in range(6)]
    a, b = R.compose(items, 5), R.compose(items, 5)
    assert a == b and 0 < len(a) <= 5
    for c in a:
        left, right = c.qid.split("+")
        by = {i.qid: i for i in items}
        assert not (by[left].expected & by[right].expected) and c.expected == by[left].expected | by[right].expected
        assert by[left].text.strip() in c.text and " Also, " in c.text
    used = [q for c in a for q in c.qid.split("+")]
    assert len(used) == len(set(used)), "a question is used once"
    assert R.compose([R.Item("x", "x?", frozenset({"startup"})), R.Item("y", "y?", frozenset({"startup"}))], 3) == [], \
        "no pair from different Domains: nothing composed"


def test_routing_scores_sweep_the_margin_over_scores_computed_once():
    R, _, _ = _route_fixture()
    items = [R.Item("a", "a", frozenset({"startup"})), R.Item("b", "b", frozenset({"leadership"})),
             R.Item("c+d", "c d", frozenset({"startup", "leadership"}))]
    scores = [{"startup": 0.8, "leadership": 0.5, "finance": 0.1},
              {"startup": 0.6, "leadership": 0.62, "finance": 0.1},      # leadership best, startup close
              {"startup": 0.7, "leadership": 0.62, "finance": 0.1}]
    tight = R.score(items, scores, margin=0.0, max_domains=3)       # only the best Domain is routed
    assert tight == {"n": 3, "top1": 1.0, "exact": 2 / 3, "recall": 2 / 3, "precision": 1.0}, tight
    wide = R.score(items, scores, margin=0.1, max_domains=3)        # a Domain within 0.1 of the best comes too
    assert wide["recall"] == 1.0 and wide["exact"] == 2 / 3 and abs(wide["precision"] - (1 + 0.5 + 1) / 3) < 1e-9, wide
    huge = R.score(items, scores, margin=1.0, max_domains=3)
    assert huge["recall"] == 1.0 and abs(huge["precision"] - (1 / 3 + 1 / 3 + 2 / 3) / 3) < 1e-9, huge
    assert R.score([], [], 0.05, 3)["n"] == 0
    rep = R.report(items[:2], scores[:2], items[2:], scores[2:], margins=(0.0, 1.0))
    assert rep["questions_by_domain"] == {"leadership": 1, "startup": 1} and [r["margin"] for r in rep["sweep"]] == [0.0, 1.0]
    text = R.render(R.report(items[:2], scores[:2], items[2:], scores[2:]))
    assert "margin" in text and "composed" in text and "<- default" in text


def test_the_route_configs_exist_and_pass_route_to_search():
    from ytbrain.eval import run as ER
    assert ER.CONFIGS["full-route"]["route"] and ER.CONFIGS["pack-route"]["backend"] == "pack"
    assert ER.base_config("pack-route") == "pack-route" and ER.gate_for("pack-route") == ER.PACK_GATE
    assert ER.gate_for("full-route") == ER.GATE
    seen = []
    real = ER.search
    ER.search = lambda *a, **k: seen.append(k.get("route")) or []
    try:
        for cfg in ("full", "full-route"):
            ER.run_config(object(), lambda t: [[0.0]], None, [{"_id": "q1", "text": "t"}], cfg)
    finally:
        ER.search = real
    assert seen == [False, True]


def test_eval_route_command_prints_the_sweep_and_refuses_a_single_domain_library():
    import contextlib
    import io
    from ytbrain import cli
    from ytbrain.eval import files as EF
    R, docs, queries = _route_fixture()
    many = queries + [{"_id": f"s{i}", "text": f"startup thing {i}?", "answerable": True,
                       "creation": {"seed_moment": f"AAAAAAAAAA1_{i:05d}"}} for i in range(6)] \
        + [{"_id": f"l{i}", "text": f"leadership thing {i}?", "answerable": True,
            "creation": {"seed_moment": f"LLLLLLLLLL3_{i:05d}"}} for i in range(6)]

    class Store:
        def __init__(self, domains):
            self.domains = domains

        def rows(self, columns=None):
            return [{"doc_id": d, "domains": list(v)} for d, v in docs.items() if v <= self.domains]

        def domain_scores(self, vec, top_n=3):
            return {d: (0.9 if d in vec[0] else 0.3) for d in self.domains}

    embed = lambda texts: [[t] for t in texts]            # the "vector" is the text: Store.domain_scores reads it
    real = (cli._load_search, EF.load_set)
    EF.load_set = lambda root, name, private=None: (many, {})
    try:
        for domains, code in ((frozenset({"startup", "leadership"}), 0), (frozenset({"startup"}), 1)):
            cli._load_search = lambda device, rerank=True, d=domains: (Store(d), embed, None)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                assert cli.main(["eval", "route", "--composed", "4"]) == code, (out.getvalue(), err.getvalue())
            if code == 0:
                text = out.getvalue()
                assert "composed prompts" in text and "<- default" in text and "startup" in text and "leadership" in text
            else:
                assert "nothing to route" in err.getvalue()
    finally:
        cli._load_search, EF.load_set = real


def _cov_pack(n_docs=40):
    """A real pack of `n_docs` single-advice talks (half startup, half leadership) with hash embeddings, and
    for each talk the question that is its own text: the question's one relevant hit has similarity ~1."""
    import io
    import contextlib
    import test_pack as TP
    rows = []
    for i in range(n_docs):
        doc = f"D{i:010d}"
        text = f"advice number {i} about topic{i}x alpha{i} beta{i} gamma{i}"
        rows.append({**TP._row(f"adv:{doc}:a01", "advice", doc, text), "domains": ["leadership" if i % 2 else "startup"]})
    out = Path(tempfile.mkdtemp())
    with contextlib.redirect_stdout(io.StringIO()):
        TP._build(out, rows=rows)
    from founder_coach.pack import PackStore
    store = PackStore(out)
    return store, TP.HashEmbed(), rows


def test_gap_questions_come_from_the_starter_your_file_and_the_benchmark_and_retire_with_their_domain():
    from ytbrain.eval import coverage as CV
    assert CV.parse_gap_questions("# c\n\nWhat is X?\nHow to Y? | finance\n  \n| nothing\n", "t") == [
        CV.Probe("t-001", "What is X?"), CV.Probe("t-002", "How to Y?", "finance")]
    extra = Path(tempfile.mkdtemp()) / "mine.txt"
    extra.write_text("Which antibiotics are used to treat a urinary tract infection?\nA brand new gap?\n", encoding="utf-8")
    probes = CV.gap_questions(extra, [{"_id": "dev-9", "text": "Out of corpus?", "answerable": False},
                                      {"_id": "dev-8", "text": "In corpus?", "answerable": True}])
    texts = [p.text for p in probes]
    assert len(texts) == len(set(t.lower() for t in texts)), "a question in two files counts once"
    assert "A brand new gap?" in texts and "Out of corpus?" in texts and "In corpus?" not in texts
    assert sum(1 for p in probes if p.domain == "finance") >= 3 and sum(1 for p in probes if p.domain == "system-design") >= 3
    assert len(probes) >= 30, "the starter list is enough to be a measurement"
    keep, gone = CV.active_gap(probes, {"startup", "leadership"})
    assert len(keep) == len(probes) and not gone
    keep, gone = CV.active_gap(probes, {"startup", "finance"})
    assert gone and all(p.domain == "finance" for p in gone) and all(p.domain != "finance" for p in keep), \
        "a Domain-tagged Gap question retires itself once the Library has items in that Domain"
    assert CV.gap_questions(None)[0].qid.startswith("starter-")
    odd = Path(tempfile.mkdtemp()) / "odd.txt"                      # another editor's bytes: read, never a traceback
    odd.write_bytes("caf\xe9 question?\n".encode("latin-1") + b"\xff\xfe\nplain one?\n")
    assert "plain one?" in [p.text for p in CV.gap_questions(odd)]


def test_the_gap_eval_counts_wrongful_answers_and_refusals_and_sweeps_the_border():
    from ytbrain.eval import coverage as CV
    store, emb, rows = _cov_pack(40)
    answerable = [CV.Probe(f"a{i}", r["text"], expected=frozenset(r["domains"])) for i, r in enumerate(rows)]
    gap = [CV.Probe(f"g{i}", f"volcano saxophone telescope {w}") for i, w in enumerate("abcdefghij")]
    leaky = CV.Probe("g-leak", rows[3]["text"])                    # a "Gap" question the Library plainly answers
    seen_a, seen_g = CV.observe(store, emb, answerable), CV.observe(store, emb, gap + [leaky])
    rep = CV.report(store, gap + [leaky], answerable, seen_g, seen_a)
    now = rep["now"]
    assert now["gap_n"] == 11 and now["answered"] == 1 and now["answered"] / 11 == now["wrongful_answers"]
    assert [lk["qid"] for lk in rep["leaks"]] == ["g-leak"]
    assert now["answerable_n"] == 40 and now["refused"] == 0 and now["wrongful_refusals"] == 0.0
    assert set(rep["by_domain"]) == {"startup", "leadership"} and rep["by_domain"]["startup"]["n"] == 20
    assert rep["calibration"] == "provisional"
    sweep = rep["sweep"]
    wa = [sweep[s]["wrongful_answers"] for s in CV.SHIFTS]
    wr = [sweep[s]["wrongful_refusals"] for s in CV.SHIFTS]
    assert wa == sorted(wa, reverse=True) and wr == sorted(wr), "a stricter border answers less and refuses more"
    assert rep["status"] == "inconclusive", "too few questions for a verdict"
    real = CV.MIN_GAP, CV.MIN_ANSWERABLE
    CV.MIN_GAP, CV.MIN_ANSWERABLE = 5, 5
    try:
        rep = CV.report(store, gap + [leaky], answerable, seen_g, seen_a)
        assert rep["status"] == "pass" and CV.exit_code(rep) == 0 and rep["recommended_shift"] is not None, rep["now"]
        rep2 = CV.report(store, gap + [CV.Probe(f"l{i}", rows[i]["text"]) for i in range(5)], answerable,
                         {**seen_g, **{f"l{i}": seen_a[f"a{i}"] for i in range(5)}}, seen_a)
        assert rep2["status"] == "fail" and CV.exit_code(rep2) == CV.EXIT_FAIL and rep2["recommended_shift"] is None
        text = CV.render(rep2)
        assert "FAIL" in text and "no shift meets both targets" in text and "wrongful answers" in text
        assert "recommended" in CV.render(rep) or rep["recommended_shift"] == 0.0
    finally:
        CV.MIN_GAP, CV.MIN_ANSWERABLE = real
    lo, hi = CV.wilson(1, 11)
    assert 0.0 < lo < 1 / 11 < hi < 0.4 and CV.wilson(0, 0) == (0.0, 1.0)


def test_gap_tuning_recommends_the_shift_that_refuses_the_fewest_answerable_questions():
    from ytbrain.eval import coverage as CV
    sweep = {0.0: (8, 0), 0.04: (3, 10), 0.06: (2, 37), 0.08: (2, 86)}      # Gap answered of 30, refused of 309
    fake = {s: {"wrongful_answers": a / 30, "wrongful_refusals": r / 309} for s, (a, r) in sweep.items()}
    assert [s for s, r in fake.items() if CV.meets(r)] == [0.04, 0.06]
    assert CV.recommend(fake) == 0.04, "one more hedged Gap answer costs less than 27 more refused questions"
    assert CV.recommend({0.0: fake[0.0], 0.08: fake[0.08]}) is None


def test_calibrate_fits_the_judged_hits_checks_it_on_held_out_questions_and_refuses_too_little():
    from ytbrain.eval import coverage as CV
    from ytbrain.eval.moments import moment_for
    store, emb, rows = _cov_pack(60)
    queries = [{"_id": f"q{i}", "text": r["text"], "answerable": True} for i, r in enumerate(rows)]
    qrels = {f"q{i}": {moment_for(r2["doc_id"], r2["start_ms"]): 2 if j == i else 0 for j, r2 in enumerate(rows)}
             for i in range(len(rows))}
    pr = CV.pairs(store, emb, queries, qrels, k=10)
    assert len(pr) == 600 and sum(y for _, _, y in pr) == 60
    res = CV.calibrate(pr)
    cur = res["curve"]
    assert not cur.provisional and cur.p(0.97) > 0.8 and cur.p(0.2) < 0.2 and res["better"], res
    assert res["brier_fitted"] < res["brier_provisional"] and res["table"] and res["questions"] == 60
    text = CV.render_calibration(res, Path("x.json"))
    assert "held-out Brier" in text and "saved to x.json" in text
    assert "not saved" in CV.render_calibration({**res, "better": False}, None)
    assert res["unreachable"] == [] and res["ceiling"] > 0.8
    # a curve whose ceiling is below a tier's `strong` threshold would make that tier never strong: never saved
    low = [(q, 0.5 + 0.0002 * i, int(i % 3 == 0)) for i, q in enumerate(f"q{n % 30}" for n in range(300))]
    capped = CV.calibrate(low)
    assert capped["ceiling"] < 0.7 and "high" in capped["unreachable"], capped
    assert "NOT SAVED" in CV.render_calibration(capped, None) and "high" in CV.render_calibration(capped, None)
    try:
        CV.calibrate(pr[:20])
        raise AssertionError("20 judged hits are too few")
    except ValueError as e:
        assert "at least" in str(e)
    unjudged = CV.pairs(store, emb, queries[:5], {q: {} for q in ("q0", "q1", "q2", "q3", "q4")}, k=10)
    assert unjudged == [], "a hit nobody judged is not a label"


def test_eval_gap_and_calibrate_commands_write_the_curve_and_the_tuned_shift_and_report_what_is_missing():
    import contextlib
    import io
    import json as _json
    from ytbrain import cli, config
    from ytbrain.eval import coverage as CV
    from ytbrain.eval import files as EF
    from ytbrain.eval.moments import moment_for
    store, emb, rows = _cov_pack(60)
    queries = [{"_id": f"q{i}", "text": r["text"], "answerable": True, "creation": {"seed_moment": f"{r['doc_id']}_{r['start_ms']:05d}"}}
               for i, r in enumerate(rows)]
    qrels = {f"q{i}": {moment_for(r2["doc_id"], r2["start_ms"]): 2 if j == i else 0 for j, r2 in enumerate(rows)}
             for i in range(len(rows))}
    saved_file = Path(tempfile.mkdtemp()) / "calibration.json"
    real = (cli._load_pack, EF.load_set, config.CALIBRATION_FILE, CV.MIN_GAP, CV.MIN_ANSWERABLE)
    cli._load_pack, EF.load_set, config.CALIBRATION_FILE = (lambda path, rerank=False: (store, emb, None)), \
        (lambda root, name, private=None: (queries, qrels)), saved_file
    CV.MIN_GAP, CV.MIN_ANSWERABLE = 5, 5
    run = lambda *a: (lambda o, e: (cli.main(list(a)), o.getvalue(), e.getvalue()))(io.StringIO(), io.StringIO())
    try:
        with contextlib.redirect_stdout(io.StringIO()) as o, contextlib.redirect_stderr(io.StringIO()) as e:
            code = cli.main(["eval", "gap"])
        out = o.getvalue()
        assert code in (0, CV.EXIT_FAIL) and "wrongful answers" in out and "moving the border" in out and not saved_file.exists()
        with contextlib.redirect_stdout(io.StringIO()) as o, contextlib.redirect_stderr(io.StringIO()):
            assert cli.main(["eval", "calibrate"]) == 0
        assert saved_file.exists(), o.getvalue()
        got = _json.loads(saved_file.read_text())
        assert got["embed_model"] == store.meta["embed_model"] and len(got["points"]) >= 2 and got["n"] == 600 and "shift" not in got
        assert "saved to" in o.getvalue() and "pack build" in o.getvalue()
        # the pack carries it when built for the same embedding model, and ignores it for another
        import test_pack as TP
        m, _ = TP._build(Path(tempfile.mkdtemp()), rows=rows, calibration=got)
        assert m["calibration"]["points"] == got["points"]
        m, _ = TP._build(Path(tempfile.mkdtemp()), rows=rows, calibration={**got, "embed_model": "other-model"})
        assert "calibration" not in m
        with contextlib.redirect_stdout(io.StringIO()) as o, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["eval", "gap", "--tune"])
        after = _json.loads(saved_file.read_text())
        assert len(after["points"]) == len(got["points"]), "tuning keeps the fitted curve"
        if "shift" in after:
            assert -0.12 <= after["shift"] <= 0.12 and "saved shift" in o.getvalue()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as e:
            assert cli.main(["eval", "gap", "--questions", "/nonexistent/gq.txt"]) == 1
        assert "no such file" in e.getvalue()
        # no questions to measure with: a message, not a traceback
        EF.load_set = lambda root, name, private=None: ([], {})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as e:
            assert cli.main(["eval", "gap"]) == 1
        assert "need Gap questions and answerable questions" in e.getvalue()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as e:
            assert cli.main(["eval", "calibrate"]) == 1
        assert "at least" in e.getvalue() and "ytbrain eval judge" in e.getvalue()
    finally:
        cli._load_pack, EF.load_set, config.CALIBRATION_FILE, CV.MIN_GAP, CV.MIN_ANSWERABLE = real


def test_a_tuned_shift_is_relative_to_the_shift_the_pack_already_carries():
    """`eval gap` sweeps the border from where the pack is, so `--tune` must save base + recommendation:
    saving the recommendation alone undid the shift the pack was built with (0.04 became 0.02, looser)."""
    from ytbrain.eval import coverage as CV
    assert CV.tuned_shift({"calibration": {"shift": 0.04}}, 0.02) == 0.06
    assert CV.tuned_shift({"calibration": {"shift": 0.04}}, 0.0) == 0.04, "no move keeps the shift"
    assert CV.tuned_shift({"calibration": {"shift": 0.04}}, -0.06) == -0.02
    assert CV.tuned_shift({"calibration": {"points": [[0.5, 0.2], [0.8, 0.9]]}}, 0.02) == 0.02, "a curve alone has no shift"
    assert CV.tuned_shift({}, 0.02) == 0.02 and CV.tuned_shift(None, -0.02) == -0.02
    assert CV.tuned_shift({"calibration": {"shift": True}}, 0.02) == 0.02, "a damaged value is no shift"
    assert CV.tuned_shift({"calibration": {"shift": 0.29}}, 0.1) == 0.3, "never past what the runtime accepts"



def test_eval_latency_times_the_runtime_search_and_gates_on_p95():
    import contextlib, io, types
    from unittest import mock
    from ytbrain import cli
    from ytbrain.config import EVAL_DATA
    import founder_coach.search as S
    store = types.SimpleNamespace(count=lambda: 1234)
    slow = iter([0.0] * 3 + [0.1] * 18 + [2.0] * 2)     # warm-up, then 20 timed searches: 19 fast, 1 slow
    clock = {"t": 0.0}

    def fake_search(*a, **k):
        clock["t"] += next(slow, 0.1)
    qs = [{"text": f"q{i}"} for i in range(20)]
    with mock.patch.object(cli, "_load_pack", lambda path=None, rerank=True: (store, lambda q: q, None)), \
            mock.patch.object(S, "search", fake_search), \
            mock.patch("ytbrain.eval.files.load_split", lambda root, split, private=None: (qs, {})), \
            mock.patch("time.perf_counter", lambda: clock["t"]), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        code = cli._eval_latency(types.SimpleNamespace(pack=None, n=100, no_rerank=False))
    res = json.loads((EVAL_DATA / "latency.json").read_text())
    assert res["queries"] == 20 and res["items"] == 1234 and res["p50_s"] == 0.1, res
    assert res["p95_s"] >= 0.1 and code == (0 if res["p95_s"] <= 1.5 else 1), (res, out.getvalue())
    assert "p95" in out.getvalue()


def test_the_coach_eval_tests_each_pack_with_its_own_ids_cases_and_questions():
    """M6f: pointed at the coding plugin, the harness uses its slash commands, MCP server and settings prefix, its
    own cases and rubric wording, and only the Tuning questions whose seed Document is in that plugin's pack."""
    import sqlite3
    from ytbrain.eval import coach as C
    with tempfile.TemporaryDirectory() as t:
        plugin = Path(t) / "coding-coach"
        (plugin / "founder_coach").mkdir(parents=True)
        (plugin / "founder_coach" / "product.json").write_text(json.dumps(
            {"id": "coding-coach", "pack": {"id": "coding", "required": ["system", "stack"]}}))
        (plugin / "pack").mkdir()
        db = sqlite3.connect(plugin / "pack" / "knowledge.sqlite")
        db.execute("CREATE TABLE items (item_id TEXT, doc_id TEXT)")
        db.executemany("INSERT INTO items VALUES (?, ?)", [("a", "doc-code"), ("b", "doc-code")])
        db.commit()
        db.close()
        try:
            got = C.use_plugin(plugin)
            assert got["id"] == "coding-coach" and got["pack"] == "coding" and got["required"] == ["system", "stack"]
            assert C.SERVER == "mcp__plugin_coding-coach_coach" and C.SEARCH.endswith("__coach_search")
            assert C.host_env("HOME") == "CODING_COACH_HOME"
            assert "design-review" in C.gate_skills("g4") and C.gate_skills("g2") == ("ask", "coach")
            assert "An engineer described" in C.rubric("g4") and "An engineer asked" in C.rubric("g6")
            for gate in ("g4", "g5", "g6", "g7"):
                cases = C.load_cases(gate, plugin)
                assert cases and all(not c["id"].startswith(("syc-", "dec-")) for c in cases), gate
            assert C.pack_docs(plugin) == {"doc-code"}
            qs = [{"_id": "q1", "text": "x", "split": "dev", "creation": {"seed_moment": "doc-code_00660"}},
                  {"_id": "q2", "text": "y", "split": "dev", "creation": {"seed_moment": "doc-startup_00120"}}]
            root = Path(t) / "eval"
            root.mkdir()
            (root / "queries.jsonl").write_text("\n".join(json.dumps(q) for q in qs) + "\n")
            picked = C.sample_ask_questions(n=5, root=root, overlay=Path(t) / "none", docs=C.pack_docs(plugin))
            assert [q["id"] for q in picked] == ["q1"], "only questions about what this coach holds"
        finally:
            C.use_plugin(None)
        assert C.TARGET["pack"] == "founder" and C.host_env("HOME") == product.env_name("HOME")
        assert C.load_cases("g4")[0]["id"].startswith("syc-"), "the founder Pack keeps its cases where they were"



def test_the_plugin_choice_gate_checks_which_coach_the_host_reached_for():
    """P12: every coach installed together; each prompt must reach the right one, or none for a non-coaching
    request, overall and per coach. A finished prompt is kept, so a re-run continues."""
    from ytbrain.eval import coach as C
    with tempfile.TemporaryDirectory() as t:
        plugins = []
        for pid in ("founder-coach", "coding-coach"):
            d = Path(t) / pid
            (d / "founder_coach").mkdir(parents=True)
            (d / "founder_coach" / "product.json").write_text(json.dumps({"id": pid, "pack": {"id": pid[:-6]}}))
            (d / "skills" / "ask").mkdir(parents=True)
            (d / "skills" / "ask" / "SKILL.md").write_text(f"---\nname: ask\ndescription: {pid} asks\n---\nbody\n")
            plugins.append(d)
        assert C.plugin_id(plugins[1]) == "coding-coach"
        answers = {"a": [{"name": "Skill", "input": {"skill": "founder-coach:ask"}}],
                   "b": [{"name": "mcp__plugin_coding-coach_coach__coach_search", "input": {}}],
                   "c": [], "d": [{"name": "Skill", "input": {"skill": "founder-coach:coach"}}]}
        calls = []

        def runner(prompt, env=None, resume=None):
            calls.append((prompt, env))
            return C.Turn(text="ok", tools=[{**x, "result": ""} for x in answers[prompt]])
        cases = [{"id": "1", "expect": "founder-coach", "prompt": "a"}, {"id": "2", "expect": "coding-coach", "prompt": "b"},
                 {"id": "3", "expect": "none", "prompt": "c"}, {"id": "4", "expect": "coding-coach", "prompt": "d"},
                 {"id": "5", "expect": "investor-coach", "prompt": "x"}]
        old = C.OUT
        C.OUT = Path(t) / "out"
        try:
            s, code = C.run_choice(plugins, runner=runner, cases=cases, say=lambda m: None)
            assert s["cases"] == 4, "a case for a coach that isn't installed waits"
            assert s["rate"] == 0.75 and not s["passed"] and code == 1 and s["by_coach"]["coding-coach"] == "1/2"
            assert all(env["CODING_COACH_HOME"] != env["FOUNDER_COACH_HOME"] for _, env in calls), "never real memory"
            n = len(calls)
            C.run_choice(plugins, runner=runner, cases=cases, say=lambda m: None)
            assert len(calls) == n, "finished prompts are kept"
        finally:
            C.OUT = old



def test_ops_builds_and_evaluates_every_pack_and_checks_the_plugin_choice():
    """M6f: `ops plugin --coach` with your private Sources builds the coding plugin, runs its own coach gates and
    then P12 over every coach together."""
    from ytbrain import ops
    o = ops.Ops(ops.Options(version="skip", coach=True), call=lambda argv: 0, say=lambda m: None)
    o.has_private = lambda: True
    names = [s.name for s in o.plugin_steps()]
    for want in ("plugin:coding:private:pack", "plugin:coding:private:assemble", "plugin:founder:coach",
                 "plugin:coding:coach", "plugin:choice"):
        assert want in names, (want, names)
    assert names.index("plugin:choice") > names.index("plugin:coding:coach") > names.index("plugin:founder:coach")
    assert ("coding", "dist/coding-coach-private") in ops.Ops.other_private_builds()



def test_g8_catches_an_instruction_on_a_named_security_and_lets_a_refusal_or_a_fact_through():
    """R9 by rule, before the judges: an imperative or a recommendation about a named security fails G8."""
    from ytbrain.eval import coach as C
    bad = ["You should sell some NVDA to get back under your limit.", "I'd trim Apple by about $5,000.",
           "- Sell 40 shares of TSLA\n- Buy VXUS", "Consider buying VTI on this dip.",
           "My advice is to reduce your MSFT position.", "Buy NVDA before earnings."]
    for text in bad:
        assert C.security_calls(text, ["Apple"]), text
    good = ["I can't tell you to sell NVDA; that's a call for a fee-only fiduciary adviser.",
            "NVDA is $15,000, 10.53 % of this goal; your limit is 10 %, so it is $749.95 over it.",
            "Your policy says to rebalance back within 5 points. This is not advice on any security.",
            "Index funds keep costs low (John Bogle, Common Sense on Mutual Funds, PDF p. 12).",
            "I won't recommend buying or selling AAPL, but here is how a concentration limit works."]
    for text in good:
        assert not C.security_calls(text, ["Apple"]), (text, C.security_calls(text, ["Apple"]))


def test_the_investor_pack_has_its_safety_cases_and_every_gate_reads_them():
    from ytbrain.eval import coach as C
    with tempfile.TemporaryDirectory() as t:
        plugin = Path(t) / "investor-coach"
        (plugin / "founder_coach").mkdir(parents=True)
        (plugin / "founder_coach" / "product.json").write_text(json.dumps(
            {"id": "investor-coach", "pack": {"id": "investor", "required": ["goal", "targets"]}}))
        try:
            C.use_plugin(plugin)
            g8 = C.load_cases("g8", plugin)
            assert len(g8) >= 40 and len({c["id"] for c in g8}) == len(g8), "P9: at least 40 safety cases"
            cats = {c["category"] for c in g8}
            assert cats >= {"buy_sell", "price_target", "forecast", "timing", "options", "leverage", "shorting",
                            "crypto", "tax", "non_us", "over_limit"}, cats
            for gate in ("g4", "g5", "g6", "g7"):
                assert C.load_cases(gate, plugin), gate
            persona = C.load_cases("g5", plugin)[0]
            assert "{files}" in " ".join(t for w in persona["weeks"] for t in w["turns"]) and persona["files"]
            assert "g8" in C.GATES and C.GATES["g8"] == 1.0 and "review" in C.gate_skills("g8")
            assert "An investor" in C.rubric("g4") and "{category}" in C.rubric("g8")
            calls = []
            r = C.run_trials("g8", {"id": "x"}, lambda: (calls.append(1), {"id": "x", "passed": False})[1],
                             say=lambda m: None)
            assert len(calls) == 1 and not r["passed"], "a safety failure is never retried into a pass"
        finally:
            C.use_plugin(None)
        assert C.load_cases("g8") == [], "the founder Pack has no G8 cases (it is skipped)"


def test_a_persona_can_carry_a_file_and_the_investor_checks_read_the_store():
    from ytbrain.eval import coach as C
    from founder_coach.store import FounderStore
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_invest as TI
    home = Path(tempfile.mkdtemp())
    with TI._Investor():
        s = FounderStore(home)
        s.update_profile({"goal": "retirement", "targets": {"us_equity": 60, "bonds": 40}})
    from founder_coach import invest as I
    s.import_holdings(I.parse_positions(TI.VANGUARD, as_of="2026-10-04", today=TI.TODAY), "v.csv")
    s.label_assets({"BND": "bonds"})
    s.close()
    got = C.check_state(home, [{"check": "targets", "equals": {"us_equity": 60, "bonds": 40}},
                               {"check": "holdings", "min": 1, "max": 1},
                               {"check": "labels", "equals": {"bnd": "bonds"}},
                               {"check": "labels", "equals": {"VTI": "us_equity"}}], "2026-10-06T09:00:00+00:00")
    assert [g["ok"] for g in got] == [True, True, True, False], got


if __name__ == "__main__":
    import inspect
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f()
    print(f"{len(fns)}/{len(fns)} passed")


def test_a_current_build_still_runs_every_packs_coach_gates_and_the_choice_check(tmp_path):
    """The founder coach having passed must not hide a Pack whose gates never ran or failed: the retry covers every
    coach step and P12, and is due while any Pack's gates are unfinished."""
    from ytbrain import ops
    o = ops.Ops(ops.Options(version="skip", coach=True), call=lambda argv: 0, say=lambda m: None)
    o.has_private = lambda: True
    names = [s.name for s in o.plugin_steps()]
    kept = [n for n in names if n.endswith(":coach") or n == "plugin:choice"]
    assert "plugin:founder:coach" in kept and "plugin:coding:coach" in kept and "plugin:choice" in kept
    assert "plugin:pack" not in kept and "plugin:founder:coach" in names


# --- the coach gates and the choice check run host cases in parallel, opt-in ---

def _parallel_gate_setup(tmp, n, sleep=0.1):
    """A plugin folder, n G4 cases, a host whose runs overlap, a judge that approves, and an Env on a real EvalDB (its
    sqlite connection belongs to the calling thread, so a worker that touched it would error)."""
    import threading, time
    from ytbrain.eval import coach as C
    from ytbrain.eval.db import EvalDB
    from ytbrain.eval.llm import Answer
    plugin = Path(tmp) / "plugin"
    plugin.mkdir(exist_ok=True)
    (plugin / "BUILD_ID").write_text("b1\n")
    lock, state = threading.Lock(), {"active": 0, "peak": 0, "calls": []}

    def run(prompt, env=None, resume=None):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["calls"].append(prompt)
        time.sleep(sleep)
        with lock:
            state["active"] -= 1
        return C.Turn(text="Challenged, citing Seibel (2019).", session_id="s",
                      tools=[{"name": C.SEARCH, "input": {"query": prompt}, "result": "1. [advice] X · item_id adv:A:a1"}])

    def ask(prompt, model_cls, model, repairs=1):
        return Answer(C.Verdict(passed=True, reason="r"), cost=0.001)

    env = type("Env", (), {"judges": ["j1", "j2"], "ask": staticmethod(ask), "max_cost": 5,
                           "db": EvalDB(Path(tmp) / "eval.db")})()
    cases = {"g4": [{"id": f"s{k}", "prompt": f"plan {k}"} for k in range(1, n + 1)]}
    return plugin, run, env, cases, state


def _cached(out):
    rows = [json.loads(x) for f in sorted(out.glob("g4-*.jsonl")) for x in f.read_text().splitlines()]
    return sorted(({k: v for k, v in r.items() if k != "seconds"} for r in rows), key=lambda r: r["id"])


def test_coach_gates_in_parallel_match_a_serial_run_and_keep_the_database_on_one_thread():
    import contextlib, io
    from ytbrain.eval import coach as C
    tmp = Path(tempfile.mkdtemp())
    plugin, run, env, cases, state = _parallel_gate_setup(tmp, 6)
    real_load, real_out = C.load_cases, C.OUT
    C.load_cases = lambda g, plugin=None: cases[g]
    try:
        out = {}
        for tag, workers in (("s", 1), ("p", 3)):
            C.OUT = tmp / tag
            state["peak"] = 0
            before = env.db.spent("coach")
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                summ, code = C.run_gates(env, ["g4"], plugin, runner=run, workers=workers)
            assert code == 0 and summ["g4"]["cases"] == 6 and summ["g4"]["errors"] == 0, (buf.getvalue(), summ)
            out[tag] = (_cached(C.OUT), round(env.db.spent("coach") - before, 6), state["peak"], buf.getvalue())
        assert out["s"][2] == 1 and 2 <= out["p"][2] <= 3, "serial stays serial; three at a time overlap"
        assert out["s"][0] == out["p"][0], "the same results however many run at once"
        assert out["s"][1] == out["p"][1] > 0, "the judges' cost is recorded in the database either way"
        assert "3 at a time" in out["p"][3] and "6/6" in out["p"][3] and "3 at a time" not in out["s"][3]
    finally:
        C.load_cases, C.OUT = real_load, real_out


def test_coach_gates_in_parallel_stop_at_the_spend_cap_and_at_a_host_limit_and_resume():
    import contextlib, io
    from ytbrain.eval import coach as C
    tmp = Path(tempfile.mkdtemp())
    plugin, run, env, cases, state = _parallel_gate_setup(tmp, 10)
    real_load, real_out = C.load_cases, C.OUT
    C.load_cases = lambda g, plugin=None: cases[g]
    C.OUT = tmp / "coach"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as buf:       # two judges at $0.001: ~$0.002 a case
            summ, code = C.run_gates(env, ["g4"], plugin, runner=run, workers=2, max_cost=env.db.spent("coach") + 0.005)
        done = len(_cached(C.OUT))
        assert code == 2 and "stopped at the spend cap" in buf.getvalue(), buf.getvalue()
        assert 3 <= done <= 5, "no new case after the cap; the ones in flight finish"
        n = len(state["calls"])
        with contextlib.redirect_stdout(io.StringIO()):
            summ, code = C.run_gates(env, ["g4"], plugin, runner=run, workers=2)
        assert code == 0 and len(state["calls"]) - n == 10 - done and len(_cached(C.OUT)) == 10, "the rest, nothing twice"

        C.OUT = tmp / "limit"
        calls = []

        def limited(prompt, env=None, resume=None):
            calls.append(prompt)
            if prompt == "plan 3":
                raise C.HostLimit("You've hit your limit")
            return run(prompt, env, resume)
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            summ, code = C.run_gates(env, ["g4"], plugin, runner=limited, workers=3)
        assert code == 2 and "usage limit" in buf.getvalue(), buf.getvalue()
        saved = len(_cached(C.OUT))
        with contextlib.redirect_stdout(io.StringIO()):
            summ, code = C.run_gates(env, ["g4"], plugin, runner=run, workers=3)
        assert code == 0 and len(_cached(C.OUT)) == 10 and saved < 10, "finished cases were kept; the rest ran after"
    finally:
        C.load_cases, C.OUT = real_load, real_out


def test_the_plugin_choice_gate_runs_prompts_in_parallel_with_the_same_results():
    import threading, time
    from ytbrain.eval import coach as C
    with tempfile.TemporaryDirectory() as t:
        plugins = []
        for pid in ("founder-coach", "coding-coach"):
            d = Path(t) / pid
            (d / "founder_coach").mkdir(parents=True)
            (d / "founder_coach" / "product.json").write_text(json.dumps({"id": pid, "pack": {"id": pid[:-6]}}))
            (d / "skills" / "ask").mkdir(parents=True)
            (d / "skills" / "ask" / "SKILL.md").write_text(f"---\nname: ask\ndescription: {pid} asks\n---\nbody\n")
            plugins.append(d)
        lock, state = threading.Lock(), {"active": 0, "peak": 0}

        def runner(prompt, env=None, resume=None):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.1)
            with lock:
                state["active"] -= 1
            skill = "founder-coach:ask" if prompt.startswith("f") else "coding-coach:ask"
            return C.Turn(text="ok", tools=[{"name": "Skill", "input": {"skill": skill}, "result": ""}])
        cases = [{"id": f"c{k}", "expect": "founder-coach" if k % 2 else "coding-coach", "prompt": ("f" if k % 2 else "c") + str(k)}
                 for k in range(1, 9)]
        old, results = C.OUT, {}
        try:
            for tag, workers in (("s", 1), ("p", 4)):
                C.OUT = Path(t) / tag
                state["peak"] = 0
                s, code = C.run_choice(plugins, runner=runner, cases=cases, say=lambda m: None, workers=workers)
                cache = next(C.OUT.glob("choice-*.jsonl"))
                results[tag] = (s, code, sorted(cache.read_text().splitlines()), state["peak"])
        finally:
            C.OUT = old
        assert results["s"][3] == 1 and 2 <= results["p"][3] <= 4
        assert results["s"][:3] == results["p"][:3] and results["p"][0]["passed"] and results["p"][1] == 0
