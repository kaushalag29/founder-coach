"""The Knowledge pack: every Verified Knowledge item in one read-only SQLite file
[ADR-0009]. Built by `ytbrain pack build`, read by the coach runtime.

Layout (FORMAT_VERSION 1; `domains` and `source_id` columns were added later, and a pack without them
opens as all-startup):
  meta       key -> JSON value: format, models and their prefixes, corpus statistics
  items      one row per item; column `n` (1..N) is also its row in the vector matrix
  items_fts  FTS5 over `indexable` (porter stemming), contentless view of `items`
  vectors    one row: the N x dim matrix, L2-normalised, little-endian float16

The pack has no Passages, so it is small (tens of MB) and brute-force cosine search over
all vectors takes milliseconds: no vector database. A sidecar `pack.json` repeats the
meta plus the file's sha256, so a copy can be verified before it's trusted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

from .search import DEFAULT_DOMAIN, Filter
from . import product

FORMAT_VERSION = 1
PACK_FILE = "knowledge.sqlite"
MANIFEST_FILE = "pack.json"
KINDS = ("advice", "takeaway", "summary")          # the default; Passages only with --with-passages
ALL_KINDS = KINDS + ("passage",)                    # (private beta only: ADR-0009, phase3 §10.2)

TEXT_COLS = ("item_id", "kind", "doc_id", "text", "evidence", "deep_link", "title", "speaker",
             "series", "provenance", "published_at", "stage_origin", "source_kind",
             "context_header", "indexable", "source_id")
INT_COLS = ("start_ms", "end_ms", "year")
LIST_COLS = ("stages", "topics", "domains")
COLUMNS = TEXT_COLS + INT_COLS + LIST_COLS

DDL = [
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE items (n INTEGER PRIMARY KEY, "
    + ", ".join(f"{c} TEXT NOT NULL DEFAULT ''" for c in TEXT_COLS) + ", "
    + ", ".join(f"{c} INTEGER NOT NULL DEFAULT -1" for c in INT_COLS) + ", "
    + ", ".join(f"{c} TEXT NOT NULL DEFAULT '[]'" for c in LIST_COLS) + ")",
    "CREATE UNIQUE INDEX items_item_id ON items(item_id)",
    "CREATE INDEX items_doc ON items(doc_id)",
    "CREATE VIRTUAL TABLE items_fts USING fts5(indexable, content='items', content_rowid='n', "
    "tokenize='porter unicode61 remove_diacritics 2')",
    "CREATE TABLE vectors (id INTEGER PRIMARY KEY CHECK (id = 1), rows INTEGER NOT NULL, "
    "dim INTEGER NOT NULL, dtype TEXT NOT NULL, data BLOB NOT NULL)",
]

# Words that only add noise to an OR query (BM25 already discounts them; dropping them
# keeps "how do I ..." questions from matching every row).
STOPWORDS = frozenset("""a an and are as at be been but by can could did do does doing for from had
has have how i if in into is it its just me my of on or our should so than that the their them
then there these they this to was we were what when where which who why will with would you
your""".split())


def resolve(path: str | Path) -> Path:
    """A pack file, or the directory holding knowledge.sqlite."""
    p = Path(path).expanduser()
    return p / PACK_FILE if p.is_dir() else p


def pack_candidates() -> list[Path]:
    """Where a pack is looked for when none is named, in order: next to the runtime (the
    assembled plugin keeps `pack/` beside `founder_coach/`), the dev repo's `data/pack`, then
    the Founder's home (~/.founder-coach/pack)."""
    from . import domain as D
    here = Path(__file__).resolve().parents[1]
    return [here / "pack", here / "data" / "pack", D.home() / "pack"]


def find_pack(arg: str | Path | None = None) -> Path:
    """The pack to open: `arg` (the plugin passes --pack), then $FOUNDER_COACH_PACK, then the
    first of pack_candidates() that exists (the last one if none does, for the error message).
    Kept here, free of MCP imports, so the CLI and the plugin assembler's --check can use it."""
    for cand in (arg, product.env("PACK")):
        if cand:
            return Path(cand).expanduser()
    cands = pack_candidates()
    return next((c for c in cands if (c / "pack.json").exists() or (c / "knowledge.sqlite").exists()), cands[-1])


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fts_query(query: str) -> str | None:
    """A safe FTS5 MATCH expression: the query's words, quoted, OR-ed; None if nothing
    is left. Quoting makes every user character literal (no FTS5 syntax injection)."""
    words = re.findall(r"[^\W_]+", query.lower())
    keep = list(dict.fromkeys(w for w in words if len(w) > 1 and w not in STOPWORDS))
    return " OR ".join(f'"{w}"' for w in keep) or None


