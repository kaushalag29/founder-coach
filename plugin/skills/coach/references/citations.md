# Citation format

Inline, right after the claim it supports:

> Charge from the first pilot, even a small amount ([Michael Seibel, "How to Price", 2019 · 12:40](https://www.youtube.com/watch?v=…&t=760s)).

- Speaker, talk title, year and the timestamp as mm:ss, linked with the hit's `deep_link`.
- An article hit (`source_kind: article`, no `start_s`) has no timestamp: author, title and year,
  linked with its `deep_link` (it opens at the quoted words), e.g.
  ([Paul Graham, "Do Things That Don't Scale", 2013](https://paulgraham.com/ds.html#:~:text=…)).
- Quote the Evidence (`quote`) when the exact wording matters.
- One Citation per claim is enough; more only when talks agree or disagree.
- Never build a link by hand: use the `deep_link` the tool returned.
- The link text is the speaker, talk, year and timestamp. item_ids are for the tools
  (`coach_read`, the `citations` of a saved record): keep them out of what the Founder reads.
