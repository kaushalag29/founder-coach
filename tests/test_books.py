"""PDF Books (ADR-0014), offline: text clean-up, the probe on tiny fixture PDFs, block assembly,
the chapter-detection chain, and `ytbrain books inspect` with a fake parser (Docling is never
imported here). Fixtures: tests/fixtures/books/ (text written for these tests)."""
import contextlib
import io
import json
import os

os.environ["YTBRAIN_DOTENV"] = "0"          # hermetic: never read the developer's .env
os.environ.setdefault("YTBRAIN_SOURCES_FILE", os.path.join(__import__("tempfile").mkdtemp(prefix="ytbrain-nosources-"), "sources.yaml"))   # hermetic: never read your sources.yaml (the file does not exist)
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("YTBRAIN_ROOT", tempfile.mkdtemp(prefix="ytbrain-bookstest-"))
FIX = ROOT / "tests" / "fixtures" / "books"


def _skipped(why: str) -> None:
    """A test that can't run here says so; CI sets REQUIRE_ALL_TESTS=1 and fails instead."""
    if os.environ.get("REQUIRE_ALL_TESTS") == "1":
        raise AssertionError(f"skipped in CI: {why}")
    print(f"    (skipped: {why})")


try:
    import pypdfium2  # noqa: F401
    HAVE_PDFIUM = True
except ImportError:
    HAVE_PDFIUM = False

from ytbrain.books import chapters as C
from ytbrain.books.model import OutlineEntry, Paragraph, ParsedBook
from ytbrain.books.normalize import (
    clean_text,
    continues,
    join,
    junk_title,
)
from ytbrain.books.parse import Block, assemble
from ytbrain.books.probe import PdfFacts


def _book(paragraphs, outline=(), labels=None, pages=None):
    pages = pages or max(p.page for p in paragraphs)
    return ParsedBook(path="x.pdf", sha256="0" * 64, pages=pages, paragraphs=list(paragraphs),
                      outline=list(outline), page_labels=labels or {})


def _text(n_words: int, seed: str = "grow") -> str:
    return " ".join(f"{seed}{i}" for i in range(n_words)) + "."


class FakeParser:
    """Blocks from the fixture's text layer: its running header and page number are furniture,
    the lines that start a chapter or front/back matter are headings. Counts its calls."""
    name = "fake"
    HEADINGS = ("A Small Test Book", "Contents", "1 Start With the Customer", "2 Hire Slowly", "3 Charge Early", "Index")

    def __init__(self):
        self.calls = 0

    def blocks(self, path):
        import pypdfium2 as pdfium
        self.calls += 1
        doc = pdfium.PdfDocument(str(path))
        try:
            for i in range(len(doc)):
                tp = doc[i].get_textpage()
                lines = [ln.strip() for ln in tp.get_text_bounded().splitlines() if ln.strip()]
                body = []
                for ln in lines:
                    if ln == "A SMALL TEST BOOK" or ln.isdigit():
                        yield Block("page_header" if not ln.isdigit() else "page_footer", ln, i + 1, furniture=True)
                    elif ln in self.HEADINGS:
                        yield Block("section_header", ln, i + 1, level=1)
                    else:
                        body.append(ln)
                if body:
                    yield Block("text", "\n".join(body), i + 1)
        finally:
            doc.close()


# ---------------------------------------------------------------- normalize

def test_clean_text_makes_pdf_text_quotable():
    raw = "The ﬁrst “monopoly” rule¹ is simple.12 Then custo-\nmers­come ‘back’."
    assert clean_text(raw) == "The first \"monopoly\" rule is simple. Then customerscome 'back'."
    assert clean_text("In 2012 the market grew 40 percent.") == "In 2012 the market grew 40 percent."
    assert clean_text("  a\n\n b\t c ") == "a b c"


def test_page_break_continuations_join_and_sentences_dont():
    assert continues("the best founders keep", "talking to users")
    assert not continues("They kept talking.", "Then they sold")
    assert not continues("a list:", "one")
    assert join("a start-", "up idea") == "a startup idea"
    assert join("keep", "going") == "keep going"


def test_garbled_lines_are_recognised_by_their_missing_letters():
    from ytbrain.books.normalize import garbled_line
    assert garbled_line("e egeeed y oog te gt pa sa ouda eea t")
    assert not garbled_line("Every great business is built around a secret that is hidden from the outside.")
    assert garbled_line("short") is None


def test_junk_metadata_titles_are_flagged():
    for t in ("Microsoft Word - final_v3.docx", "untitled", "", None, "zero_to_one.pdf", "Document1"):
        assert junk_title(t), t
    assert not junk_title("Zero to One")


# ---------------------------------------------------------------- probe

def test_probe_reads_outline_labels_and_metadata_and_refuses_what_it_cant_read():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain.books.probe import probe
    ok = probe(FIX / "book.pdf")
    assert ok.ok and ok.pages == 10 and len(ok.sha256) == 64
    assert ok.page_labels[1] == "i" and ok.page_labels[4] == "1" and ok.page_labels[10] == "7"
    assert [(o.title, o.page) for o in ok.outline] == [("Contents", 3), ("1 Start With the Customer", 4),
                                                        ("2 Hire Slowly", 6), ("3 Charge Early", 8), ("Index", 10)]
    assert ok.meta == {"title": "A Small Test Book", "author": "Ada Example"}
    enc = probe(FIX / "encrypted.pdf")
    assert not enc.ok and "password" in enc.reason and "DRM" in enc.reason
    scan = probe(FIX / "scanned.pdf")
    assert not scan.ok and scan.reason.startswith("scanned")
    bad = probe(FIX / "garbled.pdf")
    assert not bad.ok and bad.reason.startswith("garbled text layer") and bad.garbled_pages == 3
    assert ok.garbled_lines == 0


# ---------------------------------------------------------------- assemble

def test_assemble_keeps_body_text_drops_the_rest_and_rejoins_page_breaks():
    facts = PdfFacts(path="x.pdf", sha256="f" * 64, ok=True, pages=3, page_labels={1: "7"})
    blocks = [Block("page_header", "ZERO TO ONE", 1, furniture=True),
              Block("section_header", "1 The Challenge", 1, level=1),
              Block("text", "Every moment in business happens only once and the next", 1),
              Block("page_footer", "7", 1, furniture=True),
              Block("text", "Bill Gates will not build an operating system.", 2),
              Block("table", "", 2), Block("picture", "", 2), Block("caption", "Figure 1", 2),
              Block("footnote", "1. See notes.", 2), Block("document_index", "", 3),
              Block("list_item", "• talk to users", 3), Block("text", "   ", 3)]
    b = assemble(blocks, facts, "fake")
    assert [(p.kind, p.page) for p in b.paragraphs] == [("heading", 1), ("text", 1), ("list", 3)]
    assert not continues("Pricing", "Most founders"), "a short line without a full stop is not a split sentence"
    assert b.paragraphs[1].text.endswith("the next Bill Gates will not build an operating system.")
    assert b.paragraphs[0].level == 1
    assert b.dropped["running header/footer"] == 2 and b.dropped["table"] == 1 and b.dropped["picture"] == 1
    assert b.dropped["joined across a page break"] == 1 and b.contents_pages == [3]
    assert b.label(1) == "7" and b.label(2) == "2"
    again = ParsedBook.from_dict(json.loads(json.dumps(b.to_dict())))
    assert again == b, "the cache round-trips exactly"


# ---------------------------------------------------------------- chapters

