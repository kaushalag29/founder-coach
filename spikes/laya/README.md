# Spike: can Laya replace the tag Step's LLM tie-break?

A time-boxed experiment, not part of the pipeline: nothing here is imported by `ytbrain`, the plugin or
the tests. [Laya](https://huggingface.co/convaiinnovations/laya) is a 421M-parameter ModernBERT decision
model that picks among options without generating text. The question: on the tag Step's close calls,
is it as good as the `deepseek-v4-flash` tie-break, so tagging could run offline and free?

## Design (agreed 2026-10-15)

- **Arms**, all on the same items: embedding top-1 (no model), deepseek (cached answers, nothing re-asked),
  Laya choosing among the same candidates, Laya choosing among all Domains.
- **Silver labels without hand labelling**: three judges from other model families (`YTBRAIN_EVAL_JUDGES`)
  label every item blind, against all Domains, Domain order shuffled per call; the 2-of-3 majority is the
  label (a panel of judges, Verga et al. 2024, arXiv:2404.18796). Items with three different answers are
  left out and 50 of them are read by hand to check the panel.
- **Sample**: 2,400 close calls stratified by candidate set + 400 single-candidate items, proportional,
  fixed seed. 2,400 is what a 2-point non-inferiority margin needs at ~15% paired disagreement.
- **Metrics**: accuracy (Wilson CI), macro-F1, finance and investment F1, Cohen's kappa, paired bootstrap
  CI of the accuracy difference, exact McNemar, wrong-Pack moves; for Laya, ECE and Brier as shipped and
  with a temperature fitted on 30% of items (scored on the other 70%), and accuracy by confidence.

## Decision rules (fixed before the run, in score.py)

| Rule | Bar |
|---|---|
| The panel is reliable | Fleiss' kappa ≥ 0.6, else **inconclusive** |
| Non-inferior to deepseek | 95% CI lower bound of Δ accuracy > −2 pts |
| High-tier Domains | finance and investment F1 not more than 2 pts below deepseek (≥ 20 items) |
| Packs | under 1% of close calls moved to a wrong Pack |

Precedence: embedding top-1 passes → **drop the tie-break**; else Laya on all Domains passes (close calls
and whole pipeline) → **Laya replaces both stages**; else Laya on candidates passes → **Laya as the
tie-break**; else **keep deepseek**.

## Code review (2026-10-15, revision 55cf4c4)

Read in full before anything ran: `rl_agent_api.py`, `rl_common.py`, `email_utils.py`, the configs and
the tokenizer config. No network access (the encoder is built from its local config, `from_config`, and
the weights load with `safetensors.load_file`, so no pickle), no `trust_remote_code` or `auto_map`, no
subprocess, `exec` or `eval`; file reads are only its own config, tokenizer and weights. `laya_run.py`
refuses to run unless every one of those files matches the SHA-256 recorded at review, and switches
Hugging Face offline first. Only the English checkpoint is used (the repo also holds a multilingual and a
typed-decisions checkpoint: 2.37 GB in all).

## Run (from the repo root, the repo's own environment)

```
python spikes/laya/sample.py          # the sample + embedding and deepseek arms; spends nothing
python spikes/laya/jury.py            # the panel's votes; a few dollars at most (--max-cost 3); re-run to resume
python spikes/laya/fetch_model.py     # Laya's code and configs only, at the pinned revision, for review
python spikes/laya/fetch_model.py --weights   # the English checkpoint only (843 MB)
python spikes/laya/laya_run.py        # both Laya arms, offline, on MPS
python spikes/laya/score.py           # REPORT.md and the verdict
```

Everything written goes to `data/spikes/laya/` (git-ignored): the sample holds Library text, some of it
from private Books. Only REPORT.md's aggregate numbers leave that folder.

## Result (2026-10-15): keep deepseek; Laya rejected

Panel reliable (Fleiss' kappa 0.701; 2,733 of 2,800 items with a majority). On 2,342 close calls, against
the panel: deepseek 59.2%, Laya on candidates 45.3% (Δ −13.9 pts, CI −16.1 to −11.7, McNemar p 4e-33),
Laya on all Domains 36.0%, embedding top-1 37.4%. Investment F1: deepseek 0.62, Laya 0.23. No arm passes.
Laya's confidence doesn't rescue it: at ≥ 0.8 it answers 11% of close calls at 66%.

The larger finding is about the tagger itself: today's pipeline matches the panel's Domain on 62% of
items and its Pack on ~87%, and for 24% of close calls the panel's Domain is not among the embedding
candidates at all. The biggest Pack error is founder content (startup, gtm, leadership) placed in the
coding Pack. Reading 12 of those by hand, the panel was right on about 10. The embedding candidate stage,
not the tie-break, is the weak link: a follow-up for M7, not this spike.
