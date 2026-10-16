"""`ytbrain gaps` (docs/usage-log.md, follow-up): the report over the coaches' local usage logs. It reads the
`usage` table of each Project store read-only and nothing else. Each test builds its own engine home."""
import io
import json
import os
import sys
import tempfile
import contextlib
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_coach import Clock   # noqa: E402  (sets the hermetic environment first)

from founder_coach.store import FounderStore   # noqa: E402
from ytbrain import gaps as G                  # noqa: E402

NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)


def _project(home: Path, pack: str, pid: str, name: str, searches: list[dict], extra: list[tuple] = ()) -> Path:
    """One Project store with the given coach_search detail dicts (and other usage rows) in its usage table."""
    d = home / pack / "projects" / pid
    s = FounderStore(d, clock=Clock("2026-10-10T09:00:00+00:00"))
    try:
        (d / "project.json").write_text(json.dumps({"name": name}))
        for det in searches:
            outcome = "empty" if det.get("hits") == 0 else "gap" if det.get("coverage") == "none" else "ok"
            s.log_usage("coach_search", outcome, 20, run="r1", version="0.1.5", detail=det)
        for tool, outcome, det in extra:
            s.log_usage(tool, outcome, 5, run="r1", version="0.1.5", detail=det)
    finally:
        s.close()
    return d / "founder.db"


def _search(cov, sim, doms, by=None, query=None):
    det = {"mode": "semantic", "hits": 5, "top_similarity": sim, "coverage": cov, "domains_searched": doms}
    if by:
        det["coverage_by_domain"] = by
    if query:
        det["query"] = query
    return det


def _home() -> Path:
    return Path(tempfile.mkdtemp(prefix="ytbrain-gaps-test-"))


def test_g1_rates_searches_per_level_and_domain_and_ignores_other_tools():
    home = _home()
    _project(home, "founder", "filum", "Filum", [
        _search("strong", 0.77, ["startup"], by={"startup": "strong"}),
        _search("partial", 0.70, ["startup", "gtm"], by={"startup": "strong", "gtm": "partial"}),
        _search("partial", 0.66, ["startup"]),                       # before the per-Domain field: overall level
        _search(None, 0.7, None),                                    # before coverage was logged
        {"mode": "semantic", "hits": 0},                             # nothing found
        _search("none", 0.4, ["finance"], by={"finance": "none"}),
    ], extra=[("coach_get_context", "ok", {}), ("coach_update", "error", {"error": "ToolError: x"})])
    (p,) = G.load(home, days=None, now=NOW)
    s = G.summarize(p)
    assert (s["pack"], s["project"], s["searches"], s["calls"]) == ("founder", "Filum", 6, 8)
    assert s["levels"] == {"strong": 1, "partial": 2, "none": 1, "empty": 1, "unrated": 1}
    assert s["errors"] == 1
    assert s["domains"]["gtm"] == {"strong": 0, "partial": 1, "none": 0}
    assert s["domains"]["startup"] == {"strong": 2, "partial": 1, "none": 0}      # explicit + the overall fallback
    assert s["domains"]["finance"]["none"] == 1
    assert s["similarity"]["strong"]["median"] == 0.77 and s["similarity"]["partial"]["min"] == 0.66


def test_g2_question_text_is_listed_only_when_it_was_recorded_and_otherwise_the_report_says_how_to_turn_it_on():
    home = _home()
    _project(home, "coding", "api", "Billing API", [_search("partial", 0.68, ["coding"], query="sagas versus outbox")])
    (p,) = G.load(home, days=None, now=NOW)
    out = G.render(G.report([p], home, None), home)
    assert "sagas versus outbox" in out and "partial" in out and "USAGE_TEXT" not in out
    home2 = _home()
    _project(home2, "coding", "api", "Billing API", [_search("partial", 0.68, ["coding"])])
    (p2,) = G.load(home2, days=None, now=NOW)
    out2 = G.render(G.report([p2], home2, None), home2)
    assert "CODING_COACH_USAGE_TEXT=1" in out2 and "no question text was recorded" in out2


def test_g3_reading_never_changes_the_store_and_never_touches_coaching_memory():
    home = _home()
    db = _project(home, "founder", "filum", "Filum", [_search("partial", 0.7, ["startup"])])
    before = (db.read_bytes(), sorted(x.name for x in db.parent.iterdir()))
    G.load(home, days=None, now=NOW)
    assert (db.read_bytes(), sorted(x.name for x in db.parent.iterdir())) == before
    import re
    tables = set(re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)", Path(G.__file__).read_text()))
    # the store's usage table, and the Knowledge pack's items (no coaching memory lives there)
    assert tables == {"usage", "items"}, f"gaps.py must read only usage (store) and items (pack), not {tables}"


def test_g4_window_pack_filter_and_missing_logs():
    home = _home()
    db = _project(home, "founder", "filum", "Filum", [_search("partial", 0.7, ["startup"])])
    _project(home, "investor", "retire", "Retire", [])
    assert [p.pack for p in G.load(home, pack="investor", days=None, now=NOW)] == ["investor"]
    assert G.load(home, days=1, now=NOW)[0].rows == []                    # 2026-10-10 is older than one day
    assert len(G.load(home, days=30, now=NOW)[0].rows) == 1
    assert G.discover(home / "nowhere") == []
    assert "no coach Projects" in G.render(G.report([], home), home)
    import sqlite3
    c = sqlite3.connect(db)
    c.execute("DROP TABLE usage")
    c.commit()
    c.close()
    (p,) = G.load(home, pack="founder", days=None, now=NOW)
    assert p.rows == [] and "no usage log" in p.note


