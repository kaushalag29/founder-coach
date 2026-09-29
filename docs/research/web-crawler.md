# Research: the web crawler (next Source kinds after YouTube)

Status: research, 2026-09-26, kept as the record of how the crawler was chosen. Superseded by
[web-sources-plan.md](../web-sources-plan.md) and ADR-0013 wherever they differ: the crawler was
built on 2026-09-26 as the `ytbrain/web/` package (httpx + trafilatura, Playwright in the `web`
extra), Sources are `type: website`, Documents are `w-<sha256[:16]>` with Source kind `article`,
and the decisions at the end were taken there.

## Question

How should ytbrain ingest text Sources (YC Library, YC blog, Paul Graham's essays, Startup
School, Requests for Startups) so they reach the coach as Verified, cited knowledge, politely
and within each site's rules, reusing the pipeline we have?

## Findings

### 1. The targets

| Source | Size | How it's served | Overlap with the talks | Notes |
|---|---|---|---|---|
| **YC Library** (`ycombinator.com/library`) | `library/sitemap.xml` lists 862 URLs: 294 items plus 568 category pages; items are `/library/<2-char id>-<slug>` with `<lastmod>` | Item bodies aren't in the HTML a plain fetch sees (only title, description and, for videos, the embedded YouTube id): the page renders client-side | High: video items embed YouTube ids already in the corpus (e.g. `LCEmiRjPEtQ`, Karpathy, is ingested and Verified) | Only the text items are new; video items add a Series tag and a second URL at most |
| **Paul Graham essays** (`paulgraham.com/articles.html`) | 201 links on one table-based index page | Static HTML, one essay per page | None | The highest-signal new text; each essay carries its month and year |
| **YC blog** (`ycombinator.com/blog`) | not counted yet; tag and author pages exist | Server-rendered posts | Low | Mixes advice with announcements; needs a kind filter (advice vs news) |
| **Requests for Startups** (`ycombinator.com/rfs`) | a few pages | Server-rendered | None | Short, dated idea lists: "what YC wants now", date-sensitive by nature |
| **Startup School curriculum** (`startupschool.org/curriculum`) | not visible without rendering | Client-rendered | Mostly the same YouTube lectures | Low value once the talks are in; skip unless a text-only lesson turns up |

Not verified from here (the fetch tool refused the URLs): each host's `robots.txt` and the
`*.ycombinator.com` terms of use. The crawler must read and obey `robots.txt` at run time, and
the maintainer should read the terms once before the first crawl (a Hacker News thread discusses
them: news.ycombinator.com/item?id=26971592).

### 2. The 114 failed YouTube fetches are two different problems

From `data/manifest.db` (fetch Step, status `failed`):

- **70 are private videos.** `fetch.permanent_failure` already treats "Private video" as final
  (`unavailable`), but these rows were written on 2026-09-23 before that rule. The next
  `ytbrain sync` settles them as unavailable with no retries.
- **44 are HTTP 429 rate limits** on subtitle downloads. `ytbrain sync --backfill` retries them
  slowly; nothing new to build.

So "retry the failed fetches" is one command, not crawler work.

### 3. Tools (September 2026)

| Tool | Version, licence | Fits | Cost to us |
|---|---|---|---|
| **trafilatura** | 2.2.0, Apache-2.0 | Main-text extraction with metadata (title, author, date), Markdown output, sitemap and feed discovery, a crawler that honours `robots.txt`; benchmarked as the most accurate open-source extractor; used by HuggingFace, the Internet Archive and others | Pure Python, light; no browser |
| **httpx** | already a core dependency | Conditional GET (ETag / If-Modified-Since), timeouts, HTTP/2 | none |
| **urllib.robotparser** | stdlib | `robots.txt` rules and `Crawl-delay` | none |
| **Crawl4AI** | 0.9.4 (2026-09-23), Apache-2.0, Python ≥ 3.10 | Headless-browser crawling with Markdown output, `check_robots_txt`, caching, sitemap seeding (`AsyncUrlSeeder`) | Playwright and a browser download (hundreds of MB); only worth it for client-rendered pages |

Recommendation: **httpx + trafilatura for static pages** (PG essays, YC blog, RFS), and a
one-hour spike on a YC Library text item before choosing between two routes for it: (a) the page
embeds its data as JSON (look for a `data-page` or `__NEXT_DATA__` blob in the raw HTML): parse
that with httpx, no browser; (b) it doesn't: render with Crawl4AI behind an optional `web-js`
extra. No proxy rotation, no browser impersonation, no CAPTCHA handling: the same rule the README
states for YouTube.

### 4. How it fits the pipeline we have

The design already expects this (ADR-0003): one source-neutral index, with a Source kind and a
Locator per item. What changes:

