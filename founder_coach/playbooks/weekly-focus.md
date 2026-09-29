<!-- generated from plugin/skills/weekly-focus/SKILL.md by scripts/assemble_plugin.py; edit the skill -->
# Weekly focus

Follow the coaching contract in the server instructions throughout.

## Checklist

1. **Context.** `coach_get_context`: Stage, active Goals, this week's open Commitments, overdue ones, last Check-in.
2. **Clear the old week first.** If overdue Commitments exist and no Check-in happened, suggest `the check-in prompt` before planning. Planning on top of unreviewed work hides what slipped.
3. **Find what matters.** For the most important Goal, run `coach_search` for what YC advises at this Stage. Use the last Check-in's blockers as queries too. With several Goals, or none, pick from the Stage and what the Founder just said, and name that assumption in the proposal.
4. **Propose up to 3 Commitments,** fewer if this week already has open ones. Each:
   - **action**: one concrete thing ("Call 10 design-partner leads").
   - **cue**: when/if it happens ("Tuesday and Thursday 9-11am, before email").
   - **outcome**: measurable ("3 pilots with a price agreed").
   - **why**: one line with a Citation; pass its item_ids as the Commitment's `citations`.
   - linked `goal_id` when it serves a Goal.
5. **Push back** on a Commitment that doesn't move the biggest risk, and say which one you'd drop.
6. **Confirm.** Show the final list exactly as it will be saved and ask for a yes. Edit until it's right.
7. **Record** with one `coach_record` call, kind `commitments`, all accepted items together. Relay any `warnings`.

## Why this shape

If-then plans with a measurable outcome are followed through far more often than open to-dos, and three is the most a founder can protect in one week.

## Gotchas

- The proposal comes in the first reply. A missing Goal or detail is an assumption you state, never a reason to hold the proposal back.
- The week is the Founder's ISO week in their time zone; the tool fills it in.
- Carrying last week's Commitment is done in the Check-in (`coach_update`, status `carried`), not by recording a duplicate here.
