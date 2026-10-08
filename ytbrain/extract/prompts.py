"""Prompts for phase 1.

One extraction call per video (decision Q9). The model is NOT asked for
timestamps - it is asked for verbatim quotes, and verify.py resolves those to
positions in the transcript. That single property is what removes the usual
reason for chunking a document before extraction.

Chapters come from the uploader when the video has them; CHAPTER_PROMPT runs
only for the remainder.
"""
from __future__ import annotations

import hashlib

CHAPTER_PROMPT = """\
Segment this startup-advice talk into chapters using only its transcript.

Rules:
- Between 5 and 12 chapters. Fewer is better than forcing boundaries.
- Each chapter starts at a real topic shift, not at a fixed interval.
- `title` is 3-8 words and specific to this talk ("Why founders over-build the
  MVP", not "Product").
- `start_ms` must be copied from one of the `[ms]` markers in the transcript
  below. Do not compute or invent a value.
- `source` must be "llm".
- If the talk has no discernible structure, return one chapter covering it.

Return JSON matching this schema:
{schema}

TRANSCRIPT:
{transcript}
"""

EXTRACT_PROMPT = """\
Build structured metadata for a founder-coaching knowledge base from this talk.

Talk: {title}
Series: {series}
Chapters: {chapter_titles}

Hard rules:
- EVERY highlight and advice atom MUST carry `evidence_span`: a quote from the
  transcript that supports it. If you cannot quote support for a claim, do not
  make the claim. Copy ONE contiguous passage of 10-40 words exactly as it
  appears in the transcript: keep repeated words, filler ("like", "you know")
  and caption misspellings, and do not fix grammar or stitch passages together
  with "...". A title, heading or slogan of your own is not a quote.
- Do NOT output timestamps. They are derived from the transcript afterwards.
- `advice_atoms` are single, actionable, imperative recommendations ("Talk to
  ten users before writing code"), one idea each, never compound. Give each a
  short unique `atom_id` like "a01". Any highlight that tells a founder what to
  do must also appear as an advice atom. Interviews and Q&A sessions often hold
  the most concrete advice; extract it.
- `highlights` are the talk's key takeaways, paraphrased, each with its quote.
- `category` must come from the controlled list; pick the ONE that best matches
  what a founder would come to this talk to learn. Two broad ones:
    "ai-and-tech-trends": where AI/technology/markets are heading, how a
      technology works, industry outlooks and batch statistics.
    "founder-story": one founder's or company's journey told as a narrative.
  Prefer a specific topic (e.g. "sales", "fundraising") over these when the
  talk is mainly practical advice on it. Use "other" only if nothing fits.
- `speaker`: only if the transcript or title makes it clear. Otherwise null.
- `summary`: about 150 words, plain prose, no bullet points.
- Set `confidence` honestly; "low" is a useful answer.
- Size: at most {max_highlights} highlights and {max_advice} advice atoms (scaled
  to this talk's length) — the most important ones, not every point made. Keep
  each quote under 40 words.
- Record anything you could not determine in `unknowns_and_gaps` rather than
  guessing. Only a clip that is purely logistics (announcements, introductions,
  housekeeping) should return few or no atoms, and say so there.

Return JSON matching this schema:
{schema}
{feedback}
TRANSCRIPT:
{transcript}
"""

REPAIR_PROMPT = """\
Your previous output failed schema validation with these errors:

{errors}

Return the COMPLETE corrected JSON only. Keep every valid item exactly as it is
and fix only what the errors name. Where a required value is genuinely unknown,
use null or an empty list and note it in `unknowns_and_gaps`.

Previous output:
{previous}
"""

CONCISE_SUFFIX = """

IMPORTANT: your previous answer was cut off because it was too long. Return at
most {max_highlights} highlights and {max_advice} advice atoms, keep `summary`
under 150 words and every quote under 30 words.
"""

