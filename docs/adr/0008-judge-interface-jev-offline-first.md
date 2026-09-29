---
status: accepted, amended 2026-09-24
---
# Typed judgements go through one Judge interface; Jev is used for offline batch work first

Several tasks are bounded judgements, not generation: which Stage(s) and Topic(s) a piece of
Advice fits, whether a Verified quote actually *supports* its Advice, whether a candidate is
relevant to an eval question, and later whether two pieces of Advice say the same thing or
contradict. They all go through one `Judge` interface with two backends — TypeSafe's Jev
(typed Choice/Score/Noul answers with calibrated probabilities, ~0.1-0.5 s, $0.042 per million
input tokens, no output cost, not trained on customer requests) and an LLM (the extract model).
Jev is adopted now for **offline batch Steps only** (`enrich`, support check, eval labeling),
where its cost and calibration help most and a wrong answer is caught by thresholds or human
review. Query-time uses (routing, reranking) stay with the host model and a local
cross-encoder until measured, keeping the MCP server model-free [ADR-0002].

## Considered Options

- **Wait until M5 (previous plan):** leaves 59% of Advice without a Stage through the whole
  coach build, although classifying it is exactly Jev's task shape and costs cents.
- **Jev everywhere, including query time:** a one-week-old closed API on the critical path of
  every answer; no evidence yet on our data.

## Consequences

Every stored judgement records backend, version and probability, so switching backends
re-runs one Step. Only public corpus text is ever sent to a Judge — never Founder data
[ADR-0006]. Jev is dropped for a task if it trails the LLM backend by more than 3 points on
the labelled set or its API proves unstable.

## Amendment 2026-09-24: Jev parked, LLM backend first

Eval labelling (M2) came first and uses LLM judges over OpenRouter from families other than
the generator's; no Jev code exists. To ship the private beta sooner (phase3-plan §0),
enrichment uses the LLM backend only and the shared `Judge` interface is built when a second
backend earns a place. Jev stays an optional experiment, measured on the same labelled
sample with the same 3-point rule before it's adopted for any task.
