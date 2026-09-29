---
name: export
description: Exports everything the founder coach has saved about the Founder as JSON and Markdown files on this machine. Use when the founder types /{{id}}:export.
disable-model-invocation: true
argument-hint: "[output folder]"
---

# Export

1. Run with Bash: `uvx --from "${CLAUDE_SKILL_DIR}/../.." {{id}} export` — add `--out "<folder>"` if the Founder gave one: $ARGUMENTS
2. Report the folder it printed and the two files in it (JSON for tools, Markdown to read).
3. Don't open or summarise the exported contents unless asked; the Founder may be exporting to leave.
