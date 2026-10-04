"""The Knowledge index on disk: one LanceDB table of Knowledge items [ADR-0003].

A Document's items are replaced as a unit (delete + add). The index Step marks
the Document done only afterwards, so an interruption between the two simply
redoes that Document. LanceDB versions every write, so readers keep seeing the
last complete version while a rebuild runs.
"""
from __future__ import annotations

import re
from pathlib import Path

from founder_coach.search import DEFAULT_DOMAIN

from ..config import KNOWLEDGE_TABLE, LANCE_DIR

STRING_COLS = ("item_id", "kind", "doc_id", "text", "evidence", "deep_link", "source_kind",
               "series", "title", "speaker", "provenance", "published_at", "stage_origin",
               "context_header", "indexable", "embed_model", "visibility", "source_id")
LIST_COLS = ("stages", "topics", "domains")
INT_COLS = ("start_ms", "end_ms", "year")
COLUMN_DEFAULTS = {"source_kind": "talk",     # for rows written before the column existed
                   "visibility": "public"}


def _schema(dim: int):
    import pyarrow as pa
    fields = [pa.field(c, pa.string()) for c in STRING_COLS]
    fields += [pa.field(c, pa.list_(pa.string())) for c in LIST_COLS]
    fields += [pa.field(c, pa.int64()) for c in INT_COLS]
    fields.append(pa.field("vector", pa.list_(pa.float32(), dim)))
    return pa.schema(fields)


RETAG_BATCH = 200                      # Documents per commit when re-tagging


