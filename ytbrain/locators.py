"""Locators (CONTEXT.md, ADR-0013): one integer position per text unit, and what it means.

  time       milliseconds into a Talk      label 12:40   link ...watch?v=<id>&t=760s
  paragraph  paragraph number in an Article  label ¶12     link <page>#:~:text=<the quote's first words>

The text-fragment link (https://wicg.github.io/scroll-to-text-fragment/) scrolls to and
highlights the quoted words in current Chrome, Edge, Safari and Firefox; elsewhere it opens
the page at the top.
"""
from __future__ import annotations

import re
from urllib.parse import quote

FRAGMENT_WORDS = 8                   # enough to be unique on a page, short enough to match exactly


def kind(record: dict | None) -> str:
    return (record or {}).get("locator") or "time"


def label(pos: int | None, locator: str = "time") -> str:
    if pos is None or (isinstance(pos, int) and pos < 0):
        return "--:--" if locator == "time" else "¶–"
    if locator == "paragraph":
        return f"¶{int(pos)}"
    s = int(pos) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def text_fragment(quote_text: str | None) -> str:
    """`#:~:text=` for the first words of a quote (its first piece, if it has "...")."""
    first = re.split(r"\s*(?:\.\.\.|…)\s*", (quote_text or "").strip())[0]
    words = first.split()[:FRAGMENT_WORDS]
    if len(words) < 3:
        return ""
    frag = quote(" ".join(words).strip(" \"'“”‘’()[]"), safe="").replace("-", "%2D")
    return f"#:~:text={frag}"


def deep_link(url: str | None, doc_id: str, pos: int | None, locator: str = "time",
              quote_text: str | None = None) -> str:
    """The link a Citation uses: to the second of a Talk, or to the quoted words of an Article."""
    if locator == "paragraph":
        base = (url or "").split("#", 1)[0]
        return base + text_fragment(quote_text)
    base = url or f"https://www.youtube.com/watch?v={doc_id}"
    return f"{base}{'&' if '?' in base else '?'}t={max(0, int(pos or 0)) // 1000}s"
