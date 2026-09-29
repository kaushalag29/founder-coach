"""The thin graph (research doc 6).

Deliberately NOT GraphRAG. On GraphRAG-Bench a single global-search query costs
~331k tokens against Microsoft GraphRAG and ~100k against LightRAG, versus ~900
for plain RAG, for gains concentrated in multi-hop sensemaking - not the
single-hop questions founders mostly ask. So this builds three tiers:

  Tier 1  free structural edges, projected mechanically from metadata you have
          already extracted. Zero extra LLM calls. This is the gbrain insight.
  Tier 2  an ontology-constrained LLM pass over SIX edge types only. Closed
          schema, bounded cost, high precision - not open information extraction.
  Tier 3  bi-temporal validity so superseded advice is invalidated, never
          deleted. This is the one genuinely graph-shaped problem in this corpus.

Storage is DuckDB edge tables with recursive CTEs, because Kuzu - the previous
design's embedded pick - was archived by its owner on 10 Oct 2025.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# ---- ontology -------------------------------------------------------------

NODE_TYPES = ("Document", "Segment", "Chapter", "AdviceAtom", "Claim", "Concept",
              "Framework", "Speaker", "Example", "Stage", "Category", "Series")

TIER1_EDGES = (           # free: mechanical projection of extraction output
    "part_of", "discusses", "has_atom", "applies_to_stage",
    "in_category", "in_series", "mentions",
)
TIER2_EDGES = (           # the ONLY relations worth an LLM call
    "prerequisite_of", "made_by", "supported_by",
    "contradicts", "refines", "relevant_to_function",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  node_id    TEXT PRIMARY KEY,
  node_type  TEXT NOT NULL,
  label      TEXT NOT NULL,
  doc_id     TEXT,
  start_ms   INTEGER,
  props      TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS edges (
  edge_id     TEXT PRIMARY KEY,
  src         TEXT NOT NULL,
  dst         TEXT NOT NULL,
  relation    TEXT NOT NULL,
  tier        INTEGER NOT NULL,
  confidence  REAL DEFAULT 1.0,
  source_doc  TEXT,
  source_ms   INTEGER,
  -- bi-temporal: when the fact held, vs. when we learned it (research doc 6.1)
  valid_from  TEXT,
  invalid_at  TEXT,
  ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS entity_aliases (
  alias     TEXT PRIMARY KEY,
  node_id   TEXT NOT NULL,
  method    TEXT,
  merged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, relation);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, relation);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
"""


@dataclass
class Edge:
    src: str
    dst: str
    relation: str
    tier: int = 1
    confidence: float = 1.0
    source_doc: str | None = None
    source_ms: int | None = None
    valid_from: str | None = None


