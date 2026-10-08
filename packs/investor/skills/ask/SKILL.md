---
name: ask
description: Answers a personal investing question from cited investing books and investor-education pages, splitting multi-part questions and searching each part, and declining security picks and forecasts. Use when an investor asks how asset allocation, diversification, index funds, bonds, fees, rebalancing or long-term saving work, asks what the books say, or types /{{id}}:ask.
argument-hint: "[question]"
---

# Ask

Question: $ARGUMENTS

Follow the coaching contract of the coach skill throughout: every claim cited, no security calls, numbers only from the tools.

## Checklist

1. **Context.** Call `coach_get_context` unless you already did: the active goal and its policy. If setup is needed, answer now and offer `/{{id}}:setup` once at the end.
2. **Out of bounds first.** A part asking what to buy or sell, a named security's prospects, a price or return forecast, timing, options, leverage, shorting, crypto, individual tax or legal advice, or a non-US market is declined in one sentence with the referral (rule 5 of the coach skill). Keep the rest.
3. **Split** the rest into at most 4 independent parts. With more, answer the 4 that matter most now and offer to continue: none is dropped.
4. **Search each part.** One `coach_search` per part, all in one message, with `domains` `investment`, each a short standalone query in the sources' words ("rebalancing band", "expense ratio drag"). A startup or coding part goes to another coach's `coach_search` when `other_coaches` lists one; otherwise it is a Gap.
5. **Their numbers.** If a part needs their portfolio ("how far off my targets am I"), call `coach_review` (or `coach_split` for "how would I split $50,000") and use its numbers with their "as of" date. Never compute them.
6. **Check coverage, then one follow-up** per part below `strong`, all in one message. Then a part still below `strong` is declined (high-risk Domain), a part at `none` is a Gap.
7. **Completeness check.** Every part ends as an answer, a decline or a Gap: never left out.
8. **Answer** with the template below, in the order asked.
9. **Offer to keep it.** If they decide something about their own policy ("I'll keep a 5-point band"), offer to save it (the policy field, or `coach_record` kind `decision`), on a yes.

## Template

```
**Short answer:** one or two sentences.

**<Part 1>**: the principle with inline Citations: a book (author, book, PDF p. N, unlinked), a page (title, site, linked).
**<Part 2>**: …

**Your policy:** what their own Investment Policy Statement says about it, or that it doesn't say yet.
**Not covered here:** declined parts with the referral, and Gaps.
**More of your questions:** only when step 3 held asks back.
This is education, not advice on any security.
```
