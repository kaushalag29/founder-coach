---
name: decision-record
description: Records a technical decision (context, options considered, the choice and its consequences) with its Citations in the coach's memory, and on a yes writes it as an ADR file in the repository. Use when an engineer has made a technical decision or wants one documented, asks for an ADR, or types /{{id}}:decision-record.
argument-hint: "[the decision]"
---

# Decision record

The decision: $ARGUMENTS

Follow the coaching contract of the coach skill: show the exact text, save only on a yes.

## Checklist

1. **Context.** `coach_get_context` (unless already called): the active Project, and recent Decisions this one may replace or contradict (say so if it does).
2. **Gather** from the conversation, asking only for what is missing, one question at a time: the context (the problem and the forces: scale, SLOs, constraints), the options considered, the choice, and its consequences (what gets easier, what gets harder, what to watch). Use the Engineer's words.
3. **Support it.** One `coach_search` for the chosen approach's main trade-off (`domains` `system-design` or `coding`). Add Citations only from hits that support the reasoning; with none, the record says it rests on the team's judgment.
4. **Show the record** under the Project's name: the decision in one line, the reasoning in a few lines, Citations, and a revisit date when the Engineer gives one ("when we pass 10k rps", "2027-03-01").
5. **Save on a yes** with `coach_record` kind `decision` (`text`, `reasoning`, `citations`, `revisit_on` as YYYY-MM-DD if dated) and the Project id as `project`.
6. **Offer the ADR file**, once: "Also write it as an ADR in the repo?" On a yes, find where ADRs live (the profile's `workspace` `adrs`, else `docs/adr/` or `doc/adr/` if it exists, else ask), take the next free number, and show the exact file (name and full text) in the format below. Write it only after a second yes, and only with the host's file tool. Never overwrite an existing file.

## ADR format

```
# NNNN. <Decision title>

Date: YYYY-MM-DD
Status: Accepted

## Context
<the problem and the forces>

## Options considered
- <option>: <why not, or why>

## Decision
<what was decided>

## Consequences
<what gets easier, what gets harder, what to watch; when to revisit>

## Sources
<Citations, as the coach skill writes them; none: "the team's judgment">
```

## Gotchas

- The memory record and the file are two separate yeses: saving one never implies the other.
- An ADR in the repo may be read by the whole team: it holds the decision and its reasons only, never other memory (Goals, profile facts) unless the Engineer asks.
- If the repo has its own ADR template, follow that instead and say so.
