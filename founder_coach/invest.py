"""The investor Pack's arithmetic (M6g; docs/m6-plan.md 4.4, R9, R10). Pure functions, no I/O beyond reading a CSV
the person named, no network, no prices: everything is computed from their broker's positions export "as of" its
date and the targets in their own Investment Policy Statement.

Money is kept in integer cents (Decimal in between), so every amount is exact and sums add up to the cent. Nothing
here names a security to buy or sell: a review states facts against the person's own rules (R9), and a split works
on asset classes only (I2).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

# The asset classes an Investment Policy Statement's targets and a holding's label use (CONTEXT.md)
ASSET_CLASSES = ("us_equity", "intl_equity", "bonds", "cash", "real_estate", "other")
MAX_CSV_BYTES = 5_000_000
CENT = Decimal("0.01")


class ImportProblem(ValueError):
    """A positions file that can't be read as asked: the message says which column or value it needs."""


# ------------------------------------------------------------------------------------------------- money
def to_cents(value) -> int | None:
    """'$1,234.56', '(12.00)', '1234.5', 1234.5 -> cents; '--', 'n/a', '' -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        d = Decimal(str(value))
    else:
        s = str(value).strip().replace("−", "-")
        if not s or s in ("--", "-", "n/a", "N/A", "NA"):
            return None
        neg = s.startswith("(") and s.endswith(")")
        s = re.sub(r"[\s$,()]", "", s).rstrip("%")
        if s.startswith("+"):
            s = s[1:]
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
        if neg:
            d = -d
    return int((d / CENT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(cents: int) -> str:
    """12345 -> '$123.45' (negative: '-$1.00')."""
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def pct(part: int, whole: int) -> Decimal:
    """part/whole in percent, to two decimals (0 for an empty whole)."""
    if not whole:
        return Decimal("0")
    return (Decimal(part) * 100 / Decimal(whole)).quantize(CENT, rounding=ROUND_HALF_UP)


def split_cents(total_cents: int, weights: dict[str, Decimal]) -> dict[str, int]:
    """`total_cents` split by `weights` (percent), largest remainder: the parts always sum to the total exactly."""
    if total_cents < 0:
        raise ValueError("the amount to split must not be negative")
    wsum = sum(weights.values())
    if wsum <= 0:
        raise ValueError("the targets add up to nothing")
    raw = {k: Decimal(total_cents) * w / wsum for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}                         # floor (all non-negative)
    left = total_cents - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), ASSET_CLASSES.index(k) if k in ASSET_CLASSES else 99, k)):
        if left <= 0:
            break
        out[k] += 1
        left -= 1
    return out


def check_targets(value) -> dict[str, Decimal]:
    """An Investment Policy Statement's target allocation: {asset class: percent}, every class known, each 0-100,
    summing to 100 (within 0.01)."""
    if not isinstance(value, dict) or not value:
        raise ValueError("targets must be an object of asset class -> percent, e.g. "
                         '{"us_equity": 60, "intl_equity": 20, "bonds": 20}')
    out = {}
    for k, v in value.items():
        name = str(k).strip().lower().replace(" ", "_").replace("-", "_")
        if name not in ASSET_CLASSES:
            raise ValueError(f"unknown asset class {k!r}; asset classes: {', '.join(ASSET_CLASSES)}")
        try:
            d = Decimal(str(v).rstrip("%").strip())
        except InvalidOperation:
            raise ValueError(f"targets[{k!r}] must be a percent, got {v!r}") from None
        if not Decimal(0) <= d <= Decimal(100):
            raise ValueError(f"targets[{k!r}] must be between 0 and 100")
        out[name] = out.get(name, Decimal(0)) + d
    total = sum(out.values())
    if abs(total - 100) > Decimal("0.01"):
        raise ValueError(f"targets must add up to 100 %, they add up to {total.normalize()} %")
    return out


# ------------------------------------------------------------------------------------------------- CSV import
# Column names as brokers write them, lower case (the first match wins). A file with its own names is imported with
# `columns` naming them (the generic import).
COLUMNS = {
    "symbol": ("symbol", "ticker", "symbol/cusip"),
    "description": ("description", "investment name", "security description", "name", "security name"),
    "quantity": ("quantity", "shares", "qty (quantity)", "qty", "share quantity"),
    "value": ("current value", "market value", "total value", "mkt val (market value)", "market value ($)",
              "value", "current value ($)", "marketvalue"),
    "account": ("account name", "account number", "account", "account name/number", "account id"),
    # never a bare "Type": Fidelity's "Type" is Cash or Margin, the kind of account, not the asset class
    "asset_class": ("asset class", "security type", "asset type"),
}
REQUIRED = ("symbol", "value")
# what a broker's own type column says, mapped only where the answer is unambiguous (US or international equity
# never is: those are labelled by the person once, per Project)
CSV_CLASSES = {"fixed income": "bonds", "bond": "bonds", "bonds": "bonds", "cash": "cash",
               "cash and money market": "cash", "money market": "cash", "cash & cash investments": "cash",
               "real estate": "real_estate", "reit": "real_estate"}
CASH_DESCRIPTION = re.compile(r"\b(money market|cash|sweep|core position|fdic[- ]insured deposit)\b", re.I)
SKIP_SYMBOL = re.compile(r"^(account total|total|pending activity|cash & cash investments total|positions total|"
                         r"grand total)$", re.I)
CASH_SYMBOL = re.compile(r"^(cash|core|cash & cash investments|money market)$", re.I)
DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), lambda m: dt.date(int(m[3]), int(m[1]), int(m[2]))),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: dt.date(int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b([A-Z][a-z]{2})-(\d{1,2})-(\d{4})\b"),
     lambda m: dt.datetime.strptime(f"{m[1]} {m[2]} {m[3]}", "%b %d %Y").date()),
]


@dataclass
class Position:
    account: str
    symbol: str
    description: str
    quantity: str | None
    value_cents: int
    csv_class: str | None = None          # the class the file itself says, when unambiguous


@dataclass
class Snapshot:
    """One account's positions as of a date, from one file."""
    account: str
    as_of: str
    broker: str
    sha256: str
    positions: list[Position] = field(default_factory=list)

    @property
    def total_cents(self) -> int:
        return sum(p.value_cents for p in self.positions)


