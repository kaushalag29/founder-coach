---
status: accepted
---
# The founder store keeps current-state tables plus an append-only change log, and only confirmed writes reach it

The coach remembers a Founder's profile, Goals, Commitments, Decisions and Check-ins
(ADR-0006). We store the current state in plain tables and add one append-only `changes`
log: every create, update, confirmation and status change, with its before/after, the tool
or Playbook that made it, and a unique request id. Every write is a single transaction that
includes its log row, and it happens only after the Founder has approved the exact text
(the skills require it and the tool descriptions say so). Export and forget are
command-line actions, never MCP tools.

## Considered Options

- **History columns per table** (`valid_from`/`superseded_by` everywhere): the same idea
  implemented five times, and still no record of *who* changed what, or of confirmations.
- **Event sourcing** (the log is the only truth, state is rebuilt): more than a single-user
  local coach needs, and it makes simple reads slow and migrations harder.
- **Silent memory** (the model saves whatever it infers): fewer steps, but it saves wrong
  or sensitive things without the Founder knowing. The research on agent memory and our
  privacy stance both favour explicit, confirmed writes.
- **Forget as a tool:** one bad model decision could erase months of history.

## Consequences

Undo, a "recent changes" section in FOUNDER.md, staleness per profile fact and idempotent
retries all come from one mechanism. The log grows by a few rows per session, negligible
for one Founder. Profile facts additionally keep their superseded values, so "what did we
believe in March" is answerable. A hosted multi-founder version keeps the same shape.
