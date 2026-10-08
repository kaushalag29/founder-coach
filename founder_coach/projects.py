"""Projects and the Common profile (ADR-0016; M6e).

A Project is one thing the person works on with this coach (a company, an app, an investing goal). Each
has its own memory store, in its own folder, so nothing said about one ever shows up in another (G7):

    ~/.ytbrain/                       the engine home (YTBRAIN_HOME moves it)
      you.db, YOU.md                  the Common profile: name, role, timezone, answer style (only on a yes)
      installed/                      which coaches are on this machine (installed.py)
      <pack>/                         one folder per Pack, e.g. founder/
        last-project                  the Project the command line and the hook use by default
        projects/<id>/                founder.db, FOUNDER.md, backups/, exports/   (one Project)
          project.json                its display name and when it was made

A session works on one active Project (R1). With one Project it is chosen by itself; with several the coach
asks which one before reading or saving memory. Setting <PREFIX>HOME (or --home) keeps the single-folder
layout from before Projects: that folder is the store, and there is no Common profile (tests, evals and
people who pinned their data folder see no change).

The first start after an update moves the old single store (~/.<product id>/founder.db) into a Project, once:
the old folder is left as it was (it is the backup), the copy is checked, and its timezone moves to the
Common profile. A marker file makes the move happen once even when several processes start together.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import domain as D
from . import product
from .installed import engine_home

log = logging.getLogger("founder_coach")

NAME_MAX = 80
_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MIGRATED = ".migrated.json"
LOCK = ".migrating"
LOCK_STALE_S = 120


class ProjectError(ValueError):
    pass


def slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['’]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40].strip("-") or "project"


def _norm(name: str) -> str:
    return " ".join(str(name).casefold().split())


def _atomic_json(path: Path, data) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}")
    try:
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Projects:
    """The Projects of one Pack, under its folder in the engine home."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / "projects"

    def list(self) -> list[dict]:
        """[{id, name, created}] in the order they were made. A folder without a readable project.json
        still counts (named after its folder): a Project is never hidden by a damaged label."""
        out = []
        if not self.dir.is_dir():
            return out
        for d in self.dir.iterdir():
            if not d.is_dir() or d.name.startswith(".") or not _SLUG.fullmatch(d.name):
                continue
            meta = {}
            try:
                meta = json.loads((d / "project.json").read_text())
            except (OSError, ValueError):
                if not (d / "founder.db").exists():
                    continue
            out.append({"id": d.name, "name": str(meta.get("name") or d.name),
                        "created": str(meta.get("created") or "")})
        return sorted(out, key=lambda p: (p["created"], p["id"]))

    def get(self, ref: str | None) -> dict | None:
        """A Project by id, or by name ignoring case and spacing."""
        if not ref or not str(ref).strip():
            return None
        ref = str(ref).strip()
        ps = self.list()
        for p in ps:
            if p["id"] == ref:
                return p
        hits = [p for p in ps if _norm(p["name"]) == _norm(ref)] or [p for p in ps if p["id"] == slug(ref)]
        return hits[0] if len(hits) == 1 else None

    def need(self, ref: str) -> dict:
        p = self.get(ref)
        if p is None:
            have = ", ".join(f"{x['id']} ({x['name']})" for x in self.list()) or "none yet"
            raise ProjectError(f"no Project {ref!r}; Projects: {have}")
        return p

    def store_home(self, pid: str) -> Path:
        return self.dir / pid

    def create(self, name: str) -> dict:
        name = " ".join(str(name or "").split())
        if not name:
            raise ProjectError("a Project needs a name, e.g. the company or product")
        if len(name) > NAME_MAX:
            raise ProjectError(f"a Project name is at most {NAME_MAX} characters")
        if any(_norm(p["name"]) == _norm(name) for p in self.list()):
            raise ProjectError(f"there is already a Project named {name!r}")
        self.dir.mkdir(parents=True, exist_ok=True)
        base = slug(name)
        for n in range(1, 1000):
            pid = base if n == 1 else f"{base}-{n}"
            try:
                (self.dir / pid).mkdir()             # the claim: two processes never get the same folder
            except FileExistsError:
                continue
            meta = {"name": name, "created": _now()}
            _atomic_json(self.dir / pid / "project.json", meta)
            return {"id": pid, **meta}
        raise ProjectError("too many Projects with that name")

    def rename(self, ref: str, name: str) -> dict:
        """A new display name; the id (and folder) stay, so nothing that points at the Project breaks."""
        p = self.need(ref)
        name = " ".join(str(name or "").split())
        if not name or len(name) > NAME_MAX:
            raise ProjectError(f"a Project name is 1-{NAME_MAX} characters")
        if any(_norm(x["name"]) == _norm(name) and x["id"] != p["id"] for x in self.list()):
            raise ProjectError(f"there is already a Project named {name!r}")
        meta = {"name": name, "created": p["created"] or _now()}
        _atomic_json(self.dir / p["id"] / "project.json", meta)
        return {"id": p["id"], **meta}

    def last(self) -> str | None:
        try:
            pid = (self.root / "last-project").read_text().strip()
        except OSError:
            return None
        return pid if self.get(pid) else None

    def set_last(self, pid: str) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / f".last-project.tmp-{os.getpid()}"
            tmp.write_text(pid)
            os.replace(tmp, self.root / "last-project")
        except OSError as e:                         # a convenience: never fail the switch for it
            log.warning("couldn't remember the last Project: %s", e)