GROUNDING_FEEDBACK = """
FEEDBACK ON YOUR PREVIOUS ATTEMPT (fix these):
{problems}
Copy every evidence_span word for word from the TRANSCRIPT below, as one
contiguous passage. Drop any item you cannot support with an exact quote.
"""

OVERVIEW_PROMPT = """\
These are section summaries and key takeaways of ONE talk, extracted section by
section. Write the talk-level fields for the whole talk.

Talk: {title}
Series: {series}

Section summaries:
{summaries}

Key takeaways:
{takeaways}

Rules: `summary` about 150 words of plain prose covering the whole talk;
`category` from the controlled list (prefer a specific topic over
"ai-and-tech-trends"/"founder-story" when the talk is mainly practical advice);
`speaker` only if clear, else null.

Return JSON matching this schema:
{schema}
"""


# ---- the subject-neutral variant (ADR-0018) -------------------------------------------------
# For any subject (software, investing, management, a craft): no startup Category or Stage. Advice says the
# situation it is for (`applies_when`) and declarative statements become Facts. Written with the same
# talk-specific phrases as the startup prompts, so the article and book-chapter conversions below apply.

NEUTRAL_CHAPTER_PROMPT = """\
Segment this talk into chapters using only its transcript.

Rules:
- Between 5 and 12 chapters. Fewer is better than forcing boundaries.
- Each chapter starts at a real topic shift, not at a fixed interval.
- `title` is 3-8 words and specific to this talk ("Why replication lag breaks
  reads", not "Databases").
- `start_ms` must be copied from one of the `[ms]` markers in the transcript
  below. Do not compute or invent a value.
- `source` must be "llm".
- If the talk has no discernible structure, return one chapter covering it.

Return JSON matching this schema:
{schema}

TRANSCRIPT:
{transcript}
"""

NEUTRAL_EXTRACT_PROMPT = """\
Build structured, cited knowledge from this talk for a personal knowledge base.
The subject can be anything (software, investing, management, a craft):
describe it in its own terms, not as startup advice.

Talk: {title}
Series: {series}
Chapters: {chapter_titles}

Hard rules:
- EVERY highlight, advice atom and fact MUST carry `evidence_span`: a quote from the
  transcript that supports it. If you cannot quote support for a claim, do not
  make the claim. Copy ONE contiguous passage of 10-40 words exactly as it
  appears in the transcript: keep repeated words, filler ("like", "you know")
  and caption misspellings, and do not fix grammar or stitch passages together
  with "...". A title, heading or slogan of your own is not a quote.
- Do NOT output timestamps. They are derived from the transcript afterwards.
- `advice_atoms` are single, actionable, imperative recommendations ("Add an
  index before sharding the table"), one idea each, never compound. Give each a
  short unique `atom_id` like "a01". Put the situation it is meant for in
  `applies_when` ("a read-heavy table outgrowing one machine"), or null when it
  always applies. Any highlight that tells the reader what to do must also
  appear as an advice atom. Interviews and Q&A sessions often hold
  the most concrete advice; extract it.
- `facts` are declarative statements the talk presents as true ("Asynchronous
  replication can lose acknowledged writes on failover"), one each, with a short
  unique `fact_id` like "f01". Something the speaker only recommends or believes
  is advice or a highlight, not a fact.
- `highlights` are the talk's key takeaways, paraphrased, each with its quote.
- `topics`: 1-5 short subject labels in the talk's own terms, most specific
  first ("database replication", "consistency models").
- `speaker`: only if the transcript or title makes it clear. Otherwise null.
- `summary`: about 150 words, plain prose, no bullet points.
- `entities`: the people, organizations, concepts (named ideas, methods,
  patterns), works (books, papers, standards) and tools it names.
- Set `confidence` honestly; "low" is a useful answer.
- Size: at most {max_highlights} highlights, {max_advice} advice atoms and {max_advice}
  facts (scaled to this talk's length) — the most important ones, not every point
  made. Keep each quote under 40 words.
- Record anything you could not determine in `unknowns_and_gaps` rather than
  guessing. Only a clip that is purely logistics (announcements, introductions,
  housekeeping) should return few or no atoms, and say so there.

Return JSON matching this schema:
{schema}
{feedback}
TRANSCRIPT:
{transcript}
"""

