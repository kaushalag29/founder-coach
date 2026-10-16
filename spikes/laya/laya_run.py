"""Step 3b of the Laya spike: Laya's two arms, offline, in its own process.

    python spikes/laya/laya_run.py [--device auto|mps|cpu] [--batch 32]

Runs the English checkpoint (data/spikes/laya/model, pinned by common.LAYA_REVISION) on every sampled
item twice: choosing among the item's embedding candidates, and among all Domains. Imports nothing
from ytbrain and reads no settings or keys; Hugging Face is switched offline before transformers loads,
and the reviewed code and the weights are checked against the hashes below before anything runs.

Writes data/spikes/laya/laya.jsonl: per item, log-probabilities over each arm's options after Laya's own
shipped temperature (score.py refits its own on 30% of items). Resumable.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import sys
import time
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"            # nothing below may reach the network
os.environ["TRANSFORMERS_OFFLINE"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import LAYA, MODEL_DIR, REPO, SAMPLE, append_jsonl, read_jsonl  # noqa: E402

# The files read on 2026-10-15 (spikes/laya/README.md, "Code review"); anything else refuses to run.
REVIEWED = {
    "rl_common.py": "8d83611d480c971d640a7b7d3aa2f2219c5e8455e9cc2329fd073681bd8be23e",
    "rl_agent_api.py": "be3b46819c9999c3ef88e0f2ecf6d3ab1cdfed1d9a8b466fc89811e34d44031b",
    "rl_agent_config.json": "ae287b56bbcf5f8c4f4541ae9dfd00c914c4c48b940b8398c3058af37ba92bbd",
    "encoder/config.json": "bf3ab80598fdccf414855a2ce80f22859e4492d06ca8a62ddd1cfb63972f8979",
    "tokenizer/tokenizer.json": "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30",
    "tokenizer/tokenizer_config.json": "50044de60daaa73df97d262e15a40d4faf0160e7d742df64b377877a1320dd12",
    "model.safetensors": "891102d372688fc2a094dac56a384bc537b87c63f21f9f3dac0be2b7cbc8d86c",
}
INSTRUCTIONS = "Which subject is this text mainly about?"
# The checkpoint was trained with head_max_len 192, which squeezes seven "name: description" options to 25
# tokens each and cuts finance and investment off mid-description; the model card's advice for many options
# is to raise it. 256 fits all seven whole (238 tokens) and still leaves 254 for the text (longest sampled: 249).
HEAD_MAX_LEN = 256


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--batch", type=int, default=32, help="sequences per forward pass")
    a = ap.parse_args()

    for name, want in REVIEWED.items():
        p = MODEL_DIR / name
        if not p.exists():
            hint = " (run fetch_model.py --weights)" if name.endswith(".safetensors") else " (run fetch_model.py)"
            print(f"laya run: {p} is missing{hint}", file=sys.stderr)
            return 1
        if sha256(p) != want:
            print(f"laya run: {name} is not the reviewed file; refusing to run it", file=sys.stderr)
            return 1

    items = read_jsonl(SAMPLE)
    if not items:
        print("laya run: no sample yet: run sample.py first", file=sys.stderr)
        return 1
    import json

    import numpy as np
    import torch
    sys.path.insert(0, str(MODEL_DIR))
    from rl_agent_api import RLAgent
    from rl_common import QTYPES, build_sequence, collate_items, render_options, temp_bucket

    meta = json.loads((SAMPLE.parent / "sample_meta.json").read_text())
    domains = sorted(meta["domains"])
    import yaml
    reg = yaml.safe_load((REPO / "domains.yaml").read_text())["domains"]
    desc = {d: (reg[d].get("description") or d) for d in domains}     # what the tie-break model is shown
    dev = a.device
    if dev == "auto":
        dev = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    t0 = time.time()
    agent = RLAgent(str(MODEL_DIR), device=dev)
    tok, cfg, model = agent.tok, agent.cfg, agent.model
    print(f"laya run: loaded on {dev} in {time.time() - t0:.0f}s", flush=True)

    def question(opts):
        return {"t": "choice", "ins": INSTRUCTIONS, "crit": {d: desc[d] for d in opts}}

    def scored(seqs):
        """[(ids, markers, k)] -> log-probabilities after the shipped temperature, one array per sequence."""
        out = []
        for i in range(0, len(seqs), a.batch):
            part = seqs[i:i + a.batch]
            b = collate_items([[{"ids": s, "markers": m, "qtype": QTYPES["choice"], "target": [0.0] * k, "label": -1,
                                 "episode": 0, "ep_step": 0, "ep_len": 1, "src": "spike"} for s, m, k in part]],
                              tok.pad_token_id)
            with torch.no_grad():
                logits, _ = model(b["input_ids"].to(agent.device), b["attention_mask"].to(agent.device),
                                  b["marker_pos"].to(agent.device), b["marker_mask"].to(agent.device),
                                  b["qtype"].to(agent.device))
            logits = logits.float().cpu().numpy()
            for r, (_, _, k) in enumerate(part):
                t = agent.temperature_by_options.get(temp_bucket(QTYPES["choice"], k),
                                                     agent.temperature[QTYPES["choice"]])
                z = logits[r, :k] / t
                z = z - z.max()
                out.append(z - math.log(float(np.exp(z).sum())))
        return out

    done = {r["item_id"] for r in read_jsonl(LAYA)}
    todo = [it for it in items if it["item_id"] not in done]
    print(f"laya run: {len(todo)} item(s) to score ({len(done)} already done)", flush=True)
    chunk, t0 = 64, time.time()
    for c in range(0, len(todo), chunk):
        part = todo[c:c + chunk]
        seqs, keys = [], []
        for it in part:
            for arm, opts in (("cand", sorted(it["candidates"])), ("all", domains)):
                q = question(opts)
                ids, markers = build_sequence(tok, {"text": it["text"]}, q, cfg["max_len"], HEAD_MAX_LEN)
                if len(markers) != len(render_options(q)):
                    print(f"laya run: {it['item_id']}: options did not fit; skipped", file=sys.stderr)
                    continue
                seqs.append((ids, markers, len(opts)))
                keys.append((it["item_id"], arm, opts))
        rows: dict[str, dict] = {}
        for (iid, arm, opts), lp in zip(keys, scored(seqs)):
            rows.setdefault(iid, {"item_id": iid})[arm] = {d: round(float(v), 5) for d, v in zip(opts, lp)}
        append_jsonl(LAYA, [r for r in rows.values() if "cand" in r and "all" in r])
        n = c + len(part)
        rate = n / max(time.time() - t0, 1e-9)
        print(f"\r  laya run: {n}/{len(todo)} · {rate:.1f} item(s)/s · ETA {(len(todo) - n) / max(rate, 1e-9) / 60:.1f} min",
              end="", flush=True)
    print(f"\nlaya run: done in {(time.time() - t0) / 60:.1f} min -> {LAYA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
