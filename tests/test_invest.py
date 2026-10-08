"""The investor Pack (M6g): broker exports read without guessing, and the arithmetic of a review exact to the cent
(docs/m6-plan.md 4.4, I1-I5; R9, R10). Hand-computed fixtures; no network, no prices, no models."""
import datetime as dt
import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("YTBRAIN_DOTENV", "0")

from founder_coach import invest as I  # noqa: E402

TODAY = dt.date(2026, 10, 6)

FIDELITY = '''Account Number,Account Name,Symbol,Description,Quantity,Last Price,Last Price Change,Current Value,Today's Gain/Loss Dollar,Today's Gain/Loss Percent,Total Gain/Loss Dollar,Total Gain/Loss Percent,Percent Of Account,Cost Basis Total,Average Cost Basis,Type
Z12345678,Individual,SPAXX**,HELD IN MONEY MARKET,,,,$5000.00,,,,,5.00%,,,Cash
Z12345678,Individual,VTI,VANGUARD INDEX FDS TOTAL STK MKT ETF,200,$300.00,+$1.00,"$60,000.00",+$200.00,+0.33%,"+$10,000.00",+20.00%,60.00%,"$50,000.00",$250.00,Cash
Z12345678,Individual,VXUS,VANGUARD STAR FDS TOTAL INTL STK MKT ETF,320,$62.50,+$0.10,"$20,000.00",+$32.00,+0.16%,"+$2,000.00",+11.11%,20.00%,"$18,000.00",$56.25,Cash
Z12345678,Individual,NVDA,NVIDIA CORP,100,$150.00,-$2.00,"$15,000.00",-$200.00,-1.32%,"+$9,000.00",+150.00%,15.00%,"$6,000.00",$60.00,Margin
Z12345678,Individual,Pending Activity,,,,,$12.00,,,,,,,,

"The data and information in this spreadsheet is provided to you solely for your use and is not for distribution."
"Date downloaded Oct-05-2026 3:46 p.m ET"
'''

SCHWAB = '''"Positions for account Roth IRA ...890 as of 09:21 PM ET, 10/05/2026","","","","","","","",""
"Symbol","Description","Qty (Quantity)","Price","Price Chng % (Price Change %)","Price Chng $ (Price Change $)","Mkt Val (Market Value)","Day Chng % (Day Change %)","Security Type"
"SCHB","SCHWAB US BROAD MARKET ETF","1,000","$25.00","0.5%","$0.12","$25,000.00","0.5%","ETFs & Closed End Funds"
"SCHZ","SCHWAB US AGGREGATE BOND ETF","500","$23.00","0%","$0.00","$11,500.00","0%","Fixed Income"
"Cash & Cash Investments","--","--","--","--","--","$1,234.56","--","Cash and Money Market"
"Account Total","--","--","--","--","--","$37,734.56","--","--"
'''

VANGUARD = '''Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,
12345678,VANGUARD TOTAL BOND MARKET ETF,BND,400,75.00,30000.00,
12345678,VANGUARD FEDERAL MONEY MARKET FUND,VMFXX,2500.5,1.00,2500.50,
12345678,VANGUARD TOTAL STOCK MARKET ETF,VTI,33.33,300.03,10000.00,

Account Number,Trade Date,Settlement Date,Transaction Type,Transaction Description,Investment Name,Symbol,Shares,Share Price,Principal Amount,
12345678,10/01/2026,10/02/2026,Buy,Buy,VANGUARD TOTAL BOND MARKET ETF,BND,10,75.00,-750.00,
'''


def _file(text: str, name: str = "positions.csv") -> Path:
    d = Path(tempfile.mkdtemp())
    (d / name).write_text(text, encoding="utf-8")
    return d / name


def _snap_dicts(snaps, imported="2026-10-06T00:00:00Z"):
    rows, pos = [], {}
    for i, s in enumerate(snaps):
        sid = f"s{i}-{s.account}"
        rows.append({"id": sid, "account": s.account, "as_of": s.as_of, "imported_at": imported,
                     "total_cents": s.total_cents})
        pos[sid] = [{"symbol": p.symbol, "value_cents": p.value_cents, "csv_class": p.csv_class} for p in s.positions]
    return rows, pos