def _three_chapter_book():
    paras = [Paragraph("Contents", 1, "heading", 1), Paragraph("1 One ... 2 Two ... 3 Three", 1),
             Paragraph("1 Customers", 2, "heading", 1), Paragraph(_text(300, "c"), 2), Paragraph(_text(200, "c"), 3),
             Paragraph("2 Hiring", 4, "heading", 1), Paragraph(_text(400, "h"), 4),
             Paragraph("3 Fundraising", 5, "heading", 1), Paragraph(_text(350, "f"), 5),
             Paragraph("Acknowledgments", 6, "heading", 1), Paragraph(_text(120, "a"), 6),
             Paragraph("Index", 7, "heading", 1), Paragraph("customers, 2; hiring, 4", 7)]
    outline = [OutlineEntry("Contents", 1), OutlineEntry("1 Customers", 2), OutlineEntry("2 Hiring", 4),
               OutlineEntry("3 Fundraising", 5), OutlineEntry("Acknowledgments", 6), OutlineEntry("Index", 7)]
    labels = {1: "v", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6"}
    return paras, outline, labels


def test_outline_wins_and_front_and_back_matter_are_skipped():
    paras, outline, labels = _three_chapter_book()
    plan = C.detect(_book(paras, outline, labels))
    assert plan.method == "outline", plan.tried
    assert [c.title for c in plan.chapters] == ["Customers", "Hiring", "Fundraising"]
    assert [(c.start_page, c.end_page) for c in plan.chapters] == [(2, 3), (4, 4), (5, 5)]
    assert plan.skipped == ["Contents", "Acknowledgments", "Index"]
    assert plan.chapters[0].first == 2 and plan.chapters[-1].last == 8
    assert not plan.warnings


def test_include_and_exclude_override_the_matter_rules():
    paras, outline, labels = _three_chapter_book()
    plan = C.detect(_book(paras, outline, labels), {"include": ["acknowledg"], "exclude": ["hiring"]})
    assert [c.title for c in plan.chapters] == ["Customers", "Fundraising", "Acknowledgments"]


def test_parts_in_the_outline_are_skipped_for_the_chapters_below_them():
    paras, _, labels = _three_chapter_book()
    outline = [OutlineEntry("Part One", 2, 0), OutlineEntry("1 Customers", 2, 1), OutlineEntry("2 Hiring", 4, 1),
               OutlineEntry("Part Two", 5, 0), OutlineEntry("3 Fundraising", 5, 1), OutlineEntry("Index", 7, 0)]
    plan = C.detect(_book(paras, outline, labels))
    assert plan.method == "outline" and [c.title for c in plan.chapters] == ["Customers", "Hiring", "Fundraising"]
    assert "Index" in plan.skipped


def test_a_level_that_only_groups_parts_or_numbered_chapters_is_opened_and_sections_never_are():
    paras, _, labels = _three_chapter_book()
    grouped = [OutlineEntry("THE FORCE", 2, 0), OutlineEntry("PART 1: SAFE", 2, 1), OutlineEntry("1 Customers", 2, 2),
               OutlineEntry("THE PATH", 4, 0), OutlineEntry("PART 2: FORCES", 4, 1), OutlineEntry("2 Hiring", 4, 2),
               OutlineEntry("THE ABYSS", 5, 0), OutlineEntry("PART 3: REALITY", 5, 1), OutlineEntry("3 Fundraising", 5, 2),
               OutlineEntry("Index", 7, 0)]
    plan = C.detect(_book(paras, grouped, labels))
    assert [c.title for c in plan.chapters] == ["Customers", "Hiring", "Fundraising"], plan.tried
    assert any("level 1" in t for t in plan.tried), "the grouping level 0 was opened"
    sections = [OutlineEntry("1 Customers", 2, 0), OutlineEntry("Interviews", 2, 1), OutlineEntry("Pricing", 3, 1),
                OutlineEntry("2 Hiring", 4, 0), OutlineEntry("Sourcing", 4, 1),
                OutlineEntry("3 Fundraising", 5, 0), OutlineEntry("Seed", 5, 1), OutlineEntry("Index", 7, 0)]
    plan = C.detect(_book(paras, sections, labels))
    assert [c.title for c in plan.chapters] == ["Customers", "Hiring", "Fundraising"], "chapters with sections stay"


def test_photo_inserts_and_resource_lists_are_back_matter():
    for t in ("Photo Insert", "Photographs", "Additional Resources", "Resources", "Illustrations", "Photo Credits"):
        assert C.is_matter(t), t
    for t in ("Photography Pays", "Insertion Point", "Resourceful Founders"):
        assert not C.is_matter(t), t


def test_damaged_word_warnings_only_count_chapter_text():
    from ytbrain.books.ligatures import still_damaged
    assert dict(still_damaged(["the marketY2 grew", "see page10"])) == {"marketY2": 1, "page10": 1}


def test_headings_then_windows_when_the_outline_is_missing_or_useless():
    paras, _, labels = _three_chapter_book()
    plan = C.detect(_book(paras, [OutlineEntry("Cover", 1)], labels))
    assert plan.method == "headings" and len(plan.chapters) == 3
    assert any(t.startswith("outline:") for t in plan.tried)
    flat = [p for p in paras if p.kind == "text"]
    plan = C.detect(_book(flat, [], {}))
    assert plan.method == "windows" and any("page windows" in w for w in plan.warnings)
    assert any("no printed page labels" in w for w in plan.warnings)


def test_a_manual_list_uses_printed_page_labels_and_two_chapters_can_share_a_page():
    paras = [Paragraph("Why Now", 1, "heading", 1), Paragraph(_text(200, "w"), 1),
             Paragraph("Who Buys", 1, "heading", 1), Paragraph(_text(200, "b"), 1),
             Paragraph("How to Sell", 2, "heading", 1), Paragraph(_text(200, "s"), 2)]
    book = _book(paras, [], {1: "12", 2: "13"})
    cfg = {"chapters_only": True, "chapters": [{"title": "Why Now", "page": "12"}, {"title": "Who Buys", "page": 12},
                                               {"title": "How to Sell", "page": "13"}]}
    plan = C.detect(book, cfg)
    assert plan.method == "manual"
    assert [(c.first, c.last) for c in plan.chapters] == [(0, 1), (2, 3), (4, 5)]


def test_a_printed_contents_page_finds_chapters_when_there_are_no_bookmarks():
    from ytbrain.books.probe import contents_entries
    page, entries = contents_entries([["Title"], ["Contents", "Introduction ..... 1", "Part One VISION",
                                                   "1. Start .... 5", "2. Define 12", "PartTwo STEER", "3. Learn", "Index 40"]])
    assert page == 2 and entries == ["Introduction", "Part One VISION", "1. Start", "2. Define", "PartTwo STEER",
                                     "3. Learn", "Index"]
    paras = [Paragraph("Contents Introduction 1. Start 2. Define 3. Learn Index", 2),
             Paragraph("S Introduction " + _text(150, "i"), 3), Paragraph("Part One VISION", 4),
             Paragraph("B START " + _text(200, "s"), 5), Paragraph("Starting small is " + _text(20, "t"), 6),
             Paragraph("A 2 DEFINE " + _text(200, "d"), 7), Paragraph("Part Two STEER", 8),
             Paragraph("L LEARN " + _text(200, "l"), 9), Paragraph("Index customers 5", 10)]
    book = _book(paras)
    book.contents, book.contents_page = entries, 2
    plan = C.detect(book)
    assert plan.method == "contents", plan.tried
    assert [(c.title, c.start_page, c.end_page) for c in plan.chapters] == [
        ("Introduction", 3, 3), ("Start", 5, 6), ("Define", 7, 7), ("Learn", 9, 9)]
    assert {"Part One VISION", "PartTwo STEER", "Index"} <= set(plan.skipped)


def test_a_short_title_inside_body_text_never_starts_a_chapter():
    """Regression (The Lean Startup dry-run): a short paragraph on the next page that mentions
    "learning" used to be taken as the start of chapter "3. Learn"."""
    paras = [Paragraph("2 DEFINE " + _text(200, "a"), 1), Paragraph("A LEARN s an " + _text(200, "b"), 2),
             Paragraph("You cannot take learning to the bank.", 3), Paragraph(_text(100, "c"), 3),
             Paragraph("4 LEAP " + _text(200, "d"), 4), Paragraph("5 TEST " + _text(200, "e"), 5)]
    outline = [OutlineEntry("2. Define", 1), OutlineEntry("3. Learn", 2), OutlineEntry("4. Leap", 4),
               OutlineEntry("5. Test", 5)]
    plan = C.detect(_book(paras, outline))
    learn = next(c for c in plan.chapters if c.title == "Learn")
    assert (learn.first, learn.last, learn.start_page, learn.end_page) == (1, 3, 2, 3)


def test_numbering_is_stripped_but_titles_that_look_roman_are_not():
    assert C.plain("Chapter 4: The Ideology of Competition") == "The Ideology of Competition"
    assert C.plain("IV. Secrets") == "Secrets" and C.plain("12 Distribution") == "Distribution"
    assert C.plain("Civil War") == "Civil War" and C.plain("I Was Wrong") == "I Was Wrong"


def test_a_split_that_puts_most_of_the_book_in_one_chapter_is_rejected():
    paras = [Paragraph(_text(50, "x"), 1), Paragraph(_text(5000, "y"), 2), Paragraph(_text(60, "z"), 3),
             Paragraph(_text(60, "q"), 4)]
    outline = [OutlineEntry("A", 1), OutlineEntry("B", 2), OutlineEntry("C", 3), OutlineEntry("D", 4)]
    plan = C.detect(_book(paras, outline))
    assert plan.method == "windows" and any("rejected (one chapter holds" in t for t in plan.tried)


# ---------------------------------------------------------------- inspect / CLI

def test_inspect_reports_the_fixture_book_and_reuses_the_cached_parse():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain.books.inspect import render, report
    with tempfile.TemporaryDirectory() as t:
        fake = FakeParser()
        r = report(FIX / "book.pdf", Path(t), parser=fake)
        assert r["ok"] and r["method"] == "outline" and not r["cached"]
        assert [(c["title"], c["pages"]) for c in r["chapters"]] == [("Start With the Customer", "1-2"),
                                                                     ("Hire Slowly", "3-4"), ("Charge Early", "5-6")]
        assert r["skipped"][0].startswith("(before the first chapter") and "Contents" in r["skipped"]
        assert "Index" in r["skipped"] and r["dropped"]["running header/footer"] == 12
        assert r["metadata"]["title"] == "A Small Test Book" and not r["metadata_problems"]
        again = report(FIX / "book.pdf", Path(t), parser=fake)
        assert again["cached"] and fake.calls == 1, "a book is parsed once per file hash"
        text = render(r)
        assert "chapters by outline" in text and "Hire Slowly" in text
        refused = report(FIX / "encrypted.pdf", Path(t), parser=fake)
        assert not refused["ok"] and "refused: encrypted" in render(refused) and fake.calls == 1


def test_cli_books_inspect_prints_json_and_fails_for_a_refused_pdf():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain import cli
    from ytbrain.books import parse as P
    saved = dict(P.PARSERS)
    P.PARSERS["pdfium"] = FakeParser
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["books", "inspect", str(FIX), "--json", "--reparse"])
        rows = {r["file"]: r for r in json.loads(buf.getvalue())}
        assert code == 1, "refused fixtures make the run exit 1"
        assert rows["book.pdf"]["ok"] and not any(rows[f]["ok"] for f in ("scanned.pdf", "encrypted.pdf", "garbled.pdf"))
    finally:
        P.PARSERS.clear()
        P.PARSERS.update(saved)


