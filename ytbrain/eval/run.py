"""`ytbrain eval run`: score a search configuration on one split (docs/eval-spec.md §7).

Deterministic and model-free apart from the local search models: search each question,
map results to Moments, compute metrics per question, and report means with 95%
bootstrap intervals, breakdowns and, against a saved baseline, paired randomization
tests (Holm-corrected) plus the regression gate.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from ..config import EVAL_DATA, EVAL_DIR, EVAL_PRIVATE
from ..knowledge.search import POLICIES, search
from ..pages import atomic_write_text
from . import metrics as M
from .files import load_set
from .splits import SETS
from .moments import (
    moment_for,
    moment_kind,
    parse_moment_id,
    results_to_moments,
    results_to_talks,
)

CONFIGS = {
    "full": {"rerank": True, "stage_boost": False},
    "no-rerank": {"rerank": False, "stage_boost": False},
    "stage-boost": {"rerank": True, "stage_boost": True},
    # the Knowledge pack the plugin ships: ONNX models, no Passages [ADR-0009]
    "pack": {"rerank": True, "stage_boost": False, "backend": "pack"},
    "pack-no-rerank": {"rerank": False, "stage_boost": False, "backend": "pack"},
    # the full index and models on the pack's content (no Passages): separates the cost of
    # dropping Passages from the cost of the smaller ONNX models
    "full-no-passages": {"rerank": True, "stage_boost": False, "kinds": ["advice", "takeaway", "summary"]},
    # candidate diversity policies (founder_coach.search.POLICIES), source-agnostic: adopted as the
    # default only when they don't lower nDCG@10 overall or for any source kind's questions
    "full-series3": {"rerank": True, "stage_boost": False, "diversity": "series3"},
    "full-neardup": {"rerank": True, "stage_boost": False, "diversity": "neardup"},
    "pack-series3": {"rerank": False, "stage_boost": False, "backend": "pack", "diversity": "series3"},
    "pack-neardup": {"rerank": False, "stage_boost": False, "backend": "pack", "diversity": "neardup"},
}
PACK_KINDS = ["advice", "takeaway", "summary"]
# below this share of judged top-10 Moments, a comparison says more about the pool than the
# system: grade the run's new Moments first (`ytbrain eval judge`)
JUDGED_MIN = 0.90
EXIT_FAIL, EXIT_INCONCLUSIVE = 1, 3
GATE = {"ndcg@10": 0.05, "recall@10": 0.05}
PRIMARY = "ndcg@10"          # decided before looking (eval-spec §7): tested unadjusted
FACETS = ("question_type", "origin", "popularity", "stage", "topic", "seed_kind")
# Every source kind's questions are gated too, so no source is traded away for another: a group
# of at least KIND_GATE_MIN_N questions fails when its nDCG@10 drops more than KIND_GATE, and
# the drop is significant (paired randomization p < KIND_GATE_P)
KIND_GATE, KIND_GATE_MIN_N, KIND_GATE_P = 0.03, 10, 0.05
DOC_NDCG = "doc_ndcg@10"     # Document-level nDCG@10: a Document's grade is its best Moment's
# ADR-0009: the plugin ships the pack unless it loses more than 0.03 nDCG@10 to `full`
PACK_GATE = {"ndcg@10": 0.03}


def base_config(name: str) -> str:
    """The configuration a (possibly labelled) run name belongs to: `pack-no-rerank-arctic` ->
    `pack-no-rerank` (the longest known configuration it starts with)."""
    if name in CONFIGS:
        return name
    known = [c for c in CONFIGS if name.startswith(c + "-")]
    if not known:
        raise RuntimeError(f"unknown configuration {name!r}; known: {', '.join(CONFIGS)}")
    return max(known, key=len)


def gate_for(config: str) -> dict[str, float]:
    """The regression gate of a configuration (a named baseline like `prebooks` gets the default)."""
    try:
        base = base_config(config)
    except RuntimeError:
        return GATE
    return PACK_GATE if CONFIGS[base].get("backend") == "pack" else GATE
RESULT_DEPTH = 50            # items requested per question


def run_config(store, embed, reranker, queries: list[dict], config: str) -> dict:
    """{qid: {"moments": [...], "talks": [...], "scores": [...]}} for one configuration."""
    cfg = CONFIGS[config]
    out = {}
    started = time.time()
    every = max(1, min(25, len(queries) // 10 or 1))
    print(f"eval run: searching {len(queries)} questions with config {config} ...", flush=True)
    for i, q in enumerate(queries, 1):
        stage = (q.get("stage") or [None])[0] if cfg["stage_boost"] else None
        res = search(store, embed, q["text"], stage=stage, top_k=RESULT_DEPTH, kinds=cfg.get("kinds"),
                     reranker=reranker if cfg["rerank"] else None,
                     diversity=POLICIES[cfg.get("diversity", "default")])
        moments = results_to_moments(res)
        best: dict[str, float] = {}
        for r in res:                              # a Moment's score = its best item's score
            m = moment_for(r["doc_id"], r.get("start_ms")) if r.get("kind") != "summary" else None
            if m and m not in best:
                best[m] = float(r.get("score") or 0.0)
        out[q["_id"]] = {"moments": moments, "talks": results_to_talks(res),
                         "scores": [best.get(m, 0.0) for m in moments]}
        if i % every == 0 or i == len(queries):
            per = (time.time() - started) / i
            print(f"    search: {i}/{len(queries)}, {per:.1f}s/question, "
                  f"ETA {per * (len(queries) - i) / 60:.1f} min", flush=True)
    print("eval run: scoring (bootstrap intervals, 10 000 resamples per metric) ...", flush=True)
    return out


def doc_qrels(qrels: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Document-level labels: a Document's grade is its best Moment's grade (any source kind)."""
    out: dict[str, dict[str, int]] = {}
    for q, ms in qrels.items():
        for m, g in ms.items():
            d = parse_moment_id(m)[0]
            out.setdefault(q, {})[d] = max(out.get(q, {}).get(d, 0), g)
    return out


