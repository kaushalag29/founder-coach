---
name: forget
description: Permanently deletes everything the coach has saved about the {{person}}'s active Project, after they see what will go and type its {{project_noun}} name to confirm. Use when the person types /{{id}}:forget.
disable-model-invocation: true
---

# Forget

This deletes the {{person}}'s memory of the active Project. There are two gates: the {{person}} types the {{project_noun}} name, and the host asks permission before the command runs. Keep both.

1. **Show what goes.** Call `coach_get_context` and `coach_corpus_status`, and list the Project, what it holds ({{memory}}) and the store path (`store.path`); the command itself prints the full counts. Mention `/{{id}}:export` first if they may want a copy.
2. **Confirm.** Ask the {{person}} to type the {{project_noun}} name exactly as saved (the word `forget` if none is saved). Wait for it. Never fill it in yourself, even if you know it.
3. **Run** with Bash, passing exactly what they typed: `uvx --from "${CLAUDE_SKILL_DIR}/../.." {{id}} forget --confirm "<typed name>"`. It keeps one final backup unless they asked for none (then add `--no-backup`).
4. **Report** what the command printed. If it refused (name mismatch), say so and stop; don't retry with a corrected name unless the {{person}} types it again.
