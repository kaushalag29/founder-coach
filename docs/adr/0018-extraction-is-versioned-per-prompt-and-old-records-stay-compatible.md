---
status: proposed
---
# Extraction is versioned per prompt, and older records stay valid unless a change is declared breaking

Today one `SCHEMA_VERSION` (2.2.0) stamps every record, and bumping it marks every extraction stale:
about 2,300 Documents re-extracted for any schema change. Instead each record carries the prompt
variant and version that produced it, and every new version is declared, next to it in code, either:

- **compatible (the default):** older records stay valid and are read in the new shape by a mapping in
  code, the way `ytbrain refresh` already backfills Series and provenance. 2.2 records map as: Stage and
  Category become founder Facets, the jargon list becomes entities, Domains come from tagging
  (ADR-0017). Nothing is re-run;
- **breaking (rare, deliberate):** older records cannot be mapped and re-extract, always behind
  `--dry-run` (count and estimated cost) and `--max-cost`, with the reason in the CHANGELOG.

New content always runs the latest pipeline: one subject-neutral core prompt (Advice with the
conditions it applies under, Facts, Rules, Takeaways, a summary, free-text topics; no fixed Category or
Stage list), then the `tag` Step, then `enrich` (Facts and Rules) for Documents in Domains whose Packs
use them. A new user, or any new Document, never sees the 2.2 prompt. Older records move to the latest
version only when an eval shows a Pack held back by them, for that Pack's Documents, or when the owner
runs `ytbrain upgrade`; the mapping code is removed once no record uses it.

## Considered Options

- **One global version (today):** simple, but every improvement costs a full re-extract, so
  improvements get postponed or the pipeline forks quietly.
- **Re-extract gradually under a per-run budget:** converges, but spends money on records that already
  pass their gates.
- **A prompt variant per Domain:** sharper prompts, but every new Domain needs a new prompt, which is
  the hardcoding ADR-0017 removes.

## Consequences

A test fails when extraction or tag prompt text changes without a new version and a compatible or
breaking declaration. `scripts/check.sh` runs the latest pipeline end to end on fixtures, so the path a
new user takes is tested on every commit. `ytbrain status` reports records per variant and version. Eval
labels survive because they point at Moments, not items; existing vectors survive because a mapping or a
retag never changes item text.