def facet_keys(q: dict, facet: str) -> list[str]:
    """A question's groups for one facet. `seed_kind` is the source kind of the Moment the
    question was written from: one group per kind, so each source's questions are measured."""
    if facet == "seed_kind":
        seed = (q.get("creation") or {}).get("seed_moment")
        return [moment_kind(seed)] if seed else ["(none)"]
    keys = q.get(facet) or ["(none)"]
    return keys if isinstance(keys, list) else [keys]


def score(queries: list[dict], qrels: dict[str, dict[str, int]], runs: dict) -> dict:
    """Scores over the questions this run was asked (`runs`): a labelled question it never saw
    (written after the run was saved) is left out, not scored zero."""
    qrels = {q: v for q, v in qrels.items() if q in runs}
    moment_run = {q: r["moments"] for q, r in runs.items()}
    per = M.per_query(qrels, moment_run)
    if any(r.get("talks") for r in runs.values()):    # older run files have no Document ranking
        per[DOC_NDCG] = M.per_query(doc_qrels(qrels), {q: r["talks"] for q, r in runs.items()},
                                    {"x": M.METRICS["ndcg@10"]})["x"]
    summary = {}
    for name, vals in per.items():
        lo, hi = M.bootstrap_ci(list(vals.values()))
        summary[name] = {"mean": round(M.mean(vals.values()), 4), "ci95": [round(lo, 4), round(hi, 4)],
                         "n": len(vals)}
    # Diagnostic (not compared or gated): what the top 10 is made of, by source kind
    summary["_mix@10"] = M.kind_mix([moment_run.get(q, []) for q in per["ndcg@10"]])
    summary["_ceilings"] = {"recall@10": round(M.ceiling(qrels, 10), 4),
                            "recall@10-l2": round(M.ceiling(qrels, 10, 2), 4),
                            "recall@50": round(M.ceiling(qrels, 50), 4)}
    groups: dict[str, dict] = {}
    for facet in FACETS:
        buckets = defaultdict(list)
        for q in queries:
            for k in facet_keys(q, facet):
                if q["_id"] in per["ndcg@10"]:
                    buckets[k].append(per["ndcg@10"][q["_id"]])
        groups[facet] = {k: {"ndcg@10": round(M.mean(v), 4), "n": len(v)} for k, v in sorted(buckets.items())}
    kinds = {q["_id"]: facet_keys(q, "seed_kind")[0] for q in queries if q["_id"] in per["ndcg@10"]}
    return {"summary": summary, "by": groups, "per_query": per, "seed_kind": kinds}


