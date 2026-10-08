---
name: status
description: Shows the coding coach's state, what's saved about the system, what's due, the knowledge it covers and whether semantic search is ready. Use when the engineer types /{{id}}:status.
disable-model-invocation: true
---

# Status

1. Call `coach_get_context` and `coach_corpus_status`.
2. Report in this order, briefly:
   - **Due:** the Nudges, each with what handles it (`/{{id}}:setup`, a Goal past its date, a Decision due for a revisit).
   - **Project:** the active system by name, and the others if there are several (`/{{id}}:project` switches).
   - **System:** stack, scale and SLOs; stale facts marked "confirm?".
   - **Goals and Decisions:** the active Goals; the most recent Decisions.
   - **Knowledge:** what the library covers (Domains, books and talks); search mode (semantic, or keyword while models load, with the reason).
   - **Memory file:** the store path and `FOUNDER.md` location.
3. If the store reports an error, say nothing has been deleted and give the fix it names (`{{id}} restore`).
