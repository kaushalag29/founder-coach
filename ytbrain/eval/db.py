"""Working state of an eval build: one SQLite file, written only by the main thread.

Every expensive result (a generated question, a rewrite, a pool, one judge's grade for
one Moment) is a row keyed by what produced it, so an interrupted or budget-capped
build resumes exactly where it stopped and never pays twice. Worker threads only make
LLM calls and hand results back through a queue.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
  qid TEXT PRIMARY KEY, split TEXT NOT NULL, sort_key INTEGER NOT NULL,
  status TEXT NOT NULL,             -- seeded | generated | rejected | accepted | discarded | failed
  text TEXT, seed_moment TEXT, record TEXT NOT NULL, reason TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS rewrites (qid TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rewrite_failures (qid TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pools (
  qid TEXT NOT NULL, moment_id TEXT NOT NULL, variants TEXT NOT NULL, best_rank INTEGER,
  PRIMARY KEY (qid, moment_id)
);
CREATE TABLE IF NOT EXISTS pooled (qid TEXT PRIMARY KEY, depth INTEGER, n INTEGER);
CREATE TABLE IF NOT EXISTS grades (
  qid TEXT NOT NULL, moment_id TEXT NOT NULL, judge TEXT NOT NULL, prompt_ver TEXT NOT NULL,
  grade INTEGER NOT NULL, ts REAL, PRIMARY KEY (qid, moment_id, judge, prompt_ver)
);
CREATE TABLE IF NOT EXISTS answers (qid TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spend (ts REAL, build TEXT, model TEXT, kind TEXT, cost REAL);
"""


class EvalDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # -- questions -------------------------------------------------------------
    def upsert_question(self, qid: str, split: str, sort_key: int, status: str, record: dict,
                        text: str | None = None, seed_moment: str | None = None,
                        reason: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO questions(qid, split, sort_key, status, text, seed_moment, record, reason,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(qid) DO UPDATE SET"
            " status=excluded.status, text=excluded.text, seed_moment=excluded.seed_moment,"
            " record=excluded.record, reason=excluded.reason, updated_at=excluded.updated_at",
            (qid, split, sort_key, status, text, seed_moment, json.dumps(record), reason, time.time()))
        self.db.commit()

    def set_status(self, qid: str, status: str, reason: str | None = None) -> None:
        self.db.execute("UPDATE questions SET status=?, reason=?, updated_at=? WHERE qid=?",
                        (status, reason, time.time(), qid))
        self.db.commit()

    def questions(self, split: str, status: tuple[str, ...] | None = None) -> list[dict]:
        sql = "SELECT * FROM questions WHERE split=?"
        args: list = [split]
        if status:
            sql += f" AND status IN ({','.join('?' * len(status))})"
            args += list(status)
        rows = self.db.execute(sql + " ORDER BY sort_key", args).fetchall()
        return [{**dict(r), "record": json.loads(r["record"])} for r in rows]

    # -- rewrites / pools ------------------------------------------------------
    def rewrite(self, qid: str) -> str | None:
        r = self.db.execute("SELECT text FROM rewrites WHERE qid=?", (qid,)).fetchone()
        return r["text"] if r else None

    def save_rewrite(self, qid: str, text: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO rewrites VALUES (?,?)", (qid, text))
        self.db.commit()

    def rewrite_failed(self, qid: str) -> None:
        self.db.execute("INSERT INTO rewrite_failures VALUES (?,1) ON CONFLICT(qid) DO UPDATE SET n=n+1", (qid,))
        self.db.commit()

    def rewrite_failures(self, qid: str) -> int:
        r = self.db.execute("SELECT n FROM rewrite_failures WHERE qid=?", (qid,)).fetchone()
        return r["n"] if r else 0

    def is_pooled(self, qid: str) -> bool:
        return self.db.execute("SELECT 1 FROM pooled WHERE qid=?", (qid,)).fetchone() is not None

    def save_pool(self, qid: str, entries: dict[str, tuple[list[str], int]], depth: int) -> None:
        """Replace a question's pool in one transaction, then mark it complete."""
        with self.db:
            self.db.execute("DELETE FROM pools WHERE qid=?", (qid,))
            self.db.executemany("INSERT INTO pools VALUES (?,?,?,?)",
                                [(qid, m, ",".join(v), r) for m, (v, r) in entries.items()])
            self.db.execute("INSERT OR REPLACE INTO pooled VALUES (?,?,?)", (qid, depth, len(entries)))

    def add_to_pool(self, qid: str, ranked: list[tuple[str, int]], variant: str) -> int:
        """Add Moments a new system retrieved (pool extension, eval-spec §6); existing
        entries keep their variants and rank. Returns how many were new."""
        with self.db:
            before = self.db.execute("SELECT COUNT(*) FROM pools WHERE qid=?", (qid,)).fetchone()[0]
            self.db.executemany("INSERT OR IGNORE INTO pools VALUES (?,?,?,?)",
                                [(qid, m, variant, 1000 + r) for m, r in ranked])
            after = self.db.execute("SELECT COUNT(*) FROM pools WHERE qid=?", (qid,)).fetchone()[0]
            self.db.execute("UPDATE pooled SET n=? WHERE qid=?", (after, qid))
        return after - before

    def pool(self, qid: str) -> list[str]:
        return [r["moment_id"] for r in self.db.execute(
            "SELECT moment_id FROM pools WHERE qid=? ORDER BY best_rank, moment_id", (qid,))]

    # -- grades ----------------------------------------------------------------
    def save_grades(self, qid: str, judge: str, prompt_ver: str, grades: dict[str, int]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO grades VALUES (?,?,?,?,?,?)",
                                [(qid, m, judge, prompt_ver, int(g), time.time())
                                 for m, g in grades.items()])

    def grades(self, qid: str, prompt_ver: str) -> dict[str, dict[str, int]]:
        """{moment_id: {judge: grade}}"""
        out: dict[str, dict[str, int]] = {}
        for r in self.db.execute("SELECT moment_id, judge, grade FROM grades WHERE qid=? AND prompt_ver=?",
                                 (qid, prompt_ver)):
            out.setdefault(r["moment_id"], {})[r["judge"]] = r["grade"]
        return out

    # -- answers / spend -------------------------------------------------------
    def save_answer(self, qid: str, record: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO answers VALUES (?,?)", (qid, json.dumps(record)))
        self.db.commit()

    def answer(self, qid: str) -> dict | None:
        r = self.db.execute("SELECT record FROM answers WHERE qid=?", (qid,)).fetchone()
        return json.loads(r["record"]) if r else None

    def add_spend(self, build: str, model: str, kind: str, cost: float) -> None:
        self.db.execute("INSERT INTO spend VALUES (?,?,?,?,?)", (time.time(), build, model, kind, cost))
        self.db.commit()

    def spent(self, build: str | None = None) -> float:
        sql, args = "SELECT COALESCE(SUM(cost),0) AS c FROM spend", ()
        if build:
            sql, args = sql + " WHERE build=?", (build,)
        return float(self.db.execute(sql, args).fetchone()["c"])
