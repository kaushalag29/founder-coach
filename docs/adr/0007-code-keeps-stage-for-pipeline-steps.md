---
status: accepted
---
# Existing code and databases keep "stage" for pipeline Steps

The glossary reserves **Stage** for a startup's Stage and calls pipeline work units **Steps**,
but the manifest's `stage_state` table, its `stage` column and identifiers like
`invalidate_stage` keep their names. Renaming persisted names would force a migration of
every user's `data/manifest.db` for no behavioural gain. New code, docs, CLI help and
messages use "Step"; existing identifiers are left alone and are not "fixed" in passing.
