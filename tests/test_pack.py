"""Offline tests for the Knowledge pack (ADR-0009) and the shared search. No network,
no real models: a hashing embedder stands in for ONNX models."""
import contextlib
import io
import json
import os
os.environ["YTBRAIN_DOTENV"] = "0"          # hermetic: never read the developer's .env (keys, backend)
os.environ.setdefault("YTBRAIN_SOURCES_FILE", os.path.join(__import__("tempfile").mkdtemp(prefix="ytbrain-nosources-"), "sources.yaml"))   # hermetic: never read your sources.yaml (the file does not exist)
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from founder_coach import product  # noqa: E402
os.environ.setdefault("YTBRAIN_ROOT", tempfile.mkdtemp(prefix="ytbrain-packtest-"))
os.environ.setdefault(product.env_name("HOME"), tempfile.mkdtemp(prefix="coach-home-"))

from founder_coach import pack as P                                # noqa: E402
from founder_coach.search import Filter, search                    # noqa: E402
from ytbrain.pack import build_pack                                 # noqa: E402

_QUIET = contextlib.redirect_stdout(io.StringIO())


class HashEmbed:
    """Bag-of-words hashing vectors: similar wording -> similar vectors."""
    name, dim, query_prefix, doc_prefix = "hash-64", 64, "", ""

    def __init__(self, fail_after: int | None = None):
        self.embedded: list[str] = []
        self.fail_after = fail_after

    def _vec(self, t):
        import hashlib
        import math
        v = [0.0] * self.dim
        for w in t.lower().split():
            v[int(hashlib.md5(w.strip(".,?!'\"").encode()).hexdigest(), 16) % self.dim] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def __call__(self, texts):
        return [self._vec(t) for t in texts]

    def documents(self, texts):
        import numpy as np
        if self.fail_after is not None and len(self.embedded) >= self.fail_after:
            raise KeyboardInterrupt
        self.embedded += list(texts)
        return np.asarray([self._vec(t) for t in texts], dtype=np.float32)


def _row(iid, kind, doc, text, stages=("mvp",), topics=("sales",), year=2024, ms=130_000):
    return {"item_id": iid, "kind": kind, "doc_id": doc, "text": text, "evidence": f"quote {text}",
            "deep_link": f"https://www.youtube.com/watch?v={doc}&t={ms // 1000}s", "title": f"Talk {doc}",
            "speaker": f"Speaker {doc}", "series": "Startup School", "provenance": "playlist",
            "published_at": f"{year}-01-01", "stage_origin": "document", "source_kind": "talk",
            "context_header": f"From Talk {doc}", "indexable": f"From Talk {doc}\n{text}",
            "start_ms": ms, "end_ms": -1, "year": year, "stages": list(stages), "topics": list(topics)}


ROWS = [
    _row("adv:AAAAAAAAAA1:a01", "advice", "AAAAAAAAAA1", "set your price high and raise pricing every quarter"),
    _row("adv:BBBBBBBBBB2:a01", "advice", "BBBBBBBBBB2", "hire slowly and only after product market fit",
         stages=("pmf",), topics=("hiring",), year=2016),
    _row("tkw:CCCCCCCCCC3:01", "takeaway", "CCCCCCCCCC3", "pricing should follow value not your costs"),
    _row("sum:CCCCCCCCCC3", "summary", "CCCCCCCCCC3", "a talk about value based pricing", ms=0),
    _row("psg:CCCCCCCCCC3:0", "passage", "CCCCCCCCCC3", "welcome everyone pricing pricing pricing", ms=0),
]


class Source:
    def __init__(self, rows):
        self._rows = rows

    def rows(self, kinds=None, columns=None):
        return [dict(r) for r in self._rows if not kinds or r["kind"] in kinds]


def _build(out, rows=ROWS, emb=None, batch=64, route=None, calibration=None):
    emb = emb or HashEmbed()
    with contextlib.redirect_stdout(io.StringIO()):
        return build_pack(Source(rows), emb, out, rerank_model="none", batch=batch,
                          say=lambda m: None, route=route, calibration=calibration), emb


