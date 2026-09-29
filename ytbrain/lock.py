"""Single-writer lock for scheduled runs.

SQLite wants one writer, and a launchd job that fires while a manual run is in
progress would otherwise interleave. The lock is an OS advisory lock (flock) on
a file: the kernel releases it the moment the holding process exits, however it
exits (Ctrl+C, kill -9, crash, power loss), so a dead run can never wedge the
pipeline. A PID-file liveness check could: PIDs are reused, and a reused PID
made a crashed run look alive forever. The PID is still written for messages.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from .config import LOCKFILE

try:
    import fcntl
except ImportError:                     # Windows: unsupported platform, best effort below
    fcntl = None


class LockBusy(RuntimeError):
    pass


def _holder(path: Path) -> str:
    try:
        return path.read_text().strip() or "?"
    except OSError:
        return "?"


@contextmanager
def exclusive(path: Path = LOCKFILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError):
                raise LockBusy(f"another ytbrain run is active (pid {_holder(path)})") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        try:
            yield
        finally:
            os.ftruncate(fd, 0)          # no stale PID left behind for messages
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