# ---------------------------------------------------------------- more acceptance: parsers, failures, cache

def test_outline_parts_and_sections_open_into_their_chapters_and_more_matter_is_skipped():
    paras, _, labels = _three_chapter_book()
    outline = [OutlineEntry("About the Book", 1, 0), OutlineEntry("Section One: Basics", 2, 0),
               OutlineEntry("1 Customers", 2, 1), OutlineEntry("2 Hiring", 4, 1),
               OutlineEntry("Part Two", 5, 0), OutlineEntry("3a. Fundraising", 5, 1),
               OutlineEntry("Selected Bibliography", 6, 0), OutlineEntry("Illustration Credits", 7, 0)]
    plan = C.detect(_book(paras, outline, labels))
    assert plan.method == "outline" and "2 Parts opened" in plan.tried[-1]
    assert "Section One: Basics" in plan.skipped, "an opened Part bounds its chapters and is never one"
    assert [c.title for c in plan.chapters] == ["Customers", "Hiring", "Fundraising"]
    assert {"About the Book", "Selected Bibliography", "Illustration Credits"} <= set(plan.skipped)


def test_site_suffixes_are_stripped_from_metadata():
    from ytbrain.books.normalize import clean_meta
    assert clean_meta("Play Bigger - example.com") == "Play Bigger" and clean_meta("Zero to One") == "Zero to One"
    assert clean_meta(None) is None


def test_pdfium_parser_drops_running_headers_and_page_numbers_and_finds_the_chapters():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain.books.inspect import report
    from ytbrain.books.parse import PdfiumParser
    with tempfile.TemporaryDirectory() as t:
        r = report(FIX / "book.pdf", Path(t), parser=PdfiumParser())
        assert r["ok"] and r["parser"] == "pdfium" and r["method"] == "outline"
        assert [c["title"] for c in r["chapters"]] == ["Start With the Customer", "Hire Slowly", "Charge Early"]
        assert r["dropped"]["running header/footer"] >= 6
        assert r["words"] > 150 and all(c["words"] > 60 for c in r["chapters"])


class _Item:
    def __init__(self, label, text, page, layer="body", level=0):
        self.label, self.text, self.content_layer, self.level = label, text, layer, level
        self.prov = [type("P", (), {"page_no": page})()] if page else []


class _Result:
    def __init__(self, status, items=(), errors=()):
        self.status, self.errors = status, list(errors)
        self.document = type("D", (), {"iterate_items": lambda _s, **kw: ((i, 0) for i in items)})()


class _Converter:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def convert(self, path, raises_on_error=True):
        self.calls += 1
        assert raises_on_error is False
        return self.result


def test_docling_items_map_to_blocks_and_a_partial_parse_is_a_failure():
    from ytbrain.books.parse import DoclingParser, ParseError
    items = [_Item("page_header", "ZERO TO ONE", 3, "furniture"), _Item("section_header", "1 Secrets", 3, level=1),
             _Item("text", "Every great business is built around a secret.", 3), _Item("picture", "", 4),
             _Item("text", "no provenance", 0)]
    d = DoclingParser(threads=2, timeout_s=5)
    d._converter = _Converter(_Result("success", items))
    got = list(d.blocks(Path("x.pdf")))
    assert [(b.label, b.page, b.furniture, b.level) for b in got] == [
        ("page_header", 3, True, 0), ("section_header", 3, False, 1), ("text", 3, False, 0), ("picture", 4, False, 0)]
    err = type("E", (), {"error_message": "document timeout exceeded"})()
    d._converter = _Converter(_Result("partial_success", items, [err]))
    try:
        list(d.blocks(Path("x.pdf")))
        raise AssertionError("a partial parse must not pass")
    except ParseError as e:
        assert "partial_success" in str(e) and "timeout" in str(e)


