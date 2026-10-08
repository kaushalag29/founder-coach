"""Acceptance tests for website Sources (docs/web-sources-plan.md AC1-AC12). Offline: a mocked
HTTP site, a fake browser, a fake clock and sleep. Needs the `web` extra (trafilatura)."""
import hashlib
import json
import os
os.environ["YTBRAIN_DOTENV"] = "0"          # hermetic: never read the developer's .env (keys, backend)
os.environ.setdefault("YTBRAIN_SOURCES_FILE", os.path.join(__import__("tempfile").mkdtemp(prefix="ytbrain-nosources-"), "sources.yaml"))   # hermetic: never read your sources.yaml (the file does not exist)
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("YTBRAIN_ROOT", tempfile.mkdtemp(prefix="ytbrain-webtest-"))


def _skipped(why: str) -> None:
    """A test that can't run here says so; CI sets REQUIRE_ALL_TESTS=1 and fails instead."""
    if os.environ.get("REQUIRE_ALL_TESTS") == "1":
        raise AssertionError(f"skipped in CI: {why}")
    print(f"    (skipped: {why})")


try:
    import httpx
    import trafilatura  # noqa: F401
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from ytbrain import sources as S                                      # noqa: E402
from ytbrain.web import urls                                          # noqa: E402

WORDS = ("Founders who talk to users every week learn faster than founders who build in isolation, "
         "because every conversation removes one wrong assumption about the problem they are solving. ")


def article(title, n=6, lang="en", author="Paul Graham", date="2013-07-01", links=(), extra=""):
    ld = json.dumps({"@context": "https://schema.org", "@type": "Article", "headline": title,
                     "datePublished": date, "author": {"@type": "Person", "name": author}})
    body = "".join(f"<p>{title} part {i}: {WORDS}</p>" for i in range(n))
    return (f'<html lang="{lang}"><head><title>{title} | Site</title><meta property="og:site_name" content="Essays">'
            f'<script type="application/ld+json">{ld}</script></head><body><nav>'
            + "".join(f'<a href="{h}">{h}</a> ' for h in links) +
            f"</nav><article><h1>{title}</h1><h2>First</h2>{body}{extra}</article></body></html>")


def listing(links):
    return "<html lang='en'><head><title>Index</title></head><body><ul>" + "".join(
        f'<li><a href="{h}">Essay {h}</a></li>' for h in links) + "</ul></body></html>"


class Site:
    """A scripted website: path -> (status, body, headers) or a callable(request) -> same."""

    def __init__(self, routes, host="https://ex.com"):
        self.routes, self.host, self.hits = dict(routes), host, []

    def handler(self, request):
        path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        self.hits.append((path, dict(request.headers)))
        r = self.routes.get(path)
        if callable(r):
            r = r(request)
        if r is None:
            return httpx.Response(404, text="not found")
        status, body, headers = (r + ({},))[:3] if isinstance(r, tuple) else (200, r, {})
        headers = {"content-type": "text/html; charset=utf-8", **headers}
        return httpx.Response(status, text=body if isinstance(body, str) else "", content=body if isinstance(body, bytes) else None,
                              headers=headers)

    def count(self, path):
        return sum(1 for p, _ in self.hits if p == path)


class FakeRenderer:
    def __init__(self, pages=None, unavailable=None):
        self.pages, self.unavailable, self.rendered = pages or {}, unavailable, []

    def available(self):
        return self.unavailable is None

    def render(self, url):
        from ytbrain.web.render import RenderError
        if url not in self.pages:
            raise RenderError("nothing rendered")
        self.rendered.append(url)
        return self.pages[url], url, 200

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def _env(site, renderer=None, clock=None):
    from ytbrain.manifest import Manifest
    from ytbrain.web.crawl import WebsiteAdapter
    from ytbrain.web.http import Fetcher
    slept = []
    fetcher = Fetcher(transport=httpx.MockTransport(site.handler), sleep=slept.append, clock=lambda: 0.0)
    clock = clock or Clock()
    ad = WebsiteAdapter(fetcher=fetcher, renderer=renderer or FakeRenderer(), clock=clock, say=lambda m: None,
                        sources=[])                   # never the developer's sources.yaml
    m = Manifest(Path(tempfile.mkdtemp()) / "manifest.db")
    return ad, m, slept, clock


def _src(**kw):
    return S.normalize({"type": "website", "url": "https://ex.com/essays/", **kw})


def _args(**kw):
    return types.SimpleNamespace(limit=0, force=False, **kw)


def _quiet(fn, *a, **k):
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()) as out:
        res = fn(*a, **k)
    return res, out.getvalue()


def _needs_web() -> bool:
    if not HAVE_WEB:
        _skipped("the web extra (trafilatura, httpx) isn't installed")
        return False
    return True


# --- AC1 config ------------------------------------------------------------------------------
def test_ac1_config_old_and_new_entries_and_the_enabled_flag():
    old = S.normalize({"id": "cs183b", "kind": "playlist", "playlist_id": "PL1"}, {"sleep_requests": 5})
    assert old["type"] == "youtube_playlist" and old["enabled"] and old["sleep_requests"] == 5
    assert S.normalize({"playlist_id": "PL2"})["type"] == "youtube_playlist"
    w = S.normalize({"url": "https://WWW.PaulGraham.com/articles.html?utm_source=x"})
    assert w["type"] == "website" and w["depth"] == 3 and w["render"] == "auto" and w["enabled"]
    assert w["url"] == "https://www.paulgraham.com/articles.html" and w["id"].startswith("paulgraham_com_")
    assert w["name"] == "paulgraham.com" and w["max_pages"] == 10_000
    assert S.normalize({"url": "https://a.com/x", "enabled": False})["enabled"] is False
    for bad, why in (({"url": "ftp://a.com"}, "needs url: https"), ({"type": "website"}, "needs url"),
                     ({"id": "x"}, "type must be"),
                     ({"url": "https://a.com", "render": "sometimes"}, "render must"),
                     ({"url": "https://a.com", "depth": -1}, "depth must"),
                     ({"url": "https://a.com", "enabled": "yes"}, "enabled must")):
        try:
            S.normalize(bad)
        except S.SourceConfigError as e:
            assert why in str(e), (why, str(e))
        else:
            raise AssertionError(f"accepted {bad}")
    d = Path(tempfile.mkdtemp())
    (d / "s.yaml").write_text("sources:\n- url: https://ex.com/a\n  enabled: false\n- playlist_id: PL1\n")
    loaded = S.load(d / "s.yaml")
    assert [s["type"] for s in loaded] == ["website", "youtube_playlist"]
    assert [s["type"] for s in S.enabled(loaded)] == ["youtube_playlist"]
    (d / "dup.yaml").write_text("sources:\n- {url: 'https://ex.com/a', id: x}\n- {playlist_id: PL1, id: x}\n")
    try:
        S.load(d / "dup.yaml")
    except S.SourceConfigError as e:
        assert "share the id" in str(e)
    else:
        raise AssertionError("duplicate ids accepted")


