"""Released benchmark files (docs/eval-spec.md §3): BEIR layout, TREC copies, canonical
Moment labels and a checksum list. Written atomically; each split replaces only its own
rows, so building one split never touches the other."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import source_kinds
from ..pages import atomic_write_text
from .moments import moment_url, parse_moment_id

SPEC_VERSION = "1.0.0"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


def current_version(root: Path) -> str:
    p = root / "VERSION"
    return p.read_text().strip() if p.exists() else SPEC_VERSION


def bump_minor(version: str) -> str:
    """1.0.0 -> 1.1.0: labels or questions added (eval-spec §8)."""
    major, minor, *_ = (version.split(".") + ["0", "0"])[:3]
    return f"{int(major)}.{int(minor) + 1}.0"


def write_split(root: Path, split: str, queries: list[dict], labels: dict[str, dict[str, dict]],
                talk_info: dict[str, dict], answers: list[dict] | None = None,
                version: str | None = None, visibility=None) -> None:
    """queries: records per eval-spec §4. labels: {qid: {moment_id: {"grade": g, "judges": {...}}}}.
    `version` defaults to the version already released (never silently reset). With a
    `visibility` (ytbrain.visibility), a label on a private Document is refused (ADR-0014): those
    belong in the private overlay (`write_private`), and old private rows leave the corpus."""
    version = version or current_version(root)
    private = (lambda m: visibility.is_private_moment(m)) if visibility is not None else (lambda m: False)
    leaked = sorted(m for ms in labels.values() for m in ms if private(m))
    if leaked:
        raise ValueError(f"refusing to release {len(leaked)} label(s) on private Documents, e.g. {leaked[0]}")
    keep = lambda rows: [r for r in rows if r.get("split") != split]
    _write_jsonl(root / "queries.jsonl",
                 sorted(keep(_read_jsonl(root / "queries.jsonl")) + queries, key=lambda r: r["_id"]))
    def span(q: str, m: str, lab: dict) -> dict:
        doc, start = parse_moment_id(m)
        where = source_kinds.for_moment(m).span(doc, start)
        return {"qid": q, "moment_id": m, **where, "grade": lab["grade"], "judges": lab["judges"], "split": split}
    spans = [span(q, m, lab) for q, ms in sorted(labels.items()) for m, lab in sorted(ms.items())]
    _write_jsonl(root / "moments.jsonl",
                 sorted(keep(_read_jsonl(root / "moments.jsonl")) + spans,
                        key=lambda r: (r["qid"], r["moment_id"])))
    lines = ["query-id\tcorpus-id\tscore"] + [f"{s['qid']}\t{s['moment_id']}\t{s['grade']}" for s in spans]
    (root / "qrels").mkdir(parents=True, exist_ok=True)
    atomic_write_text(root / "qrels" / f"{split}.tsv", "\n".join(lines) + "\n")
    (root / "trec").mkdir(parents=True, exist_ok=True)
    atomic_write_text(root / "trec" / f"{split}.qrels",
                      "".join(f"{s['qid']} 0 {s['moment_id']} {s['grade']}\n" for s in spans))
    corpus = {r["_id"]: r for r in _read_jsonl(root / "corpus.jsonl") if not private(r["_id"])}
    for s in spans:
        doc, start = parse_moment_id(s["moment_id"])
        info = talk_info.get(doc, {})
        where = source_kinds.for_moment(s["moment_id"]).corpus_fields(doc, start)
        corpus[s["moment_id"]] = {"_id": s["moment_id"], "title": info.get("title", ""), "text": "", **where,
                                  "url": moment_url(s["moment_id"], info.get("url")),
                                  "speaker": info.get("speaker", ""),
                                  "published_at": info.get("published_at", "")}
    _write_jsonl(root / "corpus.jsonl", [corpus[k] for k in sorted(corpus)])
    if answers is not None:
        _write_jsonl(root / "answers.jsonl",
                     sorted(keep(_read_jsonl(root / "answers.jsonl")) + answers, key=lambda r: r["qid"]))
    set_version(root, version)


def set_version(root: Path, version: str) -> None:
    """Seal a release. CHECKSUMS is written first, already listing the NEW VERSION, and
    VERSION last: an interruption anywhere before that leaves the release inconsistent
    (see `consistent`), so the next run knows the labels on disk were never released."""
    write_checksums(root, version)
    atomic_write_text(root / "VERSION", version + "\n")


def _released_files(root: Path) -> list[Path]:
    # dotfiles (.DS_Store, a leftover .CHECKSUMS.tmp) and temp files are never part of a release
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name != "CHECKSUMS"
                  and not any(part.startswith(".") for part in p.relative_to(root).parts)
                  and not p.name.endswith(".tmp"))


def _checksum_lines(root: Path, version: str | None = None) -> str:
    out = []
    files = _released_files(root)
    if version is not None and root / "VERSION" not in files:
        files = sorted(files + [root / "VERSION"])
    for p in files:
        data = (version + "\n").encode() if version is not None and p == root / "VERSION" else p.read_bytes()
        out.append(f"{hashlib.sha256(data).hexdigest()}  {p.relative_to(root)}\n")
    return "".join(out)


def write_checksums(root: Path, version: str | None = None) -> None:
    atomic_write_text(root / "CHECKSUMS", _checksum_lines(root, version))


def consistent(root: Path) -> bool:
    """True when the files on disk are exactly the last sealed release (or nothing was
    released yet). False after an interrupted release or a hand edit."""
    p = root / "CHECKSUMS"
    if not p.exists():
        return not (root / "queries.jsonl").exists()
    return p.read_text() == _checksum_lines(root)


def _read_trec(path: Path, into: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    if path.exists():
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) == 4:
                into.setdefault(parts[0], {})[parts[2]] = int(parts[3])
    return into


def load_split(root: Path, split: str, private: Path | None = None) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """(queries, qrels) of one split from the released files, plus the private overlay when
    `private` names one (labels on private Sources' Moments; never released). One benchmark:
    the overlay only adds labels, so every question is scored across every Source you index."""
    queries = [q for q in _read_jsonl(root / "queries.jsonl") if q.get("split") == split]
    qrels = _read_trec(root / "trec" / f"{split}.qrels", {})
    if private is not None:
        queries += [q for q in _read_jsonl(private / "queries.jsonl") if q.get("split") == split]
        _read_trec(private / "trec" / f"{split}.qrels", qrels)
    return queries, qrels


def load_set(root: Path, name: str, private: Path | None = None) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """(queries, qrels) of a set to score: one split, or `all` (every Tuning split together:
    each source kind's questions, released and private, in one run with one row per kind)."""
    from .splits import members
    queries: list[dict] = []
    qrels: dict[str, dict[str, int]] = {}
    for split in members(name):
        q, r = load_split(root, split, private)
        queries += q
        qrels.update(r)
    return queries, qrels


def write_private(private: Path, split: str, labels: dict[str, dict[str, dict]],
                  queries: list[dict] | None = None) -> bool:
    """The private overlay of one split: labels on private Documents' Moments (and every label of
    the questions written from private Sources), with their judges, plus those questions.
    Git-ignored (data/), never released. Returns whether it changed."""
    spans = []
    for q, ms in sorted(labels.items()):
        for m, lab in sorted(ms.items()):
            doc, start = parse_moment_id(m)
            spans.append({"qid": q, "moment_id": m, **source_kinds.for_moment(m).span(doc, start),
                          "grade": lab["grade"], "judges": lab["judges"], "split": split})
    trec = "".join(f"{s['qid']} 0 {s['moment_id']} {s['grade']}\n" for s in spans)
    path = private / "trec" / f"{split}.qrels"
    old_q = [q for q in _read_jsonl(private / "queries.jsonl") if q.get("split") == split]
    new_q = sorted(queries or [], key=lambda r: r["_id"])
    if path.exists() and path.read_text() == trec and old_q == new_q:
        return False
    (private / "trec").mkdir(parents=True, exist_ok=True)
    keep_q = [q for q in _read_jsonl(private / "queries.jsonl") if q.get("split") != split]
    _write_jsonl(private / "queries.jsonl", sorted(keep_q + new_q, key=lambda r: r["_id"]))
    keep = [r for r in _read_jsonl(private / "moments.jsonl") if r.get("split") != split]
    _write_jsonl(private / "moments.jsonl", sorted(keep + spans, key=lambda r: (r["qid"], r["moment_id"])))
    atomic_write_text(path, trec)
    return True
