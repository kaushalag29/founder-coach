"""Per-stage checkpoint manifest (SQLite, stdlib only).

The design decision that matters (research doc 7.4): checkpointing is per
*stage*, not per document. A document carries an independent hash+state for
fetch / clean / extract / verify / index / graph, so:

  * new embedding model  -> invalidate `index` only, no re-crawl, no re-extract
  * new metadata schema  -> invalidate `extract` onward, no re-crawl
  * edited captions      -> fetch hash changes, everything downstream cascades
  * deleted video        -> tombstone, and downstream artefacts get purged

A manifest that only records "seen this id" cannot express any of that, which
is why `--download-archive` alone is not enough.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

STAGES = ("fetch", "clean", "extract", "verify", "index", "graph")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id        TEXT PRIMARY KEY,
  source_id     TEXT NOT NULL,
  doc_type      TEXT NOT NULL DEFAULT 'youtube',
  url           TEXT,
  title         TEXT,
  series        TEXT,
  provenance    TEXT,
  published_at  TEXT,
  duration_s    INTEGER,
  has_captions  INTEGER,
  caption_kind  TEXT,            -- 'human' | 'auto' | 'asr' | 'none'
  tombstoned_at TEXT,            -- set when the video disappears upstream
  first_seen    TEXT,
  last_seen     TEXT
);

CREATE TABLE IF NOT EXISTS stage_state (
  doc_id        TEXT NOT NULL,
  stage         TEXT NOT NULL,
  status        TEXT NOT NULL,   -- ok | failed | stale | skipped
  input_hash    TEXT,            -- hash of what this stage consumed
  output_hash   TEXT,
  version       TEXT,            -- model/schema/prompt version for this stage
  attempts      INTEGER DEFAULT 0,
  error         TEXT,
  updated_at    TEXT,
  PRIMARY KEY (doc_id, stage)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  command    TEXT,
  started_at TEXT,
  ended_at   TEXT,
  status     TEXT,            -- ok | failed
  summary    TEXT,
  error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_stage_status ON stage_state(stage, status);
CREATE INDEX IF NOT EXISTS idx_docs_source  ON documents(source_id);
"""


@dataclass
class StageState:
    doc_id: str
    stage: str
    status: str
    input_hash: Optional[str] = None
    output_hash: Optional[str] = None
    version: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None


