"""Released benchmark files (docs/eval-spec.md §3): BEIR layout, TREC copies, canonical
Moment labels and a checksum list. Written atomically; each split replaces only its own
rows, so building one split never touches the other."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..config import ARTICLE_MOMENT_PARAS, MOMENT_S
from ..pages import atomic_write_text
from .moments import is_article_moment, moment_url, parse_moment_id

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
                version: str | None = None) -> None:
    """queries: records per eval-spec §4. labels: {qid: {moment_id: {"grade": g, "judges": {...}}}}.
    `version` defaults to the version already released (never silently reset)."""
    version = version or current_version(root)
    keep = lambda rows: [r for r in rows if r.get("split") != split]
    _write_jsonl(root / "queries.jsonl",
                 sorted(keep(_read_jsonl(root / "queries.jsonl")) + queries, key=lambda r: r["_id"]))
    def span(q: str, m: str, lab: dict) -> dict:
        doc, start = parse_moment_id(m)
        where = ({"doc_id": doc, "start_paragraph": start, "end_paragraph": start + ARTICLE_MOMENT_PARAS}
                 if is_article_moment(m) else
                 {"youtube_id": doc, "start_ms": start * 1000, "end_ms": (start + MOMENT_S) * 1000})
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
    corpus = {r["_id"]: r for r in _read_jsonl(root / "corpus.jsonl")}
    for s in spans:
        doc, start = parse_moment_id(s["moment_id"])
        info = talk_info.get(doc, {})
        where = ({"doc_id": doc, "start_paragraph": start, "end_paragraph": start + ARTICLE_MOMENT_PARAS}
                 if is_article_moment(s["moment_id"]) else
                 {"youtube_id": doc, "start_s": start, "end_s": start + MOMENT_S})
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


def load_split(root: Path, split: str) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """(queries, qrels) of one split from the released files."""
    queries = [q for q in _read_jsonl(root / "queries.jsonl") if q.get("split") == split]
    qrels: dict[str, dict[str, int]] = {}
    path = root / "trec" / f"{split}.qrels"
    if path.exists():
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) == 4:
                qrels.setdefault(parts[0], {})[parts[2]] = int(parts[3])
    return queries, qrels
