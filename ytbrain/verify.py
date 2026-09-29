"""Grounding verification: does this generated assertion actually appear in the
transcript?

Exact substring matching is the wrong tool (research doc 5.3): auto-captioned
text has no punctuation or casing, and models paraphrase slightly even when
asked for verbatim quotes, so exact matching produces false rejections at a
rate that makes the check useless. Word-level Jaccard over a sliding window is
what post-hoc alignment work found to work.

Pure stdlib, no model, no network - this runs on every assertion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import (EMPTY_RECORD_MIN_WORDS, EVIDENCE_CONTAINMENT_MIN_WORDS,
                     EVIDENCE_MIN_CONTAINMENT, EVIDENCE_MIN_JACCARD, EVIDENCE_WINDOW_SCALES,
                     NARRATIVE_CATEGORIES, NO_ADVICE_FLAG_MIN_WORDS)

_NORM = re.compile(r"[^a-z0-9' ]+")
# Disfluencies are in the auto-captions but models drop them when quoting, so a
# faithful quote of "it's um really uh powerful" scored as a mismatch.
_FILLERS = {"um", "uh", "umm", "uhh", "er", "erm", "ah", "hmm", "mm", "mhm"}
# A model quoting two nearby passages joins them with an ellipsis. Each piece is
# checked on its own; pieces this short are too generic to prove anything.
_ELLIPSIS = re.compile(r"\.{3,}|\u2026|\[\s*\.\.\.\s*\]")
MIN_SEGMENT_WORDS = 4


# caption annotations ([music], (laughs), [applause], (inaudible speech)) and footnote marks ([3]):
# short bracketed groups only. A long "( ... )" is prose -- essays have whole paragraphs in
# parentheses -- and must stay, or quotes from it can never be found.
_ANNOTATION = re.compile(r"\[[^\]]{0,40}\]|\((?:\s*[^()\s]+){1,3}\s*\)")


def normalize(text: str) -> list[str]:
    text = _ANNOTATION.sub(" ", text.lower())
    return [w for w in _NORM.sub(" ", text).split() if w not in _FILLERS]


@dataclass
class Match:
    score: float
    start_ms: int | None
    matched_text: str
    ok: bool


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _flatten(utterances: list[dict]) -> list[tuple[str, int]]:
    """(word, start_ms) stream, so any window maps back to a real timestamp."""
    flat: list[tuple[str, int]] = []
    for u in utterances:
        for w in normalize(u.get("text", "")):
            flat.append((w, int(u.get("start_ms", 0))))
    return flat


def _best_window(span_words: list[str], flat: list[tuple[str, int]],
                 threshold: float) -> Match:
    """Slide a span-sized window one word at a time; keep the best Jaccard.

    Step 1, not len/4: a coarse step lands a few words off the true start of a
    long quote and knocks a perfect match below threshold.
    """
    if not span_words or not flat:
        return Match(0.0, None, "", False)
    win = max(len(span_words), 4)
    target = set(span_words)
    best = Match(0.0, None, "", False)
    for i in range(0, max(1, len(flat) - win + 1)):
        window = flat[i:i + win]
        ws = {w for w, _ in window}
        score = len(target & ws) / len(target | ws)
        if score > best.score:
            best = Match(score, window[0][1], " ".join(w for w, _ in window),
                         score >= threshold)
            if score == 1.0:
                break
    return best


def _contained(span_words: list[str], flat: list[tuple[str, int]], jac: Match) -> Match:
    """Fallback for a quote the model tidied: >= EVIDENCE_MIN_CONTAINMENT of its
    words inside one window up to 1.5x its length. The score is then the
    containment, so every consumer's `match_score >= threshold` check agrees."""
    target = set(span_words)
    # distinct words: a looping "references references references ..." is not distinctive
    if len(target) < EVIDENCE_CONTAINMENT_MIN_WORDS or not flat:
        return jac
    best = (0.0, None, "")
    for scale in EVIDENCE_WINDOW_SCALES:
        win = int(round(len(span_words) * scale))
        for i in range(0, max(1, len(flat) - win + 1)):
            window = flat[i:i + win]
            c = len(target & {w for w, _ in window}) / len(target)
            if c > best[0]:
                best = (c, window[0][1], " ".join(w for w, _ in window))
    if best[0] >= EVIDENCE_MIN_CONTAINMENT:
        return Match(best[0], best[1], best[2], True)
    return jac


