"""Coverage: how well the Library answers a question, from relevance that means something.

A search reports `coverage` -- `strong`, `partial` or `none` -- and the host acts on it: answer with
Citations, answer and say what is missing, or state a Gap (CONTEXT.md: Coverage, Gap). The signal is the
cosine similarity of each hit's closest passage, turned into a probability that the hit is relevant (a
grade of 2 or more in the eval) by a monotone curve. Until `ytbrain eval calibrate` has fitted that curve
on your graded labels the pack carries none and a provisional one is used; every response says which
(`coverage_basis`). Thresholds depend on the Domain's risk tier (domains.yaml): the more a wrong answer
costs, the closer and the more independent the evidence must be (docs/library-and-packs-plan.md §2.4).

Pure functions over search results: no models, no I/O, so the server, the eval and the tests share them.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

LEVELS = ("none", "partial", "strong")                 # weakest first
GAP_ANCHOR = 0.60                                      # the cosine the first versions called a Gap

# Provisional curve: cosine similarity -> P(relevant). Anchored so that a medium-tier Domain (startup, the
# only one early packs had) treats a closest hit below 0.60 as a Gap, exactly as before; the low tier is
# a little more lenient, the high tier needs closer hits to call anything strong.
PROVISIONAL_CURVE = ((0.40, 0.00), (0.50, 0.15), (0.57, 0.35), (0.60, 0.45), (0.65, 0.60),
                     (0.70, 0.70), (0.76, 0.80), (0.85, 0.95), (0.92, 1.00))


@dataclass(frozen=True)
class Rule:
    """When a Domain's evidence counts. `partial`: the best hit is at least this likely relevant.
    `strong`: at least `hits` hits are at least this likely relevant, coming from at least `hits`
    different Documents (`distinct="document"`) or independent Sources (`"source"`), or from anywhere ("")."""
    partial: float
    strong: float
    hits: int = 2
    distinct: str = ""


RULES = {
    "low": Rule(partial=0.35, strong=0.50),
    "medium": Rule(partial=0.45, strong=0.60, distinct="document"),
    "high": Rule(partial=0.45, strong=0.70, distinct="source"),
}
DEFAULT_TIER = "medium"                                # a Domain nobody described is treated with care
MIN_FIT_PAIRS = 50                                     # fewer judged hits than this say little about a curve


@dataclass(frozen=True)
class Calibration:
    """A monotone, piecewise-linear map from cosine similarity to P(relevant)."""
    points: tuple[tuple[float, float], ...] = PROVISIONAL_CURVE
    provisional: bool = True
    fitted_on: dict = field(default_factory=dict, compare=False)

    def p(self, similarity: float | None) -> float | None:
        if similarity is None:
            return None
        pts = self.points
        if similarity <= pts[0][0]:
            return pts[0][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if similarity <= x1:
                return y0 + (y1 - y0) * (similarity - x0) / (x1 - x0) if x1 > x0 else y1
        return pts[-1][1]

    def shifted(self, delta: float) -> "Calibration":
        return Calibration(tuple((round(x + delta, 4), y) for x, y in self.points), self.provisional, self.fitted_on)

    def as_meta(self) -> dict:
        return {"signal": "cosine", "points": [[round(x, 4), round(y, 4)] for x, y in self.points], **self.fitted_on}

    @classmethod
    def from_meta(cls, meta: dict | None, gap_similarity: float | None = None) -> "Calibration":
        """The curve a pack carries; else the provisional one, moved so that `gap_similarity` (an
        operator's override of where a Gap starts) still means what it said."""
        raw = (meta or {}).get("calibration") or {}
        pts = raw.get("points")
        shift = raw.get("shift")                          # set by `eval gap --tune`: where the Gap questions say the border is
        shift = float(shift) if isinstance(shift, (int, float)) and not isinstance(shift, bool) and abs(shift) <= 0.3 else 0.0
        try:
            if pts and len(pts) >= 2:
                pairs = tuple((float(x), float(y)) for x, y in pts)
                if all(b[0] >= a[0] and b[1] >= a[1] for a, b in zip(pairs, pairs[1:])):
                    cal = cls(pairs, False, {k: v for k, v in raw.items() if k not in ("points", "signal")})
                    return cal.shifted(shift) if shift else cal
        except (TypeError, ValueError):
            pass                                         # a damaged curve is no curve: fall back, never fail
        cal = cls()
        if shift:
            return cal.shifted(shift)
        return cal.shifted(gap_similarity - GAP_ANCHOR) if gap_similarity and abs(gap_similarity - GAP_ANCHOR) > 1e-9 else cal


def fit(pairs: list[tuple[float, int]], knots: int = 16) -> Calibration:
    """Isotonic regression (pool adjacent violators) of relevance on similarity: `pairs` are
    (cosine similarity, 1 if the hit was judged relevant else 0). Similarities are first grouped into
    equal-count bins so the curve stays a handful of knots, however many judgments there are."""
    if len(pairs) < MIN_FIT_PAIRS:
        raise ValueError(f"{len(pairs)} judged hits: need at least {MIN_FIT_PAIRS} to fit a curve")
    ys = [y for _, y in pairs]
    if not any(ys) or all(ys):
        raise ValueError("every judged hit has the same label: nothing to fit")
    data = sorted(pairs)
    n_bins = min(knots, max(2, len(data) // 10))
    size = len(data) / n_bins
    blocks = []                                          # [sum_x, sum_y, n]
    for b in range(n_bins):
        chunk = data[round(b * size):round((b + 1) * size)]
        if chunk:
            blocks.append([sum(x for x, _ in chunk), sum(y for _, y in chunk), len(chunk)])
    out: list[list[float]] = []
    for blk in blocks:                                   # pool adjacent violators
        out.append(blk)
        while len(out) > 1 and out[-2][1] / out[-2][2] > out[-1][1] / out[-1][2]:
            last = out.pop()
            out[-1] = [out[-1][0] + last[0], out[-1][1] + last[1], out[-1][2] + last[2]]
    points = [(sx / n, sy / n) for sx, sy, n in out]
    dedup: list[tuple[float, float]] = []
    for x, y in points:
        if dedup and x <= dedup[-1][0]:
            continue
        dedup.append((x, y))
    if len(dedup) < 2:
        raise ValueError("the similarities are all alike: nothing to fit")
    return Calibration(tuple(dedup), False, {"n": len(pairs), "base_rate": round(sum(ys) / len(ys), 4)})


def _combine(by_domain: dict[str, str]) -> tuple[str, list[str]]:
    """One level for a question that needs several Domains, and the Domains that fall short of the best.
    Strong only when every Domain is; none only when none has anything (a Gap for the whole question);
    otherwise partial: answer what is covered and name what is not."""
    levels = set(by_domain.values())
    level = "strong" if levels == {"strong"} else "none" if levels == {"none"} else "partial"
    top = max(by_domain.values(), key=LEVELS.index, default="none")
    return level, [d for d, lv in by_domain.items() if lv != top]


@dataclass
class Coverage:
    level: str = "none"
    basis: str = "provisional"                           # calibrated | provisional | keyword
    by_domain: dict[str, str] = field(default_factory=dict)
    stale: list[str] = field(default_factory=list)
    newest_year: int | None = None
    p: dict[str, float] = field(default_factory=dict)    # item_id -> P(relevant), for hits with a similarity
    weak: list[str] = field(default_factory=list)        # Domains asked for that came back weaker than the rest


def _rule(tier: str | None) -> Rule:
    return RULES.get(tier or DEFAULT_TIER, RULES[DEFAULT_TIER])


def _year(hit: dict) -> int | None:
    raw = str(hit.get("published_at") or hit.get("year") or "")[:4]
    return int(raw) if raw.isdigit() else None


def _independence(hit: dict) -> tuple:
    """Who a hit speaks for. Two Sources are independent; so are two Books of one Source (every owned Book shares
    one Source id but each is its own author, argument and edition: its `series` is the Book's title), while two
    chapters of one Book are one voice. Without a series it is the Source, as before."""
    return (hit.get("source_id") or hit.get("doc_id"), hit.get("series") or "")


def _level(hits: list[dict], p: dict[str, float], rule: Rule, best: float | None) -> str:
    """`strong` / `partial` / `none` for these hits under one Rule. `best` is the closest similarity's
    probability when the caller knows a closer item than any hit shown (the search's own top result)."""
    probs = [p[h["item_id"]] for h in hits if h["item_id"] in p]
    top = max(probs + ([best] if best is not None else []), default=None)
    if top is None or top < rule.partial:
        return "none"
    good = [h for h in hits if p.get(h["item_id"], 0.0) >= rule.strong]
    if rule.distinct == "document":
        enough = len({h.get("doc_id") for h in good}) >= rule.hits
    elif rule.distinct == "source":
        enough = len({_independence(h) for h in good}) >= rule.hits
    else:
        enough = len(good) >= rule.hits
    return "strong" if enough else "partial"


def assess(hits: list[dict], *, calibration: Calibration, tiers: dict[str, str] | None = None,
           focus: tuple[str, ...] = (), freshness: dict[str, int | None] | None = None,
           top_similarity: float | None = None, semantic: bool = True,
           today: dt.date | None = None) -> Coverage:
    """Coverage of `hits` (a search's results: each with item_id, doc_id, domains, published_at and, in
    semantic mode, `similarity`). `focus` is the Domains the search was limited to or routed to; with
    two or more, each is judged on its own hits and the answer is only as strong as the weakest. `tiers`
    maps a Domain to its risk tier and `freshness` to its half-life in days (None: evergreen)."""
    tiers, freshness = tiers or {}, freshness or {}
    today = today or dt.date.today()
    if not hits:
        return Coverage("none", "calibrated" if not calibration.provisional else "provisional")
    if not semantic or not any(h.get("similarity") is not None for h in hits):
        # without embeddings there is nothing to calibrate: matches exist, how close they are is unknown
        return Coverage("partial", "keyword", newest_year=max(filter(None, map(_year, hits)), default=None))
    p = {h["item_id"]: calibration.p(h["similarity"]) for h in hits if h.get("similarity") is not None}
    best = calibration.p(top_similarity)
    dom_of = lambda h: list(h.get("domains") or ["startup"])           # noqa: E731
    out = Coverage(basis="provisional" if calibration.provisional else "calibrated", p=p)
    focus = tuple(dict.fromkeys(focus))
    if len(focus) >= 2:
        for d in focus:
            mine = [h for h in hits if d in dom_of(h)]
            out.by_domain[d] = _level(mine, p, _rule(tiers.get(d)), None)
        out.level, out.weak = _combine(out.by_domain)
        scope = list(focus)
    else:
        scope = list(focus) or list(dict.fromkeys(d for h in hits[:3] for d in dom_of(h)))
        tier = max((tiers.get(d, DEFAULT_TIER) for d in scope), key=lambda t: list(RULES).index(t) if t in RULES else 1)
        out.level = _level(hits, p, _rule(tier), best)
    live = [h for h in hits if p.get(h["item_id"], 0.0) >= RULES["low"].partial]
    out.newest_year = max(filter(None, map(_year, live)), default=None)
    for d in scope:
        half = freshness.get(d)
        years = [y for h in live if d in dom_of(h) for y in [_year(h)] if y]
        if half and years and (today.year - max(years)) * 365.25 > half:
            out.stale.append(d)
    return out


def with_empty(cov: Coverage, empty: list[str], focus: tuple[str, ...]) -> Coverage:
    """Count Domains the question asked for that the Library declares but holds nothing in: each is
    `none`, so a question that needs one of them is at best partial and the host names the Gap."""
    if not empty:
        return cov
    by_domain = dict(cov.by_domain) or {d: cov.level for d in focus}
    for d in empty:
        by_domain[d] = "none"
    cov.by_domain = by_domain
    cov.level, cov.weak = _combine(by_domain)
    return cov


def for_search(rows: list[dict], info: dict, *, meta: dict | None, calibration: Calibration,
               domains: tuple[str, ...] = (), empty: tuple[str, ...] = (), semantic: bool = True,
               today: dt.date | None = None) -> Coverage:
    """Coverage of one search, from what the search returned (`rows`), what it reported (`info`:
    `top_similarity`, `routing`), and the pack's manifest (`domain_info`: risk tiers and half-lives).
    `domains` are the Domains the caller named, `empty` those of them the pack declares but holds nothing
    in. The coach runtime and the evals both call this, so the numbers the evals measure are the ones
    founders get."""
    info_d = (meta or {}).get("domain_info") or {}
    routing = info.get("routing")
    focus = tuple(domains) or (tuple(routing.domains) if routing is not None and routing.routed else ())
    cov = assess(rows, calibration=calibration, focus=focus, top_similarity=info.get("top_similarity"),
                 tiers={d: v.get("risk_tier") for d, v in info_d.items()},
                 freshness={d: v.get("freshness_days") for d, v in info_d.items()},
                 semantic=semantic, today=today)
    return with_empty(cov, list(empty), focus)
