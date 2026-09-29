---
name: forget
description: Permanently deletes everything the founder coach has saved about the Founder, after they see what will go and type their company name to confirm. Use when the founder types /{{id}}:forget.
disable-model-invocation: true
---

# Forget

This deletes the Founder's memory. There are two gates: the Founder types the company name, and the host asks permission before the command runs. Keep both.

1. **Show what goes.** Call `coach_get_context` and `coach_corpus_status`, and list the profile, active Goals, open Commitments, recent Decisions and the store path (`store.path`); the command itself prints the full counts. Mention `/{{id}}:export` first if they may want a copy.
2. **Confirm.** Ask the Founder to type the company name exactly as saved (the word `forget` if no company is saved). Wait for it. Never fill it in yourself, even if you know it.
3. **Run** with Bash, passing exactly what they typed: `uvx --from "${CLAUDE_SKILL_DIR}/../.." {{id}} forget --confirm "<typed name>"`. It keeps one final backup unless they asked for none (then add `--no-backup`).
4. **Report** what the command printed. If it refused (name mismatch), say so and stop; don't retry with a corrected name unless the Founder types it again.
