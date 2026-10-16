"""Step 4 of the Laya spike: score every arm against the jury and write the verdict.

    python spikes/laya/score.py

Reads sample.jsonl, jury.jsonl and (when present) laya.jsonl from data/spikes/laya/, writes REPORT.md
(aggregate numbers only) and disputed.md (up to 50 items the jury split on, with their text: stays in
data/, private). Pure: numpy and the standard library, no model, no network.

The decision rules are fixed here, before any result is seen (spikes/laya/README.md).
"""
from __future__ import annotations

import hashlib
import math
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import JURY, LAYA, OUT, REPORT, SAMPLE, SEED, packs, packs_of_domains, read_jsonl  # noqa: E402

MARGIN = 0.02            # non-inferiority margin on accuracy vs deepseek
F1_DROP = 0.02           # largest allowed drop in a high-tier Domain's F1
HIGH_TIER_MIN = 20       # silver items a high-tier Domain needs before its F1 can fail an arm
HARMFUL_MOVES = 0.01     # largest share of close calls an arm may move to a wrong Pack
JURY_KAPPA = 0.6         # Fleiss' kappa the panel needs before its labels count
CALIB_SHARE = 0.3        # items whose id hashes below this fit Laya's temperature; the rest are scored
BOOTSTRAP = 10_000
ARMS = ("embedding", "deepseek", "laya_cand", "laya_all")
LABEL = {"embedding": "Embedding top-1", "deepseek": "deepseek (today)", "laya_cand": "Laya, candidates only",
         "laya_all": "Laya, all Domains", "production": "Today's pipeline"}


# ----------------------------------------------------------------------------- statistics
def fleiss_kappa(votes: list[list[str]], cats: list[str]) -> float:
    """Fleiss' kappa for items each rated by the same number of raters."""
    if not votes:
        return float("nan")
    n = len(votes[0])
    M = np.array([[v.count(c) for c in cats] for v in votes], dtype=float)
    P_i = ((M * M).sum(1) - n) / (n * (n - 1))
    p_j = M.sum(0) / M.sum()
    Pe = float((p_j ** 2).sum())
    return float((P_i.mean() - Pe) / (1 - Pe)) if Pe < 1 else 1.0


