"""Shared bits of the Laya spike (spikes/laya/README.md): paths, the JSONL helpers and the Pack map.

Everything the spike writes holds Library text, some of it from private Books, so it all goes under
data/spikes/laya/ (git-ignored, ADR-0014). Only aggregate numbers ever leave that folder.
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get("LAYA_SPIKE_OUT") or REPO / "data" / "spikes" / "laya")
SAMPLE = OUT / "sample.jsonl"        # one row per sampled item: text, candidates, embedding and deepseek arms
JURY = OUT / "jury.jsonl"            # one row per (judge, item): the judge's primary and secondary Domain
LAYA = OUT / "laya.jsonl"            # one row per item: Laya's log-probabilities, candidates-only and all-7
MODEL_DIR = OUT / "model"            # Laya's files at the pinned revision
REPORT = OUT / "REPORT.md"

LAYA_REPO = "convaiinnovations/laya"
LAYA_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"   # main on 2026-10-15; never a moving ref
TEXT_CHARS = 700                     # what the deepseek tie-break saw (tagging.llm_tiebreak)
SEED = 20261015


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def append_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    tmp.replace(p)


def packs_of_domains(root: Path = REPO) -> dict[str, set[str]]:
    """Domain -> the Packs that ship it, from packs/*/pack.toml."""
    out: dict[str, set[str]] = {}
    for f in sorted((root / "packs").glob("*/pack.toml")):
        for d in tomllib.loads(f.read_text()).get("pack", {}).get("domains", []):
            out.setdefault(d, set()).add(f.parent.name)
    return out


def packs(domains, by_domain: dict[str, set[str]]) -> frozenset[str]:
    return frozenset(p for d in domains or () for p in by_domain.get(d, ()))