def test_the_pack_content_hash_ignores_when_it_was_built_but_not_what_it_holds():
    import numpy as np
    from ytbrain import pack as B
    from founder_coach import pack as P
    rows = [{c: f"{c}-{i}" for c in P.COLUMNS} for i in range(3)]
    m = np.eye(3, 4, dtype=np.float32)
    meta = {"embed_model": "e", "built_at": "2026-10-01T00:00:00", "built_by": "ytbrain 0.1.0", "router": {"enabled": False}}
    h = B.content_hash(rows, m, meta)
    assert h == B.content_hash(rows, m, {**meta, "built_at": "2026-10-04T09:00:00", "built_by": "ytbrain 0.2.0"})
    assert h != B.content_hash([{**rows[0], P.COLUMNS[0]: "changed"}] + rows[1:], m, meta), "an item changed"
    assert h != B.content_hash(rows, m * 0.5, meta), "a vector changed"
    assert h != B.content_hash(rows, m, {**meta, "router": {"enabled": True}}), "a setting changed"
    with tempfile.TemporaryDirectory() as t:
        a, _ = _build(Path(t) / "a")
        b, _ = _build(Path(t) / "b")
        assert a["content_sha256"] and a["content_sha256"] == b["content_sha256"], "two builds of the same items"


def test_pack_builds_verifies_and_searches_like_the_index():
    out = Path(tempfile.mkdtemp())
    manifest, emb = _build(out)
    store = P.PackStore(out, verify=True)
    assert store.count() == 4 and manifest["items"] == 4                      # no Passages
    assert manifest["by_kind"] == {"advice": 2, "summary": 1, "takeaway": 1}
    assert manifest["talks"] == 3 and manifest["years"] == [2016, 2024]
    assert manifest["sha256"] == P.sha256_file(out / P.PACK_FILE)
    res = search(store, emb, "how should I set pricing for my product?", top_k=3)
    assert res and "pric" in res[0]["text"] and res[0]["deep_link"].startswith("https://")
    assert all(r["kind"] != "passage" for r in res)
    only = search(store, emb, "pricing", kinds=["takeaway"])
    assert [r["kind"] for r in only] == ["takeaway"]
    staged = search(store, emb, "hire", stage="pmf", require_stage=True)
    assert [r["item_id"] for r in staged] == ["adv:BBBBBBBBBB2:a01"]
    assert not search(store, emb, "pricing", topics=["legal"])
    assert store.get("sum:CCCCCCCCCC3")["kind"] == "summary"
    assert {r["item_id"] for r in store.document_items("CCCCCCCCCC3")} == {"tkw:CCCCCCCCCC3:01", "sum:CCCCCCCCCC3"}


def test_private_items_stay_out_of_the_pack_unless_asked_and_release_refuses_them():
    book = {**_row("adv:9780000000002__go:a01", "advice", "9780000000002__go", "charge your first customers early"),
            "source_kind": "chapter", "visibility": "private", "deep_link": ""}
    rows = ROWS + [book]
    out = Path(tempfile.mkdtemp())
    manifest, _ = _build(out, rows=rows)
    store = P.PackStore(out, verify=True)
    assert store.get(book["item_id"]) is None and manifest["private_items"] == 0 and manifest["items"] == 4
    store.close()
    mine = Path(tempfile.mkdtemp())
    with contextlib.redirect_stdout(io.StringIO()):
        manifest = build_pack(Source(rows), HashEmbed(), mine, rerank_model="none", say=lambda m: None,
                              include_private=True)
    assert manifest["private_items"] == 1 and P.PackStore(mine).get(book["item_id"]) is not None
    import subprocess
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import release as R
    with tempfile.TemporaryDirectory() as t:
        root, mk = Path(t) / "repo", Path(t) / "marketplace"
        root.mkdir()
        (root / "product.toml").write_text((Path(__file__).resolve().parents[1] / "product.toml").read_text())
        subprocess.run(["git", "init", "-q", str(mk)], check=True)
        try:
            R.release(root, mine, mk, validate=False, say=lambda m: None)
        except R.ReleaseError as e:
            assert "private items" in str(e)
        else:
            raise AssertionError("a pack holding your Books must never be released")