def _norm(h: str) -> str:
    return " ".join(str(h or "").replace("﻿", "").strip().lower().split())


def _find_columns(header: list[str], columns: dict | None) -> dict[str, int]:
    names = [_norm(h) for h in header]
    out = {}
    if columns:                                        # the person named them (generic import)
        for key, want in columns.items():
            if key not in COLUMNS:
                raise ImportProblem(f"unknown column role {key!r}; roles: {', '.join(COLUMNS)}")
            if _norm(want) not in names:
                raise ImportProblem(f"no column {want!r} in the file (it has: {', '.join(h for h in header if h)})")
            out[key] = names.index(_norm(want))
    for key, aliases in COLUMNS.items():
        if key in out:
            continue
        for a in aliases:
            if a in names:
                out[key] = names.index(a)
                break
    return out


def _detect(lines: list[str], header: list[str]) -> str:
    text = " ".join(lines[:3]).lower()
    names = {_norm(h) for h in header}
    if "positions for" in text or "mkt val (market value)" in names or "security type" in names:
        return "schwab"
    if "investment name" in names and "total value" in names:
        return "vanguard"
    if "current value" in names and ("account name" in names or "account number" in names):
        return "fidelity"
    return "generic"


def _as_of(lines: list[str]) -> str | None:
    for line in lines:
        low = line.lower()
        if not any(w in low for w in ("as of", "downloaded", "date", "positions for")):
            continue
        for rx, make in DATE_PATTERNS:
            m = rx.search(line)
            if m:
                try:
                    return make(m).isoformat()
                except ValueError:
                    continue
    return None


def read_positions(path: str | Path, account: str | None = None, as_of: str | None = None,
                   columns: dict | None = None, today: dt.date | None = None) -> list[Snapshot]:
    """A broker's positions export -> one Snapshot per account in it. Refuses (ImportProblem) rather than guesses:
    a file that isn't a .csv or is too large, a missing symbol or value column, no account (column or `account`),
    no date (in the file or `as_of`), a future date, or no positions."""
    p = Path(path).expanduser()
    if p.suffix.lower() != ".csv":
        raise ImportProblem(f"{p.name}: only a .csv positions export can be imported")
    try:
        size = p.stat().st_size
    except OSError as e:
        raise ImportProblem(f"can't read {p}: {e.strerror or e}") from None
    if size > MAX_CSV_BYTES:
        raise ImportProblem(f"{p.name} is {size // 1_000_000} MB; a positions export is far smaller (limit 5 MB)")
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return parse_positions(text, account=account, as_of=as_of, columns=columns, today=today,
                           sha256=hashlib.sha256(raw).hexdigest(), name=p.name)


