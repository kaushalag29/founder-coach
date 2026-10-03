"""Locators (CONTEXT.md, ADR-0013): one integer position per text unit, and what it means.

  time       milliseconds into a Talk      label 12:40   link ...watch?v=<id>&t=760s
  paragraph  paragraph number in an Article  label ¶12     link <page>#:~:text=<the quote's first words>
  page       page * 1000 + paragraph-on-page in a Book Chapter
                                             label p. 47 (the printed page) or PDF p. 47 (no printed
                                             page labels)   link the Book's url, else none

The text-fragment link (https://wicg.github.io/scroll-to-text-fragment/) scrolls to and
highlights the quoted words in current Chrome, Edge, Safari and Firefox; elsewhere it opens
the page at the top.
"""
from __future__ import annotations

from . import source_kinds as K
from .source_kinds import FRAGMENT_WORDS, text_fragment  # noqa: F401  (re-exported)


def kind(record: dict | None) -> str:
    return (record or {}).get("locator") or "time"


def label_for(record: dict | None, pos: int | None) -> str:
    """`label` with the record's own Locator kind and printed page labels."""
    return label(pos, kind(record), (record or {}).get("page_labels"))


def page(pos: int) -> int:
    """The PDF page of a `page` Locator."""
    return K.BookChapter.page(pos)


def label(pos: int | None, locator: str = "time", page_labels: dict | None = None) -> str:
    """How a Citation names the position. `page_labels` ({pdf page: printed label}, from the
    record) turns PDF page 47 into the page number printed on it."""
    k = K.by_locator(locator)
    if pos is None or (isinstance(pos, int) and pos < 0):
        return k.empty_label
    return k.label(pos, page_labels)


def deep_link(url: str | None, doc_id: str, pos: int | None, locator: str = "time",
              quote_text: str | None = None) -> str:
    """The link a Citation uses: to the second of a Talk, to the quoted words of an Article, or
    to the Book's page when it has a url (a private PDF has none: its label is the Citation)."""
    return K.by_locator(locator).deep_link(url, doc_id, pos, quote_text)
