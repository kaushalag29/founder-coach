# Indexing Books: research notes (2026-09-29)

Question: how to add PDF Books (one Chapter = one Document) to the index and pack while keeping
retrieval quality and letting the corpus grow to hundreds of Books. Decisions live in the grilling
notes and, once settled, in an ADR; this file keeps the evidence.

## What the index already does (ytbrain/index.py, founder_coach/search.py)

- Passages: ~550 tokens (400-700), split on text-unit boundaries, 18 % overlap on the dense side only;
  ~2000-token parent windows for small-to-big.
- A fixed contextual header before embedding and BM25: "From <title> by <speaker> (<series>) -
  section: <section> at mm:ss". For Articles the position is a paragraph number, so the header
  reads "at 00:00"; it should use `locators.label` (and "p. 47" for Chapters).
- Hybrid search: BM25 (LanceDB FTS index) + dense (bge-m3), RRF fusion, bge-reranker-v2-m3, soft
  boosts (Stage, kind, recency), then at most 3 results per Document. No cap per Series/Book.
- No approximate vector index: LanceDB searches all rows (fine at 34k rows).
- No near-duplicate merging of Advice.

## Findings

| Topic | Evidence | For Books |
|---|---|---|
| Chunking | Paragraph-group chunking beat fixed-character chunking ~0.46 vs <0.25 nDCG@5 (arXiv 2603.06976). Fixed-size is competitive at a fraction of the cost; LLM chunking has poor ROI (arXiv 2606.00881). Chroma: ~200-token chunks without overlap did well; 800/400 did worst. | Reuse the current chunker on paragraph units, never crossing a Section. Smaller chunks (~300, no overlap) are an eval experiment, not a Book-only change. |
| Contextual headers | Anthropic: contextual embeddings -35 % failures, +BM25 -49 %, +rerank -67 %; LLM-written context ~$1 per million document tokens. | Already have the free version; give it Book, author, Chapter, Section and page. LLM-written context stays an experiment. |
| Hierarchy | RAPTOR: multi-level summary tree, +20 points absolute on QuALITY with GPT-4. | Books have the tree already: Book summary from Chapter summaries, plus Section summaries as items. |
| Late chunking | Gains grow with length (e.g. NFCorpus 23.5 -> 30.0 nDCG@10); needs a long-context embedder. | Possible with bge-m3 in the full index, not the pack model. Later. |
| Scale | LanceDB: flat search to ~100k rows, then IVF_PQ / IVF_HNSW_SQ + FTS + scalar indexes (BTree/Bitmap on source_kind, series, stage). | ~600 items per Book: 6 Books +11 %, 1,000 Books ~600k rows. The pack is the limit (768 float32 x 600k ~1.8 GB vs GitHub's 100 MB): int8 embeddings or per-topic packs when it matters. |
| Metadata | Open Library API: free ISBN -> title, authors, publish date, publisher. | Optional fill of missing given fields; never overwrite sources.yaml. |
| PDF text | Ligatures, curly quotes, soft hyphens, line-end hyphenation, footnote markers, epigraphs. | Normalise in `clean` so Evidence quotes verify; drop epigraphs (not the author's advice). |

## Sources

- https://arxiv.org/html/2603.06976 (chunking strategies and embedding sensitivity)
- https://arxiv.org/html/2606.00881v1 (chunking effectiveness vs cost)
- https://www.trychroma.com/research/evaluating-chunking
- https://www.anthropic.com/engineering/contextual-retrieval
- https://arxiv.org/abs/2401.18059 (RAPTOR)
- https://jina.ai/news/late-chunking-in-long-context-embedding-models/
- https://docs.lancedb.com/indexing
- https://openlibrary.org/developers/api
- PDF parsers: https://www.marktechpost.com/2026/07/24/datalab-marker-v2-vs-mineru-docling-and-liteparse-benchmark-breakdown/ ,
  https://docling-project.github.io/docling/usage/heading_levels/ , https://github.com/docling-project/docling/issues/4174
