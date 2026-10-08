"""The founder store (ADR-0006, ADR-0011): what the coach remembers about one Founder.

Current-state tables plus one append-only `changes` log. Every write:
  - is a single `BEGIN IMMEDIATE` transaction that includes its log rows, so several host
    processes (and the SessionStart hook) can share the file safely (WAL, busy_timeout);
  - can carry a `request_id`: a retried write returns the first result instead of writing twice;
  - is followed by a regenerated, human-readable FOUNDER.md.
Backups use SQLite's backup API: daily on the first write (7 kept), before every migration
and before `forget`. Nothing here imports numpy, MCP or the models, so the hook stays fast.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import domain as D
from . import product

SCHEMA_VERSION = 5
MIGRATIONS: dict[int, list[str]] = {
    1: [
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE profile_facts (id INTEGER PRIMARY KEY, founder_id TEXT NOT NULL, field TEXT NOT NULL,
             value TEXT NOT NULL, valid_from TEXT NOT NULL, superseded_at TEXT, confirmed_at TEXT NOT NULL,
             source TEXT NOT NULL)""",
        "CREATE INDEX profile_current ON profile_facts (founder_id, field, superseded_at)",
        """CREATE TABLE goals (id TEXT PRIMARY KEY, founder_id TEXT NOT NULL, text TEXT NOT NULL,
             measure TEXT, target_date TEXT, status TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]',
             note TEXT, created_at TEXT NOT NULL, closed_at TEXT)""",
        """CREATE TABLE commitments (id TEXT PRIMARY KEY, founder_id TEXT NOT NULL, week TEXT NOT NULL,
             action TEXT NOT NULL, cue TEXT, outcome TEXT NOT NULL, goal_id TEXT, citations TEXT NOT NULL DEFAULT '[]',
             status TEXT NOT NULL, carried_from TEXT, result_note TEXT, created_at TEXT NOT NULL, closed_at TEXT)""",
        "CREATE INDEX commitments_week ON commitments (founder_id, week, status)",
        """CREATE TABLE decisions (id TEXT PRIMARY KEY, founder_id TEXT NOT NULL, text TEXT NOT NULL,
             reasoning TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]', decided_at TEXT NOT NULL,
             revisit_on TEXT)""",
        """CREATE TABLE checkins (id TEXT PRIMARY KEY, founder_id TEXT NOT NULL, week TEXT NOT NULL,
             summary TEXT NOT NULL, wins TEXT NOT NULL DEFAULT '[]', blockers TEXT NOT NULL DEFAULT '[]',
             created_at TEXT NOT NULL)""",
        """CREATE TABLE changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, founder_id TEXT NOT NULL,
             entity TEXT NOT NULL, entity_id TEXT NOT NULL, op TEXT NOT NULL, before TEXT, after TEXT,
             source TEXT NOT NULL)""",
        """CREATE TABLE requests (request_id TEXT PRIMARY KEY, at TEXT NOT NULL, result TEXT NOT NULL)""",
    ],
    # v2: a request_id is bound to the write it was first used for; reusing it for a
    # different write is refused instead of silently replaying the wrong result
    2: ["ALTER TABLE requests ADD COLUMN fingerprint TEXT"],
    # v3: Feedback, the Founder's reports of a wrong answer or record (turned into eval cases)
    3: ["""CREATE TABLE feedback (id TEXT PRIMARY KEY, founder_id TEXT NOT NULL, created_at TEXT NOT NULL,
             category TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, cited TEXT NOT NULL DEFAULT '[]',
             searches TEXT NOT NULL DEFAULT '[]', note TEXT, expected TEXT, context TEXT NOT NULL DEFAULT '{}')"""],
    # v4: the usage log (docs/usage-log.md): one row per coach tool call, no Founder text by default
    4: ["""CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, founder_id TEXT NOT NULL, at TEXT NOT NULL,
             run TEXT NOT NULL, tool TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL,
             version TEXT, detail TEXT NOT NULL DEFAULT '{}')""",
        "CREATE INDEX usage_at ON usage (founder_id, at)"],
    # v5: Holdings (the investor Pack, M6g): one snapshot per account and date, its positions in cents, and the
    # person's own asset-class label per symbol. Every store gets the tables; only a Pack with `holdings` uses them.
    5: ["""CREATE TABLE holdings_snapshots (id TEXT PRIMARY KEY, founder_id TEXT NOT NULL, account TEXT NOT NULL,
             as_of TEXT NOT NULL, imported_at TEXT NOT NULL, broker TEXT NOT NULL, sha256 TEXT NOT NULL,
             source TEXT NOT NULL, total_cents INTEGER NOT NULL)""",
        "CREATE UNIQUE INDEX holdings_once ON holdings_snapshots (founder_id, account, as_of, sha256)",
        """CREATE TABLE positions (snapshot_id TEXT NOT NULL, symbol TEXT NOT NULL, description TEXT,
             quantity TEXT, value_cents INTEGER NOT NULL, csv_class TEXT)""",
        "CREATE INDEX positions_snapshot ON positions (snapshot_id)",
        """CREATE TABLE asset_classes (founder_id TEXT NOT NULL, symbol TEXT NOT NULL, asset_class TEXT NOT NULL,
             set_at TEXT NOT NULL, PRIMARY KEY (founder_id, symbol))"""],
}
TABLES = ("profile_facts", "goals", "commitments", "decisions", "checkins", "feedback", "changes",
          "holdings_snapshots", "positions", "asset_classes")
# the usage log is kept apart from TABLES: it isn't coaching memory, so it never makes the store
# "have data" (daily backups) and never shows in FOUNDER.md; forget, export and restore still cover it
USAGE_OUTCOMES = ("ok", "empty", "gap", "error")
USAGE_BUSY_MS = 250                  # a usage write waits this long for a busy store, then gives up
DAILY_BACKUPS_KEPT = 7
WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


class StoreError(ValueError):
    """A request the store can't carry out; the message says how to fix it."""


class StoreDamaged(StoreError):
    """The store file can't be read or fails its integrity check. Nothing is deleted:
    `founder-coach restore` sets the file aside and copies a backup in."""


RESTORE_HINT = f"Nothing has been deleted: run `{product.ID} restore` to set it aside and restore the latest good backup."


def _utc_now() -> dt.datetime:
    # FOUNDER_COACH_FAKE_NOW (ISO datetime) is for the coach evaluation only: it lets a
    # multi-week persona run through its weeks in minutes. Never set it for real use.
    fake = product.env("FAKE_NOW")
    if fake:
        t = dt.datetime.fromisoformat(fake)
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def iso_week(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_start(week: str) -> dt.date:
    m = WEEK_RE.match(week or "")
    if not m:
        raise StoreError(f"week must look like 2026-W39, got {week!r}")
    return dt.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)


def next_week(week: str) -> str:
    return iso_week(week_start(week) + dt.timedelta(days=7))


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