def test_money_is_exact_and_a_split_always_adds_up_to_the_cent():
    assert I.to_cents("$1,234.56") == 123456 and I.to_cents("(12.00)") == -1200 and I.to_cents("--") is None
    assert I.to_cents(1234.555) == 123456 and I.to_cents("+$0.10") == 10 and I.money(-123456) == "-$1,234.56"
    parts = I.split_cents(10000, {"us_equity": Decimal("33.33"), "intl_equity": Decimal("33.33"),
                                  "bonds": Decimal("33.34")})
    assert sum(parts.values()) == 10000 and parts == {"us_equity": 3333, "intl_equity": 3333, "bonds": 3334}
    odd = I.split_cents(1, {"us_equity": Decimal(50), "bonds": Decimal(50)})
    assert sum(odd.values()) == 1, "one cent goes somewhere, never lost"


def test_i2_a_sum_is_split_by_the_persons_targets_and_never_names_a_security():
    out = I.split("$50,000", {"us_equity": 60, "intl_equity": 25, "bonds": 15})
    assert [(p["asset_class"], p["amount"]) for p in out["parts"]] == \
        [("us_equity", "$30,000.00"), ("intl_equity", "$12,500.00"), ("bonds", "$7,500.00")]
    assert sum(p["amount_cents"] for p in out["parts"]) == 5_000_000
    text = json.dumps(out)
    assert not any(t in text for t in ("VTI", "BND", "VXUS", "ETF")), "classes only, no security (I2)"
    thirds = I.split("1000.00", {"us_equity": "33.33", "intl_equity": "33.33", "bonds": "33.34"})
    assert sum(p["amount_cents"] for p in thirds["parts"]) == 100_000
    for bad, why in (({"us_equity": 60, "bonds": 30}, "add up to 100"), ({"crypto": 100}, "unknown asset class"),
                     ({"us_equity": 120, "bonds": -20}, "between 0 and 100"), ({}, "must be an object")):
        try:
            I.split(1000, bad)
            raise AssertionError(bad)
        except ValueError as e:
            assert why in str(e), (why, str(e))
    try:
        I.split("-5", {"bonds": 100})
        raise AssertionError("negative")
    except ValueError as e:
        assert "positive" in str(e)


def test_each_brokers_positions_export_is_read_per_account_with_its_date():
    f = I.read_positions(_file(FIDELITY))
    assert len(f) == 1 and f[0].account == "Individual" and f[0].as_of == "2026-10-05" and f[0].broker == "fidelity"
    assert [(p.symbol, p.value_cents, p.csv_class) for p in f[0].positions] == [
        ("SPAXX", 500000, "cash"), ("VTI", 6000000, None), ("VXUS", 2000000, None), ("NVDA", 1500000, None)], \
        "the core position is cash; Pending Activity and the disclaimer are skipped; 'Type' (Cash/Margin) is ignored"
    s = I.read_positions(_file(SCHWAB))
    assert s[0].account == "Roth IRA ...890" and s[0].as_of == "2026-10-05" and s[0].broker == "schwab"
    assert [(p.symbol, p.value_cents, p.csv_class) for p in s[0].positions] == [
        ("SCHB", 2500000, None), ("SCHZ", 1150000, "bonds"), ("CASH", 123456, "cash")], "Account Total skipped"
    v = I.read_positions(_file(VANGUARD), as_of="2026-10-04")
    assert v[0].broker == "vanguard" and v[0].account == "12345678" and v[0].total_cents == 4250050
    assert [p.symbol for p in v[0].positions] == ["BND", "VMFXX", "VTI"], "the activity section after the blank line"
    assert v[0].positions[1].csv_class == "cash", "a money market fund by its name"
    g = I.read_positions(_file("Ticker,Holding Value,Acct\nAAPL,1000,Brokerage\nMSFT,\"2,000.50\",Brokerage\n"),
                         as_of="2026-10-01", columns={"symbol": "Ticker", "value": "Holding Value", "account": "Acct"})
    assert g[0].broker == "generic" and g[0].total_cents == 300050
    two = I.parse_positions(FIDELITY.replace("Z12345678,Individual,NVDA", "Z999,Joint,NVDA"))
    assert sorted(x.account for x in two) == ["Individual", "Joint"], "one snapshot per account in the file"
    assert I.read_positions(_file(FIDELITY))[0].sha256 == I.read_positions(_file(FIDELITY))[0].sha256


