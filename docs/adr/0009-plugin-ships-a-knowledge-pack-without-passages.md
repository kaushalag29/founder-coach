---
status: accepted
---
# The Claude plugin ships a prebuilt Knowledge pack without Passages, with query models chosen by eval

A founder installing the coach as a Claude plugin must not have to run the pipeline, hold an LLM
key or wait for a 4.6 GB model download. The plugin's MCP server therefore downloads, on first use,
a versioned **Knowledge pack** (a GitHub release asset): Advice, Takeaways and Document summaries
with their Citations, embeddings and full-text index, built by the maintainer. It contains no
Passages. Passages are cleaned transcript text, and the captions are the uploader's content, so
they are never redistributed; anyone who runs the pipeline locally gets them in their own index.
The pack is built with the query-time models the plugin will use, and those models are chosen on the
Tuning set: the full config (bge-m3 + bge-reranker-v2-m3, ~4.6 GB) is compared with at least one
lite config (< ~500 MB). The plugin ships the lite config unless it loses more than 0.03 nDCG@10.

## Considered Options

- **Build locally on install:** needs yt-dlp access, hours of LLM extraction and an API key per
  user. Wrong for a one-step install.
- **Ship the full local index, Passages included:** redistributes transcript text.
- **Hosted search API:** needs a server, accounts and ongoing cost. This is the "Commercial
  Service" our licence reserves, and it is out of scope for v1.

## Consequences

Without Passages, the plugin's recall falls back on Advice, Takeaways and summaries. The Tuning set
measures that loss too (full index vs pack). Short Evidence quotes, each with attribution and a
deep link, ship in the pack; the maintainer confirms that before the first public release. Pack
versions are tied to `SCHEMA_VERSION`, `ITEMS_VERSION` and the embedding model, and the server
refuses a pack it can't read rather than mixing versions.

## Amendment 2026-09-28

- In the private beta the pack ships inside the plugin folder (`--pack ${CLAUDE_PLUGIN_ROOT}/pack`);
  there is no download.
- `ytbrain pack build --with-passages` may ship Passages to the private beta only, never a public
  pack.
- The pack has no reranker by default: on labels v1.2.0, jina-reranker-v1-turbo lowered its
  nDCG@10 (0.457 vs 0.479) and slowed every search.
- On labels v1.3.0 (articles included) the pack scores 0.471 nDCG@10 against 0.568 for `full`, so
  it fails the 0.03 gate. It reaches only 74.2 % of the relevant Moments. Experiment A (+Passages)
  and B (another embedding model) decide it (docs/m3-status.md).
