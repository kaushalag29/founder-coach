---
name: setup
description: First-run setup for the founder coach, a short interview that records the Founder profile and one Goal, downloads the search models and offers a weekly Check-in reminder. Use when the founder types /{{id}}:setup.
disable-model-invocation: true
---

# Setup

Follow the coaching contract of the coach skill: show what you'll save and save only on a yes.

## Checklist

1. **Context.** `coach_get_context`. If a profile exists, show it and ask what to change instead of starting over.
2. **Interview,** one question at a time, short answers are fine:
   - company name and a one-line description (`company`, `one_liner`);
   - who the customer is (`customer`);
   - Stage: offer the eight values from the coach skill's stages reference and let them pick (`stage`);
   - team size (`team_size`) and the one or two numbers that matter now (`key_metrics`);
   - time zone: propose the one `coach_get_context` reports and let them correct it (`timezone`, an IANA name like `Asia/Kolkata`);
   - preferred Check-in day (`checkin_day`, e.g. `friday`).
3. **Save the profile.** Show the values, get a yes, then one `coach_update_profile` call. If the Founder already asked you to save and dictated the values, that is the yes: save exactly those, then show them, and ask about any field they left out.
4. **One Goal.** Ask for the outcome that matters most over the next 4-8 weeks, with a measure and a target date. Confirm, then `coach_record` kind `goal`.
5. **Models.** Run `uvx --from "${CLAUDE_SKILL_DIR}/../.." {{id}} warmup` with Bash. It downloads about 0.2 GB once, with progress. Until it finishes, search works in keyword mode, so the Founder can carry on.
6. **Weekly reminder.** Offer a weekly scheduled task in the Claude desktop app that runs `/{{id}}:check-in` on their Check-in day. Set it up only if they say yes; otherwise say the coach will mention an overdue Check-in at the start of a session.
7. **Close** with what's saved, where it lives (`~/.{{id}}/FOUNDER.md`, readable any time) and three things to try: `/{{id}}:ask`, `/{{id}}:weekly-focus`, `/{{id}}:check-in`.

## Gotchas

- If `uvx` is missing, tell them to install uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and stop the models step; everything else still works.
- Everything stays on this machine. Say so once, in the Close, and in the same breath: the coach also keeps a local
  usage log of which of its tools ran and how long they took (never their words), kept 90 days;
  `{{env_prefix}}USAGE=0` turns it off. Don't repeat privacy boilerplate after that.
