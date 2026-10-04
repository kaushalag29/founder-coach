"""Sources: what `sources.yaml` lists, normalized, and the adapter each Source type uses (ADR-0013).

A Source type's adapter owns the two Steps that depend on where content comes from:
  sync   discover Documents and fetch their raw files into the manifest
  clean  turn one raw file into the shared transcript: numbered text units (each with a
         position and an optional heading) plus given metadata
Every later Step (extract, verify, index, pack) and the coach are the same for all types.

Entries, old and new:
  - {playlist_id: PL..., ...}             -> type youtube_playlist (the original format)
  - {kind: playlist, ...}                 -> type youtube_playlist
  - {type: website, url: https://...}     -> everything else derived: id, name, depth, render
  - {type: pdf_books, path: data/books}   -> PDF Books (ADR-0014): a folder or one file, private
`enabled: false` keeps a Source listed but stops fetching it; its Documents stay searchable
until `ytbrain drop --source <id>` (then `ytbrain index`).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Protocol

from .config import WEB_DEFAULT_DEPTH, WEB_DEFAULT_MAX_PAGES

TYPES = ("youtube_playlist", "website", "pdf_books")
RENDER_MODES = ("auto", "always", "never")
DOC_TYPE = {"youtube_playlist": "youtube", "website": "web", "pdf_books": "book"}     # manifest documents.doc_type
BOOK_FIELDS = {"title", "subtitle", "author", "authors", "year", "isbn", "publisher", "url", "chapters",
               "chapters_only", "include", "exclude", "skip", "domains"}


class SourceConfigError(ValueError):
    pass


class Adapter(Protocol):
    """What a Source type implements (ADR-0013)."""
    type: str            # the `type` in sources.yaml
    doc_type: str        # documents.doc_type in the manifest

    def sync(self, manifest, sources: list[dict], args, say=print) -> dict: ...
    def clean(self, manifest, doc_id: str, doc) -> str: ...     # 'ok' | 'skipped' | 'refetch'


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "site"


def website_id(url: str) -> str:
    """A stable, readable, unique id for a website Source: its host plus a short hash of the
    start URL (two Sources on one host stay distinct)."""
    from .web.urls import canonicalize, host_of
    canon = canonicalize(url)
    return f"{_slug(host_of(canon).removeprefix('www.'))}_{hashlib.sha256(canon.encode()).hexdigest()[:6]}"


def normalize(entry: dict, defaults: dict | None = None) -> dict:
    """One sources.yaml entry as a complete dict with `type`, `id`, `name` and `enabled`; `domains`
    (the Domains its Documents belong to, ytbrain/domains.py) is a list of names when given."""
    src = _normalize(entry, defaults)
    if "domains" in src:
        names = src["domains"]
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
            raise SourceConfigError(f"source {src['id']}: domains must be a list of Domain names")
        src["domains"] = list(dict.fromkeys(n.strip() for n in names))
    for fname, fields in (src.get("books") or {}).items():
        if "domains" in (fields or {}):
            names = fields["domains"]
            names = [names] if isinstance(names, str) else names
            if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
                raise SourceConfigError(f"source {src['id']}: books[{fname!r}].domains must be a list of Domain names")
            fields["domains"] = list(dict.fromkeys(n.strip() for n in names))
    return src


def _normalize(entry: dict, defaults: dict | None = None) -> dict:
    if not isinstance(entry, dict):
        raise SourceConfigError(f"each source must be a mapping, got {entry!r}")
    typ = entry.get("type")
    if typ is None:
        if entry.get("playlist_id") or entry.get("kind") == "playlist":
            typ = "youtube_playlist"
        elif entry.get("url"):
            typ = "website"
        elif entry.get("path"):
            typ = "pdf_books"
    if typ not in TYPES:
        raise SourceConfigError(f"source {entry.get('id') or entry.get('url') or entry}: type must be one of "
                                f"{', '.join(TYPES)} (or give playlist_id / url / path)")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SourceConfigError(f"source {entry.get('id') or entry.get('url')}: enabled must be true or false")
    if typ == "youtube_playlist":
        src = {**(defaults or {}), **entry, "type": typ, "enabled": enabled}
        if not src.get("playlist_id"):
            raise SourceConfigError(f"source {src.get('id')}: a youtube_playlist needs playlist_id")
        src.setdefault("id", src["playlist_id"])
        return src
    if typ == "pdf_books":
        return _pdf_books(entry, enabled)
    # website: only the URL is required; YouTube-only defaults (sub_langs, ...) don't apply
    url = entry.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise SourceConfigError(f"source {entry.get('id')}: a website needs url: https://...")
    from .web.urls import canonicalize, host_of
    try:
        canon = canonicalize(url)
    except ValueError as e:
        raise SourceConfigError(f"source url {url!r}: {e}") from None
    src = {"depth": WEB_DEFAULT_DEPTH, "render": "auto", "max_pages": WEB_DEFAULT_MAX_PAGES, **entry,
           "type": typ, "url": canon, "enabled": enabled}
    src.setdefault("id", website_id(canon))
    if "name" in entry:                           # a name you wrote is the series; a defaulted one isn't
        src.setdefault("series", entry["name"])
    src.setdefault("name", host_of(canon).removeprefix("www."))
    if "author" in src and not (isinstance(src["author"], str) and src["author"].strip()):
        raise SourceConfigError(f"source {src['id']}: author must be a name (text)")
    for key, lo in (("depth", 0), ("max_pages", 1)):
        if not isinstance(src[key], int) or src[key] < lo:
            raise SourceConfigError(f"source {src['id']}: {key} must be an integer >= {lo}")
    if src["render"] not in RENDER_MODES:
        raise SourceConfigError(f"source {src['id']}: render must be one of {', '.join(RENDER_MODES)}")
    return src


def _pdf_books(entry: dict, enabled: bool) -> dict:
    """Folders (every PDF under each) or single PDFs of Books you own: `path` is one of them or a
    list. Private in v1: `distribute` may only be false (ADR-0014). `books:` holds per-file
    corrections keyed by file name (docs/books-plan.md)."""
    given = entry.get("path")
    paths = [given] if isinstance(given, str) else given
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        raise SourceConfigError(f"source {entry.get('id')}: pdf_books needs path: <folder or file> "
                                f"(or a list of them)")
    if len(set(paths)) != len(paths):
        raise SourceConfigError(f"source {entry.get('id')}: a path is listed twice")
    raw = ", ".join(paths)
    if entry.get("distribute", False) is not False:
        raise SourceConfigError(f"source {entry.get('id') or raw}: books are private: distribute must be false (ADR-0014)")
    if entry.get("lookup") not in (None, "openlibrary"):
        raise SourceConfigError(f"source {entry.get('id') or raw}: lookup must be openlibrary or left out")
    if entry.get("parser", "pdfium") not in ("pdfium", "docling"):
        raise SourceConfigError(f"source {entry.get('id') or raw}: parser must be pdfium or docling")
    books = entry.get("books") or {}
    if not isinstance(books, dict):
        raise SourceConfigError(f"source {entry.get('id') or raw}: books must map a file name to its fields")
    for name, fields in books.items():
        unknown = set(fields or {}) - BOOK_FIELDS
        if not isinstance(fields, dict) or unknown:
            raise SourceConfigError(f"source {entry.get('id') or raw}: books[{name!r}] has unknown fields "
                                    f"{sorted(unknown) if isinstance(fields, dict) else fields!r}; "
                                    f"allowed: {', '.join(sorted(BOOK_FIELDS))}")
        for row in fields.get("chapters") or []:
            if not isinstance(row, dict) or "page" not in row:
                raise SourceConfigError(f"source {entry.get('id') or raw}: books[{name!r}].chapters entries need a page")
    src = {"parser": "pdfium", "lookup": None, **entry, "type": "pdf_books", "enabled": enabled,
           "distribute": False, "books": books, "path": raw, "paths": list(paths)}
    first = Path(paths[0])
    src.setdefault("id", f"books_{_slug(first.stem or first.name)}")
    src.setdefault("name", first.name if len(paths) == 1 else f"{first.name} and {len(paths) - 1} more")
    return src


def _skipped_dir(rel: Path, base: Path) -> bool:
    """Folders under a books folder that never hold your Books: the pipeline's own caches
    (parsed, plans, openlibrary) and hidden or underscore folders (`.trash`, `_to_delete`)."""
    from .config import BOOKS_LOOKUPS, BOOKS_PARSED, BOOKS_PLANS
    caches = {c.resolve() for c in (BOOKS_PARSED, BOOKS_PLANS, BOOKS_LOOKUPS)}
    d = base
    for part in rel.parts:
        d = d / part
        if part.startswith((".", "_")) or d.resolve() in caches:
            return True
    return False


def book_files(src: dict, root: Path) -> list[Path]:
    """The PDFs of a pdf_books Source, sorted: every listed folder (recursively, so a new subfolder
    such as data/books/finance is picked up by itself) and file, never the caches. A relative
    path is under the repo (YTBRAIN_ROOT). Per-book corrections are keyed by file name, so two
    PDFs with the same name in one Source are refused."""
    found: dict[Path, None] = {}
    for raw in src.get("paths") or [src["path"]]:
        p = Path(raw).expanduser()
        p = p if p.is_absolute() else root / p
        if p.is_file():
            files = [p] if p.suffix.lower() == ".pdf" else []
        else:
            files = sorted(q for q in p.rglob("*") if q.is_file() and q.suffix.lower() == ".pdf"
                           and not _skipped_dir(q.relative_to(p).parent, p)) if p.is_dir() else []
        for f in files:
            found.setdefault(f.resolve(), None)
    names: dict[str, Path] = {}
    for f in found:
        if f.name in names:
            raise SourceConfigError(f"source {src.get('id')}: two PDFs named {f.name!r} ({names[f.name].parent} and "
                                    f"{f.parent}); rename one (corrections are keyed by file name)")
        names[f.name] = f
    return sorted(found, key=lambda f: (f.name.lower(), str(f)))


def load(path: Path) -> list[dict]:
    """Every Source in sources.yaml, normalized (enabled or not); ids are unique."""
    import yaml
    doc = yaml.safe_load(path.read_text()) or {}
    defaults = doc.get("defaults") or {}
    out, seen = [], set()
    for entry in doc.get("sources") or []:
        src = normalize(entry, defaults)
        if src["id"] in seen:
            raise SourceConfigError(f"two sources share the id {src['id']!r}; give one an explicit id")
        seen.add(src["id"])
        out.append(src)
    return out


def enabled(sources: list[dict], typ: str | None = None) -> list[dict]:
    return [s for s in sources if s["enabled"] and (typ is None or s["type"] == typ)]


class YouTubeAdapter:
    """The original pipeline behind the adapter seam, unchanged: playlist enumeration and
    caption fetch with yt-dlp (sync), srt -> deduplicated utterances (clean)."""
    type, doc_type = "youtube_playlist", "youtube"

    def sync(self, manifest, sources: list[dict], args, say=print) -> dict:
        from .cli import _sync_youtube
        return _sync_youtube(manifest, sources, args)

    def clean(self, manifest, doc_id: str, doc) -> str:
        from .cli import _clean_one
        return _clean_one(manifest, doc_id, doc)


def adapter(typ: str) -> Adapter:
    """The adapter for a Source type."""
    if typ == "youtube_playlist":
        return YouTubeAdapter()
    if typ == "website":
        from .web.crawl import WebsiteAdapter
        return WebsiteAdapter()
    if typ == "pdf_books":
        from .books.adapter import BookAdapter
        return BookAdapter()
    raise SourceConfigError(f"no adapter for Source type {typ!r}")


def adapter_for_doc_type(doc_type: str | None) -> Adapter:
    """The adapter that cleans a Document, from its manifest doc_type (youtube when unset)."""
    typ = next((t for t, d in DOC_TYPE.items() if d == (doc_type or "youtube")), None)
    if typ is None:
        raise SourceConfigError(f"no adapter for documents of type {doc_type!r}")
    return adapter(typ)
