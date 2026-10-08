---
name: status
description: Shows the investor coach's state, the active goal and its policy, the latest holdings dates, what's due, the knowledge it covers and whether semantic search is ready. Use when the investor types /{{id}}:status.
disable-model-invocation: true
---

# Status

1. Call `coach_get_context`, `coach_corpus_status` and `coach_holdings` action `list`.
2. Report in this order, briefly:
   - **Due:** the Nudges (a stale or missing import, a Decision due for a revisit), each with what handles it.
   - **Goal:** the active Project by name, and the others if there are several (`/{{id}}:project` switches).
   - **Policy:** targets, band, limit, review interval; fields not set yet.
   - **Holdings:** each account with its "as of" date and value.
   - **Knowledge:** what the library covers; search mode (semantic, or keyword while models load).
   - **Memory file:** the store path.
3. If the store reports an error, say nothing has been deleted and give the fix it names (`{{id}} restore`).
