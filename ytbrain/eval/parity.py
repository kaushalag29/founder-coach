"""Neutral-prompt parity on startup talks (ADR-0018, M6c acceptance L5).

The subject-neutral prompt replaces the startup one for new founder content only if it is as good on
founder content: on a fixed sample of startup talks already extracted with the startup prompt, each is
extracted again with the neutral prompt into a shadow file (data/eval/parity/, never the Library), both
records are verified the same way, and the neutral one must keep the verifier pass rate within 2 points
and at least 80 % of the Verified items. The Library is untouched; the result (data/eval/neutral-parity.json)
is what `versions.variant_for` reads. Resumable: a talk with a shadow file for this neutral version is not
extracted again.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

PASS_RATE_SLACK = 0.02
MIN_ITEM_RATIO = 0.80
ITEM_FIELDS = ("highlights", "advice_atoms", "facts", "rules")


def sample(doc_ids: list[str], n: int, seed: int = 7) -> list[str]:
    return sorted(doc_ids, key=lambda d: hashlib.sha256(f"{seed}:{d}".encode()).hexdigest())[:n]


def verified_items(record: dict) -> int:
    """Assertions whose quote was found (verify sets match_score; ok when it reached the threshold)."""
    thr = ((record.get("extraction_meta") or {}).get("verification") or {}).get("threshold", 0)
    return sum(1 for f in ITEM_FIELDS for it in record.get(f) or []
               if it.get("match_score") is not None and it["match_score"] >= thr)


def run(docs: list[str], load: Callable[[str], tuple[dict, dict]], extract: Callable[[dict, dict], dict],
        verify: Callable[[dict, list[dict]], dict], out_dir: Path, stamp: str, max_cost: float | None = None,
        say=print) -> dict:
    """`load(doc_id)` -> (its startup record, its transcript); `extract(transcript, record)` -> the neutral
    record (a dict, extraction_meta.calls carrying costs); `verify(record, utterances)` mutates and reports.
    Returns the summary written to neutral-parity.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, spent = [], 0.0
    for i, doc in enumerate(docs, 1):
        old, tr = load(doc)
        shadow = out_dir / f"{doc}.json"
        new = None
        if shadow.exists():
            try:
                cached = json.loads(shadow.read_text())
                if cached.get("_stamp") == stamp:
                    new = cached["record"]
            except (OSError, ValueError, KeyError):
                new = None
        if new is None:
            if max_cost is not None and spent >= max_cost:
                say(f"parity: stopped at the spend cap (${spent:.3f}); re-run to continue")
                break
            say(f"parity: [{i}/{len(docs)}] {doc}: extracting with the neutral prompt")
            new = extract(tr, old)
            spent += sum(float(c.get("cost") or 0) for c in (new.get("extraction_meta") or {}).get("calls") or [])
            shadow.write_text(json.dumps({"_stamp": stamp, "record": new}))
        utts = tr.get("utterances") or []
        a, b = copy.deepcopy(old), copy.deepcopy(new)
        ra, rb = verify(a, utts), verify(b, utts)
        rows.append({"doc_id": doc, "startup_pass_rate": round(ra["pass_rate"], 3),
                     "neutral_pass_rate": round(rb["pass_rate"], 3),
                     "startup_items": verified_items(a), "neutral_items": verified_items(b)})
    return summarize(rows, len(docs), stamp, spent)


def summarize(rows: list[dict], wanted: int, stamp: str, spent: float = 0.0) -> dict:
    if not rows:
        return {"passed": False, "neutral": stamp, "n": 0, "wanted": wanted, "why": "no talk compared yet"}
    sp = sum(r["startup_pass_rate"] for r in rows) / len(rows)
    npr = sum(r["neutral_pass_rate"] for r in rows) / len(rows)
    si = sum(r["startup_items"] for r in rows)
    ni = sum(r["neutral_items"] for r in rows)
    complete = len(rows) >= wanted
    ok_rate = npr >= sp - PASS_RATE_SLACK
    ok_items = ni >= MIN_ITEM_RATIO * si
    why = [] if complete else [f"only {len(rows)} of {wanted} talks compared"]
    why += [] if ok_rate else [f"verifier pass rate {npr:.1%} vs {sp:.1%} (more than {PASS_RATE_SLACK:.0%} lower)"]
    why += [] if ok_items else [f"{ni} Verified items vs {si} (below {MIN_ITEM_RATIO:.0%})"]
    return {"passed": complete and ok_rate and ok_items, "neutral": stamp, "n": len(rows), "wanted": wanted,
            "startup_pass_rate": round(sp, 4), "neutral_pass_rate": round(npr, 4), "startup_items": si,
            "neutral_items": ni, "cost": round(spent, 4), "why": "; ".join(why), "talks": rows}