def test_full_text_queries_are_literal_and_never_raise():
    out = Path(tempfile.mkdtemp())
    _build(out)
    store = P.PackStore(out)
    for q in ['pricing" OR 1=1 --', "NEAR(pricing hire)", "pric*", "(((", "how do I", "",
              "prïcing — über", "value AND NOT costs", "a"]:
        assert isinstance(store.text_search(q, None, 10), list), q
    assert store.text_search("how do I", None, 10) == []                   # only stopwords
    assert store.text_search("pricing", None, 10)[0]["kind"] in ("advice", "takeaway", "summary")
    assert store.text_search("pricing", Filter(kinds=("summary",)), 10)[0]["item_id"] == "sum:CCCCCCCCCC3"
    assert P.fts_query('say "hi"') == '"say" OR "hi"'


def test_rebuilds_embed_only_new_or_changed_items():
    out = Path(tempfile.mkdtemp())
    _, first = _build(out)
    assert len(first.embedded) == 4
    _, again = _build(out)
    assert again.embedded == []                                            # all cached
    changed = [dict(r) for r in ROWS]
    changed[0]["indexable"] += " and tell customers early"
    _, third = _build(out, rows=changed)
    assert len(third.embedded) == 1


def test_an_interrupted_build_keeps_the_old_pack_and_its_finished_batches():
    out = Path(tempfile.mkdtemp())
    _build(out)
    old_sha = json.loads((out / P.MANIFEST_FILE).read_text())["sha256"]
    new = [dict(r, indexable=r["indexable"] + " v2") for r in ROWS]
    try:
        _build(out, rows=new, emb=HashEmbed(fail_after=2), batch=2)       # dies after one batch
        raise AssertionError("expected an interrupt")
    except KeyboardInterrupt:
        pass
    P.PackStore(out, verify=True)                                          # old pack intact
    assert json.loads((out / P.MANIFEST_FILE).read_text())["sha256"] == old_sha
    assert not list(out.glob(".*.tmp-*"))
    _, resumed = _build(out, rows=new, batch=2)
    assert len(resumed.embedded) == 2                                      # 4 new texts, 2 cached
    assert P.PackStore(out, verify=True).get("adv:AAAAAAAAAA1:a01")["indexable"].endswith(" v2")


def test_corrupt_foreign_or_newer_packs_are_refused_with_a_reason():
    out = Path(tempfile.mkdtemp())
    _build(out)
    f = out / P.PACK_FILE
    try:
        P.PackStore(out / "missing")
        raise AssertionError("missing pack accepted")
    except RuntimeError as e:
        assert "no Knowledge pack" in str(e)
    newer = Path(tempfile.mkdtemp()) / P.PACK_FILE
    newer.write_bytes(f.read_bytes())
    db = sqlite3.connect(newer)
    db.execute("UPDATE meta SET value = '99' WHERE key = 'format_version'")
    db.commit()
    db.close()
    try:
        P.PackStore(newer)
        raise AssertionError("newer format accepted")
    except RuntimeError as e:
        assert "newer" in str(e)
    data = bytearray(f.read_bytes())
    data[-100] ^= 0xFF
    f.write_bytes(bytes(data))
    try:
        P.PackStore(out, verify=True)
        raise AssertionError("corrupt pack accepted")
    except RuntimeError as e:
        assert "corrupt" in str(e)
    junk = Path(tempfile.mkdtemp()) / "junk.sqlite"
    junk.write_bytes(b"not a database at all" * 100)
    try:
        P.PackStore(junk)
        raise AssertionError("junk accepted")
    except RuntimeError as e:
        assert "not a Knowledge pack" in str(e)


