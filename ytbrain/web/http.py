"""Polite HTTP (plan D6): per-host pacing, robots.txt (RFC 9309), retries with backoff that
honour Retry-After, conditional GET, size and type limits. httpx only.

Everything that waits takes `sleep` and `clock`, so tests run instantly and deterministically.
"""
from __future__ import annotations

import email.utils
import random
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import (WEB_BACKOFF_BASE_S, WEB_MAX_BYTES, WEB_MAX_CRAWL_DELAY_S, WEB_MIN_INTERVAL_S,
                      WEB_RETRIES, WEB_RETRY_AFTER_MAX_S, WEB_ROBOTS_AGENT, WEB_TIMEOUT_S, WEB_USER_AGENT)

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
HTML_TYPES = ("text/html", "application/xhtml+xml")


@dataclass
class Response:
    url: str                         # the final URL, after redirects
    status: int                      # 0 when no response came back
    headers: dict = field(default_factory=dict)
    text: str = ""
    error: str | None = None         # why it failed, when it failed
    body: bytes = b""                # the raw bytes (a gzipped sitemap isn't text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.error is None

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()


def retry_after_s(value: str | None, now: float | None = None) -> float | None:
    """Seconds from a Retry-After header: delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - (now if now is not None else time.time()))


class Pacer:
    """At most one request per `interval` per host (the site's Crawl-delay if slower)."""

    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self.sleep, self.clock = sleep, clock
        self.interval: dict[str, float] = {}
        self.last: dict[str, float] = {}

    def set_interval(self, host: str, seconds: float) -> None:
        self.interval[host] = max(WEB_MIN_INTERVAL_S, min(seconds, WEB_MAX_CRAWL_DELAY_S))

    def wait(self, host: str) -> None:
        gap = self.interval.get(host, WEB_MIN_INTERVAL_S)
        if host in self.last:
            left = self.last[host] + gap - self.clock()
            if left > 0:
                self.sleep(left)
        self.last[host] = self.clock()


class Fetcher:
    """GET with pacing, retries and limits. One per run; close() when done."""

    def __init__(self, user_agent: str = WEB_USER_AGENT, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep, clock=time.monotonic, retries: int = WEB_RETRIES, say=None):
        self.client = httpx.Client(headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,"
                                            "application/xml;q=0.9,*/*;q=0.5", "Accept-Language": "en"},
                                   timeout=WEB_TIMEOUT_S, follow_redirects=True, max_redirects=5,
                                   transport=transport)
        self.pacer = Pacer(sleep, clock)
        self.sleep, self.retries, self.say = sleep, retries, say or (lambda m: None)

    def close(self) -> None:
        self.client.close()

    def get(self, url: str, etag: str | None = None, last_modified: str | None = None,
            max_bytes: int = WEB_MAX_BYTES) -> Response:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:   # a caller bug or a bad link: never retried
            return Response(url, 0, error=f"bad URL: {url!r} is not an absolute http(s) URL")
        last: Response | None = None
        for attempt in range(self.retries + 1):
            self.pacer.wait(host)
            last = self._once(url, headers, max_bytes)
            network = last.status == 0 and bool(last.error) and not last.error.startswith(
                ("too large", "too many", "bad URL"))
            if last.status not in RETRYABLE and not network:
                return last
            if attempt == self.retries:
                break
            wait = retry_after_s(last.headers.get("retry-after"))
            if wait is not None and wait > WEB_RETRY_AFTER_MAX_S:
                last.error = f"server asked to retry after {wait:.0f}s; trying again next run"
                return last
            if wait is None:
                wait = WEB_BACKOFF_BASE_S * 2 ** attempt * random.uniform(0.8, 1.2)
            self.say(f"{last.status or last.error} on {url}; retry {attempt + 1}/{self.retries} in {wait:.0f}s")
            self.sleep(wait)
        if last.error is None:
            last.error = f"HTTP {last.status}"
        return last

    def _once(self, url: str, headers: dict, max_bytes: int) -> Response:
        try:
            with self.client.stream("GET", url, headers=headers) as r:
                hdrs = {k.lower(): v for k, v in r.headers.items()}
                size = int(hdrs.get("content-length") or 0)
                if size > max_bytes:
                    return Response(str(r.url), r.status_code, hdrs, error=f"too large ({size} bytes)")
                chunks, total = [], 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        return Response(str(r.url), r.status_code, hdrs, error=f"too large (> {max_bytes} bytes)")
                    chunks.append(chunk)
                body = b"".join(chunks)
                enc = _charset(hdrs.get("content-type", ""), body) or r.encoding or "utf-8"
                try:
                    text = body.decode(enc, errors="replace")
                except LookupError:
                    text = body.decode("utf-8", errors="replace")
                return Response(str(r.url), r.status_code, hdrs, text, body=body)
        except httpx.TooManyRedirects:
            return Response(url, 0, error="too many redirects")
        except (httpx.UnsupportedProtocol, httpx.InvalidURL) as e:   # can never work: not retried
            return Response(url, 0, error=f"bad URL: {str(e)[:160]}")
        except httpx.HTTPError as e:
            return Response(url, 0, error=f"{type(e).__name__}: {str(e)[:160] or 'network error'}")


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)


