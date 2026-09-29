"""Crawl state kept in the manifest database (plan D5, D8): the queue (so an interrupted crawl
resumes), what each page was last time (for conditional GET and change detection), and URL
aliases (a page that moved keeps its Document)."""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_queue (
  source_id     TEXT NOT NULL,
  url           TEXT NOT NULL,              -- canonical form
  depth         INTEGER NOT NULL,
  status        TEXT NOT NULL DEFAULT 'queued',   -- queued | done | skipped | failed | parked
  reason        TEXT,
  fails         INTEGER NOT NULL DEFAULT 0,       -- failed runs in a row
  lastmod       TEXT,                             -- from a sitemap, when it said
  discovered_at TEXT,
  updated_at    TEXT,
  PRIMARY KEY (source_id, url)
);
CREATE INDEX IF NOT EXISTS idx_web_queue ON web_queue(source_id, status, depth);
CREATE TABLE IF NOT EXISTS web_pages (
  doc_id        TEXT PRIMARY KEY,
  source_id     TEXT NOT NULL,
  url           TEXT NOT NULL,              -- canonical URL the id was made from
  url_hash      TEXT NOT NULL,              -- its full SHA-256: a second URL with the same id is refused
  text_hash     TEXT,
  etag          TEXT,
  last_modified TEXT,
  fetched_at    TEXT,                       -- last time the page body was downloaded
  checked_at    TEXT,                       -- last time it was asked for (a 304 counts)
  rendered      INTEGER NOT NULL DEFAULT 0,
  raw_path      TEXT
);
CREATE INDEX IF NOT EXISTS idx_web_pages_text ON web_pages(text_hash);
CREATE TABLE IF NOT EXISTS web_sources (
  source_id TEXT PRIMARY KEY,
  depth     INTEGER NOT NULL                -- the depth the last run crawled with
);
CREATE TABLE IF NOT EXISTS web_videos (
  source_id TEXT NOT NULL,
  url       TEXT NOT NULL,                  -- a page embedding the talk
  video_id  TEXT NOT NULL,
  PRIMARY KEY (source_id, url, video_id)
);
CREATE TABLE IF NOT EXISTS web_aliases (
  url    TEXT PRIMARY KEY,                  -- another canonical URL with the same text
  doc_id TEXT NOT NULL,
  since  TEXT
);
"""


REOPENED = "reopened for the new depth"     # queue reason: fetch in full (its links are the point)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def days_ago(days: float, clock=time.time) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock() - days * 86400))


class WebState:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript(SCHEMA)
        db.commit()

    # ---- queue
    def enqueue(self, source_id: str, url: str, depth: int, lastmod: str | None = None) -> bool:
        """Add a URL (True if new). A shorter path to a known URL lowers its depth; a sitemap
        lastmod newer than our last fetch sends a finished page back to the queue."""
        row = self.db.execute("SELECT depth, status, lastmod FROM web_queue WHERE source_id=? AND url=?",
                              (source_id, url)).fetchone()
        t = now()
        if row is None:
            self.db.execute("INSERT INTO web_queue (source_id, url, depth, status, lastmod, discovered_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?)", (source_id, url, depth, "queued", lastmod, t, t))
            self.db.commit()
            return True
        sets, args = [], []
        if depth < row["depth"]:
            sets.append("depth=?")
            args.append(depth)
            if row["status"] == "skipped" and self._depth_skip(source_id, url):
                sets.append("status='queued'")
        if lastmod and lastmod != row["lastmod"]:
            sets.append("lastmod=?")
            args.append(lastmod)
            if row["status"] == "done" and self._fetched_before(url, lastmod):
                sets.append("status='queued'")
        if sets:
            self.db.execute(f"UPDATE web_queue SET {', '.join(sets)}, updated_at=? WHERE source_id=? AND url=?",
                            (*args, t, source_id, url))
            self.db.commit()
        return False

    def _depth_skip(self, source_id: str, url: str) -> bool:
        r = self.db.execute("SELECT reason FROM web_queue WHERE source_id=? AND url=?", (source_id, url)).fetchone()
        return bool(r and (r["reason"] or "").startswith("beyond depth"))

    def _fetched_before(self, url: str, lastmod: str) -> bool:
        r = self.page_for_url(url)
        return r is None or (r["fetched_at"] or "") < lastmod

    def next(self, source_id: str):
        return self.db.execute("SELECT * FROM web_queue WHERE source_id=? AND status='queued' "
                               "ORDER BY depth, discovered_at, url LIMIT 1", (source_id,)).fetchone()

    def finish(self, source_id: str, url: str, status: str, reason: str | None = None) -> None:
        """done | skipped: settled; a success resets the failure count."""
        self.db.execute("UPDATE web_queue SET status=?, reason=?, fails=0, updated_at=? WHERE source_id=? AND url=?",
                        (status, reason, now(), source_id, url))
        self.db.commit()

    def fail(self, source_id: str, url: str, reason: str, cap: int) -> str:
        """A failed fetch: retried next run, parked after `cap` runs in a row."""
        row = self.db.execute("SELECT fails FROM web_queue WHERE source_id=? AND url=?", (source_id, url)).fetchone()
        fails = (row["fails"] if row else 0) + 1
        status = "parked" if fails >= cap else "failed"
        self.db.execute("UPDATE web_queue SET status=?, reason=?, fails=?, updated_at=? WHERE source_id=? AND url=?",
                        (status, reason, fails, now(), source_id, url))
        self.db.commit()
        return status

    def start_run(self, source_id: str, recheck_before: str, retry_parked: bool = False,
                  depth: int | None = None) -> dict:
        """At the start of a sync: failed pages, the start page (where new links appear) and
        finished pages not checked since `recheck_before` go back in the queue. When the
        Source's depth went up since the last run, the pages at the old limit go back too:
        their links were never followed."""
        n_deeper = 0
        if depth is not None:
            row = self.db.execute("SELECT depth FROM web_sources WHERE source_id=?", (source_id,)).fetchone()
            old = row["depth"] if row else None
            if old is None:                              # first run with this table: the deepest row seen
                old = self.db.execute("SELECT MAX(depth) d FROM web_queue WHERE source_id=?",
                                      (source_id,)).fetchone()["d"]
            if old is not None and depth > old:
                n_deeper = self.db.execute(
                    f"UPDATE web_queue SET status='queued', reason='{REOPENED}' WHERE source_id=? AND depth=? AND "
                    "(status='done' OR (status='skipped' AND reason LIKE 'other%'))", (source_id, old)).rowcount
            self.db.execute("INSERT INTO web_sources (source_id, depth) VALUES (?,?) "
                            "ON CONFLICT(source_id) DO UPDATE SET depth=excluded.depth", (source_id, depth))
        n_failed = self.db.execute("UPDATE web_queue SET status='queued' WHERE source_id=? AND status IN (%s)"
                                   % ("'failed','parked'" if retry_parked else "'failed'"), (source_id,)).rowcount
        n_due = self.db.execute(
            "UPDATE web_queue SET status='queued' WHERE source_id=? AND ((depth=0 AND status IN ('done','skipped')) "
            "OR (status='done' AND COALESCE(updated_at,'') < ?))", (source_id, recheck_before)).rowcount
        self.db.commit()
        # a video page whose talks all ended without YouTube captions: its transcript is the only copy
        n_nocap = self.db.execute(
            "UPDATE web_queue SET status='queued', reason='video without captions' WHERE source_id=? "
            "AND status='done' AND reason LIKE 'video%' AND url IN (SELECT v.url FROM web_videos v "
            "WHERE v.source_id=? GROUP BY v.url HAVING MIN(EXISTS (SELECT 1 FROM stage_state s WHERE "
            "s.doc_id=v.video_id AND s.stage='fetch' AND s.status='skipped')) = 1)",
            (source_id, source_id)).rowcount if self._has_stage_state() else 0
        self.db.commit()
        return {"retry": n_failed, "recheck": n_due + n_nocap, "deeper": n_deeper}

    def _has_stage_state(self) -> bool:
        return bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='stage_state'").fetchone())

    def record_videos(self, source_id: str, url: str, video_ids: list[str]) -> None:
        self.db.executemany("INSERT OR IGNORE INTO web_videos (source_id, url, video_id) VALUES (?,?,?)",
                            [(source_id, url, v) for v in video_ids])
        self.db.commit()

    def counts(self, source_id: str) -> dict:
        return {r["status"]: r["n"] for r in self.db.execute(
            "SELECT status, COUNT(*) n FROM web_queue WHERE source_id=? GROUP BY status", (source_id,))}

    # ---- pages
    def page(self, doc_id: str):
        return self.db.execute("SELECT * FROM web_pages WHERE doc_id=?", (doc_id,)).fetchone()

    def page_for_url(self, url: str):
        r = self.db.execute("SELECT * FROM web_pages WHERE url=?", (url,)).fetchone()
        if r is None:
            a = self.db.execute("SELECT doc_id FROM web_aliases WHERE url=?", (url,)).fetchone()
            r = self.page(a["doc_id"]) if a else None
        return r

    def page_with_text(self, text_hash: str, exclude: str):
        return self.db.execute("SELECT p.* FROM web_pages p JOIN documents d USING (doc_id) WHERE p.text_hash=? "
                               "AND p.doc_id != ? ORDER BY p.fetched_at LIMIT 1", (text_hash, exclude)).fetchone()

    def save_page(self, **row) -> None:
        cols = list(row)
        self.db.execute(f"INSERT INTO web_pages ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                        f"ON CONFLICT(doc_id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols if c != 'doc_id')}",
                        tuple(row.values()))
        self.db.commit()

    def checked(self, doc_id: str) -> None:
        self.db.execute("UPDATE web_pages SET checked_at=? WHERE doc_id=?", (now(), doc_id))
        self.db.commit()

    def alias(self, url: str, doc_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO web_aliases (url, doc_id, since) VALUES (?,?,?)", (url, doc_id, now()))
        self.db.commit()

    def requeue(self, source_id: str, url: str) -> None:
        self.db.execute("UPDATE web_queue SET status='queued', updated_at=? WHERE source_id=? AND url=?",
                        (now(), source_id, url))
        if not self.db.execute("SELECT 1 FROM web_queue WHERE source_id=? AND url=?", (source_id, url)).fetchone():
            self.enqueue(source_id, url, 0)
        self.db.commit()
