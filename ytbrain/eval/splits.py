"""The Tuning set's splits (docs/eval-spec.md §2): one per kind of Source the questions are
written from, plus one for every private Source. Derived from the source-kind registry, so a new
kind (a podcast feed) gets its own question set without eval code changes."""
from __future__ import annotations

import math

from .. import source_kinds
from ..config import (
    EVAL_POOL_DEPTH,
    EVAL_POOL_DEPTH_TUNING,
    EVAL_SPLIT_RATIO,
    EVAL_TUNING_SIZE,
)

PRIVATE_SPLIT = "dev-private"
# talks first: `dev` is the original split and keeps its name and ids
_KINDS = sorted(source_kinds.KINDS, key=lambda k: k.eval_split != "dev")
SPEC: dict[str, dict] = {k.eval_split: {"prefix": k.eval_prefix, "private": False, "kinds": (k.name,)}
                         for k in _KINDS}
SPEC[PRIVATE_SPLIT] = {"prefix": "devp", "private": True, "kinds": None}   # any kind, from private Sources
TUNING_SPLITS: tuple[str, ...] = tuple(SPEC)
PUBLIC_SPLITS: tuple[str, ...] = tuple(s for s in TUNING_SPLITS if not SPEC[s]["private"])
SETS: tuple[str, ...] = ("all", *TUNING_SPLITS, "test")
# what a split held before targets were computed (`dev` was built with 150 questions)
LEGACY_SIZE = {"dev": EVAL_TUNING_SIZE}


def target(n_docs: int, have: int = 0, ratio: float = EVAL_SPLIT_RATIO) -> int:
    """Questions a split should hold: at most `ratio` of the Documents it can be written from (so
    at most one per Document), and never fewer than it already has: released questions are never
    dropped to fit, the split just stops growing until its Documents catch up."""
    return max(have, math.floor(ratio * n_docs))


def pool_depth(split: str) -> int:
    return EVAL_POOL_DEPTH.get(split, EVAL_POOL_DEPTH_TUNING)


def members(name: str) -> tuple[str, ...]:
    """The splits a set scores: `all` is every Tuning split."""
    return TUNING_SPLITS if name == "all" else (name,)
