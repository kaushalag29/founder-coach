"""URL canonicalization, Document ids and crawl scope (plan D5, D7). Stdlib only.

The id of a page is derived from an *identity key* that ignores what doesn't change the page:
the scheme (http vs https), a leading `www.`, the fragment, tracking parameters and query
order. The full SHA-256 is kept so two different keys can never share an id silently.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

TRACKING = re.compile(r"^(utm_[a-z0-9_]+|gclid|dclid|gbraid|wbraid|fbclid|msclkid|yclid|twclid|igshid|"
                      r"mc_cid|mc_eid|_ga|_gl|_hsenc|_hsmi|mkt_tok|ref_src|li_fat_id|s_cid|oly_[a-z_]+)$", re.I)
_SAFE_PATH = "/%:@!$&'()*+,;=-._~"
ID_PREFIX = "w-"
ID_HEX = 16


def canonicalize(url: str) -> str:
    """http(s) only; lowercase scheme and host (IDNA), no default port, no fragment,
    tracking parameters dropped, query sorted, path percent-encoding normalized."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"not an http(s) URL: {url!r}")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError(f"no host in URL: {url!r}")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError(f"bad host in URL: {url!r}") from None
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"bad port in URL: {url!r}") from None
    netloc = host if port is None or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    path = quote(unquote(path), safe=_SAFE_PATH)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not TRACKING.match(k)))
    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def site_host(url: str) -> str:
    """The host with `www.` removed: www.example.com and example.com are one site."""
    return host_of(url).removeprefix("www.")


def identity_key(canonical: str) -> str:
    p = urlsplit(canonical)
    host = p.netloc.lower().removeprefix("www.")
    return f"//{host}{p.path}" + (f"?{p.query}" if p.query else "")


def url_hash(url: str) -> str:
    """Full SHA-256 of the identity key (the URL is canonicalized first, so any spelling works)."""
    return hashlib.sha256(identity_key(canonicalize(url)).encode("utf-8")).hexdigest()


def doc_id(canonical: str) -> str:
    """The Document id of a page: `w-` + the first 16 hex characters of its identity hash."""
    return ID_PREFIX + url_hash(canonical)[:ID_HEX]


def scope_prefix(start_url: str) -> str:
    """The path a crawl stays under: the start URL's directory ("/library" -> "/library/",
    "/articles.html" -> "/", "/blog/" -> "/blog/")."""
    path = urlsplit(start_url).path or "/"
    if path.endswith("/"):
        return path
    last = path.rsplit("/", 1)[-1]
    return posixpath.dirname(path).rstrip("/") + "/" if "." in last else path + "/"


def in_scope(url: str, start_url: str) -> bool:
    """Same site (www-insensitive) and under the start URL's directory."""
    if site_host(url) != site_host(start_url) or urlsplit(url).scheme not in ("http", "https"):
        return False
    prefix, path = scope_prefix(start_url), urlsplit(url).path or "/"
    return path == prefix.rstrip("/") or path.startswith(prefix) or prefix == "/"


def absolute(base: str, href: str) -> str | None:
    """A link resolved against its page, canonicalized; None for mailto:, javascript:, etc."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    try:
        return canonicalize(urljoin(base, href))
    except ValueError:
        return None
