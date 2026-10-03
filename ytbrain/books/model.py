"""The shapes a Book passes through, independent of the PDF library that produced them."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

PARSED_FORMAT = 3            # bump when ParsedBook's JSON changes; a cached parse of another format is redone


@dataclass(frozen=True)
class OutlineEntry:
    """One PDF bookmark: its title, the 1-based PDF page it points to and its depth (0 = top)."""
    title: str
    page: int
    level: int = 0


@dataclass(frozen=True)
class Paragraph:
    """One block of body text in reading order. `kind` is text, heading or list; `level` is a
    heading's depth (1 = top) when the parser knows it."""
    text: str
    page: int
    kind: str = "text"
    level: int = 0


@dataclass
class ParsedBook:
    """Everything later Steps need from one PDF, text only: body paragraphs with their page, the
    outline, printed page labels and the file's own metadata. Tables, figures, captions, running
    headers and footers are counted in `dropped`, never kept."""
    path: str
    sha256: str
    pages: int
    paragraphs: list[Paragraph]
    outline: list[OutlineEntry] = field(default_factory=list)
    page_labels: dict[int, str] = field(default_factory=dict)     # PDF page -> printed label ("47", "xii")
    meta: dict = field(default_factory=dict)                      # the PDF's own title/author (given, may be junk)
    contents_pages: list[int] = field(default_factory=list)       # pages the parser saw as a contents list
    contents: list[str] = field(default_factory=list)             # the printed contents' entries, in order (probe)
    contents_page: int = 0                                        # where that list starts
    dropped: dict[str, int] = field(default_factory=dict)
    repaired: dict = field(default_factory=dict)                  # damaged ligatures put back (books/ligatures.py)
    parser: str = ""
    seconds: float = 0.0

    def label(self, page: int) -> str:
        """The page as the reader's copy prints it, else the PDF page number."""
        return self.page_labels.get(page) or str(page)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["page_labels"] = {str(k): v for k, v in self.page_labels.items()}
        d["format"] = PARSED_FORMAT
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ParsedBook:
        if d.get("format") != PARSED_FORMAT:
            raise ValueError(f"cached parse has format {d.get('format')}, expected {PARSED_FORMAT}")
        d = {k: v for k, v in d.items() if k != "format"}
        d["paragraphs"] = [Paragraph(**p) for p in d.get("paragraphs", [])]
        d["outline"] = [OutlineEntry(**o) for o in d.get("outline", [])]
        d["page_labels"] = {int(k): v for k, v in (d.get("page_labels") or {}).items()}
        return cls(**d)


@dataclass(frozen=True)
class Chapter:
    """One Chapter of a Book: the paragraphs `first`..`last` (inclusive indexes into
    ParsedBook.paragraphs), with the pages they span."""
    ordinal: int
    title: str
    first: int
    last: int
    start_page: int
    end_page: int


@dataclass
class ChapterPlan:
    """How a Book splits into Chapters: which method found them, what was left out, and anything
    the maintainer should look at before paying for extraction."""
    method: str                                         # manual | outline | headings | windows
    chapters: list[Chapter]
    skipped: list[str] = field(default_factory=list)    # front/back matter titles left out
    warnings: list[str] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)      # "outline: 2 candidates (need 3)", ...
