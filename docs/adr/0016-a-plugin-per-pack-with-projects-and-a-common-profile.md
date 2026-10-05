---
status: proposed
---
# A plugin per Pack; memory isolated per Project, with one shared Common profile

Amends ADR-0015 ("one plugin and one MCP server host every installed Pack") and ADR-0012 (the data
folder follows the product id). Each Pack installs as its own plugin (`founder-coach`, `systems-coach`,
`investor-coach`) with its own skills, MCP server, Knowledge pack and coaching loop. Inside a Pack, the
unit of isolation is a **Project** (a company, a system, a portfolio): its profile facts, Goals,
Decisions, Commitments, Check-ins and holdings never appear in another Project. What is true of the
person in every Pack (name, timezone, role, how they like answers) lives once, in the **Common
profile**, which every Pack reads and writes only on an explicit yes.

Storage, under one engine home shared by every Pack plugin (`~/.ytbrain/` working name, `YTBRAIN_HOME`
overrides; the engine id is one configured value, like ADR-0012's product id):

```
~/.ytbrain/
  you.db                          Common profile + its change log (ADR-0011 shape)
  models/                         embedder and reranker, one copy for every Pack
  <pack>/                         founder/, systems/, investor/
    pack/                         the installed Knowledge pack
    projects/<project-id>.db      Pack memory of one Project: facts, Goals, Decisions, ... + its change log
    last-project                  the last-used Project id: a new session's default (one line, written atomically)
    exports/
```

One SQLite file per Project gives physical isolation: no query can join two Projects, a Project is
exported, backed up or forgotten as one file, and a schema migration runs per file. `you.db` is small
and opened by several plugins at once (WAL, busy timeout); writes to it are rare and confirmed.

Rules that make the isolation hold:
- Every Pack-memory read and write names its Project; the server rejects a write whose Project is not
  the active one (a session that switched elsewhere cannot write into the old Project).
- The active Project belongs to the session (the server holds it in memory), so two sessions in two
  Projects never read each other's memory; every context response names it, and every save prompt names
  the Project it saves to.
- Switching Projects is an explicit request; context lists the Pack's Project names, never their contents.
  Two Projects are read together only when the person's current message names both: summaries only,
  nothing saved, no Nudges from it.
- Knowledge may cross Packs, memory never does: context lists the other installed Packs and their Domains,
  and the host may search another Pack's Knowledge pack through that plugin's search tool, never its
  memory tools.
- An investor Project is a goal with its own Investment Policy Statement, holding several accounts.
- Promotion from a Project to the Common profile happens only on the person's yes, and only for fields
  the Pack declares promotable (`pack.yaml: common_fields`). The investor Pack declares none: holdings,
  amounts and its Investment Policy Statement never leave their Project.
- No Pack reads another Pack's folder.

## Considered Options

- **One plugin hosting every Pack (ADR-0015 as proposed):** one server, cross-Pack search, but every
  conversation carries every Pack's tools and skills, and the loops (a founder's weekly Check-in, an
  investor's quarterly review) share one surface the person wanted kept apart.
- **One database with a `project_id` column:** simplest code, but isolation rests on every query
  remembering a WHERE clause; forgetting one Project means a careful DELETE instead of removing a file.
- **One database per Pack with ATTACH for the Common profile:** ATTACH is limited (10 by default) and a
  transaction across attached files is not atomic; per-file isolation inside a Pack is still needed.
- **Silent memory sharing across Projects or Packs:** convenient, but it leaks one company's plans into
  another's advice and money details into unrelated Packs.

## Consequences

Today's `~/.founder-coach/founder.db` migrates forward once, with a backup: rows go to
`founder/projects/<company-slug>.db`, the few person-level facts to `you.db`, each move logged in the
change log; a re-run is a no-op and the old folder is left untouched. A question spanning two Packs is
answered by the host searching both plugins' knowledge when both are installed; no server searches across
Packs, and no Pack reads another's memory.
Project isolation becomes a release gate (G7) for every Pack.
