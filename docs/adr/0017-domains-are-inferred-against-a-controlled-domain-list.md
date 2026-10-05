---
status: proposed
---
# Domains are inferred from content against a controlled Domain list

Supersedes "a Document's Domains are configuration, never extracted" (decisions #23, #24, #29 and #32
of the Library and Packs plan). A person adding an unknown channel, site or book should not have to
say what it is about: the system reads the content and tags it. Configuration (`domains:` on a
Source, a book's folder) becomes a **hint** that breaks ties, never a requirement.

How tagging stays correct and repeatable:
- **A closed list.** Passages and Knowledge items are tagged only with Domains already in `domains.yaml`
  (name, description, aliases, examples). The answer "none fits" is allowed; inventing a name is not.
- **Embeddings first, an LLM only for close calls.** Each Passage and item is scored against each
  Domain's profile by cosine (deterministic, free, offline). Only when the top two Domains are within a
  margin does a cheap LLM decide, given the full list and a structured output whose allowed values are
  the Domain names, at temperature 0 on a pinned model, cached by (text hash, prompt version, model,
  Domain list version). The same input and the same list always give the same tags.
- **The content decides, metadata only hints.** A Document's Domains are those covering a tuned share
  of its Passages (which cover all its text), so a talk on hiring and pricing gets leadership and gtm
  with no summary or table of contents. Its items carry their own tags.
- **A new Domain is proposed, never created by a tagger.** On request (`ytbrain domains propose`),
  "none fits" items are clustered (fixed seed) and each cluster must pass four checks before it is
  shown: a normalised name match (case, punctuation, spaces, plural), the aliases, meaning (embedding
  similarity to each existing Domain), and an LLM given the whole list asking "same, narrower, or new".
  Near matches become aliases; narrower subjects become Facets.
- **The person decides what changes safety.** A new Domain's Risk tier, a Pack's Domains, every
  high-tier tag (finance, investment: "suggested" until confirmed, in bulk per group after a preview) and
  Visibility are human choices. Unconfirmed high-tier tags are not used by `pack build`.
- **Measured.** About 100 hand-labelled items gate the tagger whenever its version or the Domain list
  changes (precision, recall, no high-tier label missed); every tag records its origin (`inferred`
  with model, version and score; `hint`; `confirmed`), shown by `ytbrain domains why`.

## Considered Options

- **Configuration only (today):** auditable and free, but someone must read every Source first, a
  talk gets one Domain, and a new kind of content falls into the default Domain.
- **An LLM names subjects freely:** dynamic, but "sales", "selling" and "b2b-sales" become three
  Domains, routing and Packs break, and runs disagree with each other.
- **An LLM tags everything:** closed-list but pays per item on every run and is not repeatable.
- **Map the extracted Category to Domains:** free, but one Category per Document gives at most one
  extra Domain, and the Category list is startup-shaped.

## Consequences

Retagging rewrites metadata only (no re-extraction, no re-embedding); a new Domain or alias re-scores
everything by embedding in seconds and asks the LLM only about results that changed or became close,
then swaps the tags in atomically. `domains.yaml` gains `aliases:`; `sync --strict-domains` no longer
stops on an unknown folder (the folder name is a hint and goes through the same four checks).