def test_pack_dense_ranking_matches_the_lance_index():
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return
    from ytbrain.knowledge.store import KnowledgeStore
    tmp = Path(tempfile.mkdtemp())
    emb = HashEmbed()
    many = [_row(f"adv:D{i:010d}:a01", "advice", f"D{i:010d}", f"topic {i % 7} note {i} about {w}")
            for i, w in enumerate(["pricing", "hiring", "fundraising", "sales", "churn", "users"] * 6)]
    lance = KnowledgeStore(path=tmp / "lance")
    for r in many:
        lance.replace_document(r["doc_id"], [{**r, "vector": emb([r["indexable"]])[0],
                                              "embed_model": emb.name}], emb.dim)
    _build(tmp / "pack", rows=many, emb=emb)
    pack = P.PackStore(tmp / "pack")
    for q in ("pricing note", "topic 3 hiring", "users churn"):
        v = emb([q])[0]
        # hashing vectors tie a lot, so compare the top similarities, not ids. LanceDB
        # reports squared L2, which is 2 - 2 cos on unit vectors; the pack reports 1 - cos.
        a = [1 - r["_distance"] / 2 for r in lance.vector_search(v, None, 8)]
        b = [1 - r["_distance"] for r in pack.vector_search(v, None, 8)]
        assert all(abs(x - y) < 2e-3 for x, y in zip(a, b)) and len(a) == len(b) == 8, (q, a, b)
        assert b == sorted(b, reverse=True)
    f = Filter(kinds=("advice",), topics=("sales",))
    assert [r["item_id"] for r in lance.vector_search(emb(["pricing"])[0], f, 3)]   # Filter works on Lance too


def test_onnx_wrappers_use_the_models_prefixes_and_a_sigmoid():
    import numpy as np
    seen = {}

    class TextEmbedding:
        def __init__(self, model, cache_dir=None, threads=None):
            seen["cache"] = cache_dir

        def embed(self, texts, batch_size=32):
            seen.setdefault("texts", []).extend(texts)
            return [np.array([3.0, 4.0]) for _ in texts]

    class TextCrossEncoder:
        def __init__(self, model, cache_dir=None, threads=None):
            pass

        def rerank(self, query, docs, batch_size=16):
            return [0.0, 10.0, -10.0][:len(docs)]

    fe = types.ModuleType("fastembed")
    fe.TextEmbedding = TextEmbedding
    ce = types.ModuleType("fastembed.rerank.cross_encoder")
    ce.TextCrossEncoder = TextCrossEncoder
    saved = {k: sys.modules.get(k) for k in ("fastembed", "fastembed.rerank", "fastembed.rerank.cross_encoder")}
    sys.modules.update({"fastembed": fe, "fastembed.rerank": types.ModuleType("fastembed.rerank"),
                        "fastembed.rerank.cross_encoder": ce})
    try:
        from founder_coach import models
        e = models.load_embedder("BAAI/bge-base-en-v1.5")
        assert e.dim == 2 and seen["cache"].startswith(os.environ[product.env_name("HOME")])
        q = e(["pricing"])[0]
        assert abs(float(np.linalg.norm(q)) - 1) < 1e-6                      # normalised
        assert seen["texts"][-1].startswith("Represent this sentence")         # query prefix
        e.documents(["an item"])
        assert seen["texts"][-1] == "an item"                                  # no document prefix
        r = models.load_reranker("Xenova/ms-marco-MiniLM-L-6-v2")
        s = r("q", ["a", "b", "c"])
        assert abs(s[0] - 0.5) < 1e-9 and s[1] > 0.99 and s[2] < 0.01
        assert models.load_reranker("none") is None
        emb, rr = models.for_pack({"embed_model": "x/unknown", "query_prefix": "Q: ", "doc_prefix": "",
                                   "rerank_model": "none"})
        emb(["hi"])
        assert seen["texts"][-1] == "Q: hi" and rr is None                     # the pack's prefixes win
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_batches_group_similar_lengths_and_devices_are_checked():
    out = Path(tempfile.mkdtemp())
    texts = [("word " * (5 + 37 * (i % 3))) + f"item{i}" for i in range(9)]
    rows = [dict(ROWS[i % 4], item_id=f"adv:X{i:010d}:a01", text=t, indexable=t) for i, t in enumerate(texts)]
    calls = []

    class Recording(HashEmbed):
        def documents(self, texts):
            calls.append([len(t) for t in texts])
            return super().documents(texts)
    _build(out, rows=rows, emb=Recording(), batch=3)
    flat = [n for batch in calls for n in batch]
    assert flat == sorted(flat)                                           # short with short, long with long
    from founder_coach.models import onnx_providers
    assert onnx_providers("cpu") is None and onnx_providers(None) is None
    for bad in ("tpu",):
        try:
            onnx_providers(bad)
            raise AssertionError("unknown device accepted")
        except RuntimeError as e:
            assert "unknown ONNX device" in str(e)


