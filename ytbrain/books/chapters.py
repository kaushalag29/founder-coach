"""Where each Chapter of a Book starts, as a chain of sources tried in order (ADR-0014):

  manual    the `chapters:` list in sources.yaml with `chapters_only: true` ({title, page}; page as
            printed or the PDF page). Without `chapters_only`, the list only corrects the winning
            source's split: an entry on a page that starts a chapter renames it (`skip: true` drops
            it), an entry on another page adds a chapter there
  outline   the PDF's bookmarks, at the shallowest level with enough chapters; a Part ("Part II",
            "Section One") with chapters under it is replaced by those chapters
  contents  the printed contents page: each entry found where a page's first paragraph starts
            with it ("1. Start" matches a page opening "1 START"), in order, after that page
  headings  top-level headings from the parser (bookmarks, numbering, font style)
  windows   fixed page windows: the last resort, always flagged

The first source whose result is plausible wins; the plan records what was tried and why the
others were passed over. Front and back matter (contents, acknowledgements, notes, index...) are
left out; the preface, introduction and conclusion are kept. `include`/`exclude` (title words)
override that per Book."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

from .model import Chapter, ChapterPlan, ParsedBook

MIN_CHAPTERS = 3
HEAD_SLACK = 12              # a title may sit this many letters into a page's first paragraph (drop cap, number)
MAX_SHARE = 0.6              # one "chapter" holding more than this share of the words means the split failed
MIN_WORDS = 60               # a part-title page or a blank divider, not a Chapter
LONG_WORDS = 30_000          # a Chapter this long is extracted in windows; worth a look
WINDOW_PAGES = 15

FRONT = re.compile(r"^(cover|title( page)?|half[- ]title|copyright|dedication|epigraph|(table of )?contents"
                   r"|also by|other books by|praise( for)?|foreword|list of (figures|tables|illustrations)"
                   r"|frontispiece|about the (book|ebook))\b", re.IGNORECASE)
_QUALIFIER = r"((selected|further|recommended|suggested|additional|general|subject|name)\s+)?"
BACK = re.compile(r"^" + _QUALIFIER + r"(acknowledge?ments?|notes|endnotes|bibliography|references|sources"
                  r"|reading|index|about the (authors?|publisher)|appendix|glossary|permissions|credits"
                  r"|(photo|illustration|image|art) credits|reading group guide|newsletters?|copyright"
                  r"|resources|photo(graph)?s?( insert| section)?|insert|illustrations|plates)\b", re.IGNORECASE)
_ORDINAL = r"([ivxlc]+|\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
PART = re.compile(r"^(part|section|book)\s*" + _ORDINAL + r"\b", re.IGNORECASE)
# "Chapter 4", "4.", "4 ", "IV." (a bare roman numeral needs its dot: "Civil War" is not numbered)
NUMBERING = re.compile(r"^(?:(?i:chapter)\s+(?:[IVXLCivxlc]+|\d+)|\d+[a-z]?|[IVXLC]+(?=[.:)]))[.:)]?\s+")


@dataclass(frozen=True)
class Candidate:
    title: str
    page: int


def plain(title: str) -> str:
    """A chapter title without its numbering, for matching and matter checks."""
    t = " ".join((title or "").split())
    t = NUMBERING.sub("", t) if not NUMBERING.fullmatch(t + " ") else t
    return t.strip(" .:-–—")


def is_matter(title: str, include=(), exclude=()) -> bool:
    t = plain(title).lower()
    if any(w.lower() in t for w in include):
        return False
    if any(w.lower() in t for w in exclude):
        return True
    return bool(FRONT.match(t) or BACK.match(t))


def _opens_with(text: str, title: str) -> bool:
    """True when `text` (a paragraph) opens with `title`: its words within the first few characters
    (after a drop cap or a number), or the whole short paragraph is nearly the title."""
    t = compact(plain(title))
    if len(t) < 3:
        return False
    head = compact(text)
    return 0 <= head.find(t) <= HEAD_SLACK or SequenceMatcher(None, compact(plain(text)), t).ratio() >= 0.85


class ChapterSource(Protocol):
    name: str

    def candidates(self, book: ParsedBook, config: dict) -> tuple[list[Candidate], str]: ...


def pdf_page(book: ParsedBook, page) -> int | None:
    """A page as written in sources.yaml (a printed label or a PDF page number) -> the PDF page."""
    by_label = {v: k for k, v in book.page_labels.items()}
    return by_label.get(str(page)) or (int(page) if str(page).isdigit() else None)


class ManualChapters:
    name = "manual"

    def candidates(self, book, config):
        rows = config.get("chapters") or []
        if not rows or not config.get("chapters_only"):
            return [], "no chapters_only list in sources.yaml"
        out = []
        for r in rows:
            page = pdf_page(book, r.get("page"))
            if page is None:
                return [], f"page {r.get('page')!r} of {r.get('title')!r} is neither a printed label nor a number"
            out.append(Candidate(str(r.get("title") or f"Chapter {len(out) + 1}"), page))
        return out, f"{len(out)} listed"


def apply_overrides(book: ParsedBook, cands: list[Candidate], rows: list[dict]) -> tuple[list[Candidate], str]:
    """Corrections from sources.yaml on top of a detected split (see the module docstring)."""
    out, applied = list(cands), 0
    for r in rows or []:
        page = pdf_page(book, r.get("page"))
        if page is None:
            continue
        at = next((i for i, c in enumerate(out) if c.page == page and not is_matter(c.title)), None)
        if r.get("skip"):
            if at is not None:
                out.pop(at)
                applied += 1
        elif at is not None:
            out[at] = Candidate(str(r.get("title") or out[at].title), page)
            applied += 1
        elif r.get("title"):
            out.append(Candidate(str(r["title"]), page))
            applied += 1
    return sorted(out, key=lambda c: c.page), (f", {applied} corrections from sources.yaml" if applied else "")


class OutlineChapters:
    name = "outline"

    @staticmethod
    def _children(outline, i: int) -> list:
        """The entries one level below outline[i], up to its next sibling."""
        lvl, kids = outline[i].level, []
        for o in outline[i + 1:]:
            if o.level <= lvl:
                break
            if o.level == lvl + 1:
                kids.append(o)
        return kids

    @classmethod
    def _groups(cls, outline, body) -> bool:
        """Whether a level only groups the real chapters ("THE FORCE" > "PART 1" > "1. Protection
        from Above"): every entry has children, and most of them are Parts or numbered chapters.
        Sections inside chapters are neither, so a chapter level is never skipped."""
        index = {id(o): i for i, o in enumerate(outline)}
        kids = [cls._children(outline, index[id(o)]) for o in body]
        if not all(kids):
            return False
        flat = [c.title.strip() for k in kids for c in k]
        return sum(bool(PART.match(t) or NUMBERING.match(t)) for t in flat) * 2 >= len(flat)

    def candidates(self, book, config):
        outline = book.outline
        if not outline:
            return [], "the PDF has no bookmarks"
        notes = []
        for level in sorted({o.level for o in outline}):
            chosen, parts = [], 0
            for i, o in enumerate(outline):
                if o.level != level:
                    continue
                kids = self._children(outline, i)
                if PART.match(o.title.strip()) and kids:
                    chosen += [o] + kids           # a Part with chapters under it: its chapters, and the Part as a bound
                    parts += 1
                else:
                    chosen.append(o)               # a Part divider without children is dropped later as too short
            # matter at shallower levels bounds the chapters around it (e.g. a top-level Index)
            chosen += [o for o in outline if o.level < level and is_matter(o.title)]
            body = [o for o in chosen if not is_matter(o.title) and not PART.match(o.title.strip())]
            if len(body) >= MIN_CHAPTERS and self._groups(outline, body):
                notes.append(f"level {level}: {len(body)} groups of Parts or numbered chapters, opened")
                continue
            if len(body) >= MIN_CHAPTERS:
                via = f", {parts} Parts opened" if parts else ""
                return ([Candidate(o.title, o.page) for o in sorted(chosen, key=lambda o: o.page)],
                        f"level {level}: {len(body)} chapters{via}")
            notes.append(f"level {level}: {len(body)} chapters")
        return [], "; ".join(notes) or "no usable level"


def compact(text: str) -> str:
    """Letters and digits only, lower case: "1. Start" and "1 START" (or "Jointhe") compare equal."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


class ContentsChapters:
    name = "contents"

    def candidates(self, book, config):
        entries = list(book.contents)                      # Parts stay: their divider pages bound the chapters
        body = [e for e in entries if not is_matter(e) and not PART.match(e)]
        if len(body) < MIN_CHAPTERS:
            return [], "no printed contents page" if not book.contents else f"{len(body)} contents entries"
        openings: list[tuple[int, str]] = []                     # (page, compact start of its first paragraph)
        seen = set()
        for p in book.paragraphs:
            if p.page > book.contents_page and p.page not in seen:
                seen.add(p.page)
                openings.append((p.page, compact(p.text)[:120]))
        found, k = [], 0
        for e in entries:
            key, alt = compact(e), compact(plain(e))
            if len(alt) < 3:
                continue
            for j in range(k, len(openings)):
                page, head = openings[j]
                # the number may be missing from the page (a parser can't tell "1" from a page number),
                # so a numbered entry also matches on its title alone; order keeps that safe
                numbered = key != alt
                if (0 <= head.find(key) <= HEAD_SLACK
                        or ((numbered or len(alt) >= 8) and 0 <= head.find(alt) <= HEAD_SLACK)):
                    found.append(Candidate(e, page))
                    k = j + 1
                    break
        hits = sum(not is_matter(c.title) and not PART.match(c.title) for c in found)
        if hits >= MIN_CHAPTERS and hits >= 0.6 * len(body):
            return found, f"{hits} of {len(body)} entries found in the text"
        return [], f"only {hits} of {len(body)} contents entries found in the text"


class HeadingChapters:
    name = "headings"

    def candidates(self, book, config):
        heads = [(i, p) for i, p in enumerate(book.paragraphs) if p.kind == "heading"]
        if not heads:
            return [], "the parser found no headings"
        for label, pick in (("numbered", [h for h in heads if NUMBERING.match(h[1].text)]),
                            ("level 1", [h for h in heads if h[1].level <= 1])):
            body = [h for h in pick if not is_matter(h[1].text)]
            if MIN_CHAPTERS <= len(body) <= 80:
                chosen = sorted(set(pick) | {h for h in heads if is_matter(h[1].text)})
                return [Candidate(p.text, p.page) for _, p in chosen], f"{label}: {len(body)} chapters"
        return [], f"{len(heads)} headings, none form 3-80 chapters"


class PageWindows:
    name = "windows"

    def candidates(self, book, config):
        if not book.paragraphs:
            return [], "no text"
        first, last = book.paragraphs[0].page, book.paragraphs[-1].page
        out = [Candidate(f"Pages {book.label(p)}-{book.label(min(p + WINDOW_PAGES - 1, last))}", p)
               for p in range(first, last + 1, WINDOW_PAGES)]
        return out, f"{len(out)} windows of {WINDOW_PAGES} pages"


CHAIN: tuple[ChapterSource, ...] = (ManualChapters(), OutlineChapters(), ContentsChapters(), HeadingChapters(),
                                    PageWindows())


def _start_index(book: ParsedBook, cand: Candidate, not_before: int) -> int | None:
    """The paragraph a candidate starts at: its heading on its page when found (so two chapters
    on one page split correctly), else the first paragraph on or after its page."""
    first_on_page = None
    for i in range(not_before, len(book.paragraphs)):
        p = book.paragraphs[i]
        if p.page < cand.page:
            continue
        if p.page > cand.page + 1:
            break
        if first_on_page is None:
            first_on_page = i
        if _opens_with(p.text, cand.title):
            return i
    if first_on_page is not None:
        return first_on_page
    return next((i for i in range(not_before, len(book.paragraphs)) if book.paragraphs[i].page >= cand.page), None)


def _words(book: ParsedBook, a: int, b: int) -> int:
    return sum(len(p.text.split()) for p in book.paragraphs[a:b + 1])


def chapter_words(book: ParsedBook, chapter: Chapter) -> int:
    return _words(book, chapter.first, chapter.last)


def build(book: ParsedBook, cands: list[Candidate], method: str, config: dict) -> ChapterPlan:
    include, exclude = config.get("include") or (), config.get("exclude") or ()
    ordered = sorted(cands, key=lambda c: c.page)
    starts: list[tuple[int, Candidate]] = []
    empty: list[str] = []
    cursor = 0
    for k, c in enumerate(ordered):
        i = _start_index(book, c, cursor)
        nxt = ordered[k + 1].page if k + 1 < len(ordered) else None
        if i is None or (starts and i == starts[-1][0]) or (nxt is not None and book.paragraphs[i].page >= nxt > c.page):
            empty.append(plain(c.title) or c.title)      # no text of its own: its first paragraph belongs to the next entry
            continue
        starts.append((i, c))
        cursor = i + 1
    plan = ChapterPlan(method=method, chapters=[])
    plan.skipped += [f"{t} (no text of its own)" for t in empty]
    if starts and starts[0][0] > 0:
        plan.skipped.append(f"(before the first chapter: pages {book.label(book.paragraphs[0].page)}-"
                            f"{book.label(book.paragraphs[starts[0][0] - 1].page)})")
    for n, (i, c) in enumerate(starts):
        last = (starts[n + 1][0] - 1) if n + 1 < len(starts) else len(book.paragraphs) - 1
        title = plain(c.title) or c.title
        if method != "windows" and (is_matter(c.title, include, exclude) or PART.match(c.title.strip())):
            plan.skipped.append(title)
            continue
        words = _words(book, i, last)
        if words < MIN_WORDS:
            plan.skipped.append(f"{title} ({words} words)")
            continue
        plan.chapters.append(Chapter(len(plan.chapters) + 1, title, i, last,
                                     book.paragraphs[i].page, book.paragraphs[last].page))
    return plan


def plausible(book: ParsedBook, plan: ChapterPlan) -> str:
    """'' when the plan looks like a real split, else why not."""
    if len(plan.chapters) < MIN_CHAPTERS:
        return f"{len(plan.chapters)} chapters (need {MIN_CHAPTERS})"
    words = [_words(book, c.first, c.last) for c in plan.chapters]
    total = sum(words) or 1
    if max(words) / total > MAX_SHARE:
        return f"one chapter holds {max(words) / total:.0%} of the text"
    return ""


def detect(book: ParsedBook, config: dict | None = None) -> ChapterPlan:
    """The Book's Chapters from the first source in the chain that gives a plausible split."""
    config = config or {}
    tried: list[str] = []
    for source in CHAIN:
        cands, note = source.candidates(book, config)
        if not cands:
            tried.append(f"{source.name}: {note}")
            continue
        if source.name != "manual":
            cands, fixed = apply_overrides(book, cands, config.get("chapters") or [])
            note += fixed
        plan = build(book, cands, source.name, config)
        why = plausible(book, plan) if source.name != "windows" else ""
        if why and source.name != "manual":
            tried.append(f"{source.name}: {note}, rejected ({why})")
            continue
        plan.tried = tried + [f"{source.name}: {note}"]
        if why:
            plan.warnings.append(f"the manual chapter list looks wrong: {why}")
        if source.name == "windows":
            plan.warnings.append("no chapter structure found: split into page windows; add a `chapters:` list")
        for ch in plan.chapters:
            w = _words(book, ch.first, ch.last)
            if w > LONG_WORDS:
                plan.warnings.append(f"chapter {ch.ordinal} ({ch.title}) has {w} words: check the split")
        if not book.page_labels:
            plan.warnings.append("no printed page labels: Citations will use PDF page numbers")
        return plan
    return ChapterPlan(method="none", chapters=[], tried=tried, warnings=["no text to split"])
