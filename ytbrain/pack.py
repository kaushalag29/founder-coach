"""`ytbrain pack build`: the Knowledge pack the coach plugin ships [ADR-0009].

Every Verified Advice, Takeaway and summary from the index (no Passages), embedded
with a small ONNX model, in one SQLite file the runtime reads (founder_coach.pack).

Resumable and cheap to repeat: vectors are cached per (model, prefix, text) in
data/pack/embed-cache.db, committed batch by batch, so an interrupted build re-embeds
nothing it already did and a rebuild after new talks embeds only their items. The pack
is written to a temp file, checked by opening and searching it, and only then renamed
over the old one; the sidecar pack.json carries its sha256.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

from founder_coach import pack as P

from . import __version__
from .pages import atomic_write_text

CACHE_FILE = "embed-cache.db"
DTYPE = "float16"                  # half the size of float32; cosine ranks are unchanged in practice


class EmbedCache:
    """Vectors keyed by (model, prefix, sha256(text)); every put is committed."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS vecs (model TEXT, prefix TEXT, sha TEXT, "
                        "dim INTEGER, data BLOB, PRIMARY KEY (model, prefix, sha))")

    def get(self, model: str, prefix: str, shas: list[str]) -> dict:
        import numpy as np
        out = {}
        for i in range(0, len(shas), 500):
            chunk = shas[i:i + 500]
            q = (f"SELECT sha, dim, data FROM vecs WHERE model = ? AND prefix = ? AND sha IN "
                 f"({','.join('?' * len(chunk))})")
            for sha, dim, data in self.db.execute(q, (model, prefix, *chunk)):
                out[sha] = np.frombuffer(data, dtype="<f4").reshape(dim)
        return out

    def put(self, model: str, prefix: str, pairs) -> None:
        import numpy as np
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO vecs VALUES (?, ?, ?, ?, ?)",
                [(model, prefix, sha, int(v.shape[0]), np.asarray(v, dtype="<f4").tobytes())
                 for sha, v in pairs])

    def close(self) -> None:
        self.db.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(row: dict) -> dict:
    out = {}
    for c in P.TEXT_COLS:
        out[c] = str(row.get(c) or "")
    for c in P.INT_COLS:
        v = row.get(c)
        out[c] = int(v) if v is not None else -1
    for c in P.LIST_COLS:
        out[c] = [str(x) for x in (row.get(c) or [])]
    return out


def corpus_stats(rows: list[dict]) -> dict:
    """What the pack covers, for coach_corpus_status and the data card."""
    talks = {r["doc_id"] for r in rows}
    series: dict[str, set] = defaultdict(set)
    for r in rows:
        series[r["series"] or "(none)"].add(r["doc_id"])
    years = [r["year"] for r in rows if r["year"] and r["year"] > 0]
    return {"items": len(rows), "by_kind": dict(sorted(Counter(r["kind"] for r in rows).items())),
            "talks": len(talks),
            "series": {k: len(v) for k, v in sorted(series.items(), key=lambda kv: -len(kv[1]))},
            "years": [min(years), max(years)] if years else [],
            "speakers": len({r["speaker"] for r in rows if r["speaker"]})}


def _embed_all(rows: list[dict], embedder, cache: EmbedCache, batch: int, say) -> list:
    """One vector per row, from the cache or the model (cached as it goes)."""
    model, prefix = embedder.name, getattr(embedder, "doc_prefix", "")
    shas = [_sha(r["indexable"]) for r in rows]
    have = cache.get(model, prefix, sorted(set(shas)))
    todo: dict[str, str] = {}
    for sha, r in zip(shas, rows):
        if sha not in have and sha not in todo:
            todo[sha] = r["indexable"]
    say(f"pack: {len(rows)} items · {len(have)} vectors cached · {len(todo)} to embed "
        f"with {model}")
    started, last, done = time.time(), time.time(), 0
    # batches of similar length: each batch is padded to its longest text, so mixing a
    # 500-token summary with 40-token advice wastes most of the compute
    pending = sorted(todo.items(), key=lambda kv: len(kv[1]))
    for i in range(0, len(pending), batch):
        part = pending[i:i + batch]
        vecs = embedder.documents([t for _, t in part])
        pairs = list(zip([s for s, _ in part], vecs))
        cache.put(model, prefix, pairs)             # committed: a Ctrl+C keeps this batch
        have.update(pairs)
        done += len(part)
        if time.time() - last >= 20 or done == len(pending) or done == len(part):
            rate = done / max(1e-6, time.time() - started)
            say(f"    embedded {done}/{len(pending)} · {rate:.0f}/s · "
                f"ETA {(len(pending) - done) / max(rate, 1e-6) / 60:.1f} min")
            last = time.time()
    return [have[s] for s in shas]


def _write(tmp: Path, rows: list[dict], matrix, meta: dict) -> None:
    import numpy as np
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    try:
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        for stmt in P.DDL:
            db.execute(stmt)
        cols = ("n",) + P.COLUMNS
        db.executemany(
            f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [(n, *[json.dumps(r[c]) if c in P.LIST_COLS else r[c] for c in P.COLUMNS])
             for n, r in enumerate(rows, 1)])
        db.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
        data = np.ascontiguousarray(matrix, dtype=np.dtype(DTYPE).newbyteorder("<")).tobytes()
        db.execute("INSERT INTO vectors VALUES (1, ?, ?, ?, ?)",
                   (matrix.shape[0], matrix.shape[1], DTYPE, data))
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
        db.commit()
        db.execute("INSERT INTO items_fts(items_fts) VALUES ('optimize')")
        db.commit()
        db.execute("VACUUM")
    finally:
        db.close()


