---
status: accepted
---
# Own knowledge layer: hybrid search plus a thin graph projected from records, not a GraphRAG framework

Every Document already arrives as a structured record — Advice, Takeaways, Stage, Topic,
people, companies, Locators — so most of what GraphRAG frameworks (Microsoft GraphRAG,
LightRAG, Cognee) spend extra LLM calls building already exists. We index Knowledge items
with hybrid search (vector + full-text, fused, optionally reranked) and project a thin graph
from record fields with zero LLM calls. Recent benchmarks show agentic search over dense
retrieval closes most of the gap to GraphRAG except on multi-hop questions, and the corpus
is small (~700 Talks, ~5M tokens).

## Considered Options

- **gbrain** as the corpus store: single-operator, first-class only with OpenClaw/Hermes, no
  multi-hop or temporal retrieval, frequent breaking changes (reviewed Sep 2026). Still a
  candidate for a Founder's *personal* memory later — not for the corpus.
- **LLM-extracted graph (tier-2 edges: contradicts, refines, prerequisite_of)**: deferred to
  the Principle consolidation Step, and adopted only if the eval set shows a gain.

## Consequences

The graph is only as rich as the record schema; new relationship types mean new record
fields or a consolidation Step, not a framework swap.
