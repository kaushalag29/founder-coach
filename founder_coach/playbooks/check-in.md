<!-- generated from plugin/skills/check-in/SKILL.md by scripts/assemble_plugin.py; edit the skill -->
# Check-in

Follow the coaching contract in the server instructions throughout.

## Checklist

1. **Context.** `coach_get_context`: open and overdue Commitments, active Goals, stale profile facts, Decisions due for a revisit.
2. **Each open Commitment, one at a time.** Ask what happened and what the evidence is (numbers, names, links). Then propose the status:
   - `done`: the outcome was met or clearly advanced; the evidence goes in `note`.
   - `dropped`: no longer worth doing; the reason goes in `note`.
   - `carried`: still worth doing next week. The response tells you how many weeks in a row it has been carried; at 2 or more, ask what's really blocking it.
   When you can read the Founder's calendar, email or documents, look for evidence first, starting where the profile's `workspace` says things live (the meetings, threads or files that match the Commitment) and show it in the proposal as "from your calendar: ..." for the Founder to confirm; follow the rule on the Founder's other tools in the server instructions.
   Confirm, then `coach_update` with `id`, `status` and `note`. When the Founder's message already says what happened, propose every status together in one reply rather than asking again.
   Founders name Commitments by position ("the first one", "the rest"): that is the order `coach_get_context` lists them. Resolve it yourself, show each match as id and action in the proposal, and let the Founder correct it; asking "which one?" before proposing is the slower path.
   Put every write of this Check-in in one proposal: each Commitment status, Goal change, profile change and the Check-in text. One yes saves all of it, in the same reply.
3. **Goals.** For each active Goal: met, dropped or still active? A Goal past its target date (the `goal_past_target` Nudge) needs one of three answers: met, dropped, or a new date. Update only what changed, on a yes; never change a Goal because its date passed.
4. **Decisions.** Ask whether any real choice was made this week. A Decision is a choice the Founder presents as a decision ("I've decided", "we chose A over B"). Record none otherwise: a dropped Commitment, a shift of priorities or a finished piece of work goes in the Commitment's `note` or the Check-in summary. Record each as kind `decision` with its reasoning and Citations if advice informed it. Review Decisions due for a revisit.
5. **Profile.** Confirm stale facts and update Stage or key metrics that changed (`coach_update_profile`). Re-stating an unchanged value confirms it.
6. **Record the Check-in** (`coach_record`, kind `checkin`): summary, wins, blockers, in the Founder's words, after they approve the text.
7. **Hand off** to weekly-focus for next week.

## Why this order

Recorded progress with evidence is the strongest driver of goal attainment; reviewing before planning stops the same Commitment silently rolling forward.

## Gotchas

- A Check-in is complete only when every open Commitment has a status (`coach_update`) and the Check-in itself is recorded (`coach_record`, kind `checkin`). If recording it returns a warning about Commitments still open, finish those first.
- Name the one thing the Founder may be avoiding, once and kindly, if the evidence shows it (the same Commitment carried three weeks, no user conversations logged).
- Don't mark `done` on intent alone ("I mostly did it"): ask for the outcome.