@dataclass
class Workspace:
    """Where this coach's memory lives: one folder (single mode) or the Projects of its Pack."""
    single: bool
    root: Path                       # single: the store folder; Projects: <engine home>/<pack id>
    engine: Path | None = None
    projects: Projects | None = None
    common_fields: tuple[str, ...] = ()
    migration: dict | None = None    # what the first-start move did, once

    @classmethod
    def open(cls, home: str | Path | None = None, engine: Path | None = None, migrate: bool = True) -> Workspace:
        explicit = home or product.env("HOME")
        if explicit:
            return cls(single=True, root=Path(explicit).expanduser())
        eng = Path(engine).expanduser() if engine else engine_home()
        root = eng / str(product.PACK.get("id") or "founder")
        fields = product.PACK.get("common_fields")
        fields = tuple(D.COMMON_FIELDS if fields is None else (f for f in fields if f in D.COMMON_FIELDS))
        ws = cls(single=False, root=root, engine=eng, projects=Projects(root), common_fields=fields)
        if migrate:
            try:
                ws.migration = migrate_legacy(ws)
            except Exception as e:                     # noqa: BLE001 -- the coach runs without the move
                log.warning("couldn't move the old memory store into a Project: %s", e)
                ws.migration = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        return ws

    def describe(self, pid: str | None) -> dict | None:
        """{id, name} of a Project, for the coach to name it."""
        if self.single or pid is None or self.projects is None:
            return None
        p = self.projects.get(pid)
        return {"id": pid, "name": p["name"] if p else pid}

    # -- choosing ---------------------------------------------------------------------------
    def choose(self, requested: str | None = None, use_last: bool = False) -> str | None:
        """The Project to work on: the one asked for (by id or name), else the only one, else (for the command
        line and the hook) the last one used, else <PREFIX>PROJECT. None: there are several and nothing says
        which, or none yet. Single mode has no Projects (None)."""
        if self.single:
            if requested:
                raise ProjectError(f"Projects aren't used while {product.env_name('HOME')} (or --home) names one "
                                   "data folder")
            return None
        assert self.projects is not None
        if requested:
            return self.projects.need(requested)["id"]
        ps = self.projects.list()
        if len(ps) == 1:
            return ps[0]["id"]
        env = product.env("PROJECT")
        if env and self.projects.get(env):
            return self.projects.get(env)["id"]
        if use_last:
            return self.projects.last()
        return None

    def store_home(self, pid: str | None) -> Path:
        if self.single or pid is None:
            if not self.single:
                raise ProjectError("no Project chosen")
            return self.root
        assert self.projects is not None
        return self.projects.store_home(pid)

    # -- stores ------------------------------------------------------------------------------
    def open_common(self, clock=None, create: bool = False):
        """The Common profile store, or None: single mode, a Pack that shares no fields, nothing shared yet
        (unless `create`), or it can't be opened (the coach then runs without it)."""
        if self.single or self.engine is None or not self.common_fields:
            return None
        if not create and not (self.engine / "you.db").exists():
            return None
        from .store import FounderStore, StoreError
        try:
            return FounderStore(self.engine, clock=clock, name="you", fields=D.COMMON_FIELDS)
        except (StoreError, OSError, sqlite3.Error) as e:
            log.warning("the Common profile can't be opened (%s); each Project keeps its own facts", e)
            return None

    def open_store(self, pid: str | None, clock=None, common=None):
        """(store, error) for a Project (or the single folder), wired to the Common profile."""
        from .store import open_store
        store, err = open_store(self.store_home(pid), clock)
        if store is not None and common is not None:
            store.common, store.common_fields = common, self.common_fields
        return store, err


