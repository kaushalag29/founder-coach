---
name: project
description: Lists, switches, creates or renames the {{person}}'s Projects (one per {{project_noun}}, each with its own memory), or summarises another one on request. Use when the person types /{{id}}:project, or asks to switch {{project_noun}}, start a new one, or see which one is active.
---

# Project

Each Project keeps its own memory: {{memory}}. A session works on one active Project; nothing moves between Projects.

1. **Where we are.** `coach_project` with action `list`. Say which Project is active (or that none is) and list the others by name.
2. **Do what was asked,** one `coach_project` call:
   - *switch:* `switch` with the Project's id or name. Then `coach_get_context` and give a one-line recap of that Project (its key profile facts, the active Goal, what's due).
   - *a new {{project_noun}}:* `create` with its name (it becomes the active Project), then offer `/{{id}}:setup` for its profile.
   - *rename:* `rename` with the new name; its memory stays as it is.
   - *look at or compare another Project:* `summary` with its id. Only when the {{person}} asks; it is read-only, and nothing from it is saved into the active Project.
3. **Name the Project** in every later save you propose ("Save to Acme: …").

## Gotchas

- With several Projects and none active, ask which one before reading or saving anything about the {{person}}. A question that needs no memory can be answered first.
- A fact true for every Project (their name, role, time zone, how they like answers) may go to the Common profile their coaches share: ask "Use this for all your Projects and coaches?" and save with `scope: "common"` only on a yes.
- `mode: "single"` means one data folder is pinned ({{env_prefix}}HOME): there are no Projects to switch; say so.
