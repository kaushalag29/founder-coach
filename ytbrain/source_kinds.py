"""Source kinds (CONTEXT.md: Source, Document, Locator, Moment): everything that differs between a
Talk, an Article and a Book Chapter, in one class each, behind one interface.

The rest of the pipeline asks the registry (`for_doc`, `for_moment`, `by_name`, `by_locator`) and
never branches on the kind itself, so adding a source type (a podcast feed, a newsletter) is one
new `SourceKind` subclass here plus its adapter (ADR-0013). Ranking never looks at the kind: every
source competes on relevance alone (ADR-0014, amendment 2026-09-30).

A kind owns:
  - its Locator: what a unit's integer position means, how a Citation labels it and links to it;
  - its Moments (docs/eval-spec.md §1): the window an eval label points at, its id, its text,
    and the fields the released eval files carry for it.
Privacy is NOT a property of a kind: it comes from the Source's configuration (`visibility`).
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from urllib.parse import quote

from .config import ARTICLE_MOMENT_PARAS, ARTICLE_MOMENT_STEP, MOMENT_S, MOMENT_STEP_S

FRAGMENT_WORDS = 8                   # enough to be unique on a page, short enough to match exactly


def text_fragment(quote_text: str | None) -> str:
    """`#:~:text=` for the first words of a quote (its first piece, if it has "...")."""
    first = re.split(r"\s*(?:\.\.\.|…)\s*", (quote_text or "").strip())[0]
    words = first.split()[:FRAGMENT_WORDS]
    if len(words) < 3:
        return ""
    frag = quote(" ".join(words).strip(" \"'“”‘’()[]"), safe="").replace("-", "%2D")
    return f"#:~:text={frag}"


class SourceKind(ABC):
    """One type of Source. Subclasses are stateless singletons registered in KINDS."""

    name: str            # the record's `source_kind`
    locator: str         # the record's `locator`
    moment_tag: str      # the letter before a Moment's start in its id ("" for none)
    empty_label: str     # the label of a missing position
    review_hint: str     # how a reviewer checks a record against the source (`ytbrain sample`)
    link_name: str       # what the Document's link opens
    source_phrase: str   # what the eval question generator is told the knowledge comes from
    eval_split: str      # the Tuning split of questions written from this kind's public Sources
    eval_prefix: str     # that split's question-id prefix

    # -- which Documents and Moments are this kind's ------------------------------------------
    @abstractmethod
    def owns_doc(self, doc_id: str) -> bool:
        """Whether a Document id is of this kind (ids are all an eval label carries)."""

    def owns_moment(self, mid: str) -> bool:
        tail = mid.rsplit("_", 1)[-1]
        return tail[:1] == self.moment_tag if self.moment_tag else tail[:1].isdigit()

    # -- Locator -------------------------------------------------------------------------------
    @abstractmethod
    def label(self, pos: int, page_labels: dict | None = None) -> str:
        """How a Citation names a (valid) position."""

    @abstractmethod
    def deep_link(self, url: str | None, doc_id: str, pos: int | None, quote_text: str | None) -> str:
        """The link a Citation uses."""

    # -- Moments -------------------------------------------------------------------------------
    @abstractmethod
    def moment_start(self, pos: int) -> int:
        """The start of the Moment a position falls in (in the kind's own unit)."""

    def moment_id(self, doc_id: str, start: int) -> str:
        return f"{doc_id}_{self.moment_tag}{int(start):05d}"

    @abstractmethod
    def moment_url(self, doc_id: str, start: int, doc_url: str | None) -> str:
        """A link to the Moment (the caller knows a Document's URL)."""

    @abstractmethod
    def moment_text(self, utterances: list[dict], start: int) -> str:
        """The text of one Moment from the Document's units."""

    @abstractmethod
    def span(self, doc_id: str, start: int) -> dict:
        """Where a Moment is, as the released moments.jsonl states it."""

    def corpus_fields(self, doc_id: str, start: int) -> dict:
        """Where a Moment is, as the released corpus.jsonl states it."""
        return self.span(doc_id, start)


class Talk(SourceKind):
    name, locator, moment_tag, empty_label = "talk", "time", "", "--:--"
    review_hint, link_name = "open the video at the cited timestamps", "video"
    source_phrase = "startup talk"
    eval_split, eval_prefix = "dev", "dev"

    def owns_doc(self, doc_id: str) -> bool:
        return True                                   # the fallback: YouTube ids have no marker

    def label(self, pos, page_labels=None):
        h, rem = divmod(int(pos) // 1000, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    def deep_link(self, url, doc_id, pos, quote_text):
        base = url or f"https://www.youtube.com/watch?v={doc_id}"
        return f"{base}{'&' if '?' in base else '?'}t={max(0, int(pos or 0)) // 1000}s"

    def moment_start(self, pos):
        return (int(pos) // 1000 // MOMENT_STEP_S) * MOMENT_STEP_S

    def moment_url(self, doc_id, start, doc_url):
        return f"https://www.youtube.com/watch?v={doc_id}&t={start}s"

    def moment_text(self, utterances, start):
        lo, hi = start * 1000, (start + MOMENT_S) * 1000
        return " ".join(u.get("text", "") for u in utterances
                        if int(u.get("start_ms", 0)) < hi and int(u.get("end_ms", u.get("start_ms", 0))) > lo
                        ).strip()

    def span(self, doc_id, start):
        return {"youtube_id": doc_id, "start_ms": start * 1000, "end_ms": (start + MOMENT_S) * 1000}

    def corpus_fields(self, doc_id, start):
        return {"youtube_id": doc_id, "start_s": start, "end_s": start + MOMENT_S}


class Article(SourceKind):
    name, locator, moment_tag, empty_label = "article", "paragraph", "p", "¶–"
    review_hint, link_name = "open the page and find the cited paragraphs", "page"
    source_phrase = "startup essay or article"
    eval_split, eval_prefix = "dev-articles", "deva"

    def owns_doc(self, doc_id: str) -> bool:
        return str(doc_id).startswith("w-")           # website Documents (ytbrain.web.urls.doc_id)

    def label(self, pos, page_labels=None):
        return f"¶{int(pos)}"

    def deep_link(self, url, doc_id, pos, quote_text):
        return (url or "").split("#", 1)[0] + text_fragment(quote_text)

    def moment_start(self, pos):
        n = max(1, int(pos))
        return (n - 1) // ARTICLE_MOMENT_STEP * ARTICLE_MOMENT_STEP + 1

    def moment_url(self, doc_id, start, doc_url):
        return doc_url or ""

    def moment_text(self, utterances, start):
        lo, hi = start, start + ARTICLE_MOMENT_PARAS
        return " ".join(u.get("text", "") for u in utterances if lo <= int(u.get("start_ms", 0)) < hi).strip()

    def span(self, doc_id, start):
        return {"doc_id": doc_id, "start_paragraph": start, "end_paragraph": start + ARTICLE_MOMENT_PARAS}


class BookChapter(SourceKind):
    name, locator, moment_tag, empty_label = "chapter", "page", "b", "p. –"
    review_hint, link_name = "open the book at the cited pages", "book"
    source_phrase = "chapter of a business book"
    eval_split, eval_prefix = "dev-chapters", "devc"

    def owns_doc(self, doc_id: str) -> bool:
        doc_id = str(doc_id)                          # `<book id>__<chapter>`, never a YouTube id's 11 chars
        return "__" in doc_id and len(doc_id) != 11 and not ARTICLE.owns_doc(doc_id)

    @staticmethod
    def page(pos: int) -> int:
        return int(pos) // 1000

    def label(self, pos, page_labels=None):
        printed = (page_labels or {}).get(str(self.page(pos)))
        return f"p. {printed}" if printed else f"PDF p. {self.page(pos)}"

    def deep_link(self, url, doc_id, pos, quote_text):
        return (url or "").split("#", 1)[0] + (f"#page={self.page(pos)}" if url and pos and pos > 0 else "")

    def moment_start(self, pos):
        return self.page(pos)

    def moment_url(self, doc_id, start, doc_url):
        return f"{doc_url}#page={start}" if doc_url else ""

    def moment_text(self, utterances, start):
        return " ".join(u.get("text", "") for u in utterances
                        if int(u.get("start_ms", 0)) // 1000 == start).strip()

    def span(self, doc_id, start):
        return {"doc_id": doc_id, "page": start}


TALK, ARTICLE, BOOK_CHAPTER = Talk(), Article(), BookChapter()
KINDS: tuple[SourceKind, ...] = (ARTICLE, BOOK_CHAPTER, TALK)     # the fallback (Talk) last
_BY_NAME = {k.name: k for k in KINDS}
_BY_LOCATOR = {k.locator: k for k in KINDS}


def by_name(name: str | None) -> SourceKind:
    """The kind of a record's `source_kind` (Talk when unset: records before ADR-0013)."""
    return _BY_NAME.get(name or TALK.name, TALK)


def by_locator(locator: str | None) -> SourceKind:
    return _BY_LOCATOR.get(locator or TALK.locator, TALK)


def of_record(record: dict | None) -> SourceKind:
    """A record's kind, from its `locator` (the field every record since ADR-0013 carries)."""
    return by_locator((record or {}).get("locator"))


def for_doc(doc_id: str) -> SourceKind:
    return next(k for k in KINDS if k.owns_doc(doc_id))


def for_moment(mid: str) -> SourceKind:
    return next((k for k in KINDS if k.owns_moment(mid)), TALK)


def names() -> list[str]:
    return [k.name for k in (TALK, ARTICLE, BOOK_CHAPTER)]
