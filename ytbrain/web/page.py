"""What a web page is, without knowing the site (plan D4, D9).

Metadata comes from what the page declares about itself, in order: schema.org JSON-LD,
OpenGraph and meta tags, `rel=canonical`, `<html lang>`; trafilatura (2.2) supplies the main
text (paragraphs and headings, without navigation, footers and ads) and fills gaps in the
metadata. The page type decides what the pipeline does with it:
  article  enough main text                     -> ingested (clean, extract, verify, index)
  video    an embedded talk and little text     -> the video ids go to YouTube sync
  listing  mostly links                         -> its links are followed, nothing ingested
  other    anything else                        -> skipped
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from ..config import WEB_MIN_ARTICLE_WORDS
from .urls import absolute

ARTICLE_TYPES = {"article", "blogposting", "newsarticle", "techarticle", "report", "scholarlyarticle",
                 "socialmediaposting", "analysisnewsarticle", "opinionnewsarticle", "howto", "faqpage"}
YOUTUBE_ID = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?(?:.*&)?v=|shorts/|live/)|youtu\.be/)"
                        r"([A-Za-z0-9_-]{11})")
LISTING_MIN_LINKS = 10
# a transcript section: an HTML heading, or Markdown inside a page's JSON data ("## Transcript\\n")
TRANSCRIPT_MARK = re.compile(r"(?i)(?:>\s*|#{1,6}\s*)transcript\s*(?:<|\\n|\n|:)")
MAX_LINK_DENSITY = 0.5               # an "article" whose text is mostly link text is a listing


@dataclass
class Page:
    url: str                          # the URL it was fetched from (canonical form)
    canonical: str | None = None      # rel=canonical / og:url, canonicalized, when declared
    title: str | None = None
    author: str | None = None
    published_at: str | None = None   # YYYY-MM-DD when known
    site_name: str | None = None
    language: str | None = None       # primary subtag, e.g. "en"
    description: str | None = None
    page_type: str = "other"
    units: list[dict] = field(default_factory=list)      # [{text, heading}] in reading order
    links: list[str] = field(default_factory=list)       # canonical absolute links, in order
    video_ids: list[str] = field(default_factory=list)
    words: int = 0
    link_density: float = 0.0

    @property
    def text_hash(self) -> str:
        """Fingerprint of the main text: same text at another URL = the same Document (D5)."""
        norm = "\n".join(" ".join(u["text"].lower().split()) for u in self.units)
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()

    def given_metadata(self) -> dict:
        return {"url": self.url, "canonical": self.canonical, "title": self.title, "author": self.author,
                "published_at": self.published_at, "site_name": self.site_name, "language": self.language,
                "description": self.description, "page_type": self.page_type, "words": self.words}


def _date(value) -> str | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or "").strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december")
_BYLINE = re.compile(r"^(" + "|".join(_MONTHS) + r")\s+(?:(\d{1,2}),\s+)?((?:19|20)\d\d)(?!\d)", re.I)   # may be glued: "March 2012One..."


def _byline_date(units: list[dict]) -> str | None:
    """A 'July 2023' / 'July 5, 2023' byline opening one of the first paragraphs (essays and blogs
    without date metadata, like paulgraham.com). trafilatura's own guess is often year-only."""
    for u in units[:3]:
        m = _BYLINE.match(u["text"])
        if m:
            month = _MONTHS.index(m.group(1).lower()) + 1
            return f"{m.group(3)}-{month:02d}-{int(m.group(2) or 1):02d}"
    return None


UNWRAPPED_LINE_WORDS = 40                          # no wrapped source line is this long


def _lines(el) -> list[str]:
    """An element's text split at line breaks (<lb/>, from <br>). Sites that write paragraphs as
    `text<br><br>text` (paulgraham.com) arrive as one <p>; each piece is its own paragraph."""
    parts = [""]

    def walk(node):
        if node.tag == "lb":
            parts.append("")
        else:
            parts[-1] += node.text or ""
            for child in node:
                walk(child)
        if node is not el:
            parts[-1] += node.tail or ""

    walk(el)
    lines = parts[0].split("\n") if len(parts) == 1 else []
    if len(lines) > 1 and max(len(s.split()) for s in lines) >= UNWRAPPED_LINE_WORDS:
        # trafilatura's fallback for table layouts returns the whole page as one <p> with a newline
        # at each paragraph break. Source line wraps never reach this many words, so here the
        # newlines are paragraph breaks, not wraps.
        parts = lines
    return [s for s in (" ".join(p.split()) for p in parts) if s]


