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
`enabled: false` keeps a Source listed but stops fetching it; its Documents stay searchable
until `ytbrain drop --source <id>` (then `ytbrain index`).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Protocol

from .config import WEB_DEFAULT_DEPTH, WEB_DEFAULT_MAX_PAGES

TYPES = ("youtube_playlist", "website")
RENDER_MODES = ("auto", "always", "never")
DOC_TYPE = {"youtube_playlist": "youtube", "website": "web"}     # manifest documents.doc_type


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
    """One sources.yaml entry as a complete dict with `type`, `id`, `name` and `enabled`."""
    if not isinstance(entry, dict):
        raise SourceConfigError(f"each source must be a mapping, got {entry!r}")
    typ = entry.get("type")
    if typ is None:
        if entry.get("playlist_id") or entry.get("kind") == "playlist":
            typ = "youtube_playlist"
        elif entry.get("url"):
            typ = "website"
    if typ not in TYPES:
        raise SourceConfigError(f"source {entry.get('id') or entry.get('url') or entry}: type must be one of "
                                f"{', '.join(TYPES)} (or give playlist_id / url)")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SourceConfigError(f"source {entry.get('id') or entry.get('url')}: enabled must be true or false")
    if typ == "youtube_playlist":
        src = {**(defaults or {}), **entry, "type": typ, "enabled": enabled}
        if not src.get("playlist_id"):
            raise SourceConfigError(f"source {src.get('id')}: a youtube_playlist needs playlist_id")
        src.setdefault("id", src["playlist_id"])
        return src
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
    raise SourceConfigError(f"no adapter for Source type {typ!r}")


def adapter_for_doc_type(doc_type: str | None) -> Adapter:
    """The adapter that cleans a Document, from its manifest doc_type (youtube when unset)."""
    typ = next((t for t, d in DOC_TYPE.items() if d == (doc_type or "youtube")), None)
    if typ is None:
        raise SourceConfigError(f"no adapter for documents of type {doc_type!r}")
    return adapter(typ)