def record_issues(record: dict, utterances: list[dict]) -> list[str]:
    """Problems the evidence pass rate cannot see: a real talk that yielded nothing."""
    words = sum(len((u.get("text") or "").split()) for u in utterances)
    items = len(record.get("highlights") or []) + len(record.get("advice_atoms") or [])
    if not items and words >= EMPTY_RECORD_MIN_WORDS:
        return [f"no highlights or advice from {words} words"]
    if (not record.get("advice_atoms") and words >= NO_ADVICE_FLAG_MIN_WORDS
            and record.get("category") not in NARRATIVE_CATEGORIES):
        kind = "article" if record.get("source_kind") == "article" else "talk"
        return [f"no advice from a {record.get('category')} {kind} of {words} words"]
    return []


def find_evidence(span: str, utterances: list[dict],
                  threshold: float = EVIDENCE_MIN_JACCARD) -> Match:
    """Locate a claimed evidence span in the transcript.

    An ellipsis-joined quote ("A ... B") passes only if EVERY substantive piece
    is found; the score is the weakest piece's, and the timestamp is the first
    piece's -- the start of the evidence the agent will cite.
    """
    flat = _flatten(utterances)
    pieces = [normalize(p) for p in _ELLIPSIS.split(span)]
    pieces = [p for p in pieces if len(p) >= MIN_SEGMENT_WORDS] or \
             [w for w in [normalize(span)] if w]
    if not pieces:
        return Match(0.0, None, "", False)
    matches = [_best_window(p, flat, threshold) for p in pieces]
    matches = [m if m.ok else _contained(p, flat, m) for p, m in zip(pieces, matches)]
    weakest = min(matches, key=lambda m: m.score)
    return Match(weakest.score, matches[0].start_ms,
                 " ... ".join(m.matched_text for m in matches), all(m.ok for m in matches))


def verify_record(record: dict, utterances: list[dict],
                  threshold: float = EVIDENCE_MIN_JACCARD) -> dict:
    """Verify every grounded assertion in an extraction record.

    Returns a report, and mutates each assertion in place with `match_score`
    and a corrected `timestamp_ms` taken from the transcript rather than from
    the model - models are unreliable at arithmetic on timecodes, and the
    transcript already knows the answer.
    """
    checked = failed = 0
    details = []
    for field in ("highlights", "advice_atoms"):
        for item in record.get(field) or []:
            span = item.get("evidence_span") or ""
            m = find_evidence(span, utterances, threshold)
            item["match_score"] = round(m.score, 3)
            if m.ok and m.start_ms is not None:
                item["timestamp_ms"] = m.start_ms
            checked += 1
            if not m.ok:
                failed += 1
                details.append({"field": field, "span": span[:120], "score": round(m.score, 3)})

    pass_rate = 1.0 if checked == 0 else (checked - failed) / checked
    record.setdefault("extraction_meta", {})["verification"] = {
        "checked": checked,
        "failed": failed,
        "pass_rate": round(pass_rate, 3),
        "threshold": threshold,
    }
    issues = record_issues(record, utterances)
    if issues:           # only when present: unchanged records keep byte-identical files
        record["extraction_meta"]["verification"]["issues"] = issues
    record["extraction_meta"]["validation_status"] = (
        "failed" if pass_rate < 0.6 else "flagged" if pass_rate < 0.9 or issues else "pass"
    )
    return {"checked": checked, "failed": failed, "pass_rate": pass_rate, "details": details,
            "issues": issues}
