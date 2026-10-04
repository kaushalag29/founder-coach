---
type: llm
criteria: |
  The founder asks two things: weekly 1:1s (covered, with retrieved quotes) and sharding Postgres for a
  multi-tenant SaaS (system-design, which the context says the Library holds 0 items in). PASS if the answer
  gives the 1:1 advice with a citation AND says the Library has nothing on the database/sharding part yet
  (a Gap), without citing any talk for it. Brief general pointers on sharding are fine only if labelled as
  general knowledge. FAIL if it cites the 1:1 quotes for sharding, or answers sharding as if sourced.
---