def test_i3_a_file_it_cannot_read_is_refused_with_what_it_needs_never_guessed():
    cases = [
        (lambda: I.read_positions(_file("Date,Action,Symbol,Amount\n10/01/2026,Buy,VTI,-100\n"), as_of="2026-10-01",
                                  account="x"), "market value column"),
        (lambda: I.read_positions(_file(FIDELITY, "positions.xlsx")), "only a .csv"),
        (lambda: I.read_positions(_file(VANGUARD)), "pass as_of"),
        (lambda: I.read_positions(_file(VANGUARD), as_of="2027-01-01", today=TODAY), "in the future"),
        (lambda: I.read_positions(_file(VANGUARD), as_of="05/10/2026"), "a date like 2026-10-05"),
        (lambda: I.read_positions(_file("Symbol,Market Value\nVTI,100\n"), as_of="2026-10-01"), "pass account"),
        (lambda: I.read_positions(_file("Symbol,Market Value\n"), as_of="2026-10-01", account="a"), "no positions"),
        (lambda: I.read_positions(_file("A,B\n1,2\n"), as_of="2026-10-01", account="a",
                                  columns={"symbol": "Ticker", "value": "B"}), "no column 'Ticker'"),
        (lambda: I.read_positions("/no/such/file.csv"), "can't read"),
    ]
    for run, why in cases:
        try:
            run()
            raise AssertionError(f"accepted ({why})")
        except I.ImportProblem as e:
            assert why in str(e), (why, str(e))
    big = Path(tempfile.mkdtemp()) / "big.csv"
    big.write_bytes(b"x" * (I.MAX_CSV_BYTES + 1))
    try:
        I.read_positions(big)
        raise AssertionError("a huge file accepted")
    except I.ImportProblem as e:
        assert "limit 5 MB" in str(e)


IPS = {"targets": {"us_equity": 60, "intl_equity": 20, "bonds": 15, "cash": 5}, "rebalance_band": 5,
       "concentration_limit": 10, "concentration_exempt": ["VTI", "VXUS", "BND"], "review_days": 90}
LABELS = {"VTI": "us_equity", "VXUS": "intl_equity", "NVDA": "us_equity", "BND": "bonds"}


def _project():
    taxable = I.read_positions(_file(FIDELITY))
    ira = I.read_positions(_file(VANGUARD), as_of="2026-10-04")
    return _snap_dicts(taxable + ira)


def test_i1_i5_allocation_drift_and_concentration_across_every_account_to_the_cent():
    rows, pos = _project()
    r = I.review(rows, pos, LABELS, IPS, TODAY)
    assert r["total"] == "$142,500.50" and r["as_of"] == "2026-10-04 to 2026-10-05", "every review states its date"
    got = {a["asset_class"]: (a["value"], a["percent"], a["drift_points"], a["to_target"], a["outside_band"])
           for a in r["allocation"]}
    assert got == {"us_equity": ("$85,000.00", 59.65, -0.35, "$500.30", False),
                   "intl_equity": ("$20,000.00", 14.04, -5.96, "$8,500.10", True),
                   "bonds": ("$30,000.00", 21.05, 6.05, "-$8,624.92", True),
                   "cash": ("$7,500.50", 5.26, 0.26, "-$375.47", False)}, got
    assert r["outside_band"] == ["intl_equity", "bonds"]
    assert r["over_limit"] == [{"symbol": "NVDA", "value": "$15,000.00", "percent": 10.53, "limit_percent": 10.0,
                                "over_limit": "$749.95", "over_limit_cents": 74995}], \
        "only the holding over the person's own limit; exempt funds and cash never are"
    assert r["unclassified"] == [] and not r["stale"] and r["days_old"] == 2 and r["missing_policy"] == []
    assert sum(a["to_target_cents"] for a in r["allocation"]) in (-1, 0, 1), "to-target amounts balance (rounding)"


def test_r10_a_project_uses_each_accounts_latest_snapshot_only():
    rows, pos = _project()
    old = I.read_positions(_file(VANGUARD.replace("30000.00", "99999.00")), as_of="2026-07-01")
    o_rows, o_pos = _snap_dicts(old, imported="2026-10-06T01:00:00Z")   # imported later, but older as_of
    r = I.review(rows + o_rows, {**pos, **o_pos}, LABELS, IPS, TODAY)
    assert r["total"] == "$142,500.50" and len(r["accounts"]) == 2, "the older IRA snapshot is history, not holdings"