def test_a_pack_built_before_a_column_existed_still_opens():
    """A pack from an older build (before `source_kind`) must open with the column's default,
    not fail as "damaged": the plugin and `ytbrain search --pack` may meet an older pack."""
    import sqlite3
    out = Path(tempfile.mkdtemp()) / "pack"
    _build(out)
    db = sqlite3.connect(out / P.PACK_FILE)
    db.execute("ALTER TABLE items DROP COLUMN source_kind")
    db.commit()
    db.close()
    pk = P.PackStore(out)                               # no verify: the checksum changed with the edit
    row = pk.get(ROWS[0]["item_id"])
    assert row is not None and row["source_kind"] == ""


def test_pack_configs_use_the_adr_0009_gate():
    from ytbrain.eval.run import GATE, PACK_GATE, compare, gate_for
    assert gate_for("pack") == PACK_GATE and gate_for("full") == GATE
    base = {"summary": {"ndcg@10": {"mean": 0.60}}, "per_query": {"ndcg@10": {str(i): 0.6 for i in range(30)}}}
    cur = {"summary": {"ndcg@10": {"mean": 0.56}}, "per_query": {"ndcg@10": {str(i): 0.56 for i in range(30)}}}
    assert compare(cur, base, PACK_GATE)["ndcg@10"]["fails_gate"]           # -0.04 > 0.03
    assert not compare(cur, base, GATE)["ndcg@10"]["fails_gate"]


def test_a_build_killed_between_the_two_swaps_still_verifies_and_bad_manifests_are_refused():
    import json as _j
    import tempfile as _t
    from founder_coach import pack as PK
    with _t.TemporaryDirectory() as d:
        f = Path(d) / PK.PACK_FILE
        f.write_bytes(b"new pack bytes")
        good = PK.sha256_file(f)
        (Path(d) / PK.MANIFEST_FILE).write_text(_j.dumps({"sha256": "0" * 64}))      # the old manifest
        s = PK.PackStore.__new__(PK.PackStore)
        s.path = f
        try:
            s.verify()
            raise AssertionError("a stale manifest must fail")
        except RuntimeError as e:
            assert "corrupt" in str(e)
        (Path(d) / (PK.MANIFEST_FILE + ".next")).write_text(_j.dumps({"sha256": good}))
        s.verify()                                                                    # accepted via .next
        (Path(d) / PK.MANIFEST_FILE).write_text("{not json")
        try:
            s.verify()
            raise AssertionError("an unreadable manifest must be refused, not crash")
        except RuntimeError as e:
            assert "unreadable" in str(e)


# --- Domains (M5a): items carry the Domains of their Document; search takes several -------------
def _dom_rows():
    return [
        {**_row("adv:SSSSSSSSSS1:a01", "advice", "SSSSSSSSSS1", "talk to users every week before you build"), "domains": ["startup"]},
        {**_row("adv:LLLLLLLLLL2:a01", "advice", "LLLLLLLLLL2", "give feedback early and in private to your team"),
         "domains": ["leadership"], "source_id": "books_x"},
        {**_row("adv:BOTHBOTHBO3:a01", "advice", "BOTHBOTHBO3", "hire slowly and give clear feedback to new hires"),
         "domains": ["startup", "leadership"]},
        {**_row("adv:NODOMAINNN4:a01", "advice", "NODOMAINNN4", "charge money for the product on day one")},   # an index from before Domains
    ]