class _Crash:
    name = "crash"

    def __init__(self, exc):
        self.exc = exc

    def blocks(self, path):
        raise self.exc


def test_one_failing_book_is_reported_and_never_stops_the_run():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain.books.inspect import report
    from ytbrain.books.parse import ParseError
    with tempfile.TemporaryDirectory() as t:
        for exc, want in ((ParseError("docling failure: bad page"), "parse failed: docling failure"),
                          (ValueError("boom"), "parse failed: ValueError: boom")):
            r = report(FIX / "book.pdf", Path(t), parser=_Crash(exc))
            assert not r["ok"] and r["reason"].startswith(want), r["reason"]
        try:
            report(FIX / "book.pdf", Path(t), parser=_Crash(RuntimeError('books need the pdf extra')))
            raise AssertionError("a missing extra stops the run")
        except RuntimeError as e:
            assert "pdf extra" in str(e)
        bad = Path(t) / "not-a-pdf.pdf"
        bad.write_bytes(b"this is not a pdf")
        r = report(bad, Path(t), parser=_Crash(ValueError("never called")))
        assert not r["ok"] and r["reason"].startswith("not a readable PDF")
        gone = report(Path(t) / "missing.pdf", Path(t), parser=_Crash(ValueError("never called")))
        assert not gone["ok"] and gone["reason"].startswith("can't read the file")


def test_the_cache_follows_the_file_hash_and_the_parser_and_survives_damage():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    import shutil

    from ytbrain.books.inspect import report
    with tempfile.TemporaryDirectory() as t:
        cache, fake = Path(t) / "cache", FakeParser()
        copy = Path(t) / "book.pdf"
        shutil.copy(FIX / "book.pdf", copy)
        assert not report(copy, cache, parser=fake)["cached"] and report(copy, cache, parser=fake)["cached"]
        assert fake.calls == 1
        other = FakeParser()
        other.name = "other"
        assert not report(copy, cache, parser=other)["cached"], "another parser never reuses this parse"
        with open(copy, "ab") as f:
            f.write(b"\n% edited\n")                # new bytes, same book: a new hash
        assert not report(copy, cache, parser=fake)["cached"] and fake.calls == 2
        for f in cache.glob("*.fake.json"):
            f.write_text("{not json")
        r = report(copy, cache, parser=fake)
        assert r["ok"] and not r["cached"] and fake.calls == 3, "a damaged cache is parsed again"


def test_cli_stops_with_a_clear_message_when_the_pdf_extra_is_missing():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain import cli
    from ytbrain.books import parse as P

    class NoExtra(_Crash):
        name = "pdfium"

        def __init__(self):
            super().__init__(RuntimeError('books need the pdf extra: uv pip install -e ".[pdf]"'))
    saved = dict(P.PARSERS)
    P.PARSERS["pdfium"] = NoExtra
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["books", "inspect", str(FIX / "book.pdf"), "--reparse"])
    finally:
        P.PARSERS.clear()
        P.PARSERS.update(saved)
    assert code == 2 and "pdf extra" in err.getvalue()


# ================================================================ P1: metadata (M1-M6)

# Lines modelled on the copyright pages of the owned books (docs/books-plan.md): the tests keep the
# shapes, not the books.

def test_isbn_prefers_the_ebook_edition():
    from ytbrain.books.metadata import isbn13, pick_isbn
    assert pick_isbn(["Identifiers: LCCN 2019058039 (print) | LCCN 2019058040 (ebook) |ISBN 9781984877864 (hardcover) |",
                      "ISBN 9781984877871 (ebook)"]) == "9781984877871", "a neighbour's (ebook) label is not this ISBN's"
    assert pick_isbn(["ISBN: 978-0-06240761-0", "EPub Edition JUNE 2016 ISBN 9780062407627"]) == "9780062407627"
    assert pick_isbn(["eISBN: 978-0-307-88791-7"]) == "9780307887917"
    assert pick_isbn(["Epub ISBN 9780753550304", "Hardback ISBN: 9780753555187",
                      "Trade Paperback ISBN: 9780753555194"]) == "9780753550304"
    assert pick_isbn(["ISBN 978-0-06-227320-8", "ISBN 978-0-06-227321-5"]) == "9780062273208", "unlabelled: the first"
    assert pick_isbn(["ISBN 0-306-40615-2"]) == "9780306406157", "ISBN-10 becomes ISBN-13"
    assert pick_isbn(["ISBN 978-0-00-000000-0"]) is None, "a bad checksum is not an ISBN"
    assert isbn13("978 0 06 227320 8") == "9780062273208"


def test_year_comes_from_the_copyright_page_only():
    from ytbrain.books.metadata import copyright_page, copyright_year
    assert copyright_year(["Copyright © 2020 by Netflix, Inc."]) == 2020
    assert copyright_year(["Epub ISBN 9780753550304", "Copyright © Peter Thiel 2014",
                           "Copyright, Designs and Patents Act 1988."]) == 2014
    assert copyright_year(["Copyright 2011 by Eric Ries"]) == 2011
    assert copyright_year(["Copyright, Designs and Patents Act 1988."]) is None
    pages = {256: ["Copyright © 2001 Boomer X Publishing, Inc.;"], 257: ["Copyright © 1978 Universal-PolyGram"],
             260: ["THE HARD THING ABOUT HARD THINGS. Copyright © 2014 by Ben Horowitz.", "ISBN 978-0-06-227320-8"]}
    page, lines = copyright_page(pages)
    assert page == 260 and copyright_year(lines) == 2014, "song permissions on other pages carry their own years"


def test_title_and_subtitle():
    from ytbrain.books.metadata import split_title
    assert split_title("Play Bigger: How Pirates, Dreamers, and Innovators Create and Dominate Markets - PDFDrive.com") == \
        ("Play Bigger", "How Pirates, Dreamers, and Innovators Create and Dominate Markets")
    assert split_title("The Lean Startup: How Today’s Entrepreneurs Use Continuous Innovation") == \
        ("The Lean Startup", "How Today's Entrepreneurs Use Continuous Innovation")
    assert split_title("Zero to One") == ("Zero to One", None)
    assert split_title("Microsoft Word - final_v3.docx") == (None, None) and split_title(None) == (None, None)


def test_authors_are_split_and_ordered():
    from ytbrain.books.metadata import speaker, split_authors
    assert split_authors("Horowitz, Ben") == ["Ben Horowitz"]
    assert split_authors("Al Ramadan & Dave Peterson & Christopher Lochhead & Kevin Maney") == \
        ["Al Ramadan", "Dave Peterson", "Christopher Lochhead", "Kevin Maney"]
    assert split_authors("Reed Hastings & Erin Meyer") == ["Reed Hastings", "Erin Meyer"]
    assert split_authors("Peter Thiel, Blake Masters and Ada Example") == ["Peter Thiel", "Blake Masters", "Ada Example"]
    assert split_authors("Eric Ries") == ["Eric Ries"] and split_authors(["Peter Thiel", "Blake Masters"]) == ["Peter Thiel", "Blake Masters"]
    assert speaker(["A B"]) == "A B" and speaker(["A B", "C D"]) == "A B and C D" and speaker(["A", "B", "C"]) == "A, B and C"