def test_unlabelled_holdings_are_reported_never_guessed_and_a_missing_policy_is_named():
    rows, pos = _project()
    r = I.review(rows, pos, {"VTI": "us_equity"}, {"targets": IPS["targets"]}, TODAY)
    assert [u["symbol"] for u in r["unclassified"]] == ["BND", "VXUS", "NVDA"], r["unclassified"]
    assert r["unclassified_percent"] == 45.61
    assert r["missing_policy"] == ["rebalance_band", "concentration_limit", "review_days"]
    assert r["over_limit"] == [] and r["outside_band"] == [], "no limit or band set: nothing is judged against one"


def test_i4_holdings_older_than_the_review_interval_are_stale():
    rows, pos = _project()
    r = I.review(rows, pos, LABELS, {**IPS, "review_days": 30}, TODAY + dt.timedelta(days=40))
    assert r["stale"] and r["days_old"] == 42



# --- the store: Holdings and the Investment Policy Statement (the investor Project's profile) ----------------------
INVESTOR_FIELDS = {"goal": ("text", "what the money is for"), "targets": ("allocation", "target allocation"),
                   "rebalance_band": ("percent", "band"), "concentration_limit": ("percent", "limit"),
                   "concentration_exempt": ("symbols", "exempt"), "review_days": ("int", "days")}


class _Investor:
    """The repo's runtime is the founder Pack's: lend it the investor Pack's fields and modules for a test."""
    def __enter__(self):
        from founder_coach import domain as D, product
        self.D, self.P = D, product
        self.saved = (dict(D.PROFILE_FIELDS), list(product.PACK.get("modules") or []), D.REQUIRED)
        D.PROFILE_FIELDS.update(INVESTOR_FIELDS)
        product.PACK["modules"] = ["holdings", "decisions"]
        D.REQUIRED = ("goal", "targets")
        return self

    def __exit__(self, *exc):
        fields, modules, req = self.saved
        self.D.PROFILE_FIELDS.clear()
        self.D.PROFILE_FIELDS.update(fields)
        self.P.PACK["modules"] = modules
        self.D.REQUIRED = req


def _istore(home=None, when="2026-10-06T09:00:00+00:00"):
    from founder_coach.store import FounderStore
    return FounderStore(home or tempfile.mkdtemp(), clock=lambda: dt.datetime.fromisoformat(when))


def test_the_policy_is_validated_like_any_profile_fact_and_kept_in_history():
    from founder_coach.store import StoreError
    with _Investor():
        s = _istore()
        s.update_profile({"goal": "retirement in 2055", "targets": {"us_equity": 60, "intl_equity": "20%",
                                                                    "bonds": 20}, "rebalance_band": "5%",
                          "concentration_limit": 10, "concentration_exempt": "vti, VXUS bnd", "review_days": 90})
        prof = {f: v["value"] for f, v in s.profile().items()}
        assert prof["targets"] == {"us_equity": 60.0, "intl_equity": 20.0, "bonds": 20.0}
        assert prof["rebalance_band"] == 5.0 and prof["concentration_exempt"] == ["VTI", "VXUS", "BND"]
        for bad, why in (({"targets": {"us_equity": 70, "bonds": 20}}, "add up to 100"),
                         ({"targets": {"gold": 100}}, "unknown asset class"),
                         ({"concentration_limit": 0}, "more than 0"), ({"rebalance_band": "lots"}, "a percent"),
                         ({"concentration_exempt": ["V T I!"]}, "isn't a ticker")):
            try:
                s.update_profile(bad)
                raise AssertionError(bad)
            except StoreError as e:
                assert why in str(e), (why, str(e))
        s.update_profile({"targets": {"us_equity": 50, "intl_equity": 20, "bonds": 30}})
        assert [h["value"]["bonds"] for h in s.profile_history("targets")] == [20.0, 30.0], "a change keeps history"
        s.close()


