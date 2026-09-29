<!-- generated from plugin/skills/setup/SKILL.md by scripts/assemble_plugin.py; edit the skill -->
# Setup

Follow the coaching contract in the server instructions: show what you'll save and save only on a yes.

## Checklist

1. **Context.** `coach_get_context`. If a profile exists, show it and ask what to change instead of starting over.
2. **Interview,** one question at a time, short answers are fine:
   - company name and a one-line description (`company`, `one_liner`);
   - who the customer is (`customer`);
   - Stage: offer the eight values from the Stage list in coach_update_profile and let them pick (`stage`);
   - team size (`team_size`) and the one or two numbers that matter now (`key_metrics`);
   - time zone: propose the one `coach_get_context` reports and let them correct it (`timezone`, an IANA name like `Asia/Kolkata`);
   - preferred Check-in day (`checkin_day`, e.g. `friday`).
3. **Save the profile.** Show the values, get a yes, then one `coach_update_profile` call. If the Founder already asked you to save and dictated the values, that is the yes: save exactly those, then show them, and ask about any field they left out.
4. **One Goal.** Ask for the outcome that matters most over the next 4-8 weeks, with a measure and a target date. Confirm, then `coach_record` kind `goal`.
5. **Models.** The coach downloads its search models (about 0.2 GB, once) by itself in the background when it starts, so there is nothing the Founder must run: search uses keyword matches until the download ends, then switches to semantic on its own. Say that in one sentence; `coach_corpus_status` shows `search_mode` if they want to check. Run `founder-coach warmup` with Bash only to show download progress, and only when Bash can reach that folder. If it can't (a sandboxed shell, as in Cowork), skip it; never hand the Founder a command containing a temporary plugin path.
6. **Weekly reminder.** Offer a weekly scheduled task in the Claude desktop app that runs `the check-in prompt` on their Check-in day. Set it up only if they say yes; otherwise say the coach will mention an overdue Check-in at the start of a session.
7. **Close** with what's saved, where it lives (`~/.founder-coach/FOUNDER.md`, readable any time) and three things to try: `the ask prompt`, `the weekly-focus prompt`, `the check-in prompt`.

## Gotchas

- If `uvx` is missing, the coach can't start at all: tell them to install uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`). If `coach_corpus_status` reports the models failed to load, say so plainly, name the error, and note that search stays keyword-only meanwhile.
- Everything stays on this machine. Say so once, in the Close, and in the same breath: the coach also keeps a local
  usage log of which of its tools ran and how long they took (never their words), kept 90 days;
  `FOUNDER_COACH_USAGE=0` turns it off. Don't repeat privacy boilerplate after that.