def parse_positions(text: str, account: str | None = None, as_of: str | None = None, columns: dict | None = None,
                    today: dt.date | None = None, sha256: str | None = None, name: str = "the file") -> list[Snapshot]:
    lines = text.splitlines()
    rows = list(csv.reader(io.StringIO(text)))
    head_at, cols = None, {}
    for i, row in enumerate(rows[:25]):
        found = _find_columns(row, columns) if any(c.strip() for c in row) else {}
        if all(k in found for k in REQUIRED):
            head_at, cols = i, found
            break
    if head_at is None:
        sample = next((r for r in rows if len([c for c in r if c.strip()]) >= 3), [])
        missing = "a symbol column (Symbol or Ticker) and a market value column (Current Value, Market Value or " \
                  "Total Value)"
        raise ImportProblem(f"{name}: can't find {missing}"
                            + (f"; its columns look like: {', '.join(c for c in sample if c.strip())[:200]}" if sample
                               else "") + ". Export positions (not activity) from your broker, or name the columns "
                                          "(columns={\"symbol\": ..., \"value\": ...}).")
    header = rows[head_at]
    broker = "generic" if columns else _detect(lines, header)
    if as_of:
        try:
            when = dt.date.fromisoformat(str(as_of))
        except ValueError:
            raise ImportProblem(f"as_of must be a date like 2026-10-05, got {as_of!r}") from None
    else:
        found = _as_of(lines[:head_at] + lines[-15:])
        if not found:
            raise ImportProblem(f"{name}: the file doesn't say what date it is as of; pass as_of (YYYY-MM-DD), the "
                                "date you exported it")
        when = dt.date.fromisoformat(found)
    if when > (today or dt.date.today()):
        raise ImportProblem(f"as_of {when.isoformat()} is in the future")
    if "account" not in cols and not account:
        m = re.search(r"positions for (?:account )?(.+?)\s+as of", " ".join(lines[:head_at]), re.I)
        account = m.group(1).strip(' ",') if m else None     # Schwab names the account above its header
        if not account:
            raise ImportProblem(f"{name} has no account column: pass account, a name for the account (e.g. 'Roth IRA')")
    snaps: dict[str, Snapshot] = {}
    for row in rows[head_at + 1:]:
        if not any(c.strip() for c in row):
            if snaps:
                break                                   # a blank row ends the positions (Vanguard: activity follows)
            continue
        get = lambda k: row[cols[k]].strip() if k in cols and cols[k] < len(row) else ""   # noqa: E731
        symbol, desc = get("symbol"), get("description")
        if not symbol and not desc:
            continue
        if SKIP_SYMBOL.match(symbol or desc):
            continue
        cents = to_cents(get("value"))
        if cents is None:
            continue                                    # a disclaimer line, a pending or closed position
        acct = (get("account") or account or "").strip()
        if not acct:
            raise ImportProblem(f"{name}: a row has no account; pass account")
        csv_class = CSV_CLASSES.get(get("asset_class").lower())
        if CASH_SYMBOL.match(symbol) or (not symbol and CASH_DESCRIPTION.search(desc)):
            csv_class, symbol = "cash", "CASH"
        elif symbol.endswith("**"):                     # Fidelity's core (cash) position
            csv_class, symbol = "cash", symbol.rstrip("*")
        elif not csv_class and "money market" in desc.lower():
            csv_class = "cash"
        sym = re.sub(r"\s+", "", symbol.upper()) or desc[:40].upper()
        s = snaps.setdefault(acct, Snapshot(acct, when.isoformat(), broker, sha256 or ""))
        s.positions.append(Position(acct, sym, desc, get("quantity") or None, cents, csv_class))
    if not snaps:
        raise ImportProblem(f"{name}: no positions with a market value were found under its header row")
    return list(snaps.values())


# ------------------------------------------------------------------------------------------------- review
def classify(symbol: str, csv_class: str | None, labels: dict[str, str]) -> str | None:
    """The person's own label for the symbol wins; then what the file said unambiguously; else unknown (None)."""
    return labels.get(symbol) or csv_class


def latest_per_account(snapshots: list[dict]) -> list[dict]:
    """The newest snapshot of each account (by as_of, then import time): a Project's holdings now (R10)."""
    best: dict[str, dict] = {}
    for s in snapshots:
        cur = best.get(s["account"])
        if cur is None or (s["as_of"], s["imported_at"]) > (cur["as_of"], cur["imported_at"]):
            best[s["account"]] = s
    return sorted(best.values(), key=lambda s: s["account"])


