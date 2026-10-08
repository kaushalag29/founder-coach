"""The other coaches installed on this machine (ADR-0016, R4).

Each Pack is its own plugin, so one coach can't see another's tools. A shared registry fixes that without
sharing memory: every coach's server, when it starts for real, writes one small file under the engine home
(~/.ytbrain/installed/<product id>.json; YTBRAIN_HOME moves it) saying which Pack it is, which Domains its
knowledge covers and where its Knowledge pack lives. `coach_get_context` lists the others, so the host can
search another coach's knowledge for a part of a question outside this Pack's Domains (knowledge only: no
coach ever reads another's memory). An entry whose pack file is gone (the plugin was removed) is ignored.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path

from . import product

log = logging.getLogger(__name__)


def engine_home() -> Path:
    return Path(os.environ.get("YTBRAIN_HOME") or "~/.ytbrain").expanduser()


def register(home: Path, pack_path: Path | None, meta: dict | None) -> None:
    """Best effort: a coach that can't write its entry still works, it just isn't listed by the others."""
    if not pack_path or not meta:
        return
    try:
        d = home / "installed"
        d.mkdir(parents=True, exist_ok=True)
        entry = {"product_id": product.ID, "display_name": product.DISPLAY_NAME, "pack": product.PACK.get("id"),
                 "domains": sorted((meta.get("domains") or {}).keys()) or list(product.PACK.get("domains") or []),
                 "pack_path": str(Path(pack_path).resolve()), "search_tool": "coach_search",
                 "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        tmp = d / f".{product.ID}.json.tmp-{os.getpid()}"
        tmp.write_text(json.dumps(entry, indent=1))
        os.replace(tmp, d / f"{product.ID}.json")
    except OSError as e:
        log.warning("couldn't register this coach for the others to find: %s", e)


def others(home: Path | None) -> list[dict]:
    """The other coaches on this machine (never this one), each with its Domains and search tool."""
    if home is None:
        return []
    out = []
    for f in sorted((home / "installed").glob("*.json")):
        if f.stem == product.ID:
            continue
        try:
            e = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not e.get("pack_path") or not Path(e["pack_path"]).exists():
            continue                                    # removed: its pack is gone
        out.append({k: e.get(k) for k in ("display_name", "product_id", "pack", "domains", "search_tool")})
    return out