def test_domains_travel_into_the_pack_and_a_filter_matches_any_of_them():
    out = Path(tempfile.mkdtemp())
    manifest, emb = _build(out, rows=_dom_rows())
    assert manifest["domains"] == {"leadership": 2, "startup": 3}, manifest["domains"]    # the untagged row is startup
    store = P.PackStore(out, verify=True)
    ids = lambda flt: sorted(r["item_id"] for r in store.vector_search(emb(["feedback hire users"])[0], flt, 10))
    assert len(ids(None)) == 4
    assert ids(Filter(domains=("leadership",))) == ["adv:BOTHBOTHBO3:a01", "adv:LLLLLLLLLL2:a01"]
    assert ids(Filter(domains=("startup",))) == ["adv:BOTHBOTHBO3:a01", "adv:NODOMAINNN4:a01", "adv:SSSSSSSSSS1:a01"]
    assert len(ids(Filter(domains=("startup", "leadership")))) == 4                        # a question across two Domains
    assert ids(Filter(domains=("finance",))) == []
    assert {r["item_id"] for r in store.text_search("feedback", Filter(domains=("leadership",)), 10)} == \
        {"adv:LLLLLLLLLL2:a01", "adv:BOTHBOTHBO3:a01"}
    hits = search(store, emb, "feedback for my team", domains=["leadership"], top_k=5)
    assert hits and all("leadership" in h["domains"] for h in hits)
    both = search(store, emb, "talk to users and give feedback", domains=["startup", "leadership"], top_k=5)
    assert {d for h in both for d in h["domains"]} == {"startup", "leadership"}
    assert store.get("adv:NODOMAINNN4:a01")["domains"] == ["startup"] and store.get("adv:LLLLLLLLLL2:a01")["source_id"] == "books_x"
    assert Filter.of(domains=["a", "a", "b"]).domains == ("a", "b") and Filter.of(domains=[]) is None


def test_a_pack_built_before_domains_opens_as_all_startup():
    out = Path(tempfile.mkdtemp())
    _build(out, rows=ROWS)
    db = sqlite3.connect(out / P.PACK_FILE)
    for col in ("domains", "source_id"):
        db.execute(f"ALTER TABLE items DROP COLUMN {col}")
    db.commit()
    db.close()
    store = P.PackStore(out)                                   # (no verify: the file changed)
    assert all(r["domains"] == ["startup"] and r["source_id"] == "" for r in store.rows())
    assert store.vector_search(HashEmbed()(["pricing"])[0], Filter(domains=("startup",)), 5)
    assert not store.vector_search(HashEmbed()(["pricing"])[0], Filter(domains=("leadership",)), 5)


# --- the Domain router (M5d) -----------------------------------------------------------------------
class AxisEmbed(HashEmbed):
    """Four axes, so scores are exact: price -> 0, feedback -> 1, equity -> 2, anything else -> 3."""
    name, dim = "axis-4", 4

    def _vec(self, t):
        import math
        w = t.lower().split()
        v = [float(w.count("price")), float(w.count("feedback")), float(w.count("equity")), 0.01 * (1 + w.count("misc"))]
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]


def _axis_rows():
    rows = []
    for i in range(30):          # a big Domain whose items lean toward the other Domain's subject too
        rows.append({**_row(f"adv:S{i:010d}:a01", "advice", f"S{i:010d}", f"price price feedback n{i}"), "domains": ["startup"]})
    for i in range(3):           # a small one
        rows.append({**_row(f"adv:L{i:010d}:a01", "advice", f"L{i:010d}", f"feedback feedback l{i}"), "domains": ["leadership"]})
    for i in range(2):
        rows.append({**_row(f"adv:F{i:010d}:a01", "advice", f"F{i:010d}", f"equity equity f{i}"), "domains": ["finance"]})
    return rows


