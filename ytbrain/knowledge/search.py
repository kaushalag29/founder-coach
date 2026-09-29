"""Hybrid search over Knowledge items.

The implementation lives in `founder_coach.search` so the coach runtime and the
pipeline's eval run the same code (ADR-0010); this module keeps the old import path.
"""
from founder_coach.search import (PUBLIC_FIELDS, Filter, boosts, diversify, hybrid,  # noqa: F401
                                  rrf, search)