def test_metadata_precedence():
    from ytbrain.books.metadata import resolve
    pdf = {"title": "Zero to One", "author": "Blake Masters"}
    pages = {160: ["Epub ISBN 9780753550304", "Copyright © Peter Thiel 2014"]}
    calls = []

    def lookup(isbn):
        calls.append(isbn)
        return {"title": "Not This", "subtitle": "Notes on Startups", "authors": ["Someone Else"], "year": 1999,
                "publisher": "Crown Business"}
    m = resolve(pdf, pages, {"authors": ["Peter Thiel", "Blake Masters"]}, lookup)
    assert m.authors == ["Peter Thiel", "Blake Masters"] and m.origin["authors"] == "sources.yaml"
    assert (m.isbn, m.year) == ("9780753550304", 2014) and m.origin["isbn"] == "copyright page"
    assert m.title == "Zero to One" and m.origin["title"] == "pdf metadata"
    assert calls == ["9780753550304"], "one call, only because fields are still missing"
    assert m.subtitle == "Notes on Startups" and m.publisher == "Crown Business" and m.origin["subtitle"] == "open library"
    over = resolve(pdf, pages, {"title": "Zero to One", "subtitle": "Notes on Startups, or How to Build the Future",
                                "authors": "Peter Thiel & Blake Masters", "year": 2014}, lookup)
    assert over.subtitle == "Notes on Startups, or How to Build the Future" and over.year == 2014
    done = resolve({"title": "A: B", "author": "Ada Example"},
                   {3: ["Copyright \u00a9 2026 by Ada Example.", "eBook ISBN 978-0-00-000000-2"]}, {"publisher": "P"}, lookup)
    assert not done.problems() and len(calls) == 2, "nothing missing: no lookup at all"


def test_missing_metadata_is_flagged():
    from ytbrain.books.metadata import resolve
    bare = resolve({"title": "untitled"}, {})
    assert set(bare.problems()) == {"no usable title", "no author", "no year", "no ISBN"}
    zero = resolve({"title": "Zero to One", "author": "Blake Masters"},
                   {160: ["Epub ISBN 9780753550304", "Copyright © Peter Thiel 2014"]})
    assert zero.problems() == ["the copyright page names Peter Thiel, not among the authors"]
    fixed = resolve({"title": "Zero to One", "author": "Blake Masters"},
                    {160: ["Epub ISBN 9780753550304", "Copyright © Peter Thiel 2014"]}, {"authors": ["Peter Thiel", "Blake Masters"]})
    assert fixed.problems() == []
    org = resolve({"title": "No Rules Rules", "author": "Reed Hastings & Erin Meyer"},
                  {4: ["Copyright © 2020 by Netflix, Inc.", "ISBN 9781984877871 (ebook)"]})
    assert org.problems() == [], "an organisation on the copyright line is not a missing author"


def test_open_library_is_one_cached_call_and_failures_are_empty():
    import urllib.request

    from ytbrain.books.metadata import OpenLibrary
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=0):
        calls.append(req.full_url)
        if "000" in req.full_url:
            raise OSError("offline")
        return Resp(json.dumps({"ISBN:9780753550304": {"title": "Zero to One", "subtitle": "Notes on Startups",
                                "authors": [{"name": "Peter Thiel"}, {"name": "Blake Masters"}],
                                "publish_date": "September 16, 2014", "publishers": [{"name": "Crown"}]}}).encode())
    saved = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        with tempfile.TemporaryDirectory() as t:
            ol = OpenLibrary(Path(t), "ytbrain-test")
            first = ol("9780753550304")
            assert first == {"title": "Zero to One", "subtitle": "Notes on Startups", "authors": ["Peter Thiel", "Blake Masters"],
                             "year": 2014, "publisher": "Crown"}
            assert ol("9780753550304") == first and len(calls) == 1, "the second answer comes from the cache"
            assert ol("9780000000002") == {} and len(calls) == 2
    finally:
        urllib.request.urlopen = saved


# ================================================================ P1: Source, sync, clean (S1-S4)

def test_pdf_books_source_config():
    from ytbrain import sources as S
    src = S.normalize({"type": "pdf_books", "path": "data/books/startup"})
    assert src["id"] == "books_startup" and src["distribute"] is False and src["parser"] == "pdfium"
    assert S.normalize({"path": "data/books"})["type"] == "pdf_books", "a path alone means books"
    for bad, why in (({"type": "pdf_books"}, "needs path"), ({"path": "b", "distribute": True}, "private"),
                     ({"path": "b", "lookup": "google"}, "lookup"), ({"path": "b", "parser": "ocr"}, "parser"),
                     ({"path": "b", "books": {"x.pdf": {"titel": "typo"}}}, "unknown fields"),
                     ({"path": "b", "books": {"x.pdf": {"chapters": [{"title": "No page"}]}}}, "need a page"),
                     ({"type": "pdf_books", "path": []}, "needs path"), ({"type": "pdf_books", "path": ["a", "a"]}, "twice")):
        try:
            S.normalize(bad)
            raise AssertionError(f"accepted {bad}")
        except S.SourceConfigError as e:
            assert why in str(e), (why, str(e))
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        (root / "books" / "sub").mkdir(parents=True)
        for name in ("b.pdf", "a.PDF", "sub/c.pdf", "notes.txt"):
            (root / "books" / name).write_bytes(b"x")
        assert [p.name for p in S.book_files({"path": "books"}, root)] == ["a.PDF", "b.pdf", "c.pdf"]
        assert [p.name for p in S.book_files({"path": str(root / "books" / "b.pdf")}, root)] == ["b.pdf"]
        assert S.book_files({"path": "missing"}, root) == []
        # a new subfolder is found by itself; hidden, underscore and cache folders never are
        from ytbrain.config import BOOKS_PARSED
        for name in ("finance/e.pdf", ".trash/x.pdf", "_to_delete/y.pdf"):
            (root / "books" / name).parent.mkdir(parents=True, exist_ok=True)
            (root / "books" / name).write_bytes(b"x")
        assert [p.name for p in S.book_files({"path": "books"}, root)] == ["a.PDF", "b.pdf", "c.pdf", "e.pdf"]
        BOOKS_PARSED.mkdir(parents=True, exist_ok=True)
        (BOOKS_PARSED / "cached.pdf").write_bytes(b"x")
        assert "cached.pdf" not in [p.name for p in S.book_files({"path": str(BOOKS_PARSED.parent)}, root)]
        (root / "books" / "finance" / "e.pdf").unlink()
        # one Source, several folders (or files)
        (root / "lead").mkdir()
        (root / "lead" / "d.pdf").write_bytes(b"x")
        many = S.normalize({"type": "pdf_books", "id": "books", "path": ["books", "lead", "books/b.pdf"]})
        assert many["paths"] == ["books", "lead", "books/b.pdf"] and many["id"] == "books"
        assert [p.name for p in S.book_files(many, root)] == ["a.PDF", "b.pdf", "c.pdf", "d.pdf"], "each PDF once"
        (root / "lead" / "b.pdf").write_bytes(b"y")
        try:
            S.book_files(many, root)
            raise AssertionError("two PDFs with one name must be refused")
        except S.SourceConfigError as e:
            assert "two PDFs named 'b.pdf'" in str(e)
    assert S.adapter("pdf_books").doc_type == "book" and S.adapter_for_doc_type("book").type == "pdf_books"


def _book_env(t: Path):
    """A temporary books folder (the fixture book + an encrypted one), manifest and adapter."""
    import shutil

    from ytbrain.books.adapter import BookAdapter
    from ytbrain.manifest import Manifest
    (t / "books").mkdir()
    shutil.copy(FIX / "book.pdf", t / "books" / "book.pdf")
    shutil.copy(FIX / "encrypted.pdf", t / "books" / "locked.pdf")
    m = Manifest(t / "manifest.db")
    said: list[str] = []
    ad = BookAdapter(say=said.append, root=t, parsed_dir=t / "parsed", plans_dir=t / "plans", transcripts=t / "transcripts")
    return m, ad, said