def paired_delta(a: dict[str, float], b: dict[str, float]) -> float:
    qs = sorted(set(a) & set(b))
    return M.mean([a[q] - b[q] for q in qs]) if qs else 0.0


def question_overlap(current: dict, baseline: dict) -> dict:
    """Which questions the two results share: the ones only the current run was asked (added to
    the set since the baseline) and the ones only the baseline was asked (retired since)."""
    cur = set(current["per_query"].get(PRIMARY, {}))
    base = set(baseline.get("per_query", {}).get(PRIMARY, {}))
    new_kinds: dict[str, int] = {}
    for q in cur - base:
        kind = (current.get("seed_kind") or {}).get(q, "(none)")
        new_kinds[kind] = new_kinds.get(kind, 0) + 1
    return {"shared": len(cur & base), "new": len(cur - base), "retired": len(base - cur),
            "new_by_kind": dict(sorted(new_kinds.items()))}


def compare(current: dict, baseline: dict, gate: dict[str, float] = GATE) -> dict:
    """Per metric: the paired difference with its 95% bootstrap interval, a paired
    randomization p-value, and the regression gate. The primary metric (nDCG@10) is
    tested on its own; the secondary metrics are Holm-corrected among themselves, so
    eight correlated metrics don't dilute the one we decide on."""
    shared = [m for m in current["per_query"] if m in baseline["per_query"]]
    p = {m: M.paired_randomization(current["per_query"][m], baseline["per_query"][m]) for m in shared}
    adj = M.holm({m: v for m, v in p.items() if m != PRIMARY})
    if PRIMARY in p:
        adj[PRIMARY] = p[PRIMARY]
    out = {}
    for m in shared:
        # over the questions both scored, like the interval and p: a question one run was never
        # asked (added to the set later) is not a zero, it is outside the comparison
        delta = paired_delta(current["per_query"][m], baseline["per_query"][m])
        lo, hi = M.paired_bootstrap_ci(current["per_query"][m], baseline["per_query"][m])
        out[m] = {"delta": round(delta, 4), "ci95": [round(lo, 4), round(hi, 4)],
                  "p": round(adj[m], 4), "p_adjust": "none (primary)" if m == PRIMARY else "holm",
                  "fails_gate": m in gate and delta < -gate[m]}
    return out


def compare_kinds(current: dict, baseline: dict) -> dict:
    """nDCG@10 per source kind of the question's seed, paired against the baseline on the
    questions both scored. A kind with at least KIND_GATE_MIN_N questions fails its gate when it
    drops more than KIND_GATE and the drop is significant."""
    cur, base = current["per_query"].get(PRIMARY, {}), baseline.get("per_query", {}).get(PRIMARY, {})
    groups: dict[str, list[str]] = {}
    for q, kind in (current.get("seed_kind") or {}).items():
        if q in base and q in cur:
            groups.setdefault(kind, []).append(q)
    out = {}
    for kind, qs in sorted(groups.items()):
        a, b = {q: cur[q] for q in qs}, {q: base[q] for q in qs}
        delta = M.mean(a.values()) - M.mean(b.values())
        lo, hi = M.paired_bootstrap_ci(a, b)
        p = M.paired_randomization(a, b)
        big = len(qs) >= KIND_GATE_MIN_N
        out[kind] = {"n": len(qs), "delta": round(delta, 4), "ci95": [round(lo, 4), round(hi, 4)],
                     "p": round(p, 4), "gated": big,
                     "fails_gate": big and delta < -KIND_GATE and p < KIND_GATE_P}
    return out


