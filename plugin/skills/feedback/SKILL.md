---
name: feedback
description: Saves the Founder's Feedback that a coach answer or saved record was wrong, so it can become a test case. Use when the founder types /{{id}}:feedback, optionally with what went wrong.
argument-hint: "[what was wrong]"
disable-model-invocation: true
---

# Feedback

Follow the coaching contract of the coach skill: show the exact record and save only on a yes.

What the Founder said was wrong: $ARGUMENTS

1. **Find the answer it's about.** Use the most recent coach answer (or saved record) in this conversation, unless the Founder points to another. If there is none, ask which answer they mean and stop until they say.
2. **Build the record** from the conversation, verbatim, never reworded:
   - `question`: what the Founder asked or did;
   - `answer`: the coach's answer; if it's very long, keep the part the Feedback is about;
   - `cited`: the item_ids that answer cited, and `searches`: the queries sent to `coach_search` for it;
   - `category`, one of: `wrong_citation` (a quote doesn't say what the answer claims), `missed_gap` (it answered where it should have said the talks don't cover it), `weak_advice` (generic, wrong for their Stage, or agreed with a weak plan), `wrong_memory` (saved, recalled or updated something about them wrongly), `other`. Pick the closest from their words; if unsure, ask with your best guess as the default;
   - `note`: what they said was wrong, and `expected`: what they expected instead, if they said.
3. **Show it** in a few lines: category, the question, the first lines of the answer, what was cited, their note. Ask whether to save it.
4. **Save on a yes** with one `coach_feedback` call and a fresh `request_id`.
5. **Close** with where it lives: on this machine, in the coach's store. To share it, the export writes one file holding their Feedback (their questions and the coach's answers, nothing else from the store) for them to send to the maintainer. If they want that file now, run with Bash `uvx --from "${CLAUDE_SKILL_DIR}/../.." {{id}} feedback export` and give them the path it prints. In the same message, offer (once, no pressure) the usage log too: which tools ran, when, how long, and how they went, never their words; on a yes run `uvx --from "${CLAUDE_SKILL_DIR}/../.." {{id}} usage export` and give that path as well. Then carry on with what they were doing.
