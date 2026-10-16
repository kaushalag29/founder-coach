---
name: ask
description: Answers a software engineering or system design question from cited engineering books and talks, splitting multi-part questions and searching each part. Use when an engineer asks how to design, build, test, scale or maintain code or a system, asks what the books say about it, or types /{{id}}:ask.
argument-hint: "[question]"
---

# Ask

Question: $ARGUMENTS

Follow the coaching contract of the coach skill throughout: cite only retrieved item_ids, say what you assumed about scale, name Gaps.

## Checklist

1. **Context.** Call `coach_get_context` unless you already did in this conversation. Note the system's stack, scale, SLOs and constraints, and the Domains in `library`. If setup is needed, carry on: answer now and offer `/{{id}}:setup` once at the end.
2. **Split.** Break the question into at most 4 independent parts ("cache invalidation for the feed, and how to test it" is two). Keep the conditions of one ask together. With more than 4, answer the 4 that matter most for this system now and hold the rest: none is dropped (step 8 offers to continue).
3. **Search each part.** One `coach_search` per part, all in one message, each a short standalone query in the sources' vocabulary ("idempotency key retries", not "my API sometimes double charges"). Leave `domains` out: every search covers both `coding` and `system-design`. Pass `domains` only when the Engineer restricts the search ("only the system-design books"). A part outside both (a startup, fundraising or hiring question) is searched with another coach's `coach_search` when `other_coaches` lists one for it (its knowledge only, cited the same way); otherwise it is a Gap.
4. **Their code.** When the question points at their code or design ("our schema", "this service"), read the narrowest slice the host can reach and say what you read; follow the code rules of the coach skill. The sources give the practice; their code says how it applies.
5. **Read when thin.** If a hit looks right but is too short to answer from, `coach_read` its item_id for the full context.
6. **Check coverage, then search in rounds.** A part at `strong` is done. A part at `partial` or `none` gets a rewritten query in the next round (the sources' terms, broader or narrower), a round's searches all in one message. Stop a part when it reaches `strong`, or when two rewrites in a row don't beat its best `top_similarity` so far. An ask has at most 5 rounds and 12 searches in all. Then apply the coverage rules of the coach skill.
7. **Completeness check.** Re-read the question against your parts. An ask with no search of its own gets one now. Every part ends as an answer, a Gap or a decline: never left out.
8. **Answer** with the template below, one section per part, in the order asked.
9. **Offer to keep it.** If the answer leads to a technical decision, offer to record it (`/{{id}}:decision-record`), and save only on a yes.

## Template

```
**Short answer:** one or two sentences.

**<Part 1>**: the advice with inline Citations: a book (author, book, PDF p. N, unlinked), a talk (speaker, title, year · mm:ss), an article (author, title, year).
**<Part 2>**: …

**Trade-off:** what this choice costs, and at what scale the answer changes.
**Where sources disagree:** only if they do.
**Gaps:** only when a part is missing from the sources, thinly covered, or said without a Citation: name the part and which of the three it is. Leave this line out when the sources answered everything.
**More of your questions:** only when step 2 held asks back: "I also have N more of your questions (<a few words each>): shall I continue?"
**For your system:** one line applying it to the profile's stack, scale and SLOs.
```

## Gotchas

- A part with `coverage: none` and no convincing quote is a Gap. Answering it from memory without saying so breaks the contract.
- Prefer the best item per claim; don't cite the same chapter three times.
- Example code is yours, short, and labelled as an example, never presented as a quote.
