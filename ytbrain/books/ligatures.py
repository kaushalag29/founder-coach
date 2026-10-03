"""Ligatures lost by a PDF's fonts, repaired from the text alone.

Some PDFs (typically ebook conversions) embed fonts whose ligature glyphs (ff, fi, fl, ffi, ffl)
have no Unicode map. The text layer then holds whatever stand-in character the font used, and a
different one per embedded font: "di@erent", "e&orts", "signi%cant", "pro1t", "in3uenced",
"diKcult", "oDce". The words can't be quoted (verification never finds them) or searched.

A damaged word has one to two stray characters (a symbol, a digit or a capital letter inside a word).
Each is tried as every ligature; the word is repaired only when the result is a word this language
really has: a word of the book itself or of `ligature_words.txt` (English words with ligatures, built
from your transcripts by scripts/ligature_words.py). Anything else is left exactly as printed and
counted, so a brand ("iPhone", "eBay"), an ordinal ("1st"), "R&D" or a URL is never rewritten.
Pure functions; nothing here knows about any parser."""
from __future__ import annotations

import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

LIGATURES = ("ffi", "ffl", "ff", "fi", "fl")             # what a stand-in can be
_CORE = re.compile(r"[A-Za-z0-9@&%+=#<>^~|*$]+")          # a run that can hold a word and its stand-ins
_SYMBOLS = frozenset("@&%+=#<>^~|*$")
_OWN_WORD = re.compile(r"(?<![A-Za-z0-9@&%+=#<>^~|*$/_.-])[a-z]{3,24}(?![A-Za-z0-9@&%+=#<>^~|*$/_-])")
_HAS_LIGATURE = re.compile(r"f[filf]")
_URL_EDGE_BEFORE, _URL_EDGE_AFTER = "/:_.", "/_"           # a run inside a URL or file name is left alone
MAX_SUSPECTS = 2
MIN_LOWER_SYMBOL = 3                                       # "1st", "2nd", "4x", "3D" are never damaged words
EXAMPLES = 8


@lru_cache(maxsize=1)
def english() -> dict[str, int]:
    """{word: count} from the bundled list (ligature_words.txt)."""
    out: dict[str, int] = {}
    text = resources.files(__package__).joinpath("ligature_words.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line and not line.startswith("#"):
            word, _, n = line.partition(" ")
            out[word] = int(n or 1)
    return out


@dataclass
class Repairs:
    """What one Book's repair did: for the report and the warnings."""
    fixed: Counter = field(default_factory=Counter)        # "di@erent -> different": occurrences
    unresolved: Counter = field(default_factory=Counter)   # damaged-looking words no ligature explains
    hyphens: int = 0                                        # control characters that stood for a hyphen

    @property
    def words(self) -> int:
        return sum(self.fixed.values())

    def as_dict(self) -> dict:
        return {"words": self.words, "distinct": len(self.fixed), "hyphens": self.hyphens,
                "examples": [k for k, _ in self.fixed.most_common(EXAMPLES)],
                "unresolved": sum(self.unresolved.values()),
                "unresolved_examples": [k for k, _ in self.unresolved.most_common(EXAMPLES)]}


def _suspects(core: str) -> list[int]:
    """Positions of characters a ligature may have been replaced by: symbols, digits and capital
    letters inside a word (a capital at the start is just a capital)."""
    return [i for i, c in enumerate(core)
            if c in _SYMBOLS or c.isdigit() or (c.isupper() and i > 0)]


def _looks_damaged(core: str, suspects: list[int]) -> bool:
    lower = sum(c.islower() for c in core)
    if not suspects or len(suspects) > MAX_SUSPECTS or not lower:
        return False
    return lower >= MIN_LOWER_SYMBOL or all(core[i].isupper() for i in suspects)


def _candidates(core: str, suspects: list[int], vocab: dict[str, int], capital: bool):
    """(count, repaired word) for every way of putting a ligature at each suspect that spells a
    known word, best first."""
    found = []
    for combo in itertools.product(LIGATURES, repeat=len(suspects)):
        chars = list(core)
        for pos, lig in zip(reversed(suspects), reversed(combo)):
            chars[pos] = lig
        word = "".join(chars)
        n = vocab.get(word.lower(), 0)
        if n:
            found.append((n, word))
    found.sort(key=lambda t: -t[0])
    if capital and found:
        found = [(n, w[:1].upper() + w[1:]) for n, w in found]
    return found


_CONTROL_HYPHEN = re.compile(r"(?<=\w)[\x01-\x08\x0b\x0c\x0e-\x1f\x7f](?=\s*\w)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def repair_text(text: str, vocab: dict[str, int], repairs: Repairs | None = None,
                paragraph_start: bool = True) -> str:
    """One paragraph with its damaged words repaired (see the module docstring)."""
    repairs = repairs if repairs is not None else Repairs()
    out, last = [], 0
    for m in _CORE.finditer(text):
        core, start, end = m.group(), m.start(), m.end()
        if (start and text[start - 1] in _URL_EDGE_BEFORE) or (end < len(text) and text[end] in _URL_EDGE_AFTER):
            continue
        suspects = _suspects(core)
        if not _looks_damaged(core, suspects):
            continue
        before = text[:start].rstrip()
        sentence_start = (paragraph_start and not before) or before.endswith((".", "!", "?"))
        capital = 0 in suspects and sentence_start
        best = _candidates(core, suspects, vocab, capital)
        if not best:
            if any(core[i] != core[i].upper() or not core[i].isalpha() for i in suspects) and core[0].islower():
                repairs.unresolved[core] += 1
            continue
        fixed = best[0][1]
        repairs.fixed[f"{core} -> {fixed}"] += 1
        out += [text[last:start], fixed]
        last = end
    return "".join(out) + text[last:] if out else text


def control_hyphens(text: str, repairs: Repairs | None = None) -> str:
    """A control character between letters is how PDFium hands back some fonts' hyphen
    ("one\\x02time"); any other control character is a space."""
    text, n = _CONTROL_HYPHEN.subn("-", text)
    if repairs is not None:
        repairs.hyphens += n
    return _CONTROL.sub(" ", text)


def own_words(texts) -> dict[str, int]:
    """The ligature words a book spells correctly itself: the best evidence for its own vocabulary."""
    seen: Counter = Counter()
    for t in texts:
        seen.update(w for w in _OWN_WORD.findall(t) if _HAS_LIGATURE.search(w))
    return dict(seen)


def still_damaged(texts) -> Counter:
    """Words in already repaired text that still look damaged (what no ligature explained)."""
    left = Repairs()
    for t in texts:
        repair_text(t, {}, left)
    return left.unresolved


def repair_book(texts: list[str], extra: dict[str, int] | None = None) -> tuple[list[str], Repairs]:
    """Repair a book's paragraphs. The vocabulary is the bundled list plus the book's own words."""
    repairs = Repairs()
    vocab = {**english(), **(extra or {})}
    for w, n in own_words(texts).items():
        vocab[w] = vocab.get(w, 0) + n
    return [repair_text(control_hyphens(t, repairs), vocab, repairs) for t in texts], repairs