| Piece | Talks today | Essays |
|---|---|---|
| Source (`sources.yaml`) | `kind: playlist` | `kind: sitemap` (a sitemap URL plus an include pattern) or `kind: index` (one page of links, e.g. PG's `articles.html`) |
| Source kind (CONTEXT.md) | `talk` | `essay` (already a glossary value) |
| Document id | the YouTube id | `web-<sha1(canonical URL)[:12]>`, stable across runs; the URL stays in Document metadata |
| fetch Step | captions via yt-dlp | HTML via httpx, conditional GET; `robots.txt` checked per URL; stores raw HTML plus ETag / Last-Modified |
| clean Step | srt → utterances with timestamps | trafilatura → paragraphs `{id: "p12", text, heading}`; the same shape as utterances with a paragraph id in place of a time |
| Locator | a timestamp | a paragraph id; the deep link is the page URL plus a text fragment (`#:~:text=<first words of the quote>`), supported by current Chrome, Edge, Safari and Firefox |
| extract Step | talk prompt | the same schema; a prompt variant that says "essay", not "speaker" and "talk"; author and date come from the page metadata, never the model (the given-vs-generated invariant) |
| verify Step | quote found in utterances | unchanged: `find_evidence` already matches a span against a list of text units; paragraphs are text units |
| index / pack | items with timestamp links | unchanged apart from `deep_link` (`knowledge/items.deep_link` already takes a document URL) |
| Series | the playlist | "Paul Graham Essays", "YC Blog", "YC Library", "Requests for Startups" |

Politeness, as code (one module, `ytbrain/web.py`): an honest User-Agent with a contact URL;
per-host pacing of about one request per second with jitter, slower if `robots.txt` asks
(`Crawl-delay`); `Retry-After` honoured on 429 and 503 with exponential backoff; a host that
keeps refusing is parked like any failing Step (STEP_FAILURE_CAP); sitemap `<lastmod>` and
conditional GET make a re-crawl fetch only what changed.

Dedup: a Library item that embeds a YouTube id already in the corpus becomes metadata on that
Talk (an extra Series and URL), never a second Document.

### 5. What the coach and the eval need

- **Citations** read "Paul Graham, 'Do Things That Don't Scale' (2013)" with the text-fragment
  link: the citation rules in `plugin/skills/coach/references/citations.md` gain an essay form.
- **The pack** ships short Verified quotes with attribution and a link, as for talks (ADR-0009);
  never whole essays. Passages from essays, like transcript Passages, stay out of the pack.
- **The eval** (Tuning set) was generated from talks only, so it can't show whether essays help.
  About 20–30 new Eval questions seeded from essays, judged the same way, are needed before
  claiming a gain; the Holdout (M2c lite, G3) waits until the essays are in, as agreed.
- **Stage and Topic** for essay items come from the same extraction; the Jev plan (typed
  Stage/Topic tagging) applies to both.

### 6. Rights and terms (not legal advice)

The essays and blog posts are copyrighted. The design keeps to what the talks already do: fetch
publicly served pages, keep raw text local (git-ignored `data/`), publish only derived items
(summaries, advice) with short attributed quotes and a link to the original, honour `robots.txt`
and the site's terms, and stop if a site asks. LinkedIn is out of scope: its terms prohibit
scraping, and YC's posts there mostly point back to the blog, Library and videos, which we
collect at the source.

## Recommended plan

1. **Housekeeping (15 minutes):** `ytbrain sync` settles the 70 private videos;
   `ytbrain sync --backfill` retries the 44 rate-limited ones.
2. **Read the rules (maintainer, 15 minutes):** the three `robots.txt` files and the YC terms of use.
3. **Spike (1 hour):** fetch one YC Library text item with httpx and look for embedded JSON; decide
   route (a) or (b).
4. **M-web-1, Paul Graham essays (2 days):** `kind: index` Source, `ytbrain/web.py` (robots,
   pacing, conditional GET), clean → paragraphs, the essay prompt variant, text-fragment deep
   links, Source kind `essay`; tests with recorded HTML (no network).
5. **M-web-2, YC blog, RFS and Library text items (1–2 days):** `kind: sitemap` Sources, the
   advice-vs-news filter for the blog, dedup of Library video items against the talks.
6. **Measure (1 day):** 20–30 essay-seeded Eval questions, then `eval run` against the current
   baseline; rebuild the pack only if nDCG@10 holds or improves.
7. Then M2c lite (G3) on the final corpus, as agreed.

## Decisions for the maintainer

1. First wave: PG essays alone, or PG essays plus the YC blog together?
2. YC Library text items: accept a headless browser (the `web-js` extra) if the spike finds no
   embedded data, or leave them out?
3. YC blog: ingest announcements too (dated context), or advice posts only?
4. Pack: include essay items in the beta pack at once, or only after the essay Eval questions
   show they help?

## Sources

- YC Library sitemap: https://www.ycombinator.com/library/sitemap.xml (fetched 2026-09-26)
- Announcing and redesigning the YC Library: https://www.ycombinator.com/blog/startup-library ,
  https://www.ycombinator.com/blog/check-out-the-new-yc-library/
- Paul Graham, Essays index: https://paulgraham.com/articles.html
- Startup School curriculum: https://www.startupschool.org/curriculum
- Crawl4AI on PyPI: https://pypi.org/project/Crawl4AI/ ; docs: https://docs.crawl4ai.com/api/parameters/
- trafilatura docs: https://trafilatura.readthedocs.io/
- Discussion of the *.ycombinator.com terms of use: https://news.ycombinator.com/item?id=26971592
- Local facts: `data/manifest.db` (fetch failures), `ytbrain/fetch.py` (`PERMANENT_ERRORS`),
  `ytbrain/verify.py`, `ytbrain/knowledge/items.py`, ADR-0003, ADR-0009, CONTEXT.md