NEUTRAL_OVERVIEW_PROMPT = """\
These are section summaries and key takeaways of ONE talk, extracted section by
section. Write the talk-level fields for the whole talk.

Talk: {title}
Series: {series}

Section summaries:
{summaries}

Key takeaways:
{takeaways}

Rules: `summary` about 150 words of plain prose covering the whole talk;
`topics` 1-5 short subject labels in the talk's own terms, most specific first;
`speaker` only if clear, else null.

Return JSON matching this schema:
{schema}
"""


def prompt_hash(*parts: str) -> str:
    """Recorded on every record, so changing a prompt is detectable after the
    fact and `ytbrain invalidate extract` can be justified."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
    return h.hexdigest()[:12]


def format_transcript(utterances: list[dict], with_ms: bool = False,
                      max_words: int = 40000) -> str:
    """Render utterances for the prompt.

    `with_ms=True` is used only for chapterization, where the model must pick a
    real `start_ms`; it emits raw millisecond markers it can copy verbatim. The
    extraction prompt uses `mm:ss` for readability, since it never returns times.
    """
    out, words = [], 0
    for u in utterances:
        ms = int(u.get("start_ms", 0))
        marker = f"[{ms}]" if with_ms else f"[{ms // 60000:02d}:{ms // 1000 % 60:02d}]"
        text = u.get("text", "")
        out.append(f"{marker} {text}")
        words += len(text.split())
        if words > max_words:
            out.append("... [transcript truncated]")
            break
    return "\n".join(out)


# ---- other Source kinds (ADR-0013) --------------------------------------------------------
# One source of truth: an article's and a book chapter's prompts are the talk prompts with the
# talk-specific words replaced, so a fix to the talk prompt reaches every Source kind. tests check
# nothing talk-specific survives.
_ARTICLE_WORDS = [
    ("startup-advice talk", "startup-advice article"), ("\nTalk: ", "\nArticle: "),
    ("ONE talk", "ONE article"), ("talk-level", "article-level"),
    ("`[ms]` markers in the transcript", "`[n]` paragraph markers in the text"),
    ("Do NOT output timestamps. They are derived", "Do NOT output paragraph numbers. They are derived"),
    ("filler (\"like\", \"you know\")\n  and caption misspellings", "unusual spellings"),
    ("`speaker`: only if", "`speaker` (the author): only if"),
    ("Only a clip that is purely logistics", "Only a page that is purely logistics"),
    ("TRANSCRIPT", "TEXT"), ("transcripts", "texts"), ("transcript", "text"),
]

# A Book Chapter (ADR-0014): the Book and its authors are given, the text is the authors' prose,
# and a book quotes other people; the advice worth keeping is the authors' own.
_CHAPTER_WORDS = [
    ("startup-advice talk", "chapter of a startup book"), ("\nTalk: ", "\nChapter: "),
    ("\nSeries: ", "\nBook: "), ("\nChapters: ", "\nSections: "),
    ("ONE talk", "ONE book chapter"), ("talk-level", "chapter-level"),
    ("`[ms]` markers in the transcript", "`[p. n]` page markers in the text"),
    ("Do NOT output timestamps. They are derived", "Do NOT output page numbers. They are derived"),
    ("filler (\"like\", \"you know\")\n  and caption misspellings", "the book's spelling and punctuation"),
    ("Interviews and Q&A sessions often hold\n  the most concrete advice; extract it.",
     ("Stories and case studies often hold the\n  most concrete advice; extract the lesson the authors draw from them. A\n"
      "  quotation the authors cite from someone else (an epigraph, a famous line)\n"
      "  is not the authors' advice unless they endorse it.")),
    ("`speaker`: only if the transcript or title makes it clear. Otherwise null.",
     "`speaker`: the Book's authors as given above."),
    ("Only a clip that is purely logistics (announcements, introductions,\n  housekeeping)",
     "Only a chapter that is purely front or back matter (acknowledgments,\n  credits, notes)"),
    ("TRANSCRIPT", "TEXT"), ("transcripts", "texts"), ("transcript", "text"),
]


def _convert(text: str, words: list[tuple[str, str]], noun: str) -> str:
    import re
    fields = re.findall(r"\{[a-z_]+\}", text)             # format placeholders ({transcript}) stay as they are
    for i, f in enumerate(fields):
        text = text.replace(f, f"\x00{i}\x00", 1)
    for old, new in words:
        text = text.replace(old, new)
    text = re.sub(r"\btalk\b", noun, text)              # lowercase noun only: "Talk to ten users" stays
    for i, f in enumerate(fields):
        text = text.replace(f"\x00{i}\x00", f, 1)
    return text


def _for_article(text: str) -> str:
    return _convert(text, _ARTICLE_WORDS, "article")


def _for_chapter(text: str) -> str:
    return _convert(text, _CHAPTER_WORDS, "chapter")


_CONVERT = {"article": _for_article, "chapter": _for_chapter}


def _pages(utterances: list[dict], max_words: int) -> str:
    """A Chapter's paragraphs, with a `[p. n]` marker where each PDF page starts."""
    out, words, page = [], 0, None
    for u in utterances:
        text = u.get("text", "")
        p = int(u.get("start_ms", 0)) // 1000
        out.append(f"[p. {p}] {text}" if p != page else text)
        page = p
        words += len(text.split())
        if words > max_words:
            out.append("... [text truncated]")
            break
    return "\n".join(out)


