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
    assert tables == {"usage"}, f"gaps.py must read only the usage table, not {tables - {'usage'}}"


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_g")]
    for f in fns:
        f()
        print("ok", f.__name__)
