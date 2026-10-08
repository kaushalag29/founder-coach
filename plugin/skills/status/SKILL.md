---
name: status
description: Shows the founder coach's state, what's saved about the Founder, what's due, the knowledge it covers and whether semantic search is ready. Use when the founder types /{{id}}:status.
disable-model-invocation: true
---

# Status

1. Call `coach_get_context` and `coach_corpus_status`.
2. Report in this order, briefly:
   - **Due:** the Nudges, each with the command that handles it (`/{{id}}:check-in`, `/{{id}}:setup`).
   - **Project:** the active one by name, and the others if there are several (`/{{id}}:project` switches).
   - **Founder:** Stage and one-liner; stale facts marked "confirm?".
   - **This week:** open Commitments; the active Goals.
   - **Knowledge:** talks and years covered; search mode (semantic, or keyword while models load, with the reason).
   - **Memory file:** the store path and `FOUNDER.md` location.
3. If the store reports an error, say nothing has been deleted and give the fix it names (`{{id}} restore`).