def review(snapshots: list[dict], positions: dict[str, list[dict]], labels: dict[str, str], ips: dict,
           today: dt.date) -> dict:
    """Allocation, Drift and concentration of a Project across all its accounts' latest snapshots, against its
    Investment Policy Statement (`ips`: targets, rebalance_band, concentration_limit, concentration_exempt,
    review_days). Facts only: amounts by asset class, holdings over the person's own limit with their excess (cash
    and the holdings they exempted, such as broad index funds, are never over it), and what is missing."""
    latest = latest_per_account(snapshots)
    rows = [dict(p, account=s["account"]) for s in latest for p in positions.get(s["id"], [])]
    total = sum(r["value_cents"] for r in rows)
    by_class: dict[str, int] = {c: 0 for c in ASSET_CLASSES}
    unclassified: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    exempt = {str(x).upper() for x in (ips.get("concentration_exempt") or [])}
    for r in rows:
        c = classify(r["symbol"], r.get("csv_class"), labels)
        if c:
            by_class[c] += r["value_cents"]
        else:
            unclassified[r["symbol"]] = unclassified.get(r["symbol"], 0) + r["value_cents"]
        if c != "cash" and r["symbol"] not in exempt:  # a limit on one security: not cash, not what they exempted
            by_symbol[r["symbol"]] = by_symbol.get(r["symbol"], 0) + r["value_cents"]
    missing = [k for k in ("targets", "rebalance_band", "concentration_limit", "review_days") if ips.get(k) in (None, "")]
    targets = {k: Decimal(str(v)) for k, v in (ips.get("targets") or {}).items()}
    band = Decimal(str(ips["rebalance_band"])) if ips.get("rebalance_band") not in (None, "") else None
    limit = Decimal(str(ips["concentration_limit"])) if ips.get("concentration_limit") not in (None, "") else None
    allocation = []
    for c in ASSET_CLASSES:
        have = by_class[c]
        if not have and c not in targets:
            continue
        row = {"asset_class": c, "value": money(have), "value_cents": have, "percent": float(pct(have, total))}
        if c in targets:
            t = targets[c]
            drift = pct(have, total) - t
            to_target = int((Decimal(total) * t / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) - have
            row.update(target_percent=float(t), drift_points=float(drift), to_target=money(to_target),
                       to_target_cents=to_target, outside_band=bool(band is not None and abs(drift) > band))
        allocation.append(row)
    over = []
    if limit is not None and total:
        for sym, v in sorted(by_symbol.items(), key=lambda kv: -kv[1]):
            if pct(v, total) > limit:
                cap = int((Decimal(total) * limit / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                over.append({"symbol": sym, "value": money(v), "percent": float(pct(v, total)),
                             "limit_percent": float(limit), "over_limit": money(v - cap), "over_limit_cents": v - cap})
    dates = sorted({s["as_of"] for s in latest})
    oldest = dt.date.fromisoformat(dates[0]) if dates else None
    review_days = int(ips["review_days"]) if ips.get("review_days") not in (None, "") else None
    stale = bool(oldest and review_days is not None and (today - oldest).days > review_days)
    return {
        "as_of": dates[-1] if len(dates) == 1 else (f"{dates[0]} to {dates[-1]}" if dates else None),
        "accounts": [{"account": s["account"], "as_of": s["as_of"], "value": money(s["total_cents"])} for s in latest],
        "total": money(total), "total_cents": total,
        "allocation": allocation,
        "outside_band": [r["asset_class"] for r in allocation if r.get("outside_band")],
        "over_limit": over,
        "unclassified": [{"symbol": k, "value": money(v)} for k, v in sorted(unclassified.items(), key=lambda kv: -kv[1])],
        "unclassified_percent": float(pct(sum(unclassified.values()), total)),
        "stale": stale, "days_old": (today - oldest).days if oldest else None,
        "missing_policy": missing,
        "note": ("Facts as of the Holdings date against your own Investment Policy Statement; no live prices; not "
                 "advice on any security."),
    }


def split(amount, targets: dict) -> dict:
    """A sum split by the person's own targets, by asset class only (I2): exact to the cent."""
    cents = to_cents(amount)
    if cents is None or cents <= 0:
        raise ValueError(f"amount must be a positive sum of money, got {amount!r}")
    t = check_targets(targets)
    parts = split_cents(cents, t)
    return {"amount": money(cents), "parts": [{"asset_class": c, "target_percent": float(t[c]), "amount": money(parts[c]),
                                               "amount_cents": parts[c]} for c in ASSET_CLASSES if c in t],
            "note": "By asset class from your own targets; which fund or bond fills each is your choice."}
