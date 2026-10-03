"""PDF text clean-up, so the text the model quotes is the text verification finds (verify.py
matches Evidence quotes word by word). Pure functions; nothing here knows about any parser."""
from __future__ import annotations

import re

LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
             "ﬅ": "st", "ﬆ": "st"}
QUOTES = {"‘": "'", "’": "'", "‚": "'", "‛": "'",
          "“": '"', "”": '"', "„": '"', "‟": '"', "′": "'", "″": '"'}
INVISIBLE = dict.fromkeys(map(ord, "­\u200b‌‍⁠﻿"), None)   # soft hyphen, zero-widths
SUPERSCRIPTS = "¹²³⁰⁴⁵⁶⁷⁸⁹"

_TABLE = str.maketrans({**LIGATURES, **QUOTES})
_SUPER_MARK = re.compile(f"[{SUPERSCRIPTS}*†‡]+")                  # ¹²³, *, †, ‡ as note marks
# a note number glued to the end of a sentence or word: "future.12 Then" -> "future. Then"
_DIGIT_MARK = re.compile(r"(?<=[a-z][.,;:!?\"')])\d{1,3}(?=\s|$)")
_LINE_HYPHEN = re.compile(r"(\w)-[ \t]*\n[ \t]*(\w)")                         # custo-\nmers -> customers
_SPACES = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """One paragraph of PDF text, ready to quote: ligatures and typographic quotes as plain
    characters, invisible characters and note markers gone, line-end hyphenation joined,
    whitespace collapsed. Dashes and ellipses stay as printed."""
    t = (text or "").translate(INVISIBLE).translate(_TABLE)
    t = _SUPER_MARK.sub("", t)
    t = _DIGIT_MARK.sub("", t)
    t = _LINE_HYPHEN.sub(r"\1\2", t)
    return _SPACES.sub(" ", t).strip()


def continues(prev: str, nxt: str) -> bool:
    """True when `nxt` is the rest of `prev` split by a page break: `prev` ends mid-sentence and
    either `nxt` starts in lower case or `prev` is a real sentence fragment (8+ words), since body
    paragraphs in a book end with punctuation and "...the next / Bill Gates" is one sentence."""
    prev, nxt = prev.rstrip(), nxt.lstrip()
    if not prev or not nxt or prev[-1] in '.!?:;"\')]':
        return False
    return nxt[0].islower() or (prev.endswith("-") and nxt[0].isalpha()) or len(prev.split()) >= 8


def join(prev: str, nxt: str) -> str:
    prev, nxt = prev.rstrip(), nxt.lstrip()
    if prev.endswith("-") and nxt[:1].islower():
        return prev[:-1] + nxt                            # a word split across the page break
    return f"{prev} {nxt}"


GARBLE_LETTERS = set("rnhiflm")     # ~30 % of English letters; a font whose text map is broken drops them


def garbled_line(line: str) -> bool | None:
    """True when a line of 20+ letters has almost none of r, n, h, i, f, l, m: text from a font
    without a working Unicode map ("e egeeed y oog te gt"), which can't be quoted or verified.
    None for lines too short to judge."""
    letters = [c for c in line.lower() if c.isalpha()]
    if len(letters) < 20:
        return None
    return sum(c in GARBLE_LETTERS for c in letters) / len(letters) < 0.10


SITE_SUFFIX = re.compile(r"\s*[-|\u2013\u2014]\s*[\w.-]+\.(com|org|net|io|ru|to)\s*$", re.IGNORECASE)


def clean_meta(value: str | None) -> str | None:
    """A metadata field without a download site's suffix ("Title - example.com")."""
    return SITE_SUFFIX.sub("", value).strip() if value else value


JUNK_TITLE = re.compile(r"(^untitled|microsoft word|\.(docx?|pdf|epub|indd|qxd)\b|^document\d*$|^\W*$)", re.IGNORECASE)


def junk_title(title: str | None) -> bool:
    """A PDF title field that is a file name or an authoring tool, not the book's title."""
    return not title or bool(JUNK_TITLE.search(title.strip()))
