---
status: accepted
---
# One source-neutral index of Knowledge items, addressed by Locator

Advice, Takeaways, Passages and Document summaries live in one index, each with a
`kind`, a Source kind and a Locator (timestamp for Talks, paragraph/page for text), rather
than one table per content type or per Source. New Sources (essays, podcasts, books, the
Founder's own notes) then only need an adapter that produces Documents, Passages and
Locators; retrieval, graph, MCP tools and Playbooks stay unchanged.

## Consequences

YouTube-specific fields (caption kind, chapters) move to Document metadata; anything the
coach relies on must be expressible for every Source kind.