def _q(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class KnowledgeStore:
    def __init__(self, path=LANCE_DIR, table: str = KNOWLEDGE_TABLE):
        try:
            import lancedb
        except ImportError as e:
            from .embed import MISSING_EXTRA
            raise RuntimeError(MISSING_EXTRA) from e
        path.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(path))
        self._path = Path(path)
        self.name = table
        listed = self.db.list_tables()
        names = set(getattr(listed, "tables", listed))      # lancedb returns a response object
        self._t = self.db.open_table(table) if table in names else None

    # -- writing --------------------------------------------------------------
    def _table(self, dim: int):
        if self._t is None:
            self._t = self.db.create_table(self.name, schema=_schema(dim))
        else:
            self._upgrade()
        return self._t

    def _upgrade(self) -> None:
        """An index built before a column existed gets it, with the value its rows had implicitly
        (every item before `source_kind` came from a talk). Without this, adding a Document's new
        rows fails with "field ... does not exist in table schema"."""
        have = set(self._t.schema.names)
        add = {c: (f"'{COLUMN_DEFAULTS.get(c, '')}'") for c in STRING_COLS if c not in have}
        add.update({c: "CAST(-1 AS BIGINT)" for c in INT_COLS if c not in have})
        if add:
            self._t.add_columns(add)
        lists = [c for c in LIST_COLS if c not in have]
        if lists:                                    # NULL until `retag` fills them: see `ytbrain index`
            import pyarrow as pa
            self._t.add_columns([pa.field(c, pa.list_(pa.string())) for c in lists])

    def replace_document(self, doc_id: str, rows: list[dict], dim: int) -> None:
        t = self._table(dim)
        t.delete(f"doc_id = {_q(doc_id)}")
        if rows:
            t.add(rows)

    def retag(self, tags: dict[str, tuple[list[str], str]]) -> int:
        """Set each Document's Domains and Source id (`tags`: doc_id -> (domains, source_id)) on its
        items, without embedding anything: only Documents whose stored tags differ are updated in place (never
        re-embedded or rewritten, so an interrupted retag loses nothing). Returns how many Documents changed."""
        if self._t is None or not self.count():
            return 0
        self._upgrade()
        have: dict[str, set] = {}
        for r in self._t.search().select(["doc_id", "domains", "source_id"]).limit(self.count()).to_list():
            have.setdefault(r["doc_id"], set()).add((tuple(sorted(r.get("domains") or ())), r.get("source_id") or ""))
        groups: dict[tuple, list[str]] = {}
        for doc_id, (domains, source_id) in tags.items():
            want = (tuple(sorted(domains or [DEFAULT_DOMAIN])), source_id or "")
            if doc_id in have and have[doc_id] != {want}:
                groups.setdefault(want, []).append(doc_id)
        # One in-place update per tag set and batch of Documents: vectors are never read or rewritten, a
        # crash leaves whole batches tagged and the rest as they were (a rerun finishes them), and the
        # table gains a version per batch, not per Document. Duplicate item ids don't matter to it.
        for (domains, source_id), docs in groups.items():
            values = {"domains": "make_array(" + ", ".join(_q(d) for d in domains) + ")", "source_id": _q(source_id)}
            for i in range(0, len(docs), RETAG_BATCH):
                self._t.update(where="doc_id IN (" + ", ".join(_q(d) for d in docs[i:i + RETAG_BATCH]) + ")",
                               values_sql=values)
        return sum(len(d) for d in groups.values())

    def delete_documents_except(self, keep: set[str]) -> int:
        if self._t is None:
            return 0
        present = set(self._t.to_arrow().column("doc_id").to_pylist())
        gone = sorted(present - keep)
        for d in gone:
            self._t.delete(f"doc_id = {_q(d)}")
        return len(gone)

    def build_fulltext_index(self) -> None:
        if self._t is None:
            return
        from lancedb.index import FTS
        self._fts_marker().unlink(missing_ok=True)
        self._t.create_index("indexable", config=FTS(), replace=True)
        self._fts_marker().write_text(str(self._t.count_rows()) + ":" + str(self._t.version))

    def _fts_marker(self):
        return self._path / f".{self.name}.fts-built"

    def fulltext_current(self) -> bool:
        """True when the full-text index was built after the table's last change. An
        interrupted build leaves no marker, so the next index run rebuilds it."""
        if self._t is None:
            return True
        m = self._fts_marker()
        return m.exists() and m.read_text().strip() == f"{self._t.count_rows()}:{self._t.version}"

    # -- reading --------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._t is not None and self._t.count_rows() > 0

    def count(self) -> int:
        return self._t.count_rows() if self._t is not None else 0

    def embed_model(self) -> str | None:
        if not self.ready:
            return None
        return self._t.search().select(["embed_model"]).limit(1).to_list()[0]["embed_model"]

    def vector_search(self, vector: list[float], where, limit: int) -> list[dict]:
        """`where`: a search Filter, a LanceDB SQL string, or None."""
        where = _sql(where)
        q = self._t.search(vector, vector_column_name="vector")
        if where:
            q = q.where(where, prefilter=True)
        rows = q.limit(limit).to_list()
        for r in rows:                         # squared L2 on unit vectors is 2 - 2 cos
            if r.get("_distance") is not None:
                r["_similarity"] = 1.0 - float(r["_distance"]) / 2.0
        return rows

    def text_search(self, query: str, where, limit: int) -> list[dict]:
        where = _sql(where)
        clean = re.sub(r"[^\w\s']", " ", query).strip()
        if not clean:
            return []
        q = self._t.search(clean, query_type="fts")
        if where:
            q = q.where(where, prefilter=True)
        try:
            return q.limit(limit).to_list()
        except Exception as e:  # no full-text index yet: dense results still work, but say so once
            if not getattr(self, "_warned_fts", False):
                self._warned_fts = True
                import sys
                print(f"search: keyword index unavailable ({type(e).__name__}); run `ytbrain index` "
                      f"to rebuild it. Using vector results only.", file=sys.stderr)
            return []

    def domain_names(self) -> list[str]:
        """The Domains that have items (cached per table version)."""
        if self._t is None or not self.count():
            return []
        version = self._t.version
        if getattr(self, "_dom_cache", (None,))[0] != version:
            have = {DEFAULT_DOMAIN} if "domains" not in self._t.schema.names else set()
            if "domains" in self._t.schema.names:
                col = self._t.search().select(["domains"]).limit(self.count()).to_list()
                for r in col:
                    have.update(r.get("domains") or [DEFAULT_DOMAIN])
            self._dom_cache = (version, sorted(have))
        return list(self._dom_cache[1])

    def domain_scores(self, vector, top_n: int = 3) -> dict[str, float]:
        """Per Domain with items: the mean cosine of its `top_n` closest items (the router's signal).
        LanceDB reports squared L2, which is 2 - 2 cos on unit vectors."""
        from founder_coach.search import Filter
        out = {}
        for d in self.domain_names():
            hits = self.vector_search(vector, Filter(domains=(d,)), top_n)
            if hits:
                out[d] = sum(1 - h["_distance"] / 2 for h in hits) / len(hits)
        return out

    def get(self, item_id: str) -> dict | None:
        rows = self._t.search().where(f"item_id = {_q(item_id)}").limit(1).to_list()
        return rows[0] if rows else None

    def rows(self, kinds: list[str] | None = None, columns: list[str] | None = None) -> list[dict]:
        """Every item (optionally of some kinds), without vectors unless asked for."""
        have = set(self._t.schema.names)       # an index built before a column existed lacks it
        cols = [c for c in columns or STRING_COLS + LIST_COLS + INT_COLS if c in have]
        q = self._t.search().select(cols)
        where = where_clause(kinds=kinds)
        if where:
            q = q.where(where)
        out = q.limit(max(1, self.count())).to_list()
        for r in out:
            for c, v in COLUMN_DEFAULTS.items():
                if c not in r and (columns is None or c in columns):
                    r[c] = v
            if columns is None or "domains" in columns:
                r["domains"] = list(r.get("domains") or [DEFAULT_DOMAIN])
        return out

    def document_items(self, doc_id: str) -> list[dict]:
        return self._t.search().where(f"doc_id = {_q(doc_id)}").limit(10_000).to_list()


def _sql(where) -> str | None:
    """A search Filter (founder_coach.search.Filter) as LanceDB SQL; strings pass through."""
    if where is None or isinstance(where, str):
        return where
    return where_clause(kinds=list(where.kinds) or None, stage=where.stage,
                        topics=list(where.topics) or None, doc_id=where.doc_id,
                        domains=list(where.domains) or None)


def where_clause(kinds: list[str] | None = None, stage: str | None = None,
                 topics: list[str] | None = None, doc_id: str | None = None,
                 domains: list[str] | None = None) -> str | None:
    parts = []
    if kinds:
        parts.append("kind IN (" + ", ".join(_q(k) for k in kinds) + ")")
    if stage:
        parts.append(f"array_has(stages, {_q(stage)})")
    if topics:
        parts.append("(" + " OR ".join(f"array_has(topics, {_q(t)})" for t in topics) + ")")
    if doc_id:
        parts.append(f"doc_id = {_q(doc_id)}")
    if domains:
        any_of = [f"array_has(domains, {_q(d)})" for d in domains]
        if DEFAULT_DOMAIN in domains:            # items indexed before Domains existed are startup until retagged
            any_of.append("domains IS NULL")
        parts.append("(" + " OR ".join(any_of) + ")")
    return " AND ".join(parts) or None