class Manifest:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # ---------- documents ----------

    def upsert_document(self, doc_id: str, source_id: str, is_fallback: bool = False,
                        **fields) -> None:
        """Insert or update a document row.

        `is_fallback=True` marks a catch-all source (the channel uploads
        playlist). A video reached through both a specific playlist and the
        uploads catch-all must keep the specific series: a CS183B lecture
        belongs to CS183B more than it belongs to "all uploads". Without this
        guard the last writer wins and series assignment is effectively
        arbitrary (decision Q15).
        """
        now = _now()
        if is_fallback:
            existing = self.get_document(doc_id)
            if existing and existing["series"]:
                fields.pop("series", None)
                fields.pop("provenance", None)
                fields.pop("source_id", None)
        cols = {"doc_id": doc_id, "source_id": source_id, "last_seen": now, **fields}
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{k}=excluded.{k}" for k in cols if k != "doc_id")
        self.db.execute(
            f"INSERT INTO documents ({keys}, first_seen) VALUES ({marks}, ?) "
            f"ON CONFLICT(doc_id) DO UPDATE SET {updates}",
            (*cols.values(), now),
        )
        self.db.commit()

    def get_document(self, doc_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()

    def known_ids(self, source_id: Optional[str] = None) -> set[str]:
        q = "SELECT doc_id FROM documents WHERE tombstoned_at IS NULL"
        args: tuple = ()
        if source_id:
            q += " AND source_id=?"
            args = (source_id,)
        return {r["doc_id"] for r in self.db.execute(q, args)}

    def stage_settled_ids(self, stage: str,
                          statuses: tuple[str, ...] = ("ok", "skipped")) -> set[str]:
        """doc_ids whose `stage` reached a terminal state.

        NOT interchangeable with known_ids(): a document row is written at
        enumeration, before any work runs, so presence in `documents` says
        nothing about whether the stage completed. An interrupted run leaves
        no stage_state row at all, so the doc is correctly retried. 'skipped'
        counts as settled -- a video with no caption track will not grow one
        by being re-requested every run.
        """
        marks = ",".join("?" * len(statuses))
        return {r["doc_id"] for r in self.db.execute(
            f"SELECT doc_id FROM stage_state WHERE stage=? AND status IN ({marks})",
            (stage, *statuses))}

    def tombstone_missing(self, source_id: str, seen_ids: Iterable[str]) -> list[str]:
        """Weekly reconciliation: anything we know about that upstream no longer
        lists has been deleted or privatised. Returns the ids so the caller can
        purge chunks and graph edges - the failure mode a push-only design
        structurally cannot detect (research doc 4.5)."""
        seen = set(seen_ids)
        gone = [d for d in self.known_ids(source_id) if d not in seen]
        for doc_id in gone:
            self.db.execute("UPDATE documents SET tombstoned_at=? WHERE doc_id=?", (_now(), doc_id))
            self.db.execute("UPDATE stage_state SET status='stale' WHERE doc_id=?", (doc_id,))
        self.db.commit()
        return gone

    def tombstone(self, doc_ids: Iterable[str]) -> int:
        """A Document gone upstream (a page answering 404/410): every Step goes stale, so the
        next `index` drops its items; its files stay, so a return costs no re-extraction."""
        n = 0
        for doc_id in doc_ids:
            n += self.db.execute("UPDATE documents SET tombstoned_at=? WHERE doc_id=? AND tombstoned_at IS NULL",
                                 (_now(), doc_id)).rowcount
            self.db.execute("UPDATE stage_state SET status='stale', error='gone upstream' WHERE doc_id=?", (doc_id,))
        self.db.commit()
        return n

    def restore(self, doc_id: str) -> bool:
        """A tombstoned Document came back upstream: live again, and clean onward re-runs."""
        n = self.db.execute("UPDATE documents SET tombstoned_at=NULL WHERE doc_id=? AND tombstoned_at IS NOT NULL",
                            (doc_id,)).rowcount
        self.db.commit()
        return bool(n)

    # ---------- stages ----------

    def stage(self, doc_id: str, stage: str) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM stage_state WHERE doc_id=? AND stage=?", (doc_id, stage)
        ).fetchone()

    def needs(self, doc_id: str, stage: str, input_hash: str, version: str = "") -> bool:
        """True when this stage must (re-)run: never run, previously failed, or
        its inputs/version changed."""
        row = self.stage(doc_id, stage)
        if row is None or row["status"] != "ok":
            return True
        return row["input_hash"] != input_hash or (row["version"] or "") != version

    def mark(self, st: StageState) -> None:
        self.db.execute(
            "INSERT INTO stage_state (doc_id, stage, status, input_hash, output_hash, version,"
            " attempts, error, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(doc_id, stage) DO UPDATE SET status=excluded.status,"
            " input_hash=excluded.input_hash, output_hash=excluded.output_hash,"
            " version=excluded.version, attempts=stage_state.attempts+excluded.attempts,"
            " error=excluded.error, updated_at=excluded.updated_at",
            (st.doc_id, st.stage, st.status, st.input_hash, st.output_hash,
             st.version, st.attempts, st.error, _now()),
        )
        self.db.commit()

    def invalidate_old_versions(self, stage: str, version: str) -> int:
        """Mark 'ok' rows produced under a different version as stale.

        pending() only looks at status, so without this a SCHEMA_VERSION bump
        silently left every old record in place."""
        cur = self.db.execute(
            "UPDATE stage_state SET status='stale', error=? WHERE stage=? AND status='ok'"
            " AND COALESCE(version, '') != ?",
            (f"version changed to {version}", stage, version))
        self.db.commit()
        return cur.rowcount

    def stage_status(self, doc_id: str, stage: str) -> str | None:
        row = self.db.execute("SELECT status FROM stage_state WHERE doc_id=? AND stage=?",
                              (doc_id, stage)).fetchone()
        return row["status"] if row else None

    def flagged_ids(self) -> list[str]:
        """Docs whose last verify found unsupported evidence (flagged or failed)."""
        return [r["doc_id"] for r in self.db.execute(
            "SELECT doc_id FROM stage_state WHERE stage='verify' AND status='ok'"
            " AND (error LIKE 'flagged%' OR error LIKE 'failed%')")]

    def invalidate_docs(self, stage: str, doc_ids: list[str], reason: str = "") -> int:
        """Targeted re-run: mark just these docs stale at `stage`."""
        n = 0
        for d in doc_ids:
            n += self.db.execute(
                "UPDATE stage_state SET status='stale', error=? WHERE stage=? AND doc_id=?",
                (reason or None, stage, d)).rowcount
        self.db.commit()
        return n

    def invalidate_stage(self, stage: str, reason: str = "") -> int:
        """Bulk re-run trigger: `ytbrain invalidate index` after swapping the
        embedding model. Cheap precisely because stages are independent."""
        cur = self.db.execute(
            "UPDATE stage_state SET status='stale', error=? WHERE stage=? AND status='ok'",
            (reason or None, stage),
        )
        self.db.commit()
        return cur.rowcount

    def pending(self, stage: str, limit: int = 0) -> list[str]:
        q = ("SELECT d.doc_id FROM documents d LEFT JOIN stage_state s"
             " ON s.doc_id=d.doc_id AND s.stage=?"
             # 'skipped' is terminal for a Step (no captions, too short): offering it
             # again every run only re-does settled work. A changed input re-opens it
             # explicitly (sync marks clean stale after a successful re-fetch).
             # 'parked': failed STEP_FAILURE_CAP runs in a row (or permanently); only
             # `--retry-failed` brings it back, so one broken talk can't burn every run
             " WHERE d.tombstoned_at IS NULL AND (s.status IS NULL OR s.status NOT IN ('ok','skipped','parked'))"
             " ORDER BY d.published_at DESC")
        if limit:
            q += f" LIMIT {int(limit)}"
        return [r["doc_id"] for r in self.db.execute(q, (stage,))]

    def fail(self, doc_id: str, stage: str, error: str, permanent: bool = False,
             cap: int | None = None) -> str:
        """Record a failed Step for one Document; returns 'failed' or 'parked'. `attempts` here
        counts failures in a row (a success resets it), so the cap means consecutive runs."""
        from .config import STEP_FAILURE_CAP
        prev = self.stage(doc_id, stage)
        in_a_row = prev is not None and prev["status"] in ("failed", "parked")
        n = ((prev["attempts"] or 0) if in_a_row else 0) + 1
        status = "parked" if permanent or n >= (cap or STEP_FAILURE_CAP) else "failed"
        self.db.execute(
            "INSERT INTO stage_state (doc_id, stage, status, attempts, error, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(doc_id, stage) DO UPDATE SET status=excluded.status,"
            " attempts=excluded.attempts, error=excluded.error, updated_at=excluded.updated_at",
            (doc_id, stage, status, n, (error or "")[:500], _now()))
        self.db.commit()
        return status

    def succeeded(self, doc_id: str, stage: str) -> None:
        """Reset the failures-in-a-row count after a successful Step."""
        self.db.execute("UPDATE stage_state SET attempts=0 WHERE doc_id=? AND stage=?", (doc_id, stage))
        self.db.commit()

    def unpark(self, stage: str) -> int:
        """`--retry-failed`: give parked Documents another go (their count restarts)."""
        n = self.db.execute("UPDATE stage_state SET status='failed', attempts=0 "
                            "WHERE stage=? AND status='parked'", (stage,)).rowcount
        self.db.commit()
        return n

    def parked(self, stage: str) -> int:
        return self.db.execute("SELECT COUNT(*) c FROM stage_state WHERE stage=? AND status='parked'",
                               (stage,)).fetchone()["c"]

    def stats(self) -> dict:
        out = {"documents": self.db.execute(
            "SELECT COUNT(*) c FROM documents WHERE tombstoned_at IS NULL").fetchone()["c"],
            "tombstoned": self.db.execute(
            "SELECT COUNT(*) c FROM documents WHERE tombstoned_at IS NOT NULL").fetchone()["c"]}
        for stage in STAGES:
            row = self.db.execute(
                "SELECT COUNT(*) c FROM stage_state WHERE stage=? AND status='ok'", (stage,)
            ).fetchone()
            out[stage] = row["c"]
        return out

    def close_abandoned_runs(self) -> int:
        """Runs still 'running' when a new one starts under the run lock died without a
        goodbye (SIGKILL, power loss): record that instead of 'running' forever."""
        n = self.db.execute("UPDATE runs SET status='abandoned', ended_at=?, "
                            "error=COALESCE(error, 'process ended without finishing (killed or power loss)') "
                            "WHERE status='running'", (_now(),)).rowcount
        self.db.commit()
        return n

    def start_run(self, command: str) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (command, started_at, status) VALUES (?,?,'running')",
            (command, _now()))
        self.db.commit()
        return cur.lastrowid

    def end_run(self, run_id: int, status: str, summary: str = "", error: str = "") -> None:
        """Scheduled jobs fail silently by default; recording the outcome is what
        lets `ytbrain report` surface a run that has been broken for a month."""
        self.db.execute(
            "UPDATE runs SET ended_at=?, status=?, summary=?, error=? WHERE run_id=?",
            (_now(), status, summary or None, error or None, run_id))
        self.db.commit()

    def last_runs(self, limit: int = 5) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,))]

    def documents(self, where: str = "", args: tuple = ()) -> list[dict]:
        q = "SELECT * FROM documents WHERE tombstoned_at IS NULL"
        if where:
            q += f" AND {where}"
        return [dict(r) for r in self.db.execute(q, args)]

    def tombstoned_ids(self) -> set[str]:
        """Documents removed from their Source (kept as tombstones)."""
        return {r[0] for r in self.db.execute("SELECT doc_id FROM documents WHERE tombstoned_at IS NOT NULL")}

    def export(self) -> str:
        return json.dumps(self.stats(), indent=2)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