class FounderStore:
    """One store file. A Project's memory is `founder.db` (with FOUNDER.md); the Common profile the coaches
    share is the same kind of store named `you` (you.db, YOU.md) that keeps only D.COMMON_FIELDS (ADR-0016).
    A Project store given `common` reads the shared facts it doesn't set itself (`common_fields`)."""

    def __init__(self, home: str | Path | None = None, founder_id: str = "me",
                 clock: Callable[[], dt.datetime] | None = None, name: str = "founder",
                 fields: tuple[str, ...] | None = None):
        self.home = Path(home).expanduser() if home else D.home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.path = self.home / f"{name}.db"
        self.backups = self.home / "backups"
        self.md_path = self.home / f"{name.upper()}.md"
        self.fields = tuple(fields) if fields else tuple(D.PROFILE_FIELDS)
        self.common: FounderStore | None = None          # the Common profile (multi-Project mode only)
        self.common_fields: tuple[str, ...] = ()         # the shared facts this Pack reads from it
        self.fid = founder_id
        self.clock = clock or _utc_now
        self.cite_check: Callable[[list[str]], list[str]] | None = None    # -> unknown ids
        self.cite_lookup: Callable[[str], dict | None] | None = None       # id -> {title, deep_link}
        self._lock = threading.RLock()
        self._open()

    def _open(self) -> None:
        existed = self.path.exists() and self.path.stat().st_size > 0
        self._conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._file = _file_key(self.path)
        try:
            self._conn.execute("PRAGMA busy_timeout=5000")
            # Switching a new file to WAL needs a moment alone with it, and SQLite answers "locked" at once
            # instead of waiting: two coaches opening the shared Common profile together retry for a while.
            for attempt in range(50):
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e).lower() or attempt == 49:
                        raise
                    time.sleep(0.1)
            self._migrate(existed)
        except sqlite3.DatabaseError as e:
            self._conn.close()
            if isinstance(e, sqlite3.OperationalError) and "locked" in str(e).lower():
                raise                                      # busy, not damaged: the caller may retry
            raise StoreDamaged(f"the founder store at {self.path} can't be read ({e}). {RESTORE_HINT}") from e
        except StoreError:
            self._conn.close()
            raise

    @property
    def db(self) -> sqlite3.Connection:
        """The connection, reopened first if a restore renamed another file into place (the
        old one is set aside): a running session reads and writes the restored store, never
        the file that was moved away. Never mid-transaction."""
        if not self._conn.in_transaction:
            self._follow_replaced_file()
        return self._conn

    def _follow_replaced_file(self) -> None:
        key = _file_key(self.path)
        if key is not None and key != self._file:
            import logging
            logging.getLogger("founder_coach").warning("the founder store was replaced (restore); reopening it")
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._open()

    def check(self) -> dict:
        """SQLite's full integrity check: {"ok": bool, "detail": "ok" or the first problems}."""
        with self._lock:
            return _integrity(self.db)

    # ------------------------------------------------------------------ plumbing
    def _version(self) -> int:
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            return 0
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0

    def _migrate(self, existed: bool) -> None:
        with self._lock:
            v = self._version()
            if v > SCHEMA_VERSION:
                raise StoreError(f"the founder store at {self.path} is schema v{v}, newer than this coach "
                                 f"(v{SCHEMA_VERSION}): update the coach")
            if v == SCHEMA_VERSION:
                return
            if existed and v > 0:
                self.backup(f"pre-migration-v{v}")
            self.db.execute("BEGIN EXCLUSIVE")
            try:
                v = self._version()                  # another process may have migrated meanwhile
                for n in range(v + 1, SCHEMA_VERSION + 1):
                    for stmt in MIGRATIONS[n]:
                        self.db.execute(stmt)
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(n),))
                self.db.execute("COMMIT")
            except BaseException:
                _rollback(self.db)
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def now(self) -> dt.datetime:
        return self.clock().astimezone(dt.timezone.utc)

    def _iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def tz(self) -> ZoneInfo:
        name = self._current_value("timezone") or self._common_value("timezone") or D.system_timezone()
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def today(self) -> dt.date:
        return self.now().astimezone(self.tz()).date()

    def this_week(self) -> str:
        return iso_week(self.today())

    def _new_id(self, kind: str) -> str:
        table = {"goal": "goals", "commitment": "commitments", "decision": "decisions", "checkin": "checkins",
                 "feedback": "feedback", "holdings": "holdings_snapshots"}[kind]
        while True:
            rid = f"{D.ID_PREFIX[kind]}-{secrets.token_hex(2)}"
            if not self.db.execute(f"SELECT 1 FROM {table} WHERE id=?", (rid,)).fetchone():
                return rid

    def _change(self, entity: str, entity_id: str, op: str, before, after, source: str) -> None:
        self.db.execute("INSERT INTO changes (at, founder_id, entity, entity_id, op, before, after, source) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (self._iso(), self.fid, entity, entity_id, op,
                         None if before is None else _j(before), None if after is None else _j(after), source))

    def _transact(self, request_id: str | None, fn: Callable[[], dict], what: object = None) -> dict:
        """Run `fn` -- validation and write together -- in one write transaction. A repeated
        request_id returns the first result before anything is re-validated (a retried carry
        replays; it doesn't fail as "already carried"), and only for the same write: `what`
        (the operation and its arguments) is fingerprinted with the id."""
        if request_id is not None and not (1 <= len(request_id) <= 100):
            raise StoreError("request_id must be 1-100 characters; reuse the same one when retrying")
        fp = hashlib.sha256(_j(what).encode()).hexdigest()[:16] if what is not None else None
        with self._lock:
            self._backup_if_due()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if request_id:
                    row = self.db.execute("SELECT result, fingerprint FROM requests WHERE request_id=?",
                                          (request_id,)).fetchone()
                    if row:
                        if fp and row["fingerprint"] and row["fingerprint"] != fp:
                            raise StoreError(f"request_id {request_id!r} was already used for a different "
                                             "write; use a fresh id for a new write (reuse it only to retry "
                                             "the same one)")
                        self.db.execute("COMMIT")
                        return {**json.loads(row[0]), "replayed": True}
                out = fn()
                if request_id:
                    self.db.execute("INSERT INTO requests (request_id, at, result, fingerprint) VALUES (?,?,?,?)",
                                    (request_id, self._iso(), _j(out), fp))
                self.db.execute("COMMIT")
            except BaseException:
                _rollback(self.db)
                raise
            self._write_markdown()
        return out

    # ------------------------------------------------------------------ backups
    def backup(self, label: str) -> Path | None:
        """A consistent copy via SQLite's backup API (safe while others read or write)."""
        with self._lock:
            if not self._has_data():
                return None
            self.backups.mkdir(parents=True, exist_ok=True)
            dest = self.backups / f"{self.name}-{label}.db"
            # unique per process: two hosts may take the same daily backup at the same moment
            tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
            try:
                out = sqlite3.connect(tmp)
                try:
                    self.db.backup(out)
                finally:
                    out.close()
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
            return dest

    def _has_data(self) -> bool:
        try:
            return any(self.db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in TABLES)
        except sqlite3.OperationalError:
            return False

    def _backup_if_due(self) -> None:
        """The day's first write takes a backup. A failed backup is logged, never a failed write."""
        day = self.today().isoformat()                 # the Founder's day, not UTC's
        if (self.backups / f"{self.name}-daily-{day}.db").exists():
            return
        try:
            if self.backup(f"daily-{day}"):
                daily = sorted(self.backups.glob(f"{self.name}-daily-*.db"))
                for old in daily[:-DAILY_BACKUPS_KEPT]:
                    old.unlink(missing_ok=True)
        except (OSError, sqlite3.Error) as e:
            import logging
            logging.getLogger("founder_coach").warning("daily backup failed: %s", e)

    # ------------------------------------------------------------------ validation
    def _check_citations(self, ids) -> list[str]:
        ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
        if len(ids) > 10:
            raise StoreError("at most 10 citations per record")
        if ids and self.cite_check:
            unknown = self.cite_check(ids)
            if unknown:
                raise StoreError(f"unknown citation id(s): {', '.join(unknown)}. Cite only item_ids that "
                                 f"coach_search or coach_read returned in this conversation.")
        return list(dict.fromkeys(ids))

    @staticmethod
    def _text(value, name: str, required: bool = True) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise StoreError(f"{name} is required")
            return None
        if not isinstance(value, str):
            raise StoreError(f"{name} must be text")
        value = " ".join(value.split())
        if len(value) > D.TEXT_MAX:
            raise StoreError(f"{name} is longer than {D.TEXT_MAX} characters; shorten it")
        return value

    @staticmethod
    def _date(value, name: str) -> str | None:
        if value in (None, ""):
            return None
        try:
            return dt.date.fromisoformat(str(value)).isoformat()
        except ValueError:
            raise StoreError(f"{name} must be a date like 2026-10-31, got {value!r}") from None

    @staticmethod
    def _list(value, name: str) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or len(value) > 10:
            raise StoreError(f"{name} must be a list of up to 10 short texts")
        return [FounderStore._text(v, name) for v in value if str(v).strip()]

    def _profile_value(self, field: str, value):
        kind = D.PROFILE_FIELDS[field][0]
        if kind == "text":
            return self._text(value, field)
        if kind == "stage":
            v = str(value or "").strip().lower().replace(" ", "-").replace("_", "-")
            v = {"product-market-fit": "pmf", "pre-seed": "idea"}.get(v, v)
            if v not in D.STAGES:
                raise StoreError(f"stage must be one of: {', '.join(D.STAGES)}")
            return v
        if kind == "allocation":
            from .invest import check_targets
            try:
                return {k: float(v) for k, v in check_targets(value).items()}
            except ValueError as e:
                raise StoreError(f"{field}: {e}") from None
        if kind == "percent":
            try:
                n = float(str(value).rstrip("%").strip())
            except (TypeError, ValueError):
                raise StoreError(f"{field} must be a percent, e.g. 10") from None
            if not 0 < n <= 100:
                raise StoreError(f"{field} must be more than 0 and at most 100")
            return round(n, 2)
        if kind == "symbols":
            items = value if isinstance(value, list) else [x for x in str(value or "").replace(",", " ").split()]
            out = []
            for x in items:
                sym = re.sub(r"\s+", "", str(x)).upper()
                if not re.fullmatch(r"[A-Z0-9.\-/]{1,12}", sym):
                    raise StoreError(f"{field}: {x!r} isn't a ticker symbol")
                if sym not in out:
                    out.append(sym)
            if len(out) > 50:
                raise StoreError(f"{field} takes at most 50 symbols")
            return out
        if kind == "enum":
            v = str(value or "").strip().lower().replace(" ", "-").replace("_", "-")
            allowed = D.FIELD_VALUES.get(field, ())
            if v not in allowed:
                raise StoreError(f"{field} must be one of: {', '.join(allowed)}")
            return v
        if kind == "int":
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise StoreError(f"{field} must be a whole number") from None
            if not 0 <= n <= 100_000:
                raise StoreError(f"{field} must be between 0 and 100000")
            return n
        if kind == "metrics":
            if not isinstance(value, dict) or len(value) > 20:
                raise StoreError("key_metrics must be an object of up to 20 name -> value pairs")
            out = {}
            for k, v in value.items():
                if not isinstance(v, (str, int, float)) or isinstance(v, bool):
                    raise StoreError(f"key_metrics[{k!r}] must be text or a number")
                out[self._text(str(k), "metric name")] = v if not isinstance(v, str) else self._text(v, str(k))
            return out
        if kind == "places":
            if not isinstance(value, dict) or not value or len(value) > 20:
                raise StoreError(f"{field} must be an object of up to 20 what -> where pairs, "
                                 "e.g. {\"pipeline\": \"HubSpot\"}")
            out = {}
            for k, v in value.items():
                if not isinstance(v, str) or not v.strip():
                    raise StoreError(f"{field}[{k!r}] must say where, as text (a tool, a file or a folder)")
                out[self._text(str(k), f"{field} entry")] = self._text(v, str(k))
            return out
        if kind == "tz":
            try:
                ZoneInfo(str(value))
            except (ZoneInfoNotFoundError, ValueError):
                raise StoreError(f"timezone must be an IANA name like Asia/Kolkata, got {value!r}") from None
            return str(value)
        if kind == "weekday":
            v = str(value or "").strip().lower()[:3]
            if v not in D.WEEKDAYS:
                raise StoreError(f"checkin_day must be one of: {', '.join(D.WEEKDAYS)}")
            return v
        raise StoreError(f"unsupported field {field}")

    # ------------------------------------------------------------------ profile
    def _current(self, field: str) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute("SELECT * FROM profile_facts WHERE founder_id=? AND field=? AND superseded_at IS NULL",
                                   (self.fid, field)).fetchone()

    def _current_value(self, field: str):
        row = self._current(field)
        return json.loads(row["value"]) if row else None

    def _common_value(self, field: str):
        """A shared fact from the Common profile, when this Pack reads that field; never fails a read."""
        if self.common is None or field not in self.common_fields:
            return None
        try:
            return self.common._current_value(field)
        except sqlite3.Error:
            return None

    def shared_fields(self) -> list[str]:
        """The profile fields whose value comes from the Common profile (not set in this Project)."""
        if self.common is None or not self.common_fields:
            return []
        try:
            mine = set(self._own_profile())
            return [f for f in self.common._own_profile() if f in self.common_fields and f not in mine]
        except sqlite3.Error:
            return []

    def profile(self) -> dict[str, dict]:
        """{field: {value, since, confirmed_at, stale}} for the fields that have a value: this store's own
        facts, then the Common profile's for the shared fields this store doesn't set."""
        out = self._own_profile()
        if self.common is not None and self.common_fields:
            try:
                shared = self.common._own_profile()
            except sqlite3.Error:
                shared = {}
            for f, v in shared.items():
                if f in self.common_fields and f not in out:
                    out[f] = v
        return {f: out[f] for f in D.PROFILE_FIELDS if f in out}

    def _own_profile(self) -> dict[str, dict]:
        out = {}
        cutoff = self.now() - dt.timedelta(days=D.PROFILE_STALE_DAYS)
        with self._lock:
            rows = self.db.execute("SELECT * FROM profile_facts WHERE founder_id=? AND superseded_at IS NULL",
                                   (self.fid,)).fetchall()
        for r in rows:
            confirmed = dt.datetime.fromisoformat(r["confirmed_at"])
            out[r["field"]] = {"value": json.loads(r["value"]), "since": r["valid_from"],
                               "confirmed_at": r["confirmed_at"],
                               "stale": r["field"] not in D.NEVER_STALE and confirmed < cutoff}
        return out

    def profile_history(self, field: str) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM profile_facts WHERE founder_id=? AND field=? ORDER BY id",
                                   (self.fid, field)).fetchall()
        return [{"value": json.loads(r["value"]), "valid_from": r["valid_from"],
                 "superseded_at": r["superseded_at"]} for r in rows]

    def update_profile(self, changes: dict, request_id: str | None = None,
                       source: str = "coach_update_profile") -> dict:
        return self._transact(request_id, lambda: self._update_profile(changes, source),
                              what=("profile", changes))

    def _update_profile(self, changes: dict, source: str) -> dict:
        if not isinstance(changes, dict) or not changes:
            raise StoreError("changes must be an object of field -> value, e.g. {\"stage\": \"mvp\"}")
        unknown = [f for f in changes if f not in self.fields]
        if unknown:
            raise StoreError(f"unknown profile field(s): {', '.join(unknown)}. Fields: "
                             + "; ".join(f"{f} ({D.PROFILE_FIELDS[f][1]})" for f in self.fields))
        clean = {f: self._profile_value(f, v) for f, v in changes.items()}

        def apply() -> dict:
            now = self._iso()
            updated, confirmed = [], []
            for field, value in clean.items():
                cur = self._current(field)
                if cur is not None and json.loads(cur["value"]) == value:
                    self.db.execute("UPDATE profile_facts SET confirmed_at=? WHERE id=?", (now, cur["id"]))
                    self._change("profile", field, "confirm", value, value, source)
                    confirmed.append(field)
                    continue
                if cur is not None:
                    self.db.execute("UPDATE profile_facts SET superseded_at=? WHERE id=?", (now, cur["id"]))
                self.db.execute("INSERT INTO profile_facts (founder_id, field, value, valid_from, superseded_at, "
                                "confirmed_at, source) VALUES (?,?,?,?,NULL,?,?)",
                                (self.fid, field, _j(value), now, now, source))
                self._change("profile", field, "update" if cur is not None else "create",
                             json.loads(cur["value"]) if cur is not None else None, value, source)
                updated.append(field)
            return {"updated": updated, "confirmed": confirmed,
                    "profile": {f: v["value"] for f, v in self.profile().items()}}
        return apply()

    def retire_profile(self, fields, source: str, request_id: str | None = None) -> list[str]:
        """End this store's own value of `fields` (kept in history, logged): used when a fact moves to the
        Common profile, so the shared value isn't hidden behind an older local one. Returns the fields ended."""
        want = [f for f in fields if f in D.PROFILE_FIELDS]

        def apply() -> dict:
            now, done = self._iso(), []
            for f in want:
                cur = self._current(f)
                if cur is None:
                    continue
                self.db.execute("UPDATE profile_facts SET superseded_at=? WHERE id=?", (now, cur["id"]))
                self._change("profile", f, "move_to_common", json.loads(cur["value"]), None, source)
                done.append(f)
            return {"retired": done}
        if not want:
            return []
        return self._transact(request_id, apply, what=("retire", want))["retired"]

    # ------------------------------------------------------------------ records
    def record(self, entry: dict, request_id: str | None = None, source: str = "coach_record") -> dict:
        return self._transact(request_id, lambda: self._record(entry, source), what=("record", entry))

    def _record(self, entry: dict, source: str) -> dict:
        if not isinstance(entry, dict) or entry.get("kind") not in ("goal", "commitments", "decision", "checkin"):
            raise StoreError("entry.kind must be goal, commitments, decision or checkin")
        kind = entry["kind"]
        now_week = self.this_week()
        if kind == "goal":
            rows = [{"text": self._text(entry.get("text"), "text"), "measure": self._text(entry.get("measure"), "measure", False),
                     "target_date": self._date(entry.get("target_date"), "target_date"),
                     "citations": self._check_citations(entry.get("citations"))}]
        elif kind == "commitments":
            items = entry.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= D.MAX_OPEN_COMMITMENTS_PER_WEEK:
                raise StoreError(f"commitments.items must hold 1-{D.MAX_OPEN_COMMITMENTS_PER_WEEK} Commitments")
            week = entry.get("week") or now_week
            week_start(week)
            rows = []
            for i, it in enumerate(items, 1):
                if not isinstance(it, dict):
                    raise StoreError(f"commitment {i} must be an object with action, cue and outcome")
                gid = it.get("goal_id")
                if gid and not self.db.execute("SELECT 1 FROM goals WHERE id=? AND founder_id=?", (gid, self.fid)).fetchone():
                    raise StoreError(f"commitment {i}: no Goal {gid}; ids come from coach_get_context")
                rows.append({"week": week, "action": self._text(it.get("action"), f"commitment {i} action"),
                             "cue": self._text(it.get("cue"), f"commitment {i} cue", False),
                             "outcome": self._text(it.get("outcome"), f"commitment {i} outcome (measurable)"),
                             "goal_id": gid or None, "citations": self._check_citations(it.get("citations"))})
        elif kind == "decision":
            rows = [{"text": self._text(entry.get("text"), "text"),
                     "reasoning": self._text(entry.get("reasoning"), "reasoning"),
                     "revisit_on": self._date(entry.get("revisit_on"), "revisit_on"),
                     "citations": self._check_citations(entry.get("citations"))}]
        else:
            rows = [{"week": now_week, "summary": self._text(entry.get("summary"), "summary"),
                     "wins": self._list(entry.get("wins"), "wins"), "blockers": self._list(entry.get("blockers"), "blockers")}]

        def apply() -> dict:
            now, ids = self._iso(), []
            for r in rows:
                if kind == "goal":
                    rid = self._new_id("goal")
                    self.db.execute("INSERT INTO goals (id, founder_id, text, measure, target_date, status, citations, "
                                    "created_at) VALUES (?,?,?,?,?,?,?,?)",
                                    (rid, self.fid, r["text"], r["measure"], r["target_date"], "active",
                                     _j(r["citations"]), now))
                    self._change("goal", rid, "create", None, r, source)
                elif kind == "commitments":
                    rid = self._new_id("commitment")
                    self.db.execute("INSERT INTO commitments (id, founder_id, week, action, cue, outcome, goal_id, "
                                    "citations, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                    (rid, self.fid, r["week"], r["action"], r["cue"], r["outcome"], r["goal_id"],
                                     _j(r["citations"]), "open", now))
                    self._change("commitment", rid, "create", None, r, source)
                elif kind == "decision":
                    rid = self._new_id("decision")
                    self.db.execute("INSERT INTO decisions (id, founder_id, text, reasoning, citations, decided_at, "
                                    "revisit_on) VALUES (?,?,?,?,?,?,?)",
                                    (rid, self.fid, r["text"], r["reasoning"], _j(r["citations"]), now, r["revisit_on"]))
                    self._change("decision", rid, "create", None, r, source)
                else:
                    rid = self._new_id("checkin")
                    self.db.execute("INSERT INTO checkins (id, founder_id, week, summary, wins, blockers, created_at) "
                                    "VALUES (?,?,?,?,?,?,?)",
                                    (rid, self.fid, r["week"], r["summary"], _j(r["wins"]), _j(r["blockers"]), now))
                    self._change("checkin", rid, "create", None, r, source)
                ids.append(rid)
            warnings = self._warnings(week=rows[0].get("week") if kind == "commitments" else None)
            if kind == "checkin":
                left = self.overdue_commitments()
                if left:        # a Check-in reviews each Commitment; the Founder's report may already say how
                    warnings.append(f"{len(left)} Commitment(s) from earlier weeks are still open ("
                                    + ", ".join(f"{c['id']} {c['action'][:60]!r}" for c in left)
                                    + "): set each to done, dropped or carried with coach_update, from what the "
                                    "Founder reported or after asking.")
            return {"ids": ids, "warnings": warnings}
        return apply()

    # ------------------------------------------------------------------ Feedback
    def feedback(self, entry: dict, request_id: str | None = None, context: dict | None = None,
                 source: str = "coach_feedback") -> dict:
        """Save one Feedback record (CONTEXT.md). `context` is what the runtime knows about itself
        (product, version, pack) so an eval case can be rebuilt against the same knowledge."""
        return self._transact(request_id, lambda: self._feedback(entry, context or {}, source),
                              what=("feedback", entry))

    @staticmethod
    def _long_text(value, name: str, limit: int, required: bool = True) -> str | None:
        """Text kept as written (line breaks and all): an answer is evidence, not a label."""
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise StoreError(f"{name} is required")
            return None
        if not isinstance(value, str):
            raise StoreError(f"{name} must be text")
        value = value.strip()
        if len(value) > limit:
            raise StoreError(f"{name} is longer than {limit} characters; keep the part the Feedback is about")
        return value

    def _feedback(self, entry: dict, context: dict, source: str) -> dict:
        if not isinstance(entry, dict):
            raise StoreError("feedback must be an object")
        cat = entry.get("category")
        if cat not in D.FEEDBACK_CATEGORIES:
            raise StoreError(f"category must be one of {', '.join(D.FEEDBACK_CATEGORIES)}")

        def strings(value, name: str, most: int, each: int) -> list[str]:
            if value in (None, ""):
                return []
            if not isinstance(value, list) or len(value) > most:
                raise StoreError(f"{name} must be a list of up to {most} texts")
            return list(dict.fromkeys(str(v).strip()[:each] for v in value if str(v).strip()))

        row = {"category": cat,
               "question": self._long_text(entry.get("question"), "question", D.FEEDBACK_QUESTION_MAX),
               "answer": self._long_text(entry.get("answer"), "answer", D.FEEDBACK_ANSWER_MAX),
               "cited": strings(entry.get("cited"), "cited", 50, 200),
               "searches": strings(entry.get("searches"), "searches", 20, 500),
               "note": self._long_text(entry.get("note"), "note", D.TEXT_MAX * 2, required=False),
               "expected": self._long_text(entry.get("expected"), "expected", D.TEXT_MAX * 2, required=False)}

        def apply() -> dict:
            rid, now = self._new_id("feedback"), self._iso()
            self.db.execute("INSERT INTO feedback (id, founder_id, created_at, category, question, answer, cited, "
                            "searches, note, expected, context) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (rid, self.fid, now, row["category"], row["question"], row["answer"], _j(row["cited"]),
                             _j(row["searches"]), row["note"], row["expected"], _j(context)))
            self._change("feedback", rid, "create", None, {"category": cat}, source)
            return {"id": rid, "saved_at": now, "category": cat}
        return apply()

    def feedback_list(self) -> list[dict]:
        rows = self._rows("SELECT * FROM feedback WHERE founder_id=? ORDER BY created_at, id")
        for r in rows:
            for k in ("cited", "searches", "context"):
                if isinstance(r.get(k), str):
                    try:
                        r[k] = json.loads(r[k])
                    except ValueError:
                        pass
        return rows

    def export_feedback(self, out_dir: Path | None = None) -> dict:
        """All Feedback as one JSONL file to send to the maintainers (nothing else from the store)."""
        rows = self.feedback_list()
        out_dir = Path(out_dir) if out_dir else self.home / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"feedback-{self.now().strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        _atomic(path, "".join(json.dumps({**r, "product": product.ID}, ensure_ascii=False) + "\n" for r in rows))
        return {"path": path, "count": len(rows)}

    # ------------------------------------------------------------------ usage log
    def log_usage(self, tool: str, outcome: str, duration_ms: int, run: str, version: str | None = None,
                  detail: dict | None = None) -> bool:
        """Append one usage event (docs/usage-log.md). Best effort by design: never raises, never
        waits more than USAGE_BUSY_MS for a busy store, adds no change-log row and no FOUNDER.md
        rewrite. Returns whether the row was written."""
        try:
            with self._lock:
                self.db.execute(f"PRAGMA busy_timeout={USAGE_BUSY_MS}")
                try:
                    self.db.execute("INSERT INTO usage (founder_id, at, run, tool, outcome, duration_ms, version, detail) "
                                    "VALUES (?,?,?,?,?,?,?,?)",
                                    (self.fid, self._iso(), str(run)[:40], str(tool)[:60],
                                     outcome if outcome in USAGE_OUTCOMES else "error", max(0, int(duration_ms)),
                                     version, _j(detail or {})))
                finally:
                    self.db.execute("PRAGMA busy_timeout=5000")
            return True
        except Exception as e:                     # noqa: BLE001 -- telemetry must never cost a tool call
            import logging
            logging.getLogger("founder_coach").debug("usage event not logged: %s", e)
            return False

    def prune_usage(self, days: int) -> int:
        """Delete usage events older than `days` days; returns how many."""
        cutoff = (self.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            n = self.db.execute("DELETE FROM usage WHERE founder_id=? AND at < ?", (self.fid, cutoff)).rowcount
        return n

    def usage_rows(self, days: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM usage WHERE founder_id=?", [self.fid]
        if days is not None:
            sql += " AND at >= ?"
            args.append((self.now() - dt.timedelta(days=days)).isoformat(timespec="seconds"))
        with self._lock:
            rows = [dict(r) for r in self.db.execute(sql + " ORDER BY at, id", args)]
        for r in rows:
            r.pop("founder_id", None)
            try:
                r["detail"] = json.loads(r["detail"] or "{}")
            except ValueError:
                r["detail"] = {}
        return rows

    def usage_summary(self, days: int = 30) -> dict:
        """Per tool: calls, errors, empty results, Gaps, keyword-mode searches, p50/p95 duration."""
        rows = self.usage_rows(days)

        def pct(xs: list[int], q: float) -> int | None:
            if not xs:
                return None
            xs = sorted(xs)
            return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]
        tools: dict[str, dict] = {}
        for r in rows:
            t = tools.setdefault(r["tool"], {"calls": 0, "errors": 0, "empty": 0, "gaps": 0, "keyword": 0, "_ms": []})
            t["calls"] += 1
            t["errors"] += r["outcome"] == "error"
            t["empty"] += r["outcome"] == "empty"
            t["gaps"] += r["outcome"] == "gap"
            t["keyword"] += r["detail"].get("mode") == "keyword"
            t["_ms"].append(r["duration_ms"])
        for t in tools.values():
            ms = t.pop("_ms")
            t["p50_ms"], t["p95_ms"] = pct(ms, 0.5), pct(ms, 0.95)
        return {"days": days, "calls": len(rows), "runs": len({r["run"] for r in rows}),
                "first": rows[0]["at"] if rows else None, "last": rows[-1]["at"] if rows else None,
                "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1]["calls"]))}

    def export_usage(self, out_dir: Path | None = None) -> dict:
        """The usage log as one JSONL file to send to the maintainers (nothing else from the store)."""
        rows = self.usage_rows()
        out_dir = Path(out_dir) if out_dir else self.home / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"usage-{self.now().strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        _atomic(path, "".join(json.dumps({**r, "product": product.ID}, ensure_ascii=False) + "\n" for r in rows))
        return {"path": path, "count": len(rows)}

    def clear_usage(self) -> int:
        with self._lock:
            return self.db.execute("DELETE FROM usage WHERE founder_id=?", (self.fid,)).rowcount

    def _warnings(self, week: str | None = None) -> list[str]:
        out = []
        week = week or self.this_week()
        n = self.db.execute("SELECT COUNT(*) FROM commitments WHERE founder_id=? AND week=? AND status='open'",
                            (self.fid, week)).fetchone()[0]
        if n > D.MAX_OPEN_COMMITMENTS_PER_WEEK:
            out.append(f"{n} open Commitments in {week}; Focus works best with at most "
                       f"{D.MAX_OPEN_COMMITMENTS_PER_WEEK}. Ask the Founder which to drop or carry.")
        g = self.db.execute("SELECT COUNT(*) FROM goals WHERE founder_id=? AND status='active'", (self.fid,)).fetchone()[0]
        if g > D.MAX_ACTIVE_GOALS:
            out.append(f"{g} active Goals; suggest the Founder keeps at most {D.MAX_ACTIVE_GOALS}.")
        return out

    def get(self, rid: str) -> dict | None:
        kind = D.KIND_OF_PREFIX.get((rid or "").split("-", 1)[0])
        if not kind:
            return None
        table = {"goal": "goals", "commitment": "commitments", "decision": "decisions", "checkin": "checkins"}[kind]
        with self._lock:
            row = self.db.execute(f"SELECT * FROM {table} WHERE id=? AND founder_id=?", (rid, self.fid)).fetchone()
        return {"kind": kind, **_row(row)} if row else None

    def update(self, rid: str, status: str | None = None, changes: dict | None = None, note: str | None = None,
               request_id: str | None = None, source: str = "coach_update") -> dict:
        # read, check and write in one BEGIN IMMEDIATE: two hosts carrying the same Commitment
        # can't both see it "open" and make two copies
        return self._transact(request_id, lambda: self._update(rid, status, changes, note, source),
                              what=("update", rid, status, changes, note))

    def _update(self, rid: str, status: str | None, changes: dict | None, note: str | None, source: str) -> dict:
        rec = self.get(rid)
        if rec is None:
            raise StoreError(f"no record {rid!r}; ids come from coach_get_context or coach_record")
        kind = rec["kind"]
        changes = dict(changes or {})
        if status is not None:
            allowed = D.STATUSES.get(kind)
            if not allowed:
                raise StoreError(f"a {kind} has no status; change its fields instead")
            if status not in allowed:
                raise StoreError(f"a {kind}'s status is one of: {', '.join(allowed)}")
        bad = [k for k in changes if k not in D.EDITABLE[kind]]
        if bad:
            raise StoreError(f"can't change {', '.join(bad)} on a {kind}; editable: {', '.join(D.EDITABLE[kind])}")
        if note is not None:
            if kind not in ("goal", "commitment"):
                raise StoreError(f"a note applies to Goals and Commitments; for a {kind}, change its text instead")
            changes["note" if kind == "goal" else "result_note"] = note
        clean = {}
        for k, v in changes.items():
            if k in ("target_date", "revisit_on"):
                clean[k] = self._date(v, k)
            elif k in ("wins", "blockers"):
                clean[k] = _j(self._list(v, k))
            elif k == "goal_id":
                if v and not self.db.execute("SELECT 1 FROM goals WHERE id=?", (v,)).fetchone():
                    raise StoreError(f"no Goal {v}")
                clean[k] = v or None
            else:
                clean[k] = self._text(v, k, required=k in ("text", "action", "outcome", "reasoning", "summary"))
        if status is None and not clean:
            raise StoreError("nothing to change: pass a status, changes or a note")
        if status == "carried" and rec["status"] != "open":
            raise StoreError(f"only an open Commitment can be carried; {rid} is {rec['status']}")
        table = {"goal": "goals", "commitment": "commitments", "decision": "decisions", "checkin": "checkins"}[kind]

        def apply() -> dict:
            now = self._iso()
            sets = dict(clean)
            if status is not None:
                sets["status"] = status
                sets["closed_at"] = now if status in D.TERMINAL else None
            self.db.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                            (*sets.values(), rid))
            after = self.get(rid)
            self._change(kind, rid, "status" if status is not None else "update", rec, after, source)
            out = {"id": rid, "before": rec, "after": after}
            if status == "carried":
                week = max(self.this_week(), next_week(rec["week"]))
                new = self._new_id("commitment")
                self.db.execute("INSERT INTO commitments (id, founder_id, week, action, cue, outcome, goal_id, "
                                "citations, status, carried_from, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (new, self.fid, week, rec["action"], rec["cue"], rec["outcome"], rec["goal_id"],
                                 _j(rec["citations"]), "open", rid, now))
                self._change("commitment", new, "create", None, {"carried_from": rid, "week": week}, source)
                out.update(new_id=new, carried_weeks=self._carry_streak(new))
            out["warnings"] = self._warnings(week=rec.get("week"))
            return out
        return apply()

    def _carry_streak(self, rid: str) -> int:
        n, cur = 0, rid
        while True:
            with self._lock:
                row = self.db.execute("SELECT carried_from FROM commitments WHERE id=?", (cur,)).fetchone()
            if not row or not row[0]:
                return n
            n, cur = n + 1, row[0]

    # ------------------------------------------------------------------ reads
    def _rows(self, sql: str, *args) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self.db.execute(sql, (self.fid, *args)).fetchall()]

    def goals(self, status: str | None = "active") -> list[dict]:
        if status:
            return self._rows("SELECT * FROM goals WHERE founder_id=? AND status=? ORDER BY created_at", status)
        return self._rows("SELECT * FROM goals WHERE founder_id=? ORDER BY created_at")

    def commitments(self, week: str | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM commitments WHERE founder_id=?", []
        if week:
            sql += " AND week=?"
            args.append(week)
        if status:
            sql += " AND status=?"
            args.append(status)
        rows = self._rows(sql + " ORDER BY week, created_at", *args)
        for r in rows:
            if r.get("carried_from"):
                r["carried_weeks"] = self._carry_streak(r["id"])
        return rows

    def overdue_commitments(self) -> list[dict]:
        return [c for c in self.commitments(status="open") if c["week"] < self.this_week()]

    def checkins(self, limit: int = 5) -> list[dict]:
        return self._rows("SELECT * FROM checkins WHERE founder_id=? ORDER BY created_at DESC LIMIT ?", limit)

    def decisions(self, limit: int = 5) -> list[dict]:
        return self._rows("SELECT * FROM decisions WHERE founder_id=? ORDER BY decided_at DESC LIMIT ?", limit)

    def decisions_to_revisit(self) -> list[dict]:
        return self._rows("SELECT * FROM decisions WHERE founder_id=? AND revisit_on IS NOT NULL AND revisit_on<=? "
                          "ORDER BY revisit_on", self.today().isoformat())

    def recent_changes(self, limit: int = 10) -> list[dict]:
        return self._rows("SELECT seq, at, entity, entity_id, op, source FROM changes WHERE founder_id=? "
                          "ORDER BY seq DESC LIMIT ?", limit)

    def created_at(self) -> dt.datetime | None:
        with self._lock:
            row = self.db.execute("SELECT MIN(at) FROM changes WHERE founder_id=?", (self.fid,)).fetchone()
        return dt.datetime.fromisoformat(row[0]) if row and row[0] else None

    # ------------------------------------------------------------------ Holdings (the investor Pack)
    def import_holdings(self, snapshots, source: str, request_id: str | None = None) -> dict:
        """Save positions snapshots (founder_coach.invest.read_positions), once each: the same account, date and
        file again is skipped, so a retried or repeated import never doubles a Project's money."""
        snaps = list(snapshots)

        def apply() -> dict:
            now, done, skipped = self._iso(), [], []
            for s in snaps:
                if self.db.execute("SELECT 1 FROM holdings_snapshots WHERE founder_id=? AND account=? AND as_of=? "
                                   "AND sha256=?", (self.fid, s.account, s.as_of, s.sha256)).fetchone():
                    skipped.append({"account": s.account, "as_of": s.as_of})
                    continue
                sid = self._new_id("holdings")
                self.db.execute("INSERT INTO holdings_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                                (sid, self.fid, s.account, s.as_of, now, s.broker, s.sha256, source[:200],
                                 s.total_cents))
                self.db.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?)",
                                    [(sid, p.symbol, (p.description or "")[:200], p.quantity, p.value_cents,
                                      p.csv_class) for p in s.positions])
                self._change("holdings", sid, "import", None, {"account": s.account, "as_of": s.as_of,
                                                                "positions": len(s.positions),
                                                                "total_cents": s.total_cents}, source[:200])
                done.append({"id": sid, "account": s.account, "as_of": s.as_of, "positions": len(s.positions),
                             "total_cents": s.total_cents})
            labels = self.asset_labels()
            unlabelled = sorted({p.symbol for s in snaps for p in s.positions
                                 if not labels.get(p.symbol) and not p.csv_class})
            return {"imported": done, "skipped": skipped, "unlabelled": unlabelled}
        what = ("holdings", [(s.account, s.as_of, s.sha256) for s in snaps])
        return self._transact(request_id, apply, what=what)

    def label_assets(self, labels: dict, request_id: str | None = None, source: str = "coach_holdings") -> dict:
        """The person's asset class for each symbol (their word wins over anything a file said)."""
        from .invest import ASSET_CLASSES
        if not isinstance(labels, dict) or not labels:
            raise StoreError("labels must be an object of symbol -> asset class, e.g. {\"VTI\": \"us_equity\"}")
        clean = {}
        for sym, cls in labels.items():
            s = re.sub(r"\s+", "", str(sym)).upper()
            c = str(cls or "").strip().lower().replace(" ", "_").replace("-", "_")
            if not s or len(s) > 40:
                raise StoreError(f"{sym!r} isn't a symbol")
            if c not in ASSET_CLASSES:
                raise StoreError(f"{sym}: asset class must be one of {', '.join(ASSET_CLASSES)}, got {cls!r}")
            clean[s] = c

        def apply() -> dict:
            now = self._iso()
            for s, c in clean.items():
                before = self.db.execute("SELECT asset_class FROM asset_classes WHERE founder_id=? AND symbol=?",
                                         (self.fid, s)).fetchone()
                self.db.execute("INSERT OR REPLACE INTO asset_classes VALUES (?,?,?,?)", (self.fid, s, c, now))
                self._change("asset_class", s, "update" if before else "create", before[0] if before else None, c,
                             source)
            return {"labelled": clean}
        return self._transact(request_id, apply, what=("labels", clean))

    def asset_labels(self) -> dict[str, str]:
        with self._lock:
            return {r[0]: r[1] for r in self.db.execute(
                "SELECT symbol, asset_class FROM asset_classes WHERE founder_id=?", (self.fid,))}

    def holdings(self) -> tuple[list[dict], dict[str, list[dict]]]:
        """Every snapshot (oldest first) and its positions, for founder_coach.invest.review."""
        snaps = self._rows("SELECT * FROM holdings_snapshots WHERE founder_id=? ORDER BY as_of, imported_at")
        pos: dict[str, list[dict]] = {s["id"]: [] for s in snaps}
        with self._lock:
            for r in self.db.execute("SELECT p.* FROM positions p JOIN holdings_snapshots h ON h.id = p.snapshot_id "
                                     "WHERE h.founder_id=?", (self.fid,)):
                pos[r["snapshot_id"]].append({"symbol": r["symbol"], "description": r["description"],
                                              "value_cents": r["value_cents"], "csv_class": r["csv_class"]})
        return snaps, pos

    def holdings_review(self) -> dict:
        """A review of this Project's latest Holdings against its own Investment Policy Statement (profile)."""
        from .invest import review
        snaps, pos = self.holdings()
        ips = {f: v["value"] for f, v in self._own_profile().items()}
        return review(snaps, pos, self.asset_labels(), ips, self.today())

    # ------------------------------------------------------------------ Founder control
    def export(self, out_dir: Path | None = None) -> dict[str, Path]:
        out_dir = Path(out_dir) if out_dir else self.home / "exports" / self.now().strftime("%Y%m%dT%H%M%SZ")
        out_dir.mkdir(parents=True, exist_ok=True)
        data = {"founder_id": self.fid, "exported_at": self._iso(), "schema_version": SCHEMA_VERSION,
                "profile": self.profile(),
                "profile_history": {f: self.profile_history(f) for f in D.PROFILE_FIELDS},
                "goals": self.goals(None), "commitments": self.commitments(),
                "decisions": self._rows("SELECT * FROM decisions WHERE founder_id=? ORDER BY decided_at"),
                "checkins": self._rows("SELECT * FROM checkins WHERE founder_id=? ORDER BY created_at"),
                "feedback": self.feedback_list(),
                "holdings": self._rows("SELECT * FROM holdings_snapshots WHERE founder_id=? ORDER BY as_of"),
                "positions": self._rows("SELECT p.* FROM positions p JOIN holdings_snapshots h ON h.id = "
                                        "p.snapshot_id WHERE h.founder_id=?"),
                "asset_classes": self.asset_labels(),
                "usage": self.usage_rows(),
                "changes": self._rows("SELECT * FROM changes WHERE founder_id=? ORDER BY seq")}
        j, md = out_dir / "founder-export.json", out_dir / "FOUNDER.md"
        _atomic(j, json.dumps(data, indent=1, ensure_ascii=False))
        _atomic(md, self.markdown())
        return {"json": j, "markdown": md}

    def forget(self, keep_backup: bool = True) -> dict:
        """Delete everything the coach remembers. By default one final backup is kept (and
        reported) so an accidental forget can be undone; keep_backup=False removes backups too."""
        # Emptied in place, not unlinked: a coach server another host has open keeps its
        # connection to this file, so it sees the empty store at once instead of carrying on
        # with (and re-saving) data from a deleted file.
        with self._lock:
            final = self.backup("pre-forget-" + self.now().strftime("%Y%m%dT%H%M%SZ")) if keep_backup else None
            self.db.execute("PRAGMA secure_delete=ON")     # overwrite freed pages, don't just unlink them
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for t in (*TABLES, "requests", "usage"):
                    self.db.execute(f"DELETE FROM {t}")
                self.db.execute("COMMIT")
            except BaseException:
                _rollback(self.db)
                raise
            for stmt in ("VACUUM", "PRAGMA wal_checkpoint(TRUNCATE)"):
                try:
                    self.db.execute(stmt)
                except sqlite3.Error as e:              # another app mid-read: the data is deleted anyway
                    import logging
                    logging.getLogger("founder_coach").warning("forget: %s skipped (%s)", stmt, e)
            self.md_path.unlink(missing_ok=True)
            removed = 0
            if not keep_backup and self.backups.exists():
                for p in self.backups.glob("*"):
                    p.unlink(missing_ok=True)
                    removed += 1
                self.backups.rmdir()
        return {"deleted": str(self.path), "backup": str(final) if final else None, "backups_removed": removed}

    # ------------------------------------------------------------------ FOUNDER.md
    def markdown(self) -> str:
        from .nudges import render_common_markdown, render_markdown
        return render_markdown(self) if self.name == "founder" else render_common_markdown(self)

    def _write_markdown(self) -> None:
        try:
            with self._lock:                  # one consistent snapshot, written by one thread at a time
                _atomic(self.md_path, self.markdown())
        except Exception as e:                # noqa: BLE001 -- FOUNDER.md is a view; never fail a write for it
            import logging
            logging.getLogger("founder_coach").warning("FOUNDER.md not updated: %s", e)