IDS = ["9780000000002__start-with-the-customer", "9780000000002__hire-slowly", "9780000000002__charge-early"]


def test_book_sync_warns_about_a_folder_that_is_not_a_domain_and_strict_makes_it_exit_1():
    from types import SimpleNamespace

    from ytbrain import sources as S
    from ytbrain.books.adapter import BookAdapter
    from ytbrain.manifest import Manifest
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / "books" / "sytem-design").mkdir(parents=True)
        (t / "books" / "finance").mkdir()
        (t / "books" / "sytem-design" / "X.pdf").write_bytes(b"%PDF-1.4 not a book")
        (t / "books" / "finance" / "Y.pdf").write_bytes(b"%PDF-1.4 not a book")
        src = S.normalize({"path": "books"})
        said: list[str] = []
        ad = BookAdapter(say=said.append, root=t, parsed_dir=t / "p", plans_dir=t / "pl", transcripts=t / "tr")
        assert ad._stray(src, S.book_files(src, t)) == {"sytem-design": 1}
        m = Manifest(t / "m.db")
        from ytbrain import runstatus
        real_status, runstatus.STATUS = runstatus.STATUS, t / "stop.json"      # never your data/ops
        try:
            for strict in (False, True):
                said.clear()
                res = ad.sync(m, [src], SimpleNamespace(strict_domains=strict))
                assert any("sytem-design/" in line and "not a Domain name" in line for line in said), said
                assert not any("finance/" in line and "declared Domain" in line for line in said)
                assert res["code"] == 1, "the unreadable test PDFs are refused either way"
            assert (runstatus.read() or {}).get("reason") == "config", "strict tells `ops` to stop, not to retry the network"
        finally:
            runstatus.STATUS = real_status


def test_book_sync_registers_one_document_per_chapter_and_isolates_a_refused_pdf():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain import sources as S
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        m, ad, said = _book_env(t)
        src = S.normalize({"path": "books"})
        res = ad.sync(m, [src], None)
        assert res["code"] == 1, "one refused PDF makes the run exit 1"
        assert sorted(m.known_ids(src["id"])) == sorted(IDS)
        doc = m.get_document(IDS[1])
        assert (doc["doc_type"], doc["title"], doc["series"], doc["published_at"]) == ("book", "Hire Slowly", "A Small Test Book", "2026")
        assert m.stage_status(IDS[0], "fetch") == "ok", "clean waits on a settled fetch, like every Source"
        plan = json.loads((t / "plans" / "9780000000002.json").read_text())
        assert plan["meta"]["isbn"] == "9780000000002" and plan["method"] == "outline" and len(plan["chapters"]) == 3
        text = "\n".join(said)
        assert "locked.pdf: refused: encrypted" in text and "3 chapters by outline" in text and "(private)" in text


def test_book_sync_is_idempotent_and_tombstones_removed_books_but_keeps_a_failed_parse():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain import sources as S
    from ytbrain.books.parse import ParseError
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        m, ad, _said = _book_env(t)
        src = S.normalize({"path": "books"})
        ad.sync(m, [src], None)
        for did in IDS:
            ad.clean(m, did, m.get_document(did))
        before = {d: m.stage(d, "clean")["updated_at"] for d in IDS}
        ad.sync(m, [src], None)
        assert all(m.stage_status(d, "clean") == "ok" for d in IDS), "an unchanged book is not cleaned again"
        assert {d: m.stage(d, "clean")["updated_at"] for d in IDS} == before
        (t / "parsed").joinpath("x").write_text("")          # the parse cache is gone: a crash must not drop the book
        for f in (t / "parsed").glob("*.json"):
            f.unlink()
        ad._parser = _Crash(ParseError("simulated"))
        ad.sync(m, [src], None)
        assert sorted(m.known_ids(src["id"])) == sorted(IDS), "a failed parse keeps what was there"
        ad._parser = None
        (t / "books" / "book.pdf").unlink()
        ad.sync(m, [src], None)
        assert m.known_ids(src["id"]) == set(), "a book removed from the folder leaves the index"


def test_a_second_pdf_of_the_same_book_is_refused_instead_of_overwriting_the_first():
    """Two editions or a copy share a book id: the second would overwrite the first's plan and orphan its Chapters."""
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    import shutil

    from ytbrain import sources as S
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        m, ad, said = _book_env(t)
        shutil.copy(t / "books" / "book.pdf", t / "books" / "copy-of-book.pdf")
        src = S.normalize({"path": "books"})
        res = ad.sync(m, [src], None)
        text = "\n".join(said)
        assert "copy-of-book.pdf: refused: same book id (9780000000002) as book.pdf" in text, text
        assert "skip: true" in text and res["code"] == 1
        assert sorted(m.known_ids(src["id"])) == sorted(IDS), "the first PDF keeps every Chapter"
        plan = json.loads((t / "plans" / "9780000000002.json").read_text())
        assert plan["file"] == "book.pdf", "the first file, in folder order, owns the plan"
        said.clear()
        skipped = S.normalize({"path": "books", "books": {"copy-of-book.pdf": {"skip": True}}})
        ad.sync(m, [skipped], None)
        assert "same book id" not in "\n".join(said) and "copy-of-book.pdf: skipped" in "\n".join(said)
        assert sorted(m.known_ids(skipped["id"])) == sorted(IDS)


