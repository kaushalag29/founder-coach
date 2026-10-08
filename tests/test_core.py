"""Unit tests for the parts that must be correct and are cheap to check.

Run:  python3 tests/test_core.py      (stdlib only; schema tests auto-skip)
 or:  pytest tests/

Everything here runs offline against synthetic fixtures. The tests that matter
most are the ones guarding invariants that would fail silently in production:
rolling-caption dedup, evidence grounding in both directions, per-stage
checkpoint independence, series precedence, and deterministic serialization.
"""
import json
import os
os.environ["YTBRAIN_DOTENV"] = "0"          # hermetic: never read the developer's .env (keys, backend)
os.environ.setdefault("YTBRAIN_SOURCES_FILE", os.path.join(__import__("tempfile").mkdtemp(prefix="ytbrain-nosources-"), "sources.yaml"))   # hermetic: never read your sources.yaml (the file does not exist)
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="ytbrain-test-")
os.environ["YTBRAIN_ROOT"] = _TMP          # must precede any ytbrain import

from ytbrain import captions, fetch, pages, verify            # noqa: E402
from ytbrain.config import RAW                                 # noqa: E402
from ytbrain.lock import LockBusy, exclusive                   # noqa: E402
from ytbrain.manifest import Manifest, StageState              # noqa: E402

SRT = """1
00:00:00,000 --> 00:00:02,500
so the first thing

2
00:00:02,500 --> 00:00:05,000
<c>you should do</c> is talk to users

3
00:00:05,000 --> 00:00:07,000
every single week
"""


# --- caption parsing -------------------------------------------------------


def _skipped(why: str) -> None:
    """A test that can't run here says so; CI sets REQUIRE_ALL_TESTS=1 and fails instead."""
    if os.environ.get("REQUIRE_ALL_TESTS") == "1":
        raise AssertionError(f"skipped in CI: {why}")
    print(f"    (skipped: {why})")


def test_the_suites_never_see_a_real_sources_yaml():
    """A configured machine must fail like CI: a test that needs sources.yaml writes its own."""
    from ytbrain import cli
    assert not cli.SOURCES.exists(), f"{cli.SOURCES} exists: tests would read your real sources.yaml"

def test_a_unit_too_long_for_the_embedding_window_is_cut_into_the_fewest_pieces_that_fit():
    from ytbrain.config import EMBED_MAX_SEQ
    from ytbrain.index import MAX_UNIT_WORDS, chunk_transcript, chunked_differently, split_long_units
    assert MAX_UNIT_WORDS * 1.6 + 60 * 1.6 <= EMBED_MAX_SEQ and EMBED_MAX_SEQ >= 1024, "never below 1024"
    long = " ".join(f"w{i}" for i in range(1946))                     # one article paragraph of 1946 words
    units = [{"start_ms": 21, "end_ms": 21, "text": "a short lead-in paragraph"},
             {"start_ms": 22, "end_ms": 22, "text": long},
             {"start_ms": 23, "end_ms": 23, "text": "and a closing line"}]
    pieces = split_long_units(units)
    assert [u.get("part", 0) for u in pieces] == [0, 1, 2, 3, 4, 0]
    assert max(len(u["text"].split()) for u in pieces) <= MAX_UNIT_WORDS
    assert " ".join(u["text"] for u in pieces[1:5]) == long, "nothing lost or reordered"
    assert all(u["start_ms"] == 22 for u in pieces[1:5]), "same position: Citations and Moments are unchanged"
    chunks = chunk_transcript({"doc_id": "w-x", "utterances": units})
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids)) and all(len(c.text.split()) <= MAX_UNIT_WORDS + 60 for c in chunks)
    assert chunked_differently(units) and not chunked_differently(units[:1])


def test_a_short_chunk_is_never_carried_whole_into_the_next_one():
    """A short chunk closed by a long unit used to be carried entirely as overlap, so the next
    Passage started at the same unit and both got one id (one overwrote the other)."""
    from ytbrain.index import chunk_transcript
    from ytbrain.knowledge.items import build_items
    units = [{"start_ms": 1000, "end_ms": 2000, "text": "short opening"},
             {"start_ms": 3000, "end_ms": 9000, "text": " ".join(["word"] * 420)},
             {"start_ms": 9000, "end_ms": 9500, "text": "tail"}]
    chunks = chunk_transcript({"doc_id": "abcdefghijk", "utterances": units})
    assert [c.start_ms for c in chunks] == [1000, 3000, 9000]
    rec = {"doc_id": "abcdefghijk", "title_raw": "t", "highlights": [], "advice_atoms": []}
    ids = [i["item_id"] for i in build_items(rec, {"utterances": units}) if i["kind"] == "passage"]
    assert len(ids) == len(set(ids)) == 3


def test_parse_srt_basic():
    evs = captions.parse_srt(SRT)
    assert len(evs) == 3
    assert evs[0].start_ms == 0 and evs[0].dur_ms == 2500
    assert evs[1].text == "you should do is talk to users"     # inline tags stripped


def test_parse_srt_tolerates_bom_crlf_and_vtt_timing():
    raw = "﻿1\r\n00:00:01.000 --> 00:00:02.000\r\nhello\r\n\r\n"
    evs = captions.parse_srt(raw)
    assert len(evs) == 1 and evs[0].start_ms == 1000 and evs[0].text == "hello"


def test_dedup_is_noop_on_clean_srt():
    """The whole reason srt replaced json3 — and why keeping dedup costs nothing."""
    evs = captions.parse_srt(SRT)
    assert [e.text for e in captions.dedup_rolling(evs)] == [e.text for e in evs]


def test_dedup_collapses_rolling_captions():
    evs = [captions.Event(0, 900, "so the first thing"),
           captions.Event(900, 900, "so the first thing you should do"),
           captions.Event(1800, 900, "you should do is talk to users")]
    assert [e.text for e in captions.dedup_rolling(evs)] == [
        "so the first thing", "you should do", "is talk to users"]


def test_dedup_collapses_exact_repeat():
    evs = [captions.Event(0, 500, "same line"), captions.Event(500, 500, "same line")]
    assert [e.text for e in captions.dedup_rolling(evs)] == ["same line"]


def test_utterance_splits_on_pause():
    evs = [captions.Event(0, 500, "first part"), captions.Event(9000, 500, "after a pause")]
    utts = captions.merge_utterances(evs, gap_ms=1200)
    assert len(utts) == 2 and utts[1].start_ms == 9000


def test_utterances_keep_cue_timestamps():
    utts = captions.merge_utterances(captions.dedup_rolling(captions.parse_srt(SRT)))
    assert utts[0].start_ms == 0 and utts[0].end_ms == 7000
    assert "talk to users" in utts[0].text


# --- caption selection (the bug that filenames cannot express) --------------

def _write_info(doc_id: str, info: dict) -> Path:
    p = RAW / f"{doc_id}.info.json"
    p.write_text(json.dumps(info))
    return p


def test_human_captions_win_over_auto():
    """yt-dlp writes both tracks as <id>.en.srt, so only info.json can decide."""
    (RAW / "h1.en.srt").write_text(SRT)
    p = _write_info("h1", {"subtitles": {"en": [{}]}, "automatic_captions": {"en": [{}]}})
    sel = fetch.select_caption("h1", p)
    assert sel["caption_kind"] == "human"


def test_falls_back_to_auto_when_no_human_track():
    (RAW / "a1.en.srt").write_text(SRT)
    p = _write_info("a1", {"subtitles": {}, "automatic_captions": {"en": [{}]}})
    assert fetch.select_caption("a1", p)["caption_kind"] == "auto"


def test_advertised_but_missing_file_is_reported_not_silent():
    p = _write_info("m1", {"subtitles": {"en": [{}]}})
    assert fetch.select_caption("m1", p)["caption_kind"] == "missing_file"


def test_plain_en_preferred_over_regional_variants():
    assert fetch._english_langs({"en-US": [], "en": [], "en-orig": []})[0] == "en"


def test_private_video_is_permanent_but_rate_limit_is_not():
    assert fetch.permanent_failure("ERROR: [youtube] 3yAoITAXKis: Private video\n") == "Private video"
    assert fetch.permanent_failure("ERROR: [youtube] x: Video unavailable. This video is no longer available")
    rl = "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"
    assert fetch.permanent_failure(rl) is None
    assert fetch.failure_reason(rl) == "rate limited, HTTP 429"
    assert fetch.failure_reason("ERROR: [youtube] abc: Sign in to confirm your age") == "Sign in to confirm your age"


def test_ytdlp_args_enable_a_found_js_runtime_and_hide_proxy_credentials():
    orig_which, orig_proxy = fetch.shutil.which, fetch.YTDLP_PROXY
    try:
        fetch.js_runtime.cache_clear()
        fetch.shutil.which = lambda exe: "/usr/bin/node" if exe == "node" else None
        assert fetch.js_runtime() == ("node", ("--js-runtimes", "node"))   # Deno is yt-dlp's only default
        fetch.js_runtime.cache_clear()
        fetch.shutil.which = lambda exe: "/opt/homebrew/bin/deno" if exe == "deno" else None
        assert fetch.js_runtime() == ("deno", ())
        fetch.YTDLP_PROXY = "http://alice:s3cret@gate.example.com:7000"
        assert fetch.ytdlp_common_args()[-2:] == ["--proxy", fetch.YTDLP_PROXY]
        report = "\n".join(fetch.environment_report())
        assert "gate.example.com:7000" in report and "s3cret" not in report and "alice" not in report
        fetch.js_runtime.cache_clear()
        fetch.shutil.which = lambda exe: None
        assert any("no JavaScript runtime" in l for l in fetch.environment_report())
    finally:
        fetch.shutil.which, fetch.YTDLP_PROXY = orig_which, orig_proxy
        fetch.js_runtime.cache_clear()