def test_holdings_import_once_labels_win_and_the_review_reads_the_projects_policy():
    with _Investor():
        s = _istore()
        s.update_profile({"goal": "retirement", "targets": {"us_equity": 60, "intl_equity": 20, "bonds": 15,
                                                            "cash": 5},
                          "rebalance_band": 5, "concentration_limit": 10,
                          "concentration_exempt": ["VTI", "VXUS", "BND"], "review_days": 90})
        snaps = I.read_positions(_file(FIDELITY)) + I.read_positions(_file(VANGUARD), as_of="2026-10-04")
        r = s.import_holdings(snaps, "positions.csv", request_id="imp-1")
        assert [x["account"] for x in r["imported"]] == ["Individual", "12345678"] and not r["skipped"]
        assert r["unlabelled"] == ["BND", "NVDA", "VTI", "VXUS"], "money market and core cash need no label"
        again = s.import_holdings(snaps, "positions.csv", request_id="imp-2")
        assert again["imported"] == [] and len(again["skipped"]) == 2, "the same files twice never double the money"
        assert s.import_holdings(snaps, "positions.csv", request_id="imp-1")["replayed"]
        s.label_assets(LABELS)
        rev = s.holdings_review()
        assert rev["total"] == "$142,500.50" and [o["symbol"] for o in rev["over_limit"]] == ["NVDA"]
        assert rev["outside_band"] == ["intl_equity", "bonds"]
        s.label_assets({"VMFXX": "bonds"})                       # the person's word wins over the file's
        assert {a["asset_class"]: a["value"] for a in s.holdings_review()["allocation"]}["bonds"] == "$32,500.50"
        ops = [c["op"] for c in s.recent_changes(20) if c["entity"] in ("holdings", "asset_class")]
        assert ops.count("import") == 2 and "create" in ops, "every import and label is in the change log"
        from founder_coach.store import StoreError
        try:
            s.label_assets({"VTI": "stocks"})
            raise AssertionError("unknown class")
        except StoreError as e:
            assert "us_equity" in str(e)
        out = json.loads(s.export(Path(tempfile.mkdtemp()))["json"].read_text())
        assert len(out["holdings"]) == 2 and len(out["positions"]) == 7 and out["asset_classes"]["NVDA"] == "us_equity"
        assert "## Holdings" in s.markdown() and "$100,000.00 as of 2026-10-05" in s.markdown()
        s.forget()
        assert s.holdings() == ([], {}) and s.asset_labels() == {}, "forget deletes Holdings too"
        s.close()


def test_i4_the_coach_nudges_for_a_fresh_import_and_never_reviews_silently_on_old_holdings():
    from founder_coach import nudges as N
    with _Investor():
        home = Path(tempfile.mkdtemp())
        s = _istore(home)
        s.update_profile({"goal": "retirement", "targets": {"us_equity": 100}, "review_days": 30})
        assert [n["kind"] for n in N.nudges(s)] == ["holdings_missing"]
        s.import_holdings(I.read_positions(_file(VANGUARD), as_of="2026-10-01"), "v.csv")
        assert N.nudges(s) == []
        s.close()
        later = _istore(home, when="2026-11-15T09:00:00+00:00")
        kinds = {n["kind"]: n["message"] for n in N.nudges(later)}
        assert "holdings_stale" in kinds and "12345678 as of 2026-10-01" in kinds["holdings_stale"]
        assert later.holdings_review()["stale"]
        later.close()


def test_a_founder_store_upgrades_to_v5_and_a_founder_coach_never_shows_holdings():
    from founder_coach import nudges as N
    from founder_coach.store import SCHEMA_VERSION
    s = _istore()
    s.update_profile({"company": "Acme", "stage": "mvp"})
    assert SCHEMA_VERSION >= 5 and s.holdings() == ([], {})
    assert all(n["kind"] not in ("holdings_missing", "holdings_stale") for n in N.nudges(s))
    assert "## Holdings" not in s.markdown()
    s.close()



