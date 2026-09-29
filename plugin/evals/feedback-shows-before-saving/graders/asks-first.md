---
type: llm
criteria: |
  PASS if the reply either shows the Feedback it would save (a category such as wrong_citation,
  the question and the answer it is about, and the Founder's note) and asks whether to save it,
  or asks which answer the Founder means because it can't see that answer in the conversation.
  FAIL if it claims the Feedback is already saved, or argues with the Founder instead of recording it.
---
