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


def test_schema_bump_makes_old_records_pending():
    with tempfile.TemporaryDirectory() as d:
        m = Manifest(Path(d) / "m.db")
        for v, ver in (("old", "2.0.0"), ("new", "2.1.0")):
            m.upsert_document(v, "s")
            m.mark(StageState(v, "extract", "ok", version=ver))
        assert m.pending("extract") == []
        assert m.invalidate_old_versions("extract", "2.1.0") == 1
        assert m.pending("extract") == ["old"]


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
    saved = (runner._min_interval[0], runner._rpm_ceiling[0], runner._rpm_changed[0])
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
        runner._min_interval[0], runner._rpm_ceiling[0], runner._rpm_changed[0] = saved


def test_a_429_right_after_a_speed_up_still_cuts_the_rate():
    from ytbrain.extract import runner
    saved = (runner._min_interval[0], runner._rpm_ceiling[0], runner._rpm_changed[0], runner._rpm_cut[0])
    try:
        runner.set_max_rpm(60)
        assert runner._rate_limited(1000.0) == 30
        assert runner._went_through(1061.0) == 36            # a quiet minute: speed up
        assert runner._rate_limited(1065.0) == 18            # 4 s later a 429: cut again, not ignored
    finally:
        runner._min_interval[0], runner._rpm_ceiling[0], runner._rpm_changed[0], runner._rpm_cut[0] = saved


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


if __name__ == "__main__":
    raise SystemExit(_run())