def refresh_baseline_command(split: str, name: str | None, baseline: dict) -> str:
    """The free command that scores a saved baseline again against the current labels. A baseline
    named after another run (`--as full-prebooks`) remembers its run file, so the same run is
    re-scored under the same name; any other baseline is its configuration's newest run."""
    src = baseline.get("rescored_from") or ""
    m = RUN_NAME.fullmatch(src)
    if m:
        return (f"ytbrain eval rescore --set {split} --config {m['config']} --run {src} "
                f"--as {name} --save-baseline")
    return f"ytbrain eval rescore --set {split} --config {name} --save-baseline"


def verdict(result: dict, baseline: dict, config: str) -> tuple[int, dict]:
    """(exit code, {"status", "reasons"}): a failed gate counts only when the comparison is
    sound -- both scored against the same labels, and this run's top 10 mostly judged."""
    reasons = []
    if baseline.get("qrels_sha256") and baseline["qrels_sha256"] != result.get("qrels_sha256"):
        reasons.append("the baseline was scored against different labels: re-score it with "
                       f"`{refresh_baseline_command(result['split'], result.get('compared_with'), baseline)}`")
    elif not baseline.get("qrels_sha256"):
        reasons.append("the baseline predates label tracking; if labels changed since, re-save it")
    judged = result["summary"].get("judged@10", {}).get("mean", 1.0)
    if judged < JUDGED_MIN:
        reasons.append(f"only {judged:.0%} of this run's top-10 Moments were judged (the pool came from "
                       f"other systems), so its scores are underestimates: run `ytbrain eval judge "
                       f"--set {result['split']} --config {config}`, then rescore")
    blocking = [r for r in reasons if not r.startswith("the baseline predates")]
    failed = any(c["fails_gate"] for c in result.get("compare", {}).values()) or \
        any(c["fails_gate"] for c in result.get("compare_kinds", {}).values())
    if blocking:
        return EXIT_INCONCLUSIVE, {"status": "inconclusive", "reasons": reasons}
    return (EXIT_FAIL if failed else 0), {"status": "fail" if failed else "pass", "reasons": reasons}


def write_trec_run(path: Path, runs: dict, tag: str) -> None:
    """The ranked Moments in TREC run format, so trec_eval or ranx can re-score them, plus the
    ranked talks in `<name>.talks.run` (for talk_ndcg@10 when re-scoring)."""
    lines, talks = [], []
    for q in sorted(runs):
        for rank, (m, sc) in enumerate(zip(runs[q]["moments"], runs[q].get("scores") or []), 1):
            lines.append(f"{q} Q0 {m} {rank} {sc:.6f} {tag}")
        for rank, t in enumerate(runs[q].get("talks") or [], 1):
            talks.append(f"{q} Q0 {t} {rank} {1.0 / rank:.6f} {tag}")
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    atomic_write_text(path.with_name(path.stem + ".talks.run"), "\n".join(talks) + ("\n" if talks else ""))


