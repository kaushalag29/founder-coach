---
status: accepted
---
# Founder state is local, separate from the corpus, and keyed by founder id

Founder profile, Goals, Commitments, Decisions and Check-ins are private and live in their
own local store, never in `data/` (the rebuildable corpus) and never uploaded by ytbrain.
Every row carries a founder id even though one Founder per installation is the only
supported mode, so a hosted multi-founder version is a storage change, not a data-model
rewrite.
