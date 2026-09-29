"""MCP server exposing the corpus to a founder-coach agent.

Spec note (research doc 7.3): the current MCP specification is 2026-07-28 and
the changes are structural, not cosmetic - MCP is now stateless (no
initialize/initialized handshake, no Mcp-Session-Id), `server/discover` is
required, all results carry `resultType`, list results require `ttlMs` and
`cacheScope`, and Roots/Sampling/Logging are deprecated. Build against the
official Python SDK >= 2.2.0 and let it handle negotiation rather than hand-
rolling transport.

Six tools, in deterministic order (the spec now asks for this so clients can
cache the tool list and hit the model's prompt cache). No text-to-Cypher tool:
generated queries are still unreliable, and an agent that must cite cannot
afford a malformed traversal.
"""
from __future__ import annotations

import json
from typing import Any

from .config import GRAPH_DB, METADATA
from .graph import Graph

TOOL_ORDER = ["search_corpus", "get_document", "get_chunk",
              "get_advice", "graph_neighbors", "list_sources"]


# --------------------------------------------------------------------------
# Tool implementations (transport-independent so they are directly testable)
# --------------------------------------------------------------------------

def search_corpus(query: str, filters: dict | None = None, top_k: int = 8,
                  mode: str = "hybrid") -> dict:
    """Hybrid dense+BM25 retrieval with RRF fusion and reranking.

    Every hit carries the citation payload the agent needs: video id, title,
    speaker, start_ms and a deep link that drops the founder at the exact moment
    in YC's own player. Returns structured JSON, not a markdown blob - the spec
    now allows full JSON Schema on outputs, so structure survives to the client.
    """
    raise NotImplementedError(
        "Wire to index.upsert_lancedb's table: dense search + fts search -> "
        "index.rrf_fuse -> cross-encoder rerank -> parent expansion."
    )


def get_document(doc_id: str) -> dict:
    """Video-level record: metadata, chapters, summary, atom ids."""
    path = METADATA / f"{doc_id}.json"
    if not path.exists():
        return {"error": "not_found", "doc_id": doc_id}
    rec = json.loads(path.read_text())
    return {
        "doc_id": doc_id,
        "title": rec.get("title_raw"),
        "url": rec.get("url"),
        "series": rec.get("series"),
        "provenance": rec.get("provenance"),
        "speaker": rec.get("speaker"),
        "speaker_confidence": rec.get("speaker_confidence"),
        "category": rec.get("category"),
        "stage_relevance": rec.get("stage_relevance"),
        "summary": rec.get("summary"),
        "chapters": rec.get("chapters"),
        "validation_status": (rec.get("extraction_meta") or {}).get("validation_status"),
        "gaps": rec.get("unknowns_and_gaps"),
    }


def get_chunk(chunk_id: str, expand_to_parent: bool = False) -> dict:
    raise NotImplementedError("Read from the LanceDB chunks table by chunk_id.")


def get_advice(stage: str, category: str | None = None, limit: int = 20) -> dict:
    """Stage-filtered advice atoms - the coach's primary query shape.

    This is the call that makes the corpus useful day to day: 'I'm pre-PMF, what
    should I be doing?' resolves to a graph filter, not a similarity search.
    """
    g = Graph(GRAPH_DB)
    atoms = g.advice_for(stage, limit=limit)
    return {
        "stage": stage,
        "count": len(atoms),
        "atoms": [{
            "atom_id": a["node_id"],
            "text": a["label"],
            "doc_id": a["doc_id"],
            "start_ms": a["start_ms"],
            "deep_link": f"https://www.youtube.com/watch?v={a['doc_id']}&t={(a['start_ms'] or 0)//1000}s",
        } for a in atoms],
    }


def graph_neighbors(node_id: str, relation: str | None = None, depth: int = 1,
                    as_of: str | None = None) -> dict:
    """Prerequisite chains, contradictions, shared-entity links.

    `as_of` honours the bi-temporal model: ask what the corpus considered true
    at a point in time, which is how 'has YC's advice on this changed?' gets a
    real answer instead of two undated chunks.
    """
    g = Graph(GRAPH_DB)
    rows = g.neighbors(node_id, relation=relation, depth=depth, as_of=as_of)
    return {"node_id": node_id, "relation": relation, "depth": depth, "count": len(rows),
            "neighbors": rows}


def list_sources() -> dict:
    """Registry, counts, last sync, and - importantly - known coverage gaps, so
    the agent can say what the brain does NOT know rather than confabulating."""
    from .config import MANIFEST_DB
    from .manifest import Manifest
    m = Manifest(MANIFEST_DB)
    return {"stats": m.stats(), "note": "coverage gaps are reported per source"}


TOOLS: dict[str, Any] = {
    "search_corpus": search_corpus,
    "get_document": get_document,
    "get_chunk": get_chunk,
    "get_advice": get_advice,
    "graph_neighbors": graph_neighbors,
    "list_sources": list_sources,
}


def serve() -> None:
    """Register the tools with the official SDK.

    Kept thin on purpose: SDK 2.2.0 implements 2026-07-28 plus every earlier
    revision, so version negotiation, `server/discover` and `resultType` are its
    job, not ours.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'ytbrain[serve]' (mcp>=2.2.0)") from e

    app = FastMCP("ytbrain")
    for name in TOOL_ORDER:          # deterministic order -> better prompt-cache hits
        app.tool(name=name)(TOOLS[name])
    app.run()


if __name__ == "__main__":
    serve()
