---
status: accepted
---
# Sources plug in through adapters that end at one text-units transcript

ytbrain started as a YouTube pipeline; websites are next and PDF lists after them. Each Source
type gets one **adapter** that owns the two Source-specific Steps: `sync` (discover Documents and
fetch their raw files into the manifest) and `clean` (turn a raw file into the shared transcript:
numbered text units, each with a position and an optional heading, plus given metadata). Every
Step after `clean` (extract, verify, index, pack) and the coach are the same for every Source
type. The YouTube code becomes the first adapter without behaviour change.

A position is one integer per text unit: milliseconds for a talk, a paragraph number for an
article (later a page number for a PDF). The record's given `locator` field (`time`,
`paragraph`) says which, and one module turns a position into a label and a link
(`mm:ss` and `&t=` for talks, `¶n` and a `#:~:text=` fragment for articles).

## Considered Options

- **A separate pipeline per Source type:** duplicates retries, checkpoints, verification and
  indexing, and every fix would have to be made N times.
- **A crawling framework (Scrapy, Crawl4AI) owning storage and scheduling:** a second checkpoint
  system beside the manifest, and its own Markdown and caching we don't need. We use Playwright
  for rendering and trafilatura for main-text extraction as libraries instead.
- **Renaming `start_ms` to a neutral name in every file:** the right name, but it touches the
  record schema, the pack format and the eval benchmark for no behaviour change. The `locator`
  field makes the meaning explicit instead.

## Consequences

A new Source type is one adapter module plus, if its text isn't a talk or an article, one prompt
wording. `source_kind` and `locator` are given fields with defaults equal to what existing talk
records mean, so adding them doesn't change the generated schema (the LLM's contract) and no talk
is re-extracted; `SCHEMA_VERSION` now tracks the generated schema, pinned by a test.