def _memo_path() -> Path:
    from . import domain as D
    return D.home() / "pack-verified.json"


def _verified_memo() -> dict:
    try:
        return json.loads(_memo_path().read_text())
    except (OSError, ValueError):
        return {}


def _remember_verified(key: str, stamp: dict) -> None:
    try:
        memo = _verified_memo()
        memo[key] = stamp
        p = _memo_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(memo))
        os.replace(tmp, p)
    except OSError:
        pass                                          # only a speed-up: never fail on it


def _manifest_sha(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise RuntimeError(f"{path.name} is unreadable ({e}): rebuild the pack") from e
    return data.get("sha256") if isinstance(data, dict) else None


class PackStore:
    """Read-only search store over a Knowledge pack (same interface as the ytbrain index)."""

    def __init__(self, path: str | Path, verify: bool | str = False):
        """verify=True hashes the whole file; verify="cached" does that once per pack version
        (file size + mtime + manifest checksum remembered in the coach's home), so the MCP
        server's start-up stays fast."""
        import numpy as np
        self.path = resolve(path)
        if not self.path.exists():
            raise RuntimeError(f"no Knowledge pack at {self.path}: build one with `ytbrain pack build`, "
                               f"or pass --pack PATH (or set {product.ENV_PREFIX}PACK)")
        if verify:
            self.verify(cached=verify == "cached")
        # immutable=1: a shipped pack never changes in place (a new one replaces the file),
        # so SQLite can skip locking entirely and several processes can share it.
        self._db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
                                   check_same_thread=False)
        self._lock = threading.Lock()
        try:
            self.meta = {k: json.loads(v) for k, v in self._db.execute("SELECT key, value FROM meta")}
            fmt = self.meta.get("format_version")
            if not isinstance(fmt, int) or fmt > FORMAT_VERSION:
                raise RuntimeError(f"Knowledge pack format {fmt} is newer than this runtime supports "
                                   f"({FORMAT_VERSION}): update the coach")
            # a pack built before a column existed opens with that column's default
            have = {r[1] for r in self._db.execute("PRAGMA table_info(items)")}
            cols = [c for c in COLUMNS if c in have]
            missing = {c: (-1 if c in INT_COLS else "[]" if c in LIST_COLS else "") for c in COLUMNS if c not in have}
            missing["domains"] = json.dumps([DEFAULT_DOMAIN])      # a pack from before Domains: all startup
            cur = self._db.execute(f"SELECT n, {', '.join(cols)} FROM items ORDER BY n")
            self._rows: list[dict] = []
            for rec in cur:
                row = {**missing, **dict(zip(cols, rec[1:]))}
                for c in LIST_COLS:
                    row[c] = json.loads(row[c] or "[]")
                row["domains"] = row["domains"] or [DEFAULT_DOMAIN]
                self._rows.append(row)
            if [r for r in self._db.execute("SELECT n FROM items ORDER BY n LIMIT 1")] not in ([], [(1,)]):
                raise RuntimeError(f"{self.path}: item numbering must start at 1")
            self._by_id = {r["item_id"]: i for i, r in enumerate(self._rows)}
            got = self._db.execute("SELECT rows, dim, dtype, data FROM vectors WHERE id = 1").fetchone()
            if got is None:
                raise RuntimeError(f"{self.path} has no vectors")
            rows, dim, dtype, data = got
            if rows != len(self._rows):
                raise RuntimeError(f"{self.path}: {rows} vectors for {len(self._rows)} items")
            self._m = np.frombuffer(data, dtype=np.dtype(dtype).newbyteorder("<")) \
                .reshape(rows, dim).astype(np.float32)
            self.dim = dim
        except RuntimeError:
            self._db.close()
            raise
        except (sqlite3.DatabaseError, ValueError, KeyError, TypeError) as e:
            # a damaged or foreign file must surface as "pack unavailable", never crash the caller
            self._db.close()
            raise RuntimeError(f"{self.path} is not a Knowledge pack, or is damaged ({type(e).__name__}: {e})") from e

    # -- identity -------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return bool(self._rows)

    def count(self) -> int:
        return len(self._rows)

    def embed_model(self) -> str | None:
        return self.meta.get("embed_model")

    def verify(self, cached: bool = False) -> None:
        """Raise if the file doesn't match the sha256 in the sidecar manifest."""
        manifest = self.path.with_name(MANIFEST_FILE)
        if not manifest.exists():
            raise RuntimeError(f"{manifest} missing: can't verify {self.path.name}")
        want = _manifest_sha(manifest)
        st = self.path.stat()
        key, stamp = str(self.path.resolve()), {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": want}
        memo = _verified_memo()
        if cached and want and memo.get(key) == stamp:
            return                                   # this exact pack version already passed
        got = sha256_file(self.path)
        nxt = manifest.with_name(manifest.name + ".next")
        if want != got and nxt.exists() and _manifest_sha(nxt) == got:
            want = got                  # a build was killed between swapping the file and its manifest
        if want == got:
            _remember_verified(key, stamp)
        if want != got:
            raise RuntimeError(f"{self.path.name} is corrupt or was changed "
                               f"(sha256 {got[:12]}..., manifest says {str(want)[:12]}...)")

    # -- search ---------------------------------------------------------------
    def _mask(self, flt: Filter | None):
        import numpy as np
        if flt is None or flt.empty:
            return None
        return np.fromiter((flt.matches(r) for r in self._rows), dtype=bool, count=len(self._rows))

    def _domain_rows(self) -> dict:
        """Domain -> the row numbers of its items (an item in two Domains is in both)."""
        import numpy as np
        if getattr(self, "_dom_rows", None) is None:
            idx: dict[str, list[int]] = {}
            for i, r in enumerate(self._rows):
                for d in r["domains"]:
                    idx.setdefault(d, []).append(i)
            self._dom_rows = {d: np.asarray(v, dtype=np.int64) for d, v in sorted(idx.items())}
        return self._dom_rows

    def domain_counts(self) -> dict[str, int]:
        return {d: int(len(v)) for d, v in self._domain_rows().items()}

    def domain_scores(self, vector, top_n: int = 3) -> dict[str, float]:
        """Per Domain with items: the mean cosine of its `top_n` closest items (the router's signal)."""
        import numpy as np
        q = np.asarray(vector, dtype=np.float32).reshape(-1)
        if q.shape[0] != self.dim:
            raise RuntimeError(f"query vector has {q.shape[0]} dimensions, the pack {self.dim} "
                               f"(built with {self.embed_model()})")
        norm = float(np.linalg.norm(q))
        sims = self._m @ (q / norm if norm else q)
        out = {}
        for d, idx in self._domain_rows().items():
            part = sims[idx]
            k = min(top_n, part.size)
            out[d] = float(np.partition(part, part.size - k)[part.size - k:].mean())
        return out

    def vector_search(self, vector, flt: Filter | None, limit: int) -> list[dict]:
        import numpy as np
        if limit <= 0 or not self._rows:
            return []
        q = np.asarray(vector, dtype=np.float32).reshape(-1)
        if q.shape[0] != self.dim:
            raise RuntimeError(f"query vector has {q.shape[0]} dimensions, the pack {self.dim} "
                               f"(built with {self.embed_model()})")
        norm = float(np.linalg.norm(q))
        q = q / norm if norm else q
        mask = self._mask(flt)
        idx = np.arange(len(self._rows)) if mask is None else np.nonzero(mask)[0]
        if idx.size == 0:
            return []
        scores = self._m[idx] @ q
        k = min(limit, idx.size)
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.lexsort((idx[top], -scores[top]))]        # best first; ties by row: stable
        return [{**self._rows[idx[t]], "_distance": float(1.0 - scores[t]), "_similarity": float(scores[t])}
                for t in top]

    def text_search(self, query: str, flt: Filter | None, limit: int) -> list[dict]:
        expr = fts_query(query)
        if not expr or limit <= 0:
            return []
        filtered = flt is not None and not flt.empty
        sql = "SELECT rowid FROM items_fts WHERE items_fts MATCH ? ORDER BY bm25(items_fts)"
        params: tuple = (expr,)
        if not filtered:
            sql += " LIMIT ?"
            params = (expr, limit)
        try:
            with self._lock:
                hits = [n for (n,) in self._db.execute(sql, params)]
        except sqlite3.OperationalError:
            return []
        out = []
        for n in hits:
            row = self._rows[n - 1]
            if filtered and not flt.matches(row):
                continue
            out.append(dict(row))
            if len(out) >= limit:
                break
        return out

    # -- lookups (coach_read) -------------------------------------------------
    def get(self, item_id: str) -> dict | None:
        i = self._by_id.get(item_id)
        return dict(self._rows[i]) if i is not None else None

    def document_items(self, doc_id: str) -> list[dict]:
        return [dict(r) for r in self._rows if r["doc_id"] == doc_id]

    def rows(self, kinds: list[str] | None = None, columns: list[str] | None = None) -> list[dict]:
        keep = [r for r in self._rows if not kinds or r["kind"] in kinds]
        return [{c: r.get(c) for c in columns} if columns else dict(r) for r in keep]

    def close(self) -> None:
        self._db.close()
