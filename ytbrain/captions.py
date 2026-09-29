"""SRT caption parsing -> cue-level timestamped utterances.

FORMAT DECISION (revised 21 Sep 2026, see research doc addendum):
srt, not json3. json3 was originally chosen for word-level timing and for the
`aAppend` flags that make rolling-caption dedup reliable. Word-level timing is
out of scope, and yt-dlp PR #13411 (merged mid-2025) added `srt` as a format
requested natively from YouTube's timedtext endpoint precisely because it does
not carry the rolling duplication. Note the trap: `--convert-subs srt` does NOT
help, because converting an already-duplicated VTT carries the duplication
across. Request `--sub-format "srt/best"` instead.

`dedup_rolling` is retained as an idempotent safety net: the native-srt claim
comes from the PR author and issue #1734 still reads open, so on clean input
this is a no-op and on duplicated input it saves the corpus.

Pure stdlib: this is the most load-bearing module and should be testable with
nothing installed.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable

from .config import UTTERANCE_GAP_MS, UTTERANCE_MAX_MS


@dataclass
class Event:
    start_ms: int
    dur_ms: int
    text: str


@dataclass
class Utterance:
    start_ms: int
    end_ms: int
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")                     # <c>, <00:00:01.234>, <i> etc.
_BRACKETED = re.compile(r"\[(music|applause|laughter|inaudible)[^\]]*\]", re.I)
_TIMING = re.compile(
    r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2})[,.](?P<sms>\d{3})"
    r"\s*-->\s*"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2})[,.](?P<ems>\d{3})"
)


def _ts_to_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def parse_srt(raw: str) -> list[Event]:
    """Parse SRT (or WebVTT, whose cue timing differs only in the ms separator)
    into (start, duration, text) events.

    Deliberately lenient: real caption files carry BOM markers, CRLF, stray
    blank lines, positioning cues and inline tags. A strict parser fails on a
    corpus this size, and a dropped video is worse than a slightly messy line.
    """
    events: list[Event] = []
    block: list[str] = []

    def flush(lines: list[str]) -> None:
        timing = None
        text_lines: list[str] = []
        for ln in lines:
            m = _TIMING.search(ln)
            if m and timing is None:
                timing = m
                continue
            if timing is None and (ln.strip().isdigit() or not ln.strip()):
                continue        # cue index or padding before the timing line
            if timing is not None:
                text_lines.append(ln)
        if timing is None:
            return
        start = _ts_to_ms(timing["sh"], timing["sm"], timing["ss"], timing["sms"])
        end = _ts_to_ms(timing["eh"], timing["em"], timing["es"], timing["ems"])
        text = _WS.sub(" ", _TAGS.sub("", " ".join(text_lines))).strip()
        if text:
            events.append(Event(start, max(end - start, 0), text))

    for line in raw.replace("﻿", "").replace("\r\n", "\n").split("\n"):
        if line.strip():
            block.append(line)
        elif block:
            flush(block)
            block = []
    if block:
        flush(block)
    return events


def dedup_rolling(events: Iterable[Event]) -> list[Event]:
    """Collapse rolling-caption repetition. No-op on clean (srt) input.

    YouTube's auto-captions historically re-emit the tail of the previous cue so
    the text appears to scroll:
        t=0     "so the first thing"
        t=900   "so the first thing you should do"
        t=1800  "you should do is talk to users"
    Keep only each cue's novel suffix, matched on word sequences so minor
    tokenisation differences don't defeat it.
    """
    out: list[Event] = []
    prev_words: list[str] = []
    for ev in events:
        words = ev.text.split()
        if not words:
            continue
        novel = _novel_suffix(prev_words, words)
        if novel:
            out.append(Event(ev.start_ms, ev.dur_ms, " ".join(novel)))
        prev_words = words
    return out


def _novel_suffix(prev: list[str], cur: list[str]) -> list[str]:
    """Part of `cur` not already covered by `prev`: drops the longest suffix of
    `prev` that is a prefix of `cur`. Exact duplicates collapse to nothing."""
    if not prev:
        return cur
    if prev == cur:
        return []
    for k in range(min(len(prev), len(cur)), 0, -1):
        if prev[-k:] == cur[:k]:
            return cur[k:]
    return cur


def merge_utterances(events: Iterable[Event], max_ms: int = UTTERANCE_MAX_MS,
                     gap_ms: int = UTTERANCE_GAP_MS) -> list[Utterance]:
    """Group cues into ~10-25s utterances, breaking on long pauses.

    The utterance is the citable atom: its `start_ms` becomes
    `watch?v=<id>&t=<seconds>s` in a coach answer, so it must never be
    synthesised or guessed downstream. This is the ONLY time granularity the
    pipeline keeps - word-level timing is deliberately out of scope.
    """
    utterances: list[Utterance] = []
    cur_words: list[str] = []
    cur_start = cur_end = 0

    for ev in events:
        ev_end = ev.start_ms + max(ev.dur_ms, 0)
        if cur_words:
            if (ev_end - cur_start > max_ms) or (ev.start_ms - cur_end > gap_ms):
                utterances.append(Utterance(cur_start, cur_end, " ".join(cur_words)))
                cur_words, cur_start, cur_end = [], ev.start_ms, ev_end
        else:
            cur_start, cur_end = ev.start_ms, ev_end
        cur_words.extend(ev.text.split())
        cur_end = max(cur_end, ev_end)

    if cur_words:
        utterances.append(Utterance(cur_start, cur_end, " ".join(cur_words)))
    return utterances


def caption_quality(utterances: list[Utterance]) -> dict:
    """Diagnostics carried into the record so the stratified acceptance sample
    (see `ytbrain sample`) can target the videos most likely to be bad."""
    text = " ".join(u.text for u in utterances)
    words = text.split()
    n = max(len(words), 1)
    punct = sum(text.count(c) for c in ".?!")
    return {
        "words": len(words),
        "punct_per_100w": round(100 * punct / n, 2),
        "bracketed_markers": len(_BRACKETED.findall(text)),
        "type_token_ratio": round(len({w.lower() for w in words}) / n, 3),
        "looks_unpunctuated": punct / n < 0.005,
    }


def to_transcript(utterances: list[Utterance], meta: dict) -> dict:
    return {
        "doc_id": meta.get("doc_id"),
        "title": meta.get("title"),
        "series": meta.get("series"),
        "published_at": meta.get("published_at"),
        "caption_kind": meta.get("caption_kind"),
        "quality": caption_quality(utterances),
        "utterances": [u.to_dict() for u in utterances],
    }


def restore_punctuation(utterances: list[Utterance]) -> list[Utterance]:
    """Intentional no-op for phase 1 (decision Q14).

    Auto-captions arrive unpunctuated. Wiring a restoration model pulls in a
    torch dependency, and the fuzzy evidence matcher already normalises
    punctuation away before comparing, so grounding is largely unaffected.
    Revisit only if the stratified acceptance sample shows summaries degrading
    specifically on auto-captioned videos.
    """
    return utterances
