"""A Book's given metadata, never from the LLM (docs/books-plan.md "Metadata"). First match wins:

  1. sources.yaml       what the maintainer wrote for this file
  2. copyright page     ISBN (the ebook one when several) and year, read with patterns
  3. PDF metadata       title (subtitle after the first colon), authors ("Last, First" and "A & B")
  4. Open Library       by ISBN, only when asked (`lookup: openlibrary`): fills what is still empty

Every field records where it came from; `problems()` names what is missing or suspect, so the
report asks for a sources.yaml line instead of guessing."""
from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .normalize import clean_meta, clean_text, junk_title

FIELDS = ("title", "subtitle", "authors", "year", "isbn", "publisher", "url")
LOOKUP_FIELDS = ("title", "subtitle", "authors", "year", "publisher")    # what Open Library may fill
EBOOK = re.compile(r"e-?book|epub|e-?isbn|digital|kindle|electronic", re.IGNORECASE)
_ISBN = re.compile(r"(e?-?ISBN(?:-1[03])?)\s*[:#]?\s*((?:97[89][-‐\s]?)?(?:\d[-‐\s]?){9}[\dX])", re.IGNORECASE)
_YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b")
_COPYRIGHT = re.compile(r"©|\(c\)|^copyright\s+\d{4}|copyright\s+©", re.IGNORECASE)


def isbn13(raw: str) -> str | None:
    """A valid ISBN-13 from an ISBN-10 or -13 with any separators, else None."""
    d = re.sub(r"[^0-9X]", "", raw.upper())
    if len(d) == 10 and d[:9].isdigit():
        if sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d)) % 11:
            return None
        d = "978" + d[:9]
        return d + str((10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d)) % 10) % 10)
    if len(d) == 13 and d.isdigit() and sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d)) % 10 == 0:
        return d
    return None


def copyright_page(pages: dict[int, list[str]]) -> tuple[int, list[str]]:
    """(page, lines) of the copyright page: the first page, front to back, that prints an ISBN."""
    for page in sorted(pages):
        if any("isbn" in ln.lower() for ln in pages[page]):
            return page, pages[page]
    return 0, []


def pick_isbn(lines: list[str]) -> str | None:
    """The edition this file is: an ISBN labelled ebook/epub/eISBN, else the first valid one."""
    found: list[tuple[bool, str]] = []
    # a label belongs to its own entry: entries are split by "|" or ";" and by the next ISBN
    for seg in (s for ln in lines for s in re.split(r"[|;]", ln)):
        matches = list(_ISBN.finditer(seg))
        for k, mt in enumerate(matches):
            start = matches[k - 1].end() if k else 0
            end = matches[k + 1].start() if k + 1 < len(matches) else len(seg)
            context = seg[max(start, mt.start() - 30):min(end, mt.end() + 25)]
            number = isbn13(mt.group(2))
            if number and number not in (n for _, n in found):
                found.append((bool(EBOOK.search(context)), number))
    return next((n for ebook, n in found if ebook), found[0][1] if found else None)


def copyright_year(lines: list[str]) -> int | None:
    """The year of the book's own copyright line ("Copyright © 2014 by X", "© X 2014"), on the
    copyright page only: permissions for quoted songs elsewhere carry their own years."""
    for ln in lines:
        if _COPYRIGHT.search(ln.strip()):
            years = _YEAR.findall(ln)
            if years:
                return int(years[0])
    return None


_ORG = re.compile(r"\b(inc|llc|ltd|corp|corporation|company|co|press|publishing|group|gmbh|foundation)\b\.?", re.IGNORECASE)
_NAMES = re.compile(r"(?:\u00a9|\(c\)|copyright)\s*(?:\u00a9\s*)?(?:\d{4}\s*)?(?:by\s+)?(.+?)(?:\s+\d{4})?[.;]?$", re.IGNORECASE)


def copyright_names(lines: list[str]) -> list[str]:
    """People named on the book's own copyright line ("© 2014 by Ben Horowitz", "© Peter Thiel 2014").
    Organisations (Netflix, Inc.) are left out. Used only to warn about a missing author."""
    for ln in lines:
        if not _COPYRIGHT.search(ln.strip()) or not _YEAR.search(ln):
            continue
        m = _NAMES.search(ln.strip())
        if not m:
            return []
        names = re.split(r"\.\s|\s+all rights", m.group(1), maxsplit=1, flags=re.IGNORECASE)[0]   # the sentence ends there
        who = re.split(r"\s+with\s+|,\s*(?:and\s+)?|\s+and\s+", names)
        return [w.strip(" .") for w in who
                if len(w.split()) >= 2 and not _ORG.search(w) and not _YEAR.search(w) and w[:1].isupper()]
    return []


def split_title(raw: str | None) -> tuple[str | None, str | None]:
    """(title, subtitle): a download site's suffix stripped, the subtitle after the first colon."""
    t = clean_text(clean_meta(raw) or "")
    if not t or junk_title(t):
        return None, None
    title, _, sub = t.partition(":")
    return title.strip() or None, sub.strip() or None