def _axis_store():
    out = Path(tempfile.mkdtemp())
    manifest, emb = _build(out, rows=_axis_rows(), emb=AxisEmbed())
    return P.PackStore(out), emb, manifest


def test_the_router_scores_each_domain_by_its_closest_items_and_routes_those_near_the_best():
    from founder_coach import router as R
    store, emb, manifest = _axis_store()
    assert store.domain_counts() == {"finance": 2, "leadership": 3, "startup": 30}
    sc = store.domain_scores(emb(["feedback"])[0], 3)
    assert abs(sc["leadership"] - 1.0) < 1e-3 and abs(sc["startup"] - 1 / 5 ** .5) < 1e-3 and sc["finance"] < 0.02, sc
    # a small Domain is not drowned by a large one: it scores on its own best items
    assert R.route(store, emb(["feedback"])[0]).domains == ("leadership",)
    assert R.route(store, emb(["equity"])[0]).domains == ("finance",)
    both = emb(["price feedback"])[0]
    assert R.route(store, both).domains == ("startup",), "the default margin is narrow: startup is 0.24 ahead here"
    assert R.route(store, both, margin=0.3).domains == ("startup", "leadership")
    three = emb(["feedback feedback equity"])[0]                 # leadership .89, finance .45, startup .40
    assert R.route(store, three, margin=2.0, max_domains=2).domains == ("leadership", "finance")
    assert R.route(store, three, margin=2.0).domains == ("leadership", "finance", "startup")
    assert R.route(store, three, margin=0.0).domains == ("leadership",)
    assert R.route(store, both, margin=0.3).as_list()[0] == {"domain": "startup", "score": round(R.route(store, both).scores[0][1], 3)}
    # one Domain in the Library: nothing to choose between
    one = Path(tempfile.mkdtemp())
    _build(one, rows=ROWS, emb=AxisEmbed())
    assert R.route(P.PackStore(one), emb(["price"])[0]).domains == () and not R.route(P.PackStore(one), emb(["price"])[0]).routed
    assert R.route(object(), emb(["price"])[0]).domains == (), "a store with no domain_scores routes nothing"
    assert R.choose({}).domains == () and R.choose({"a": 0.5}).domains == ()


def test_routing_favours_the_routed_domains_and_a_floor_keeps_every_routed_domain_in_the_answer():
    store, emb, _ = _axis_store()
    ids = lambda res: [r["item_id"][4] for r in res]
    q = "price feedback"
    assert ids(search(store, emb, q, top_k=5)).count("L") == 0, "unrouted, the big Domain crowds the small one out"
    info = {}
    res = search(store, emb, q, top_k=5, route={"margin": 0.3}, info=info)
    assert info["routing"].domains == ("startup", "leadership")
    assert ids(res).count("L") == 2 and ids(res).count("S") == 3 and ids(res).count("F") == 0, ids(res)
    assert [r["score"] for r in res] == sorted((r["score"] for r in res), reverse=True)
    assert len({r["item_id"] for r in res}) == 5
    # one routed Domain: no floor, and the same results as an unrouted search of that question
    plain = search(store, emb, "price", top_k=5)
    assert [r["item_id"] for r in search(store, emb, "price", top_k=5, route=True)] == [r["item_id"] for r in plain]
    # explicit domains win: no routing is made and results are limited to them
    info = {}
    res = search(store, emb, q, top_k=5, domains=["leadership"], route=True, info=info)
    assert "routing" not in info and set(ids(res)) == {"L"}
    # a floor never pulls in a Domain whose items are far less relevant than the best result
    res = search(store, emb, "price price price feedback", top_k=5, route={"margin": 2.0})
    assert ids(res).count("S") >= 3
    # no route requested: nothing changes for anyone
    info = {}
    search(store, emb, q, top_k=5, info=info)
    assert "routing" not in info


