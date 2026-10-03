# Library and Packs: from Founder Coach to "your own expert agent from your own sources"

Status: **proposed** (2026-10-03). Decisions settled in a grilling session; research behind them in
the Project doc `claude/ytbrain-generalization-research.md`. Architecture decision: [ADR-0015](adr/0015-one-engine-many-packs-over-one-library.md).

Goal: a new Source or a new use case is configuration, not code, without losing what makes the coach
worth trusting: Verified Citations, Gaps stated instead of guesses, and eval gates before anything ships.

Positioning: "Your own expert agent, built from the sources you trust: cited, current, and honest about
what it doesn't know." Nothing retrains a model (ADR-0012 already says so; X's terms also forbid training on its data).

## 1. Decisions

| # | Decision | Why |
|---|---|---|
| 1 | One engine; each agent is a **Pack** (config). Founder Coach becomes Pack `founder`. | New use case = new Pack, not a fork |
| 2 | Kaushal dogfoods first; config stays simple enough for others. Local only, no hosted service. | A Pack must be graded by someone who can judge it |
| 3 | Pack #2 = software and system design (your coding books). Investing is Pack #3, after posts and the safety gate. | Lowest harm, gradeable by its owner |
| 4 | **Risk tier** low / medium / high per Domain; a Pack takes the highest tier among its Domains. | "Founder" + "finance" Domains make the founder Pack medium |
| 5 | No weight training. Later, at most a per-Pack embedding or reranker model, only if evals demand it. | Host model improves for free |
| 6 | Posts only via official APIs, RSS and your own exports, with your own keys and a monthly cost cap; LinkedIn by manual save. | ToS and lawsuits (research §8) |
| 7 | **Domain** = named subject area (startup, finance, leadership, system-design...). Category and Topic keep their meaning inside a Domain. | "Category" and "profile" were taken |
| 8 | Order: Library and routing (M5) -> engine and Packs (M6) -> posts (M7). | Every Pack needs routing; posts are the riskiest Source |
| 9 | Own engine; borrow GBrain's pack manifest with `extends`, per-skill routing evals, source boosts. | Our edge is ingestion, verification and eval gates |
| 10 | One shared **Library**: every Source synced, extracted, verified and indexed once, tagged with Domains. A Pack is a view over it. | Extraction is the expensive step; no drift between copies |
| 11 | One engine plugin and one MCP server host every installed Pack; skills stay per Pack. | No N servers x tool descriptions; cross-Pack questions work |
| 12 | The runtime stays LLM-free. The server reports evidence signals; the host decides. | Local, free per question, deterministic, testable |
| 13 | **Catalog cards** (Domain, Book or Series, Document, Section) are generated navigation metadata, `trust: generated`, never cited; written as OKF-style markdown (`index.md` per level). | Keeps ADR-0004; makes 5-of-500 books cheap to find |
| 14 | Routing is **route-and-backfill**, never a hard filter unless the host passes `domains`. | A misroute costs a little ranking, never recall |
| 15 | Contextual retrieval for Passages and Chapter items, kept only if the eval gate passes. | Best-evidenced cheap gain (about 2/3 fewer failed lookups) |
| 16 | New Knowledge item type **Fact**; a trade-off is Advice with a condition. | Domain-neutral without distorting statements into advice |
| 17 | Per Pack: routing questions and **Gap questions** (out of corpus) gate a release; high tier adds a safety gate. | Declining is a requirement, so it is measured |
| 18 | Targets on a 16 GB M2: 1,000 Books + 2,000 Talks/Articles (~1M items), search p95 <= 1.5 s with no GPU, incremental indexing. | Shapes the index and reranker choices below |

## 2. Design

### 2.1 Library, Domains and Packs

