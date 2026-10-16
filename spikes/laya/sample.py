"""Step 1 of the Laya spike: draw the sample and read the two arms that need no new model call.

    python spikes/laya/sample.py [--close 2400] [--single 400]

Recomputes the tag Step's candidates exactly as `ytbrain tag` does (same embedder, Domain profiles,
hints and settings), then:

  close calls   (2+ candidates)  stratified by candidate set, proportional; the deepseek arm is the
                                 cached tie-break answer (nothing is asked; a close call without a
                                 decided cached answer is left out and counted)
  single        (1 candidate)    stratified by that Domain, proportional; decided by embeddings alone
  inherited     (0 candidates)   not sampled: they take their Document's Domains, no classifier involved

Writes data/spikes/laya/sample.jsonl and sample_meta.json. Spends nothing.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT, REPO, SAMPLE, SEED, TEXT_CHARS, write_jsonl  # noqa: E402

sys.path.insert(0, str(REPO))


def allocate(sizes: dict, n: int) -> dict:
    """Proportional allocation with largest remainders: sums to min(n, total), never above a stratum."""
    total = sum(sizes.values())
    if total <= n:
        return dict(sizes)
    raw = {k: n * v / total for k, v in sizes.items()}
    got = {k: min(sizes[k], int(r)) for k, r in raw.items()}
    for k in sorted(raw, key=lambda k: (-(raw[k] - int(raw[k])), str(k))):
        if sum(got.values()) >= n:
            break
        if got[k] < sizes[k]:
            got[k] += 1
    return got


def draw(groups: dict, n: int, rng: random.Random) -> list:
    alloc = allocate({k: len(v) for k, v in groups.items()}, n)
    out = []
    for k in sorted(groups, key=str):
        out += rng.sample(sorted(groups[k], key=lambda r: r["item_id"]), alloc[k])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--close", type=int, default=2400)
    ap.add_argument("--single", type=int, default=400)
    a = ap.parse_args()

    from ytbrain import tagging as T
    from ytbrain.cli import _doc_tags, _domains, _tag_inputs, _tag_model
    from ytbrain.config import MANIFEST_DB
    from ytbrain.manifest import Manifest

    got = _tag_inputs("laya sample")
    if isinstance(got, int):
        return got
    _store, rows, vectors, embed = got
    reg = _domains()
    s, model, db = T.load_settings(), _tag_model(), T.TagDB()
    docs = sorted({r["doc_id"] for r in rows})
    hints = {d: v[0] for d, v in _doc_tags(Manifest(MANIFEST_DB), docs, default=False).items()}
    prof = T.profile_texts(reg)
    pvecs = embed([t for _, t in prof])
    scores = T.score_matrix(vectors, [(d, v) for (d, _), v in zip(prof, pvecs)], reg.names)

    close, single = defaultdict(list), defaultdict(list)
    n = Counter()
    pending = []
    for r, sc in zip(rows, scores):
        c = T.candidates(sc, set(hints.get(r["doc_id"]) or ()), s)
        text = (r.get("text") or "")
        row = {"item_id": r["item_id"], "doc_id": r["doc_id"], "kind": r.get("kind"), "source_id": r.get("source_id"),
               "text": text[:TEXT_CHARS], "candidates": c, "scores": {d: round(v, 4) for d, v in sc.items()},
               "hinted": sorted(hints.get(r["doc_id"]) or ()), "embedding": c[:1]}
        if not c:
            n["inherited"] += 1
        elif len(c) == 1:
            row.update(stratum="single", deepseek=None, production=c[:1])
            single[c[0]].append(row)
            n["single"] += 1
        else:
            row["_key"] = T.tiebreak_key(text, c, reg, model)
            pending.append(row)
    have = db.cached([r["_key"] for r in pending])
    for row in pending:
        ans = have.get(row.pop("_key"))
        if ans is None:
            n["close_not_cached"] += 1
        elif not ans or not set(ans) <= set(row["candidates"]):
            n["close_undecided"] += 1
        else:
            row.update(stratum="close", deepseek=list(ans), production=list(ans))
            close[tuple(sorted(row["candidates"]))].append(row)
            n["close"] += 1
    if not close:
        print("laya sample: no close call has a cached tie-break answer for the current settings and model "
              f"({model}); run `ytbrain tag` first", file=sys.stderr)
        return 1

    rng = random.Random(SEED)
    picked = draw(close, a.close, rng) + draw(single, a.single, rng)
    write_jsonl(SAMPLE, picked)
    meta = {"seed": SEED, "tag_model": model, "settings": json.loads(s.key()), "domains": reg.names,
            "population": dict(n),
            "strata_close": {"|".join(k): len(v) for k, v in sorted(close.items())},
            "strata_single": {k: len(v) for k, v in sorted(single.items())},
            "sampled": dict(Counter(r["stratum"] for r in picked)),
            "deepseek_two_domains": sum(1 for v in close.values() for r in v if len(r["deepseek"]) > 1)}
    (OUT / "sample_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"laya sample: {len(rows)} item(s): {n['close']} decided close call(s), {n['single']} single, "
          f"{n['inherited']} inherited; left out {n['close_undecided']} undecided and {n['close_not_cached']} "
          f"uncached close call(s)")
    print(f"laya sample: drew {meta['sampled']} into {SAMPLE}")
    if n["close_not_cached"] > 0.02 * max(1, n["close"]):
        print("laya sample: warning: many close calls have no cached answer; the tag settings or model may have "
              "changed since the last `ytbrain tag`", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