def test_naming_several_domains_guarantees_each_a_share_so_a_big_domain_cannot_crowd_a_small_one_out():
    store, emb, _ = _axis_store()
    ids = lambda res: [r["item_id"][4] for r in res]
    q = "price feedback"
    both = search(store, emb, q, top_k=5, domains=["startup", "leadership"])
    assert ids(both).count("L") >= 2 and ids(both).count("S") >= 2 and "F" not in ids(both), ids(both)
    assert [r["score"] for r in both] == sorted((r["score"] for r in both), reverse=True) and len({r["item_id"] for r in both}) == 5
    assert ids(search(store, emb, q, top_k=5, domains=["startup", "leadership", "startup"])) == ids(both), "repeats don't count twice"
    # one Domain (even named twice): a plain filtered search, no floor, same as before
    one = search(store, emb, q, top_k=5, domains=["leadership"])
    assert set(ids(one)) == {"L"} and ids(search(store, emb, q, top_k=5, domains=["leadership", "leadership"])) == ids(one)
    # the floor still never pads with results far less relevant than the best
    far = search(store, emb, "price price price price", top_k=5, domains=["startup", "leadership"])
    assert ids(far).count("S") >= 3, ids(far)
    # no routing is made for explicit Domains
    info = {}
    search(store, emb, q, top_k=5, domains=["startup", "leadership"], route=True, info=info)
    assert "routing" not in info


def test_a_pack_records_what_each_domain_is_about_and_whether_the_server_routes():
    out = Path(tempfile.mkdtemp())
    manifest, _ = _build(out, rows=_dom_rows())
    assert manifest["router"] == {"enabled": False}, "off until the eval says otherwise"
    from ytbrain import domains as D
    info = manifest["domain_info"]
    assert set(info) == set(D.load().names), "every Domain declared, items or not"
    assert info["leadership"]["risk_tier"] == "low" and info["finance"]["risk_tier"] == "high"
    assert info["leadership"]["description"] and info["startup"]["examples"]
    assert info["finance"]["web_policy"] == "always_latest" and info["finance"]["freshness_days"] == 730
    assert "freshness_days" not in info["startup"], "evergreen Domains carry no half-life"
    assert manifest["domains"] == {"leadership": 2, "startup": 3}, "counts stay the Domains with items"
    with contextlib.redirect_stdout(io.StringIO()):
        on = build_pack(Source(_dom_rows()), HashEmbed(), Path(tempfile.mkdtemp()), rerank_model="none",
                        say=lambda m: None, route={"margin": 0.07})
    assert on["router"] == {"enabled": True, "margin": 0.07}


def test_domain_scores_and_routed_search_agree_between_the_lance_index_and_the_pack():
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return
    from ytbrain.knowledge.store import KnowledgeStore
    tmp = Path(tempfile.mkdtemp())
    emb, rows = AxisEmbed(), _axis_rows()
    lance = KnowledgeStore(path=tmp / "lance")
    for r in rows:
        lance.replace_document(r["doc_id"], [{**r, "vector": emb([r["indexable"]])[0], "embed_model": emb.name}], emb.dim)
    _build(tmp / "pack", rows=rows, emb=emb)
    pack = P.PackStore(tmp / "pack")
    assert lance.domain_names() == ["finance", "leadership", "startup"] == sorted(pack.domain_counts())
    for q in ("price", "feedback", "price feedback", "equity feedback feedback", "unrelated words misc"):
        v = emb([q])[0]
        a, b = lance.domain_scores(v, 3), pack.domain_scores(v, 3)
        assert a.keys() == b.keys() and all(abs(a[d] - b[d]) < 2e-3 for d in a), (q, a, b)
    ids = lambda res: [r["item_id"][4] for r in res]
    from_lance = search(lance, emb, "price feedback", top_k=5, route={"margin": 0.3})
    assert ids(from_lance).count("L") == 2 and ids(from_lance) == ids(search(pack, emb, "price feedback", top_k=5, route={"margin": 0.3}))


if __name__ == "__main__":
    import inspect
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f()
        print(f"  PASS  {f.__name__}")
    print(f"{len(fns)}/{len(fns)} passed")