# ---------------------------------------------------------------------------------------------- migration
def _lock(root: Path) -> Path | None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime < LOCK_STALE_S:
                    return None                        # another process is moving it now
                path.unlink(missing_ok=True)           # a crashed move: take over
            except OSError:
                return None
    return None


def migrate_legacy(ws: Workspace, legacy: Path | None = None) -> dict | None:
    """Move the single store from before Projects into a Project, once. Never raises and never changes the
    old folder: a failure is reported (and retried at the next start), the coach carries on."""
    if ws.single or ws.projects is None or product.env("MIGRATE") == "0":    # 0: the coach evaluation's machines
        return None
    marker = ws.root / MIGRATED
    if marker.exists():
        return None
    legacy = Path(legacy) if legacy else product.default_home()
    src = legacy / "founder.db"
    try:
        if not src.is_file() or src.stat().st_size == 0 or legacy.resolve() == ws.root.resolve():
            return None
    except OSError:
        return None
    lock = _lock(ws.root)
    if lock is None:
        return {"status": "busy"}
    tmp = ws.projects.dir / f".migrating-{os.getpid()}-{secrets.token_hex(2)}"
    try:
        if marker.exists():                            # finished by another process while we waited
            return None
        from .store import SCHEMA_VERSION, FounderStore, _check_file, _integrity
        chk = _check_file(src)
        if not chk["ok"]:
            why = ("it is newer than this coach: update it" if "newer than this coach" in chk["detail"] else
                   f"it fails its check ({chk['detail']}); run `{product.ID} restore --home {legacy}` first")
            return {"status": "failed", "error": f"the old store at {src} wasn't moved: {why}"}
        tmp.mkdir(parents=True)
        # a consistent copy, even while an older coach still has the old store open
        live = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)
        try:
            out = sqlite3.connect(tmp / "founder.db")
            try:
                live.backup(out)
                res = _integrity(out)
            finally:
                out.close()
        finally:
            live.close()
        if not res["ok"]:
            return {"status": "failed", "error": f"the old store at {src} fails its check ({res['detail']}); "
                                                 f"run `{product.ID} restore --home {legacy}` first"}
        store = FounderStore(tmp)                      # brings an older schema up to SCHEMA_VERSION
        try:
            company = (store.profile().get("company") or {}).get("value")
            moved = None
            tz = store._current_value("timezone")
            common = ws.open_common(create=True) if tz and "timezone" in ws.common_fields else None
            if common is not None:
                try:
                    have = common._current_value("timezone")
                    if have is None:
                        common.update_profile({"timezone": tz}, source="migration")
                    if (have or tz) == tz:
                        store.retire_profile(["timezone"], source="migration")
                        moved = tz
                finally:
                    common.close()
            store.backup("migrated")
        finally:
            store.close()
        meta = {"name": str(company or "My company")[:NAME_MAX], "created": _now(), "migrated_from": str(src)}
        _atomic_json(tmp / "project.json", meta)
        pid = None
        base = slug(meta["name"])
        for n in range(1, 1000):
            cand = base if n == 1 else f"{base}-{n}"
            try:
                os.rename(tmp, ws.projects.dir / cand)   # fails if the folder exists: never merges into one
                pid = cand
                break
            except OSError:
                if not (ws.projects.dir / cand).exists():
                    raise
        if pid is None:
            raise OSError("no free Project folder name")
        s2 = FounderStore(ws.projects.store_home(pid))
        try:
            s2._write_markdown()                       # FOUNDER.md at its new place
        finally:
            s2.close()
        if ws.projects.last() is None:
            ws.projects.set_last(pid)
        info = {"status": "migrated", "project": pid, "from": str(src), "timezone_to_common": moved,
                "schema_version": SCHEMA_VERSION, "at": _now()}
        _atomic_json(marker, info)
        log.info("moved the memory store from %s into Project %s", src, pid)
        return info
    except (OSError, sqlite3.Error, ValueError) as e:
        log.warning("couldn't move the old memory store into a Project (%s); the old folder is unchanged and "
                    "the move is retried at the next start", e)
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        lock.unlink(missing_ok=True)