def test_sync_backfill_retries_only_retryable_failures_slowly():
    try:
        from ytbrain import cli
        from ytbrain.config import MANIFEST_DB
    except ImportError:
        _skipped("optional dependency not installed")
        return
    import io, contextlib, types
    m = Manifest(MANIFEST_DB)
    for d, err in (("bf1", "ERROR: HTTP Error 429: Too Many Requests"),
                   ("bf2", "ERROR: [youtube] bf2: Private video"),
                   ("bf3", "ERROR: HTTP Error 429: Too Many Requests")):
        m.upsert_document(d, "bfsrc", title=d)
        m.mark(StageState(d, "fetch", "failed", error=err))
    calls, sleeps = [], []
    def fake_fetch(doc_id, sub_langs=None, sleep=1.5, sleep_subtitles=0):
        calls.append((doc_id, sleep_subtitles))
        return {"doc_id": doc_id, "returncode": 0, "stderr_tail": "", "info_path": None,
                "caption_path": None, "caption_kind": "none", "caption_lang": None}
    orig = (cli.fetch.fetch_captions, cli.time.sleep, cli._enabled_sources, cli.SOURCES)
    cli.fetch.fetch_captions, cli.time.sleep = fake_fetch, sleeps.append
    cli._enabled_sources = lambda: [{"id": "bfsrc", "name": "S", "playlist_id": "PLx"}]
    cfg = Path(tempfile.mkdtemp()) / "sources.yaml"          # never the developer's own sources.yaml
    cfg.write_text("sources:\n- id: bfsrc\n  name: S\n  playlist_id: PLx\n")
    cli.SOURCES = cfg
    try:
        args = types.SimpleNamespace(backfill=True, per_hour=10, limit=0, force=False,
                                     reconcile=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert cli.cmd_sync(args) == 0
        ids = [c[0] for c in calls]
        assert "bf2" not in ids and {"bf1", "bf3"} <= set(ids)       # private stays settled
        assert all(sub >= cli.YT_BACKFILL_SLEEP_SUBTITLES for _, sub in calls)   # backfill pause
        assert sleeps and max(sleeps) > 300                          # ~6 min apart at 10/hour
        assert m.stage_status("bf1", "fetch") == "skipped"
        assert "sync --backfill" in out.getvalue()
    finally:
        cli.fetch.fetch_captions, cli.time.sleep, cli._enabled_sources, cli.SOURCES = orig


def test_default_sub_langs_exclude_machine_translations():
    import re
    pats = fetch.DEFAULT_SUB_LANGS.split(",")
    want = lambda k: any(re.fullmatch(p, k, re.I) for p in pats)
    assert all(want(k) for k in ("en", "en-orig", "en-US", "en-GB"))
    assert not any(want(k) for k in ("en-en", "en-tr-TW7qhz_uLiI", "en-en-US", "fr"))


def test_uploader_chapters_parsed_when_present():
    p = _write_info("c1", {"chapters": [{"title": "Intro", "start_time": 0, "end_time": 90}]})
    chs = fetch.uploader_chapters(p)
    assert chs and chs[0]["start_ms"] == 0 and chs[0]["source"] == "uploader"


def test_no_uploader_chapters_returns_empty():
    assert fetch.uploader_chapters(_write_info("c2", {})) == []


# --- grounding -------------------------------------------------------------

def test_evidence_fuzzy_match_accepts_paraphrase():
    """Exact matching would reject this; unpunctuated ASR makes it the norm."""
    utts = [{"start_ms": 12000, "end_ms": 20000,
             "text": "the single most important thing is to talk to your users every week"}]
    m = verify.find_evidence("the most important thing is to talk to your users every week", utts)
    assert m.ok and m.start_ms == 12000


def test_evidence_rejects_invention():
    utts = [{"start_ms": 0, "end_ms": 5000, "text": "raise a seed round from angels"}]
    assert not verify.find_evidence("hire a VP of Sales before product-market fit", utts).ok


def test_evidence_ignores_fillers_the_model_dropped():
    utts = [{"start_ms": 5000, "end_ms": 9000, "text":
             "um and so pushing that decision into the agent itself rather than the harness "
             "has been a really um really powerful thing similarly um allowing the agent"}]
    m = verify.find_evidence("pushing that decision into the agent itself rather than the "
                             "harness has been a really powerful thing", utts)
    assert m.ok and m.start_ms == 5000


def test_evidence_ellipsis_pieces_must_each_be_found():
    utts = [{"start_ms": 1000, "end_ms": 4000, "text": "the number of hard tech companies in the batch went from 8 to 20"},
            {"start_ms": 4000, "end_ms": 8000, "text": "lots of other words in between here about nothing"},
            {"start_ms": 8000, "end_ms": 12000, "text": "things that actually touch atoms and not just bits"}]
    ok = verify.find_evidence("the number of hard tech companies in the batch went from 8 to 20... "
                              "things that actually touch atoms and not just bits", utts)
    assert ok.ok and ok.start_ms == 1000
    # a real first half cannot smuggle in an invented second half
    bad = verify.find_evidence("the number of hard tech companies in the batch went from 8 to 20... "
                               "you should always hire a VP of sales first", utts)
    assert not bad.ok


def test_verify_keeps_parenthetical_prose_and_drops_only_short_annotations():
    """Found in Paul Graham's essays: whole paragraphs written in parentheses. Stripping every
    "(...)" as if it were a caption annotation erased them, so quotes from them were
    "unmatched". Only short annotations like (laughs) or [music] are dropped."""
    para = ("(The reason I say short-term greed is that the underlying problem with the labels and "
            "studios is that the people who run them are driven by bonuses rather than equity.)")
    utts = [{"text": "Intro sentence about something else entirely here.", "start_ms": 1, "end_ms": 1},
            {"text": para, "start_ms": 2, "end_ms": 2}]
    m = verify.find_evidence("the underlying problem with the labels and studios is that the people who "
                             "run them are driven by bonuses rather than equity.", utts)
    assert m.ok and m.start_ms == 2, m
    assert verify.normalize("so (laughs) it's [music] really good") == ["so", "it's", "really", "good"]
    assert "reason" in verify.normalize(para)


def test_verify_sets_status_and_derives_timestamp():
    utts = [{"start_ms": 30000, "end_ms": 38000, "text": "launch early and iterate with real users"},
            {"start_ms": 38000, "end_ms": 46000, "text": "charge money for your product from day one"}]
    rec = {"highlights": [
               {"text": "Launch early", "evidence_span": "launch early and iterate with real users"},
               {"text": "Charge money", "evidence_span": "charge money for your product from day one"}],
           "advice_atoms": [
               {"atom_id": "a1", "text": "x", "evidence_span": "an invented sentence about tax law"}]}
    report = verify.verify_record(rec, utts)
    assert report["checked"] == 3 and report["failed"] == 1
    assert rec["highlights"][0]["timestamp_ms"] == 30000      # from transcript, not model
    assert rec["extraction_meta"]["validation_status"] == "flagged"


def test_verify_all_unsupported_is_failed():
    utts = [{"start_ms": 0, "end_ms": 5000, "text": "raise a seed round from angels"}]
    rec = {"highlights": [{"text": "x", "evidence_span": "hire a chief revenue officer now"}]}
    verify.verify_record(rec, utts)
    assert rec["extraction_meta"]["validation_status"] == "failed"


def test_evidence_accepts_a_tidied_quote_but_not_an_invented_one():
    """Models drop repeated words and verbal tics when quoting. Word-set Jaccard over
    a quote-sized window punished that; containment in a slightly longer window
    accepts it, while words the speaker never said still fail."""
    utts = [{"start_ms": 1000, "end_ms": 7000, "text": "welcome back everyone to the show"},
            {"start_ms": 7000, "end_ms": 15000, "text":
             "and so basically we we started sort of a running log you know of all the "
             "little issues in the whole company honestly so this is kind of like a "
             "psychologically safe place you know for people to actually think big"}]
    tidied = ("we started a running log of all the issues in the company so this is "
              "a psychologically safe place for people to think big")
    flat = verify._flatten(utts)
    assert verify._best_window(verify.normalize(tidied), flat, 0.7).score < 0.7   # Jaccard alone fails
    m = verify.find_evidence(tidied, utts)
    assert m.ok and m.score >= 0.9 and m.start_ms == 7000
    invented = ("we started a running log of all the wins in the company so everyone "
                "feels proud and motivated")
    assert not verify.find_evidence(invented, utts).ok
    loop = "issues issues issues issues issues issues issues issues issues in the company"
    assert not verify.find_evidence(loop, utts).ok        # a repetition loop proves nothing


def test_empty_or_adviceless_records_are_flagged_not_passed():
    words = [{"start_ms": i * 1000, "end_ms": i * 1000 + 900,
              "text": "founders should talk to users and ship quickly " * 3} for i in range(60)]
    empty = {"category": "founder-story", "highlights": [], "advice_atoms": []}
    verify.verify_record(empty, words)
    assert empty["extraction_meta"]["validation_status"] == "flagged"
    assert "no highlights or advice" in empty["extraction_meta"]["verification"]["issues"][0]
    q = "founders should talk to users and ship quickly founders should talk"
    practical = {"category": "fundraising", "advice_atoms": [],
                 "highlights": [{"text": "x", "evidence_span": q}]}
    verify.verify_record(practical, words)
    assert practical["extraction_meta"]["validation_status"] == "flagged"
    story = {"category": "founder-story", "advice_atoms": [],
             "highlights": [{"text": "x", "evidence_span": q}]}
    verify.verify_record(story, words)
    assert story["extraction_meta"]["validation_status"] == "pass"
    assert "issues" not in story["extraction_meta"]["verification"]   # unchanged files stay identical
    tiny = {"category": "other", "highlights": [], "advice_atoms": []}
    verify.verify_record(tiny, words[:2])                              # a 50-word clip may be empty
    assert tiny["extraction_meta"]["validation_status"] == "pass"



def test_stage_independence():
    """Swapping the embedding model must not force a re-crawl or re-extract."""
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        m.upsert_document("v1", "yc_uploads", title="T")
        for stage in ("fetch", "clean", "extract", "index"):
            m.mark(StageState("v1", stage, "ok", input_hash="h1", version="v1"))
        m.invalidate_stage("index", "new embedding model")
        assert m.needs("v1", "index", "h1", "v1")
        assert not m.needs("v1", "fetch", "h1", "v1")
        assert not m.needs("v1", "extract", "h1", "v1")


def test_reextract_makes_verify_pending_again():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        m.upsert_document("v1", "src", title="t")
        m.mark(StageState("v1", "extract", "ok"))
        m.mark(StageState("v1", "verify", "ok"))
        assert "v1" not in m.pending("verify")
        m.mark(StageState("v1", "verify", "stale", error="re-extracted"))   # what cmd_extract now does
        assert "v1" in m.pending("verify")


def test_schema_version_change_invalidates_extract():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        m.upsert_document("v1", "s")
        m.mark(StageState("v1", "extract", "ok", input_hash="h1", version="1.0.0"))
        assert m.needs("v1", "extract", "h1", "2.0.0")


def test_only_flagged_records_are_redone():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        for v, err in (("good", None), ("weak", "flagged: 3 unmatched"), ("bad", "failed: 7 unmatched")):
            m.upsert_document(v, "s")
            m.mark(StageState(v, "extract", "ok"))
            m.mark(StageState(v, "verify", "ok", error=err))
        assert sorted(m.flagged_ids()) == ["bad", "weak"]
        m.invalidate_docs("extract", m.flagged_ids())
        assert sorted(m.pending("extract")) == ["bad", "weak"]


def test_invalidate_flags_combine_instead_of_one_winning():
    """`--source X --only-flagged` is the flagged records of X, not all of X (it once re-ran a
    whole Source's extraction because --source silently won)."""
    import contextlib
    import io
    from ytbrain import cli
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "m.db"
        m = Manifest(db)
        for v, src, err in (("a-ok", "A", None), ("a-weak", "A", "flagged: 2 unmatched"),
                            ("b-weak", "B", "flagged: 1 unmatched"), ("b-ok", "B", None)):
            m.upsert_document(v, src)
            m.mark(StageState(v, "extract", "ok"))
            m.mark(StageState(v, "verify", "ok", error=err))
        real = cli.MANIFEST_DB
        cli.MANIFEST_DB = db
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                assert cli.main(["invalidate", "extract", "--only-flagged", "--source", "A"]) == 0
            assert Manifest(db).pending("extract") == ["a-weak"]
            with contextlib.redirect_stdout(io.StringIO()):
                assert cli.main(["invalidate", "extract", "--source", "B"]) == 0
            assert sorted(Manifest(db).pending("extract")) == ["a-weak", "b-ok", "b-weak"]
        finally:
            cli.MANIFEST_DB = real


def test_rate_pacing_applies_only_to_hosted_endpoints():
    try:
        from ytbrain.extract import runner
    except ImportError:                     # pydantic not installed: runner unavailable
        _skipped("optional dependency not installed")
        return
    for url in ("http://localhost:1234/v1", "http://127.0.0.1:11434", "http://mac.local:1234/v1"):
        assert runner.is_local_endpoint(url), url
    for url in ("https://openrouter.ai/api/v1", "https://integrate.api.nvidia.com/v1"):
        assert not runner.is_local_endpoint(url), url


def test_damaged_json_is_detected_not_trusted():
    try:
        from ytbrain.cli import _unreadable_json
    except ImportError:
        _skipped("optional dependency not installed")
        return
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.json"
        assert _unreadable_json(p, "utterances") == "missing"
        p.write_text('{"doc_id": "x", "utteran')                     # truncated write
        assert "corrupt" in _unreadable_json(p, "utterances")
        p.write_text('{"doc_id": "x"}')
        assert "incomplete" in _unreadable_json(p, "utterances")
        p.write_text('{"doc_id": "x", "utterances": []}')
        assert _unreadable_json(p, "utterances") is None


def test_series_comes_from_the_source_and_catch_all_assigns_none():
    try:
        from ytbrain import cli
    except ImportError:
        _skipped("optional dependency not installed")
        return
    assert cli._series_for({"id": "ss", "name": "Startup School"}) == "Startup School"
    assert cli._series_for({"id": "ss", "name": "X", "series": "Explicit"}) == "Explicit"
    assert cli._series_for({"id": "up", "name": "Uploads", "fallback": True}) is None
    assert cli._provenance_for({}, {"uploader": "Stanford Online"}) == "Stanford Online"
    assert cli._provenance_for({"provenance": "yc-official"}, {"uploader": "x"}) == "yc-official"


def test_refresh_backfills_series_and_provenance_idempotently():
    try:
        from ytbrain import cli
        from ytbrain.config import MANIFEST_DB, METADATA, TRANSCRIPTS
    except ImportError:
        _skipped("optional dependency not installed")
        return
    import io, contextlib
    m = Manifest(MANIFEST_DB)
    m.upsert_document("rf1", "ss", title="t")
    m.upsert_document("rf2", "gone", title="t")                    # its Source was removed
    (RAW / "rf1.info.json").write_text(json.dumps({"uploader": "Y Combinator"}))
    (TRANSCRIPTS / "rf1.json").write_text(json.dumps({"doc_id": "rf1", "series": None, "utterances": []}))
    rec = {"doc_id": "rf1", "title_raw": "t", "series": None, "provenance": "yc-official",
           "extraction_meta": {}}
    (METADATA / "rf1.json").write_text(json.dumps(rec))
    orig = cli._sources
    cli._sources = lambda: {"sources": [{"id": "ss", "name": "Startup School"}]}
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_refresh(None)
        assert "1 document(s) and 2 file(s) updated" in out.getvalue(), out.getvalue()
        assert "belong to Sources no longer in sources.yaml" in out.getvalue()   # rf2 (+ other tests' docs)
        assert m.get_document("rf2")["series"] is None
        d = m.get_document("rf1")
        assert (d["series"], d["provenance"]) == ("Startup School", "Y Combinator")
        r = json.loads((METADATA / "rf1.json").read_text())
        assert (r["series"], r["provenance"]) == ("Startup School", "Y Combinator")
        assert json.loads((TRANSCRIPTS / "rf1.json").read_text())["series"] == "Startup School"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_refresh(None)                                  # second run: nothing to do
        assert "0 document(s) and 0 file(s) updated" in out.getvalue(), out.getvalue()
    finally:
        cli._sources = orig


def _fake_backend(script):
    """A scripted LLM: each call pops the next reply (str or Completion) and
    records the prompt, so tests can assert on what the pipeline sent."""
    from ytbrain.extract import runner
    sent = []
    def fn(prompt, schema, **kw):
        sent.append(prompt)
        reply = script.pop(0)
        return reply(prompt) if callable(reply) else reply
    return fn, sent


_UTTS = [{"start_ms": i * 10000, "end_ms": i * 10000 + 9000,
          "text": f"segment {i} talk to your users every single week and charge money from day one number {i}"}
         for i in range(40)]


def _gen(quote, advice_quote=None, n_high=1):
    import json as _j
    return _j.dumps({"title_canonical": "t", "category": "sales", "summary": "s",
                     "highlights": [{"text": f"h{i}", "evidence_span": quote} for i in range(n_high)],
                     "advice_atoms": ([{"atom_id": "a01", "text": "Talk to users",
                                        "evidence_span": advice_quote}] if advice_quote else [])})


def test_truncated_answer_is_asked_again_shorter_not_repaired():
    try:
        from ytbrain.extract import runner
        from ytbrain.extract.schema import Generated
    except ImportError:
        _skipped("optional dependency not installed")
        return
    fn, sent = _fake_backend([runner.Completion('{"title_canonical": "t", "summ', truncated=True),
                              _gen("talk to your users every single week")])
    runner.BACKENDS["fake"] = fn
    calls = []
    inst, _ = runner.generate_validated("PROMPT", Generated, "fake", calls=calls)
    assert inst is not None and len(sent) == 2
    assert "cut off" in sent[1] and "failed schema validation" not in sent[1]
    assert [c["kind"] for c in calls] == ["extract", "extract:concise"] and calls[0]["truncated"]


def test_cut_off_but_complete_answer_is_kept_and_call_log_has_diagnostics():
    try:
        from ytbrain.extract import runner
        from ytbrain.extract.schema import Generated
    except ImportError:
        _skipped("optional dependency not installed")
        return
    padded = _gen("talk to your users every single week") + "\n" * 50
    fn, sent = _fake_backend([runner.Completion(padded, truncated=True,
                              info={"finish": "length", "out_tokens": 8192, "reasoning_tokens": 7000})])
    runner.BACKENDS["fake"] = fn
    calls = []
    inst, _ = runner.generate_validated("PROMPT", Generated, "fake", calls=calls)
    assert inst is not None and len(sent) == 1                # no second, "concise" call
    assert calls[0]["ok"] and calls[0]["reasoning_tokens"] == 7000 and "note" in calls[0]


def test_looping_provider_is_skipped_on_the_retry():
    try:
        from ytbrain.extract import runner
        from ytbrain.extract.schema import Generated
    except ImportError:
        _skipped("optional dependency not installed")
        return
    import os as _os
    bodies = []
    orig_post, orig_base = runner._post_with_backoff, _os.environ.get("YTBRAIN_LLM_BASE_URL")
    replies = [runner.Completion('{"stage_relevance": ["idea", "idea", "idea"', truncated=True,
                                 info={"provider": "LoopyCloud", "finish": "length"}),
               runner.Completion(_gen("talk to your users every single week"))]
    runner._post_with_backoff = lambda url, body, *a, **k: (bodies.append(body), replies.pop(0))[1]
    _os.environ["YTBRAIN_LLM_BASE_URL"] = "https://openrouter.ai/api/v1"
    try:
        runner._tls.avoid_providers = None
        calls = []
        inst, _ = runner.generate_validated("PROMPT", Generated, "openai", calls=calls)
        assert inst is not None
        assert "provider" not in bodies[0] or "LoopyCloud" not in bodies[0]["provider"].get("ignore", [])
        assert "LoopyCloud" in bodies[1]["provider"]["ignore"]
        assert calls[0]["retry_avoids"] == "LoopyCloud"
    finally:
        runner._post_with_backoff = orig_post
        runner._tls.avoid_providers = None
        if orig_base is None:
            _os.environ.pop("YTBRAIN_LLM_BASE_URL", None)
        else:
            _os.environ["YTBRAIN_LLM_BASE_URL"] = orig_base


def test_failed_video_raises_with_its_call_log():
    try:
        from ytbrain.extract import runner
    except ImportError:
        _skipped("optional dependency not installed")
        return
    fn, _ = _fake_backend([runner.Completion('{"title', truncated=True)] * 20)
    runner.BACKENDS["fake"] = fn
    try:
        runner.extract_video({"utterances": _UTTS}, {"doc_id": "v9", "title": "T"},
                             [{"chapter_id": "ch01", "title": "all", "start_ms": 0,
                               "source": "uploader"}], backend="fake")
    except runner.ExtractionFailed as e:
        assert e.calls and all("error" in c for c in e.calls)
        assert e.calls[0]["tail"].endswith('{"title')
    else:
        raise AssertionError("expected ExtractionFailed")


def test_item_budget_scales_with_talk_length():
    try:
        from ytbrain.extract import runner
    except ImportError:
        _skipped("optional dependency not installed")
        return
    assert runner.item_budget(150) == (2, 2)          # a recruiting clip
    assert runner.item_budget(900) == (6, 8)
    assert runner.item_budget(12000) == (10, 12)      # capped


def test_repair_sees_the_whole_previous_answer():
    try:
        from ytbrain.extract import runner
        from ytbrain.extract.schema import Generated
    except ImportError:
        _skipped("optional dependency not installed")
        return
    long_bad = '{"title_canonical": "t", "category": "not-a-category", "summary": "' + "x" * 6000 + '"}'
    fn, sent = _fake_backend([long_bad, _gen("talk to your users every single week")])
    runner.BACKENDS["fake"] = fn
    inst, errs = runner.generate_validated("PROMPT", Generated, "fake")
    assert inst is not None and len(errs) == 1
    assert long_bad in sent[1]          # previously cut at 4000 chars, so repairs dropped content


def test_self_check_retries_paraphrased_quotes_and_keeps_the_grounded_result():
    try:
        from ytbrain.extract import runner
    except ImportError:
        _skipped("optional dependency not installed")
        return
    para = _gen("Being relentless is the key trait of great founders", n_high=3)
    good = _gen("talk to your users every single week and charge money",
                advice_quote="charge money from day one", n_high=3)
    fn, sent = _fake_backend([para, good])
    runner.BACKENDS["fake"] = fn
    rec = runner.extract_video({"utterances": _UTTS}, {"doc_id": "v1", "title": "T"},
                               [{"chapter_id": "ch01", "title": "all", "start_ms": 0, "source": "uploader"}],
                               backend="fake")
    g = rec.extraction_meta.grounding
    assert g["retried"] and g["kept"] == "retry" and g["found"] == 4
    assert "NOT found" in sent[1] and "Being relentless" in sent[1]
    assert [c["kind"] for c in rec.extraction_meta.calls] == ["extract", "extract:grounding-retry"]


def test_a_result_verify_would_flag_gets_one_retry_and_repeated_misquotes_are_listed_once():
    """75 % of quotes grounded passed the old 60 % self-check, but verify flags it (< 90 %)."""
    try:
        from ytbrain.extract import runner
    except ImportError:
        _skipped("optional dependency not installed")
        return
    import json as _j
    first = _j.loads(_gen("talk to your users every single week", n_high=9))   # 9 of 12 grounded: 75 %
    first["advice_atoms"] = [{"atom_id": f"a0{i}", "text": f"Do {i}", "evidence_span": "listen to the voice of the customer"}
                             for i in range(1, 4)]
    good = _gen("talk to your users every single week and charge money", advice_quote="charge money from day one", n_high=10)
    fn, sent = _fake_backend([_j.dumps(first), good])       # the retry grounds more quotes (11), so it is kept
    runner.BACKENDS["fake"] = fn
    rec = runner.extract_video({"utterances": _UTTS}, {"doc_id": "v1", "title": "T"},
                               [{"chapter_id": "ch01", "title": "all", "start_ms": 0, "source": "uploader"}],
                               backend="fake")
    g = rec.extraction_meta.grounding
    assert g["retried"] and g["kept"] == "retry"
    assert sent[1].count("listen to the voice of the customer") == 1, "a misquote reused by 3 items is listed once"


def test_substantial_talk_with_no_advice_gets_one_retry():
    try:
        from ytbrain.extract import runner
    except ImportError:
        _skipped("optional dependency not installed")
        return
    no_adv = _gen("talk to your users every single week", n_high=2)
    fn, sent = _fake_backend([no_adv, no_adv])            # model insists: no advice
    runner.BACKENDS["fake"] = fn
    long_utts = [dict(u, text=u["text"] * 3) for u in _UTTS]  # > NO_ADVICE_RETRY_MIN_WORDS
    rec = runner.extract_video({"utterances": long_utts}, {"doc_id": "v2", "title": "T"},
                               [{"chapter_id": "ch01", "title": "all", "start_ms": 0, "source": "uploader"}],
                               backend="fake")
    assert "no advice atoms" in sent[1]
    assert rec.extraction_meta.grounding["kept"] == "first"   # a tie keeps the first answer


def test_long_talk_uses_few_windows_dedups_and_writes_one_overview():
    try:
        from ytbrain.extract import runner
    except ImportError:
        _skipped("optional dependency not installed")
        return
    import json as _j
    overview = _j.dumps({"title_canonical": "Whole talk", "category": "sales", "summary": "covers everything"})
    window_reply = _gen("talk to your users every single week", advice_quote="charge money from day one",
                        n_high=2)
    reply = lambda prompt: overview if "Section summaries" in prompt else window_reply
    fn, sent = _fake_backend([reply] * 50)
    runner.BACKENDS["fake"] = fn
    old_single, old_win = runner.SINGLE_CALL_MAX_TOKENS, runner.WINDOW_TOKENS
    runner.SINGLE_CALL_MAX_TOKENS, runner.WINDOW_TOKENS = 100, 300   # force windows
    try:
        chapters = [{"chapter_id": f"ch{i:02d}", "title": f"c{i}", "start_ms": i * 10000,
                     "source": "uploader"} for i in range(40)]         # 40 chapters
        rec = runner.extract_video({"utterances": _UTTS}, {"doc_id": "v3", "title": "T"}, chapters,
                                   backend="fake")
    finally:
        runner.SINGLE_CALL_MAX_TOKENS, runner.WINDOW_TOKENS = old_single, old_win
    windows = rec.extraction_meta.passes
    assert 1 < windows < 40                                   # windows, not one call per chapter
    assert len(sent) == windows + 1                           # + one overview call
    assert rec.summary == "covers everything" and rec.title_canonical == "Whole talk"
    assert len(rec.highlights) == 2 and len(rec.advice_atoms) == 1   # duplicates merged


def test_skipped_is_settled_and_a_refetch_reopens_clean():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        m.upsert_document("nocap", "s")
        m.mark(StageState("nocap", "fetch", "skipped", error="no captions"))
        m.mark(StageState("nocap", "clean", "skipped", error="no captions"))
        assert "nocap" not in m.pending("clean")           # not re-offered every run
        m.mark(StageState("nocap", "fetch", "ok"))
        m.invalidate_docs("clean", ["nocap"], "re-fetched")  # what sync does after a good re-fetch
        assert "nocap" in m.pending("clean")
        assert m.stage_status("nocap", "clean") == "stale" and m.stage_status("zzz", "clean") is None


# --- knowledge index (M1) --------------------------------------------------

def _record(doc_id, advice, highlights=(), stages=("mvp",), summary="A talk about users."):
    return {"doc_id": doc_id, "title_raw": f"Talk {doc_id}", "title_canonical": f"Talk {doc_id}",
            "url": f"https://www.youtube.com/watch?v={doc_id}", "speaker": "Pat", "series": "S",
            "published_at": "2024-05-01", "category": "sales", "summary": summary,
            "stage_relevance": list(stages), "chapters": [],
            "advice_atoms": [dict(a) for a in advice], "highlights": [dict(h) for h in highlights],
            "extraction_meta": {"verification": {"threshold": 0.7}}}


def test_knowledge_items_are_verified_only_and_carry_citations():
    from ytbrain.knowledge.items import build_items
    rec = _record("k1", [
        {"atom_id": "a01", "text": "Talk to users weekly", "evidence_span": "talk to users",
         "timestamp_ms": 61000, "match_score": 0.95, "applies_to_stage": ["idea"]},
        {"atom_id": "a02", "text": "Invented", "evidence_span": "never said", "match_score": 0.2},
        {"atom_id": "a03", "text": "Charge early", "evidence_span": "charge money",
         "timestamp_ms": 5000, "match_score": 0.9, "applies_to_stage": []}])
    items = {i["item_id"]: i for i in build_items(rec, {"utterances": [
        {"start_ms": 0, "end_ms": 4000, "text": "hello there founders"}]})}
    assert "adv:k1:a02" not in items                                  # unverified never indexed
    a1, a3 = items["adv:k1:a01"], items["adv:k1:a03"]
    assert a1["stages"] == ["idea"] and a1["stage_origin"] == "item"
    assert a3["stages"] == ["mvp"] and a3["stage_origin"] == "document"   # inherited [ADR-0005]
    assert a1["deep_link"].endswith("&t=61s") and a1["evidence"] == "talk to users"
    assert "sum:k1" in items and "psg:k1:0" in items
    assert all(i["indexable"].startswith('From "Talk k1" by Pat (S, 2024)') for i in items.values())


def test_search_fusion_boosts_and_diversity():
    from ytbrain.knowledge.search import boosts, diversify, rrf
    fused = rrf([["a", "b", "c"], ["c", "a"]])
    assert max(fused, key=fused.get) == "a" and fused["c"] > fused["b"]
    item_stage = {"kind": "advice", "stages": ["mvp"], "stage_origin": "item", "year": 2026}
    doc_stage = dict(item_stage, stage_origin="document")
    assert boosts(item_stage, "mvp", 2026) > boosts(doc_stage, "mvp", 2026) > boosts(doc_stage, "exit", 2026)
    rows = [{"doc_id": "d", "i": n} for n in range(5)] + [{"doc_id": "e", "i": 9}]
    assert [r["i"] for r in diversify(rows, per_doc=2)] == [0, 1, 9]
    same = [{"doc_id": "d", "i": 1, "evidence": "Save as much cash as possible."},
            {"doc_id": "d", "i": 2, "evidence": "save as much  cash as possible."},
            {"doc_id": "e", "i": 3, "evidence": "save as much cash as possible."}]
    assert [r["i"] for r in diversify(same)] == [1, 3]      # one result per quote per talk


class _FakeEmbedder:
    """Hashed bag of words: texts sharing words get similar vectors. Offline, no torch."""
    name, dim, device = "fake-embed", 64, "cpu"
    def __call__(self, texts):
        import hashlib, math
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in t.lower().split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def test_index_step_checkpoints_reindexes_changes_and_search_finds_advice():
    try:
        import lancedb  # noqa: F401
        from ytbrain import cli, config
        from ytbrain.knowledge import embed as E
        from ytbrain.knowledge.search import search
        from ytbrain.knowledge.store import KnowledgeStore
    except ImportError:
        _skipped("optional dependency not installed")
        return                                          # optional `index` extra not installed
    import io, contextlib, types
    from ytbrain.config import MANIFEST_DB, METADATA, TRANSCRIPTS
    m = Manifest(MANIFEST_DB)
    docs = {"ix1": ("Charge money from day one", "charge money from day one"),
            "ix2": ("Talk to ten users before writing code", "talk to ten users before writing code")}
    for d, (text, quote) in docs.items():
        m.upsert_document(d, "s", title=d, published_at="2024-01-01")
        (METADATA / f"{d}.json").write_text(json.dumps(_record(d, [
            {"atom_id": "a01", "text": text, "evidence_span": quote, "timestamp_ms": 1000,
             "match_score": 1.0, "applies_to_stage": ["mvp"]}])))
        (TRANSCRIPTS / f"{d}.json").write_text(json.dumps({"doc_id": d, "utterances": [
            {"start_ms": 0, "end_ms": 9000, "text": quote}]}))
        for st in ("extract", "verify"):
            m.mark(StageState(d, st, "ok"))
    orig_env, orig_load = config.EMBED_MODEL, E.load_embedder
    E.load_embedder = lambda model=None, device=None: _FakeEmbedder()
    import ytbrain.config as C
    C.EMBED_MODEL = "fake-embed"
    args = types.SimpleNamespace(limit=0)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert cli.cmd_index(args) == 0
        assert "2 Document(s)" in out.getvalue(), out.getvalue()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_index(args)                                          # nothing changed
        assert "0 to index" in out.getvalue()
        assert KnowledgeStore().fulltext_current()
        KnowledgeStore()._fts_marker().unlink()                        # as if Ctrl+C hit the FTS build
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_index(args)
        assert "building full-text index" in out.getvalue() and KnowledgeStore().fulltext_current()
        rec = json.loads((METADATA / "ix1.json").read_text())         # a re-extraction changes ix1
        rec["advice_atoms"][0]["text"] = "Charge customers money from the very first day"
        (METADATA / "ix1.json").write_text(json.dumps(rec))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_index(args)
        assert "1 to index" in out.getvalue()
        store = KnowledgeStore()
        hits = search(store, _FakeEmbedder(), "talk to users before code", kinds=["advice"], top_k=3)
        assert hits and hits[0]["item_id"] == "adv:ix2:a01" and hits[0]["deep_link"].endswith("&t=1s")
        m.mark(StageState("ix1", "verify", "stale"))                   # no longer verified
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_index(args)
        assert "removed 1" in out.getvalue(), out.getvalue()
        assert not KnowledgeStore().document_items("ix1")
    finally:
        E.load_embedder, C.EMBED_MODEL = orig_load, orig_env


def test_an_index_built_before_a_column_existed_gets_it_on_the_next_write():
    """The Knowledge index from before `source_kind` (talk-only) must accept new rows that carry
    it: the existing rows read as talks, the new ones keep their value."""
    try:
        import lancedb
        import pyarrow as pa
        from ytbrain.knowledge import store as KS
    except ImportError:
        _skipped("the index extra (lancedb) isn't installed")
        return
    d = Path(tempfile.mkdtemp())
    old = [c for c in KS.STRING_COLS if c != "source_kind"]
    fields = [pa.field(c, pa.string()) for c in old] + [pa.field(c, pa.list_(pa.string())) for c in KS.LIST_COLS] \
        + [pa.field(c, pa.int64()) for c in KS.INT_COLS] + [pa.field("vector", pa.list_(pa.float32(), 2))]
    t = lancedb.connect(str(d)).create_table("k", schema=pa.schema(fields))
    base = {**{c: ["a"] for c in KS.LIST_COLS}, **{c: 1 for c in KS.INT_COLS}, "vector": [0.1, 0.2]}
    t.add([{**{c: "old" for c in old}, **base}])
    ks = KS.KnowledgeStore(d, "k")
    ks.replace_document("new", [{**{c: "new" for c in KS.STRING_COLS}, **base, "source_kind": "article"}], 2)
    got = sorted((r["doc_id"], r["source_kind"]) for r in ks._t.to_arrow().to_pylist())
    assert got == [("new", "article"), ("old", "talk")], got


def test_device_flag_prefers_mps_and_never_silently_falls_back():
    import types
    from ytbrain.knowledge.embed import resolve_device
    real = sys.modules.get("torch")
    def fake_torch(mps, cuda):
        t = types.ModuleType("torch")
        t.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
        t.cuda = types.SimpleNamespace(is_available=lambda: cuda)
        return t
    try:
        sys.modules["torch"] = fake_torch(mps=True, cuda=False)
        assert resolve_device("auto") == "mps" and resolve_device("cpu") == "cpu"
        sys.modules["torch"] = fake_torch(mps=False, cuda=False)
        assert resolve_device("auto") == "cpu"
        for bad in ("mps", "cuda", "tpu"):
            try:
                resolve_device(bad)
                raise AssertionError(f"{bad} should have been refused")
            except RuntimeError:
                pass
    finally:
        if real is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = real


def test_tombstones_removed_videos():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        m.upsert_document("v1", "s"); m.upsert_document("v2", "s")
        assert m.tombstone_missing("s", ["v1"]) == ["v2"]
        assert m.known_ids("s") == {"v1"}


def test_fallback_source_does_not_clobber_specific_series():
    """A CS183B lecture also appears in the uploads catch-all; CS183B must win."""
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        m.upsert_document("v1", "cs183b_2014", series="cs183b", provenance="yc-official")
        m.upsert_document("v1", "yc_uploads", is_fallback=True, series="yc-channel")
        assert m.get_document("v1")["series"] == "cs183b"


def test_non_fallback_source_may_update_series():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        m.upsert_document("v1", "a", series="old")
        m.upsert_document("v1", "b", series="new")
        assert m.get_document("v1")["series"] == "new"


def test_run_status_is_recorded():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        rid = m.start_run("run")
        m.end_run(rid, "failed", error="enumeration 403")
        last = m.last_runs(1)[0]
        assert last["status"] == "failed" and "403" in last["error"]


def test_an_interrupted_run_is_recorded_not_left_running():
    from unittest import mock
    from ytbrain import cli
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "m.db"
        def cmd_sync(args):
            raise KeyboardInterrupt
        with mock.patch.object(cli, "MANIFEST_DB", db), mock.patch.object(cli, "cmd_sync", cmd_sync):
            try:
                cli.cmd_run(mock.Mock())
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("the interrupt must propagate")
        last = Manifest(db).last_runs(1)[0]
        assert last["status"] == "interrupted" and last["ended_at"], last
        m = Manifest(db)
        m.start_run("run")                                   # then one killed with SIGKILL ...
        assert m.close_abandoned_runs() == 1                 # ... is closed by the next run
        assert m.last_runs(1)[0]["status"] == "abandoned"


def test_a_step_failing_repeatedly_is_parked_until_retry_failed():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        m.upsert_document("v1", "s", published_at="2024-01-01")
        assert m.pending("extract") == ["v1"]
        assert m.fail("v1", "extract", "boom") == "failed" and m.pending("extract") == ["v1"]
        m.mark(StageState("v1", "extract", "ok", attempts=1)); m.succeeded("v1", "extract")
        assert [m.fail("v1", "extract", "boom") for _ in range(3)] == ["failed", "failed", "parked"]
        assert m.pending("extract") == [] and m.parked("extract") == 1   # a success reset the count
        assert m.unpark("extract") == 1 and m.pending("extract") == ["v1"]
        assert m.fail("v1", "extract", "HTTP 400", permanent=True) == "parked"


def test_a_hung_ytdlp_is_a_retryable_failure_not_a_crash():
    import subprocess
    from unittest import mock
    def hang(args, timeout=900):
        raise subprocess.TimeoutExpired(args, timeout)
    with mock.patch.object(fetch, "_run", hang):
        res = fetch.fetch_captions("zzzzzzzzzzz")
    assert res["returncode"] == 124 and "timed out" in res["stderr_tail"]
    assert fetch.permanent_failure(res["stderr_tail"]) is None


def test_a_listed_caption_track_without_a_file_is_retried_not_settled():
    from unittest import mock
    from ytbrain import cli
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        m.upsert_document("v2", "s", title="t")
        res = {"doc_id": "v2", "returncode": 0, "stderr_tail": "", "info_path": None,
               "caption_path": None, "caption_kind": "missing_file", "caption_lang": None}
        with mock.patch.object(fetch, "fetch_captions", return_value=res), \
             mock.patch.object(cli.time, "sleep"), mock.patch("sys.stdout"):
            cli._fetch_video(m, dict(m.get_document("v2")) | {"duration_s": None}, {"id": "s"},
                             False, cli._SyncState(), "(1/1)", 0)
        row = m.stage("v2", "fetch")
        assert row["status"] == "failed" and "no file was written" in row["error"]
        assert "v2" not in m.stage_settled_ids("fetch")


def test_new_captions_send_the_talk_back_to_extract_and_one_bad_talk_does_not_stop_clean():
    import io, contextlib, types
    from unittest import mock
    from ytbrain import cli
    from ytbrain.config import MANIFEST_DB, TRANSCRIPTS
    m = Manifest(MANIFEST_DB)
    words = " ".join(f"word{i}" for i in range(80))
    def srt(text):
        return f"1\n00:00:00,000 --> 00:00:09,000\n{text}\n"
    for d in ("cc1", "cc2"):
        m.upsert_document(d, "s", title=d, published_at="2024-01-01")
        m.mark(StageState(d, "fetch", "ok"))
        (RAW / f"{d}.en.srt").write_text(srt(words))
        (RAW / f"{d}.info.json").write_text(json.dumps({"automatic_captions": {"en": []}}))
    args = types.SimpleNamespace(limit=0, retry_failed=False)
    real = cli.captions.parse_srt
    def flaky(text, _real=real):
        if "BROKEN" in text:
            raise ValueError("damaged subtitle file")
        return _real(text)
    with contextlib.redirect_stdout(io.StringIO()):
        cli.cmd_clean(args)
    for d in ("cc1", "cc2"):
        assert m.stage_status(d, "clean") == "ok"
        m.mark(StageState(d, "extract", "ok"))
    (RAW / "cc1.en.srt").write_text(srt(words + " human captions now"))    # a re-fetch
    (RAW / "cc2.en.srt").write_text(srt("BROKEN"))
    m.invalidate_docs("clean", ["cc1", "cc2"], "re-fetched")
    with mock.patch.object(cli.captions, "parse_srt", flaky), \
         contextlib.redirect_stdout(io.StringIO()) as out:
        assert cli.cmd_clean(args) == 0
    assert m.stage_status("cc1", "clean") == "ok" and m.stage_status("cc1", "extract") == "stale"
    assert "human captions now" in (TRANSCRIPTS / "cc1.json").read_text()
    assert m.stage_status("cc2", "clean") == "failed" and "1 failed" in out.getvalue()
    assert m.stage_status("cc2", "extract") == "ok"            # untouched: its transcript didn't change


def test_pages_are_written_only_for_verified_records():
    import io, contextlib, types
    from ytbrain import cli
    from ytbrain.config import MANIFEST_DB, METADATA, PAGES
    m = Manifest(MANIFEST_DB)
    for d, verified in (("pg1", True), ("pg2", False)):
        m.upsert_document(d, "s", title=d)
        (METADATA / f"{d}.json").write_text(json.dumps({"doc_id": d, "title_raw": d, "highlights": [],
                                                        "advice_atoms": [], "extraction_meta": {}}))
        (PAGES / f"{d}.md").unlink(missing_ok=True)
        m.mark(StageState(d, "verify", "ok" if verified else "stale"))
    with contextlib.redirect_stdout(io.StringIO()) as out:
        cli.cmd_pages(types.SimpleNamespace())
    assert (PAGES / "pg1.md").exists() and not (PAGES / "pg2.md").exists()
    assert "wait for `ytbrain verify`" in out.getvalue()


def test_extract_parks_a_rejected_request_and_marks_verify_stale_before_saving():
    import io, contextlib, types
    from unittest import mock
    from ytbrain import cli, config
    from ytbrain.config import MANIFEST_DB, METADATA, TRANSCRIPTS
    try:
        from ytbrain.extract import runner
    except ImportError:
        _skipped("optional dependency not installed")
        return
    m = Manifest(MANIFEST_DB)
    for d in ("ex1", "ex2"):
        m.upsert_document(d, "s", title=d, published_at="2030-01-01")
        (TRANSCRIPTS / f"{d}.json").write_text(json.dumps({"doc_id": d, "utterances": _UTTS}))
        m.mark(StageState(d, "clean", "ok"))
    m.mark(StageState("ex2", "verify", "ok"))                       # an older record was verified
    order = []
    class Rec:
        def model_dump_json(self):
            order.append(("write", m.stage_status("ex2", "verify")))
            return json.dumps({"doc_id": "ex2", "highlights": [], "advice_atoms": []})
    def fake_extract(transcript, meta, chapters, on_step=None):
        if meta["doc_id"] == "ex1":
            raise runner.RequestRejected("HTTP 400: context length exceeded")
        return Rec()
    with mock.patch.object(config, "LLM_BACKEND", "fake"), \
         mock.patch.object(runner, "extract_video", fake_extract), \
         contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
        cli.cmd_extract(types.SimpleNamespace(limit=0, workers=1, retry_failed=False, doc=["ex1", "ex2"]))
    assert m.stage_status("ex1", "extract") == "parked", out.getvalue()
    assert "--retry-failed" in out.getvalue()
    assert order == [("write", "stale")]                            # verify was reset before the write
    assert m.stage_status("ex2", "extract") == "ok" and (METADATA / "ex2.json").exists()
    with mock.patch.object(config, "LLM_BACKEND", "fake"), \
         mock.patch.object(runner, "extract_video", fake_extract), \
         contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
        cli.cmd_extract(types.SimpleNamespace(limit=0, workers=1, retry_failed=False, doc=["ex1", "ex2"]))
    assert "0 to extract" in out.getvalue() and "1 parked" in out.getvalue()    # not retried every run


def test_extract_gives_up_on_a_document_with_no_progress_but_not_on_a_slow_one_that_reports_steps():
    """A provider that trickles bytes never trips the request timeout: a Document whose extraction reports no new
    step for EXTRACT_STALL_S is recorded as failed and a fresh worker takes its place. One that is slow but keeps
    reporting steps (many calls, retries, backoff) is left alone, so a long talk is never cut off."""
    import io, contextlib, threading, time, types
    from unittest import mock
    from ytbrain import cli, config
    from ytbrain.config import MANIFEST_DB, TRANSCRIPTS
    try:
        from ytbrain.extract import runner
    except ImportError:
        _skipped("optional dependency not installed")
        return
    m = Manifest(MANIFEST_DB)
    ids = ("sd1", "sd2", "sd3")
    for d in ids:
        m.upsert_document(d, "s", title=d, published_at="2030-01-01")
        (TRANSCRIPTS / f"{d}.json").write_text(json.dumps({"doc_id": d, "utterances": _UTTS}))
        m.mark(StageState(d, "clean", "ok"))
    release = threading.Event()

    class Rec:
        def __init__(self, d):
            self.d = d

        def model_dump_json(self):
            return json.dumps({"doc_id": self.d, "highlights": [], "advice_atoms": []})

    def fake(transcript, meta, chapters, on_step=None):
        d = meta["doc_id"]
        if d == "sd1":                       # one request that never answers
            on_step("extracting")
            release.wait(30)
        elif d == "sd2":                     # slow, but a step every 0.3 s, for longer than the stall limit
            for k in range(6):
                on_step(f"ch{k}")
                time.sleep(0.3)
        return Rec(d)

    def run(fn):
        with mock.patch.object(config, "LLM_BACKEND", "fake"), mock.patch.object(runner, "extract_video", fn), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = cli.cmd_extract(types.SimpleNamespace(limit=0, workers=2, retry_failed=False, doc=list(ids)))
        return code, out.getvalue()

    try:
        with mock.patch.object(cli, "EXTRACT_STALL_S", 0.6):
            code, out = run(fake)
        release.set()
        assert code == 0 and "2 records, 1 failed" in out and "no progress for" in out, out
        assert m.stage_status("sd1", "extract") == "failed"
        assert m.stage_status("sd2", "extract") == "ok" and m.stage_status("sd3", "extract") == "ok", \
            "the slow Document that kept reporting steps, and the one the replacement worker took, are saved"
        code, out = run(lambda tr, meta, ch, on_step=None: Rec(meta["doc_id"]))
        assert code == 0 and "1 records" in out, "the rerun does only the one that stalled"
        assert m.stage_status("sd1", "extract") == "ok"
    finally:
        release.set()



# --- extraction versions (ADR-0018) ----------------------------------------

def _registry(*startup, neutral=None):
    """A stand-in VARIANTS: startup releases as (version, compat[, upcast]) and an optional neutral variant."""
    from ytbrain.extract import versions as V
    out = {"startup": V.Variant("startup", "test", tuple(V.Release(r[0], r[1], f"r{r[0]}", *r[2:]) for r in startup))}
    if neutral:
        out["neutral"] = V.Variant("neutral", "test", tuple(V.Release(r[0], r[1], f"r{r[0]}") for r in neutral))
    return out


def test_the_prompt_registry_is_sound_and_matches_the_schema_version():
    from ytbrain.config import SCHEMA_VERSION
    from ytbrain.extract import versions as V
    assert V.problems() == [], V.problems()
    assert V.VARIANTS[V.LEGACY_VARIANT].latest == SCHEMA_VERSION, "SCHEMA_VERSION is the startup variant's latest"
    bad = _registry(("2.3.0", "compatible"), ("2.2.0", "breaking"))
    assert any("oldest first" in p for p in V.problems(bad)) and any("starting point" in p for p in V.problems(bad))


def test_a_prompt_change_needs_a_new_release():
    """L3: every variant's latest release pins its prompt hashes; editing a prompt without declaring a
    release (compatible or breaking) fails here instead of silently drifting under one version."""
    from ytbrain.extract import versions as V
    pinned = json.loads((Path(__file__).parent / "golden" / "prompt_versions.json").read_text())
    for name in V.VARIANTS:
        key = V.stamp(name)
        now = V.fingerprints(name)
        assert pinned.get(key) == now, (
            f"the {name} prompt changed ({now}) but its latest release is still {key}: add a Release to VARIANTS in "
            f"ytbrain/extract/versions.py (compatible, with an upcast if records change shape, or breaking), bump "
            f"SCHEMA_VERSION if the generated schema changed, and pin the new hashes in tests/golden/prompt_versions.json")


def test_a_compatible_release_leaves_records_valid_and_a_breaking_one_redoes_only_its_variant():
    """L1: no global re-extract. Stamps from before variants ("2.2.0") belong to the startup variant."""
    from unittest import mock
    from ytbrain.extract import versions as V
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        stamps = {"legacy": "2.2.0", "s22": "startup@2.2.0", "s23": "startup@2.3.0", "n10": "neutral@1.0.0",
                  "old": "2.1.0", "none": None, "alien": "other@1.0.0"}
        for doc, ver in stamps.items():
            m.upsert_document(doc, "s")
            m.mark(StageState(doc, "extract", "ok", version=ver))
        with mock.patch.dict(V.VARIANTS, _registry(("2.2.0", "breaking"), ("2.3.0", "compatible"),
                                                   neutral=[("1.0.0", "breaking")]), clear=True):
            assert m.invalidate_versions("extract", V.is_current, "r") == 3
            assert sorted(m.pending("extract")) == ["alien", "none", "old"], "only unknown or replaced stamps"
        for doc in ("alien", "none", "old"):
            m.mark(StageState(doc, "extract", "ok", version="startup@2.3.0"))
        with mock.patch.dict(V.VARIANTS, _registry(("2.2.0", "breaking"), ("2.3.0", "compatible"), ("3.0.0", "breaking"),
                                                   neutral=[("1.0.0", "breaking")]), clear=True):
            m.invalidate_versions("extract", V.is_current, "r")
            assert "n10" not in m.pending("extract"), "a breaking startup release leaves the neutral variant alone"
            assert len(m.pending("extract")) == 6
        assert m.versions("extract") == {"neutral@1.0.0": 1}


def test_an_older_record_is_read_in_the_latest_shape_by_compatible_upcasts_in_order():
    from unittest import mock
    from ytbrain.extract import versions as V
    def up23(r):
        return {**r, "facets": ["stage=" + r.get("stage", "?")]}
    def up24(r):
        return {**r, "facets": r["facets"] + ["v24"]}
    reg = _registry(("2.2.0", "breaking"), ("2.3.0", "compatible", up23), ("2.4.0", "compatible", up24))
    rec = {"stage": "mvp", "extraction_meta": {"schema_version": "2.2.0"}}
    with mock.patch.dict(V.VARIANTS, reg, clear=True):
        assert V.upcast(rec)["facets"] == ["stage=mvp", "v24"]
        later = {"facets": ["x"], "extraction_meta": {"prompt_variant": "startup", "schema_version": "2.3.0"}}
        assert V.upcast(later)["facets"] == ["x", "v24"], "only the releases after the record's own"
        latest = {"extraction_meta": {"schema_version": "2.4.0"}}
        assert V.upcast(latest) is latest
    assert rec == {"stage": "mvp", "extraction_meta": {"schema_version": "2.2.0"}}, "the record itself is not changed"


def test_extract_stamps_a_new_record_with_its_variants_latest_and_redoes_nothing_else():
    """L4 (extraction part): new content always gets the latest version; a valid older stamp stays 'ok'."""
    import io, contextlib, types
    from unittest import mock
    from ytbrain import cli, config
    from ytbrain.config import MANIFEST_DB, SCHEMA_VERSION, TRANSCRIPTS
    try:
        from ytbrain.extract import runner
    except ImportError:
        _skipped("optional dependency not installed")
        return
    m = Manifest(MANIFEST_DB)
    for d in ("vs-new", "vs-old"):
        m.upsert_document(d, "s", title=d, published_at="2030-01-01")
        (TRANSCRIPTS / f"{d}.json").write_text(json.dumps({"doc_id": d, "utterances": _UTTS}))
        m.mark(StageState(d, "clean", "ok"))
    m.mark(StageState("vs-old", "extract", "ok", version="2.2.0"))         # written before variants existed
    class Rec:
        def model_dump_json(self):
            return json.dumps({"doc_id": "vs-new", "highlights": [], "advice_atoms": [],
                               "extraction_meta": {"prompt_variant": "startup", "schema_version": SCHEMA_VERSION}})
    with mock.patch.object(config, "LLM_BACKEND", "fake"), \
         mock.patch.object(runner, "extract_video", lambda *a, **k: Rec()), \
         contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
        cli.cmd_extract(types.SimpleNamespace(limit=0, workers=1, retry_failed=False, doc=["vs-"]))
    assert m.stage("vs-new", "extract")["version"] == f"startup@{SCHEMA_VERSION}", out.getvalue()
    assert m.stage_status("vs-old", "extract") == "ok", "a valid older stamp is not re-extracted"


def _upgrade_fixture(m, docs):
    from ytbrain.config import METADATA, TRANSCRIPTS
    for d, cost, chars in docs:
        m.upsert_document(d, "s", title=d)
        m.mark(StageState(d, "extract", "ok", version="startup@2.2.0"))
        calls = [{"kind": "extract", "cost": cost}] if cost is not None else [{"kind": "extract"}]
        (METADATA / f"{d}.json").write_text(json.dumps({"doc_id": d, "source_kind": "talk",
                                                        "extraction_meta": {"schema_version": "2.2.0", "calls": calls}}))
        (TRANSCRIPTS / f"{d}.json").write_text(json.dumps({"doc_id": d, "utterances": [{"text": "x" * chars}]}))


def test_upgrade_shows_the_cost_first_and_re_extracts_only_what_fits_the_cap():
    import io, contextlib, types
    from unittest import mock
    from ytbrain import cli
    from ytbrain.config import MANIFEST_DB
    from ytbrain.extract import versions as V
    m = Manifest(MANIFEST_DB)
    _upgrade_fixture(m, [("up-a", 0.01, 100), ("up-b", 0.02, 100), ("up-c", None, 300), ("up-d", 0.03, 100)])
    args = lambda **k: types.SimpleNamespace(**{"dry_run": False, "max_cost": None, "variant": None, "doc": ["up-"], **k})
    with mock.patch.dict(V.VARIANTS, _registry(("2.2.0", "breaking")), clear=True):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert cli.cmd_upgrade(args(dry_run=True)) == 0
        assert "nothing to do" in out.getvalue(), "everything on the latest version: nothing to upgrade"
    with mock.patch.dict(V.VARIANTS, _registry(("2.2.0", "breaking"), ("2.3.0", "compatible")), clear=True):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert cli.cmd_upgrade(args(dry_run=True)) == 0
        text = out.getvalue()
        # up-c has no past cost: its 300 characters at the median rate (0.0002 per character) = $0.06
        assert "4 record(s) startup@2.2.0 -> startup@2.3.0" in text and "~$0.12" in text and "dry run" in text, text
        assert all(m.stage_status(d, "extract") == "ok" for d in ("up-a", "up-b", "up-c", "up-d"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert cli.cmd_upgrade(args(max_cost=0.035)) == 0
        assert "2 record(s)" in out.getvalue() and "2 more stay as they are" in out.getvalue(), out.getvalue()
        assert {"up-a", "up-b"} <= set(m.pending("extract")) and m.stage_status("up-c", "extract") == "ok"
        assert m.stage_status("up-d", "extract") == "ok"
    for d in ("up-a", "up-b", "up-c", "up-d"):
        m.tombstone([d])


def test_upgrade_refuses_a_cap_it_cannot_estimate():
    import io, contextlib, types
    from unittest import mock
    from ytbrain import cli
    from ytbrain.config import MANIFEST_DB
    from ytbrain.extract import versions as V
    m = Manifest(MANIFEST_DB)
    _upgrade_fixture(m, [("upx-a", None, 100)])
    a = types.SimpleNamespace(dry_run=False, max_cost=1.0, variant=None, doc=["upx-"])
    with mock.patch.dict(V.VARIANTS, _registry(("2.2.0", "breaking"), ("2.3.0", "compatible")), clear=True):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert cli.cmd_upgrade(a) == 1
    assert "can't be honoured" in out.getvalue() and m.stage_status("upx-a", "extract") == "ok"
    m.tombstone(["upx-a"])


def test_status_reports_records_per_extraction_version():
    import io, contextlib, types
    from ytbrain import cli
    from ytbrain.config import MANIFEST_DB, SCHEMA_VERSION
    m = Manifest(MANIFEST_DB)
    m.upsert_document("st-legacy", "s")
    m.mark(StageState("st-legacy", "extract", "ok", version=SCHEMA_VERSION))
    with contextlib.redirect_stdout(io.StringIO()) as out:
        cli.cmd_status(types.SimpleNamespace())
    assert f"startup@{SCHEMA_VERSION}:" in out.getvalue() and "latest" in out.getvalue(), out.getvalue()
    m.tombstone(["st-legacy"])



# --- inferred Domains: the tag Step (ADR-0017) ---------------------------------

_AXES = ["GTM", "LEAD", "FIN", "START", "NOISE", "COOK", "PEOPLE", "SALESX"]


def _axis_embed(texts):
    """Texts -> unit vectors on marker axes (a text with GTM and LEAD lies between them)."""
    import math as _m
    out = []
    for text in texts:
        v = [float(text.split().count(a)) for a in _AXES]
        n = _m.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def _tag_registry():
    from ytbrain import domains as D
    return D.parse({"default": "startup", "domains": {
        "startup": {"description": "START startups", "risk_tier": "medium"},
        "gtm": {"description": "GTM selling", "risk_tier": "medium", "aliases": ["go-to-market", "sales", "marketing"]},
        "leadership": {"description": "LEAD people", "risk_tier": "low", "aliases": ["management"]},
        "finance": {"description": "FIN money", "risk_tier": "high"}}})


def _tag_rows():
    rows = [("psg:d1:1", "d1", "passage", "GTM"), ("psg:d1:2", "d1", "passage", "LEAD"),
            ("psg:d1:3", "d1", "passage", "GTM LEAD"), ("psg:d1:4", "d1", "passage", "NOISE"),
            ("adv:d1:a1", "d1", "advice", "FIN"), ("sum:d1", "d1", "summary", "GTM"),
            ("psg:d2:1", "d2", "passage", "FIN"), ("psg:d3:1", "d3", "passage", "NOISE")]
    out = [{"item_id": i, "doc_id": d, "kind": k, "text": f"text {x}", "source_id": "s1" if d != "d2" else "s2"}
           for i, d, k, x in rows]
    return out, _axis_embed([x for *_, x in rows])


def test_domain_names_match_whatever_their_spelling_and_aliases_cannot_collide():
    """L9: ~30 spellings of existing Domains and their aliases all resolve to the existing Domain."""
    from ytbrain import domains as D
    reg = _tag_registry()
    variants = {"gtm": ["GTM", "gtm ", "G.T.M", "Go-To-Market", "go to market", "GO_TO_MARKET", "Sales", "sale",
                        "SALES!", "Marketing", "marketings"],
                "leadership": ["Leadership", "leaderships", "LEADERSHIP.", "Management", "managements", "management "],
                "startup": ["Startups", "start-up", "STARTUP", "startup's"],
                "finance": ["Finance", "finances", "FINANCE", "finance/"]}
    for want, names in variants.items():
        for n in names:
            got = reg.lookup(n)
            assert got == want, (n, got)
    assert sum(len(v) for v in variants.values()) >= 25
    assert reg.lookup("cooking") is None and D.norm_key("Systems Designs") == "system-design"
    try:
        D.parse({"domains": {"gtm": {"aliases": ["sales"]}, "sales-ops": {"aliases": ["Sale"]}}})
        raise AssertionError("an alias naming another Domain must be refused")
    except D.DomainConfigError as e:
        assert "same name as" in str(e)


def test_add_alias_keeps_comments_and_refuses_a_name_already_taken():
    from ytbrain import domains as D
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "domains.yaml"
        p.write_text("# my notes\ndefault: startup\ndomains:\n  startup:\n    description: \"x\"  # keep me\n"
                     "    risk_tier: medium\n  gtm:\n    description: \"y\"\n    risk_tier: medium\n")
        D.add_alias("gtm", "Go To Market", p)
        reg = D.add_alias("gtm", "selling", p)
        assert reg.get("gtm").aliases == ("Go To Market", "selling") and "# keep me" in p.read_text()
        assert "# my notes" in p.read_text()
        for bad in ("go-to-market", "Startup"):
            try:
                D.add_alias("gtm", bad, p)
                raise AssertionError(bad)
            except D.DomainConfigError as e:
                assert "already names" in str(e)
        assert D.folder_domain("/b/Go To Market/x.pdf", reg, [Path("/b")]) == "gtm", "an alias folder is its Domain"


def test_the_tag_step_tags_by_content_breaks_close_calls_and_holds_high_tier_tags_for_review():
    from ytbrain import tagging as T
    reg = _tag_registry()
    rows, vecs = _tag_rows()
    s = T.Settings(floor=0.5, close=0.05, share=0.2, hint_bonus=0.02, min_passages=1)   # a 3-Passage fixture
    asked = []

    def ask(batch):
        asked.append(batch)
        return [["leadership"] for _ in batch], 0.01, 1
    with tempfile.TemporaryDirectory() as d:
        db = T.TagDB(Path(d) / "tags.db")
        hints = {"d2": ["finance"]}
        res = T.run(rows, vecs, reg, _axis_embed, hints, s, ask, db, "m", say=lambda m: None)
        it = res.items
        assert it["psg:d1:1"].domains == ("gtm",) and it["psg:d1:1"].origin == "embedding"
        assert it["psg:d1:3"].domains == ("leadership",) and it["psg:d1:3"].origin == "tiebreak"
        assert asked == [[("text GTM LEAD", ["gtm", "leadership"])]], "only close calls, only their candidates"
        assert res.docs["d1"].domains == ("leadership", "gtm"), "a talk on two subjects gets both, by Passage share"
        assert res.docs["d1"].shares == {"gtm": 0.333, "leadership": 0.667}
        assert it["psg:d1:4"].origin == "inherited" and it["psg:d1:4"].domains == ("leadership", "gtm")
        assert it["sum:d1"].domains == ("leadership", "gtm") and it["sum:d1"].origin == "document"
        assert it["adv:d1:a1"].suggested == ("finance",) and "finance" not in it["adv:d1:a1"].domains, \
            "an unconfirmed high-tier tag is only suggested"
        assert res.docs["d2"].domains == ("finance",) and not res.docs["d2"].suggested, "a hint confirms it"
        assert res.docs["d3"].origin == "default" and res.docs["d3"].domains == ("startup",) and res.unplaced == ["d3"]
        db.replace(res.items, res.docs, {"tagger_version": "1"})
        # L8: same input, same list -> same tags, and no model call (the cache answers)
        again = T.run(rows, vecs, reg, _axis_embed, hints, s, ask, db, "m", say=lambda m: None)
        assert len(asked) == 1 and again.cached == 1 and again.asked == 0
        assert {k: (v.domains, v.suggested) for k, v in again.items.items()} == \
            {k: (v.domains, v.suggested) for k, v in res.items.items()}
        # a confirmation for the Source releases the suggestion
        db.confirm(["source:s1"], "finance")
        conf = T.run(rows, vecs, reg, _axis_embed, hints, s, ask, db, "m", say=lambda m: None)
        assert "finance" in conf.items["adv:d1:a1"].domains and not conf.items["adv:d1:a1"].suggested
        db.close()


def test_aliases_name_domains_but_never_score_content_and_a_rejection_is_final():
    from ytbrain import tagging as T
    reg = _tag_registry()
    texts = [x for _, x in T.profile_texts(reg)]
    assert "sales" not in texts and "management" not in texts and "GTM selling" in texts, texts
    from ytbrain import domains as D
    neg = D.parse({"domains": {"investment": {"description": "INV portfolios", "risk_tier": "high",
                                              "not_about": "raising money for a startup"},
                               "finance": {"description": "FIN money", "risk_tier": "high"}}})
    assert all("raising" not in x for _, x in T.profile_texts(neg)), "what a Domain is not is never embedded"
    sent = []
    T.llm_tiebreak.__wrapped__ if hasattr(T.llm_tiebreak, "__wrapped__") else None
    from unittest import mock
    from ytbrain.eval import llm
    with mock.patch.object(llm, "ask", lambda prompt, cls, model: (sent.append(prompt), llm.Answer(None))[1]):
        T.llm_tiebreak(neg, "m")([("text", ["finance", "investment"])])
    assert "Not about: raising money for a startup" in sent[0], "the tie-break model reads it"
    assert T.tiebreak_key("x", ["investment"], neg, "m") != T.tiebreak_key(
        "x", ["investment"], D.parse({"domains": {"investment": {"description": "INV portfolios", "risk_tier": "high"}}}), "m")
    rows, vecs = _tag_rows()
    with tempfile.TemporaryDirectory() as d:
        db = T.TagDB(Path(d) / "tags.db")
        s = T.Settings(floor=0.5, close=0.05)
        res = T.run(rows, vecs, reg, _axis_embed, {}, s, None, db, "m", say=lambda m: None)
        assert res.items["adv:d1:a1"].suggested == ("finance",)
        db.reject(["source:s1"], "finance")
        res = T.run(rows, vecs, reg, _axis_embed, {}, s, None, db, "m", say=lambda m: None)
        assert res.items["adv:d1:a1"].suggested == () and "finance" not in res.items["adv:d1:a1"].domains
        assert res.awaiting_review == 1, "only d2's finance (source s2) is still awaiting review"
        db.confirm(["source:s1"], "finance")                    # the later answer wins
        res = T.run(rows, vecs, reg, _axis_embed, {}, s, None, db, "m", say=lambda m: None)
        assert "finance" in res.items["adv:d1:a1"].domains and not db.rejections()
        db.close()


def test_one_stray_passage_never_decides_a_documents_domain():
    """A Domain needs two Passages of the Document (or half of it): one Passage of five is noise."""
    from ytbrain import tagging as T
    reg = _tag_registry()
    texts = [("psg:e1:1", "e1", "LEAD"), ("psg:e1:2", "e1", "LEAD"), ("psg:e1:3", "e1", "LEAD"),
             ("psg:e1:4", "e1", "LEAD"), ("psg:e1:5", "e1", "FIN"),                 # 1 of 5 = 20 %: noise
             ("psg:e2:1", "e2", "LEAD"), ("psg:e2:2", "e2", "FIN"),                 # 1 of 2 = half: kept
             ("psg:e3:1", "e3", "GTM")]                                             # one Passage: it decides
    rows = [{"item_id": i, "doc_id": d, "kind": "passage", "text": f"text {x}", "source_id": "s"} for i, d, x in texts]
    vecs = _axis_embed([x for *_, x in texts])
    res = T.run(rows, vecs, reg, _axis_embed, {}, T.Settings(floor=0.5, close=0.05), None, None, "m",
                say=lambda m: None)
    docs = {d: set(t.domains) | set(t.suggested) for d, t in res.docs.items()}
    assert docs == {"e1": {"leadership"}, "e2": {"leadership", "finance"}, "e3": {"gtm"}}, docs
    old = T.run(rows, vecs, reg, _axis_embed, {}, T.Settings(floor=0.5, close=0.05, min_passages=1), None, None, "m",
                say=lambda m: None)
    assert "finance" in set(old.docs["e1"].suggested), "min_passages=1 is the old rule (and in the tune grid)"
    assert T.Settings().key() != T.Settings(min_passages=1).key(), "a labelled set passes for one rule only"


def test_a_close_call_the_model_cannot_decide_is_cached_and_not_paid_for_again():
    from ytbrain import tagging as T
    reg = _tag_registry()
    rows, vecs = _tag_rows()
    s = T.Settings(floor=0.5, close=0.05, batch=1, workers=1)
    asked = []

    def unsure(batch):
        asked.append(batch)
        return [[] for _ in batch], 0.01, 1
    with tempfile.TemporaryDirectory() as d:
        db = T.TagDB(Path(d) / "tags.db")
        first = T.run(rows, vecs, reg, _axis_embed, {}, s, unsure, db, "m", say=lambda m: None)
        assert first.unresolved == 1 and len(asked) == 1
        again = T.run(rows, vecs, reg, _axis_embed, {}, s, unsure, db, "m", say=lambda m: None)
        assert len(asked) == 1 and again.undecided == 1 and again.unresolved == 1, "not asked (or paid for) again"
        assert again.items["psg:d1:3"].domains == ("gtm", "leadership"), "it keeps every candidate"
        T.run(rows, vecs, reg, _axis_embed, {}, s, unsure, db, "m", say=lambda m: None, retry_undecided=True)
        assert len(asked) == 2, "--retry-undecided asks again"
        T.run(rows, vecs, reg, _axis_embed, {}, s, unsure, db, "other-model", say=lambda m: None)
        assert len(asked) == 3, "another model is a new question"
        db.close()


def test_a_tie_break_that_cannot_run_keeps_every_candidate_and_respects_the_spend_cap():
    """R8 and L13."""
    from ytbrain import tagging as T
    reg = _tag_registry()
    rows, vecs = _tag_rows()
    s = T.Settings(floor=0.5, close=0.05, batch=1, workers=1)

    def down(batch):
        raise ConnectionError("endpoint unreachable")
    said = []
    res = T.run(rows, vecs, reg, _axis_embed, {}, s, down, None, "m", say=said.append)
    assert res.items["psg:d1:3"].domains == ("gtm", "leadership") and res.items["psg:d1:3"].origin == "unresolved"
    assert res.unresolved == 1 and any("unavailable" in m for m in said)
    rows2 = rows + [{"item_id": "psg:d4:1", "doc_id": "d4", "kind": "passage", "text": "text LEAD GTM", "source_id": "s"}]
    vecs2 = vecs + _axis_embed(["GTM LEAD"])
    paid = []

    def costly(batch):
        paid.append(batch)
        return [["gtm"] for _ in batch], 1.0, 1
    res = T.run(rows2, vecs2, reg, _axis_embed, {}, s, costly, None, "m", max_cost=0.5, say=said.append)
    assert len(paid) == 1 and res.asked == 1 and res.unresolved == 1, "stops asking at the cap; the rest wait"
    dry = T.run(rows2, vecs2, reg, _axis_embed, {}, s, costly, None, "m", dry_run=True, say=said.append)
    assert len(paid) == 1 and dry.est_calls == 2, "a dry run asks nothing and estimates the calls"
    # each batch is cached when it arrives: an interrupted run keeps what it paid for
    with tempfile.TemporaryDirectory() as d:
        db = T.TagDB(Path(d) / "tags.db")
        calls = {"n": 0}

        def stop_after_one(batch):
            calls["n"] += 1
            if calls["n"] > 1:
                raise ConnectionError("dropped mid-run")
            return [["gtm"] for _ in batch], 0.01, 1
        T.run(rows2, vecs2, reg, _axis_embed, {}, s, stop_after_one, db, "m", say=said.append)
        assert len(db.cached([T.tiebreak_key(r["text"], ["gtm", "leadership"], reg, "m")
                              for r in rows2 if "LEAD" in r["text"] and "GTM" in r["text"]])) >= 1
        assert T.Settings(workers=1).key() == T.Settings(workers=16).key(), "speed never changes the gate's key"
        db.close()


def test_propose_groups_unplaced_content_and_runs_the_four_duplicate_checks():
    from ytbrain import tagging as T
    reg = _tag_registry()
    texts = ["COOK"] * 3 + ["SALESX"] * 3 + ["PEOPLE"] * 3
    rows = [{"item_id": f"psg:p:{i:02d}", "doc_id": "p", "kind": "passage", "text": f"{x} {i}"} for i, x in enumerate(texts)]
    vecs = _axis_embed(texts)
    origins = {r["item_id"]: "inherited" for r in rows}
    answers = {"COOK": {"name": "Cooking", "description": "COOK recipes", "examples": ["How long to rest dough?"],
                        "risk_tier": "low", "relation": "new", "existing": None},
               "SALESX": {"name": "Sales", "description": "selling", "relation": "new"},
               "PEOPLE": {"name": "People Ops", "description": "hr things", "relation": "narrower",
                          "existing": "leadership"}}

    def ask_json(prompt):
        assert "gtm: GTM selling (also: go-to-market, sales, marketing)" in prompt, "the whole list goes to the model"
        return next(v for k, v in answers.items() if f"] {k} " in prompt)
    props = {p.name: p for p in T.propose(rows, vecs, reg, _axis_embed, ask_json, origins, min_size=3)}
    assert props["cooking"].verdict == "new" and props["cooking"].size == 3
    assert props["sales"].verdict == "already gtm", "an alias of an existing Domain is never proposed"
    assert props["people-ops"].verdict == "narrower than leadership"
    assert T.clusters(vecs, [r["item_id"] for r in rows], min_size=3) == \
        T.clusters(vecs, [r["item_id"] for r in rows], min_size=3), "deterministic"


def test_the_labelled_set_scores_precision_recall_and_high_tier_misses():
    """L6/L7 gate arithmetic, and the sheets the labels are written on."""
    import csv
    from ytbrain import tagging as T
    reg = _tag_registry()
    rows, vecs = _tag_rows()
    with tempfile.TemporaryDirectory() as d:
        db = T.TagDB(Path(d) / "tags.db")
        res = T.run(rows, vecs, reg, _axis_embed, {"d2": ["finance"]}, T.Settings(floor=0.5, close=0.05),
                    None, db, "m", say=lambda m: None)
        db.replace(res.items, res.docs, {})
        items, docs = T.sample_sheets(db, {"d1": "Talk one"}, {r["item_id"]: r["text"] for r in rows}, 5, 2)
        assert len(items) == 5 and all(r["label"] == "" for r in items) and not any(r["item_id"].startswith("sum:") for r in items)
        assert items == T.sample_sheets(db, {"d1": "Talk one"}, {r["item_id"]: r["text"] for r in rows}, 5, 2)[0]
        p = Path(d) / "items.csv"
        with open(p, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=T.SHEET_COLUMNS)
            w.writeheader()
            w.writerows([{"item_id": "psg:d1:1", "label": "Go-To-Market"}, {"item_id": "psg:d1:2", "label": "management"},
                         {"item_id": "adv:d1:a1", "label": "finance"}, {"item_id": "psg:d2:1", "label": "finance|startup"},
                         {"item_id": "psg:d1:4", "label": ""}, {"item_id": "psg:d3:1", "label": "astrology"}])
        labels, bad = T.read_labels(p, "item_id", reg)
        assert labels == {"psg:d1:1": {"gtm"}, "psg:d1:2": {"leadership"}, "adv:d1:a1": {"finance"},
                          "psg:d2:1": {"finance", "startup"}} and bad == ["psg:d3:1: 'astrology' is not a Domain"]
        r = T.score_labels(labels, {"d1": {"gtm", "leadership"}}, *T.current(db), reg)
        # adv:d1:a1 predicted leadership+gtm, suggested finance -> 1 tp, 2 fp; psg:d2:1 misses startup
        assert (r["precision"], r["recall"]) == (round(4 / 6, 4), round(4 / 5, 4)) and r["high_missed"] == []
        assert r["documents"] == 1.0 and not r["passed"]
        r2 = T.score_labels({"psg:d1:1": {"gtm", "finance"}}, {}, *T.current(db), reg)
        assert r2["high_missed"] == ["psg:d1:1: finance"] and not r2["passed"], "a missed high-tier label fails the gate"
        db.close()


def test_tags_reach_the_index_only_after_the_labelled_set_passes_for_this_tagger():
    from ytbrain import tagging as T
    reg = _tag_registry()
    s = T.Settings(floor=0.5)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "tags.json"
        assert "no `ytbrain eval tags`" in T.gate(reg, s, p)
        ok = {"passed": True, "tagger_version": T.TAGGER_VERSION, "domain_list": reg.fingerprint(), "settings": s.key()}
        p.write_text(json.dumps(ok))
        assert T.gate(reg, s, p) is None
        p.write_text(json.dumps({**ok, "passed": False}))
        assert "failed" in T.gate(reg, s, p)
        p.write_text(json.dumps(ok))
        assert "changed" in T.gate(reg, T.Settings(floor=0.6), p), "new thresholds need a new pass"
        from ytbrain import domains as D
        bigger = D.parse({"domains": {"startup": {"description": "START startups", "risk_tier": "medium"},
                                      "cooking": {"description": "COOK"}}})
        assert "changed" in T.gate(bigger, s, p), "a new Domain needs a new pass"


def test_status_counts_leave_out_dropped_documents():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        for doc in ("keep", "gone"):
            m.upsert_document(doc, "s")
            m.mark(StageState(doc, "extract", "ok", version="2.2.0"))
        m.db.execute("UPDATE documents SET tombstoned_at='2026-01-01' WHERE doc_id='gone'")
        m.db.commit()
        assert m.stats()["extract"] == 1 and m.versions("extract") == {"2.2.0": 1}



# --- the subject-neutral prompt, parity and enrich (ADR-0018, M6c) -----------------

def _neutral_answer(quote: str) -> str:
    return json.dumps({"title_canonical": "Replication", "summary": "s", "topics": ["database replication"],
                       "highlights": [{"text": "h", "evidence_span": quote}],
                       "advice_atoms": [{"atom_id": "a01", "text": "Charge from day one", "applies_when": "a paid product",
                                         "evidence_span": quote}],
                       "facts": [{"fact_id": "f01", "text": "Users meet weekly", "evidence_span": quote}],
                       "entities": {"organizations": ["Acme"], "concepts": ["replication"], "tools": ["Postgres"],
                                    "works": ["DDIA"]}})


def test_the_neutral_prompt_extracts_facts_and_conditions_into_the_same_stored_record():
    try:
        from ytbrain.extract import runner, versions
        from ytbrain.knowledge.items import build_items
    except ImportError:
        _skipped("optional dependency not installed")
        return
    quote = _UTTS[3]["text"]
    sent = []

    def fake(prompt, schema, **kw):
        sent.append((prompt, schema))
        return _neutral_answer(quote)
    runner.BACKENDS["fake-neutral"] = fake
    tr = {"doc_id": "nv1", "utterances": _UTTS}
    chaps = [{"chapter_id": "c1", "title": "All", "start_ms": 0}]
    rec = runner.extract_video(tr, {"doc_id": "nv1", "title": "T", "series": "S", "source_kind": "talk",
                                    "prompt_variant": "neutral"}, chaps, backend="fake-neutral").model_dump()
    props = sent[0][1]["properties"]
    assert "facts" in props and "category" not in props and "stage_relevance" not in props, "no startup taxonomy"
    assert "applies_when" in sent[0][0] and "not as startup advice" in sent[0][0]
    assert (rec["category"], rec["stage_relevance"], rec["topics"]) == ("other", [], ["database replication"])
    assert rec["advice_atoms"][0]["applies_when"] == "a paid product" and rec["facts"][0]["text"] == "Users meet weekly"
    assert rec["entities"]["companies"] == ["Acme"] and rec["entities"]["frameworks"] == ["replication", "Postgres"]
    em = rec["extraction_meta"]
    assert (em["prompt_variant"], em["schema_version"]) == ("neutral", versions.VARIANTS["neutral"].latest)
    assert em["prompt_hash"] == versions.fingerprints("neutral")["talk"]
    report = verify.verify_record(rec, _UTTS)
    assert report["checked"] == 3 and report["failed"] == 0 and not report["issues"], "facts are verified too"
    items = build_items(rec, None)
    kinds = sorted(i["kind"] for i in items)
    assert kinds == ["advice", "fact", "summary", "takeaway"], kinds
    adv = next(i for i in items if i["kind"] == "advice")
    assert adv["text"].endswith("(when: a paid product)") and adv["topics"] == ["database replication"]
    # the startup prompt is unchanged for everything else
    sent.clear()
    runner.BACKENDS["fake-neutral"] = lambda prompt, schema, **kw: (sent.append((prompt, schema)), _gen(quote, n_high=1))[1]
    old = runner.extract_video(tr, {"doc_id": "nv2", "title": "T", "series": "S", "source_kind": "talk"}, chaps,
                               backend="fake-neutral").model_dump()
    assert "category" in sent[0][1]["properties"] and old["extraction_meta"]["prompt_variant"] == "startup"
    assert old["extraction_meta"]["prompt_hash"] == versions.fingerprints("startup")["talk"] and old["facts"] == []


def test_which_prompt_a_document_gets():
    from ytbrain.extract import versions as V
    assert V.variant_for(None, None, False) == "startup", "nothing known yet: today's prompt"
    assert V.variant_for(None, {"startup", "gtm", "finance"}, False) == "startup"
    assert V.variant_for(None, {"coding"}, False) == "neutral"
    assert V.variant_for(None, {"startup", "system-design"}, False) == "startup", \
        "a founder Domain keeps today's prompt until parity (L1)"
    assert V.variant_for(None, {"coding", "system-design", "investment"}, False) == "neutral"
    assert V.variant_for(None, {"startup"}, True) == "neutral", "after parity, everything new"
    p = V.parity_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps({"passed": True, "neutral": V.stamp("neutral")}))
        assert V.parity_passed() and V.variant_for(None, {"startup"}) == "neutral"
        p.write_text(json.dumps({"passed": True, "neutral": "neutral@0.9.0"}))
        assert not V.parity_passed(), "a pass for an older neutral prompt doesn't count"
    finally:
        p.unlink(missing_ok=True)


def _tagdb_with(docs: dict):
    from ytbrain import tagging as T
    db = T.TagDB()
    db.replace({}, {d: T.DocTag("s", tuple(doms)) for d, doms in docs.items()}, {"applied": "0"})
    db.close()


def test_upgrade_moves_coding_chapters_to_the_neutral_prompt_and_leaves_startup_ones():
    import io, contextlib, types
    from ytbrain import cli
    from ytbrain import tagging as T
    from ytbrain.config import MANIFEST_DB
    m = Manifest(MANIFEST_DB)
    _upgrade_fixture(m, [("upn-a", 0.01, 100), ("upn-b", 0.01, 100)])
    _tagdb_with({"upn-a": ["coding"], "upn-b": ["startup"]})
    args = types.SimpleNamespace(dry_run=True, max_cost=None, variant=None, doc=["upn-"])
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert cli.cmd_upgrade(args) == 0
        assert "nothing to do" in out.getvalue(), "content tags alone never switch a prompt before parity (L1)"
        real = cli._doc_tags
        cli._doc_tags = lambda m, ids, default=True: {d: (["coding"] if d == "upn-a" else [], "s") for d in ids}
        try:                                          # declared: a Book in the coding folder
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert cli.cmd_upgrade(args) == 0
        finally:
            cli._doc_tags = real
        text = out.getvalue()
        assert "1 record(s) startup@2.2.0 -> neutral@1.0.0" in text and "~$0.01" in text, text
    finally:
        T.TAGS_DB.unlink(missing_ok=True)
        m.tombstone(["upn-a", "upn-b"])


def test_parity_compares_both_prompts_on_the_same_talks_and_resumes():
    from ytbrain.eval import parity as PA
    quote = _UTTS[3]["text"]
    old = {"doc_id": "p1", "highlights": [{"text": "a", "evidence_span": quote}, {"text": "b", "evidence_span": quote}],
           "extraction_meta": {}}
    good = {"doc_id": "p1", "highlights": [{"text": "a", "evidence_span": quote}],
            "facts": [{"fact_id": "f01", "text": "f", "evidence_span": quote}],
            "advice_atoms": [{"atom_id": "a01", "text": "x", "evidence_span": quote}],
            "extraction_meta": {"calls": [{"cost": 0.01}]}}
    bad = {**good, "facts": [{"fact_id": "f01", "text": "f", "evidence_span": "nothing like this appears anywhere at all ok"}],
           "advice_atoms": [], "highlights": []}
    extracted = []
    with tempfile.TemporaryDirectory() as d:
        def run(new, out):
            def extract(tr, o):
                extracted.append(o["doc_id"])
                return new
            return PA.run(["p1", "p2"], lambda doc: ({**old, "doc_id": doc}, {"utterances": _UTTS}), extract,
                          verify.verify_record, Path(d) / out, "neutral@1.0.0", say=lambda m: None)
        r = run(good, "a")
        assert r["passed"] and r["n"] == 2 and r["neutral_items"] == 6 and r["startup_items"] == 4 and r["cost"] == 0.02
        assert run(good, "a")["passed"] and extracted == ["p1", "p2"], "resumed from the shadow files: no new calls"
        r = run(bad, "b")
        assert not r["passed"] and "pass rate" in r["why"] and "Verified items" in r["why"]
    assert not PA.summarize([], 30, "neutral@1.0.0")["passed"]


def test_enrich_adds_rules_and_facts_whose_quotes_verify_and_never_runs_twice():
    import io, contextlib, types
    from unittest import mock
    from ytbrain import cli
    from ytbrain import enrich as E
    from ytbrain import tagging as T
    from ytbrain.config import MANIFEST_DB, METADATA, TRANSCRIPTS
    from ytbrain.knowledge.items import build_items
    quote = _UTTS[5]["text"]
    assert E.wants({"coding"}) and not E.wants({"startup", "gtm"})
    rec = {"doc_id": "enr-a", "source_kind": "chapter", "title_canonical": "Ch", "series": "Book", "highlights": [],
           "advice_atoms": [], "extraction_meta": {"processed_at": "2026-10-01T00:00:00Z", "prompt_hash": "x",
                                                   "schema_version": "2.2.0", "calls": [{"cost": 0.01}]}}
    p, cls = E.build_prompt(rec, {"utterances": _UTTS})
    assert cls is E.RulesAndFacts and "RULES and FACTS" in p and "Chapter: Ch" in p and "Book: Book" in p
    assert E.build_prompt({**rec, "facts": [{"text": "x"}]}, {"utterances": _UTTS})[1] is E.RulesOnly
    m = Manifest(MANIFEST_DB)
    m.upsert_document("enr-a", "s", title="Ch")
    for st in ("clean", "extract", "verify"):
        m.mark(StageState("enr-a", st, "ok", version="2.2.0" if st == "extract" else None))
    (METADATA / "enr-a.json").write_text(json.dumps(rec))
    (TRANSCRIPTS / "enr-a.json").write_text(json.dumps({"doc_id": "enr-a", "utterances": _UTTS}))
    _tagdb_with({"enr-a": ["coding"]})
    answer = E.RulesAndFacts(rules=[E._Rule(rule_id="z9", text="Always version your API", authority="the author",
                                            evidence_span=quote)],
                             facts=[E._Fact(fact_id="q", text="Weekly user calls are held", evidence_span=quote)])
    args = lambda **k: types.SimpleNamespace(**{"dry_run": False, "max_cost": None, "doc": ["enr-"], "limit": 0, **k})
    try:
        with mock.patch.object(E, "llm_ask", lambda model: (lambda prompt, c: (answer, 0.003))):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert cli.cmd_enrich(args(dry_run=True)) == 0
            assert "1 Document(s)" in out.getvalue() and "~$0.01" in out.getvalue(), out.getvalue()
            assert m.stage_status("enr-a", "enrich") is None
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert cli.cmd_enrich(args()) == 0
            new = json.loads((METADATA / "enr-a.json").read_text())
            assert [r["rule_id"] for r in new["rules"]] == ["r01"] and [f["fact_id"] for f in new["facts"]] == ["f01"]
            assert new["extraction_meta"]["enrich"]["version"] == E.PROMPT_VERSION
            assert m.stage_status("enr-a", "verify") == "stale" and m.stage("enr-a", "enrich")["version"] == E.STAMP
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cli.cmd_enrich(args())
            assert "0 Document(s)" in out.getvalue(), "enriched once per extraction and prompt"
        report = verify.verify_record(new, _UTTS)
        assert report["checked"] == 2 and report["failed"] == 0
        rule = next(i for i in build_items(new, None) if i["kind"] == "rule")
        assert rule["item_id"] == "rul:enr-a:r01" and rule["text"] == "Always version your API [the author]"
    finally:
        T.TAGS_DB.unlink(missing_ok=True)
        m.tombstone(["enr-a"])



# --- enrich runs its model calls in parallel, as extract does ---------------

def _enrich_fixture(prefix: str, n: int):
    """n coding Documents, verified and waiting to be enriched; returns (manifest, ids, the answer every call gives)."""
    from ytbrain import enrich as E
    from ytbrain.config import MANIFEST_DB, METADATA, TRANSCRIPTS
    quote = _UTTS[5]["text"]
    m = Manifest(MANIFEST_DB)
    ids = [f"{prefix}{k}" for k in range(1, n + 1)]
    for d in ids:
        rec = {"doc_id": d, "source_kind": "chapter", "title_canonical": f"Ch {d}", "series": "Book", "highlights": [],
               "advice_atoms": [], "extraction_meta": {"processed_at": "2026-10-01T00:00:00Z", "prompt_hash": "x",
                                                       "schema_version": "2.2.0", "calls": [{"cost": 0.01}]}}
        m.upsert_document(d, "s", title=f"Ch {d}")
        for st in ("clean", "extract", "verify"):
            m.mark(StageState(d, st, "ok", version="2.2.0" if st == "extract" else None))
        (METADATA / f"{d}.json").write_text(json.dumps(rec))
        (TRANSCRIPTS / f"{d}.json").write_text(json.dumps({"doc_id": d, "utterances": _UTTS}))
    _tagdb_with({d: ["coding"] for d in ids})
    answer = E.RulesAndFacts(rules=[E._Rule(rule_id="z9", text="Always version your API", authority="the author",
                                            evidence_span=quote)],
                             facts=[E._Fact(fact_id="q", text="Weekly user calls are held", evidence_span=quote)])
    return m, ids, answer


def _enrich_run(prefix, fake_ask, **kw):
    """cmd_enrich over the Documents with this prefix, the model replaced by fake_ask(prompt, cls) -> (answer, cost)."""
    import io, contextlib, types
    from unittest import mock
    from ytbrain import cli
    from ytbrain import enrich as E
    args = types.SimpleNamespace(**{"dry_run": False, "max_cost": None, "doc": [prefix], "limit": 0, **kw})
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(E, "llm_ask", lambda model: fake_ask):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_enrich(args)
    return code, out.getvalue(), err.getvalue()


def _enrich_cleanup(m, ids):
    from ytbrain import tagging as T
    T.TAGS_DB.unlink(missing_ok=True)
    m.tombstone(ids)


def test_enrich_in_parallel_gives_the_same_records_and_writes_only_on_the_main_thread():
    """`--workers N` runs the model calls on N threads; the records, the manifest states and the progress are those of a
    serial run, and every file write happens on the calling thread (one SQLite writer, as in extract)."""
    import threading, time
    from unittest import mock
    from ytbrain import cli
    from ytbrain import enrich as E
    from ytbrain.config import METADATA
    results = {}
    for tag, workers in (("enrs", 1), ("enrp", 4)):
        m, ids, answer = _enrich_fixture(tag, 6)
        lock, active, peak, wrote_on = threading.Lock(), [0], [0], set()

        def ask(prompt, cls, answer=answer, lock=lock, active=active, peak=peak):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with lock:
                active[0] -= 1
            return answer, 0.003

        real = cli.pages.write_record
        try:
            with mock.patch.object(cli.pages, "write_record",
                                   lambda rec, real=real, w=wrote_on: (w.add(threading.current_thread().name), real(rec))[1]):
                code, out, err = _enrich_run(tag, ask, workers=workers)
            assert code == 0, (out, err)
            assert wrote_on == {threading.current_thread().name}, "writes stay on the main thread"
            assert f"{workers} worker(s)" in out and "(6/6)" in out and "6 Document(s) enriched, 0 failed" in out, out
            assert all(m.stage(d, "enrich")["version"] == E.STAMP and m.stage_status(d, "verify") == "stale" for d in ids)
            results[workers] = ([json.loads((METADATA / f"{d}.json").read_text())["rules"] for d in ids], peak[0])
        finally:
            _enrich_cleanup(m, ids)
    assert results[1][1] == 1 and 2 <= results[4][1] <= 4, "serial stays serial; four workers overlap, never more than four"
    assert results[1][0] == results[4][0], "the same records however many workers"


def test_enrich_default_workers_are_extracts_and_the_spend_cap_stops_new_calls_only():
    """No --workers: YTBRAIN_LLM_WORKERS, like extract. --max-cost stops dispatching; calls in flight finish, so a
    parallel run overshoots by fewer Documents than workers; a second run does the rest and nothing twice."""
    import threading, time
    from unittest import mock
    from ytbrain import config
    m, ids, answer = _enrich_fixture("enrc", 12)
    lock, active, peak, calls = threading.Lock(), [0], [0], []

    def ask(prompt, cls):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            calls.append(1)
        time.sleep(0.1)
        with lock:
            active[0] -= 1
        return answer, 0.003

    try:
        with mock.patch.object(config, "LLM_WORKERS", 3):
            code, out, err = _enrich_run("enrc", ask, max_cost=0.01)
        assert code == 0 and "3 worker(s)" in out and "spend cap $0.01" in out, out
        assert peak[0] == 3, "the default is extract's LLM_WORKERS"
        done = [d for d in ids if m.stage_status(d, "enrich") == "ok"]
        assert 4 <= len(done) <= 4 + 2, "at most workers - 1 Documents past the cap"
        assert f"stopped at the spend cap" in out and f"{12 - len(done)} Document(s) wait for the next run" in out, out
        before = len(calls)
        code, out, err = _enrich_run("enrc", ask, workers=2)
        assert code == 0 and len(calls) - before == 12 - len(done), "the rest, and nothing twice"
        assert all(m.stage_status(d, "enrich") == "ok" for d in ids)
    finally:
        _enrich_cleanup(m, ids)


def test_enrich_a_refusing_endpoint_stops_the_run_and_a_failing_document_does_not():
    """BackendUnavailable stops everything (exit 1, nothing in flight marked); one Document's error is recorded and
    the others are saved, then a rerun does only that one."""
    import threading
    from unittest import mock
    from ytbrain import runstatus
    from ytbrain.extract import runner
    m, ids, answer = _enrich_fixture("enrf", 6)
    bad, attempts = ids[2], []

    def flaky(prompt, cls):
        if f"Ch {bad}" in prompt:
            attempts.append(1)
            if len(attempts) == 1:
                raise ValueError("garbled")
        return answer, 0.003

    def refusing(prompt, cls):
        raise runner.BackendUnavailable("credits exhausted")

    try:
        code, out, err = _enrich_run("enrf", flaky, workers=3)
        assert code == 0 and "5 Document(s) enriched, 1 failed" in out and "FAILED" in out, out
        assert [d for d in ids if m.stage_status(d, "enrich") == "ok"] == [d for d in ids if d != bad]
        assert m.stage_status(bad, "enrich") != "ok"
        code, out, err = _enrich_run("enrf", flaky, workers=3)
        assert code == 0 and "1 Document(s) enriched" in out and len(attempts) == 2, "a rerun does only the failed one"
    finally:
        _enrich_cleanup(m, ids)
    m, ids, answer = _enrich_fixture("enrx", 6)
    try:
        with mock.patch.object(runstatus, "record", lambda *a, **k: None):
            code, out, err = _enrich_run("enrx", refusing, workers=3)
        assert code == 1 and "credits exhausted" in err and "STOPPED" in out, (out, err)
        assert all(m.stage_status(d, "enrich") is None for d in ids), "nothing in flight is marked, ok or failed"
    finally:
        _enrich_cleanup(m, ids)


def test_enrich_gives_up_on_a_call_that_never_answers_and_the_rerun_does_only_that_one():
    """A provider that hangs on one request (it trickles bytes, so no timeout trips) must not hold the whole run:
    that Document is recorded as failed after the deadline, a fresh worker takes its place, the others are saved."""
    import threading
    from unittest import mock
    from ytbrain import cli
    m, ids, answer = _enrich_fixture("enrh", 5)
    release = threading.Event()

    def hung(prompt, cls):
        if f"Ch {ids[1]}" in prompt:
            release.wait(30)
        return answer, 0.003

    try:
        with mock.patch.object(cli, "ENRICH_CALL_DEADLINE_S", 0.6):
            code, out, err = _enrich_run("enrh", hung, workers=2)
        release.set()
        assert code == 0 and "4 Document(s) enriched, 1 failed" in out and "no answer after" in out, out
        assert m.stage_status(ids[1], "enrich") != "ok" and all(m.stage_status(d, "enrich") == "ok" for d in ids if d != ids[1])
        code, out, err = _enrich_run("enrh", lambda p, c: (answer, 0.003), workers=2)
        assert code == 0 and "1 Document(s) enriched" in out, "the rerun does only the one that hung"
    finally:
        _enrich_cleanup(m, ids)


# --- serialization + lock --------------------------------------------------

def test_atomic_write_replaces_whole_file_and_leaves_no_temp():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rec.json"
        pages.atomic_write_text(p, "old")
        pages.atomic_write_text(p, "new")
        assert p.read_text() == "new"
        assert sorted(x.name for x in Path(d).iterdir()) == ["rec.json"]


def test_canonical_json_is_key_order_independent():
    """Without this, every re-run produces a meaningless diff and git history dies."""
    a = {"b": 1, "a": {"y": 2.123456, "x": [3, 1]}}
    b = {"a": {"x": [3, 1], "y": 2.123456}, "b": 1}
    assert pages.canonical_json(a) == pages.canonical_json(b)
    assert "2.123" in pages.canonical_json(a)


def test_markdown_render_is_stable_and_links_timestamps():
    rec = {"doc_id": "abc", "title_raw": "T", "title_canonical": "T",
           "url": "u", "stage_relevance": ["mvp", "idea"],
           "highlights": [{"text": "H", "evidence_span": "q", "timestamp_ms": 72000}],
           "extraction_meta": {"validation_status": "pass"}}
    out = pages.render_markdown(rec)
    assert pages.render_markdown(rec) == out
    assert "stage_relevance: [idea, mvp]" in out          # sorted, stable
    assert "watch?v=abc&t=72s" in out and "[01:12]" in out


def test_page_withholds_claims_whose_quote_was_not_found():
    rec = {"doc_id": "abc", "title_raw": "T",
           "highlights": [{"text": "Real", "evidence_span": "q1", "timestamp_ms": 5000, "match_score": 0.95},
                          {"text": "Invented", "evidence_span": "q2", "match_score": 0.2}],
           "advice_atoms": [{"atom_id": "a1", "text": "Made up advice", "evidence_span": "q3",
                             "match_score": 0.1}],
           "extraction_meta": {"verification": {"checked": 3, "failed": 2, "threshold": 0.7}}}
    page = pages.render_markdown(rec)
    assert "Real" in page and "Invented" not in page and "Made up advice" not in page
    assert "withheld_unverified: 2" in page and "2 claim(s) withheld" in page
    review = pages.render_markdown(rec, show_unverified=True)     # the human sample sheet
    assert "Invented" in review and "Made up advice" in review
    assert review.count(pages.UNVERIFIED_MARK) == 2
    unverified = dict(rec, extraction_meta={})                     # before `ytbrain verify`
    assert "Invented" in pages.render_markdown(unverified)
    assert "Not yet verified" in pages.render_markdown(unverified)


def test_lock_is_exclusive_and_clears():
    lock = Path(_TMP) / "t.lock"
    with exclusive(lock):
        assert lock.exists()
        try:
            with exclusive(lock):
                raise AssertionError("second acquisition should have failed")
        except LockBusy:
            pass
    with exclusive(lock):                       # released on exit: can be taken again
        pass


def test_lock_held_by_a_killed_process_is_free_even_if_its_pid_is_reused():
    import subprocess, signal, time as _t
    lock = Path(_TMP) / "crash.lock"
    code = ("import sys, time; sys.path.insert(0, %r); from ytbrain.lock import exclusive\n"
            "from pathlib import Path\nwith exclusive(Path(%r)):\n    print('held', flush=True); time.sleep(60)"
            % (str(Path(__file__).resolve().parents[1]), str(lock)))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    try:
        with exclusive(lock):
            raise AssertionError("a live holder must block")
    except LockBusy:
        pass
    p.send_signal(signal.SIGKILL)
    p.wait()
    lock.write_text("1")                         # a stale PID (1 is always alive) must not block
    with exclusive(lock):
        pass


# --- schema (needs pydantic) -----------------------------------------------

def test_schema_shape_if_pydantic_available():
    try:
        from ytbrain.extract.schema import Generated, VideoMetadata
    except ImportError:
        _skipped("pydantic not installed")
        return
    gen = set(Generated.model_json_schema()["properties"])
    assert "quotable_claims" not in gen and "prerequisites" not in gen   # trimmed
    assert {"summary", "highlights", "advice_atoms", "category"} <= gen
    assert "chapters" in VideoMetadata.model_json_schema()["properties"]


def test_chapter_snapping_if_pydantic_available():
    try:
        from ytbrain.extract.runner import snap_chapters
        from ytbrain.extract.schema import Chapter
    except ImportError:
        _skipped("pydantic not installed")
        return
    utts = [{"start_ms": i * 5000, "end_ms": (i + 1) * 5000, "text": "w"} for i in range(6)]
    out = snap_chapters([Chapter(chapter_id="c1", title="a", start_ms=1234)], utts)
    assert out[0].start_ms == 0        # snapped to a real utterance start


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


def test_rate_halves_on_429_bursts_and_climbs_back():
    from ytbrain.extract import runner
    saved, runner._pacers = runner._rpm_ceiling[0], {}
    try:
        runner.set_max_rpm(60)
        t = 1000.0
        assert runner._rate_limited(t) == 30                 # first 429: halve
        assert runner._rate_limited(t + 1) is None           # same burst: counted once
        assert runner._went_through(t + 30) is None          # not quiet long enough
        assert runner._rate_limited(t + 25) == 15            # a new burst later halves again
        for _ in range(3):
            runner._rate_limited(t + 25 + 21 * (_ + 1))
        assert runner.current_rpm() == 6                     # never below the floor
        t2 = t + 200
        assert runner._went_through(t2) == 12                # +10 % of the ceiling per quiet minute
        assert runner._went_through(t2 + 10) is None
        for i in range(1, 10):
            runner._went_through(t2 + 60 * i)
        assert runner.current_rpm() == 60                    # back to the ceiling, not above
        assert runner._went_through(t2 + 6000) is None
        runner.set_max_rpm(4)                                # a ceiling under the floor is respected
        assert runner._rate_limited(t2 + 9000) == 4
    finally:
        runner._rpm_ceiling[0], runner._pacers = saved, {}


def test_a_429_right_after_a_speed_up_still_cuts_the_rate():
    from ytbrain.extract import runner
    saved, runner._pacers = runner._rpm_ceiling[0], {}
    try:
        runner.set_max_rpm(60)
        assert runner._rate_limited(1000.0) == 30
        assert runner._went_through(1061.0) == 36            # a quiet minute: speed up
        assert runner._rate_limited(1065.0) == 18            # 4 s later a 429: cut again, not ignored
    finally:
        runner._rpm_ceiling[0], runner._pacers = saved, {}


def test_an_upstream_429_slows_only_that_model_but_an_account_429_slows_all():
    """OpenRouter says "qwen/x is temporarily rate-limited upstream": that model's limit. It must not hold
    back the other judges; a 429 that names no upstream is the account's and still slows every model."""
    from ytbrain.extract import runner
    saved, runner._pacers = runner._rpm_ceiling[0], {}
    try:
        runner.set_max_rpm(60)
        assert runner._rate_limited(1000.0, key="qwen") == 30
        assert runner.current_rpm("qwen") == 30 and runner.current_rpm("gemma") == 60
        assert runner.current_rpm() == 30                    # no key: the slowest model, for "is anything throttled"
        assert runner._went_through(1061.0, key="gemma") is None     # gemma is at its ceiling: nothing to raise
        assert runner._went_through(1061.0, key="qwen") == 36        # each model climbs back by itself
        assert runner.current_rpm("gemma") == 60
        assert runner._rate_limited(2000.0, key="gemma", everyone=True) == 30
        assert runner.current_rpm("qwen") == 18 and runner.current_rpm("gemma") == 30
    finally:
        runner._rpm_ceiling[0], runner._pacers = saved, {}


def test_a_pause_holds_one_model_unless_the_limit_is_the_accounts():
    from unittest import mock
    from ytbrain.extract import runner
    saved, runner._pacers = runner._rpm_ceiling[0], {}
    try:
        with mock.patch.object(runner.time, "sleep"), mock.patch.object(runner, "_say"):
            runner._acquire_slot("https://openrouter.ai/api/v1", "gemma")      # creates both schedules
            runner._acquire_slot("https://openrouter.ai/api/v1", "qwen")
            runner._backoff(0, "30", "HTTP 429 from qwen", pause_all=True, key="qwen", everyone=False)
            assert runner._pacers["qwen"].pause_until > runner.time.monotonic() + 20
            assert runner._pacers["gemma"].pause_until == 0.0
            runner._backoff(0, "30", "HTTP 429", pause_all=True, key="qwen", everyone=True)
            assert runner._pacers["gemma"].pause_until > runner.time.monotonic() + 20
    finally:
        runner._rpm_ceiling[0], runner._pacers = saved, {}


def test_post_with_backoff_names_the_throttled_model_and_leaves_the_others_alone():
    """The 429 text decides the scope: an upstream one is the body's model's alone."""
    from types import SimpleNamespace as NS
    from unittest import mock
    from ytbrain.extract import runner
    upstream = ('{"error":{"message":"Provider returned error","code":429,"metadata":{"raw":'
                '"qwen/qwen3.8-flash is temporarily rate-limited upstream."}}}')
    saved, runner._pacers = runner._rpm_ceiling[0], {}
    notes: list[str] = []
    runner._tls.on_step = notes.append
    try:
        runner.set_max_rpm(60)
        with mock.patch("httpx.post", return_value=NS(status_code=429, text=upstream, headers={})), \
                mock.patch.object(runner.time, "sleep"):
            try:
                runner._post_with_backoff("https://openrouter.ai/api/v1/chat/completions",
                                          {"model": "qwen/qwen3.8-flash"}, 5, {})
            except runner.RetriesExhausted:
                pass
            else:
                raise AssertionError("six 429s should exhaust the retries")
        assert runner.current_rpm("qwen/qwen3.8-flash") < 60 and runner.current_rpm("google/gemma-4-31b-it") == 60
        assert any("slowing qwen/qwen3.8-flash to" in n for n in notes), notes
        assert any("HTTP 429 from qwen/qwen3.8-flash, retry 1/" in n for n in notes), notes
        notes.clear()
        with mock.patch("httpx.post", return_value=NS(status_code=429, text="Too many requests", headers={})), \
                mock.patch.object(runner.time, "sleep"):
            try:
                runner._post_with_backoff("https://openrouter.ai/api/v1/chat/completions",
                                          {"model": "google/gemma-4-31b-it"}, 5, {})
            except runner.RetriesExhausted:
                pass
        assert any("slowing every model to" in n for n in notes), notes      # no upstream named: the account's limit
        assert runner.current_rpm("qwen/qwen3.8-flash") < 60 and runner.current_rpm("google/gemma-4-31b-it") < 60
    finally:
        runner._tls.on_step = None
        runner._rpm_ceiling[0], runner._pacers = saved, {}


def test_the_providers_whole_429_reason_is_kept_in_the_error():
    """"(Alibaba) Rate limit exceeded ... add your own key" sat past character 200 and was cut off."""
    from types import SimpleNamespace as NS
    from unittest import mock
    from ytbrain.extract import runner
    reason = "x" * 230 + " (Alibaba) Rate limit exceeded. Consider adding your own API key"
    saved, runner._pacers = runner._rpm_ceiling[0], {}
    try:
        with mock.patch("httpx.post", return_value=NS(status_code=429, text=reason, headers={})), \
                mock.patch.object(runner.time, "sleep"), mock.patch.object(runner, "_say"):
            try:
                runner._post_with_backoff("https://openrouter.ai/api/v1/chat/completions", {"model": "m"}, 5, {})
            except runner.RetriesExhausted as e:
                assert "adding your own API key" in str(e), str(e)
            else:
                raise AssertionError("six 429s should exhaust the retries")
    finally:
        runner._rpm_ceiling[0], runner._pacers = saved, {}


def test_retry_after_is_honoured_but_capped():
    from unittest import mock
    from ytbrain.extract import runner
    waits = []
    with mock.patch.object(runner.time, "sleep", waits.append), mock.patch.object(runner, "_say"):
        runner._backoff(0, "3600", "HTTP 429")
        runner._backoff(0, "7", "HTTP 429")
    assert waits == [runner.LLM_BACKOFF_MAX_S, 7.0]



def test_source_kinds_own_their_locators_and_moments_behind_one_interface():
    """ADR-0013: adding a source type is one SourceKind; nothing else branches on the kind."""
    from ytbrain import source_kinds as K
    assert [k.name for k in K.KINDS][-1] == "talk", "Talk is the fallback, tried last"
    assert K.for_doc("w-abc") is K.ARTICLE and K.for_doc("9780753550304__secrets") is K.BOOK_CHAPTER
    assert K.for_doc("UdIPveR__jw") is K.TALK, "a YouTube id may hold '__'"
    for k, pos, label, mid in ((K.TALK, 61_000, "01:01", "UdIPveRyyjw_00060"),
                               (K.ARTICLE, 7, "¶7", "w-abc_p00007"),
                               (K.BOOK_CHAPTER, 47_003, "PDF p. 47", "9780753550304__secrets_b00047")):
        doc = mid.rsplit("_", 1)[0]
        assert k.label(pos) == label and K.by_name(k.name) is k and K.by_locator(k.locator) is k
        assert k.moment_id(doc, k.moment_start(pos)) == mid and K.for_moment(mid) is k
        assert isinstance(k.span(doc, k.moment_start(pos)), dict)
    assert K.by_name(None) is K.TALK and K.of_record({"locator": "page"}) is K.BOOK_CHAPTER
    assert K.BOOK_CHAPTER.label(47_003, {"47": "31"}) == "p. 31"



def test_diversity_rules_are_source_agnostic_and_compose():
    from founder_coach.search import (POLICIES, DiversityPolicy, NearDuplicateCollapse, PerDocumentCap,
                                      PerSeriesCap, SameQuote)
    rows = [{"i": i, "doc_id": d, "series": s, "text": t, "evidence": ""} for i, (d, s, t) in enumerate([
        ("a", "PG", "talk to users every week"), ("b", "PG", "charge money early"),
        ("c", "PG", "hire slowly and fire fast"), ("d", "PG", "focus on one metric"),
        ("e", "Zero to One", "talk to users every single week"), ("f", "", "no series here")])]
    assert [r["i"] for r in DiversityPolicy((PerSeriesCap(3, 10),)).apply(rows)] == [0, 1, 2, 4, 5], \
        "any Series (a blog here) is capped the same way; no Series, no cap"
    later = rows[:1] + rows[4:] + rows[1:4]
    assert [r["i"] for r in DiversityPolicy((PerSeriesCap(1, 2),)).apply(later)] == [0, 4, 5, 1, 2, 3], \
        "past the window nothing is capped"
    assert [r["i"] for r in DiversityPolicy((NearDuplicateCollapse(0.8),)).apply(rows)] == [0, 1, 2, 3, 5], \
        "a near-duplicate from another Document and source is dropped; the higher rank stays"
    assert POLICIES["default"].apply(rows) == DiversityPolicy((SameQuote(), PerDocumentCap(3))).apply(rows)
    assert POLICIES["series3"].rules[-1] == PerSeriesCap(3, 10)


# --- Domains (M5a) -----------------------------------------------------------------------------
def test_domains_yaml_declares_the_registry_and_a_missing_file_means_startup_only():
    from ytbrain import domains as D
    reg = D.load()                                           # the repo's own domains.yaml
    assert reg.names == ["startup", "gtm", "leadership", "system-design", "finance", "investment", "coding"] and reg.default == "startup"
    assert reg.get("finance").risk_tier == "high" and reg.get("finance").web_policy == "always_latest"
    assert reg.get("startup").half_life_days is None and reg.get("system-design").half_life_days == 1825
    assert all(d.description and d.examples for d in reg.domains.values()), "the router needs both"
    only = D.load(Path(tempfile.mkdtemp()) / "none.yaml")
    assert only.names == ["startup"] and only.default == "startup"


def test_domains_yaml_errors_say_what_to_fix():
    from ytbrain import domains as D
    bad = {
        "no domains": {"default": "x"},
        "bad name": {"domains": {"Not Ok": {}}},
        "bad tier": {"domains": {"a": {"risk_tier": "scary"}}},
        "bad policy": {"domains": {"a": {"web_policy": "sometimes"}}},
        "bad freshness": {"domains": {"a": {"freshness": "soon"}}},
        "zero days": {"domains": {"a": {"freshness": 0}}},
        "unknown key": {"domains": {"a": {"colour": "red"}}},
        "examples": {"domains": {"a": {"examples": "one question"}}},
        "default": {"default": "b", "domains": {"a": {}}},
    }
    for label, doc in bad.items():
        try:
            D.parse(doc)
        except D.DomainConfigError as e:
            assert "domains.yaml" in str(e), (label, str(e))
        else:
            raise AssertionError(f"{label}: should be refused")
    ok = D.parse({"domains": {"a": {"freshness": 30, "risk_tier": "high"}, "b": None}})
    assert ok.default == "a" and ok.get("a").half_life_days == 30 and ok.get("b").risk_tier == "low"
    p = Path(tempfile.mkdtemp()) / "domains.yaml"
    p.write_text("domains: [unclosed")
    try:
        D.load(p)
    except D.DomainConfigError as e:
        assert "not valid YAML" in str(e)
    else:
        raise AssertionError("broken YAML must be a DomainConfigError")


def test_stray_book_folders_are_named_and_ignore_folders_silences_a_sorting_folder():
    from ytbrain import domains as D
    reg = D.load()
    root = Path("/library/books")
    files = [root / "system-desing" / "A.pdf", root / "system-desing" / "B.pdf", root / "finance" / "C.pdf",
             root / "to-read" / "D.pdf", root / "Top.pdf", root / "leadership" / "misc" / "E.pdf",
             root / "typo" / "Own.pdf"]
    src = {"id": "b", "books": {"Own.pdf": {"domains": ["finance"]}}}
    assert D.stray_folders(reg, src, files, [root]) == {"system-desing": 2, "to-read": 1}
    quiet = D.parse({"domains": {"startup": {}, "finance": {}, "leadership": {}}, "ignore_folders": ["to-read"]})
    assert D.stray_folders(quiet, src, files, [root]) == {"system-desing": 2}
    assert quiet.ignore_folders == ("to-read",)
    try:
        D.parse({"domains": {"startup": {}}, "ignore_folders": "to-read"})
        raise AssertionError("ignore_folders must be a list")
    except D.DomainConfigError as e:
        assert "ignore_folders" in str(e)


def test_stray_folders_are_found_when_the_books_folder_is_reached_through_a_symlink():
    """macOS: a temp dir is /var/... but resolves to /private/var/...; a linked books folder does the same."""
    import tempfile

    from ytbrain import domains as D
    with tempfile.TemporaryDirectory() as t:
        real = Path(t).resolve() / "real" / "books"
        (real / "sytem-design").mkdir(parents=True)
        (real / "finance").mkdir()
        files = [(real / "sytem-design" / "X.pdf").resolve(), (real / "finance" / "Y.pdf").resolve()]
        link = Path(t) / "link"
        link.symlink_to(real.parent, target_is_directory=True)
        assert D.stray_folders(D.load(), {"id": "b"}, files, [link / "books"]) == {"sytem-design": 1}


DOMAINS_FIXTURE = """# The Library's Domains.
default: startup                       # the Domain of a Source that names none
domains:
  startup:
    description: "Startups."           # keep this comment
    risk_tier: medium
  coding:
    description: "Code."

# a comment after the block stays after it
ignore_folders: []   # sorting folders
"""


def test_adding_a_domain_appends_it_keeps_every_comment_and_validates_before_writing():
    import yaml
    from ytbrain import domains as D
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "domains.yaml"
        f.write_text(DOMAINS_FIXTURE)
        reg = D.add_domain("GTM", risk_tier="medium", description='Sales: "founder-led", outbound; pricing',
                           examples=["How do I price: per seat or usage?"], freshness=1095, path=f)
        assert reg.names == ["startup", "coding", "gtm"] and reg.get("gtm").half_life_days == 1095
        assert reg.get("gtm").description == 'Sales: "founder-led", outbound; pricing'
        assert reg.get("gtm").examples == ("How do I price: per seat or usage?",)
        text = f.read_text()
        for comment in ("# keep this comment", "# the Domain of a Source that names none",
                        "# a comment after the block stays after it", "# sorting folders"):
            assert comment in text, comment
        assert text.index("gtm:") < text.index("# a comment after the block"), "inside `domains:`, not after it"
        assert D.load(f).names == reg.names and yaml.safe_load(text)["ignore_folders"] == []
        for bad, why in ((dict(name="gtm"), "already"), (dict(name="Legal/Advice"), "lowercase"),
                         (dict(name="legal", description=" "), "description"),
                         (dict(name="legal", risk_tier="extreme"), "risk tier")):
            kw = {"risk_tier": "high", "description": "Law.", **bad}
            before = f.read_text()
            try:
                D.add_domain(kw.pop("name"), path=f, **kw)
                raise AssertionError(f"accepted {bad}")
            except D.DomainConfigError as e:
                assert why in str(e), (why, str(e))
            assert f.read_text() == before, "a refused edit changes nothing"
        missing = Path(t) / "none.yaml"
        reg = D.add_domain("legal", risk_tier="high", description="Law for founders.", path=missing)
        assert reg.names == ["startup", "legal"] and reg.get("legal").risk_tier == "high"


def test_ignoring_a_folder_adds_it_once_whatever_the_list_style():
    import yaml
    from ytbrain import domains as D
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "domains.yaml"
        f.write_text(DOMAINS_FIXTURE)
        D.ignore_folder("to-read", path=f)
        D.ignore_folder("To Read/", path=f)                     # the same folder: nothing added
        assert yaml.safe_load(f.read_text())["ignore_folders"] == ["to-read"] and "# sorting folders" in f.read_text()
        f.write_text(DOMAINS_FIXTURE.replace("ignore_folders: []   # sorting folders", "ignore_folders:\n  - old\n  - archive"))
        D.ignore_folder("drafts", path=f)
        assert yaml.safe_load(f.read_text())["ignore_folders"] == ["old", "archive", "drafts"]
        f.write_text(DOMAINS_FIXTURE.replace("ignore_folders: []   # sorting folders\n", ""))
        assert D.ignore_folder("misc", path=f).ignore_folders == ("misc",)


def test_a_folders_capitals_and_spaces_never_make_its_books_miss_their_domain():
    from ytbrain import domains as D
    reg = D.parse({"domains": {"startup": {}, "gtm": {}, "system-design": {}}, "ignore_folders": ["To Read"]})
    root = Path("/library/books")
    assert D.folder_domain(root / "GTM" / "A.pdf", reg, [root]) == "gtm"
    assert D.folder_domain(root / "System Design" / "B.pdf", reg, [root]) == "system-design"
    assert D.folder_domain(root / "system_design" / "C.pdf", reg, [root]) == "system-design"
    files = [root / "GTM" / "A.pdf", root / "to-read" / "D.pdf", root / "Legal" / "E.pdf"]
    assert D.stray_folders(reg, {"id": "b"}, files, [root]) == {"Legal": 1}


def test_the_domains_command_lists_adds_and_ignores():
    import contextlib
    import io
    from ytbrain import cli
    from ytbrain import domains as D
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "domains.yaml"
        f.write_text(DOMAINS_FIXTURE)
        real, D.DOMAINS_FILE = D.DOMAINS_FILE, f
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert cli.main(["domains", "add", "Legal", "--risk", "high", "--description", "Law for founders.",
                                 "--example", "Do I need a lawyer to incorporate?"]) == 0
                assert cli.main(["domains", "ignore", "to-read"]) == 0
                assert cli.main(["domains"]) == 0
            text = out.getvalue()
            assert "added `legal` (high risk)" in text and "ytbrain tag" in text
            assert "legal" in text and "high" in text and "ignore_folders: to-read" in text, text
            with contextlib.redirect_stderr(io.StringIO()) as err:
                assert cli.main(["domains", "add", "legal", "--risk", "low", "--description", "x"]) == 2
            assert "already" in err.getvalue()
        finally:
            D.DOMAINS_FILE = real


def test_a_documents_domains_come_from_its_book_then_its_folder_then_its_source_then_the_default():
    from ytbrain import domains as D
    reg = D.load()
    src = {"id": "books", "domains": ["startup"],
           "books": {"Odd.pdf": {"domains": ["finance", "leadership"]}}}
    root = Path("/library/books")
    assert D.resolve(reg, src, root / "leadership" / "High Output.pdf", [root]) == ["leadership"]     # the folder
    assert D.resolve(reg, src, root / "finance" / "Odd.pdf", [root]) == ["finance", "leadership"]      # the book wins
    assert D.resolve(reg, src, root / "misc" / "Other.pdf", [root]) == ["startup"]                     # the Source
    assert D.resolve(reg, {"id": "b"}, root / "misc" / "Other.pdf", [root]) == ["startup"]            # the default
    assert D.resolve(reg, {"id": "w", "domains": ["leadership"]}) == ["leadership"]
    assert D.resolve(reg) == ["startup"]
    # a parent directory above the Source's own folder never counts
    assert D.resolve(reg, {"id": "b"}, Path("/home/finance/books/Other.pdf"), [Path("/home/finance/books")]) == ["startup"]
    assert D.folder_domain(Path("/x/system-design/deep/dive/Book.pdf"), reg, [Path("/x")]) == "system-design"
    assert D.folder_domain(Path("/x/leadership/finance/Book.pdf"), reg, [Path("/x")]) == "finance", "the nearest folder"
    assert D.folder_domain(Path("/elsewhere/leadership/Book.pdf"), reg, [Path("/x")]) is None, "outside the Source"
    # the repo was moved after the plan was written: the Source's folder still matches by its last two parts
    assert D.folder_domain(Path("/Users/old/repo/data/books/finance/Book.pdf"), reg, [Path("/new/repo/data/books")]) == "finance"
    assert D.folder_domain(Path("/Users/old/finance/data/books/Book.pdf"), reg, [Path("/new/repo/data/books")]) is None
    try:
        D.resolve(reg, {"id": "w", "domains": ["cooking"]})
    except D.DomainConfigError as e:
        assert "cooking" in str(e) and "finance" in str(e), str(e)
    else:
        raise AssertionError("an undeclared Domain must be refused, naming the declared ones")


def test_sources_take_domains_and_books_may_override_them():
    from ytbrain import domains as D
    from ytbrain import sources as S
    reg = D.load()
    w = S.normalize({"type": "website", "url": "https://ex.com/essays/", "domains": "leadership"})
    assert w["domains"] == ["leadership"]
    w2 = S.normalize({"type": "website", "url": "https://ex.com/", "domains": ["startup", "startup", "finance"]})
    assert w2["domains"] == ["startup", "finance"]
    assert "domains" not in S.normalize({"type": "website", "url": "https://ex.com/"})
    b = S.normalize({"type": "pdf_books", "path": "data/books", "domains": ["leadership"],
                     "books": {"A.pdf": {"domains": "finance"}}})
    assert b["domains"] == ["leadership"] and b["books"]["A.pdf"]["domains"] == ["finance"]
    D.check_sources(reg, [w, w2, b])
    for entry in ({"type": "website", "url": "https://ex.com/", "domains": []},
                  {"type": "website", "url": "https://ex.com/", "domains": [3]},
                  {"type": "pdf_books", "path": "x", "books": {"A.pdf": {"domains": []}}}):
        try:
            S.normalize(entry)
        except S.SourceConfigError as e:
            assert "domains" in str(e)
        else:
            raise AssertionError(f"{entry} should be refused")
    try:
        D.check_sources(reg, [S.normalize({"type": "website", "url": "https://ex.com/", "domains": ["cooking"]})])
    except D.DomainConfigError as e:
        assert "cooking" in str(e)
    else:
        raise AssertionError("an undeclared Domain in sources.yaml must be refused")


def test_items_carry_their_documents_domains_and_source():
    from ytbrain.knowledge.items import build_items
    rec = {"doc_id": "dom000000001", "title_raw": "T", "summary": "A summary.", "source_kind": "talk",
           "advice_atoms": [], "highlights": []}
    plain = build_items(rec, None)
    assert plain and all(i["domains"] == ["startup"] and i["source_id"] == "" for i in plain)
    tagged = build_items(rec, None, ["leadership", "startup"], "books_x")
    assert all(i["domains"] == ["leadership", "startup"] and i["source_id"] == "books_x" for i in tagged)


def test_the_index_is_retagged_in_place_filters_by_domain_and_upgrades_an_old_table():
    try:
        import lancedb  # noqa: F401
        import pyarrow as pa
    except ImportError:
        return _skipped("the index extra is not installed")
    from founder_coach.search import Filter
    from ytbrain.knowledge import store as KS
    d = Path(tempfile.mkdtemp())
    old = [c for c in KS.STRING_COLS if c != "source_id"]
    lists = [c for c in KS.LIST_COLS if c != "domains"]
    fields = [pa.field(c, pa.string()) for c in old] + [pa.field(c, pa.list_(pa.string())) for c in lists] \
        + [pa.field(c, pa.int64()) for c in KS.INT_COLS] + [pa.field("vector", pa.list_(pa.float32(), 2))]
    t = lancedb.connect(str(d)).create_table("k", schema=pa.schema(fields))
    base = {**{c: ["a"] for c in lists}, **{c: 1 for c in KS.INT_COLS}}
    row = lambda doc, n, v: {**{c: f"{doc}-{n}" for c in old}, **base, "doc_id": doc, "item_id": f"{doc}:{n}", "vector": v,
                             "kind": "advice", "source_kind": "talk"}
    t.add([row("old1", 1, [1.0, 0.0]), row("old1", 2, [0.9, 0.1]), row("old2", 1, [0.0, 1.0])])    # a table from before Domains
    ks = KS.KnowledgeStore(d, "k")
    ks._upgrade()
    f = lambda *doms: sorted(r["doc_id"] for r in ks.vector_search([1.0, 0.0], Filter(domains=doms), 10))
    assert f("startup") == ["old1", "old1", "old2"], "untagged items are startup, never silently dropped"
    assert f("leadership") == []
    assert {tuple(r["domains"]) for r in ks.rows()} == {("startup",)}
    assert ks.retag({"old1": (["leadership", "startup"], "books_x"), "old2": (["startup"], "yt"), "gone": (["x"], "y")}) == 2
    assert f("leadership") == ["old1", "old1"] and f("startup") == ["old1", "old1", "old2"] and f("finance") == []
    assert f("leadership", "finance") == ["old1", "old1"]
    got = {r["doc_id"]: (sorted(r["domains"]), r["source_id"]) for r in ks._t.to_arrow().to_pylist()}
    assert got == {"old1": (["leadership", "startup"], "books_x"), "old2": (["startup"], "yt")}, got
    assert ks.count() == 3 and ks.retag({"old1": (["startup", "leadership"], "books_x")}) == 0, "same tags: nothing rewritten"
    assert ks.retag({"old1": (["startup"], "books_x")}) == 1 and f("leadership") == []
    assert KS.where_clause(domains=["a", "b'c"]).startswith("(array_has(domains, 'a') OR array_has(domains, 'b''c'))")
    assert "domains IS NULL" in KS.where_clause(domains=["startup"]) and "IS NULL" not in KS.where_clause(domains=["finance"])


def _tagged_store(n_docs, per=2):
    from ytbrain.knowledge import store as KS
    ks = KS.KnowledgeStore(Path(tempfile.mkdtemp()), "k")
    for i in range(n_docs):
        rows = [{**{c: f"{i}-{j}" for c in KS.STRING_COLS}, **{c: ["a"] for c in KS.LIST_COLS},
                 **{c: 1 for c in KS.INT_COLS}, "doc_id": f"d{i}", "item_id": f"d{i}:{j}", "kind": "advice",
                 "vector": [1.0 - i / 10, i / 10], "domains": ["startup"], "source_id": "s"} for j in range(per)]
        ks.replace_document(f"d{i}", rows, 2)
    return ks


def test_an_interrupted_retag_keeps_every_vector_and_the_old_tags_and_a_rerun_finishes():
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return _skipped("the index extra is not installed")
    from ytbrain.knowledge import store as KS
    ks = _tagged_store(5)
    dup = [{k: v for k, v in r.items() if not k.startswith("_")} for r in ks.document_items("d2")][:1]
    ks._t.add(dup)                                # the real index has a few repeated item ids; retag must not care
    tags = {f"d{i}": (["leadership"], "s2") for i in range(5)}
    real, calls = ks._t, []

    class Dies:                                   # the second batch's commit fails
        def __getattr__(self, k):
            return getattr(real, k)

        def update(self, *a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return real.update(*a, **k)
    old, KS.RETAG_BATCH = KS.RETAG_BATCH, 2
    try:
        ks._t = Dies()
        try:
            ks.retag(tags)
            raise AssertionError("the interruption should reach the caller")
        except KeyboardInterrupt:
            pass
        ks._t = real
        assert ks.count() == 11, "no item was lost"
        assert all(len(r["vector"]) == 2 for r in ks._t.to_arrow().to_pylist())
        done = {r["doc_id"]: r["domains"] for r in ks._t.to_arrow().to_pylist()}
        assert sorted(d for d, v in done.items() if v == ["leadership"]) == ["d0", "d1"], done   # batch one landed whole
        assert ks.retag(tags) == 3 and ks.retag(tags) == 0, "a rerun tags only what is left"
        assert {tuple(r["domains"]) for r in ks._t.to_arrow().to_pylist()} == {("leadership",)}
        assert ks.count() == 11 and len(calls) >= 2
    finally:
        KS.RETAG_BATCH = old


def test_index_tags_each_document_from_sources_domains_and_book_folders():
    from ytbrain import cli
    from ytbrain.config import BOOKS_PLANS, ROOT
    from ytbrain.manifest import Manifest
    tmp = Path(tempfile.mkdtemp())
    m = Manifest(tmp / "manifest.db")
    books = ROOT / "books-library"
    (BOOKS_PLANS).mkdir(parents=True, exist_ok=True)
    for doc, src in (("vid00000001", "yt_a"), ("web00000001", "site_a"), ("web00000002", "site_b"),
                     ("9780000000001__go", "books_all"), ("9780000000002__lead", "books_all"),
                     ("9780000000003__misc", "books_all"), ("orphan000001", "gone_source")):
        m.upsert_document(doc, src)
    for isbn, folder, name in (("9780000000001", "finance", "Go.pdf"), ("9780000000002", "leadership", "Lead.pdf"),
                               ("9780000000003", "misc", "Misc.pdf")):
        (BOOKS_PLANS / f"{isbn}.json").write_text(json.dumps({"format": 1, "path": str(books / folder / name)}))
    sources = tmp / "sources.yaml"
    sources.write_text(f"""sources:
- {{id: yt_a, kind: playlist, playlist_id: PL1}}
- {{id: site_a, type: website, url: "https://a.example.com/", domains: leadership}}
- {{id: site_b, type: website, url: "https://b.example.com/", domains: [startup, finance]}}
- {{id: books_all, type: pdf_books, path: {books}, domains: [startup]}}
""")
    real = cli.SOURCES
    cli.SOURCES = sources
    try:
        tags = cli._doc_tags(m, ["vid00000001", "web00000001", "web00000002", "9780000000001__go",
                                 "9780000000002__lead", "9780000000003__misc", "orphan000001", "unknown0001"])
        assert tags == {"vid00000001": (["startup"], "yt_a"), "web00000001": (["leadership"], "site_a"),
                        "web00000002": (["startup", "finance"], "site_b"),
                        "9780000000001__go": (["finance"], "books_all"),       # the folder names the Domain
                        "9780000000002__lead": (["leadership"], "books_all"),
                        "9780000000003__misc": (["startup"], "books_all"),     # no such folder: the Source's Domains
                        "orphan000001": (["startup"], "gone_source"),          # its Source left sources.yaml
                        "unknown0001": (["startup"], "")}, tags
        sources.write_text(sources.read_text().replace("domains: leadership", "domains: cooking"))
        try:
            cli._doc_tags(m, ["web00000001"])
        except SystemExit as e:
            assert "cooking" in str(e) and "sources.yaml" in str(e), str(e)
        else:
            raise AssertionError("an undeclared Domain must stop the index with a message")
        cli.SOURCES = tmp / "missing.yaml"                    # no sources.yaml: the index still builds, all startup
        assert cli._doc_tags(m, ["web00000001"]) == {"web00000001": (["startup"], "site_a")}
    finally:
        cli.SOURCES = real


def test_every_package_data_pattern_matches_a_shipped_file():
    """A data file the code reads at run time (the Gap-question starter, coach cases, playbooks) must be
    declared in pyproject's package-data, or an installed wheel lacks it while the editable install works."""
    import tomllib
    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    for pkg, patterns in declared.items():
        for pat in patterns:
            assert list((root / pkg).glob(pat)), f"package-data {pkg}/{pat} matches no file"
    for needed in ("eval/gap_questions.txt", "eval/golden.example.jsonl"):
        assert needed in declared["ytbrain"], needed
    assert "playbooks/*.md" in declared["founder_coach"]


if __name__ == "__main__":
    raise SystemExit(_run())