def cohen_kappa(a: list[str], b: list[str]) -> float:
    if not a:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / len(a)
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (len(a) ** 2)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value: the discordant pairs under Binomial(b + c, 1/2)."""
    n = b + c
    if not n:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def stratified_bootstrap(diffs: dict[str, np.ndarray], weights: dict[str, float], rng) -> tuple[float, float, float]:
    """Weighted mean of per-stratum mean differences, with a percentile 95% CI from resampling within strata."""
    keys = [k for k in diffs if len(diffs[k])]
    w = np.array([weights[k] for k in keys])
    w = w / w.sum()
    point = float(sum(wi * diffs[k].mean() for wi, k in zip(w, keys)))
    boots = np.zeros(BOOTSTRAP)
    for wi, k in zip(w, keys):
        d = diffs[k]
        idx = rng.integers(0, len(d), size=(BOOTSTRAP, len(d)))
        boots += wi * d[idx].mean(1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return point, float(lo), float(hi)


def f1_by_class(pred: list[str], gold: list[str], cats) -> dict[str, tuple[float, int]]:
    out = {}
    for c in cats:
        tp = sum(p == c and g == c for p, g in zip(pred, gold))
        fp = sum(p == c and g != c for p, g in zip(pred, gold))
        fn = sum(p != c and g == c for p, g in zip(pred, gold))
        support = tp + fn
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / support if support else 0.0
        out[c] = ((2 * prec * rec / (prec + rec)) if prec + rec else 0.0, support)
    return out


def macro_f1(pred, gold) -> float:
    cats = sorted(set(gold))
    return float(np.mean([f for f, _ in f1_by_class(pred, gold, cats).values()])) if cats else float("nan")


def set_f1(pred: set, gold: set) -> float:
    tp = len(pred & gold)
    return 2 * tp / (len(pred) + len(gold)) if pred or gold else 1.0


def calibrated(logp: dict[str, float], t: float) -> dict[str, float]:
    z = {d: v / t for d, v in logp.items()}
    m = max(z.values())
    e = {d: math.exp(v - m) for d, v in z.items()}
    s = sum(e.values())
    return {d: v / s for d, v in e.items()}


def fit_temperature(rows: list[tuple[dict, str]]) -> float:
    """The temperature (on Laya's own log-probabilities) minimising negative log-likelihood of the silver label."""
    rows = [(lp, g) for lp, g in rows if g in lp]
    if not rows:
        return 1.0
    grid = np.exp(np.linspace(math.log(0.05), math.log(20), 400))
    def nll(t):
        return -sum(math.log(max(calibrated(lp, t)[g], 1e-12)) for lp, g in rows)
    return float(min(grid, key=nll))


def ece(conf: list[float], correct: list[bool], bins: int = 15) -> float:
    if not conf:
        return float("nan")
    c, k = np.array(conf), np.array(correct, dtype=float)
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (c > lo) & (c <= hi)
        if m.any():
            total += m.mean() * abs(c[m].mean() - k[m].mean())
    return float(total)


def brier(probs: list[dict], gold: list[str]) -> float:
    return float(np.mean([sum((p.get(d, 0.0) - (d == g)) ** 2 for d in set(p) | {g}) for p, g in zip(probs, gold)]))


def in_calibration(item_id: str) -> bool:
    return int(hashlib.sha256(item_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < CALIB_SHARE


# ----------------------------------------------------------------------------- the panel
def silver_labels(items, jury):
    """item_id -> (majority primary or None, set of Domains named by 2+ judges, the votes)."""
    by = {}
    for r in jury:
        by.setdefault(r["item_id"], {})[r["judge"]] = r
    judges = sorted({r["judge"] for r in jury})
    out = {}
    for it in items:
        v = by.get(it["item_id"], {})
        if len(v) < len(judges) or len(judges) < 3:
            continue
        prim = [v[j]["primary"] for j in judges]
        top, n = Counter(prim).most_common(1)[0]
        named = Counter(d for j in judges for d in {v[j]["primary"], v[j].get("secondary")} if d)
        out[it["item_id"]] = (top if n >= 2 else None, {d for d, k in named.items() if k >= 2}, prim)
    return out, judges


# ----------------------------------------------------------------------------- the report
def pct(x: float) -> str:
    return "n/a" if x != x else f"{100 * x:.1f}%"


def main() -> int:
    items = read_jsonl(SAMPLE)
    jury = read_jsonl(JURY)
    if not items or not jury:
        print("laya score: needs sample.jsonl and jury.jsonl (run sample.py and jury.py)", file=sys.stderr)
        return 1
    meta = __import__("json").loads((OUT / "sample_meta.json").read_text())
    cats = list(meta["domains"])
    laya = {r["item_id"]: r for r in read_jsonl(LAYA)}
    silver, judges = silver_labels(items, jury)
    by_domain = packs_of_domains()
    rng = np.random.default_rng(SEED)
    lines: list[str] = []
    say = lines.append

    # temperature for Laya, fitted on the calibration share only
    temps = {}
    for arm, key in (("laya_cand", "cand"), ("laya_all", "all")):
        rows = [(laya[i][key], silver[i][0]) for i in laya if i in silver and silver[i][0] and in_calibration(i)]
        temps[arm] = fit_temperature(rows) if rows else None

    def pred(it, arm):
        if arm == "embedding":
            return it["embedding"]
        if arm == "deepseek":
            return it["deepseek"] or it["production"]
        if arm == "production":
            return it["production"]
        r = laya.get(it["item_id"])
        if not r:
            return None
        lp = r["cand" if arm == "laya_cand" else "all"]
        return [max(lp, key=lp.get)]

    arms = [a for a in ARMS if a in ("embedding", "deepseek") or laya]

    # --- the jury
    votes = [silver[i][2] for i in silver]
    fk = fleiss_kappa(votes, cats)
    labelled = {i: s for i, s in silver.items() if s[0]}
    contested = [it for it in items if it["item_id"] in silver and not silver[it["item_id"]][0]]
    say("# Laya spike: report\n")
    say(f"Tie-break model `{meta['tag_model']}`; judges {', '.join(f'`{j}`' for j in judges)}; "
        f"seed {meta['seed']}. Population: {meta['population']}.\n")
    say("## The jury\n")
    say(f"- Fleiss' kappa over {len(votes)} item(s): **{fk:.3f}** (needs ≥ {JURY_KAPPA}).")
    say(f"- Majority label on {len(labelled)} item(s); {len(contested)} contested (three different answers), "
        f"left out of accuracy.")
    for j in judges:
        agree = [silver[i][2][judges.index(j)] == silver[i][0] for i in labelled]
        say(f"- `{j}` agrees with the majority on {pct(sum(agree) / len(agree) if agree else float('nan'))}.")
    jury_ok = fk >= JURY_KAPPA

    # --- close calls: the drop-in comparison
    close = [it for it in items if it["stratum"] == "close" and it["item_id"] in labelled]
    gold = [labelled[it["item_id"]][0] for it in close]
    outside = sum(g not in it["candidates"] for it, g in zip(close, gold))
    say("\n## Close calls (the tie-break's job)\n")
    say(f"{len(close)} close call(s) with a majority label. The jury's label is outside the embedding candidates "
        f"for {outside} ({pct(outside / len(close) if close else float('nan'))}): an embedding-stage miss no "
        f"tie-break can fix (counted wrong for the deepseek and candidates-only arms alike).\n")
    say("| Arm | Accuracy (95% CI) | Macro-F1 | κ vs jury | finance F1 (n) | investment F1 (n) |")
    say("|---|---|---|---|---|---|")
    res = {}
    for arm in arms:
        ok = []
        top = []
        for it, g in zip(close, gold):
            p = pred(it, arm)
            ok.append(bool(p) and g in p)
            top.append(g if p and g in p else (p[0] if p else "-"))
        k = sum(ok)
        lo, hi = wilson(k, len(ok))
        f1 = f1_by_class(top, gold, cats)
        res[arm] = {"ok": np.array(ok, dtype=float), "top": top, "f1": f1}
        say(f"| {LABEL[arm]} | {pct(k / len(ok) if ok else float('nan'))} ({pct(lo)}–{pct(hi)}) | "
            f"{macro_f1(top, gold):.3f} | {cohen_kappa(top, gold):.3f} | {f1['finance'][0]:.2f} ({f1['finance'][1]}) "
            f"| {f1['investment'][0]:.2f} ({f1['investment'][1]}) |")

    verdicts = {}
    say("\n### Against deepseek, paired on the same items\n")
    say("| Arm | Δ accuracy (95% CI) | McNemar p | κ with deepseek | wrong-Pack moves | passes |")
    say("|---|---|---|---|---|---|")
    ds = res["deepseek"]
    for arm in arms:
        if arm == "deepseek":
            continue
        r = res[arm]
        d, lo, hi = stratified_bootstrap({"close": r["ok"] - ds["ok"]}, {"close": 1.0}, rng)
        b = int(((r["ok"] == 1) & (ds["ok"] == 0)).sum())
        c = int(((r["ok"] == 0) & (ds["ok"] == 1)).sum())
        harmful = 0
        for it, g in zip(close, gold):
            pa, pd = packs(pred(it, arm), by_domain), packs(pred(it, "deepseek"), by_domain)
            if pa != pd and pa != packs([g], by_domain):
                harmful += 1
        hrate = harmful / len(close) if close else float("nan")
        f1_fail = [dm for dm in ("finance", "investment")
                   if r["f1"][dm][1] >= HIGH_TIER_MIN and r["f1"][dm][0] < ds["f1"][dm][0] - F1_DROP]
        passes = jury_ok and lo > -MARGIN and hrate < HARMFUL_MOVES and not f1_fail
        verdicts[arm] = passes
        kap = cohen_kappa([pred(it, arm)[0] if pred(it, arm) else "-" for it in close],
                          [pred(it, "deepseek")[0] for it in close])
        say(f"| {LABEL[arm]} | {d * 100:+.1f} pts ({lo * 100:+.1f} to {hi * 100:+.1f}) | {mcnemar_exact(b, c):.3g} "
            f"| {kap:.3f} | {pct(hrate)} | {'yes' if passes else 'no'}"
            + (f" (F1 drop: {', '.join(f1_fail)})" if f1_fail else "") + " |")

    # --- the whole pipeline, close + single, weighted by the population
    pop = meta["population"]
    weights = {"close": pop.get("close", 0), "single": pop.get("single", 0)}
    every = [it for it in items if it["item_id"] in labelled]
    say("\n## Whole pipeline (close calls and single-candidate items, weighted by population)\n")
    say("| Arm | close | single | weighted | Δ vs today (95% CI) | passes |")
    say("|---|---|---|---|---|---|")
    whole = {}
    for arm in ["production"] + [a for a in ("embedding", "laya_all") if a in arms]:
        ok = {"close": [], "single": []}
        for it in every:
            p = pred(it, arm)
            ok[it["stratum"]].append(bool(p) and labelled[it["item_id"]][0] in p)
        whole[arm] = {k: np.array(v, dtype=float) for k, v in ok.items()}
    for arm, o in whole.items():
        acc = {k: (v.mean() if len(v) else float("nan")) for k, v in o.items()}
        wacc = sum(weights[k] * acc[k] for k in acc if acc[k] == acc[k]) / sum(
            weights[k] for k in acc if acc[k] == acc[k])
        if arm == "production":
            say(f"| {LABEL[arm]} | {pct(acc['close'])} | {pct(acc['single'])} | {pct(wacc)} | – | – |")
            continue
        d, lo, hi = stratified_bootstrap({k: o[k] - whole["production"][k] for k in o}, weights, rng)
        passes = jury_ok and lo > -MARGIN
        if arm == "laya_all":
            verdicts["laya_all_whole"] = passes
        say(f"| {LABEL[arm]} | {pct(acc['close'])} | {pct(acc['single'])} | {pct(wacc)} | {d * 100:+.1f} pts "
            f"({lo * 100:+.1f} to {hi * 100:+.1f}) | {'yes' if passes else 'no'} |")

    # --- two-Domain answers
    say("\n## Two-Domain answers (reported, not decisive)\n")
    sets = [(it, labelled[it["item_id"]][1]) for it in close if labelled[it["item_id"]][1]]
    for arm in arms:
        f = [set_f1(set(pred(it, arm) or []), g) for it, g in sets]
        j = [len(set(pred(it, arm) or []) & g) / len(set(pred(it, arm) or []) | g) for it, g in sets]
        say(f"- {LABEL[arm]}: set F1 {np.mean(f):.3f}, Jaccard {np.mean(j):.3f} over {len(sets)} close call(s).")
    say(f"- The jury named two Domains (each by 2+ judges) on {sum(len(g) > 1 for _, g in sets)} of them; "
        f"deepseek gave two on {meta.get('deepseek_two_domains')} close call(s) in the whole Library.")

    # --- Laya's confidence
    if laya:
        say("\n## Laya's confidence (temperature fitted on 30% of items, measured on the other 70%)\n")
        for arm, key in (("laya_cand", "cand"), ("laya_all", "all")):
            t = temps.get(arm)
            test = [it for it in (close if arm == "laya_cand" else every)
                    if not in_calibration(it["item_id"]) and it["item_id"] in laya]
            if not test or t is None:
                continue
            g = [labelled[it["item_id"]][0] for it in test]
            for name, tt in (("as shipped", 1.0), (f"refitted T={t:.2f}", t)):
                pr = [calibrated(laya[it["item_id"]][key], tt) for it in test]
                conf = [max(p.values()) for p in pr]
                corr = [max(p, key=p.get) == gg for p, gg in zip(pr, g)]
                say(f"- {LABEL[arm]}, {name}: ECE {ece(conf, corr):.3f}, Brier {brier(pr, g):.3f} ({len(test)} items).")
            if arm == "laya_cand":
                say("\n| Confidence ≥ | Share Laya answers | Laya accuracy there | Laya at or above, deepseek below: accuracy |")
                say("|---|---|---|---|")
                pr = [calibrated(laya[it["item_id"]][key], t) for it in test]
                for th in (0.5, 0.6, 0.7, 0.8, 0.9):
                    keep = [max(p.values()) >= th for p in pr]
                    acc_k = [max(p, key=p.get) == gg for p, gg, k in zip(pr, g, keep) if k]
                    hyb = [(max(p, key=p.get) == gg) if k else (gg in (it["deepseek"] or []))
                           for p, gg, k, it in zip(pr, g, keep, test)]
                    say(f"| {th} | {pct(sum(keep) / len(keep))} | {pct(np.mean(acc_k) if acc_k else float('nan'))} "
                        f"| {pct(np.mean(hyb))} |")
                say("")
    else:
        say("\n## Laya\n\nNot run yet (laya.jsonl missing): only the embedding and deepseek arms are scored.")

    # --- the verdict, by the rules fixed in advance
    say("\n## Verdict\n")
    if not jury_ok:
        v = f"**Inconclusive**: the jury agrees too little (Fleiss' kappa {fk:.3f} < {JURY_KAPPA}); nothing changes."
    elif verdicts.get("embedding"):
        v = "**Drop the tie-break stage**: embedding top-1 is non-inferior to deepseek; neither model is needed."
    elif laya and verdicts.get("laya_all_whole") and verdicts.get("laya_all"):
        v = "**Laya can replace both stages** (all Domains): non-inferior on close calls and on the whole pipeline."
    elif laya and verdicts.get("laya_cand"):
        v = "**Adopt Laya as the tie-break**: non-inferior to deepseek on close calls."
    elif laya:
        v = "**Keep deepseek**: no arm is non-inferior to it."
    else:
        v = "**Pending**: run Laya (laya_run.py) for the Laya arms; the embedding arm alone did not pass."
    say(v)
    say(f"\nRules (fixed before the run): 95% CI lower bound of Δ accuracy above −{MARGIN * 100:.0f} pts; "
        f"finance and investment F1 not more than {F1_DROP * 100:.0f} pts below deepseek (when ≥ {HIGH_TIER_MIN} "
        f"silver items); under {HARMFUL_MOVES:.0%} of close calls moved to a wrong Pack; jury Fleiss' kappa ≥ "
        f"{JURY_KAPPA}. Precedence: drop the tie-break > Laya for both stages > Laya as tie-break > keep deepseek.")
    REPORT.write_text("\n".join(lines) + "\n")

    dis = ["# Items the jury split on (private: stays in data/)\n"]
    for it in contested[:50]:
        votes_i = silver[it["item_id"]][2]
        dis.append(f"## {it['item_id']} ({it['stratum']})\n\nvotes: {', '.join(votes_i)} · candidates: "
                   f"{', '.join(it['candidates'])} · deepseek: {', '.join(it['deepseek'] or [])}\n\n{it['text']}\n")
    (OUT / "disputed.md").write_text("\n".join(dis))
    print(f"laya score: {v.replace('**', '')}")
    print(f"laya score: report in {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