def _check(tmp: Path, rows: list[dict], matrix, embedder) -> None:
    """Open the new pack and search it before it replaces anything."""
    store = P.PackStore(tmp)
    try:
        if store.count() != len(rows):
            raise RuntimeError(f"pack check: {store.count()} items written, {len(rows)} expected")
        probe = rows[len(rows) // 2]
        hit = store.vector_search(matrix[len(rows) // 2], None, 1)
        # (an item with an identical twin may return the twin: same vector, distance ~0)
        if not hit or hit[0]["_distance"] > 0.01:
            raise RuntimeError("pack check: an item's own vector doesn't find it")
        words = " ".join(probe["text"].split()[:8])
        if P.fts_query(words) and not store.text_search(words, None, 20):
            raise RuntimeError("pack check: full-text search finds nothing for an item's own words")
        q = embedder([probe["text"]])[0]
        if len(q) != store.dim:
            raise RuntimeError(f"pack check: query vectors have {len(q)} dims, items {store.dim}")
    finally:
        store.close()


def build_pack(source, embedder, out_dir: Path, *, rerank_model: str | None,
               kinds: tuple[str, ...] = P.KINDS, batch: int = 64, say=print,
               source_info: dict | None = None, include_private: bool = False) -> dict:
    """Build out_dir/knowledge.sqlite + pack.json from `source.rows(kinds=...)`.
    A Private Source's items (your own Books, ADR-0014) are left out unless `include_private`:
    such a pack is for your own coach only, and `scripts/release.py` refuses it. Returns the manifest."""
    import numpy as np
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f".{P.PACK_FILE}.tmp-*"):        # left by a killed build
        old.unlink(missing_ok=True)
    raw = list(source.rows(kinds=list(kinds)))
    private = [r for r in raw if (r.get("visibility") or "public") == "private"]
    if private and not include_private:
        say(f"pack: leaving out {len(private)} private items (your Books; `--include-private` "
            "keeps them, for your own coach only)")
        raw = [r for r in raw if (r.get("visibility") or "public") != "private"]
    rows = sorted((_clean(r) for r in raw), key=lambda r: r["item_id"])
    rows = [r for r in rows if r["kind"] in kinds]          # Passages only when asked for (--with-passages)
    if not rows:
        raise RuntimeError("no Verified items in the index -- run `ytbrain index` first")
    dupes = [i for i, n in Counter(r["item_id"] for r in rows).items() if n > 1]
    if dupes:
        raise RuntimeError(f"duplicate item ids in the index, e.g. {dupes[0]}: rebuild it with `ytbrain index`")
    cache = EmbedCache(out_dir / CACHE_FILE)
    try:
        vecs = _embed_all(rows, embedder, cache, batch, say)
    finally:
        cache.close()
    matrix = np.vstack(vecs).astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.where(norms == 0, 1.0, norms)
    meta = {"format_version": P.FORMAT_VERSION,
            "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "built_by": f"ytbrain {__version__}",
            "kinds": list(kinds),
            "embed_model": embedder.name, "dim": int(matrix.shape[1]), "dtype": DTYPE,
            "query_prefix": getattr(embedder, "query_prefix", ""),
            "doc_prefix": getattr(embedder, "doc_prefix", ""),
            "rerank_model": rerank_model or "none",
            "source": source_info or {},
            "private_items": len(private) if include_private else 0,
            **corpus_stats(rows)}
    final = out_dir / P.PACK_FILE
    tmp = out_dir / f".{P.PACK_FILE}.tmp-{os.getpid()}"
    try:
        say(f"pack: writing {len(rows)} items ...")
        _write(tmp, rows, matrix, meta)
        _check(tmp, rows, matrix, embedder)
        manifest = {**meta, "file": P.PACK_FILE, "bytes": tmp.stat().st_size,
                    "sha256": P.sha256_file(tmp)}
        # the new manifest goes down first as pack.json.next: a kill between the two swaps
        # leaves a pack whose checksum still verifies (against .next), never a false "corrupt"
        nxt = out_dir / (P.MANIFEST_FILE + ".next")
        atomic_write_text(nxt, json.dumps(manifest, indent=1, ensure_ascii=False))
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    os.replace(nxt, out_dir / P.MANIFEST_FILE)
    return manifest


def describe(manifest: dict) -> str:
    mb = manifest.get("bytes", 0) / 1e6
    kinds = ", ".join(f"{n} {k}" for k, n in manifest.get("by_kind", {}).items())
    years = manifest.get("years") or ["?", "?"]
    return (f"Knowledge pack: {manifest['items']} items ({kinds}) from {manifest['talks']} talks, "
            f"{years[0]}-{years[1]} · {mb:.1f} MB\n"
            f"  embedding {manifest['embed_model']} ({manifest['dim']} dims, {manifest['dtype']}) · "
            f"reranker {manifest['rerank_model']}\n"
            f"  built {manifest['built_at']} by {manifest['built_by']} · sha256 {manifest['sha256'][:16]}...")
