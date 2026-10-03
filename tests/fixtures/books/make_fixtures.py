"""Rebuild the tiny test PDFs (text written for these tests; no real book). Needs reportlab and
pypdf, which the tests themselves don't:  python tests/fixtures/books/make_fixtures.py

  book.pdf       10 pages: title, copyright, contents (roman labels i-iii), three chapters of two pages
                 (labels 1-6, running header and page number on each), index (label 7); bookmarks
  scanned.pdf    3 pages with a drawing and no text layer
  encrypted.pdf  book.pdf behind a user password
  garbled.pdf    3 pages of text in a "font" with no working text map (letters r, n, h, i, f, l, m missing)
"""
import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.constants import PageLabelStyle
from reportlab.lib.pagesizes import A5
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
PAGES = [
    ("front", "A Small Test Book", ["Ada Example"]),
    ("front", None, ["Copyright \u00a9 2026 by Ada Example. All rights reserved.",
                     "ISBN 978-1-00-000000-9 (hardcover)", "eBook ISBN 978-0-00-000000-2"]),
    ("front", "Contents", ["1 Start With the Customer ..... 1", "2 Hire Slowly ..... 3", "3 Charge Early ..... 5",
                           "Index ..... 7"]),
    ("body", "1 Start With the Customer",
     ["Talk to ten customers before you write any code. Ask what they did the last time",
      "the problem hurt, not what they would buy. Answers about past behaviour are far",
      "more reliable than any promise about the future, and they cost you nothing but",
      "an afternoon. Keep asking until the answers stop surprising you."]),
    ("body", None,
     ["Write down each conversation the same day. Patterns only show up when you can",
      "compare notes across interviews, and memory quietly rewrites what people said.",
      "A shared document with one page per interview is enough for the first month."]),
    ("body", "2 Hire Slowly",
     ["Hire slowly and only when the pain of not hiring is obvious to everyone. Every",
      "early hire changes the culture more than any document you write about it, so",
      "the first ten people decide what the company will feel like for years."]),
    ("body", None,
     ["Check references by calling people the candidate did not list. The best signal",
      "is how former teammates describe working with them during a bad week, when the",
      "plan failed and nobody knew what to do next."]),
    ("body", "3 Charge Early",
     ["Charge from the first customer, even if the price is small. Payment is the only",
      "honest signal that the problem matters, and a customer who pays tells you what",
      "is missing far more directly than one who is using the product for free."]),
    ("body", None,
     ["Raise prices before you think you are ready. Most early teams undercharge because",
      "they confuse the discomfort of asking with the value they deliver to the buyer."]),
    ("back", "Index", ["customers, 1", "hiring, 3", "pricing, 5"]),
]


def book_bytes() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A5)
    w, h = A5
    n = 0
    for part, heading, lines in PAGES:
        if part == "body":
            n += 1
            c.setFont("Helvetica", 8)
            c.drawString(40, h - 30, "A SMALL TEST BOOK")
            c.drawString(w / 2, 30, str(n))
        y = h - 70
        if heading:
            c.setFont("Helvetica-Bold", 16)
            c.drawString(40, y, heading)
            y -= 30
        c.setFont("Times-Roman", 10)
        for ln in lines:
            c.drawString(40, y, ln)
            y -= 14
        c.showPage()
    c.save()
    wtr = PdfWriter()
    wtr.append(PdfReader(io.BytesIO(buf.getvalue())))
    wtr.set_page_label(0, 2, style=PageLabelStyle.LOWERCASE_ROMAN)
    wtr.set_page_label(3, 9, style=PageLabelStyle.DECIMAL, start=1)
    wtr.add_metadata({"/Title": "A Small Test Book", "/Author": "Ada Example"})
    for title, page in (("Contents", 2), ("1 Start With the Customer", 3), ("2 Hire Slowly", 5),
                        ("3 Charge Early", 7), ("Index", 9)):
        wtr.add_outline_item(title, page)
    out = io.BytesIO()
    wtr.write(out)
    return out.getvalue()


def scanned_bytes() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A5)
    for _ in range(3):
        c.rect(40, 40, 300, 500, fill=1)
        c.showPage()
    c.save()
    return buf.getvalue()


def garbled_bytes() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A5)
    line = "e egeeed y oog te gt pa sa ouda eea"
    for _ in range(3):
        c.setFont("Times-Roman", 10)
        for k in range(30):
            c.drawString(40, 520 - 14 * k, line)
        c.showPage()
    c.save()
    return buf.getvalue()


def encrypted_bytes(plain: bytes) -> bytes:
    wtr = PdfWriter()
    wtr.append(PdfReader(io.BytesIO(plain)))
    wtr.encrypt(user_password="secret", owner_password="owner", algorithm="AES-128")
    out = io.BytesIO()
    wtr.write(out)
    return out.getvalue()


if __name__ == "__main__":
    book = book_bytes()
    (HERE / "book.pdf").write_bytes(book)
    (HERE / "scanned.pdf").write_bytes(scanned_bytes())
    (HERE / "encrypted.pdf").write_bytes(encrypted_bytes(book))
    (HERE / "garbled.pdf").write_bytes(garbled_bytes())
    print("wrote", ", ".join(p.name for p in sorted(HERE.glob("*.pdf"))))