# --- AC2 identity ----------------------------------------------------------------------------
def test_ac2_canonical_urls_and_collision_proof_ids():
    c = urls.canonicalize("HTTPS://Ex.COM:443/a//b?utm_source=n&b=2&a=1&fbclid=z#top")
    assert c == "https://ex.com/a/b?a=1&b=2"
    same = {urls.doc_id(u) for u in ("https://ex.com/a/b?a=1&b=2", "http://www.ex.com/a/b?b=2&a=1&utm_medium=x")}
    assert len(same) == 1 and next(iter(same)).startswith("w-") and len(next(iter(same))) == 2 + 16
    assert urls.doc_id("https://ex.com/a") != urls.doc_id("https://ex.com/b")
    assert urls.url_hash("https://ex.com/a") == hashlib.sha256(b"//ex.com/a").hexdigest()
    for bad in ("mailto:x@y.com", "javascript:void(0)", "ftp://x"):
        assert urls.absolute("https://ex.com/", bad) is None
    assert urls.in_scope("https://www.ex.com/essays/x.html", "https://ex.com/essays/")
    assert not urls.in_scope("https://ex.com/about.html", "https://ex.com/essays/")
    assert not urls.in_scope("https://other.com/essays/x", "https://ex.com/essays/")
    assert urls.scope_prefix("https://ex.com/articles.html") == "/"


def test_ac2_a_second_url_with_the_same_id_is_refused_not_merged():
    if not _needs_web():
        return
    from ytbrain.web.state import WebState
    site = Site({"/robots.txt": (404, ""), "/essays/": article("Essay A")})
    ad, m, _, _ = _env(site)
    _quiet(ad.sync, m, [_src(depth=0)], _args())
    did = urls.doc_id("https://ex.com/essays/")
    st = WebState(m.db)
    st.db.execute("UPDATE web_pages SET url_hash='forged' WHERE doc_id=?", (did,))
    st.db.execute("UPDATE web_queue SET status='queued'")
    st.db.commit()
    res, out = _quiet(ad.sync, m, [_src(depth=0)], _args())
    assert "refused, not merged" in out and st.page(did)["url_hash"] == "forged"


# --- AC3 scope and depth, AC4 robots ---------------------------------------------------------------
def _site_basic():
    return Site({
        "/robots.txt": "User-agent: *\nDisallow: /essays/private\nCrawl-delay: 2\nSitemap: https://ex.com/sitemap.xml\n",
        "/sitemap.xml": (200, "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                         "<url><loc>https://ex.com/essays/s.html</loc><lastmod>2024-01-01</lastmod></url>"
                         "<url><loc>https://ex.com/shop/x.html</loc></url></urlset>", {"content-type": "application/xml"}),
        "/essays/": listing(["a.html", "b.html", "c.html", "private/x.html", "https://other.com/z", "/about.html",
                             "d.html", "e.html", "f.html", "g.html", "h.html"]),
        "/essays/a.html": article("Essay A", links=["deep.html"]),
        "/essays/b.html": ("<html lang='en'><body><h1>Talk</h1><p>Watch the talk.</p>"
                           "<iframe src='https://www.youtube.com/embed/LCEmiRjPEtQ'></iframe></body></html>"),
        "/essays/c.html": article("Essai C", lang="fr"),
        "/essays/s.html": article("From the sitemap"),
        "/essays/deep.html": article("Deep essay"),
    })


def test_ac3_ac4_ac5_first_crawl_follows_scope_depth_robots_and_routes_page_types():
    if not _needs_web():
        return
    from ytbrain.web.state import WebState
    site = _site_basic()
    ad, m, slept, _ = _env(site)
    res, out = _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert res["code"] == 0 and res["videos"] == 1
    assert site.count("/essays/private/x.html") == 0, "robots.txt disallows it"
    assert site.count("/about.html") == 0 and not any(p == "/z" for p, _ in site.hits), "out of scope"
    assert site.count("/shop/x.html") == 0, "sitemap URLs out of scope are ignored"
    assert site.count("/essays/s.html") == 1, "sitemap URLs in scope are crawled"
    assert site.count("/essays/deep.html") == 0, "depth 1 stops before a.html's links"
    assert 2.0 in slept, "Crawl-delay: 2 sets the per-host interval"
    a = urls.doc_id("https://ex.com/essays/a.html")
    doc = m.get_document(a)
    assert doc["doc_type"] == "web" and doc["title"] == "Essay A" and doc["published_at"] == "2013-07-01"
    assert doc["provenance"] == "Essays" and m.stage_status(a, "fetch") == "ok"
    assert m.get_document("LCEmiRjPEtQ")["doc_type"] == "youtube", "an embedded talk goes to YouTube sync"
    assert m.get_document(urls.doc_id("https://ex.com/essays/c.html")) is None, "French isn't ingested"
    q = {r["url"].rsplit("/", 1)[-1]: (r["status"], r["reason"]) for r in m.db.execute("SELECT * FROM web_queue")}
    assert q["c.html"][1] == "language: fr" and q["x.html"] == ("skipped", "robots.txt disallows it")
    assert q[""][1].startswith("listing") and q["b.html"][1].startswith("video")
    orphans = m.db.execute("SELECT d.doc_id FROM documents d LEFT JOIN stage_state s ON s.doc_id=d.doc_id "
                           "AND s.stage='fetch' WHERE d.doc_type='youtube' AND s.doc_id IS NULL").fetchall()
    assert [r[0] for r in orphans] == ["LCEmiRjPEtQ"], "YouTube sync picks up talks found on websites"
    # max_pages: a cap stops the run; the rest stays queued for the next one
    ad2, m2, _, _ = _env(_site_basic())
    _quiet(ad2.sync, m2, [_src(depth=2, max_pages=3)], _args())
    assert WebState(m2.db).counts(_src()["id"]).get("queued", 0) > 0


def test_ac4_robots_404_allows_everything_and_5xx_fetches_nothing():
    if not _needs_web():
        return
    site = Site({"/robots.txt": (404, ""), "/essays/": article("Only page")})
    ad, m, _, _ = _env(site)
    _quiet(ad.sync, m, [_src(depth=0)], _args())
    assert site.count("/essays/") == 1
    down = Site({"/robots.txt": (503, "down"), "/essays/": article("Never fetched")})
    ad, m, _, _ = _env(down)
    res, _ = _quiet(ad.sync, m, [_src(depth=0)], _args())
    assert res["code"] == 1 and down.count("/essays/") == 0 and down.count("/robots.txt") == 4, "retried, then stop"