class Graph:
    """SQLite here so the scaffold runs with zero installs; the DDL is plain
    enough to point at DuckDB (`ytbrain[graph]`) unchanged when you want
    columnar analytics and recursive-CTE traversal at speed."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # ---- writes ----

    def add_node(self, node_id: str, node_type: str, label: str, **props) -> str:
        self.db.execute(
            "INSERT OR REPLACE INTO nodes (node_id,node_type,label,doc_id,start_ms,props,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (node_id, node_type, label, props.get("doc_id"), props.get("start_ms"),
             str(props), _now()),
        )
        return node_id

    def add_edge(self, e: Edge) -> None:
        eid = f"{e.src}|{e.relation}|{e.dst}"
        self.db.execute(
            "INSERT OR REPLACE INTO edges (edge_id,src,dst,relation,tier,confidence,"
            "source_doc,source_ms,valid_from,invalid_at,ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,NULL,?)",
            (eid, e.src, e.dst, e.relation, e.tier, e.confidence,
             e.source_doc, e.source_ms, e.valid_from, _now()),
        )

    def invalidate(self, src: str, relation: str, dst: str, when: str | None = None) -> None:
        """Supersede rather than delete: the old claim keeps its citation and the
        agent can still answer 'has this advice changed?'."""
        self.db.execute(
            "UPDATE edges SET invalid_at=? WHERE src=? AND relation=? AND dst=? AND invalid_at IS NULL",
            (when or _now(), src, relation, dst),
        )
        self.db.commit()

    def purge_document(self, doc_id: str) -> int:
        """Called when a video is tombstoned upstream."""
        cur = self.db.execute("DELETE FROM edges WHERE source_doc=?", (doc_id,))
        self.db.execute("DELETE FROM nodes WHERE doc_id=?", (doc_id,))
        self.db.commit()
        return cur.rowcount

    # ---- tier 1: free structural projection ----

    def project_metadata(self, record: dict) -> int:
        """Turn one extraction record into nodes+edges with no LLM calls."""
        doc_id = record["doc_id"]
        n = 0
        self.add_node(doc_id, "Document", record.get("title_raw", doc_id), doc_id=doc_id)

        if record.get("series"):
            sid = f"series:{record['series']}"
            self.add_node(sid, "Series", record["series"])
            self.add_edge(Edge(doc_id, sid, "in_series", 1, source_doc=doc_id)); n += 1

        if record.get("category"):
            cid = f"category:{record['category']}"
            self.add_node(cid, "Category", str(record["category"]))
            self.add_edge(Edge(doc_id, cid, "in_category", 1, source_doc=doc_id)); n += 1

        for stage in record.get("stage_relevance") or []:
            sid = f"stage:{stage}"
            self.add_node(sid, "Stage", str(stage))
            self.add_edge(Edge(doc_id, sid, "applies_to_stage", 1, source_doc=doc_id)); n += 1

        for atom in record.get("advice_atoms") or []:
            aid = atom.get("atom_id") or f"atom:{doc_id}:{atom.get('timestamp_ms', 0)}"
            self.add_node(aid, "AdviceAtom", atom.get("text", "")[:200],
                          doc_id=doc_id, start_ms=atom.get("timestamp_ms"))
            self.add_edge(Edge(doc_id, aid, "has_atom", 1, atom.get("match_score") or 1.0,
                               doc_id, atom.get("timestamp_ms"))); n += 1
            for stage in atom.get("applies_to_stage") or []:
                sid = f"stage:{stage}"
                self.add_node(sid, "Stage", str(stage))
                self.add_edge(Edge(aid, sid, "applies_to_stage", 1, source_doc=doc_id)); n += 1

        ents = record.get("entities") or {}
        for kind, node_type in (("people", "Speaker"), ("companies", "Document"),
                                ("frameworks", "Framework"), ("yc_jargon", "Concept")):
            for name in ents.get(kind) or []:
                nid = self.resolve_entity(name, node_type)
                self.add_edge(Edge(doc_id, nid, "mentions", 1, source_doc=doc_id)); n += 1

        self.db.commit()
        return n

    # ---- entity resolution (research doc 6.3) ----

    def resolve_entity(self, name: str, node_type: str) -> str:
        """Alias-first resolution. This is the quality ceiling on the whole graph:
        published analysis shows resolution accuracy 95% -> 85% collapsing 5-hop
        answer accuracy 77% -> 44%, because errors compound per hop. Every merge
        is logged so it stays reversible; embedding-threshold and LLM-adjudicated
        tiers slot in below the exact-match tier when you need them."""
        key = _norm(name)
        row = self.db.execute("SELECT node_id FROM entity_aliases WHERE alias=?", (key,)).fetchone()
        if row:
            return row["node_id"]
        nid = f"{node_type.lower()}:{key}"
        self.add_node(nid, node_type, name)
        self.db.execute(
            "INSERT OR REPLACE INTO entity_aliases (alias,node_id,method,merged_at) VALUES (?,?,?,?)",
            (key, nid, "exact", _now()))
        return nid

    # ---- reads: parameterized traversals, no text-to-Cypher ----

    def neighbors(self, node_id: str, relation: str | None = None, depth: int = 1,
                  as_of: str | None = None) -> list[dict]:
        """Bounded BFS. Deliberately parameterized rather than generated:
        Neo4j's own 2026 analyses still document text-to-Cypher unreliability,
        and an agent that must cite cannot afford a malformed traversal."""
        seen, frontier, out = {node_id}, [node_id], []
        for _ in range(max(depth, 1)):
            if not frontier:
                break
            marks = ",".join("?" for _ in frontier)
            q = (f"SELECT e.*, n.label, n.node_type, n.doc_id, n.start_ms FROM edges e"
                 f" JOIN nodes n ON n.node_id = e.dst WHERE e.src IN ({marks})")
            args = list(frontier)
            if relation:
                q += " AND e.relation=?"; args.append(relation)
            q += " AND (e.invalid_at IS NULL" + (" OR e.invalid_at > ?)" if as_of else ")")
            if as_of:
                args.append(as_of)
            rows = [dict(r) for r in self.db.execute(q, args)]
            out += rows
            frontier = [r["dst"] for r in rows if r["dst"] not in seen]
            seen.update(frontier)
        return out

    def contradictions(self, node_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM edges WHERE relation='contradicts' AND (src=? OR dst=?)",
            (node_id, node_id))]

    def advice_for(self, stage: str, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT n.node_id, n.label, n.doc_id, n.start_ms FROM edges e"
            " JOIN nodes n ON n.node_id = e.src"
            " WHERE e.relation='applies_to_stage' AND e.dst=? AND n.node_type='AdviceAtom'"
            " AND e.invalid_at IS NULL LIMIT ?", (f"stage:{stage}", limit))]

    def stats(self) -> dict:
        nodes = {r["node_type"]: r["c"] for r in self.db.execute(
            "SELECT node_type, COUNT(*) c FROM nodes GROUP BY node_type")}
        edges = {r["relation"]: r["c"] for r in self.db.execute(
            "SELECT relation, COUNT(*) c FROM edges GROUP BY relation")}
        invalid = self.db.execute(
            "SELECT COUNT(*) c FROM edges WHERE invalid_at IS NOT NULL").fetchone()["c"]
        return {"nodes": nodes, "edges": edges, "superseded_edges": invalid}

    def to_networkx(self):
        """Hand the edge table to NetworkX for Leiden communities / PageRank
        (research doc 6.2). The graph is small; loading it in memory is fine."""
        import networkx as nx
        g = nx.DiGraph()
        for r in self.db.execute("SELECT node_id,node_type,label FROM nodes"):
            g.add_node(r["node_id"], node_type=r["node_type"], label=r["label"])
        for r in self.db.execute("SELECT src,dst,relation FROM edges WHERE invalid_at IS NULL"):
            g.add_edge(r["src"], r["dst"], relation=r["relation"])
        return g


def _norm(s: str) -> str:
    return " ".join(s.lower().replace(".", "").split())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
