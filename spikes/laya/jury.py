"""Step 2 of the Laya spike: the silver labels, from a blind panel of three judges.

    python spikes/laya/jury.py [--max-cost 3] [--batch 20]

Each judge (YTBRAIN_EVAL_JUDGES: three models from families other than the tie-break's, checked by the
eval preflight) labels every sampled text against ALL the Domains, never seeing the candidates, the
embedding scores or any arm's answer. The Domain list is shuffled per call so position can't decide.
Answers go to data/spikes/laya/jury.jsonl as they arrive; a re-run asks only what is missing.
The panel's majority (2 of 3) is the silver label (score.py); this script only collects votes.
"""
from __future__ import annotations

import argparse
import hashlib
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import JURY, REPO, SAMPLE, append_jsonl, read_jsonl  # noqa: E402

sys.path.insert(0, str(REPO))

PROMPT = """You label short texts by subject. For each numbered text, choose the Domain it is MAINLY about
(`primary`) from the list below. Give a `secondary` Domain only when the text is equally about a second
one; otherwise leave it null. Use only the Domain names listed, exactly as written. If none fits well,
choose the closest.

Domains:
{domains}

Texts:
{texts}

Answer with JSON only: {{"answers": [{{"n": 1, "primary": "<domain>", "secondary": null}}, ...]}}, one entry
per text."""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-cost", type=float, default=3.0, help="USD, a hard stop (default 3)")
    ap.add_argument("--batch", type=int, default=20, help="texts per call (default 20)")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    from pydantic import BaseModel

    from ytbrain.cli import _domains
    from ytbrain.config import EVAL_JUDGES
    from ytbrain.eval import llm
    from ytbrain.eval.build import Env, preflight
    from ytbrain.extract.runner import BackendUnavailable

    class One(BaseModel):
        n: int
        primary: str
        secondary: str | None = None

    class Batch(BaseModel):
        answers: list[One]

    items = read_jsonl(SAMPLE)
    if not items:
        print("laya jury: no sample yet: run spikes/laya/sample.py first", file=sys.stderr)
        return 1
    reg = _domains()
    names = list(reg.names)
    env = Env(judges=list(EVAL_JUDGES))
    err = preflight(env, generator=False)
    if err:
        print(f"laya jury: {err}", file=sys.stderr)
        return 1
    judges = list(env.judges)
    if len(judges) < 3:
        print(f"laya jury: needs three judges for a majority; have {', '.join(judges)}", file=sys.stderr)
        return 1
    done = {(r["judge"], r["item_id"]) for r in read_jsonl(JURY)}
    jobs = []
    for j in judges:
        todo = [it for it in items if (j, it["item_id"]) not in done]
        jobs += [(j, todo[i:i + a.batch]) for i in range(0, len(todo), a.batch)]
    if not jobs:
        print(f"laya jury: every item already has a vote from {', '.join(judges)}")
        return 0

    def describe(order):
        return "\n".join(f"- {d}: {reg.get(d).description}"
                         + (f" Not about: {reg.get(d).not_about}" if reg.get(d).not_about else "") for d in order)

    def work(job):
        judge, part = job
        seed = int(hashlib.sha256((judge + part[0]["item_id"]).encode()).hexdigest()[:8], 16)
        order = random.Random(seed).sample(names, len(names))
        texts = "\n\n".join(f"[{i}]\n{it['text']}" for i, it in enumerate(part, 1))
        ans = llm.ask(PROMPT.format(domains=describe(order), texts=texts), Batch, judge)
        budget.add(ans.cost)                          # in the worker: the next call sees the spend at once
        return ans

    totals = {"calls": 0, "cost": 0.0, "invalid": 0, "failed": 0}

    def on_result(job, ans, e):
        judge, part = job
        if e is not None or ans.value is None:
            totals["failed"] += 1
            return
        totals["calls"] += ans.calls
        totals["cost"] += ans.cost
        by_n = {x.n: x for x in ans.value.answers}
        out = []
        for i, it in enumerate(part, 1):
            x = by_n.get(i)
            p = x.primary.strip() if x and x.primary else None
            sec = x.secondary.strip() if x and x.secondary else None
            if p not in names:
                totals["invalid"] += 1
                continue                              # asked again on the next run
            out.append({"judge": judge, "item_id": it["item_id"], "primary": p,
                        "secondary": sec if sec in names and sec != p else None})
        append_jsonl(JURY, out)

    budget = llm.Budget(a.max_cost)
    print(f"laya jury: {len(jobs)} call(s) across {', '.join(judges)}, {a.workers} at a time, up to ${a.max_cost:g}",
          flush=True)
    try:
        stopped = llm.run_parallel(jobs, work, on_result, workers=a.workers, budget=budget, label="laya jury")
    except BackendUnavailable as e:
        stopped = f"backend: {e}"
    print(f"laya jury: {totals['calls']} call(s), ${totals['cost']:.3f}; {totals['invalid']} answer(s) outside the "
          f"Domain list and {totals['failed']} failed call(s) are asked again on the next run")
    if stopped:
        print(f"laya jury: stopped ({stopped}); re-run to finish", file=sys.stderr)
        return 1
    have = read_jsonl(JURY)
    missing = len(items) * len(judges) - len({(r['judge'], r['item_id']) for r in have})
    if missing:
        print(f"laya jury: {missing} vote(s) still missing: re-run to finish", file=sys.stderr)
        return 1
    print(f"laya jury: complete: {len(items)} item(s) x {len(judges)} judge(s) in {JURY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
