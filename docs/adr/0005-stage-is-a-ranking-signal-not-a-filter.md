---
status: accepted
---
# Stage is a ranking signal, not a hard filter

59% of Advice has no Stage and Document-level Stages are broad (78% of Talks list "idea"),
so filtering by Stage would silently hide most relevant Advice. Retrieval boosts Knowledge
items matching the Founder's Stage and only filters when a Playbook explicitly asks. A
later enrichment Step classifies Advice Stage and Topic; the boost becomes stronger as
coverage improves, measured on the eval set.
