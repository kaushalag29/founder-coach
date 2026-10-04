<!-- generated from plugin/skills/ask/SKILL.md by scripts/assemble_plugin.py; edit the skill -->
# Ask

Question: {arguments}

Follow the coaching contract in the server instructions throughout: cite only retrieved item_ids, date the advice, name Gaps.

## Checklist

1. **Context.** Call `coach_get_context` unless you already did in this conversation. Note the Stage, and the Domains in `library` when it is present (the subjects the knowledge covers). If setup is needed, carry on: answer now and offer `the setup prompt` once at the end.
2. **Split.** Break the question into at most 4 independent parts ("price a pilot and when to raise" is two). Most questions are one part: split only when the Founder asked about separate things.
3. **Search each part.** One `coach_search` per part, passing the Stage, all issued together in one message. Rewrite each part as a short standalone query in the talks' vocabulary. When `library` lists several Domains, pass `domains` with the Domains that part touches (one or several: a part that spans two subjects lists both); leave `domains` out when unsure, and the whole Library is searched. At most one follow-up search per part, and only with a clearly different query; after that, what's still missing is a Gap.
4. **Read when thin.** If a hit looks right but its text is too short to answer from, `coach_read` its item_id for the talk's full context.
5. **Check coverage.** For each part read `coverage` (and `coverage_by_domain`, `stale_domains`) and apply the coverage rule in the server instructions: a part below `strong` in a high-risk Domain is declined, a part with `none` is a Gap, and web search happens only as those rules allow.
6. **Answer** with the template below. Every part gets an answer or a Gap.
7. **Offer to keep it.** If the answer leads to a choice the Founder makes, offer to record it as a Decision (`coach_record`, kind `decision`) with its Citations, and save only on a yes.

## Template

```
**Short answer:** one or two sentences.

**<Part 1>**: the advice with inline Citations: a talk (speaker, title, year · mm:ss), an article (author, title, year), a book (author, book, PDF p. N, unlinked).
**<Part 2>**: …

**Where sources disagree:** only if they do.
**Gaps:** what the sources don't cover, and "general knowledge" for anything said without a Citation.
**For you:** one line applying it to the Founder's Stage and Goals.
```

## Gotchas

- A part with `coverage: none` (`gap_suspected: true`) and no convincing quote is a Gap. Answering it from memory without saying so breaks the contract.
- Don't repeat the same talk three times; prefer the best item per claim.
- Imperfect hits are normal. Answer from the closest items and name the Gap rather than searching again and again: every extra call is a turn the Founder waits through.
