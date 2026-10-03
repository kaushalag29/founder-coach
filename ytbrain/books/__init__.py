"""PDF Books as a Source type (ADR-0014, docs/books-plan.md).

A Book is one PDF; each of its Chapters is a Document. The modules, each usable on its own:

  model      the data shapes: ParsedBook (text-only paragraphs with pages), Chapter, ChapterPlan
  probe      a fast, model-free look at the file (pypdfium2): refusals, outline, page labels,
             contents page, copyright page
  normalize  PDF text clean-up so Evidence quotes verify (ligatures, hyphenation, footnote marks)
  parse      BookParser: PdfiumParser (default, model-free) and DoclingParser (parked) -> ParsedBook
  chapters   the chapter-detection chain (manual, outline, headings, page windows) and front/back matter
  inspect    one report of all of the above, for `ytbrain books inspect`
  metadata   the Book's given fields: sources.yaml > copyright page > PDF metadata > Open Library
  adapter    BookAdapter: sync (one Document per Chapter) and clean (the shared transcript)
"""