def split_authors(raw: str | list | None) -> list[str]:
    """"Horowitz, Ben" -> [Ben Horowitz]; "A & B & C", "A; B", "A, B and C" -> three names."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [clean_text(a) for a in raw if a and clean_text(a)]
    t = clean_text(raw)
    if re.search(r"\s&\s|;", t):
        parts = re.split(r"\s*(?:&|;)\s*", t)
    elif t.count(",") == 1 and " and " not in t and all(len(p.split()) <= 2 for p in t.split(",")):
        last, first = (p.strip() for p in t.split(","))
        parts = [f"{first} {last}"]
    else:
        parts = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", t)
    return [p.strip() for p in parts if p.strip()]


def speaker(authors: list[str]) -> str | None:
    """How a Citation names the authors: "A", "A and B", "A, B and C"."""
    if not authors:
        return None
    return authors[0] if len(authors) == 1 else f"{', '.join(authors[:-1])} and {authors[-1]}"


def slug(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit].strip("-") or "untitled"


@dataclass
class BookMeta:
    title: str | None = None
    subtitle: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    isbn: str | None = None
    publisher: str | None = None
    url: str | None = None
    origin: dict[str, str] = field(default_factory=dict)       # field -> sources.yaml | copyright page | pdf metadata | open library
    copyright_page: int = 0
    copyright_names: list[str] = field(default_factory=list)

    @property
    def book_id(self) -> str:
        """Stable across re-splits and file renames: the ISBN-13, else a slug of the title."""
        return self.isbn or slug(self.title or "untitled-book")

    @property
    def speaker(self) -> str | None:
        return speaker(self.authors)

    def problems(self) -> list[str]:
        out = []
        if not self.title:
            out.append("no usable title")
        if not self.authors:
            out.append("no author")
        if not self.year:
            out.append("no year")
        if not self.isbn:
            out.append("no ISBN")
        surnames = {a.split()[-1].lower() for a in self.authors}
        missing = [n for n in self.copyright_names if n.split()[-1].lower() not in surnames]
        if missing and self.origin.get("authors") != "sources.yaml":
            out.append(f"the copyright page names {', '.join(missing)}, not among the authors")
        return out

    def as_dict(self) -> dict:
        return {**asdict(self), "book_id": self.book_id, "speaker": self.speaker}


Lookup = Callable[[str], dict]           # isbn -> {title, subtitle, authors, year, publisher}


def _set(meta: BookMeta, name: str, value, origin: str) -> None:
    if value in (None, "", []) or getattr(meta, name) not in (None, "", []):
        return
    setattr(meta, name, value)
    meta.origin[name] = origin


def resolve(pdf_meta: dict, pages: dict[int, list[str]], overrides: dict | None = None,
            lookup: Lookup | None = None) -> BookMeta:
    """The Book's metadata, first match wins (module docstring)."""
    o = overrides or {}
    meta = BookMeta()
    for name in FIELDS:
        value = o.get("author") if name == "authors" and "authors" not in o else o.get(name)
        if name == "authors":
            value = split_authors(value)
        elif name == "year" and value is not None:
            value = int(value)
        elif name == "isbn" and value is not None:
            value = isbn13(str(value)) or str(value)
        _set(meta, name, value, "sources.yaml")
    meta.copyright_page, lines = copyright_page(pages)
    _set(meta, "isbn", pick_isbn(lines), "copyright page")
    _set(meta, "year", copyright_year(lines), "copyright page")
    meta.copyright_names = copyright_names(lines)
    title, subtitle = split_title(pdf_meta.get("title"))
    if not o.get("title"):                        # a title you set is the whole title: no subtitle from the PDF
        _set(meta, "title", title, "pdf metadata")
        _set(meta, "subtitle", subtitle, "pdf metadata")
    _set(meta, "authors", split_authors(clean_meta(pdf_meta.get("author"))), "pdf metadata")
    if lookup and meta.isbn and any(getattr(meta, f) in (None, "", []) for f in LOOKUP_FIELDS):
        found = lookup(meta.isbn) or {}
        for name in LOOKUP_FIELDS:
            _set(meta, name, found.get(name), "open library")
    return meta


class OpenLibrary:
    """ISBN -> metadata from openlibrary.org (free, no key), one cached call per ISBN. Only used
    when a Source says `lookup: openlibrary`; failures just leave fields empty."""
    URL = "https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"

    def __init__(self, cache_dir: Path, user_agent: str, timeout_s: float = 15.0):
        self.cache_dir, self.user_agent, self.timeout_s = Path(cache_dir), user_agent, timeout_s

    def __call__(self, isbn: str) -> dict:
        cache = self.cache_dir / f"{isbn}.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        try:
            req = urllib.request.Request(self.URL.format(isbn=isbn), headers={"User-Agent": self.user_agent})
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                data = json.loads(r.read().decode("utf-8")).get(f"ISBN:{isbn}") or {}
        except (OSError, ValueError):
            return {}
        years = _YEAR.findall(data.get("publish_date") or "")
        out = {"title": data.get("title"), "subtitle": data.get("subtitle"),
               "authors": [a.get("name") for a in data.get("authors") or [] if a.get("name")],
               "year": int(years[0]) if years else None,
               "publisher": ((data.get("publishers") or [{}])[0]).get("name")}
        from ..pages import atomic_write_text
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cache, json.dumps(out, ensure_ascii=False))
        return out
