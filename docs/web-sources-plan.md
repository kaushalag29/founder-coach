# Web Sources: plan, decisions and acceptance criteria

Status: decided and built 2026-09-26; first real crawls (paulgraham.com, pmarchive.com, YC Library) 2026-09-26/27, fixes in CHANGELOG. Research: [research/web-crawler.md](research/web-crawler.md).
Architecture decision: [ADR-0013](adr/0013-sources-plug-in-through-adapters.md). Vocabulary: [CONTEXT.md](../CONTEXT.md).

## Goal

Any list of websites in `sources.yaml` becomes Verified, cited knowledge for the coach, through
the same Steps as the talks, and a future Source type (PDF lists next) is one adapter plus one
prompt wording. The maintainer gives only a URL and how to crawl it; the crawler works out what
each page is.

## Decisions

| # | Decision |
|---|---|
| D1 | **Config**: `type: website`, `url`, optional `depth` (3), `render` (`auto`), `max_pages` (10 000 per run), `enabled` (true), `author` (none: the speaker when a page names no author), `id`/`name` (derived; a `name` you write is the series). Old YouTube entries (`playlist_id`, `kind: playlist`) are read as `type: youtube_playlist` unchanged. `enabled: false` stops fetching; its Documents stay searchable until `ytbrain drop --source <id>` (then `index`). |
| D2 | **Adapter seam** (ADR-0013): an adapter owns `sync` (discover + fetch into the manifest) and `clean` (raw file → the shared text-units transcript). Everything after `clean` is shared. YouTube becomes the first adapter with no behaviour change. |
| D3 | **Browser**: Playwright (Chromium, headless), only when needed: `render: auto` fetches with HTTP first and renders when the main text is under 150 words; `always` / `never` force it. Missing Playwright → HTTP only, one warning. Installed with the `web` extra plus `playwright install chromium`. |
| D4 | **Page metadata without site knowledge**: schema.org JSON-LD, OpenGraph, meta tags, `rel=canonical`, `<html lang>`, and trafilatura (2.2) for the main text. **Page type** from those plus signals: `article` (≥ 150 words of main text) is ingested; `listing` is used for links only; `video` (YouTube embed, little text) routes its video ids to YouTube sync; `other` is skipped. A page embedding a YouTube talk (with or without its transcript) is `video`; if YouTube has no captions for any of its talks, the page's transcript is ingested as an article instead. The start page (depth ≥ 1) and archive/category/tag/author/`/page/N` URLs are `listing`. A page reclassified as non-article after extraction is tombstoned. No extra LLM call: Topic, Stage and advice come from the existing extraction, and an adviceless article is already kept out of the index. |
| D5 | **Identity**: Document id `w-` + first 16 hex of SHA-256 of the canonical URL (scheme/host lowercased, default port, fragment and tracking parameters removed, query sorted). The full hash is stored; a different URL with the same id is refused (never merged). A page whose extracted-text hash matches an existing Document is an **alias** (moved or renamed site): no new Document, no second extraction. |
| D6 | **Politeness (industry defaults)**: `robots.txt` obeyed (RFC 9309: 4xx → allow all, 5xx/unreachable → disallow this run); one request per second per host or the site's `Crawl-delay` if slower; one host at a time; HTTP timeout 30 s, browser 60 s; 3 retries with exponential backoff and jitter on 429/5xx/network errors, honouring `Retry-After` (capped); honest User-Agent with a contact URL; 15 MB page cap (Googlebot's HTML limit); a host failing 5 pages in a row is paused for the run; only `http(s)`; never logs in; no proxy rotation, fingerprint spoofing or CAPTCHA solving. |
| D7 | **Scope**: same host as the start URL (`www.` treated as the same host) and under the start URL's directory; `depth` link hops; sitemaps from `robots.txt` and `/sitemap.xml` seed the queue (depth 1) when their URLs are in scope. |
| D8 | **Incremental and resumable**: the crawl queue lives in the manifest database, so an interrupted crawl resumes where it stopped. Known leaf pages (at the Source's `depth`) are re-checked after 7 days (or sooner when the sitemap's `lastmod` is newer) with conditional GET (ETag / Last-Modified); pages whose links are followed are fetched in full, since a 304 carries no links, and raising `depth` reopens the old frontier; an unchanged text hash re-runs nothing downstream; a changed one sends the Document back through clean → extract. 404/410 on a known page tombstones it: it leaves the index at the next `index`, the record stays archived. |
| D9 | **Language**: English only for now (declared `lang`, `og:locale` or JSON-LD `inLanguage`); others are skipped as `language: xx` and can be opened later without re-crawling. |
| D10 | **Record** (same shape as a talk's): given fields from the page (url, title, published date, series, provenance = site name, `speaker` = author, `source_kind: article`, `locator: paragraph`), generated fields from extraction. `source_kind` and `locator` are new *given* fields with defaults equal to what talk records already mean, so the generated schema (the LLM contract) is unchanged and no talk is re-extracted; a test pins the generated schema to `SCHEMA_VERSION`. |
| D11 | **Locators**: a paragraph number in the position field (`start_ms` for talks); links use a text fragment (`#:~:text=` + the quote's first words); pages and the coach show `¶12` instead of `mm:ss`. The coach's search hits gain `source_kind`, and `start_s` is null for articles. |
| D12 | **PDF and other files**: not now; a later `pdf_list` adapter. |

## Scenarios

Happy paths:
1. First crawl of a static site: start page → links within scope to depth → articles saved, listings followed, `clean` writes paragraph transcripts, `extract`/`verify`/`index` run unchanged, pack items link to the quote.
2. First crawl of a JavaScript site: HTTP gives an empty shell → rendered with Playwright → same as 1.
3. Daily re-run: nothing changed → pages not re-fetched within 7 days; after that, 304 or same hash → nothing downstream.
4. Page edited upstream → new hash → clean → extract again for that page only.
5. Site moves domain (new config URL) → pages found again by text hash → aliases, no re-extraction.
6. A page embeds a YouTube talk → the video id is queued for YouTube sync with the site as Series source; the page itself isn't ingested twice.
7. `enabled: false` → the Source is skipped with a note.

Failure paths:
8. `robots.txt` disallows a path → never fetched, recorded `skipped: robots.txt`.
9. `robots.txt` returns 5xx → nothing fetched from that host this run, retried next run.
10. 429 with `Retry-After` → waits (capped), retries up to 3 times, then `failed` (retried next run; parked after 3 failed runs in a row); a `Retry-After` over 120 s skips the page for this run; 5 failures in a row (401/403/429 included) → host paused for the run.
11. Network error / timeout → same as 10.
12. Page too large, not HTML, or redirected off-site → `skipped` with the reason.
13. 404/410 on a new URL → `skipped: gone`; on a known Document → tombstoned.
14. Playwright missing or the browser crashes → HTTP text is kept if any, else `failed: render`; one warning per run.
15. Interrupted (Ctrl+C, crash) mid-crawl → queue and finished pages are in SQLite; the next run continues; no half-written file is kept (atomic writes, sweep of `.tmp`).
16. Two URLs hash to the same id (not expected) → the second is refused and logged, never merged.
17. Non-English page → `skipped: language`.
18. Main text under 150 words even after rendering → `listing` or `other`, not ingested.

## Acceptance criteria (tests/test_web.py, offline: mocked HTTP, fake renderer)

- **AC1 Config**: old YouTube entries parse as `youtube_playlist`; a website entry with only `url` gets id, name, depth 3, render auto, enabled; `enabled: false` is not synced.
- **AC2 Identity**: canonicalization removes tracking params/fragments, lowercases the host, sorts the query; ids are `w-` + 16 hex, stable across runs; a forced collision is refused.
- **AC3 Scope and depth**: only same-host, in-directory links are queued; depth is respected; sitemap URLs in scope seed the queue; `max_pages` stops the run.
- **AC4 robots.txt**: disallowed URLs are never requested; `Crawl-delay` raises the per-host interval; 404 robots → allowed; 5xx robots → nothing fetched.
- **AC5 Page types and metadata**: an article yields title, author → speaker, date, site name → provenance, lang, paragraphs with headings; a listing yields links only; a YouTube-embed page routes its video id; non-English is skipped.
- **AC6 Rendering**: `auto` renders only when HTTP text is thin; `never` never renders; missing Playwright falls back with a warning.
- **AC7 Retries and failures**: 429 + `Retry-After` then 200 succeeds; persistent 5xx ends `failed` and is retried next run; 5 failures in a row pause the host; 404 on a known Document tombstones it.
- **AC8 Incremental**: a second run within 7 days fetches nothing; after 7 days a 304 or same hash changes nothing downstream; a changed page re-opens clean and extract; the same text at a new URL becomes an alias.
- **AC9 Resume**: an exception mid-crawl leaves the queue; the next run fetches only the rest.
- **AC10 Shared Steps**: `clean` writes a transcript with `source_kind: article`, `locator: paragraph`, numbered units and heading chapters; the extraction prompt for articles speaks of an article and its text; the record carries the given fields; `verify` locates quotes to paragraph numbers; items and pages link with a text fragment and show `¶n`; YouTube records are unchanged.
- **AC11 Coach**: an article hit has `source_kind: article` and `start_s: null`; the tool-schema snapshot changes only by that field.
- **AC12 No regressions**: the five existing suites pass; the generated schema equals the pinned one for `SCHEMA_VERSION`.

## Out of scope now

PDFs and other files (D12), non-English, cross-host parallel crawling, near-duplicate detection,
Eval questions from articles and the pack decision for articles (later).