# --- the tools: import, label, review, split through the MCP server ------------------------------------------------
def test_the_holdings_tools_import_label_review_and_split_exactly_and_refuse_what_they_cannot_read():
    try:
        import anyio
        from mcp import Client
        from test_coach import _Models, _pack
    except ImportError:
        from test_coach import _skipped
        return _skipped("optional dependency not installed")
    from founder_coach.server import create_server
    with _Investor():
        tmp = Path(tempfile.mkdtemp())
        fid, van = _file(FIDELITY), _file(VANGUARD)
        srv = create_server(pack=_pack(tmp), home=tmp / "home", models=_Models(), start_models=False,
                            clock=lambda: dt.datetime(2026, 10, 6, 9, tzinfo=dt.timezone.utc))

        async def main():
            async with Client(srv) as c:
                async def call(tool, args):
                    r = await c.call_tool(tool, args)
                    text = " ".join(getattr(x, "text", "") for x in r.content)
                    return (None if r.is_error else r.structured_content), text, r.is_error
                names = [t.name for t in (await c.list_tools()).tools]
                assert {"coach_holdings", "coach_review", "coach_split"} <= set(names)
                _, text, err = await call("coach_split", {"amount": "$50,000"})
                assert err and "no target allocation yet" in text
                await call("coach_update_profile", {"changes": {"goal": "retirement", "targets": IPS["targets"],
                                                                "rebalance_band": 5, "concentration_limit": 10,
                                                                "concentration_exempt": ["VTI", "VXUS", "BND"],
                                                                "review_days": 90}, "request_id": "p1"})
                out, _, err = await call("coach_holdings", {"action": "import", "path": str(fid), "request_id": "i1"})
                assert not err and out["imported"][0]["total"] == "$100,000.00"
                _, text, err = await call("coach_holdings", {"action": "import", "path": str(van), "request_id": "i2"})
                assert err and "Nothing was imported" in text and "pass as_of" in text, "refused with what it needs"
                out, _, _ = await call("coach_holdings", {"action": "import", "path": str(van), "as_of": "2026-10-04",
                                                          "request_id": "i3"})
                assert out["unlabelled"] == ["BND", "VTI"], out
                _, text, err = await call("coach_holdings", {"action": "label", "labels": {"VTI": "stocks"},
                                                             "request_id": "l0"})
                assert err and "us_equity" in text, "a class that isn't one of the six is refused"
                await call("coach_holdings", {"action": "label", "labels": LABELS, "request_id": "l1"})
                rev, _, _ = await call("coach_review", {})
                assert rev["total"] == "$142,500.50" and rev["over_limit"][0]["over_limit"] == "$749.95"
                assert rev["as_of"] == "2026-10-04 to 2026-10-05" and "not advice on any security" in rev["note"]
                lst, _, _ = await call("coach_holdings", {"action": "list", "request_id": "x"})
                assert [a["account"] for a in lst["accounts"]] == ["12345678", "Individual"]
                sp, _, _ = await call("coach_split", {"amount": "$50,000"})
                assert [p["amount"] for p in sp["parts"]] == ["$30,000.00", "$10,000.00", "$7,500.00", "$2,500.00"]
                _, text, err = await call("coach_holdings", {"action": "import", "path": str(tmp / "x.xlsx"),
                                                             "request_id": "i4"})
                assert err and "only a .csv" in text
        anyio.run(main)



def test_c3_an_investor_goal_promotes_nothing_to_the_shared_profile_or_another_coach():
    """C3: the investor Pack lists no common_fields, so its policy, holdings and amounts never reach you.db; the
    founder coach on the same machine sees none of it."""
    from test_projects import _machine, _serve
    from founder_coach import product
    saved = product.PACK.get("common_fields")
    with _Investor(), _machine() as user:
        product.PACK["common_fields"] = []
        csv_file = _file(VANGUARD)
        try:
            async def go(call):
                await call("coach_project", {"action": "create", "name": "Retirement 2055"})
                await call("coach_update_profile", {"changes": {"goal": "retirement", "targets": {"bonds": 100},
                                                                "name": "Kay"}, "request_id": "p"})
                _, text, err = await call("coach_update_profile", {"changes": {"name": "Kay"}, "scope": "common",
                                                                   "request_id": "c"})
                assert err and "shares no profile fields" in text
                out, text, err = await call("coach_holdings", {"action": "import", "path": str(csv_file),
                                                            "as_of": "2026-09-01", "request_id": "i"})
                assert not err and out["imported"], text
            assert _serve(user, go) is not False
        finally:
            product.PACK["common_fields"] = saved
        eng = user / ".ytbrain"
        assert not (eng / "you.db").exists(), "nothing shared"
        others = [p for p in (eng).glob("*/projects/*/founder.db") if "founder/" in str(p)]
        assert all("42,500" not in p.read_bytes().decode("latin-1") for p in others)


if __name__ == "__main__":
    import inspect
    import logging
    import traceback
    logging.disable(logging.WARNING)
    fns = [f for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        try:
            f()
            print(f"  PASS  {f.__name__}")
        except Exception as e:                   # noqa: BLE001 -- report every test, not just the first
            failed += 1
            print(f"  FAIL  {f.__name__}: {type(e).__name__}: {str(e)[:300]}")
            if os.environ.get("VERBOSE"):
                traceback.print_exc()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