- `domains.yaml`: per Domain `name`, `description`, example questions (the router's utterances),
  `risk_tier`, `freshness` (`evergreen` or `half_life_days`), `web_policy` (below).
- `sources.yaml`: every Source lists `domains: [...]`. A Book folder maps to a Domain by its folder
  name (`data/books/system-design/`), a Book can override it. A Document with no Domain gets one
  suggested by a cheap LLM call on its summary, marked `suggested` until you confirm it (`ytbrain
  domains review`); routing uses suggestions, evals report them.
- `packs/<id>/pack.yaml` (pydantic-validated, `extends` supported): identity (absorbs `product.toml`),
  `domains`, persona and contract text, facets, ranking boosts, store modules, skills, evals. The
  assembler generates what is now hand-copied (the Stage enum in four places, MCP Literals, eval lists).

### 2.2 Extraction: shared core, Pack facets later

- Schema 3.0 is Pack-neutral: Advice (with `applies_when`), **Fact**, Takeaway, Summary, Sections,
  Entities (typed, replaces `yc_jargon`). Prompts are templates filled from the Source kind and Domain.
- **Facets** (Stage for founder; lifecycle and concern for system design) are a separate tagging Step per
  Pack, stored as `(item, facet, value, tagger_version)`, re-runnable without re-extracting.
- Migration: existing Talk and Article records stay (Stage becomes a founder facet). Books re-extract
  under 3.0 to gain Facts (181 Chapters, a few dollars); others re-extract only when a Pack needs it.

### 2.3 Index and Packs on disk

- One Library index (LanceDB): items gain `domains[]`, `source_id`, `facets[]` ("name=value", filtered
  with `array_has`). A second table holds Catalog cards with embeddings for the router.
- `pack build --pack <id>` exports items whose Domains intersect the Pack's (Visibility rules as today)
  plus their cards. Brute-force vectors up to ~200k; above that an HNSW index file (usearch or
  sqlite-vec, chosen by a benchmark) ships in the pack. The runtime loads several packs and dedupes by item id.

### 2.4 Search: route, retrieve, rerank, report

1. **Route** (no LLM): embed the question, score Domain and Book cards (vector + BM25), return top-3
   Domains with confidences; inside a Domain with more than 50 Books, also pick the top 10 Books.
   The host may pass `domains` / `books` instead (hard filter).
2. **Retrieve**: top 40 within the routed set + top 40 from the whole Library, RRF-merged with a routed
   boost tuned on the eval.
3. **Rerank** the top 30 with a small ONNX cross-encoder (or none, if the eval keeps saying so), then
   recency decay per Domain (`score*(1-w) + w*0.5^(age/half_life)`, `w=0` for evergreen), then the
   DiversityPolicy.
4. **Report** with every response: `coverage` (strong / partial / none), `domains_routed` with
   confidences, `newest_date`, `stale` per Domain, and per hit a calibrated `p_relevant`.

**Coverage** comes from an isotonic curve fitted on our graded labels (reranker or hybrid score ->
P(grade >= 2)), shipped in the pack manifest. Thresholds per tier, tuned on the Gap questions:

| Tier | strong | partial |
|---|---|---|
| low | >= 2 hits with p >= 0.5 | >= 1 hit with p >= 0.35 |
| medium | >= 2 hits with p >= 0.6 from >= 2 Documents | >= 1 hit with p >= 0.45 |
| high | >= 2 hits with p >= 0.7 from >= 2 independent Sources | below strong counts as partial |

### 2.5 The host's rules (skills)

- One search per sub-question; at most two reformulations; then answer, state the Gap, or decline.
- Web search only when coverage is partial or none **and** the question is time-sensitive or outside
  the Pack's Domains, or always for a Domain with `web_policy: always_latest` (markets). Web results
  are cited as "web, unverified" and never override the Library on a high-tier rule.
- High tier: a Citation per claim, two independent sources, no instruction to trade, dose or accept a
  diagnosis, the decline-and-refer message when coverage isn't strong.
- Conflicting sources: show both sides with Citations (the Principle's dissenting Advice), never average them.
- Ingested text is data: posts and web pages are marked `untrusted` and fenced in tool output so
  instructions inside them are never followed.

### 2.6 Evals per Pack

- Question sets per source kind as today, each question carrying its seed Document's Domains, so the
  **routing set** comes free (expected Domains = the seed's); accuracy@1 and @3.
- **Gap questions**: ~30 per Pack, written for topics outside its Domains or from Documents held out,
  pooled and judged to confirm no relevant Moment; measure wrongful answers and wrongful refusals.
- Coach gates G2/G4/G5/G6 per Pack; high tier adds a safety gate.
- `ytbrain ops` runs them per Pack and records history (the ops history item from 2026-10-01).

## 3. Milestones

### M5 — Library and routing (current corpus; Founder Coach unchanged to its users)

| Step | What | Size |
|---|---|---|
| M5a | `domains.yaml`, Source and folder Domain tags, `source_id` / `domains` / `facets` through items, LanceDB, pack, `Filter` | S |
| M5b | Catalog cards (Document cards from Verified summaries; Book, Series, Domain cards generated), OKF markdown export, `browse` tool | M |
| M5c | Contextual Passages as eval config `full-ctx`; adopted only on PASS | S-M |
| M5d | Router + route-and-backfill; routing set from existing questions | M |
| M5e | Calibration, `coverage` in every response, skill rules for web, stop and decline; Gap questions | M |
| M5f | Latency: ONNX reranker on the top 30, ANN benchmark, p95 measured | S-M |

**Done when:** no nDCG@10 regression on any question set; routing accuracy@3 >= 90 %; wrongful answers
on Gap questions <= 10 % with wrongful refusals <= 15 %; search p95 <= 1.5 s from the plugin.

### M6 — Engine and Packs

ADR-0015 accepted; `pack.yaml` with generated enums; schema 3.0 with Fact and the facet Step; runtime
split into an engine core (search, catalog, profile facts, Decisions, Feedback, usage) and optional
modules (the founder accountability loop: Goals, Commitments, Check-ins); `packs/founder` with a parity
test (same results on the 263 questions); `packs/systems` from your coding books (facets: lifecycle,
concern); the engine's product id and data-folder migration with backup.

### M7 — Posts and freshness

Source kind **Post** (Locator = the post URL; one Passage per post or thread, Takeaways only for long
threads; a post is a Speaker's view, never a Fact). Connectors in order: RSS / Substack / Bluesky
(free), X official API with a monthly cap (default $10), LinkedIn manual-save inbox and your own export.
Sync windows (`since: 6 months`), deletions honored, `superseded_by`, `web_policy` live. Then Pack #3
(investing, high tier) behind the safety gate.

### Later, only if evals ask for it

Cross-book synthesis (HippoRAG 2) when multi-Document questions fail; personal memory with validity
windows (Graphiti- or GBrain-style); a per-Pack embedding or reranker model.

## 4. Scenarios and how they are handled

| Scenario | Handling |
|---|---|
| Question spans two Domains or Packs | Router returns both; one server searches both |
| Router is wrong | Backfill from the whole Library; host can widen with `domains` |
| Nothing relevant in the Library | `coverage: none` -> state the Gap; web only per `web_policy` |
| Time-sensitive question (latest tweet, price) | Domain freshness + `stale`; `always_latest` makes the host check the web first |
| Sources disagree | Both sides cited, never merged |
| High-stakes question with weak evidence | Decline and refer; safety gate in evals |
| Private data (books, medical records) | Visibility from the Source; never in a shared pack or released eval |
| Instructions hidden in a post or page | `untrusted` content fenced; never followed |
| A new Source type | One adapter module + its Source kind; the rest is config |
| A new use case | A `pack.yaml`, its Domains, skills and evals; no engine code |
| Corpus grows 10x | Routed candidate sets, ANN in the pack, incremental indexing |
| Cost runaway | Spend caps on extraction, evals and connectors (X per-read billing) |
