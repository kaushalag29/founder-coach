---
status: accepted
---
# One engine, many Packs over one shared Library

Supersedes ADR-0012's "vertical founder coach, not a general tool" once accepted; its product-id
mechanism stays and moves into each Pack's manifest.

The engine (pipeline, index, runtime) becomes the product: each agent is a **Pack**, a configuration
of Domains, persona, facets, skills, store modules, risk tier and evals. Founder Coach becomes the Pack
`founder`. All Packs read one shared **Library**: every Source is synced, extracted, verified and
indexed once and tagged with Domains; a Pack selects Domains. One plugin and one MCP server host every
installed Pack. Extraction is Pack-neutral; Pack-specific facets (such as Stage) are a separate,
re-runnable tagging Step. The runtime stays LLM-free and reports evidence signals (routed Domains,
calibrated relevance, coverage) so the host can answer, state a Gap, search the web or decline.

## Considered Options

- **Keep the vertical coach and fork per domain:** every fork re-implements ingestion, verification
  and evals, and pays extraction again for shared books.
- **A corpus and pipeline per Pack:** simple isolation, but the same book is extracted once per Pack and
  the copies drift.
- **One plugin per Pack:** N MCP servers, N sets of tool descriptions in every conversation, and no
  single search across Packs.
- **Pack-specific extraction schemas:** sharper fields, but a Source shared by two Packs would need two
  extractions; facets tagged after extraction give the same fields without that.
- **Build on GBrain:** strong memory and graph, but none of our ingestion, quote verification or eval
  gating, and a different stack; its pack manifest ideas are borrowed instead.

## Consequences

ADR-0012's id mechanism moves into `packs/<id>/pack.yaml`. The Founder store splits into an engine core
(profile facts, Decisions, Feedback, usage) and an optional accountability module (Goals,
Commitments, Check-ins), a forward-only migration with a backup. MCP tool names become engine-neutral,
and the founder Pack must reproduce today's results on the 263 Tuning questions before it replaces the
current plugin. Catalog cards are generated, not Verified, and are never cited (ADR-0004 holds).

## Amendments 2026-10-05

- **A plugin per Pack**, not one plugin for all: [ADR-0016](0016-a-plugin-per-pack-with-projects-and-a-common-profile.md),
  which also isolates memory per Project and adds a shared Common profile.
- **Domains are inferred from content** against a controlled Domain list, configuration is a hint:
  [ADR-0017](0017-domains-are-inferred-against-a-controlled-domain-list.md).
- **Schema 3.0 lands without a full re-extract**: [ADR-0018](0018-extraction-is-versioned-per-prompt-and-old-records-stay-compatible.md).
- The parity bar is today's Tuning set (dev, dev-articles, dev-private), no longer "the 263 questions".
