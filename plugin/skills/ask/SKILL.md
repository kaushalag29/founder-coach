---
name: ask
description: Answers a founder's startup question from cited Y Combinator talks, splitting multi-part questions and searching each part. Use when the founder asks how to do something at their startup, asks what YC advises, or types /{{id}}:ask.
argument-hint: "[question]"
---

# Ask

Question: $ARGUMENTS

Follow the coaching contract of the coach skill throughout: cite only retrieved item_ids, date the advice, name Gaps.

## Checklist

1. **Context.** Call `coach_get_context` unless you already did in this conversation. Note the Stage, and the Domains in `library` when it is present (the subjects the knowledge covers). If setup is needed, carry on: answer now and offer `/{{id}}:setup` once at the end.
2. **Split.** Break the question into at most 4 independent parts ("price a pilot and when to raise" is two) and keep the list in mind for step 7. Most questions are one part: split only when the Founder asked about separate things, and keep the conditions of one ask together ("seed fundraising for a B2B marketplace in India" is one part). With more than 4 asks, answer the 4 that matter most for the Founder's Stage now and hold the rest: none is dropped (step 8 offers to continue, and a yes runs the next batch of up to 4 with these same steps).
3. **Search each part.** One `coach_search` per part, passing the Stage, all issued together in one message. Rewrite each part as a short standalone query in the talks' vocabulary. When `library` lists several Domains, pass `domains` with the Domains that part touches (one or several: a part that spans two subjects lists both; a sales, marketing or pricing part lists `gtm` and `startup`); leave `domains` out when unsure, and the whole Library is searched. A part about a subject this coach doesn't cover but another coach in `other_coaches` does (its `domains`) is searched with that coach's `coach_search` instead: its knowledge only, never its other tools, cited the same way; with no such coach, that part is a Gap.
4. **The Founder's own data.** When the question points at their data ("our pipeline", "my last investor update") and the host can read it, read the narrowest slice that answers and say what you read; follow the connector rules of the coach skill. The talks give the advice; their data says how it applies.
5. **Read when thin.** If a hit looks right but its text is too short to answer from, `coach_read` its item_id for the talk's full context.
6. **Check coverage, then one follow-up.** For each part read `coverage` (and `coverage_by_domain`, `stale_domains`). A part at `partial` or `none` gets exactly one follow-up search with a rewritten query: the talks' vocabulary, a broader or narrower phrasing, or the Domain that part belongs to (all follow-ups together in one message). After that, apply the coverage rules of the coach skill: a part below `strong` in a high-risk Domain is declined, a part still at `none` is a Gap, and web search happens only as those rules allow.
7. **Completeness check.** Re-read the Founder's question against your list of parts. An ask with no search of its own (a second question tucked into a clause, "and how much equity?") gets its search now. Every part ends as an answer, a Gap or a decline: never left out.
8. **Answer** with the template below, one section per part, in the order the Founder asked.
9. **Offer to keep it.** If the answer leads to a choice the Founder makes, offer to record it as a Decision (`coach_record`, kind `decision`) with its Citations, and save only on a yes.

## Template

```
**Short answer:** one or two sentences.

**<Part 1>**: the advice with inline Citations: a talk (speaker, title, year · mm:ss), an article (author, title, year), a book (author, book, PDF p. N, unlinked).
**<Part 2>**: …

**Where sources disagree:** only if they do.
**Gaps:** what the sources don't cover, and "general knowledge" for anything said without a Citation.
**More of your questions:** only when step 2 held asks back: "I also have N more of your questions (<a few words each>): shall I continue?"
**For you:** one line applying it to the Founder's Stage and Goals.
```

## Gotchas

- A part with `coverage: none` (`gap_suspected: true`) and no convincing quote is a Gap. Answering it from memory without saying so breaks the contract.
- Don't repeat the same talk three times; prefer the best item per claim.
- Imperfect hits are normal. Answer from the closest items and name the Gap rather than searching again and again: every extra call is a turn the Founder waits through.
