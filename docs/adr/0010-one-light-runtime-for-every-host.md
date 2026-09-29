---
status: accepted
---
# The coach ships as one light runtime, launched with uvx, with open-standard skills and thin per-host manifests

The coach must install in one step in Claude Code and Claude Desktop, and later in Cursor, Kiro,
Codex, VS Code/Copilot and Gemini CLI. We ship a separate runtime package (`founder_coach`)
containing only the MCP server, the Knowledge pack reader, search and the founder store:
no torch, no yt-dlp, no LanceDB, with ONNX query models and the pack inside the wheel. Every host
starts it the same way (`uvx founder-coach@<pinned>`). The Playbooks are written once as Agent
Skills (SKILL.md) and generated into MCP prompts, and each host gets only a small manifest.
The founder store lives in one user-level folder, so every host on a machine shares the same
founder memory.

## Considered Options

- **One package with the pipeline:** a plugin install would pull in about 2 GB (torch, yt-dlp,
  LanceDB) for a server that only needs to search 13k items.
- **Per-host server builds or host-specific plugin-root paths:** three different root variables
  (`CLAUDE_PLUGIN_ROOT`, `PLUGIN_ROOT`, `CURSOR_PLUGIN_ROOT`) and N release paths.
- **Hosted remote server:** needs accounts, OAuth and per-founder storage, and it is the
  "Commercial Service" our licence reserves. Deferred, not rejected.

## Consequences

Search logic lives in one module used by both packages, so eval numbers describe what founders
actually run. `uv` becomes a prerequisite outside Claude Desktop's uv bundle, and setup checks
for it. The store's schema is a public contract: migrations are forward-only and numbered.

The private beta (phase3-plan §0) launches the same runtime from the plugin folder
(`uvx --from ${CLAUDE_PLUGIN_ROOT} founder-coach`) with the pack inside that folder, so nothing
is published to PyPI before the public release. The pinned `uvx founder-coach@X.Y.Z` launch
applies from the public release on. The ONNX query models are not bundled in either case:
`founder-coach warmup` (run by setup) downloads them once (about 0.2 GB).