def load_run(path: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """({qid: ranked Moments}, {qid: ranked talks}) from a saved run (talks may be empty)."""
    def read(p: Path) -> dict[str, list[str]]:
        rows: dict[str, list[tuple[int, str]]] = {}
        if p.exists():
            for line in p.read_text().splitlines():
                parts = line.split()
                if len(parts) == 6:
                    rows.setdefault(parts[0], []).append((int(parts[3]), parts[2]))
        return {q: [d for _, d in sorted(v)] for q, v in rows.items()}
    return read(path), read(path.with_name(path.stem + ".talks.run"))


# a set name may contain "-" (dev-articles): known sets first, so `dev-articles-full-...` is not
# read as set `dev`, config `articles-full`
RUN_NAME = re.compile(r"(?P<split>" + "|".join(sorted(SETS, key=len, reverse=True)) + r"|[a-z]+)"
                      r"-(?P<config>[a-z0-9-]+)-(?P<at>\d{4}-\d\d-\d\dT\d{6})\.run")


def latest_run(out_dir: Path, split: str, config: str) -> Path | None:
    """The newest saved run of exactly this configuration. The config name is read from the file
    name up to its timestamp, so `pack` never picks up a `pack-no-rerank` run."""
    runs = []
    for p in out_dir.glob(f"{split}-*.run"):
        m = RUN_NAME.fullmatch(p.name)
        if m and m["split"] == split and m["config"] == config:
            runs.append((m["at"], p))
    return max(runs)[1] if runs else None


def qrels_sha(qrels: dict[str, dict[str, int]]) -> str:
    canon = "".join(f"{q}\t{m}\t{g}\n" for q in sorted(qrels) for m, g in sorted(qrels[q].items()))
    return hashlib.sha256(canon.encode()).hexdigest()


def reachable(store, kinds: list[str] | None) -> set[str] | None:
    """Moments the configuration can return at all (a store without Passages can't reach
    Moments that have no Advice or Takeaway); None if the store can't list its items."""
    if not hasattr(store, "rows"):
        return None
    try:
        rows = store.rows(kinds=kinds, columns=["kind", "doc_id", "start_ms"])
    except Exception:              # noqa: BLE001 -- a diagnostic, never a reason to fail the run
        return None
    return {m for r in rows if r.get("kind") != "summary"
            for m in [moment_for(r["doc_id"], r.get("start_ms"))] if m}


def evaluate(store, embed, reranker, split: str, config: str, root: Path = EVAL_DIR,
             out_dir: Path = EVAL_DATA / "runs", baseline: str | None = None,
             save_baseline: bool = False, label: str | None = None,
             private: Path | None = EVAL_PRIVATE) -> tuple[dict, int]:
    if label and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", label):
        raise RuntimeError("--label must be lowercase letters, digits and dashes, e.g. arctic-passages")
    queries, qrels = load_set(root, split, private)
    if not queries:
        raise RuntimeError(f"no {split} questions in {root} -- run `ytbrain eval build --set {split}` first")
    answerable = [q for q in queries if q.get("answerable", True)]
    if baseline:
        _baseline_path(out_dir, split, baseline)          # fail before a 20-minute search, not after
    runs = run_config(store, embed, reranker, answerable, config)
    pack_kinds = (getattr(store, "meta", None) or {}).get("kinds") or PACK_KINDS
    reach = reachable(store, CONFIGS[config].get("kinds") or
                      (pack_kinds if CONFIGS[config].get("backend") == "pack" else None))
    name = f"{config}-{label}" if label else config
    return _finish(answerable, qrels, runs, split, name, out_dir, baseline, save_baseline,
                   reach=reach, write_runs=True)


def rescore(split: str, config: str, root: Path = EVAL_DIR, out_dir: Path = EVAL_DATA / "runs",
            baseline: str | None = None, save_baseline: bool = False,
            private: Path | None = EVAL_PRIVATE, run: Path | None = None,
            name: str | None = None) -> tuple[dict, int]:
    """Score a saved run again, against the current labels, without searching (seconds).
    `run` picks a saved run file (default: the newest of `config`); `name` saves it under another
    name, so any past run can be a named baseline (`--run dev-full-...run --as full-prebooks`)."""
    queries, qrels = load_set(root, split, private)
    if name and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise RuntimeError(f"--as {name!r}: use lowercase letters, digits and '-'")
    path = (run if run.is_absolute() or run.exists() else out_dir / run) if run else latest_run(out_dir, split, config)
    if path is None or not path.exists():
        raise RuntimeError(f"no saved {split} run for {config} -- run `ytbrain eval run --set {split} "
                           f"--config {config}` first" if run is None else f"no run file {path}")
    config = name or config
    moments, talks = load_run(path)
    asked = asked_questions(path, moments)
    answerable = [q for q in queries if q.get("answerable", True) and q["_id"] in asked]
    not_asked = sum(1 for q in queries if q.get("answerable", True) and q["_id"] not in asked)
    runs = {q["_id"]: {"moments": moments.get(q["_id"], []), "talks": talks.get(q["_id"], [])}
            for q in answerable}
    print(f"eval rescore: {config} from {path.name} against the current labels"
          + (f" ({not_asked} question(s) added since that run: not scored, not zero)" if not_asked else ""),
          flush=True)
    return _finish(answerable, qrels, runs, split, config, out_dir, baseline, save_baseline,
                   reach=None, write_runs=False, source=path.name)


def asked_questions(path: Path, moments: dict[str, list[str]]) -> set[str]:
    """The questions a saved run was asked: its `.asked` file, or, for runs saved before those
    existed, every question it ranked something for (hybrid search always returns results)."""
    listed = path.with_suffix(".asked")
    if listed.exists():
        return {line.strip() for line in listed.read_text().splitlines() if line.strip()}
    return set(moments)


def baseline_run(out_dir: Path, split: str, name: str) -> Path | None:
    """The saved run a baseline was scored from, so it can be scored again against new labels."""
    p = out_dir / f"baseline-{split}-{name}.json"
    if not p.exists():
        return None
    base = json.loads(p.read_text())
    src = base.get("rescored_from") or f"{split}-{base.get('config', name)}-{str(base.get('at', '')).replace(':', '')}.run"
    run = out_dir / src
    return run if run.exists() else None


def _baseline_path(out_dir: Path, split: str, baseline: str) -> Path:
    p = out_dir / f"baseline-{split}-{baseline}.json"
    if not p.exists():
        raise RuntimeError(f"no saved baseline named {baseline} -- save one with `ytbrain eval run --set {split} "
                           f"--config {baseline} --save-baseline` (a configuration), or "
                           f"`ytbrain eval rescore --set {split} --config <config> --run <file>.run "
                           f"--as {baseline} --save-baseline` (a saved run)")
    return p


def _finish(queries, qrels, runs, split, config, out_dir, baseline, save_baseline, *,
            reach=None, write_runs=True, source=None) -> tuple[dict, int]:
    at = dt.datetime.now().isoformat(timespec="seconds")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{split}-{config}-{at.replace(':', '')}"
    if write_runs:
        # saved before scoring: a Ctrl+C during the bootstrap keeps the (slow) search, and
        # `eval rescore` can finish the job
        write_trec_run(out_dir / f"{stem}.run", runs, f"ytbrain-{config}")
        atomic_write_text(out_dir / f"{stem}.asked", "".join(f"{q}\n" for q in sorted(runs)))
    result = score(queries, qrels, runs)
    result.update({"split": split, "config": config, "at": at,
                   "n_questions": len(queries), "qrels_sha256": qrels_sha(qrels)})
    if source:
        result["rescored_from"] = source
    if reach is not None:
        rel = [(m in reach) for q in qrels.values() for m, g in q.items() if g >= 1]
        result["summary"]["_ceilings"]["reachable"] = round(sum(rel) / len(rel), 4) if rel else 0.0
    code = 0
    if baseline:
        base = json.loads(_baseline_path(out_dir, split, baseline).read_text())
        result["compare"] = compare(result, base, gate_for(config))
        result["compare_kinds"] = compare_kinds(result, base)
        result["overlap"] = question_overlap(result, base)
        result["compared_with"] = baseline
        code, result["verdict"] = verdict(result, base, config)
    atomic_write_text(out_dir / f"{stem}.json", json.dumps(result, indent=1))   # with the comparison
    if save_baseline:
        atomic_write_text(out_dir / f"baseline-{split}-{config}.json", json.dumps(result, indent=1))
    return result, code


def render(result: dict) -> str:
    lines = [f"eval {result['split']} · config {result['config']} · {result['n_questions']} questions", ""]
    ceilings = result["summary"].get("_ceilings", {})
    if "reachable" in ceilings:
        lines.append(f"  relevant Moments this configuration can return at all: {ceilings['reachable']:.1%}")
    if "rescored_from" in result:
        lines.append(f"  (re-scored from {result['rescored_from']} against the current labels)")
    if len(lines) > 2:
        lines.append("")
    for name, s in result["summary"].items():
        if name.startswith("_"):
            continue
        cap = f"   (best possible {ceilings[name]:.4f})" if name in ceilings else ""
        lines.append(f"  {name:14} {s['mean']:.4f}   95% CI [{s['ci95'][0]:.4f}, {s['ci95'][1]:.4f}]{cap}")
    mix = result["summary"].get("_mix@10") or {}
    if mix:
        lines.append("  top-10 mix by source kind: " + ", ".join(f"{k} {v:.1%}" for k, v in mix.items()))
    for facet in FACETS:
        groups = result["by"].get(facet) or {}
        if facet in ("stage", "topic"):          # long tails: show the groups big enough to read
            groups = {k: v for k, v in groups.items() if v["n"] >= 10}
        if not groups:
            continue
        lines.append(f"\n  nDCG@10 by {facet}:" + ("  (groups with n >= 10)" if facet in ("stage", "topic") else ""))
        for k, v in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
            small = "  (too few to compare)" if v["n"] < 10 else ""
            lines.append(f"    {k:28} {v['ndcg@10']:.4f}  (n={v['n']}){small}")
    if "compare" in result:
        ov = result.get("overlap") or {}
        lines.append(f"\n  vs baseline {result.get('compared_with', '')} "
                     f"(difference, its 95% CI, p; nDCG@10 unadjusted, others Holm-corrected):")
        if ov.get("new") or ov.get("retired"):
            kinds = ", ".join(f"{k} {n}" for k, n in (ov.get("new_by_kind") or {}).items())
            lines.append(f"    compared on the {ov['shared']} question(s) both were asked"
                         + (f"; {ov['new']} new since the baseline ({kinds}), not compared" if ov.get("new") else "")
                         + (f"; {ov['retired']} retired since" if ov.get("retired") else ""))
        for m, c in result["compare"].items():
            flag = "  FAILS GATE" if c["fails_gate"] else ""
            ci = c.get("ci95") or [0, 0]
            lines.append(f"    {m:14} {c['delta']:+.4f}  [{ci[0]:+.4f}, {ci[1]:+.4f}]  "
                         f"p={c.get('p', c.get('p_holm', 1.0)):.3f}{flag}")
    if result.get("compare_kinds"):
        lines.append(f"\n  nDCG@10 by the source kind of the question's seed, vs the baseline "
                     f"(gated: n >= {KIND_GATE_MIN_N}, drop > {KIND_GATE}, p < {KIND_GATE_P}):")
        for kind, c in result["compare_kinds"].items():
            flag = "  FAILS GATE" if c["fails_gate"] else ("" if c["gated"] else "  (too few to gate)")
            lines.append(f"    {kind:14} {c['delta']:+.4f}  [{c['ci95'][0]:+.4f}, {c['ci95'][1]:+.4f}]  "
                         f"p={c['p']:.3f}  (n={c['n']}){flag}")
    new_kinds = [k for k in (result.get("overlap") or {}).get("new_by_kind", {})
                 if k not in (result.get("compare_kinds") or {})]
    if "compare_kinds" in result and new_kinds:
        lines.append(f"    {', '.join(new_kinds)}: no baseline yet (every such question is new); "
                     f"this run's score is the first reference")
    if "verdict" in result:
        v = result["verdict"]
        lines.append(f"\n  verdict: {v['status'].upper()}")
        lines += [f"    - {r}" for r in v["reasons"]]
    return "\n".join(lines)
