"""The website adapter (ADR-0013): `sync` crawls each enabled website Source politely into the
manifest; `clean` turns a saved page into the shared paragraph transcript.

sync, per Source (docs/web-sources-plan.md):
  1. robots.txt for the host; 5xx/unreachable -> nothing fetched this run (RFC 9309)
  2. the queue: the start page, sitemap URLs in scope, failed pages and pages due a re-check
  3. per URL, in depth order: robots check -> polite GET (conditional for known pages) ->
     render with Playwright when the text is thin (render: auto) -> page type ->
       article  saved as a Document (id from the canonical URL; same text elsewhere = alias)
       video    its YouTube ids become Documents for YouTube sync
       listing  links followed, nothing saved
     links in scope go in the queue at depth+1 while depth < the Source's `depth`
  4. 404/410 on a known Document tombstones it; failures retry next run; a host that keeps
     failing is paused for the run
Everything is in SQLite as it happens, so an interrupted crawl resumes where it stopped.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

from .. import pages
from ..config import (MIN_TRANSCRIPT_WORDS, WEB_HOST_FAILURE_CAP, WEB_LANGUAGES, WEB_MAX_SITEMAPS,
                      WEB_MIN_ARTICLE_WORDS, WEB_RAW, WEB_RECHECK_DAYS, WEB_URL_FAILURE_CAP)
from ..manifest import StageState
from . import urls
from .state import REOPENED, WebState, days_ago, now

HTML_TYPES = ("text/html", "application/xhtml+xml")
SITEMAP_MAX_BYTES = 50 * 2**20       # the sitemaps protocol's own limit (uncompressed)
HOST_FAILURES = {401, 403, 429}          # also count toward pausing the host (blocked, throttled)


def _dur(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def raw_path(canonical: str, doc_id: str) -> Path:
    return WEB_RAW / urls.site_host(canonical).replace(":", "_") / f"{doc_id}.html"


LISTING_PATH = re.compile(r"/(?:archives?|categor(?:y|ies)|tags?|topics?|authors?|search|feed|page/\d+)(?:/|$)", re.I)


def uncaptioned(m, video_ids: list[str]) -> bool:
    """Every talk on the page ended without YouTube captions (private, removed, none in English)."""
    return bool(video_ids) and m is not None and all(m.stage_status(v, "fetch") == "skipped" for v in video_ids)


def page_kind(page, url: str, src: dict, m=None) -> str:
    """The page's type for this Source: a start page crawled with depth >= 1 is the Source's
    index (its links are the point), whatever its word count; a video page whose talks YouTube
    has no captions for is an article (its transcript is the only copy)."""
    if page.page_type == "video" and uncaptioned(m, page.video_ids) and page.words >= WEB_MIN_ARTICLE_WORDS:
        return "article"
    start = (src or {}).get("url")
    if page.page_type == "article" and start and (src.get("depth") or 0) >= 1 \
            and url.rstrip("/") == start.rstrip("/"):
        return "listing"
    if page.page_type == "article" and LISTING_PATH.search(urlsplit(url).path):
        return "listing"                       # archive / category / tag pages: post excerpts
    return page.page_type


def _is_sitemap_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith((".xml", ".xml.gz"))


def _saved_path(row, doc_id: str) -> Path | None:
    """Where a saved page is: stored relative to data/raw/web (absolute in rows from before
    2026-09-26); if that file isn't there, where it would be saved today."""
    if row is None:
        return None
    stored = Path(row["raw_path"]) if row["raw_path"] else None
    if stored is not None:
        stored = stored if stored.is_absolute() else WEB_RAW / stored
        if stored.exists():
            return stored
    try:
        return raw_path(urls.canonicalize(row["url"]), doc_id)
    except ValueError:
        return stored