def _file_key(path: Path) -> tuple[int, int] | None:
    """Which file is at `path` (device, inode): changes when another file is renamed into place."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("citations", "wins", "blockers"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    d.pop("founder_id", None)
    return d


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)          # disk full mid-write: no partial temp file left behind


def _rollback(db: sqlite3.Connection) -> None:
    """ROLLBACK that never hides the real error: SQLite may already have rolled back by
    itself (disk full, I/O error), and "no transaction is active" would replace it."""
    try:
        db.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------- integrity and restore
def _integrity(db: sqlite3.Connection) -> dict:
    try:
        rows = [r[0] for r in db.execute("PRAGMA integrity_check(20)").fetchall()]
    except sqlite3.DatabaseError as e:
        return {"ok": False, "detail": str(e)}
    ok = rows == ["ok"]
    return {"ok": ok, "detail": "ok" if ok else "; ".join(rows)[:500]}


def _ro_uri(path: Path) -> str:
    """Read-only, immutable (no -wal/-shm side files); as_uri() escapes spaces, '?' and '#'."""
    return Path(path).resolve().as_uri() + "?mode=ro&immutable=1"


def _check_file(path: Path) -> dict:
    """Integrity of a store file opened read-only, plus a readable schema version."""
    try:
        db = sqlite3.connect(_ro_uri(path), uri=True)   # no -wal/-shm side files
    except sqlite3.Error as e:
        return {"ok": False, "detail": str(e)}
    try:
        res = _integrity(db)
        if res["ok"]:
            try:
                row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            except sqlite3.DatabaseError as e:
                return {"ok": False, "detail": f"not a founder store ({e})"}
            if not row:
                return {"ok": False, "detail": "not a founder store (no schema_version)"}
            if int(row[0]) > SCHEMA_VERSION:
                return {"ok": False, "detail": f"schema v{row[0]} is newer than this coach (v{SCHEMA_VERSION})"}
        return res
    finally:
        db.close()


def open_store(home: str | Path | None = None, clock: Callable[[], dt.datetime] | None = None
               ) -> tuple[FounderStore | None, str | None]:
    """The store after its integrity check, or (None, why). Callers that must keep running
    (the MCP server, `status`, the hook) use this instead of FounderStore()."""
    try:
        store = FounderStore(home, clock=clock)
    except StoreError as e:                          # damaged (StoreDamaged) or a newer schema
        return None, str(e)
    except (OSError, sqlite3.Error) as e:
        return None, f"the founder store can't be opened ({e}). Nothing has been deleted."
    chk = store.check()
    if not chk["ok"]:
        path = store.path
        store.close()
        return None, f"the founder store at {path} fails its integrity check ({chk['detail']}). {RESTORE_HINT}"
    return store, None


def _home(home: str | Path | None) -> Path:
    return Path(home).expanduser() if home else D.home()


def list_backups(home: str | Path | None = None) -> list[dict]:
    """Backups in <home>/backups, newest first, each with its integrity result."""
    folder = _home(home) / "backups"
    if not folder.exists():
        return []
    out = []
    for p in sorted(folder.glob("founder-*.db"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True):
        st = p.stat()
        chk = _check_file(p)
        out.append({"path": p, "name": p.name, "label": p.stem[len("founder-"):], "size": st.st_size,
                    "modified": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat(timespec="seconds"),
                    "ok": chk["ok"], "detail": chk["detail"]})
    return out


def restore(home: str | Path | None = None, backup: str | Path | None = None) -> dict:
    """Replace the store with a backup (default: the newest one that passes its check).

    Never deletes: the current founder.db and its -wal/-shm files move to
    <home>/before-restore-<time>/ first. The copy goes through SQLite's backup API into a
    temporary file, is checked, then renamed into place; FOUNDER.md is regenerated."""
    home = _home(home)
    backups = list_backups(home)
    if backup:
        want = Path(backup).expanduser()
        match = [b for b in backups if want.name in (b["name"], f"founder-{want.name}.db") or b["label"] == str(backup)]
        if want.is_file():
            chosen = {"path": want, "name": want.name, **_check_file(want)}
        elif match:
            chosen = match[0]
        else:
            raise StoreError(f"no backup {backup!r} in {home / 'backups'}; `{product.ID} restore --list` shows them")
        if not chosen["ok"]:
            raise StoreError(f"backup {chosen['name']} fails its check ({chosen['detail']}); pick another with --list")
    else:
        good = [b for b in backups if b["ok"]]
        if not good:
            raise StoreError(f"no usable backup in {home / 'backups'}"
                             + (f" ({len(backups)} found, none passes its check)" if backups else "")
                             + "; nothing has been changed")
        chosen = good[0]
    src_path = Path(chosen["path"])
    path = home / "founder.db"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    aside, in_place = None, False
    current_ok = path.exists() and _check_file(path)["ok"]
    if current_ok:
        # Online restore: keep a copy of the current store aside (never delete), then copy the
        # backup INTO the live file with SQLite's backup API. A coach server another host has
        # open keeps its connection and sees the restored data on its next call.
        aside = home / f"before-restore-{stamp}"
        aside.mkdir(parents=True, exist_ok=True)
        live = sqlite3.connect(path, timeout=10.0)
        try:
            keep = sqlite3.connect(aside / "founder.db")
            try:
                live.backup(keep)
            finally:
                keep.close()
            src = sqlite3.connect(_ro_uri(src_path), uri=True)
            try:
                src.backup(live)
            finally:
                src.close()
        finally:
            live.close()
        in_place = True
    else:
        # Damaged (or missing): nothing can have it open usefully. Build the copy beside it,
        # check it, move the damaged files aside and rename the copy in.
        tmp = home / f".founder.db.restore-{os.getpid()}.tmp"
        try:
            src = sqlite3.connect(_ro_uri(src_path), uri=True)
            try:
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            chk = _check_file(tmp)
            if not chk["ok"]:
                raise StoreError(f"the restored copy fails its check ({chk['detail']}); nothing has been changed")
            present = [p for p in (path, path.with_name("founder.db-wal"), path.with_name("founder.db-shm"))
                       if p.exists()]
            if present:
                aside = home / f"before-restore-{stamp}"
                aside.mkdir(parents=True, exist_ok=True)
                for p in present:
                    os.replace(p, aside / p.name)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    store = FounderStore(home)                       # runs any migration an older backup needs
    try:
        final = store.check()
        store._write_markdown()
    finally:
        store.close()
    return {"restored_from": str(src_path), "set_aside": str(aside) if aside else None,
            "store": str(path), "check": final["detail"], "in_place": in_place}