def test_ac4_relative_sitemap_urls_resolve_and_bad_urls_are_not_retried():
    """Found on paulgraham.com: robots.txt says `Sitemap: /sitemap.xml` (RFC 9309 wants an absolute
    URL; real sites don't always comply). Relative sitemap URLs resolve against the file they came
    from, and a URL that can never work (no scheme) fails at once instead of being retried."""
    if not _needs_web():
        return
    site = Site({
        "/robots.txt": "User-agent: *\nSitemap: /sitemap.xml\n",
        "/sitemap.xml": (200, "<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                         "<sitemap><loc>/child.xml</loc></sitemap></sitemapindex>", {"content-type": "application/xml"}),
        "/child.xml": (200, "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                       "<url><loc>/essays/s.html</loc></url></urlset>", {"content-type": "application/xml"}),
        "/essays/": listing([]),
        "/essays/s.html": article("From a relative sitemap"),
    })
    ad, m, slept, _ = _env(site)
    res, _ = _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert res["code"] == 0
    assert site.count("/sitemap.xml") == 1 and site.count("/child.xml") == 1
    assert site.count("/essays/s.html") == 1, "a page from a relative sitemap entry is crawled"
    from ytbrain.web.http import Fetcher
    naps = []
    f = Fetcher(transport=httpx.MockTransport(site.handler), sleep=naps.append, clock=lambda: 0.0)
    r = f.get("/sitemap.xml")
    assert not r.ok and r.error.startswith("bad URL") and naps == [], "no retries for a URL that can't work"


def test_ac3_a_sitemap_listed_inside_a_urlset_is_read_as_a_sitemap():
    """Found on ycombinator.com: the site's sitemap lists `/library/sitemap.xml` as an ordinary
    <url>. A .xml / .xml.gz entry is a child sitemap to read, not a page to fetch."""
    if not _needs_web():
        return
    xml = {"content-type": "application/xml"}
    site = Site({
        "/robots.txt": "User-agent: *\nSitemap: https://ex.com/sitemap.xml\n",
        "/sitemap.xml": (200, "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                         "<url><loc>https://ex.com/essays/sitemap.xml</loc></url></urlset>", xml),
        "/essays/sitemap.xml": (200, "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                                "<url><loc>https://ex.com/essays/deep.html</loc></url></urlset>", xml),
        "/essays/": listing([]),
        "/essays/deep.html": article("Only in the child sitemap"),
    })
    ad, m, _, _ = _env(site)
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert site.count("/essays/sitemap.xml") == 1 and site.count("/essays/deep.html") == 1
    q = [r["url"] for r in m.db.execute("SELECT url FROM web_queue")]
    assert not any(u.endswith(".xml") for u in q), q


# A month page: a heading and a list of links. A page of bare links extracts to 0 words with some trafilatura
# releases (2.3.0) and 2 with others (2.2.0); at 0 words an unrenderable page is `failed` and retried every
# run, which is not what this test is about. With a heading it is a listing under every release.
MONTH_PAGE = ("<html lang='en'><head><title>October 2026</title></head><body><h1>October 2026</h1><ul><li><a href='post.html'>A post</a></li></ul></body></html>")


def test_ac3_raising_depth_reopens_the_old_frontier():
    """Also: the start page is fetched every run, even after a skip. Found on blog.samaltman.com: depth 1 from /archive reaches the month pages but not the posts
    they list. Raising `depth` in sources.yaml must reach them on the next run, without waiting a
    week for the month pages' re-check."""
    if not _needs_web():
        return
    site = Site({"/robots.txt": (404, ""), "/essays/": listing(["month1.html"]),
                 "/essays/month1.html": MONTH_PAGE,
                 "/essays/post.html": article("A post")})
    ad, m, _, _ = _env(site)
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert site.count("/essays/month1.html") == 1 and site.count("/essays/post.html") == 0
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert site.count("/essays/month1.html") == 1, "same depth: finished pages wait for their re-check"
    _quiet(ad.sync, m, [_src(depth=2)], _args())
    assert site.count("/essays/post.html") == 1, "depth 2 follows the month page's links"
    # a start page skipped once (too large, a bad day) is tried again every run
    m.db.execute("UPDATE web_queue SET status='skipped', reason='too large' WHERE depth=0")
    _quiet(ad.sync, m, [_src(depth=2)], _args())
    assert site.count("/essays/") == 4
    assert m.get_document(urls.doc_id("https://ex.com/essays/post.html")) is not None


def test_ac3_a_video_page_with_its_transcript_is_the_talk_and_the_start_page_is_the_index():
    """Found on ycombinator.com/library: most items are a YouTube video plus its full transcript,
    and the rendered index page holds ~14k words of item summaries. The talk is ingested once,
    from YouTube (timestamps, deep links); the page is not a second copy. A start page crawled
    with depth >= 1 is the Source's index, never an article. An essay that merely embeds a video
    is still an article."""
    if not _needs_web():
        return
    from ytbrain.web.page import parse
    body = "".join(f"<p>Speaker [00:0{i}:00] - {WORDS}</p>" for i in range(8))
    video_page = ("<html lang='en'><body><h1>On raising a seed round</h1><iframe src='https://www.youtube.com/embed/abcdefghijk'>"
                  "</iframe><h2>Transcript</h2>" + body + "</body></html>")
    assert parse(video_page, "https://ex.com/essays/v.html").page_type == "video"
    essay = article("Why startups die", extra="<iframe src='https://www.youtube.com/embed/abcdefghijk'></iframe>")
    assert parse(essay, "https://ex.com/essays/e.html").page_type == "article"
    index = article("The library", n=12, links=[f"i{n}.html" for n in range(12)])
    site = Site({"/robots.txt": (404, ""), "/essays/": index, "/essays/v.html": video_page, "/essays/e.html": essay,
                 **{f"/essays/i{n}.html": listing([]) for n in range(12)}})
    site.routes["/essays/"] = article("The library", n=12, links=["v.html", "e.html"])
    ad, m, _, _ = _env(site)
    res, _ = _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert m.get_document(urls.doc_id("https://ex.com/essays/")) is None, "the start page is the index"
    assert m.get_document(urls.doc_id("https://ex.com/essays/v.html")) is None, "the talk comes from YouTube"
    assert m.get_document("abcdefghijk")["doc_type"] == "youtube"
    assert m.get_document(urls.doc_id("https://ex.com/essays/e.html"))["doc_type"] == "web"
    # archive, category and tag pages list posts with excerpts: they are listings by their URL
    from ytbrain.web.crawl import page_kind
    long = parse(article("Excerpts", n=10), "https://ex.com/blog/archive")
    for u in ("https://ex.com/blog/archive", "https://ex.com/blog/categories/Essay", "https://ex.com/tag/growth/",
              "https://ex.com/blog/page/3", "https://ex.com/author/jane/"):
        assert page_kind(long, u, _src(url="https://ex.com/blog/")) == "listing", u
    assert page_kind(long, "https://ex.com/blog/archive-of-my-thoughts", _src(url="https://ex.com/blog/")) == "article"
    # pages saved before this rule are caught at clean: marked skipped, never extracted
    ad2, m2, _, _ = _env(site)
    from ytbrain.web import page as P
    real = P.TRANSCRIPT_MARK
    P.TRANSCRIPT_MARK = __import__("re").compile("(?!x)x")                 # the old behaviour: no rule
    try:
        _quiet(ad2.sync, m2, [_src(depth=0, url="https://ex.com/essays/v.html")], _args())
    finally:
        P.TRANSCRIPT_MARK = real
    vid = urls.doc_id("https://ex.com/essays/v.html")
    assert m2.get_document(vid) is not None
    res, _ = _quiet(ad2.clean, m2, vid, m2.get_document(vid))
    assert res == "skipped" and m2.stage_status(vid, "clean") == "skipped"


def test_review_pages_with_an_xml_declaration_or_a_meta_charset_parse():
    """XHTML pages start with <?xml ... encoding=...?>, which lxml refuses in a str: they came out
    empty (0 words, silently skipped). Pages whose charset is only in <meta> must decode with it."""
    if not _needs_web():
        return
    from ytbrain.web.page import parse
    xhtml = "<?xml version='1.0' encoding='utf-8'?>\n" + article("An XHTML essay")
    assert parse(xhtml, "https://ex.com/essays/x.html").page_type == "article"
    latin = article("Caf\u00e9 culture", extra="<p>Na\u00efve founders \u00e0 la carte.</p>").replace(
        "<head>", "<head><meta charset='iso-8859-1'>").encode("iso-8859-1")
    site = Site({"/robots.txt": (404, ""), "/essays/": (200, latin, {"content-type": "text/html"})})
    ad, m, _, _ = _env(site)
    _quiet(ad.sync, m, [_src(depth=0)], _args())
    doc = m.get_document(urls.doc_id("https://ex.com/essays/"))
    assert doc is not None and doc["title"] == "Caf\u00e9 culture", doc and doc["title"]


def test_review_a_gzip_bomb_sitemap_is_capped():
    if not _needs_web():
        return
    import gzip as _gz
    from ytbrain.web import crawl
    bomb = _gz.compress(b"<urlset>" + b" " * (crawl.SITEMAP_MAX_BYTES + 10) + b"</urlset>")
    site = Site({"/robots.txt": "User-agent: *\nSitemap: https://ex.com/big.xml.gz\n",
                 "/big.xml.gz": (200, bomb, {"content-type": "application/gzip"}),
                 "/essays/": article("Only page")})
    ad, m, _, _ = _env(site)
    res, out = _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert res["code"] == 0 and site.count("/essays/") == 1


def test_review_a_page_reopened_for_depth_is_fetched_in_full_not_conditionally():
    """Raising depth reopens pages at the old limit to follow their links. A conditional GET
    answered 304 has no body, so no links: those pages must be fetched in full."""
    if not _needs_web():
        return
    def page_a(req):
        if req.headers.get("if-none-match") == '"v1"':
            return (304, "", {"etag": '"v1"'})
        return (200, article("Essay A", links=["deep.html"]), {"etag": '"v1"'})
    site = Site({"/robots.txt": (404, ""), "/essays/": listing(["a.html"]), "/essays/a.html": page_a,
                 "/essays/deep.html": article("Deep essay")})
    ad, m, _, _ = _env(site)
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert site.count("/essays/deep.html") == 0
    _quiet(ad.sync, m, [_src(depth=2)], _args())
    assert site.count("/essays/deep.html") == 1, "a.html's links are followed after the depth increase"


# --- AC5 clean, AC10 shared Steps -----------------------------------------------------------------
def test_ac5_ac10_clean_extract_verify_items_and_pages_for_an_article():
    if not _needs_web():
        return
    from ytbrain import pages, verify
    from ytbrain.config import TRANSCRIPTS
    from ytbrain.extract import prompts, runner
    from ytbrain.knowledge.items import build_items
    site = Site({"/robots.txt": (404, ""), "/essays/": article("Talk to users", n=8)})
    ad, m, _, _ = _env(site)
    _quiet(ad.sync, m, [_src(depth=0)], _args())
    did = urls.doc_id("https://ex.com/essays/")
    res, _ = _quiet(ad.clean, m, did, m.get_document(did))
    assert res == "ok"
    tr = json.loads((TRANSCRIPTS / f"{did}.json").read_text())
    assert tr["source_kind"] == "article" and tr["locator"] == "paragraph" and tr["speaker"] == "Paul Graham"
    assert [u["start_ms"] for u in tr["utterances"]] == list(range(1, 9))
    assert tr["chapters"][0]["title"] == "First" and tr["chapters"][0]["start_ms"] == 1
    # the article prompt speaks of an article and its text; the talk prompt is unchanged
    ap = prompts.for_kind("article")
    import re
    for name in ("EXTRACT_PROMPT", "CHAPTER_PROMPT", "GROUNDING_FEEDBACK", "OVERVIEW_PROMPT"):
        text = re.sub(r"\{[a-z_]+\}", "", getattr(ap, name))          # format placeholders aren't wording
        left = re.findall(r"\b(talks?|transcripts?|clips?|captions?|timestamps?|video)\b", text)
        assert not left, (name, left)
    assert prompts.for_kind("talk").EXTRACT_PROMPT == prompts.EXTRACT_PROMPT
    quote = tr["utterances"][3]["text"][:120].rsplit(" ", 1)[0]
    sent = []

    def fake(prompt, schema, **kw):
        sent.append(prompt)
        return json.dumps({"title_canonical": "Talk to users", "speaker": "Someone Else", "category": "sales",
                           "summary": "s", "highlights": [{"text": "h", "evidence_span": quote}],
                           "advice_atoms": [{"atom_id": "a01", "text": "Talk to users weekly", "evidence_span": quote}]})
    runner.BACKENDS["fake-web"] = fake
    rec = runner.extract_video(tr, {"doc_id": did, "title": "Talk to users", "url": tr["url"],
                                    "source_kind": "article", "speaker": tr["speaker"]},
                               tr["chapters"], backend="fake-web").model_dump()
    assert len(sent) == 1 and "Article: Talk to users" in sent[0] and "TEXT:" in sent[0] and "[4]" in sent[0]
    assert rec["source_kind"] == "article" and rec["locator"] == "paragraph" and rec["caption_kind"] == "none"
    assert rec["url"] == "https://ex.com/essays/" and rec["speaker"] == "Paul Graham", "given beats generated"
    verify.verify_record(rec, tr["utterances"])
    assert rec["advice_atoms"][0]["timestamp_ms"] == 4, "the quote is located to paragraph 4"
    items = build_items(rec, tr)
    adv = next(i for i in items if i["kind"] == "advice")
    assert adv["source_kind"] == "article" and adv["start_ms"] == 4
    assert adv["deep_link"].startswith("https://ex.com/essays/#:~:text=Talk%20to%20users%20part%203")
    assert next(i for i in items if i["kind"] == "summary")["deep_link"] == "https://ex.com/essays/"
    page = pages.render_markdown(rec)
    assert "[¶4](https://ex.com/essays/#:~:text=" in page and "source_kind: \"article\"" in page
    assert "video_id" not in page


def test_ac5_source_name_and_author_fill_gaps_and_saved_paths_are_relative():
    """Found on paulgraham.com: pages carry no author metadata and no site name. A Source's own
    `name` (when written) is the series and its optional `author` is the fallback speaker; a page's
    own author metadata still wins. Saved-page paths are stored relative to data/raw/web, so the
    repo (or YTBRAIN_ROOT) can move."""
    if not _needs_web():
        return
    from ytbrain.config import TRANSCRIPTS
    from ytbrain.web.crawl import WebsiteAdapter
    from ytbrain.web.http import Fetcher
    from ytbrain.web.state import WebState
    bare = ("<html lang='en'><head><title>How to Do Great Work</title></head><body><p>July 2023</p>"
            + "".join(f"<p>Part {i}: {WORDS}</p>" for i in range(6)) + "</body></html>")
    site = Site({"/robots.txt": (404, ""), "/essays/": listing(["bare.html", "signed.html"]),
                 "/essays/bare.html": bare, "/essays/signed.html": article("Signed", author="Jessica Livingston")})
    src = _src(depth=1, name="PG Essays", author="Paul Graham")
    assert src["series"] == "PG Essays", "an explicit name is the series"
    assert _src()["name"] == "ex.com" and "series" not in _src(), "a defaulted name isn't"
    fetcher = Fetcher(transport=httpx.MockTransport(site.handler), sleep=lambda s: None, clock=lambda: 0.0)
    ad = WebsiteAdapter(fetcher=fetcher, renderer=FakeRenderer(), clock=Clock(), say=lambda msg: None, sources=[src])
    from ytbrain.manifest import Manifest
    m = Manifest(Path(tempfile.mkdtemp()) / "manifest.db")
    _quiet(ad.sync, m, [src], _args())
    bare_id, signed_id = urls.doc_id("https://ex.com/essays/bare.html"), urls.doc_id("https://ex.com/essays/signed.html")
    assert m.get_document(bare_id)["series"] == "PG Essays"
    assert m.get_document(bare_id)["published_at"] == "2023-07-01", "a 'July 2023' byline is the date"
    from ytbrain.web.page import parse
    run_in = parse("<html lang='en'><body><p>October 2014 We're a couple of weeks into the course. "
                   + WORDS + "</p>" + "".join(f"<p>{WORDS}</p>" for _ in range(4)) + "</body></html>",
                   "https://ex.com/essays/before.html")
    assert run_in.published_at == "2014-10-01", run_in.published_at
    # paulgraham.com markup: no <p> per paragraph, <br><br> between them, the date glued to the text
    brs = parse("<html lang='en'><body><p>March 2012One of the more surprising things. " + WORDS
                + "<br><br>Second paragraph. " + WORDS + "<br><br><b>1. A point.</b><br><br>Third. " + WORDS
                + "</p></body></html>", "https://ex.com/essays/ambitious.html")
    assert brs.published_at == "2012-03-01", brs.published_at
    assert len(brs.units) >= 3 and brs.units[1]["text"].startswith("Second paragraph"), [u["text"][:20] for u in brs.units]
    from lxml import etree
    from ytbrain.web.page import _lines
    fallback = etree.fromstring("<p>October 2015\n" + WORDS * 2 + "\n" + WORDS * 2 + "</p>")  # trafilatura's table fallback
    assert len(_lines(fallback)) == 3
    wrapped = etree.fromstring("<p>a paragraph wrapped\nat seventy characters\nin the source</p>")
    assert _lines(wrapped) == ["a paragraph wrapped at seventy characters in the source"], "wraps aren't breaks"
    later = parse("<html lang='en'><body>" + "".join(f"<p>{WORDS}</p>" for _ in range(5))
                  + "<p>March 2011 is when this happened.</p></body></html>", "https://ex.com/essays/x.html")
    assert later.published_at != "2011-03-01", "only a byline near the top counts, not a date in the text"
    row = WebState(m.db).page(bare_id)
    assert not Path(row["raw_path"]).is_absolute(), row["raw_path"]
    for did, speaker in ((bare_id, "Paul Graham"), (signed_id, "Jessica Livingston")):
        res, _ = _quiet(ad.clean, m, did, m.get_document(did))
        assert res == "ok", res
        tr = json.loads((TRANSCRIPTS / f"{did}.json").read_text())
        assert tr["speaker"] == speaker and tr["series"] == "PG Essays", (tr["speaker"], tr["series"])
    # a row written before this fix (absolute path from another machine) still finds the file
    m.db.execute("UPDATE web_pages SET raw_path=? WHERE doc_id=?", ("/elsewhere/old/" + bare_id + ".html", bare_id))
    m.db.execute("UPDATE documents SET published_at='2023-01-01', series='ex.com' WHERE doc_id=?", (bare_id,))
    res, _ = _quiet(ad.clean, m, bare_id, m.get_document(bare_id))
    assert res in ("ok", "unchanged"), res
    doc = m.get_document(bare_id)
    assert (doc["published_at"], doc["series"]) == ("2023-07-01", "PG Essays"), "clean re-reads the saved page"
    # a Source written with an author that isn't text is a config error
    try:
        _src(author=["x"])
        raise AssertionError("author must be text")
    except S.SourceConfigError:
        pass


def test_ac10_talk_records_are_unchanged():
    from ytbrain import pages
    from ytbrain.extract.schema import VideoMetadata
    from ytbrain.knowledge.items import build_items
    rec = {"doc_id": "abc", "title_raw": "T", "url": "https://www.youtube.com/watch?v=abc",
           "highlights": [{"text": "H", "evidence_span": "q q q", "timestamp_ms": 72000, "match_score": 1.0}],
           "extraction_meta": {"verification": {"threshold": 0.7}}, "summary": "s"}
    assert "watch?v=abc&t=72s" in pages.render_markdown(rec) and "[01:12]" in pages.render_markdown(rec)
    it = build_items(rec, None)
    assert it[0]["deep_link"] == "https://www.youtube.com/watch?v=abc&t=72s" and it[0]["source_kind"] == "talk"
    fields = VideoMetadata.model_fields
    assert fields["source_kind"].default == "talk" and fields["locator"].default == "time"


# --- AC6 rendering ---------------------------------------------------------------------------------
def test_ac6_render_only_when_needed_and_fall_back_without_a_browser():
    if not _needs_web():
        return
    shell = "<html lang='en'><body><div id='root'></div><script>app()</script></body></html>"
    full = article("Rendered essay")
    site = Site({"/robots.txt": (404, ""), "/essays/": shell})
    r = FakeRenderer({"https://ex.com/essays/": full})
    ad, m, _, _ = _env(site, renderer=r)
    _quiet(ad.sync, m, [_src(depth=0)], _args())
    assert r.rendered == ["https://ex.com/essays/"]
    assert m.get_document(urls.doc_id("https://ex.com/essays/"))["title"] == "Rendered essay"
    r2 = FakeRenderer({"https://ex.com/essays/": full})
    ad, m, _, _ = _env(Site({"/robots.txt": (404, ""), "/essays/": shell}), renderer=r2)
    _quiet(ad.sync, m, [_src(depth=0, render="never")], _args())
    assert r2.rendered == [] and m.get_document(urls.doc_id("https://ex.com/essays/")) is None
    rich = Site({"/robots.txt": (404, ""), "/essays/": full})
    r3 = FakeRenderer({"https://ex.com/essays/": full})
    ad, m, _, _ = _env(rich, renderer=r3)
    _quiet(ad.sync, m, [_src(depth=0)], _args())
    assert r3.rendered == [], "a page with enough text isn't rendered"
    none = FakeRenderer(unavailable="Playwright isn't installed")
    ad = _env(Site({"/robots.txt": (404, ""), "/essays/": full}), renderer=none)[0]
    ad.say = (said := []).append
    _quiet(ad.sync, _env(rich)[1], [_src(depth=0, render="always")], _args())
    assert any("Playwright isn't installed" in s for s in said), "one note when the browser is missing"


# --- AC7 retries and failures ----------------------------------------------------------------------
def test_ac7_retry_after_backoff_persistent_errors_host_pause_and_tombstones():
    if not _needs_web():
        return
    from ytbrain.web.state import WebState
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return (429, "slow down", {"retry-after": "7"}) if calls["n"] == 1 else (200, article("After a 429"), {})
    site = Site({"/robots.txt": (404, ""), "/essays/": flaky})
    ad, m, slept, _ = _env(site)
    _quiet(ad.sync, m, [_src(depth=0)], _args())
    assert 7.0 in slept and m.get_document(urls.doc_id("https://ex.com/essays/")) is not None
    bad = Site({"/robots.txt": (404, ""), "/essays/": listing([f"p{i}.html" for i in range(12)]),
                **{f"/essays/p{i}.html": (503, "down") for i in range(12)}})
    ad, m, slept, _ = _env(bad)
    res, out = _quiet(ad.sync, m, [_src(depth=1)], _args())
    st = WebState(m.db)
    c = st.counts(_src()["id"])
    assert c.get("failed") == 5 and c.get("queued") == 7, "5 failures in a row pause the host"
    assert bad.count("/essays/p0.html") == 4, "3 retries per request"
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert st.counts(_src()["id"]).get("failed") == 5 and bad.count("/essays/p0.html") == 8, "retried next run"
    # a known article answering 404 is tombstoned, and leaves the pipeline
    gone = {"flag": False}
    site = Site({"/robots.txt": (404, ""), "/essays/": lambda r: (404, "") if gone["flag"] else (200, article("Soon gone"))})
    clock = Clock()
    ad, m, _, _ = _env(site, clock=clock)
    _quiet(ad.sync, m, [_src(depth=0)], _args())
    did = urls.doc_id("https://ex.com/essays/")
    gone["flag"] = True
    _quiet(ad.sync, m, [_src(depth=0)], _args())
    assert m.get_document(did)["tombstoned_at"] and m.stage_status(did, "fetch") == "stale"
    assert did not in m.pending("clean"), "tombstoned Documents aren't worked on"


# --- AC8 incremental, AC9 resume ------------------------------------------------------------------
def test_ac8_rechecks_are_conditional_changes_reopen_clean_and_moved_text_is_an_alias():
    if not _needs_web():
        return
    body = {"html": article("Essay A")}
    site = Site({"/robots.txt": (404, ""), "/essays/": listing(["a.html"] + [f"z{i}" for i in range(10)]),
                 "/essays/a.html": lambda r: (304, "", {}) if r.headers.get("if-none-match") == '"v1"' and body["html"] == article("Essay A")
                 else (200, body["html"], {"etag": '"v1"'})})
    clock = Clock()
    ad, m, _, _ = _env(site, clock=clock)
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    a = urls.doc_id("https://ex.com/essays/a.html")
    _quiet(ad.clean, m, a, m.get_document(a))
    assert site.count("/essays/a.html") == 1
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert site.count("/essays/a.html") == 1 and site.count("/essays/") == 2, "within 7 days: only the start page"
    clock.t += 8 * 86400
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    last = [h for p, h in site.hits if p == "/essays/a.html"][-1]
    assert last.get("if-none-match") == '"v1"' and m.stage_status(a, "clean") == "ok", "304: nothing downstream"
    body["html"] = article("Essay A", n=7)
    clock.t += 8 * 86400
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert m.stage_status(a, "clean") == "stale", "a changed page re-opens clean (and so extract)"
    # the same text at a new URL (a moved page) is an alias, not a second Document
    moved = Site({"/robots.txt": (404, ""), "/new/": listing(["essay-a"] + [f"q{i}" for i in range(10)]),
                  "/new/essay-a": body["html"]})
    ad2 = _env(moved)[0]
    ad2.clock = clock
    res, out = _quiet(ad2.sync, m, [S.normalize({"type": "website", "url": "https://ex.com/new/", "depth": 1})], _args())
    assert m.get_document(urls.doc_id("https://ex.com/new/essay-a")) is None
    assert m.get_document(a)["url"] == "https://ex.com/new/essay-a", "the Document follows its text"


def test_ac9_an_interrupted_crawl_resumes_without_refetching():
    if not _needs_web():
        return
    site = Site({"/robots.txt": (404, ""), "/essays/": listing([f"e{i}.html" for i in range(10)]),
                 **{f"/essays/e{i}.html": article(f"Essay {i}") for i in range(10)}})
    ad, m, _, _ = _env(site)
    boom = {"after": 4}
    real = ad._one

    def crashing(*a, **k):
        boom["after"] -= 1
        if boom["after"] < 0:
            raise KeyboardInterrupt
        return real(*a, **k)
    ad._one = crashing
    try:
        _quiet(ad.sync, m, [_src(depth=1)], _args())
    except KeyboardInterrupt:
        pass
    ad._one = real
    fetched_before = {p for p, _ in site.hits if p.startswith("/essays/e")}
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    again = [p for p, _ in site.hits if p.startswith("/essays/e")]
    assert len(fetched_before) == 3 and len(again) == 10 and len(set(again)) == 10, "each page fetched once"


# --- end to end through the CLI, over real HTTP on localhost -------------------------------------------
def test_cli_sync_and_clean_over_a_real_local_http_server():
    """`ytbrain sync` then `ytbrain clean` with a website in sources.yaml: real sockets (a local
    server, nothing leaves the machine), the adapter dispatch, the disabled flag, and the talks
    found on the site handed to the YouTube adapter."""
    if not _needs_web():
        return
    import contextlib, functools, http.server, io, threading
    from ytbrain import cli
    from ytbrain.config import MANIFEST_DB, TRANSCRIPTS
    from ytbrain.manifest import Manifest
    from ytbrain.web import http as H
    site = Path(tempfile.mkdtemp())
    (site / "essays").mkdir()
    (site / "essays" / "index.html").write_text(listing(["a.html", "b.html"] + [f"n{i}.html" for i in range(9)]))
    # a title no earlier run can have left behind: the same text in a reused data root is a duplicate and is skipped
    (site / "essays" / "a.html").write_text(article(f"Local essay {os.urandom(4).hex()}"))
    (site / "essays" / "b.html").write_text("<html lang='en'><body><p>Watch.</p>"
                                            "<iframe src='https://www.youtube.com/embed/abcdefghijk'></iframe></body></html>")
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(site))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    cfg = Path(tempfile.mkdtemp()) / "sources.yaml"
    cfg.write_text(f"sources:\n- url: {base}/essays/index.html\n  depth: 1\n  render: never\n"
                   f"- url: {base}/other/\n  enabled: false\n")
    yt_calls = []
    real_sources, real_interval, real_yt = cli.SOURCES, H.WEB_MIN_INTERVAL_S, S.YouTubeAdapter.sync
    cli.SOURCES, H.WEB_MIN_INTERVAL_S = cfg, 0.0
    S.YouTubeAdapter.sync = lambda self, m, srcs, args, say=print: yt_calls.append([s["id"] for s in srcs]) or {"code": 0}
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["sync"])
        assert code == 0, out.getvalue()[-800:]
        assert "is disabled (enabled: false) -- skipped" in out.getvalue()
        # the last call hands the talk found on the site to the YouTube adapter (an earlier suite in
        # the same process may have left YouTube Documents that trigger a first call)
        assert yt_calls and yt_calls[-1] == [], "the talk found on the site goes to the YouTube adapter"
        m = Manifest(MANIFEST_DB)
        did = urls.doc_id(f"{base}/essays/a.html")
        assert m.get_document(did)["doc_type"] == "web" and m.get_document("abcdefghijk")["doc_type"] == "youtube"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert cli.main(["clean"]) == 0
        assert json.loads((TRANSCRIPTS / f"{did}.json").read_text())["source_kind"] == "article"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            assert cli.main(["sync", "--source", "nope"]) == 1
        assert "no source 'nope'" in err.getvalue()
        sid = S.load(cfg)[0]["id"]
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.main(["invalidate", "clean", "--source", sid]) == 0
        assert Manifest(MANIFEST_DB).stage_status(did, "clean") == "stale"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert cli.main(["drop", "--source", sid]) == 0
        assert Manifest(MANIFEST_DB).get_document(did)["tombstoned_at"], "drop takes a Source out of the knowledge"
    finally:
        cli.SOURCES, H.WEB_MIN_INTERVAL_S, S.YouTubeAdapter.sync = real_sources, real_interval, real_yt
        srv.shutdown()


def test_cli_sync_type_picks_websites_or_youtube_only():
    """`ytbrain sync --type website|youtube`: one Source type per run, validated against --source."""
    if not _needs_web():
        return
    import contextlib, io
    from ytbrain import cli
    from ytbrain.config import MANIFEST_DB
    from ytbrain.manifest import Manifest
    from ytbrain.web.crawl import WebsiteAdapter
    cfg = Path(tempfile.mkdtemp()) / "sources.yaml"
    cfg.write_text("sources:\n- id: talks\n  playlist_id: PLabc\n- id: essays\n  url: https://ex.com/essays/\n")
    Manifest(MANIFEST_DB).upsert_document("orphanvid01", "essays", doc_type="youtube")  # found on a site earlier
    calls = []
    real = cli.SOURCES, S.YouTubeAdapter.sync, WebsiteAdapter.sync
    cli.SOURCES = cfg
    S.YouTubeAdapter.sync = lambda self, m, srcs, args, say=print: calls.append(("yt", [s["id"] for s in srcs])) or {"code": 0}
    WebsiteAdapter.sync = lambda self, m, srcs, args, say=print: calls.append(("web", [s["id"] for s in srcs])) or {"code": 0, "videos": 0}
    try:
        def run(*argv):
            calls.clear()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.main(["sync", *argv])
            return code, list(calls), err.getvalue()
        assert run("--type", "website")[:2] == (0, [("web", ["essays"])]), "no YouTube pass at all"
        assert run("--type", "youtube")[:2] == (0, [("yt", ["talks"])])
        assert run()[:2] == (0, [("yt", ["talks"]), ("web", ["essays"])])
        code, got, err = run("--type", "youtube", "--source", "essays")
        assert code == 1 and not got and "is a website" in err, err
    finally:
        cli.SOURCES, S.YouTubeAdapter.sync, WebsiteAdapter.sync = real


def test_review_report_counts_articles_apart_from_talks():
    """`ytbrain report`'s record rate is records per captioned talk; articles (caption_kind
    "none") are not talks without captions and don't inflate the talk record rate."""
    import contextlib, io
    from ytbrain import cli
    from ytbrain.config import MANIFEST_DB, METADATA
    from ytbrain.manifest import Manifest, StageState
    m = Manifest(MANIFEST_DB)
    m.upsert_document("rep_talk_01", "s", doc_type="youtube", caption_kind="human", has_captions=1)
    for d in ("w-rep0000000000001", "w-rep0000000000002"):
        m.upsert_document(d, "site", doc_type="web", caption_kind="none", has_captions=0)
    for d in ("rep_talk_01", "w-rep0000000000001", "w-rep0000000000002"):
        m.mark(StageState(d, "extract", "ok"))
        METADATA.mkdir(parents=True, exist_ok=True)
        (METADATA / f"{d}.json").write_text(json.dumps({"doc_id": d, "extraction_meta": {"validation_status": "pass"}}))
    with contextlib.redirect_stdout(io.StringIO()) as out:
        cli.main(["report"])
    text = out.getvalue()
    talks = [d for d in m.documents() if (d["doc_type"] or "youtube") == "youtube"]
    uncaptioned = sum(d["caption_kind"] not in ("human", "auto") for d in talks)
    talks_line = next(l for l in text.splitlines() if "no captions fetched" in l)
    assert int(talks_line.split()[-1]) == uncaptioned, (talks_line, uncaptioned)   # articles aren't counted
    assert "web pages saved" in text and "articles cleaned" in text


def test_backlog_a_video_page_whose_talk_has_no_captions_uses_the_pages_transcript():
    """A video page is normally left to YouTube (the talk, with timestamps). When YouTube has no
    captions for it (private, removed, none in English), the page's transcript is the only copy:
    the next sync saves the page as an article instead of losing the talk."""
    if not _needs_web():
        return
    from ytbrain.manifest import StageState
    body = "".join(f"<p>Speaker [00:0{i}:00] - {WORDS}</p>" for i in range(8))
    page = ("<html lang='en'><body><h1>On pricing</h1><iframe src='https://www.youtube.com/embed/nocaption01'>"
            "</iframe><h2>Transcript</h2>" + body + "</body></html>")
    captioned = page.replace("nocaption01", "hascaption1").replace("On pricing", "On hiring")
    site = Site({"/robots.txt": (404, ""), "/essays/": listing(["v.html", "w.html"]),
                 "/essays/v.html": page, "/essays/w.html": captioned})
    ad, m, _, clock = _env(site)
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    vid, wid = urls.doc_id("https://ex.com/essays/v.html"), urls.doc_id("https://ex.com/essays/w.html")
    assert m.get_document(vid) is None and m.get_document("nocaption01")["doc_type"] == "youtube"
    m.mark(StageState("nocaption01", "fetch", "skipped", error="no English captions"))   # YouTube sync's verdict
    m.mark(StageState("hascaption1", "fetch", "ok"))
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert m.get_document(vid)["doc_type"] == "web", "the page's transcript is kept"
    assert m.get_document(wid) is None, "a talk YouTube has captions for still comes from YouTube"
    res, _ = _quiet(ad.clean, m, vid, m.get_document(vid))
    assert res == "ok"


def test_backlog_a_page_reclassified_after_extraction_leaves_the_knowledge():
    """A page saved as an article under an older rule, already extracted, that `clean` now finds
    is not an article: it leaves the index (tombstoned), and comes back if it is an article again."""
    if not _needs_web():
        return
    import re as _re
    from ytbrain.manifest import StageState
    from ytbrain.web import page as P
    body = "".join(f"<p>Speaker [00:0{i}:00] - {WORDS}</p>" for i in range(8))
    html = ("<html lang='en'><body><iframe src='https://www.youtube.com/embed/abcdefghij2'></iframe>"
            "<h2>Transcript</h2>" + body + "</body></html>")
    site = Site({"/robots.txt": (404, ""), "/essays/v.html": html})
    ad, m, _, _ = _env(site)
    real = P.TRANSCRIPT_MARK
    P.TRANSCRIPT_MARK = _re.compile("(?!x)x")                          # the older rule
    try:
        _quiet(ad.sync, m, [_src(depth=0, url="https://ex.com/essays/v.html")], _args())
    finally:
        P.TRANSCRIPT_MARK = real
    did = urls.doc_id("https://ex.com/essays/v.html")
    m.mark(StageState(did, "clean", "ok"))
    m.mark(StageState(did, "extract", "ok"))
    res, _ = _quiet(ad.clean, m, did, m.get_document(did))
    assert res == "skipped" and m.db.execute("SELECT tombstoned_at FROM documents WHERE doc_id=?",
                                             (did,)).fetchone()[0], "its items leave at the next index"


def test_review_pages_whose_links_matter_are_never_fetched_conditionally():
    """Found on pmarchive.com: its start page was saved as an article (before the start-page rule),
    so each run asked "changed since?", got 304, and never read the page's links again: new posts
    were never found. A page below the Source's depth is fetched in full; only leaf pages (at the
    depth limit) are re-checked conditionally."""
    if not _needs_web():
        return
    links = ["a.html"]

    def start(req):
        if req.headers.get("if-none-match") == '"s1"':
            return (304, "", {"etag": '"s1"'})
        return (200, article("Home", n=8, links=list(links)), {"etag": '"s1"'})

    def leaf(req):
        if req.headers.get("if-none-match") == '"a1"':
            return (304, "", {"etag": '"a1"'})
        return (200, article("Essay A"), {"etag": '"a1"'})
    site = Site({"/robots.txt": (404, ""), "/essays/": start, "/essays/a.html": leaf,
                 "/essays/b.html": article("Essay B")})
    ad, m, _, clock = _env(site)
    from ytbrain.web.state import WebState
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    st = WebState(m.db)                              # as if the start page had been saved as an article
    st.save_page(doc_id="w-start", source_id=_src()["id"], url="https://ex.com/essays/", url_hash="x" * 64,
                 text_hash="t", etag='"s1"', last_modified=None, fetched_at="2026-01-01T00:00:00Z",
                 checked_at="2026-01-01T00:00:00Z", rendered=0, raw_path="ex.com/w-start.html")
    links.append("b.html")                           # a new post appears on the start page
    clock.t += 8 * 86400                             # past the re-check window
    _quiet(ad.sync, m, [_src(depth=1)], _args())
    assert site.count("/essays/b.html") == 1, "the start page was read in full, so b.html was found"
    leaf_conditional = [h for p_, h in site.hits if p_ == "/essays/a.html" and h.get("if-none-match")]
    assert leaf_conditional, "a leaf page is still re-checked conditionally"


# --- AC11 coach, AC12 pinned generated schema ------------------------------------------------------
def test_ac11_coach_hits_say_what_they_are():
    try:
        from founder_coach.server import _hit
    except ImportError:
        _skipped("mcp isn't installed")
        return
    row = {"item_id": "adv:w-1:a01", "kind": "advice", "text": "t", "evidence": "q", "title": "Essay",
           "speaker": "PG", "year": 2013, "deep_link": "https://ex.com/#:~:text=q", "start_ms": 4,
           "source_kind": "article"}
    h = _hit(row, False)
    assert h.source_kind == "article" and h.start_s is None
    t = _hit({**row, "source_kind": "talk", "start_ms": 760000}, False)
    assert t.source_kind == "talk" and t.start_s == 760


def test_ac12_the_generated_schema_is_pinned_to_its_version():
    """SCHEMA_VERSION tracks what the model generates (ADR-0013): a change to Generated,
    Overview or ChapterList without a version bump fails here."""
    from ytbrain.config import SCHEMA_VERSION
    from ytbrain.extract.schema import ChapterList, Generated, Overview
    blob = json.dumps([m.model_json_schema() for m in (Generated, Overview, ChapterList)], sort_keys=True)
    digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
    pinned = json.loads((ROOT / "tests" / "golden" / "generated_schema.json").read_text())
    assert pinned.get(SCHEMA_VERSION) == digest, (
        f"the generated schema changed ({digest}) but SCHEMA_VERSION is still {SCHEMA_VERSION}: bump it, declare the "
        "release compatible or breaking in ytbrain/extract/versions.py (ADR-0018), then add the new digest to "
        "tests/golden/generated_schema.json")


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
