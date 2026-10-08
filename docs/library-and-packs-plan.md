# Library and Packs: from Founder Coach to "your own expert agent from your own sources"

Status: **M5 built, M6 built and under evaluation** (2026-10-07); M6 and its acceptance criteria: [m6-plan.md](m6-plan.md). ADR-0015 accepted with amendments (ADR-0016, 0017, 0018); M5a, M5d and M5e built (2026-10-03), awaiting your corpus to measure them. Decisions settled in a grilling session; research behind them in
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
| 11 | ~~One engine plugin and one MCP server host every installed Pack~~ **Revised 2026-10-05: a plugin per Pack (ADR-0016).** Skills stay per Pack. | No N servers x tool descriptions; cross-Pack questions work |
| 12 | The runtime stays LLM-free. The server reports evidence signals; the host decides. | Local, free per question, deterministic, testable |
| 13 | **Catalog cards** (Domain, Book or Series, Document, Section) are generated navigation metadata, `trust: generated`, never cited; written as OKF-style markdown (`index.md` per level). | Keeps ADR-0004; makes 5-of-500 books cheap to find |
| 14 | Routing is **route-and-backfill**, never a hard filter unless the host passes `domains`. | A misroute costs a little ranking, never recall |
| 15 | Contextual retrieval for Passages and Chapter items, kept only if the eval gate passes. | Best-evidenced cheap gain (about 2/3 fewer failed lookups) |
| 16 | New Knowledge item type **Fact**; a trade-off is Advice with a condition. | Domain-neutral without distorting statements into advice |
| 17 | Per Pack: routing questions and **Gap questions** (out of corpus) gate a release; high tier adds a safety gate. | Declining is a requirement, so it is measured |
| 20 | **The host chooses Domains first.** `coach_get_context` lists the pack's Domains (name, size, what each is about) when there are several; the skills tell the host to pass `domains` per sub-question. A server-side **router** (each Domain scored by the mean cosine of its 3 closest items, so a small Domain is not drowned by a big one) is built, measured by `ytbrain eval route` and `--config pack-route`, and **off by default per pack** (`pack build --route`) until it shows no loss. | An LLM host routes by meaning at no cost; a similarity router is a backstop that must earn its place with numbers. Centroids were dropped: they blur and favour big Domains |
| 18 | Targets on a 16 GB M2: 1,000 Books + 2,000 Talks/Articles (~1M items), search p95 <= 1.5 s with no GPU, incremental indexing. | Shapes the index and reranker choices below |
| 19 | **Multi-domain by construction.** A question may touch several Domains: the router returns a set, search takes any-of, results are merged with a floor per routed Domain, and the host splits a multi-part question and routes each part. Adding books for a new Domain needs no code. | The final agent must answer "how do I raise a seed round and also keep my team motivated" from startup and leadership books together |
| 21 | **Compound questions are split by the host, not the server** (already in the ask playbook, step 2, and in #19/#20). The server never decomposes: no query-time LLM (#12). `eval route`'s composed set measures that a joined question still reaches both Domains when the host does not split. A server-side splitter is added only if `eval gap` shows compound questions failing where a host split cannot help. | One retrieval per sub-question keeps coverage judgeable per Domain and the citations traceable |
| 22 | **Sources are not routed; Domains are.** Book-level routing (M5b) waits until one Domain holds about 50 Books or the `eval route` miss analysis shows right-Domain, wrong-Book failures. Sources matter for Citations and the high-tier independence rule only. | At 22 Books the rerank within a Domain does this work; a second routing layer would be unmeasured complexity |
| 23 | **Revised 2026-10-05 (ADR-0017): a folder name is a hint, checked against the Domain list like any proposal.** Was: **A Book folder that is not a declared Domain is reported.** `ytbrain sync` warns per folder (`ytbrain ops` runs it with `--strict-domains`, exit 1); `ignore_folders:` in `domains.yaml` lists folders that only sort files. | A typo or an undeclared `coding/` silently tagged Books as `startup` once; tags are configuration, so a bad one should be loud |
| 24 | **Revised 2026-10-05 (ADR-0017): Domains are inferred per Passage and item against the Domain list.** Was: **A Document's extra Domains are per-Source or per-Book configuration, added only when `eval gap` shows the need** (for example finance-heavy talks tagged `[startup, finance]`, so a high-tier finance question can find two independent voices). Never classified per Document by an LLM at extraction time. | Keeps "Domain is configuration, never extracted" true and every tag auditable; guessing before the numbers exist would be tuning blind |
| 25 | **Coverage keeps the provisional cosine curve plus a Gap-tuned border shift; the fitted curve is not shipped yet** (2026-10-04). Fitted on the strict labels (grade >= 2) the curve has a base rate of 0.43 and tops out at p 0.699: with the 0.45 / 0.60 / 0.70 rules it refused 40 % of answerable questions and could never call a high-tier answer strong. With the provisional curve: 0 % wrongful refusals, and the border is tuned on Gap questions (`eval gap --tune`). Near-miss Gap questions (right Domain, wrong specifics) are what top-hit cosine separates worst; the lever for them is a reranker score (M5f), and calibration moves to that signal when it lands. | Asymmetric cost: a refused answerable question is a hard failure, a `partial` Gap is hedged by the host (and declined in a high tier); thresholds are re-based only against the signal that will ship |
| 26 | **The coach eval is a release gate, cached per gate on its inputs, and a failed G4-G6 case gets a majority of three runs** (2026-10-04). `ytbrain ops plugin` builds and validates; `--coach` adds the eval. Each gate's results are keyed on what it reads (the runtime, the pack's content hash, the full text of the skills it uses and only the descriptions of the others, its cases), so a version bump or an unrelated skill edit reuses them. | Three consecutive haiku builds flipped G5 between 0 and 2 of 3 personas with single runs; re-running every gate on every build cost about 25 minutes of plan usage and measured noise. Majority-of-3 on failures follows repeated-trial practice (Anthropic, "Demystifying evals for AI agents", 2026; tau-bench pass^k) while a pass costs one run |
| 27 | **Host routing is what ships, so it is what G6 measures.** G6 carries multi-Domain Compound questions and reports how many parts searched their Domain (or the whole Library); report only until a few runs set a fair bar. The M5 routing criterion (accuracy@3, composed recall) gates the server-side router alone, which stays opt-in. | The router is off and the host chooses Domains; gating M5 on the router measured something founders never use |
| 28 | **Decomposition keeps the cap of 4 parts and gains a completeness loop:** one rewritten follow-up for a part at partial or none, a re-read of the question for any ask left without a search, the conditions of one ask kept together, and asks beyond four listed as questions to ask next. | Splitting one multi-condition ask hurts first-stage retrieval (Yin et al., "When Should Queries Be Decomposed?", 2026); the risk in a coach is a part silently dropped, so completeness is checked rather than the cap raised |
| 29 | **`gtm` is a Domain** (revised 2026-10-05: YC's GTM talks are tagged gtm by the `tag` Step, ADR-0017) (sales, marketing, positioning, pricing, launches). Its own Sources and Books (`data/books/gtm/`) carry it; YC's GTM talks stay in their `startup` Sources and a GTM part searches `[gtm, startup]`. Per-Document Domain overrides only if `eval gap` shows GTM parts missing YC talks. | Keeps #24 (Domains are configuration per Source or Book); searching several Domains takes any of them, so nothing is lost |
| 30 | **The Founder's own data comes through the host's Connectors, never through the coach's server.** Skills ask for a capability ("this week's calendar"), not a product's tool; reading is for the task at hand and is said out loud; sending, scheduling or editing happens only when the Founder asks, after the exact text is shown; what a Connector returns is data and never triggers a save or an action; Founder memory never goes into another tool unless asked. | Every platform the Founder uses already has a Connector (Gmail, Microsoft 365, Google Calendar, Slack, Notion, Miro, HubSpot), so the server stays local and LLM-free; private memory + untrusted email + a send tool is the lethal trifecta (Willison, 2025), so actions need a yes |
| 31 | **A Goal past its target date is a Nudge, never an automatic status change;** a stale profile fact says when it was last confirmed. Full validity windows (Graphiti-style) stay on the Later list. | Real use hits stale Goals first; the confirm-before-write rule (ADR-0011) holds |
| 32 | **A new books folder becomes a Domain by a choice, never by itself** (revised 2026-10-05: proposals come from `ytbrain domains propose` after the duplicate checks of ADR-0017; the Risk tier is still asked) (refines #23). `ytbrain domains add NAME --risk ... --description ...` appends it to domains.yaml (comments kept, validated before the file is replaced); `ytbrain domains ignore FOLDER` marks a sorting folder; `ytbrain ops` asks both questions at the terminal when sync stops on a stray folder, and stops as before with no answer. Folder names match case- and space-insensitively (`GTM/` is `gtm`). | Auto-adding would turn a typo into a Domain and give `medical/` a default risk tier; the tier is the one field that changes what the coach may say, so it is asked, not guessed |
| 33 | **The Founder's Workspace is a profile fact** (`workspace`: what -> where, never stale), saved on a yes when they say where something lives or name a tool to use; the coach looks there first. A named tool the host can't reach gets one sentence on connecting it in the host's settings. The coach keeps no list of Connectors: whatever the host has enabled is used. | Turns "a Connector exists" into "the coach knows where your numbers are"; one profile field, no new table |

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
- Why it matters now (seen on the first coding and system-design books, 2026-10-04): the 2.2 extraction prompt and its
  enums are startup-shaped. A database chapter gets `category: product-market-fit`, and a pure software-craft chapter
  (Clean Code, "Formatting") can come back with takeaways but no Advice because "no startup stage applies", which the
  verifier flags as "no advice". 3 of 165 non-matter chapters in the new books; their takeaways and summaries are indexed
  normally. The fix belongs here: a Domain-aware prompt variant for non-startup Domains whose hash leaves the startup
  prompt's hash untouched (a changed prompt hash marks every extraction stale and costs a full re-extract).
- Migration: existing Talk and Article records stay (Stage becomes a founder facet). Books re-extract
  under 3.0 to gain Facts (181 Chapters, a few dollars); others re-extract only when a Pack needs it.

### 2.3 Index and Packs on disk

- One Library index (LanceDB): items gain `domains[]`, `source_id`, `facets[]` ("name=value", filtered
  with `array_has`). A second table holds Catalog cards with embeddings for the router.
- `pack build --pack <id>` exports items whose Domains intersect the Pack's (Visibility rules as today)
  plus their cards. Brute-force vectors up to ~200k; above that an HNSW index file (usearch or
  sqlite-vec, chosen by a benchmark) ships in the pack. The runtime loads several packs and dedupes by item id.

### 2.4 Search: route, retrieve, rerank, report

1. **Choose Domains.** Normally the host: it reads the pack's Domains from `coach_get_context` and passes
   `domains` (any-of, several allowed) for each sub-question. As a backstop the server can **route** (no LLM,
   no extra model, off unless the pack says so): score each Domain by the mean cosine of its 3 closest items,
   route the Domains within a margin of the best (at most 3), never a forced single choice. Later (M5b) Domain,
   Book and Series cards refine the choice inside a Domain with more than 50 Books (top 10 Books).
2. **Retrieve**: top 40 within the routed set + top 40 from the whole Library, RRF-merged with a routed
   boost tuned on the eval. With several routed Domains each is retrieved on its own and merged with a
   floor (at least 2 slots per routed or explicitly named Domain while its hits clear the relevance floor), so a Domain
   with many books cannot crowd out the one the question also needs.
3. **Rerank** the top 30 with a small ONNX cross-encoder (or none, if the eval keeps saying so), then
   recency decay per Domain (`score*(1-w) + w*0.5^(age/half_life)`, `w=0` for evergreen), then the
   DiversityPolicy.
4. **Report** with every response: `coverage` (strong / partial / none), `domains_routed` with
   confidences, `newest_date`, `stale` per Domain, and per hit a calibrated `p_relevant`.

**Coverage** comes from an isotonic curve fitted on our graded labels (the hit's cosine similarity ->
P(grade >= 2); built as the closest-passage cosine because it is available with and without the reranker), shipped in the pack
manifest; until a fit exists a provisional curve anchored at the old 0.60 Gap cut-off is used and every response says
`coverage_basis: provisional`. `eval gap --tune` adds a `shift` of the border when the curve is right but the border is not. Thresholds per tier, tuned on the Gap questions:

| Tier | strong | partial |
|---|---|---|
| low | >= 2 hits with p >= 0.5 | >= 1 hit with p >= 0.35 |
| medium | >= 2 hits with p >= 0.6 from >= 2 Documents | >= 1 hit with p >= 0.45 |
| high | >= 2 hits with p >= 0.7 from >= 2 independent Sources (two Books count as independent; two Chapters of one Book do not) | below strong counts as partial |

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
- **Composed questions** test multi-domain routing without new books: two questions from different
  Domains joined into one prompt ("..., and also ..."), expected Domains = both; measured as set recall
  (every expected Domain routed) and precision (no more than one extra), plus the answer-side check that
  hits from both Domains reach the top 8.
- **Gap questions**: ~30 per Pack, written for topics outside its Domains or from Documents held out,
  pooled and judged to confirm no relevant Moment; measure wrongful answers and wrongful refusals.
- Coach gates G2/G4/G5/G6 per Pack; high tier adds a safety gate.
- `ytbrain ops` runs them per Pack and records history (the ops history item from 2026-10-01).

## 3. Milestones

### M5 — Library and routing (current corpus; Founder Coach unchanged to its users)

Order changed 2026-10-03: the router (multi-domain) moves up, because it is what the final agent needs.

| Step | What | Size | State |
|---|---|---|---|
| M5a | `domains.yaml`, Source and folder Domain tags, `source_id` / `domains` through items, LanceDB, pack, `Filter` (any-of), `coach_search domains`, in-place retag | S | **done** |
| M5d | Host-driven Domain choice (context lists Domains, skills pass `domains`), router (opt-in per pack), route-and-backfill with a floor per routed Domain, composed-question routing eval (`ytbrain eval route`), `routing` and `domains_searched` in every search response | M | **built; the router stays off until `eval route` and `pack-route` say it is safe (needs your corpus)** |
| M5e | Calibration, `coverage` in every response, skill rules for web, stop and decline; Gap questions | M | **built (`founder_coach/coverage.py`, `eval calibrate`, `eval gap`); curves stay provisional and the router off until your corpus has books in a second Domain and judged labels** |
| M5b | Catalog cards (Document cards from Verified summaries; Book, Series, Domain cards generated), OKF markdown export, `browse` tool; Book-level routing | M | |
| M5c | Contextual Passages as eval config `full-ctx`; adopted only on PASS (paid: LLM call per Passage, capped at $15 for all of M5) | S-M | |
| M5f | Latency: p95 measured (`eval latency`, offline, on the shipped pack); the reranker's score measured against similarity on near-miss Gap questions before any reranker is built | S-M | |

**Done when:** no nDCG@10 regression on any question set; G6 passes with its multi-Domain questions (#27); wrongful answers
on Gap questions <= 10 % with wrongful refusals <= 15 %, on a Gap set of at least 60 questions; search p95 <= 1.5 s from the plugin.
The router's own bar (routing accuracy@3 >= 90 %, composed recall >= 90 %) applies only before a pack turns it on. M5b and M5c
are not part of M5's exit (decided 2026-10-04): Catalog cards wait for about 50 Books in one Domain (#22), contextual Passages
for a measured need.

Status 2026-10-04: nDCG met (ops eval PASS); Gap targets met on 30 questions (10.0 % / 3.2 %), set to grow; G6 multi-Domain
cases added (report only); p95 not yet measured.

### M6 — Engine and Packs

**Superseded by [m6-plan.md](m6-plan.md)** (2026-10-05): versioned records, inferred Domains, neutral extraction,
a plugin per Pack, Projects and the Common profile, the coding and investor Packs, with acceptance criteria.
The paragraph below is the earlier sketch.

ADR-0015 accepted; `pack.yaml` with generated enums; schema 3.0 with Fact and the facet Step; runtime
split into an engine core (search, catalog, profile facts, Decisions, Feedback, usage) and optional
modules (the founder accountability loop: Goals, Commitments, Check-ins); `packs/founder` with a parity
test (same results on the 263 questions); `packs/coding` (plugin `coding-coach`) from your coding books (facets: lifecycle,
concern); the engine's product id and data-folder migration with backup.

### M7 — Posts and freshness

Source kind **Post** (Locator = the post URL; one Passage per post or thread, Takeaways only for long
threads; a post is a Speaker's view, never a Fact). Connectors in order: RSS / Substack / Bluesky
(free), X official API with a monthly cap (default $10), LinkedIn manual-save inbox and your own export.
Sync windows (`since: 6 months`), deletions honored, `superseded_by`, `web_policy` live. (The investing Pack
moved into M6, step M6g, 2026-10-05.)

### Later, only if evals ask for it

Cross-book synthesis (HippoRAG 2) when multi-Document questions fail; personal memory with validity
windows (Graphiti- or GBrain-style); a per-Pack embedding or reranker model.

## 4. Scenarios and how they are handled

| Scenario | Handling |
|---|---|
| Question spans two Domains or Packs | Router returns both; one search covers both with a floor per Domain; a multi-part prompt is split by the host and each part routed |
| A Domain has no books yet (finance) | The router never routes to a Domain with no items; asking for it by name returns a stated Gap, not an error |
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
