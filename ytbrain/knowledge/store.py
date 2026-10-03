"""The Knowledge index on disk: one LanceDB table of Knowledge items [ADR-0003].

A Document's items are replaced as a unit (delete + add). The index Step marks
the Document done only afterwards, so an interruption between the two simply
redoes that Document. LanceDB versions every write, so readers keep seeing the
last complete version while a rebuild runs.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import KNOWLEDGE_TABLE, LANCE_DIR

STRING_COLS = ("item_id", "kind", "doc_id", "text", "evidence", "deep_link", "source_kind",
               "series", "title", "speaker", "provenance", "published_at", "stage_origin",
               "context_header", "indexable", "embed_model", "visibility")
LIST_COLS = ("stages", "topics")
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

    def replace_document(self, doc_id: str, rows: list[dict], dim: int) -> None:
        t = self._table(dim)
        t.delete(f"doc_id = {_q(doc_id)}")
        if rows:
            t.add(rows)

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
        return q.limit(limit).to_list()

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
        return out

    def document_items(self, doc_id: str) -> list[dict]:
        return self._t.search().where(f"doc_id = {_q(doc_id)}").limit(10_000).to_list()


def _sql(where) -> str | None:
    """A search Filter (founder_coach.search.Filter) as LanceDB SQL; strings pass through."""
    if where is None or isinstance(where, str):
        return where
    return where_clause(kinds=list(where.kinds) or None, stage=where.stage,
                        topics=list(where.topics) or None, doc_id=where.doc_id)


def where_clause(kinds: list[str] | None = None, stage: str | None = None,
                 topics: list[str] | None = None, doc_id: str | None = None) -> str | None:
    parts = []
    if kinds:
        parts.append("kind IN (" + ", ".join(_q(k) for k in kinds) + ")")
    if stage:
        parts.append(f"array_has(stages, {_q(stage)})")
    if topics:
        parts.append("(" + " OR ".join(f"array_has(topics, {_q(t)})" for t in topics) + ")")
    if doc_id:
        parts.append(f"doc_id = {_q(doc_id)}")
    return " AND ".join(parts) or None
