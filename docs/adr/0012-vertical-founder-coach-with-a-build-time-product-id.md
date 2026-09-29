---
status: accepted
---
# The product is the vertical founder coach, and its name is one build-time product id

We ship a startup coach for Founders, not a general "build your own agent from your documents"
tool. The engine that could become that tool (the `ytbrain` pipeline, the Knowledge pack, the
runtime) stays internal until dogfooding shows it works on this one domain; it can be
open-sourced separately later under its own name.

The product's name is one stable **product id** (`founder-coach`), kept with its display name,
description and keywords in a committed `product.toml`. The plugin assembler writes it into
everything that ships: `plugin.json`, `marketplace.json`, `hooks.json`, the plugin's
`pyproject.toml` (CLI and package name) and the skill text (`/{{id}}:ask`). The Founder's data
folder (`~/.<id>`) and settings prefix (`<ID>_*`) follow the id. Internal names do not: the
Python import package `founder_coach`, the MCP server key `coach`, and the pipeline `ytbrain`.
The display name, description and keywords carry search terms ("AI startup coach", "Y Combinator
talks", "fundraising") and can change without touching installs; "YC" and "Y Combinator" appear
only in descriptions, never in the id or display name.

## Considered Options

- **Horizontal "agent from your documents" product now:** no eval target and no first user;
  competes with NotebookLM, Claude Projects and company-brain frameworks before the coach is
  proven. "Fine-tuning" would also misdescribe it: nothing retrains a model.
- **A YC-branded name (`yc-brain`):** reads as an official Y Combinator product and stops fitting
  once non-YC Sources arrive.
- **Name in `.env`:** `.env` holds secrets, stays out of git and never ships, and the host reads
  the plugin's static files, not environment variables.
- **Generic names (PersonalizedMind/Agent/Brain):** free, but generic phrases rank poorly and say
  nothing about founders; kept in mind for the engine if it is ever released on its own.

## Consequences

`plugin/` becomes a template: its files say `{{id}}`, and only the assembled `dist/plugin` loads
in Claude Code. The model-only contract skill is named `coach`, not after the product, so a
rename touches no skill folder. The runtime reads `founder_coach/product.json` (generated,
committed, checked by CI) through `founder_coach.product`.

Renaming is one line plus a rebuild. A changed id still changes the install id: the release
script adds the old id to `renames` in `marketplace.json`, so Claude Code migrates existing
installs, and testers move their data folder once. Tests fail when a shipped file spells out
the id instead of using the placeholder or `founder_coach.product`.