_VARIANT_PROMPTS = {
    "startup": (CHAPTER_PROMPT, EXTRACT_PROMPT, OVERVIEW_PROMPT),
    "neutral": (NEUTRAL_CHAPTER_PROMPT, NEUTRAL_EXTRACT_PROMPT, NEUTRAL_OVERVIEW_PROMPT),
}


class _Prompts:
    def __init__(self, kind: str, variant: str = "startup"):
        conv = _CONVERT.get(kind, lambda s: s)
        chapter, extract, overview = _VARIANT_PROMPTS[variant]
        self.kind = kind
        self.variant = variant
        self.CHAPTER_PROMPT = conv(chapter)
        self.EXTRACT_PROMPT = conv(extract)
        self.REPAIR_PROMPT = REPAIR_PROMPT
        self.CONCISE_SUFFIX = CONCISE_SUFFIX
        self.GROUNDING_FEEDBACK = conv(GROUNDING_FEEDBACK)
        self.OVERVIEW_PROMPT = conv(overview)

    def format(self, utterances: list[dict], with_ms: bool = False, max_words: int = 40000) -> str:
        """Talks: `[mm:ss]` (or raw ms for chaptering); articles: `[n]` paragraph numbers;
        book chapters: `[p. n]` where each page starts."""
        if self.kind == "chapter":
            return _pages(utterances, max_words)
        if self.kind != "article":
            return format_transcript(utterances, with_ms=with_ms, max_words=max_words)
        out, words = [], 0
        for u in utterances:
            text = u.get("text", "")
            out.append(f"[{int(u.get('start_ms', 0))}] {text}")
            words += len(text.split())
            if words > max_words:
                out.append("... [text truncated]")
                break
        return "\n".join(out)

    def hash(self) -> str:
        return prompt_hash(self.EXTRACT_PROMPT, self.CHAPTER_PROMPT, self.REPAIR_PROMPT,
                           self.GROUNDING_FEEDBACK, self.OVERVIEW_PROMPT)


def for_kind(kind: str | None, variant: str = "startup") -> _Prompts:
    """The prompts for a Source kind ('talk' (default), 'article' or 'chapter') and a prompt variant
    ('startup', or the subject-neutral 'neutral', ADR-0018)."""
    return _Prompts(kind if kind in _CONVERT else "talk", variant if variant in _VARIANT_PROMPTS else "startup")