def test_g5_command_prints_text_and_json():
    from ytbrain import cli
    home = _home()
    _project(home, "founder", "filum", "Filum", [_search("strong", 0.77, ["startup"], by={"startup": "strong"})])

    def run(*argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["gaps", "--home", str(home), "--days", "0", *argv])
        return code, buf.getvalue()
    code, text = run()
    assert code == 0 and "founder coach" in text and "strong 1 (100%)" in text
    code, js = run("--json")
    rep = json.loads(js)
    assert code == 0 and rep["projects"][0]["levels"]["strong"] == 1


def test_g6_a_search_row_carries_the_per_domain_coverage_and_no_words():
    from founder_coach.server import _usage_detail
    sc = {"hits": [{"item_id": "tkw:A:01"}], "coverage": "partial", "top_similarity": 0.7, "domains_searched": ["startup", "gtm"],
          "coverage_by_domain": {"startup": "strong", "gtm": "partial"}, "coverage_basis": "provisional",
          "stale_domains": ["gtm"], "routing": "explicit"}
    outcome, d = _usage_detail("coach_search", {"query": "my secret pricing question"}, sc, False)
    assert outcome == "ok"
    assert d["coverage_by_domain"] == {"startup": "strong", "gtm": "partial"} and d["basis"] == "provisional"
    assert d["stale"] == ["gtm"] and "query" not in d and "secret" not in json.dumps(d)
    assert _usage_detail("coach_search", {"query": "q"}, sc, True)[1]["query"] == "q"


def test_g7_hits_per_search_default_to_ten_and_the_setting_is_clamped():
    from founder_coach import product
    from founder_coach import server as S
    name = product.env_name("TOP_K")
    old = os.environ.get(name)
    try:
        for value, want in ((None, 10), ("8", 8), ("30", 15), ("0", 1), ("ten", 10)):
            os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)
            assert S._default_top_k() == want, (value, want)
    finally:
        os.environ.pop(name, None) if old is None else os.environ.__setitem__(name, old)
    sc = {"hits": [{"item_id": "tkw:A:01"}], "coverage": "strong"}
    assert S._usage_detail("coach_search", {"query": "q"}, sc, False)[1]["top_k"] == S.DEFAULT_TOP_K
    assert S._usage_detail("coach_search", {"query": "q", "top_k": 3}, sc, False)[1]["top_k"] == 3


def test_g8_searches_group_into_asks_and_rounds_by_session_and_time():
    home = _home()
    d = home / "founder" / "projects" / "p"
    clock = Clock("2026-10-14T09:00:00+00:00")
    st = FounderStore(d, clock=clock)
    try:
        def log(run, secs, cov="partial"):
            clock.advance(seconds=secs)
            st.log_usage("coach_search", "ok", 10, run=run, version="0.1.7",
                         detail={"hits": 5, "coverage": cov, "routing": "none"})
        log("r1", 0)
        log("r1", 1)            # same round
        log("r1", 20)           # round 2
        log("r1", 400)          # a new ask
        log("r2", 1)            # another session: a new ask
    finally:
        st.close()
    (p,) = G.load(home, days=None, now=NOW)
    asks = G.asks_of([r for r in p.rows if r["tool"] == "coach_search"])
    assert [len(a) for a in asks] == [2, 1, 1] and [sum(len(x) for x in a) for a in asks] == [3, 1, 1]
    s = G.summarize(p)
    assert s["asks"] == {"n": 3, "searches_avg": 1.7, "searches_max": 3, "rounds_avg": 1.3, "rounds_max": 2, "at_guard": 0}
    assert s["explicit_domains"] == {"n": 0, "of": 5}


def test_g9_a_search_that_named_no_domain_is_attributed_to_its_hits_domain_from_the_installed_pack():
    import sqlite3
    home = _home()
    pack = home / "packdir"
    pack.mkdir()
    c = sqlite3.connect(pack / "knowledge.sqlite")
    c.execute("CREATE TABLE items (item_id TEXT, domains TEXT)")
    c.executemany("INSERT INTO items VALUES (?, ?)", [("a", '["startup"]'), ("b", '["finance"]'), ("c", '["finance","startup"]'),
                                                      ("d", '["finance"]')])
    c.commit()
    c.close()
    (home / "installed").mkdir()
    (home / "installed" / "founder-coach.json").write_text(json.dumps({"product_id": "founder-coach", "pack": "founder",
                                                                       "pack_path": str(pack)}))
    _project(home, "founder", "p", "P", [
        {"mode": "semantic", "hits": 5, "coverage": "partial", "top_similarity": 0.7, "items": ["a", "b", "d"], "routing": "none"},
        {"mode": "semantic", "hits": 5, "coverage": "strong", "top_similarity": 0.77, "items": ["a", "c"], "routing": "none"},
        _search("strong", 0.78, ["gtm"], by={"gtm": "strong"}),
    ])
    rep = G.report(G.load(home, days=None, now=NOW), home, None)
    (s,) = rep["projects"]
    assert s["domains"]["finance"] == {"strong": 0, "partial": 1, "none": 0}          # 2 of 3 hits are finance
    assert s["domains"]["startup"] == {"strong": 1, "partial": 0, "none": 0}          # a: startup, c: finance+startup -> startup
    assert s["domains"]["finance"]["strong"] == 0 and s["domains"]["gtm"]["strong"] == 1
    assert s["domains_from_hits"] == 2
    assert "attributed by their hits' Domain" in G.render(rep, home)
    # without a registered pack the same searches simply have no Domain
    (home / "installed" / "founder-coach.json").unlink()
    (s2,) = G.report(G.load(home, days=None, now=NOW), home, None)["projects"]
    assert s2["domains_from_hits"] == 0 and "finance" not in s2["domains"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_g")]
    for f in fns:
        f()
        print("ok", f.__name__)
