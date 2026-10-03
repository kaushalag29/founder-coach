"""Chunking + contextual headers + hybrid index.

Chunking rules (research doc 7.1), and why:
  * split on utterance boundaries so a chunk never straddles a timestamp it
    cannot cite;
  * 400-700 tokens (~45-90s of speech), 18% overlap on the dense side only -
    overlap buys nothing for BM25;
  * small-to-big: retrieve on the chunk, expand to a parent window for synthesis;
  * prepend a Contextual Retrieval header before embedding AND before BM25
    indexing - Anthropic measured 5.7% -> 1.9% failure@20 for contextual
    embeddings + contextual BM25 + reranking, the single largest available win;
  * fixed-size beats semantic chunking in the published sweeps, so the default
    here is deliberately simple.

The chunker itself is stdlib and unit-tested; LanceDB is an optional extra.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict

from .config import (CHUNK_OVERLAP_RATIO, CHUNK_TARGET_TOKENS, EMBED_MAX_SEQ, PARENT_TARGET_TOKENS,
                     TOKENS_PER_WORD)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    parent_chunk_id: str | None
    text: str
    context_header: str
    start_ms: int
    end_ms: int
    n_tokens: int
    part: int = 0              # > 0: a piece of one unit too long for the embedding window

    def indexable(self) -> str:
        """What actually gets embedded and BM25-indexed: header + text."""
        return f"{self.context_header}\n\n{self.text}" if self.context_header else self.text

    def deep_link(self) -> str:
        return f"https://www.youtube.com/watch?v={self.doc_id}&t={self.start_ms // 1000}s"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["deep_link"] = self.deep_link()
        return d


def est_tokens(text: str) -> int:
    return int(len(text.split()) * TOKENS_PER_WORD)


# A unit (an utterance, an article or book paragraph) longer than this is cut into the fewest
# equal pieces that fit the embedding window with their context header: 1.6 tokens per word is a
# pessimistic bound for English, 60 words leave room for the header. Never smaller than needed:
# Passages stay ~CHUNK_TARGET_TOKENS, and only a unit the embedder would truncate is cut.
MAX_UNIT_WORDS = int(EMBED_MAX_SEQ / 1.6) - 60
SPLIT_VERSION = "units1"            # in the index input hash of a Document that has such a unit


def split_long_units(utts: list[dict], max_words: int = MAX_UNIT_WORDS) -> list[dict]:
    """Each unit longer than `max_words` becomes ceil(words / max_words) near-equal pieces with
    the same position (so Citations and eval Moments are unchanged) and `part` 1, 2, ..."""
    out = []
    for u in utts:
        words = (u.get("text") or "").split()
        if len(words) <= max_words:
            out.append(u)
            continue
        n = -(-len(words) // max_words)
        size = -(-len(words) // n)
        out += [{**u, "text": " ".join(words[i:i + size]), "part": k}
                for k, i in enumerate(range(0, len(words), size), 1)]
    return out


def has_long_units(utts: list[dict], max_words: int = MAX_UNIT_WORDS) -> bool:
    return any(len((u.get("text") or "").split()) > max_words for u in utts)


def chunked_differently(utts: list[dict], target_tokens: int = CHUNK_TARGET_TOKENS,
                        overlap_ratio: float = CHUNK_OVERLAP_RATIO) -> bool:
    """Whether SPLIT_VERSION changed this Document's Passages: it has a unit long enough to be
    cut, or to close a short chunk whose whole text used to be carried into the next one."""
    big = target_tokens * (1 - overlap_ratio)
    return any(est_tokens(u.get("text") or "") > big for u in utts)


def chunk_transcript(transcript: dict,
                     target_tokens: int = CHUNK_TARGET_TOKENS,
                     overlap_ratio: float = CHUNK_OVERLAP_RATIO) -> list[Chunk]:
    doc_id = transcript["doc_id"]
    utts = split_long_units(transcript["utterances"])
    chunks: list[Chunk] = []
    cur: list[dict] = []
    cur_tokens = 0
    carried = 0                    # leading units of `cur` that are overlap from the last chunk

    def make(units: list[dict]) -> Chunk:
        text = " ".join(u["text"] for u in units)
        part = int(units[0].get("part") or 0)
        return Chunk(chunk_id=_cid(doc_id, units[0]["start_ms"]) + (f"_{part}" if part else ""),
                     doc_id=doc_id, parent_chunk_id=None, text=text, context_header="",
                     start_ms=units[0]["start_ms"], end_ms=units[-1]["end_ms"], n_tokens=est_tokens(text),
                     part=part)

    def flush():
        nonlocal cur, cur_tokens, carried
        if len(cur) <= carried:    # only the previous chunk's overlap: nothing new to emit
            return
        chunks.append(make(cur))
        # carry the tail forward as overlap (dense side only)
        keep, kept = [], 0
        budget = target_tokens * overlap_ratio
        for u in reversed(cur):
            t = est_tokens(u["text"])
            if kept + t > budget:
                break
            keep.insert(0, u)
            kept += t
        if len(keep) == len(cur):  # a whole short chunk as overlap would start the next one at the
            keep, kept = [], 0     # same unit: two Passages with one id
        cur, cur_tokens, carried = keep, kept, len(keep)

    for u in utts:
        t = est_tokens(u["text"])
        if cur_tokens + t > target_tokens and cur:
            flush()
        cur.append(u)
        cur_tokens += t
    if len(cur) > carried:
        chunks.append(make(cur))

    _attach_parents(chunks, doc_id)
    return chunks


def _attach_parents(chunks: list[Chunk], doc_id: str) -> None:
    """Group consecutive chunks into ~2000-token parent windows for small-to-big
    retrieval; the parent id is resolved at query time, not duplicated in storage."""
    acc, pid = 0, None
    for c in chunks:
        if pid is None or acc + c.n_tokens > PARENT_TARGET_TOKENS:
            pid, acc = _cid(doc_id, c.start_ms, prefix="p"), 0
        c.parent_chunk_id = pid
        acc += c.n_tokens


def build_context_header(chunk: Chunk, video_meta: dict) -> str:
    """Deterministic fallback header.

    The measured Contextual Retrieval win comes from an LLM writing a bespoke
    situating sentence per chunk. That is one cheap call per chunk (use prompt
    caching over the whole transcript). This function is the zero-cost version
    so the pipeline runs end-to-end before you turn that on - and so you can A/B
    the two against the eval harness rather than assuming.
    """
    from .locators import label_for
    bits = [video_meta.get("title_raw") or video_meta.get("title", "")]
    if video_meta.get("speaker"):
        bits.append(f"by {video_meta['speaker']}")
    if video_meta.get("series"):
        bits.append(f"({video_meta['series']})")
    ch = _chapter_at(video_meta.get("chapters") or [], chunk.start_ms)
    if ch:
        bits.append(f"- section: {ch}")
    return f"From {' '.join(b for b in bits if b)} at {label_for(video_meta, chunk.start_ms)}."


def _chapter_at(chapters: list[dict], ms: int) -> str | None:
    best = None
    for c in chapters:
        if c.get("start_ms", 0) <= ms:
            best = c.get("title")
    return best


def _cid(doc_id: str, start_ms: int, prefix: str = "c") -> str:
    return f"{prefix}_{doc_id}_{start_ms}"


# --------------------------------------------------------------------------
# Optional LanceDB sink (ytbrain[index])
# --------------------------------------------------------------------------

def upsert_lancedb(chunks: list[Chunk], video_meta: dict, embed_fn, table_name: str = "chunks"):
    """Hybrid store: vectors + native BM25 FTS in one embedded database.

    Keep the writer single-threaded - LanceDB's concurrent deletes are not
    safely composable under load, which a cron-driven pipeline satisfies anyway.
    RRF fusion is implemented in search.py rather than assumed from the store.
    """
    import lancedb
    from .config import LANCE_DIR

    db = lancedb.connect(str(LANCE_DIR))
    rows = []
    for c in chunks:
        c.context_header = c.context_header or build_context_header(c, video_meta)
        rows.append({
            **c.to_dict(),
            "vector": embed_fn(c.indexable()),
            "indexable": c.indexable(),
            "title": video_meta.get("title_raw", ""),
            "series": video_meta.get("series"),
            "speaker": video_meta.get("speaker"),
            "category": str(video_meta.get("category", "")),
            "stage": ",".join(str(s) for s in video_meta.get("stage_relevance", [])),
            "published_at": video_meta.get("published_at"),
        })

    if table_name in db.table_names():
        tbl = db.open_table(table_name)
        tbl.delete(f"doc_id = '{chunks[0].doc_id}'")   # replace, never duplicate
        tbl.add(rows)
    else:
        tbl = db.create_table(table_name, rows)
        tbl.create_fts_index("indexable")
    return tbl


def rrf_fuse(dense: list[str], sparse: list[str], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion over two ranked id lists."""
    scores: dict[str, float] = {}
    for ranking in (dense, sparse):
        for rank, cid in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]