def _charset(content_type: str, body: bytes) -> str | None:
    """The page's encoding: the Content-Type header's charset, else a <meta charset> (or
    http-equiv) in the first 4 KB, else None (UTF-8). httpx alone assumes UTF-8 when the header
    has no charset, which garbles pages that declare theirs only in HTML."""
    m = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", content_type or "", re.I)
    if m:
        return m.group(1)
    if "html" in (content_type or "html").lower():
        m = _META_CHARSET.search(body[:4096])
        if m:
            return m.group(1).decode("ascii", "ignore")
    return None


@dataclass
class Robots:
    """One host's robots.txt, fetched once per run (RFC 9309 status rules)."""
    parser: urllib.robotparser.RobotFileParser
    reachable: bool                      # False: 5xx/network error -> nothing may be fetched this run
    note: str = ""
    base: str = ""                       # the robots.txt URL; relative Sitemap lines resolve against it

    def allowed(self, url: str) -> bool:
        return self.reachable and self.parser.can_fetch(WEB_ROBOTS_AGENT, url)

    def crawl_delay(self) -> float | None:
        d = self.parser.crawl_delay(WEB_ROBOTS_AGENT)
        return float(d) if d is not None else None

    def sitemaps(self) -> list[str]:
        """Absolute http(s) sitemap URLs. RFC 9309 wants absolute ones; some sites write
        `Sitemap: /sitemap.xml`, so resolve against robots.txt's own URL."""
        out = []
        for s in self.parser.site_maps() or []:
            u = urljoin(self.base, s.strip())
            if urlsplit(u).scheme in ("http", "https") and u not in out:
                out.append(u)
        return out


def fetch_robots(fetcher: Fetcher, origin: str) -> Robots:
    """robots.txt for `scheme://host`. 2xx: parse it; 4xx: no rules (allow all);
    5xx or no answer: disallow everything for this run (RFC 9309 §2.3.1.4)."""
    rp = urllib.robotparser.RobotFileParser()
    where = origin.rstrip("/") + "/robots.txt"
    r = fetcher.get(where, max_bytes=500_000)
    if r.ok:
        rp.parse(r.text.splitlines())
        return Robots(rp, True, base=r.url or where)
    if 400 <= r.status < 500:
        rp.allow_all = True
        return Robots(rp, True, note=f"no robots.txt (HTTP {r.status}): no rules", base=where)
    rp.disallow_all = True
    return Robots(rp, False, note=f"robots.txt unreachable ({r.error or r.status}): nothing fetched this run",
                  base=where)