def _names(value) -> str | None:
    """JSON-LD author: a string, a Person/Organization, or a list of them."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _names(value.get("name"))
    if isinstance(value, list):
        names = [n for n in (_names(v) for v in value) if n]
        return ", ".join(dict.fromkeys(names)) or None
    return None


def _jsonld(tree) -> list[dict]:
    out = []
    for s in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(s.text_content() or "")
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop(0)
            if isinstance(obj, dict):
                out.append(obj)
                graph = obj.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(obj, list):
                stack.extend(obj)
    return out


def _types(obj: dict) -> set[str]:
    t = obj.get("@type")
    return {str(x).lower() for x in (t if isinstance(t, list) else [t]) if x}


def _meta(tree, *keys: str) -> str | None:
    for k in keys:
        for attr in ("property", "name", "itemprop"):
            vals = tree.xpath(f'//meta[@{attr}="{k}"]/@content')
            if vals and vals[0].strip():
                return vals[0].strip()
    return None


def _lang(value: str | None) -> str | None:
    if not value:
        return None
    return re.split(r"[-_]", value.strip().lower())[0] or None


_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


def parse(html: str, url: str) -> Page:
    """Everything the crawler and `clean` need from one page's HTML. Deterministic."""
    import lxml.html
    import trafilatura

    page = Page(url=url)
    html = _XML_DECL.sub("", html or "", count=1)   # lxml refuses a str that declares its encoding (XHTML)
    try:
        tree = lxml.html.fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return page
    base = (tree.xpath("//base/@href") or [url])[0]

    # --- declared metadata: JSON-LD, then OpenGraph / meta, then tags
    ld = _jsonld(tree)
    art = next((o for o in ld if _types(o) & ARTICLE_TYPES), None) or {}
    videos_ld = [o for o in ld if "videoobject" in _types(o)]
    canon = (tree.xpath('//link[@rel="canonical"]/@href') or [None])[0] or _meta(tree, "og:url")
    page.canonical = absolute(base, canon) if canon else None
    page.title = (str(art.get("headline") or art.get("name") or "").strip() or _meta(tree, "og:title")
                  or (tree.findtext(".//title") or "").strip() or None)
    page.author = _names(art.get("author")) or _meta(tree, "author", "article:author", "parsely-author")
    if page.author and page.author.startswith("http"):
        page.author = None                    # article:author is often a profile URL, not a name
    page.published_at = _date(art.get("datePublished") or art.get("dateCreated")
                              or _meta(tree, "article:published_time", "datePublished", "date", "pubdate",
                                       "dc.date", "DC.date.issued")
                              or (tree.xpath("//time/@datetime") or [None])[0])
    page.site_name = _meta(tree, "og:site_name", "application-name") or _names(art.get("publisher"))
    page.language = _lang(tree.get("lang") or art.get("inLanguage") or _meta(tree, "og:locale", "language"))
    page.description = _meta(tree, "description", "og:description")

    # --- main text: trafilatura keeps paragraphs, list items, quotes and headings in order
    doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_formatting=True,
                                      include_comments=False, include_tables=False, favor_recall=True)
    if doc is not None:
        heading = None
        body = getattr(doc, "body", None)
        for el in (body.iter() if body is not None else []):
            tag = el.tag if isinstance(el.tag, str) else ""
            text = " ".join("".join(el.itertext()).split())
            if not text:
                continue
            if tag == "head":
                heading = text
            elif tag in ("p", "item", "quote") and not (tag == "p" and el.getparent() is not None
                                                        and el.getparent().tag in ("item", "quote")):
                for part in _lines(el):                # <br>-separated paragraphs are separate units
                    page.units.append({"text": part, "heading": heading})
        page.title = page.title or getattr(doc, "title", None)
        page.author = page.author or getattr(doc, "author", None)
        page.published_at = page.published_at or _byline_date(page.units) or _date(getattr(doc, "date", None))
        page.site_name = page.site_name or getattr(doc, "sitename", None)
        page.language = page.language or _lang(getattr(doc, "language", None))
    page.words = sum(len(u["text"].split()) for u in page.units)

    # --- links and embedded talks
    seen = set()
    for href in tree.xpath("//a/@href"):
        u = absolute(base, href)
        if u and u not in seen:
            seen.add(u)
            page.links.append(u)
    srcs = tree.xpath("//iframe/@src") + tree.xpath("//embed/@src") + tree.xpath('//meta[@property="og:video"]/@content') \
        + tree.xpath('//meta[@property="og:video:url"]/@content')
    for o in videos_ld:
        srcs += [str(o.get(k) or "") for k in ("embedUrl", "contentUrl", "url")]
    ids = []
    for s in srcs:
        m = YOUTUBE_ID.search(s or "")
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    page.video_ids = ids

    body_el = tree.find(".//body")
    all_text = len(" ".join(body_el.text_content().split())) if body_el is not None else 0
    link_text = sum(len(" ".join(a.text_content().split())) for a in tree.xpath("//body//a"))
    page.link_density = round(link_text / all_text, 3) if all_text else 0.0

    if page.video_ids and TRANSCRIPT_MARK.search(html):
        page.page_type = "video"          # the talk and its transcript: ingested once, from YouTube
    elif page.words >= WEB_MIN_ARTICLE_WORDS and page.link_density < MAX_LINK_DENSITY:
        page.page_type = "article"
    elif page.video_ids:
        page.page_type = "video"
    elif len(page.links) >= LISTING_MIN_LINKS:
        page.page_type = "listing"
    else:
        page.page_type = "other"
    return page
