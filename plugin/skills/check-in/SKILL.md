---
name: check-in
description: Runs the founder's weekly Check-in, reviewing each open Commitment with evidence, capturing Decisions and blockers, updating Stage and metrics that changed, then planning next week's Focus. Use when the founder wants a check-in, a weekly review or retro, reports how their week or Commitments went, or when a Nudge says a Check-in is overdue.
---

# Check-in

Follow the coaching contract of the coach skill throughout.

## Checklist

1. **Context.** `coach_get_context`: open and overdue Commitments, active Goals, stale profile facts, Decisions due for a revisit.
2. **Each open Commitment, one at a time.** Ask what happened and what the evidence is (numbers, names, links). Then propose the status:
   - `done`: the outcome was met or clearly advanced; the evidence goes in `note`.
   - `dropped`: no longer worth doing; the reason goes in `note`.
   - `carried`: still worth doing next week. The response tells you how many weeks in a row it has been carried; at 2 or more, ask what's really blocking it.
   Confirm, then `coach_update` with `id`, `status` and `note`. When the Founder's message already says what happened, propose every status together in one reply rather than asking again.
3. **Goals.** For each active Goal: met, dropped or still active? Update only what changed, on a yes.
4. **Decisions.** Ask whether any real choice was made this week. A Decision is a choice between options the Founder names; the reason a Commitment was dropped belongs in its `note`, not in a Decision. Record each as kind `decision` with its reasoning and Citations if advice informed it. Review Decisions due for a revisit.
5. **Profile.** Confirm stale facts and update Stage or key metrics that changed (`coach_update_profile`). Re-stating an unchanged value confirms it.
6. **Record the Check-in** (`coach_record`, kind `checkin`): summary, wins, blockers, in the Founder's words, after they approve the text.
7. **Hand off** to weekly-focus for next week.

## Why this order

Recorded progress with evidence is the strongest driver of goal attainment; reviewing before planning stops the same Commitment silently rolling forward.

## Gotchas

- A Check-in is complete only when every open Commitment has a status (`coach_update`) and the Check-in itself is recorded (`coach_record`, kind `checkin`). If recording it returns a warning about Commitments still open, finish those first.
- Name the one thing the Founder may be avoiding, once and kindly, if the evidence shows it (the same Commitment carried three weeks, no user conversations logged).
- Don't mark `done` on intent alone ("I mostly did it"): ask for the outcome.