def _write_raw(path: Path, html: str, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pages.atomic_write_text(path, html)
    pages.atomic_write_text(path.with_suffix(".meta.json"), json.dumps(meta, indent=1, ensure_ascii=False))


def parse_sitemap(xml_text: str) -> tuple[list[tuple[str, str | None]], list[str]]:
    """(page URLs with lastmod, child sitemap URLs) from a urlset or sitemapindex."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return [], []
    tag = lambda e: e.tag.rsplit("}", 1)[-1]
    pages_, children = [], []
    for node in root:
        loc = next((c.text.strip() for c in node if tag(c) == "loc" and c.text), None)
        lastmod = next((c.text.strip() for c in node if tag(c) == "lastmod" and c.text), None)
        if not loc:
            continue
        (children.append(loc) if tag(node) == "sitemap" else pages_.append((loc, lastmod)))
    return pages_, children


class WebsiteAdapter:
    type, doc_type = "website", "web"

    def __init__(self, fetcher=None, renderer=None, clock=time.time, say=print, sources=None):
        self._fetcher, self._renderer, self.clock, self.say = fetcher, renderer, clock, say
        self._sources = sources                  # None: read sources.yaml when clean needs it

    def _source(self, source_id: str) -> dict:
        """The Source's config (for its series and author fallback); {} if it's gone or unreadable."""
        if self._sources is None:
            from .. import sources as S
            from ..cli import SOURCES
            try:
                self._sources = S.load(SOURCES) if SOURCES.exists() else []
            except S.SourceConfigError:
                self._sources = []
        return next((s for s in self._sources if s.get("id") == source_id), {})

    # ------------------------------------------------------------------ sync
    def sync(self, m, sources: list[dict], args, say=None) -> dict:
        say = say or self.say
        try:
            import trafilatura  # noqa: F401
        except ImportError:
            say('sync: websites need the web extra: uv pip install -e ".[web]"  (skipping website sources)')
            return {"code": 1, "videos": 0}
        from .http import Fetcher
        from .render import Renderer
        fetcher = self._fetcher or Fetcher(say=lambda msg: say(f"      ({msg})"))
        renderer = self._renderer or Renderer()
        st = WebState(m.db)
        total = {"videos": 0, "code": 0}
        try:
            for i, src in enumerate(sources, 1):
                res = self._sync_source(m, st, fetcher, renderer, src, args, f"[{i}/{len(sources)}] {src['id']}", say)
                total["videos"] += res.get("videos", 0)
                total["code"] = total["code"] or res.get("code", 0)
        finally:
            if self._renderer is None:
                renderer.close()
            if self._fetcher is None:
                fetcher.close()
        return total

    def _sync_source(self, m, st, fetcher, renderer, src, args, head, say) -> dict:
        from .http import fetch_robots
        start = src["url"]
        origin = f"{urlsplit(start).scheme}://{urlsplit(start).netloc}"
        robots = fetch_robots(fetcher, origin)
        if robots.note:
            say(f"{head}: {robots.note}")
        if not robots.reachable:
            return {"code": 1}
        delay = robots.crawl_delay()
        if delay:
            fetcher.pacer.set_interval(urls.host_of(start), delay)
            say(f"{head}: robots.txt asks for {delay:g}s between requests")
        cap = int(getattr(args, "limit", 0) or src.get("max_pages") or 0) or None
        run = st.start_run(src["id"], days_ago(WEB_RECHECK_DAYS, self.clock),
                           retry_parked=bool(getattr(args, "force", False)), depth=src["depth"])
        st.enqueue(src["id"], start, 0)
        st.requeue(src["id"], start) if getattr(args, "force", False) else None
        n_sitemap = self._seed_sitemaps(st, fetcher, robots, src, origin)
        counts = st.counts(src["id"])
        deeper = f", {run['deeper']} reopened for the new depth" if run.get("deeper") else ""
        say(f"{head}: {counts.get('queued', 0)} to fetch ({n_sitemap} from sitemaps, {run['recheck']} re-checks, "
            f"{run['retry']} retries{deeper}), depth {src['depth']}, render {src['render']}"
            + (f", at most {cap} this run" if cap else ""))
        tally = {"article": 0, "unchanged": 0, "listing": 0, "video": 0, "skipped": 0, "failed": 0, "gone": 0,
                 "alias": 0}
        fetched, host_fails, videos, t0 = 0, 0, 0, time.time()
        warned_render = False
        while cap is None or fetched < cap:
            row = st.next(src["id"])
            if row is None:
                break
            url, depth = row["url"], row["depth"]
            fresh = (row["reason"] or "").startswith(REOPENED)   # needs its body for links: no 304
            fetched += 1
            print(f"    ({fetched}) {url[:90]} ... ", end="", flush=True)
            try:
                outcome, note, new_videos = self._one(m, st, fetcher, renderer, robots, src, url, depth, fresh)
            except Exception as e:                    # noqa: BLE001 -- one bad page never stops the crawl
                outcome, note, new_videos = "failed", f"{type(e).__name__}: {str(e)[:120]}", 0
                st.fail(src["id"], url, note, WEB_URL_FAILURE_CAP)
            print(f"{outcome}" + (f" ({note})" if note else ""), flush=True)
            if renderer.unavailable and not warned_render and src["render"] != "never":
                say(f"    note: {renderer.unavailable}")
                warned_render = True
            tally[outcome] = tally.get(outcome, 0) + 1
            videos += new_videos
            host_fails = host_fails + 1 if outcome == "failed" or note.startswith("blocked") else 0
            if host_fails >= WEB_HOST_FAILURE_CAP:
                say(f"{head}: {host_fails} pages in a row failed -- pausing this host until the next run")
                break
            if fetched % 10 == 0:
                left = st.counts(src["id"]).get("queued", 0)
                if cap:
                    left = min(left, cap - fetched)
                per = (time.time() - t0) / fetched
                scope = " this run" if cap else ""
                say(f"    {head}: {fetched} done, {left} to go{scope} · {per:.1f}s/page · ETA {_dur(per * left)}")
        left = st.counts(src["id"]).get("queued", 0)
        say(f"{head}: {tally['article']} article(s) saved, {tally['unchanged']} unchanged, {tally['alias']} moved, "
            f"{tally['listing']} listing(s), {tally['video']} video page(s) ({videos} new video(s)), "
            f"{tally['gone']} gone, {tally['skipped']} skipped, {tally['failed']} failed"
            + (f"; {left} still queued (next run continues)" if left else ""))
        return {"videos": videos, "code": 0}

    def _seed_sitemaps(self, st, fetcher, robots, src, origin) -> int:
        todo = robots.sitemaps() or [origin + "/sitemap.xml"]
        seen, added = set(), 0
        while todo and len(seen) < WEB_MAX_SITEMAPS:
            sm = todo.pop(0)
            if sm in seen or not robots.allowed(sm):
                continue
            seen.add(sm)
            r = fetcher.get(sm, max_bytes=SITEMAP_MAX_BYTES)
            if not r.ok:
                continue
            text = r.text
            if r.body[:2] == b"\x1f\x8b":                  # a gzipped sitemap (.xml.gz) the server didn't decode
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(r.body)) as gz:
                        raw = gz.read(SITEMAP_MAX_BYTES + 1)   # capped: a small file can inflate without limit
                except (OSError, EOFError):
                    continue
                if len(raw) > SITEMAP_MAX_BYTES:
                    continue
                text = raw.decode("utf-8", "replace")
            found, children = parse_sitemap(text)
            for c in children:                             # <loc> may be relative on real sites
                c = urls.absolute(sm, c)
                if c and urls.site_host(c) == urls.site_host(src["url"]):
                    todo.append(c)
            for loc, lastmod in found:
                u = urls.absolute(sm, loc)
                if u and _is_sitemap_url(u):              # some sites list child sitemaps as <url>s
                    if urls.site_host(u) == urls.site_host(src["url"]):
                        todo.append(u)
                    continue
                if u and urls.in_scope(u, src["url"]) and src["depth"] >= 1:
                    added += st.enqueue(src["id"], u, 1, lastmod)
        return added

    def _one(self, m, st, fetcher, renderer, robots, src, url, depth, fresh=False) -> tuple[str, str, int]:
        """Fetch and route one URL; returns (outcome, note, new videos)."""
        sid = src["id"]
        if not robots.allowed(url):
            st.finish(sid, url, "skipped", "robots.txt disallows it")
            return "skipped", "robots.txt", 0
        known = st.page_for_url(url)
        # only a leaf page (at the depth limit) is re-checked conditionally: a 304 has no body, and
        # a page whose links are followed must be read in full or new links are never found
        cond = known if known and not fresh and depth >= src["depth"] else None
        r = fetcher.get(url, etag=cond["etag"] if cond else None,
                        last_modified=cond["last_modified"] if cond else None)
        if r.status == 304 and known:
            st.checked(known["doc_id"])
            st.finish(sid, url, "done", "not modified")
            return "unchanged", "304", 0
        if r.status in (404, 410):
            return self._gone(m, st, sid, url, known, r.status)
        if not r.ok:
            reason = r.error or f"HTTP {r.status}"
            if r.status and r.status < 500 and r.status not in (408, 425, 429):
                st.finish(sid, url, "skipped", reason)
                return "skipped", ("blocked: " if r.status in HOST_FAILURES else "") + reason, 0
            status = st.fail(sid, url, reason, WEB_URL_FAILURE_CAP)
            return "failed", reason + (" -- parked" if status == "parked" else " -- retried next run"), 0
        final = urls.canonicalize(r.url)
        if not urls.in_scope(final, src["url"]):
            st.finish(sid, url, "skipped", f"redirected off-site to {urls.host_of(final)}")
            return "skipped", "redirected off-site", 0
        if r.content_type and r.content_type not in HTML_TYPES:
            st.finish(sid, url, "skipped", f"not a web page ({r.content_type})")
            return "skipped", r.content_type, 0

        from .page import parse
        html, page, rendered = r.text, parse(r.text, final), False
        mode = src["render"]
        if mode == "always" or (mode == "auto" and page.words < WEB_MIN_ARTICLE_WORDS):
            if renderer.available():
                from .render import RenderError
                try:
                    html2, final2, status2 = renderer.render(final)
                    page2 = parse(html2, urls.canonicalize(final2))
                    if page2.words >= page.words or mode == "always":
                        html, page, rendered = html2, page2, True
                except RenderError as e:
                    if page.words == 0:
                        status = st.fail(sid, url, f"render: {e}", WEB_URL_FAILURE_CAP)
                        return "failed", f"render: {e}", 0

        page.page_type = page_kind(page, final if depth else src["url"], src, m)
        if page.video_ids:
            st.record_videos(sid, url, page.video_ids)
        # links: follow within scope, one hop deeper, while the Source's depth allows
        if depth < src["depth"]:
            for link in page.links:
                if urls.in_scope(link, src["url"]):
                    st.enqueue(sid, link, depth + 1)
        videos = self._route_videos(m, src, page.video_ids)

        if page.page_type != "article":
            label = {"video": "video", "listing": "listing"}.get(page.page_type, "skipped")
            st.finish(sid, url, "done" if label != "skipped" else "skipped",
                      f"{page.page_type} ({page.words} words)")
            return label, f"{page.words} words, {len(page.links)} links", videos
        if page.language and page.language not in WEB_LANGUAGES:
            st.finish(sid, url, "skipped", f"language: {page.language}")
            return "skipped", f"language {page.language}", videos
        return self._save_article(m, st, src, url, final, page, html, r, rendered) + (videos,)

    def _route_videos(self, m, src, video_ids) -> int:
        new = 0
        for vid in video_ids:
            if m.get_document(vid) is None:
                m.upsert_document(vid, src["id"], doc_type="youtube", url=f"https://www.youtube.com/watch?v={vid}",
                                  series=src.get("series") or src.get("name"))
                new += 1
        return new

    def _gone(self, m, st, sid, url, known, status) -> tuple[str, str, int]:
        if known:
            doc = m.get_document(known["doc_id"])
            if doc is not None and doc["url"] == url:          # not moved elsewhere: really gone
                m.tombstone([known["doc_id"]])
                st.finish(sid, url, "skipped", f"gone (HTTP {status})")
                return "gone", f"HTTP {status}: {known['doc_id']} leaves the index at the next `index`", 0
        st.finish(sid, url, "skipped", f"gone (HTTP {status})")
        return "skipped", f"HTTP {status}", 0

    def _save_article(self, m, st, src, url, final, page, html, r, rendered) -> tuple[str, str]:
        sid = src["id"]
        canonical = page.canonical if page.canonical and urls.in_scope(page.canonical, src["url"]) else final
        did, uhash, thash = urls.doc_id(canonical), urls.url_hash(canonical), page.text_hash
        existing = st.page(did)
        if existing is not None and existing["url_hash"] != uhash:
            st.finish(sid, url, "skipped", f"id collision with {existing['url']}")
            return "skipped", f"id {did} already belongs to {existing['url']} (refused, not merged)"
        if existing is None:
            other = st.page_with_text(thash, did)
            if other is not None:                         # the same text elsewhere: a moved page (D5)
                st.alias(canonical, other["doc_id"])
                if url != canonical:
                    st.alias(url, other["doc_id"])
                m.upsert_document(other["doc_id"], sid, url=canonical)
                m.restore(other["doc_id"])
                st.save_page(**{**dict(other), "checked_at": now()})
                st.finish(sid, url, "done", f"alias of {other['doc_id']}")
                return "alias", f"same text as {other['doc_id']} (no new Document)"
        if url != canonical:
            st.alias(url, did)
        path = raw_path(canonical, did)
        meta = {**page.given_metadata(), "doc_id": did, "canonical": canonical, "fetched_url": url,
                "final_url": final, "rendered": rendered, "etag": r.headers.get("etag"),
                "last_modified": r.headers.get("last-modified"), "fetched_at": now(), "source_id": sid,
                "text_hash": thash}
        _write_raw(path, html, meta)
        changed = existing is not None and existing["text_hash"] != thash
        came_back = m.restore(did)
        m.upsert_document(did, sid, doc_type="web", url=canonical, title=page.title,
                          series=src.get("series") or page.site_name or src.get("name"),
                          provenance=page.site_name or src.get("name"), published_at=page.published_at,
                          has_captions=0, caption_kind="none")
        st.save_page(doc_id=did, source_id=sid, url=canonical, url_hash=uhash, text_hash=thash,
                     etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"),
                     fetched_at=now(), checked_at=now(), rendered=int(rendered),
                     raw_path=path.relative_to(WEB_RAW).as_posix())
        if (changed or came_back) and m.stage_status(did, "clean") is not None:
            m.invalidate_docs("clean", [did], "page changed" if changed else "page is back")
        m.mark(StageState(did, "fetch", "ok", input_hash=uhash[:16], output_hash=thash[:16], attempts=1))
        m.succeeded(did, "fetch")
        st.finish(sid, url, "done", "article")
        if existing is not None and not changed and not came_back:
            return "unchanged", "same text"
        return "article", (f"{page.words} words" + (", rendered" if rendered else "")
                           + (", changed: re-extract queued" if changed else ""))

    # ------------------------------------------------------------------ clean
    def clean(self, m, doc_id: str, doc) -> str:
        """Saved page -> paragraph transcript (the shared text-units shape, ADR-0013)."""
        from .page import parse
        st = WebState(m.db)
        row = st.page(doc_id)
        path = _saved_path(row, doc_id)
        if path is None or not path.exists():
            m.mark(StageState(doc_id, "fetch", "stale", error="saved page missing on disk"))
            if row is not None:
                st.requeue(row["source_id"], row["url"])
            print("PAGE FILE MISSING -- re-fetched on next sync", flush=True)
            return "refetch"
        html = path.read_text(encoding="utf-8")
        page = parse(html, row["url"])
        words = page.words
        h = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
        kind = page_kind(page, row["url"], self._source(row["source_id"]), m)
        if kind != "article":                      # saved before a rule that now says otherwise
            why = {"video": "a video page: the talk comes from YouTube",
                   "listing": "the Source's index page"}.get(kind, kind)
            if m.stage_status(doc_id, "extract") is not None:
                m.tombstone([doc_id])              # already extracted: its items leave at the next index
            m.mark(StageState(doc_id, "clean", "skipped", input_hash=h, error=f"not an article ({why})"))
            print(f"NOT AN ARTICLE ({why}) -- not extracted", flush=True)
            return "skipped"
        if words < MIN_TRANSCRIPT_WORDS:
            m.mark(StageState(doc_id, "clean", "skipped", input_hash=h, error=f"article too short ({words} words)"))
            print(f"TOO SHORT ({words} words) -- not extracted", flush=True)
            return "skipped"
        units, chapters, heading = [], [], object()
        for n, u in enumerate(page.units, 1):
            units.append({"text": u["text"], "start_ms": n, "end_ms": n})
            if u["heading"] and u["heading"] != heading:
                chapters.append({"chapter_id": f"ch{len(chapters) + 1:02d}", "title": u["heading"][:120],
                                 "start_ms": n, "source": "uploader"})
            heading = u["heading"]
        for i, ch in enumerate(chapters):
            ch["end_ms"] = chapters[i + 1]["start_ms"] if i + 1 < len(chapters) else len(units)
        src = self._source(row["source_id"])
        series = doc["series"] if doc else None
        fix = {}
        if src.get("series") and src["series"] != series:     # the Source was renamed in sources.yaml
            series = fix["series"] = src["series"]
        published = page.published_at or (doc["published_at"] if doc else None)
        if doc and published != doc["published_at"]:          # a better parser re-reads the saved page
            fix["published_at"] = published
        if fix:
            m.upsert_document(doc_id, row["source_id"], **fix)
        rec = {"doc_id": doc_id, "title": (doc["title"] if doc else None) or page.title,
               "series": series, "published_at": published, "caption_kind": "none", "source_kind": "article", "locator": "paragraph",
               "url": doc["url"] if doc else row["url"], "speaker": page.author or src.get("author"),
               "provenance": doc["provenance"] if doc else page.site_name, "language": page.language,
               "chapters": chapters, "quality": {"words": words, "paragraphs": len(units)}, "utterances": units}
        body = pages.canonical_json(rec)
        out_hash = hashlib.sha256(body.encode()).hexdigest()[:16]
        prev = m.stage(doc_id, "clean")
        changed = prev is not None and prev["output_hash"] and prev["output_hash"] != out_hash
        if changed and m.stage_status(doc_id, "extract") is not None:
            m.mark(StageState(doc_id, "extract", "stale", error="article changed"))
        from ..config import TRANSCRIPTS
        pages.atomic_write_text(TRANSCRIPTS / f"{doc_id}.json", body)
        m.mark(StageState(doc_id, "clean", "ok", input_hash=h, output_hash=out_hash, attempts=1))
        m.succeeded(doc_id, "clean")
        print(f"article · {len(units)} paragraphs · {words} words"
              + (" · changed: re-extract queued" if changed else ""), flush=True)
        return "ok"