def test_book_clean_writes_the_shared_transcript_and_reacts_to_changes():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain import sources as S
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        m, ad, _ = _book_env(t)
        src = S.normalize({"path": "books"})
        ad.sync(m, [src], None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert ad.clean(m, IDS[0], m.get_document(IDS[0])) == "ok"
        tr = json.loads((t / "transcripts" / f"{IDS[0]}.json").read_text())
        assert (tr["source_kind"], tr["locator"], tr["private"], tr["series"], tr["speaker"]) == \
            ("chapter", "page", True, "A Small Test Book", "Ada Example")
        assert tr["book"]["isbn"] == "9780000000002" and tr["book"]["chapter"] == 1 and tr["book"]["chapters"] == 3
        starts = [u["start_ms"] for u in tr["utterances"]]
        assert starts == sorted(set(starts)) and all(s // 1000 in (4, 5) for s in starts), "page * 1000 + n, unique and ordered"
        assert tr["page_labels"] == {"4": "1", "5": "2"} and "chapter 1/3" in out.getvalue()
        assert m.stage_status(IDS[0], "clean") == "ok"
        m.mark(__import__("ytbrain.manifest", fromlist=["StageState"]).StageState(IDS[0], "extract", "ok"))
        src2 = S.normalize({"path": "books", "books": {"book.pdf": {"authors": ["Ada Example", "Bo Example"]}}})
        with contextlib.redirect_stdout(io.StringIO()):
            ad.sync(m, [src2], None)
            assert m.stage_status(IDS[0], "clean") == "stale", "new metadata: clean again"
            ad.clean(m, IDS[0], m.get_document(IDS[0]))
        assert m.stage_status(IDS[0], "extract") == "stale", "a changed transcript sends extraction back"
        for f in (t / "plans").glob("*.json"):
            f.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            assert ad.clean(m, IDS[1], m.get_document(IDS[1])) == "refetch"
        assert m.stage_status(IDS[1], "fetch") == "stale", "a missing plan sends the Chapter back to sync"


def test_partial_chapter_override():
    """A `chapters:` entry's page is the printed label when the book has labels (label "3" is PDF page 4)."""
    paras, outline, labels = _three_chapter_book()
    book = _book(paras, outline, labels)
    renamed = C.detect(book, {"chapters": [{"page": "3", "title": "Hiring, Slowly"}]})
    assert [c.title for c in renamed.chapters] == ["Customers", "Hiring, Slowly", "Fundraising"]
    assert "1 corrections from sources.yaml" in renamed.tried[-1]
    added = C.detect(book, {"chapters": [{"page": "2", "title": "Customers, Continued"}]})
    assert [(c.title, c.start_page) for c in added.chapters] == [("Customers", 2), ("Customers, Continued", 3),
                                                                ("Hiring", 4), ("Fundraising", 5)]
    moved = C.detect(book, {"chapters": [{"page": "2", "title": "Customers, Continued"}, {"page": "3", "skip": True}]})
    assert [c.title for c in moved.chapters] == ["Customers", "Customers, Continued", "Fundraising"]
    assert moved.chapters[1].end_page == 4, "a skipped start joins the chapter before it"


def test_expectations_compare_every_field():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain.books.inspect import compare, report
    with tempfile.TemporaryDirectory() as t:
        r = report(FIX / "book.pdf", Path(t))
        want = {"title": "A Small Test Book", "subtitle": None, "authors": ["Ada Example"], "year": 2026,
                "isbn": "9780000000002", "method": "outline", "chapters": 3,
                "first_chapter": "Start With the Customer", "last_chapter": "Charge Early"}
        assert compare(r, want) == []
        wrong = compare(r, {**want, "year": 2025, "authors": ["Someone"], "chapters": 4})
        assert len(wrong) == 3 and any(w.startswith("year: expected 2025") for w in wrong)


def test_cli_books_inspect_expect_passes_and_fails():
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    import yaml

    from ytbrain import cli
    with tempfile.TemporaryDirectory() as t:
        good = Path(t) / "good.yaml"
        good.write_text(yaml.safe_dump({"books": {"book.pdf": {"isbn": "9780000000002", "chapters": 3, "year": 2026}}}))
        bad = Path(t) / "bad.yaml"
        bad.write_text(yaml.safe_dump({"books": {"book.pdf": {"chapters": 5}, "other.pdf": {"chapters": 1}}}))
        for exp, want_code, want_text in ((good, 0, "1/1 books match"), (bad, 1, "EXPECTED BOOK NOT FOUND: other.pdf")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["books", "inspect", str(FIX / "book.pdf"), "--expect", str(exp)])
            assert code == want_code and want_text in buf.getvalue(), buf.getvalue()[-400:]


def test_chapter_prompts_speak_of_a_book_chapter_and_the_talk_prompts_are_unchanged():
    import re

    from ytbrain.extract import prompts
    cp = prompts.for_kind("chapter")
    for name in ("EXTRACT_PROMPT", "GROUNDING_FEEDBACK", "OVERVIEW_PROMPT"):
        text = re.sub(r"\{[a-z_]+\}", "", getattr(cp, name))            # placeholders aren't wording
        left = re.findall(r"\b(talks?|transcripts?|clips?|captions?|timestamps?|video)\b", text)
        assert not left, (name, left)
    assert "\nChapter: {title}\nBook: {series}\nSections: {chapter_titles}" in cp.EXTRACT_PROMPT
    assert "not the authors' advice unless they endorse it" in cp.EXTRACT_PROMPT
    assert prompts.for_kind("talk").EXTRACT_PROMPT == prompts.EXTRACT_PROMPT
    assert prompts.for_kind("article").hash() != cp.hash() != prompts.for_kind("talk").hash()
    text = cp.format([{"text": "One.", "start_ms": 4001}, {"text": "Two.", "start_ms": 4002},
                      {"text": "Three.", "start_ms": 5001}])
    assert text == "[p. 4] One.\nTwo.\n[p. 5] Three.", "a page marker where each page starts"


def test_page_locators_name_the_printed_page_or_the_pdf_page():
    from ytbrain import locators as L
    assert L.label(72003, "page") == "PDF p. 72" and L.label(72003, "page", {"72": "48"}) == "p. 48"
    assert L.label(None, "page") == "p. –" and L.label(-1, "time") == "--:--" and L.label(12, "paragraph") == "¶12"
    assert L.label_for({"locator": "page", "page_labels": {"4": "1"}}, 4002) == "p. 1"
    assert L.deep_link(None, "b__x", 72003, "page") == "", "a private PDF has no link: its label is the Citation"
    assert L.deep_link("https://ex.com/book.pdf", "b__x", 72003, "page") == "https://ex.com/book.pdf#page=72"


def test_a_chapter_goes_through_extract_verify_pages_items_and_moments():
    """The P2 path on the fixture book: its Chapter extracts with the chapter prompt (fake LLM),
    verifies, renders with page Citations and becomes private Knowledge items with a Book header."""
    if not HAVE_PDFIUM:
        return _skipped("pypdfium2 not installed")
    from ytbrain import pages, verify
    from ytbrain import sources as S
    from ytbrain.eval.moments import is_book, moment_for, moment_text, parse_moment_id
    from ytbrain.extract import runner
    from ytbrain.knowledge.items import build_items
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        m, ad, _ = _book_env(t)
        with contextlib.redirect_stdout(io.StringIO()):
            ad.sync(m, [S.normalize({"path": "books"})], None)
            ad.clean(m, IDS[0], m.get_document(IDS[0]))
        tr = json.loads((t / "transcripts" / f"{IDS[0]}.json").read_text())
        unit = tr["utterances"][1]
        quote = " ".join(unit["text"].split()[:14])
        sent = []

        def fake(prompt, schema, **kw):
            sent.append(prompt)
            return json.dumps({"title_canonical": "Start with the customer", "speaker": "Someone Invented",
                               "category": "customer-discovery", "summary": "s",
                               "highlights": [{"text": "h", "evidence_span": quote}],
                               "advice_atoms": [{"atom_id": "a01", "text": "Talk to customers first",
                                                 "evidence_span": quote}]})
        runner.BACKENDS["fake-book"] = fake
        doc = m.get_document(IDS[0])
        rec = runner.extract_video(tr, {"doc_id": IDS[0], "title": doc["title"], "series": doc["series"],
                                        "published_at": doc["published_at"], "source_kind": "chapter",
                                        "speaker": tr["speaker"], "private": tr["private"],
                                        "page_labels": tr["page_labels"]},
                                   tr["chapters"], backend="fake-book").model_dump()
        assert len(sent) == 1, "no chaptering call: a Chapter's Sections are its headings"
        assert "Chapter: Start With the Customer" in sent[0] and "Book: A Small Test Book by Ada Example" in sent[0]
        assert "[p. 4]" in sent[0] and "TEXT:" in sent[0]
        assert (rec["source_kind"], rec["locator"], rec["private"], rec["url"], rec["caption_kind"]) == \
            ("chapter", "page", True, "", "none")
        assert rec["speaker"] == "Ada Example", "the Book's authors are given, never generated"
        assert rec["page_labels"] == {"4": "1", "5": "2"}
        report = verify.verify_record(rec, tr["utterances"])
        assert report["failed"] == 0 and rec["advice_atoms"][0]["timestamp_ms"] == unit["start_ms"]
        rec["extraction_meta"]["verification"] = {"threshold": 0.7, **report}
        md = pages.render_markdown(rec)
        page = unit["start_ms"] // 1000
        assert f"(p. {tr['page_labels'][str(page)]})" in md and "]()" not in md and "youtube" not in md
        assert "private: true" in md and "source_kind: \"chapter\"" in md
        items = build_items(rec, tr)
        assert items and all(i["visibility"] == "private" and i["source_kind"] == "chapter" for i in items)
        adv = next(i for i in items if i["kind"] == "advice")
        assert adv["context_header"].startswith('From the book "A Small Test Book" by Ada Example (2026), '
                                                'chapter "Start With the Customer"'), adv["context_header"]
        assert adv["deep_link"] == ""
        mid = moment_for(IDS[0], adv["start_ms"])
        assert mid == f"{IDS[0]}_b{page:05d}" and parse_moment_id(mid) == (IDS[0], page) and is_book(IDS[0])
        assert unit["text"] in moment_text(tr["utterances"], page, locator="page")


def test_chapter_ids_and_moments_never_look_like_a_talk():
    from ytbrain.books.adapter import chapter_ids
    from ytbrain.eval.moments import is_book, moment_for, moment_id
    assert chapter_ids("abc", ["Go", "Go"]) == ["abc__chapter-01-go", "abc__chapter-02-go"]
    assert all(len(d) != 11 for d in chapter_ids("abcd", ["efghi", "x"]))
    assert not is_book("UdIPveR__jw"), "a YouTube id may hold '__'"
    assert moment_for("UdIPveR__jw", 61_000) == moment_id("UdIPveR__jw", 60)


def test_extract_and_sample_take_a_document_prefix():
    from ytbrain import cli
    ns = type("A", (), {"doc": ["9780753550304"]})()
    assert cli._only({"9780753550304__secrets", "9780062273215__intro", "UdIPveR__jw"}, ns) == {"9780753550304__secrets"}
    assert cli._only({"a", "b"}, type("A", (), {"doc": None})()) == {"a", "b"}


def test_the_sample_sheet_says_how_to_check_each_source_kind():
    import contextlib, io, json, types
    from ytbrain import cli
    from ytbrain.config import METADATA, REPORTS
    METADATA.mkdir(parents=True, exist_ok=True)
    recs = {"smp_talk_01": {"locator": "time", "url": "https://youtu.be/x", "caption_kind": "manual"},
            "9780000000001__smp-ch": {"locator": "page", "source_kind": "chapter", "url": None}}
    for doc_id, extra in recs.items():
        (METADATA / f"{doc_id}.json").write_text(json.dumps(
            {"doc_id": doc_id, "title_raw": doc_id, "highlights": [], "advice_atoms": [], **extra}))
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.cmd_sample(types.SimpleNamespace(n=2, seed=1, doc=["smp_talk", "9780000000001"])) == 0
    sheet = (REPORTS / "sample-seed1-n2.md").read_text()
    assert "- talk: open the video at the cited timestamps" in sheet and "- video: https://youtu.be/x" in sheet
    assert "- chapter: open the book at the cited pages" in sheet and "- book: (no link: your own copy)" in sheet


# ---------------------------------------------------------------- ligatures

def test_damaged_ligatures_are_repaired_from_the_word_list():
    from ytbrain.books.ligatures import Repairs, repair_text
    vocab = {"different": 40, "efforts": 9, "significant": 30, "profit": 20, "influenced": 8, "difficult": 25,
             "office": 30, "scientific": 12, "satisfied": 5, "first": 90, "stuff": 4, "off": 60, "influence": 15}
    fixes = {"di@erent": "different", "e&orts": "efforts", "signi%cant": "significant", "pro1t": "profit",
             "in3uenced": "influenced", "diKcult": "difficult", "oDce": "office", "scienti+c": "scientific",
             "satis#ed": "satisfied", "di>cult": "difficult", "in<uence": "influence", "inJuence": "influence"}
    r = Repairs()
    for bad, good in fixes.items():
        assert repair_text(f"a {bad} case", vocab, r) == f"a {good} case", bad
    assert r.words == len(fixes) and not r.unresolved
    assert repair_text("stuL and oL", vocab) == "stuff and off", "a capital letter can stand in for a ligature"


def test_a_damaged_word_at_a_sentence_start_keeps_its_capital():
    from ytbrain.books.ligatures import repair_text
    vocab = {"first": 90, "different": 40}
    assert repair_text("8rst we wait.", vocab) == "First we wait."
    assert repair_text("It ends. DiLerent now, and %rst too.", vocab) == "It ends. Different now, and first too."
    assert repair_text("and 8rst", vocab) == "and first"


def test_words_that_are_not_damaged_ligatures_are_left_exactly_as_printed():
    from ytbrain.books.ligatures import Repairs, repair_text
    vocab = {"first": 90, "different": 40, "profit": 20}
    r = Repairs()
    for ok in ("the 1st and 2nd 3rd", "a 4x gain", "R&D and Q&A", "an iPhone or eBay", "QuickBooks", "3D printing",
               "COVID19", "A=B", "see http://example.com/di@erent", "visit www.site.com/pro1t.html"):
        assert repair_text(ok, vocab, r) == ok, ok
    assert not r.fixed


def test_a_damaged_word_no_ligature_explains_is_counted_never_guessed():
    from ytbrain.books.ligatures import Repairs, repair_text
    r = Repairs()
    assert repair_text("the marketY2 and at1rst word", {"first": 90}, r) == "the marketY2 and at1rst word"
    assert not r.fixed and set(r.unresolved) == {"marketY2", "at1rst"}
    assert r.as_dict()["unresolved"] == 2


def test_control_characters_between_letters_are_hyphens_and_the_rest_are_spaces():
    from ytbrain.books.ligatures import Repairs, control_hyphens
    r = Repairs()
    assert control_hyphens("one\x02time and salt-and\x02pepper", r) == "one-time and salt-and-pepper"
    assert r.hyphens == 2
    assert control_hyphens("a\x02 b\x03c", r) == "a- b-c" and r.hyphens == 4
    assert control_hyphens("(\x02)", r) == "( )"


def test_a_book_is_repaired_with_its_own_spellings_too():
    from ytbrain.books.ligatures import repair_book
    texts = ["The affiliate model is different here.", "An a@iliate earns a fee.", "Another di@erent case."]
    out, r = repair_book(texts)
    assert out[1] == "An affiliate earns a fee.", "the book's own 'affiliate' resolves its damaged one"
    assert out[2] == "Another different case." and out[0] == texts[0]
    assert r.words == 2 and r.as_dict()["distinct"] == 2


def test_assemble_repairs_paragraphs_outline_and_contents_and_reports_it():
    facts = PdfFacts(path="x.pdf", sha256="e" * 64, ok=True, pages=2, page_labels={},
                     outline=[OutlineEntry("1 The Di@erent Way", 1)])
    blocks = [Block("section_header", "1 The Di@erent Way", 1, level=1),
              Block("text", "We %rst tried the one\x02time fix; it was di@erent from the rest.", 1),
              Block("text", "The di@erent path won.", 2)]
    b = assemble(blocks, facts, "fake")
    text = " ".join(p.text for p in b.paragraphs)
    assert "first" in text and "one-time" in text and "@" not in text and "\x02" not in text
    assert all("@" not in o.title for o in b.outline) and "Different" in b.outline[0].title
    assert b.repaired["words"] >= 2 and b.repaired["hyphens"] == 1
    again = ParsedBook.from_dict(json.loads(json.dumps(b.to_dict())))
    assert again.repaired == b.repaired, "the report survives the cache"


def test_a_cached_parse_of_an_older_format_is_parsed_again():
    from ytbrain.books.model import PARSED_FORMAT
    d = _book([Paragraph("Text.", 1)]).to_dict()
    assert d["format"] == PARSED_FORMAT
    d["format"] = PARSED_FORMAT - 1
    try:
        ParsedBook.from_dict(d)
    except ValueError:
        return
    raise AssertionError("an older cached parse must be refused")


if __name__ == "__main__":
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:                                   # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
