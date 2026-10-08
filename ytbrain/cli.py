"""ytbrain CLI: public startup-advice content (YouTube talks, website articles) to verified,
citable knowledge, and the tools that build and measure the founder-coach plugin.

Pipeline (each Step checkpointed per Document, so any one can be re-run alone):
    ytbrain sync [--type website|youtube] [--source ID]   captions + web pages
    ytbrain clean / extract / verify / index              transcripts -> records -> Verified -> index
    ytbrain run                 sync -> clean -> extract -> verify -> index
    ytbrain report / sample / status                      quality gates, review sheet, counts
    ytbrain invalidate <step> [--source ID] / drop --source ID
    ytbrain pack build          the Knowledge pack the plugin ships
    ytbrain eval build|run|judge|coach                    benchmark and coach gates
    ytbrain ops [all|ingest|eval|plugin]                  the whole loop, resumable, only what changed
    ytbrain claude -- <args>    the local Claude Code for plugin work (your plan, Haiku)

README.md has the full guide; docs/commands.md the commands by phase.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import queue
import time
import random
import re
import shutil
import subprocess
import shlex
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import captions, fetch, pages, source_kinds
from .config import (ACCEPT_MIN_RECORD_RATE, ACCEPT_MIN_SPAN_PASS_RATE,
                     ACCEPT_SAMPLE_SEED, EXTRACT_FAILURES, MANIFEST_DB, ROOT, METADATA, PAGES, RAW, REPORTS,
                     MIN_TRANSCRIPT_WORDS, SCHEMA_VERSION, TRANSCRIPTS, YT_COOLDOWN_BASE_S, YT_COOLDOWN_MAX_S,
                     YT_BACKFILL_PER_HOUR, YT_BACKFILL_SLEEP_SUBTITLES, YT_ENUM_RETRIES,
                     YT_ERROR_BACKOFF_S, YT_MAX_429_STREAK, YT_SLEEP_SUBTITLES, YT_VIDEO_RETRIES)
from .lock import LockBusy, exclusive
from .manifest import Manifest, StageState


from .config import SOURCES_FILE as SOURCES   # noqa: E402 -- yours, git-ignored (YTBRAIN_SOURCES_FILE in tests)
SOURCES_EXAMPLE = SOURCES.with_name("sources.example.yaml")              # tracked template
SYNC_TYPES = {"youtube": "youtube_playlist", "website": "website", "book": "pdf_books"}   # `sync --type` -> Source type


def _sources() -> dict:
    import yaml
    if not SOURCES.exists():
        sys.exit("sources.yaml not found. Start from the template:\n"
                 "  cp sources.example.yaml sources.yaml\n"
                 "or build one with: ytbrain discover --method ytdlp --channel @SomeChannel")
    return yaml.safe_load(SOURCES.read_text()) or {}


def _series_for(src: dict) -> str | None:
    """The Series a Source assigns: explicit `series`, else the Source's name.

    A catch-all (`fallback: true`) assigns none -- "all uploads" is not a
    Series, and the fallback guard in Manifest.upsert_document keeps any
    specific Series a Document already has.
    """
    if src.get("series"):
        return src["series"]
    if src.get("fallback"):
        return None
    return src.get("name") or src.get("id")


def _provenance_for(src: dict, info: dict | None) -> str | None:
    """Who published the Document: the Source's explicit `provenance`, else the
    uploader recorded by yt-dlp (e.g. "Y Combinator", "Stanford Online")."""
    return src.get("provenance") or (info or {}).get("uploader")


def _all_sources() -> list[dict]:
    """Every Source in sources.yaml, normalized (ytbrain/sources.py); exits on a config error."""
    from . import sources as S
    _sources()                                    # the friendly "sources.yaml not found" exit
    from . import domains as D
    try:
        srcs = S.load(SOURCES)
        D.check_sources(D.load(), srcs)
        return srcs
    except (S.SourceConfigError, D.DomainConfigError) as e:
        sys.exit(f"sources.yaml: {e}")


def _pack_docs_arg(args) -> set[str] | None:
    """--only-pack-docs: the Documents of the pack being scored (None: every question counts)."""
    if not getattr(args, "only_pack_docs", False):
        return None
    from .config import DATA
    from .eval.files import pack_doc_ids
    pack = Path(args.pack) if getattr(args, "pack", None) else DATA / "pack"
    docs = pack_doc_ids(pack)
    if docs is None:
        sys.exit(f"eval: --only-pack-docs: no readable pack at {pack}")
    return docs


def _eval_coverage(args, store, embed) -> int:
    """`eval gap` and `eval calibrate`: is `coverage` honest? (ytbrain/eval/coverage.py)"""
    import datetime as dt
    from .config import CALIBRATION_FILE, EVAL_DATA, EVAL_DIR, EVAL_PRIVATE
    from .eval import coverage as CV
    from .eval import route as R
    from .eval.files import load_set
    from .pages import atomic_write_text
    meta = getattr(store, "meta", None) or {}
    queries, qrels = load_set(EVAL_DIR, args.set, EVAL_PRIVATE)
    if (docs := _pack_docs_arg(args)) is not None:
        from .eval.files import about_docs
        kept = about_docs(queries, docs)
        print(f"eval {args.eval_cmd}: {len(kept)} of {len(queries)} question(s) are about this pack's Documents",
              flush=True)
        queries = kept
    say = lambda m: print(m, flush=True)
    if args.eval_cmd == "calibrate":
        rows = CV.pairs(store, embed, queries, qrels, k=args.k, say=say)
        try:
            res = CV.calibrate(rows)
        except ValueError as e:
            print(f"eval calibrate: {e}. Judge more questions first (`ytbrain eval judge`), or widen --set.", file=sys.stderr)
            return 1
        save = (args.force or (res["better"] and not res["unreachable"])) and not args.no_save
        if save:
            payload = {**res["curve"].as_meta(), "embed_model": meta.get("embed_model"),
                       "fitted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "set": args.set}
            atomic_write_text(CALIBRATION_FILE, json.dumps(payload, indent=1))
        print(CV.render_calibration(res, CALIBRATION_FILE if save else None))
        if save:
            print("next: `ytbrain pack build` (or `ytbrain ops plugin`) puts the curve in the pack; then `ytbrain eval gap`")
        return 0
    have = set(meta.get("domains") or ["startup"])
    mine = Path(args.questions) if args.questions else EVAL_DATA / "gap-questions.txt"
    if args.questions and not mine.exists():
        print(f"eval gap: --questions {mine}: no such file", file=sys.stderr)
        return 1
    try:
        probes = CV.gap_questions(mine, queries)
    except ValueError as e:
        print(f"eval gap: {e}", file=sys.stderr)
        return 1
    gap, retired = CV.active_gap(probes, have)
    answerable = [CV.Probe(i.qid, i.text, expected=i.expected) for i in R.questions(queries, R.doc_domains(store))]
    if not gap or not answerable:
        print("eval gap: need Gap questions and answerable questions in this pack "
              f"({len(gap)} Gap, {len(answerable)} answerable)", file=sys.stderr)
        return 1
    print(f"eval gap: {len(gap)} Gap questions, {len(answerable)} answerable", flush=True)
    rep = CV.report(store, gap, answerable, CV.observe(store, embed, gap, say=say),
                    CV.observe(store, embed, answerable, say=say), retired)
    print(CV.render(rep))
    if args.tune:
        shift = rep["recommended_shift"]
        if shift is None:
            print("eval gap: no shift meets both targets; nothing saved", file=sys.stderr)
        else:
            try:
                saved = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8")) if CALIBRATION_FILE.exists() else {}
            except (OSError, ValueError):
                saved = {}
            if saved.get("embed_model") not in (None, meta.get("embed_model")):
                saved = {}                                 # a curve for another embedding model says nothing about this pack
            total = CV.tuned_shift(meta, shift)            # the sweep is relative to the pack's own shift
            had = ((meta.get("calibration") or {}).get("shift") or 0.0)
            saved.update({"embed_model": meta.get("embed_model"), "shift": total})
            atomic_write_text(CALIBRATION_FILE, json.dumps(saved, indent=1))
            print(f"eval gap: saved shift {total:+.2f} to {CALIBRATION_FILE} (the pack's {had:+.2f} plus {shift:+.2f}); "
                  f"`ytbrain pack build` applies it")
    return CV.exit_code(rep)


def _saved_calibration() -> dict | None:
    """The curve `ytbrain eval calibrate` saved, if there is a readable one (a damaged file is no curve)."""
    from .config import CALIBRATION_FILE
    try:
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8")) if CALIBRATION_FILE.exists() else None
    except (OSError, ValueError):
        return None


def _domains():
    """The Domain registry (domains.yaml); exits on a config error."""
    from . import domains as D
    try:
        return D.load()
    except D.DomainConfigError as e:
        sys.exit(str(e))


def _doc_tags(m, doc_ids, default: bool = True) -> dict[str, tuple[list[str], str]]:
    """doc_id -> (Domains, Source id), from domains.yaml, sources.yaml and the manifest. A Book's
    folder is read from its plan (data/books/plans). Without a sources.yaml every Document is
    in the default Domain: tags are configuration and the index must still build. With
    default=False, only what a person declared (the tag Step's hints): [] where nothing was."""
    from . import domains as D
    from .config import BOOKS_PLANS
    reg = _domains()
    srcs = {s["id"]: s for s in _all_sources()} if SOURCES.exists() else {}
    out = {}
    for doc_id in doc_ids:
        row = m.get_document(doc_id)
        sid = (row["source_id"] if row is not None else None) or ""
        src = srcs.get(sid)
        book = roots = None
        if src and src["type"] == "pdf_books":
            try:
                book = json.loads((BOOKS_PLANS / f"{doc_id.split('__', 1)[0]}.json").read_text(encoding="utf-8")).get("path")
            except (OSError, ValueError):
                book = None
            roots = [(Path(p).expanduser() if Path(p).expanduser().is_absolute() else ROOT / p)
                     for p in src.get("paths") or [src["path"]]]
        try:
            out[doc_id] = (D.resolve(reg, src, book, roots or (), default=default), sid)
        except D.DomainConfigError as e:
            sys.exit(str(e))
    return out


def _visibility():
    """Which Documents are private, from sources.yaml (`distribute: false`) and the manifest."""
    from .visibility import Visibility
    return Visibility.from_manifest(Manifest(MANIFEST_DB), _all_sources())


def _enabled_sources() -> list[dict]:
    """Enabled YouTube playlists, specific ones first and catch-all uploads last (decision
    Q15): the fallback must not overwrite a series already set by a specific playlist.
    The `defaults:` block is merged into each (sources.normalize), so cmd_sync reads
    sleep_requests etc. off the source dict."""
    from . import sources as S
    srcs = S.enabled(_all_sources(), "youtube_playlist")
    return sorted(srcs, key=lambda s: bool(s.get("fallback")))


# --------------------------------------------------------------------------
# DISCOVERY METHODS
# --------------------------------------------------------------------------

def _discover_api() -> int:
    """Method 1: Manual YouTube Data API (user must have API key).
    
    Prints API calls that the user runs manually, then copy-pastes results.
    """
    print("Method: YouTube Data API v3 (manual)")
    print("="*60)
    print()
    print("Get an API key: https://console.cloud.google.com/")
    print("Enable YouTube Data API v3 in your project.")
    print()
    print("Run these API calls with your key, then paste results into sources.yaml:")
    print()
    
    seen = set()
    for s in _sources()["sources"]:
        cid = s.get("channel_id")
        if cid and cid not in seen:
            seen.add(cid)
            print(f"  channels.list?part=contentDetails&id={cid}")
            print(f"  playlists.list?part=snippet,contentDetails&channelId={cid}&maxResults=50")
    print()
    print("  videos.list?part=contentDetails&id=<up to 50 ids>   # sum durations")
    print()
    return 0


def _discover_ytdlp(channel_id_or_url: str) -> int:
    """Method 2: yt-dlp (direct YouTube scraping, no API key needed).

    Three facts about yt-dlp that this function exists to get right:

    1. Probing a channel ROOT returns the channel's TABS ("Videos", "Live",
       "Shorts") -- not videos and not playlists. Each tab carries the channel
       id, so writing them to sources.yaml yields three identical UC... ids
       that sync cannot enumerate (HTTP 400). Real playlists live on the
       /playlists tab; that is what we probe.
    2. 'id' is whatever you asked for -- the HANDLE (@ycombinator) if you
       passed a handle. The UC... channel id is in 'channel_id'. The uploads
       playlist is UU + channel_id[2:], and may only be derived from a real
       UC... id.
    3. _type is 'playlist' for channels too (extractor/common.py
       playlist_result hardcodes it), so it cannot distinguish the two.

    Without extract_flat, yt-dlp resolves every video one request at a time --
    on a large channel that looks like a hang. All probes here are flat.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        print("yt-dlp not found. Install with: uv pip install yt-dlp", file=sys.stderr)
        return 1

    import yaml

    FLAT = {"extract_flat": "in_playlist", "quiet": True,
            "no_warnings": True, "socket_timeout": 30}

    def probe(url, limit=None):
        opts = dict(FLAT)
        if limit:
            opts["playlistend"] = limit
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    print("Method: yt-dlp (direct YouTube scraping)")
    print("=" * 60)
    print()

    raw = channel_id_or_url.strip()
    if raw.startswith("@"):
        base, is_channel = f"https://www.youtube.com/{raw}", True
    elif raw.startswith("UC") and "/" not in raw:
        base, is_channel = f"https://www.youtube.com/channel/{raw}", True
    elif (raw.startswith("PL") or raw.startswith("UU")) and "/" not in raw:
        base, is_channel = f"https://www.youtube.com/playlist?list={raw}", False
    else:
        base = raw.rstrip("/")
        is_channel = "list=" not in base and "/playlist" not in base
        for tab in ("/videos", "/playlists", "/streams", "/shorts", "/featured"):
            if base.endswith(tab):
                base = base[: -len(tab)]

    updates: list[dict] = []

    # ---------------- direct playlist input ----------------
    if not is_channel:
        print(f"Probing:  {base}")
        sys.stdout.flush()
        try:
            info = probe(base, limit=1)
        except Exception as e:
            print(f"Failed: {e}", file=sys.stderr)
            return 1
        pid = info.get("id") or ""
        print(f"Loaded:   {info.get('title', 'Unknown')}  (id: {pid})")
        print()
        name = input("Series name for this playlist (blank to skip): ").strip()
        if name:
            updates.append({
                "id": re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"),
                "name": name, "kind": "playlist", "playlist_id": pid,
                "fallback": False, "verify": False,
            })
        print()
        return _write_sources(updates)

    # ---------------- channel: probe the /playlists tab ----------------
    print(f"Probing:  {base}/playlists")
    print("Fetching: flat playlist listing (a few seconds)...")
    sys.stdout.flush()
    try:
        info = probe(f"{base}/playlists", limit=200)
    except Exception as e:
        print(f"Failed to list playlists: {e}", file=sys.stderr)
        if "403" in str(e) or "proxy" in str(e).lower():
            print("  A 403 at a proxy means you are in a sandbox -- run this on the Mac.",
                  file=sys.stderr)
        return 1

    channel_id = info.get("channel_id") or ""
    channel_name = info.get("channel") or info.get("title") or "Unknown"
    print(f"Channel:  {channel_name}")
    print(f"UC id:    {channel_id or '(not reported)'}")
    print()

    found = []
    for e in info.get("entries") or []:
        if not e:
            continue
        pid = e.get("id") or ""
        # a playlist id is never the channel id
        if not pid or pid.startswith("UC"):
            continue
        found.append({"id": pid,
                      "title": e.get("title") or "Untitled",
                      "count": e.get("playlist_count")})

    if not found:
        print("No playlists found on this channel's /playlists tab.")
    else:
        print(f"Found {len(found)} playlist(s):")
        print()
        for i, pl in enumerate(found, 1):
            n = f"{pl['count']} videos" if pl["count"] else "? videos"
            print(f"  [{i:3d}] {pl['title'][:58]:<58} {n}")
            print(f"        {pl['id']}")
        print()
        print("Select: comma-separated numbers, ranges (1-5), 'all', or blank for none")
        sel = input("> ").strip().lower()

        picked = []
        if sel == "all":
            picked = list(range(len(found)))
        elif sel:
            for part in sel.split(","):
                part = part.strip()
                if "-" in part:
                    try:
                        a, b = (int(x) for x in part.split("-", 1))
                        picked += [i - 1 for i in range(a, b + 1)]
                    except ValueError:
                        print(f"  skipping bad range '{part}'", file=sys.stderr)
                else:
                    try:
                        picked.append(int(part) - 1)
                    except ValueError:
                        print(f"  skipping bad entry '{part}'", file=sys.stderr)
        picked = [i for i in dict.fromkeys(picked) if 0 <= i < len(found)]
        print()

        # Auto-slug every pick, then allow targeted renames. Prompting per
        # playlist is fine for a handful and intolerable for 'all' (55 here).
        taken = set()
        chosen = []
        for i in picked:
            pl = found[i]
            slug = re.sub(r"[^a-z0-9]+", "_", pl["title"].lower()).strip("_")[:40]
            base, n = slug, 2
            while slug in taken:          # two titles can slug identically
                slug = f"{base}_{n}"
                n += 1
            taken.add(slug)
            chosen.append({"slug": slug, "pl": pl})

        if len(chosen) <= 5:
            for c in chosen:
                print(f"  {c['pl']['title']}")
                custom = input(f"    series id [{c['slug']}]: ").strip()
                if custom:
                    c["slug"] = re.sub(r"[^a-z0-9]+", "_", custom.lower()).strip("_")
        else:
            print(f"Auto-named {len(chosen)} series:")
            for n, c in enumerate(chosen, 1):
                print(f"  [{n:3d}] {c['slug']}")
            print()
            print("Rename any? e.g.  3=paper_club, 7=neurips_2025   (blank to accept all)")
            edits = input("> ").strip()
            for part in edits.split(","):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                num, name = (x.strip() for x in part.split("=", 1))
                try:
                    idx = int(num) - 1
                except ValueError:
                    print(f"  skipping '{part}'", file=sys.stderr)
                    continue
                if 0 <= idx < len(chosen) and name:
                    chosen[idx]["slug"] = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

        for c in chosen:
            updates.append({
                "id": c["slug"], "name": c["pl"]["title"], "kind": "playlist",
                "playlist_id": c["pl"]["id"], "fallback": False, "verify": False,
            })
        if chosen:
            print()

    # ---------------- uploads catch-all ----------------
    if channel_id.startswith("UC"):
        uploads = f"UU{channel_id[2:]}"
        ans = input(f"Also add uploads catch-all {uploads}? [y/N]: ").strip().lower()
        if ans == "y":
            updates.append({
                "id": "uploads", "name": f"{channel_name} - Uploads",
                "kind": "playlist", "playlist_id": uploads,
                "fallback": True, "verify": False,   # loses to specific playlists (Q15)
            })
            print(f"  added {uploads} as catch-all (fallback: loses to specific playlists)")
        else:
            print("  skipped -- only videos inside the selected playlists will be ingested")
        print()
    elif channel_id:
        print(f"Not a UC id ({channel_id}) -- cannot derive uploads playlist; skipping.")
        print()

    # ---------------- manual additions ----------------
    print("Add any further playlists as:  series_name|playlist_id")
    print("  e.g.  CS183B 2014|PL11qn6zM2Y3bMZdChxEqHKaCaKUjwItGL")
    print("Blank line when done.")
    while True:
        entry = input("> ").strip()
        if not entry:
            break
        if "|" not in entry:
            print("  need: series_name|playlist_id", file=sys.stderr)
            continue
        name, pid = (p.strip() for p in entry.split("|", 1))
        if not name or not pid:
            print("  both parts required", file=sys.stderr)
            continue
        if pid.startswith("UC"):
            print("  that is a CHANNEL id, not a playlist id -- sync would 400 on it.",
                  file=sys.stderr)
            print(f"  the enumerable equivalent is UU{pid[2:]}", file=sys.stderr)
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if any(u["id"] == slug for u in updates):
            print(f"  '{slug}' already added", file=sys.stderr)
            continue
        updates.append({"id": slug, "name": name, "kind": "playlist",
                        "playlist_id": pid, "fallback": False, "verify": False})
        print(f"  added {name} ({pid})")

    return _write_sources(updates)


def _write_sources(updates: list[dict]) -> int:
    """Merge new sources into sources.yaml by id; fallbacks sort last (Q15)."""
    import yaml
    if not updates:
        print()
        print("Nothing selected; sources.yaml left unchanged.")
        return 0

    path = SOURCES
    if not path.exists():             # first discover: start from the template's defaults, no sources
        tmpl = yaml.safe_load(SOURCES_EXAMPLE.read_text()) if SOURCES_EXAMPLE.exists() else {}
        path.write_text(yaml.dump({"defaults": (tmpl or {}).get("defaults") or {}, "sources": []},
                                  default_flow_style=False, sort_keys=False))
    data = yaml.safe_load(path.read_text()) or {}
    existing = data.get("sources") or []
    new_ids = {u["id"] for u in updates}
    kept = [s for s in existing if s.get("id") not in new_ids]

    # A playlist must not appear twice under two series ids: sync would fetch
    # it twice and the later series would win the assignment. Keep the entry
    # already in the file (the user named it) and drop the incoming duplicate.
    owned = {s.get("playlist_id"): s.get("id") for s in kept}
    deduped = []
    for u in updates:
        prior = owned.get(u["playlist_id"])
        if prior:
            print(f"  skipping '{u['id']}': {u['playlist_id']} already held by '{prior}'")
            continue
        owned[u["playlist_id"]] = u["id"]
        deduped.append(u)
    merged = kept + deduped
    merged.sort(key=lambda s: bool(s.get("fallback")))
    data["sources"] = merged
    pages.atomic_write_text(path, yaml.dump(data, default_flow_style=False, sort_keys=False))

    print()
    print(f"sources.yaml now has {len(merged)} source(s):")
    for s in merged:
        tag = "  [fallback]" if s.get("fallback") else ""
        print(f"  {s['id']:<28} {s['playlist_id']}{tag}")
    print()
    print("Next: ytbrain sync --limit 5")
    return 0

def cmd_discover(args) -> int:
    """Phase 0: discover playlists and populate sources.yaml.
    
    Two methods:
      --method api       Manual YouTube Data API (user runs API calls manually)
      --method ytdlp     Automatic yt-dlp discovery (no API key needed, interactive)
    """
    method = args.method or "ytdlp"
    
    if method == "api":
        return _discover_api()
    elif method == "ytdlp":
        channel = args.channel or "@ycombinator"
        return _discover_ytdlp(channel)
    else:
        print(f"Unknown method: {method}", file=sys.stderr)
        return 1


@dataclass
class _SyncState:
    """Shared across every video of one sync: YouTube throttles per IP, so the
    429 streak and the stop decision span sources."""
    streak_429: int = 0
    throttled: bool = False
    new: int = 0
    tally: dict = field(default_factory=lambda: defaultdict(int))


def _fetch_video(m: Manifest, row: dict, src: dict, is_fallback: bool, st: _SyncState,
                 label: str, sub_sleep: float) -> None:
    """Fetch one video's captions with per-video retries and record the result."""
    title = (row["title"] or "")[:44]
    print(f"    {label} {row['doc_id']}  {title} ... ", end="")
    sys.stdout.flush()

    # Retry THIS video with backoff before moving on. yt-dlp never
    # retries a subtitle 429, and YouTube throttles per IP: a 429 gets
    # an exponential cooldown (30, 60, 120, 240s), shared with the
    # streak breaker below; other transient errors get 10, 20s.
    # Private/removed videos and "no captions" are final at once.
    cooldown_after = 0
    for attempt in range(YT_VIDEO_RETRIES + 1):
        res = fetch.fetch_captions(row["doc_id"],
                                   sub_langs=src.get("sub_langs", fetch.DEFAULT_SUB_LANGS),
                                   sleep=float(src.get("sleep_requests", 1.5)),
                                   sleep_subtitles=sub_sleep)
        ok = res["returncode"] == 0 and res["caption_path"]
        if ok:
            status, err = "ok", None
        elif res["returncode"] != 0 and (perm := fetch.permanent_failure(res["stderr_tail"])):
            status, err = "skipped", f"unavailable: {perm}"
        elif res["returncode"] != 0:
            # 'failed', not 'skipped': a rate limit is not evidence
            # that the video has no captions -- the next sync retries it.
            status, err = "failed", res["stderr_tail"]
        elif res["caption_kind"] == "missing_file":
            # the track is listed but yt-dlp wrote no file (usually a missing PO token or a
            # dropped download): not evidence of "no captions", so it stays retryable
            status, err = "failed", "caption track listed but no file was written (PO token or download failure)"
        else:
            status, err = "skipped", f"no captions ({res['caption_kind']})"
        if status != "failed":
            st.streak_429 = 0
            break
        rate_limited = "429" in (err or "")
        if rate_limited:
            st.streak_429 += 1
            if st.streak_429 >= YT_MAX_429_STREAK:
                st.throttled = True
                break
            wait = min(YT_COOLDOWN_BASE_S * 2 ** (st.streak_429 - 1), YT_COOLDOWN_MAX_S)
        else:
            wait = YT_ERROR_BACKOFF_S * 2 ** attempt
        if attempt == YT_VIDEO_RETRIES:
            if rate_limited:        # still throttled: cool down before the NEXT video too
                cooldown_after = wait
            break
        print(f"{fetch.failure_reason(err)} -- retry {attempt + 1}/{YT_VIDEO_RETRIES} "
              f"in {wait}s ... ", end="", flush=True)
        time.sleep(wait)

    info = fetch.info_fields(res.get("info_path"))
    m.upsert_document(row["doc_id"], src["id"], is_fallback=is_fallback,
                      caption_kind=res["caption_kind"],
                      has_captions=int(res["caption_kind"] in ("human", "auto")),
                      published_at=info.get("published_at"),
                      duration_s=info.get("duration_s") or row.get("duration_s"),
                      **({"provenance": p} if (p := _provenance_for(src, info)) else {}))
    if status == "ok" and m.stage_status(row["doc_id"], "clean") is not None:
        # a re-fetch produced new captions: clean (and everything after) must redo
        m.invalidate_docs("clean", [row["doc_id"]], "re-fetched")
    m.mark(StageState(row["doc_id"], "fetch", status,
                      input_hash=row["doc_id"],
                      output_hash=fetch.file_hash(res["caption_path"]) if res["caption_path"] else None,
                      attempts=attempt + 1, error=err))
    if ok:
        print(res["caption_kind"])
    elif status == "failed":
        print(f"FAILED ({fetch.failure_reason(err)}) -- retried next sync")
    elif err.startswith("unavailable"):
        print(f"UNAVAILABLE ({err.split(': ', 1)[1].lower()}) -- won't retry")
    else:
        print(f"NO CAPTIONS ({res['caption_kind']})")
    sys.stdout.flush()
    st.tally[status if status != "skipped" or not err.startswith("unavailable") else "unavailable"] += 1
    st.new += 1

    if cooldown_after and not st.throttled:
        print(f"    rate limited -- cooling down {cooldown_after}s before the next video",
              flush=True)
        time.sleep(cooldown_after)


def cmd_sync(args) -> int:
    """Fetch every enabled Source: YouTube playlists (captions only, never video) and
    websites (pages, politely), through each Source type's adapter (ADR-0013).

    Progress is printed per source and per Document: a quiet run here is
    indistinguishable from a hang, and with polite pacing a full pass takes minutes.
    """
    from . import sources as S
    m = Manifest(MANIFEST_DB)
    every = _all_sources()
    only = getattr(args, "source", None)
    if only and not any(s["id"] == only for s in every):
        print(f"sync: no source {only!r} in sources.yaml", file=sys.stderr)
        return 1
    kind = SYNC_TYPES.get(getattr(args, "source_type", None) or "")
    if kind and getattr(args, "backfill", False) and kind != "youtube_playlist":
        print("sync: --backfill retries YouTube caption fetches; it can't be combined with --type website",
              file=sys.stderr)
        return 1
    chosen = [s for s in every if (not only or s["id"] == only) and (not kind or s["type"] == kind)]
    if only and not chosen:
        what = next(s["type"] for s in every if s["id"] == only)
        print(f"sync: {only} is a {dict(website='website', pdf_books='pdf_books Source').get(what, 'YouTube playlist')}, "
              f"not --type {args.source_type}", file=sys.stderr)
        return 1
    for s in chosen:
        if not s["enabled"]:
            print(f"sync: {s['id']} is disabled (enabled: false) -- skipped")
    yt = sorted(S.enabled(chosen, "youtube_playlist"), key=lambda s: bool(s.get("fallback")))
    web = S.enabled(chosen, "website")
    books = S.enabled(chosen, "pdf_books")
    if not yt and not web and not books:
        print("sync: no enabled sources in sources.yaml", file=sys.stderr)
        return 1
    code = 0
    # --type website/book skips the YouTube pass (talks found on the sites this run are still fetched below)
    if kind not in ("website", "pdf_books") and (yt or getattr(args, "backfill", False) or m.db.execute(
            "SELECT 1 FROM documents d LEFT JOIN stage_state s ON s.doc_id=d.doc_id AND s.stage='fetch' "
            "WHERE d.doc_type='youtube' AND s.doc_id IS NULL AND d.tombstoned_at IS NULL LIMIT 1").fetchone()):
        code = S.adapter("youtube_playlist").sync(m, yt, args).get("code", 0)
    if web and not getattr(args, "backfill", False):
        res = S.adapter("website").sync(m, web, args)
        code = code or res.get("code", 0)
        if res.get("videos"):             # talks found on websites: fetch their captions too
            print(f"sync: {res['videos']} new video(s) found on websites -- fetching their captions")
            code = S.adapter("youtube_playlist").sync(m, [], args).get("code", 0) or code
    if books and not getattr(args, "backfill", False):
        code = S.adapter("pdf_books").sync(m, books, args).get("code", 0) or code
    return code


def _sync_youtube(m: Manifest, srcs: list[dict], args) -> dict:
    """The YouTube adapter's sync: enumerate playlists, fetch captions, then any video another
    Source found (a website embedding a talk) that has no caption fetch yet."""
    failed = skipped_known = 0
    st = _SyncState()
    _ytdlp_preflight()
    if getattr(args, "backfill", False):
        return {"code": _sync_backfill(m, srcs, st, args)}
    sub_sleep = YT_SLEEP_SUBTITLES if getattr(args, "sleep_subtitles", None) is None \
        else args.sleep_subtitles

    lim = args.limit or None
    if srcs:
        print(f"sync: {len(srcs)} playlist(s), limit={lim or 'none'} per playlist")
    sys.stdout.flush()

    for si, src in enumerate(srcs, 1):
        if st.throttled:
            break
        head = f"[{si}/{len(srcs)}] {src['id']}"
        is_fallback = bool(src.get("fallback"))
        print(f"{head}: enumerating {src['playlist_id']} ...")
        sys.stdout.flush()
        rows = None
        for attempt in range(YT_ENUM_RETRIES + 1):
            try:
                rows = fetch.enumerate_playlist(src["playlist_id"], limit=lim)
                break
            except Exception as e:
                last_err = e
                if attempt == YT_ENUM_RETRIES or "403" in str(e) or "proxy" in str(e).lower():
                    break
                wait = 30 * 2 ** attempt
                print(f"{head}: enumeration failed ({fetch.failure_reason(str(e))}); "
                      f"retrying in {wait}s", flush=True)
                time.sleep(wait)
        if rows is None:
            e = last_err
            print(f"{head}: enumeration FAILED: {e}", file=sys.stderr)
            if "403" in str(e) or "proxy" in str(e).lower():
                print("  A 403 at a proxy means you are in a sandbox -- run this on the Mac.",
                      file=sys.stderr)
            failed += 1
            continue

        print(f"{head}: {len(rows)} video(s)")
        sys.stdout.flush()

        # fetch-settled, not merely enumerated: see Manifest.stage_settled_ids
        known = m.stage_settled_ids("fetch")
        to_fetch = sum(1 for r in rows if args.force or r["doc_id"] not in known)
        if to_fetch:
            print(f"{head}: {to_fetch} to fetch, {len(rows) - to_fetch} already settled", flush=True)
        fetched, t_src = 0, time.time()
        for ri, row in enumerate(rows, 1):
            fields = dict(title=row["title"], duration_s=row.get("duration_s"),
                          series=_series_for(src),
                          url=f"https://www.youtube.com/watch?v={row['doc_id']}")
            if src.get("provenance"):       # else keep what fetch recorded (the publisher)
                fields["provenance"] = src["provenance"]
            m.upsert_document(row["doc_id"], src["id"], is_fallback=is_fallback, **fields)
            if row["doc_id"] in known and not args.force:
                skipped_known += 1
                continue

            _fetch_video(m, row, src, is_fallback, st, f"({ri}/{len(rows)})", sub_sleep)
            fetched += 1
            if fetched % 10 == 0 and fetched < to_fetch:
                per = (time.time() - t_src) / fetched
                print(f"    {head}: {fetched}/{to_fetch} fetched · {_dur(per)}/video · "
                      f"ETA {_dur(per * (to_fetch - fetched))}", flush=True)
            if st.throttled:
                print(f"\nsync: {st.streak_429} rate-limit errors in a row -- YouTube is throttling "
                      f"this IP. Stopping early; re-run later (failed videos are retried).")
                break

        if args.reconcile:
            gone = m.tombstone_missing(src["id"], [r["doc_id"] for r in rows])
            if gone:
                print(f"{head}: tombstoned {len(gone)} removed videos")

    # videos other Sources found (e.g. a website page embedding a talk): fetch like any other
    orphans = [dict(r) for r in m.db.execute(
        "SELECT d.doc_id, d.title, d.duration_s, d.source_id FROM documents d LEFT JOIN stage_state s "
        "ON s.doc_id=d.doc_id AND s.stage='fetch' WHERE d.doc_type='youtube' AND s.doc_id IS NULL "
        "AND d.tombstoned_at IS NULL ORDER BY d.first_seen").fetchall()]
    if orphans and not st.throttled:
        print(f"videos found on other sources: {len(orphans)} to fetch", flush=True)
        for i, row in enumerate(orphans, 1):
            _fetch_video(m, row, {"id": row["source_id"]}, True, st, f"({i}/{len(orphans)})", sub_sleep)
            if st.throttled:
                break

    print()
    t = st.tally
    print(f"sync: {st.new} attempted ({t['ok']} ok, {t['skipped']} no captions, "
          f"{t['unavailable']} unavailable, {t['failed']} failed -- retried next sync), "
          f"{skipped_known} already known, {failed} source(s) failed")
    return {"code": 1 if failed and not st.new else 0}


def _ytdlp_preflight() -> None:
    """Say what yt-dlp is missing before a long sync, not after 49 rate limits."""
    for line in fetch.environment_report():
        print(f"  {line}", flush=True)


def _sync_backfill(m: Manifest, srcs: list[dict], st: _SyncState, args) -> int:
    """Retry only videos whose caption fetch failed for a reason a retry can fix
    (mostly HTTP 429), slowly: no playlist listing, at most --per-hour videos an
    hour, and a longer pause before each subtitle download. Meant to be left
    running (e.g. under caffeinate) until the backlog is gone."""
    by_id = {s["id"]: s for s in srcs}
    rows = m.db.execute(
        "SELECT d.doc_id, d.title, d.duration_s, d.source_id FROM stage_state s "
        "JOIN documents d USING (doc_id) WHERE s.stage='fetch' AND s.status='failed' "
        # least recently tried first: a video that just got a 429 goes to the back,
        # so restarting doesn't hit the same stubborn video first every time
        "AND d.tombstoned_at IS NULL ORDER BY s.updated_at ASC, d.published_at DESC").fetchall()
    rows = [dict(r) for r in rows if not fetch.permanent_failure(
        m.db.execute("SELECT error FROM stage_state WHERE doc_id=? AND stage='fetch'",
                     (r["doc_id"],)).fetchone()["error"])]
    if args.limit:
        rows = rows[:args.limit]
    per_hour = max(1, args.per_hour or YT_BACKFILL_PER_HOUR)
    gap = 3600 / per_hour
    sub_sleep = max(YT_SLEEP_SUBTITLES, YT_BACKFILL_SLEEP_SUBTITLES) \
        if getattr(args, "sleep_subtitles", None) is None else args.sleep_subtitles
    print(f"sync --backfill: {len(rows)} video(s) to retry, <= {per_hour}/hour "
          f"(~{_dur(len(rows) * gap)}), {sub_sleep:g}s before each subtitle download", flush=True)
    last = 0.0
    for i, row in enumerate(rows, 1):
        wait = last + gap - time.time()
        if last and wait > 0:
            print(f"    next in {_dur(wait)} · {len(rows) - i + 1} left, ETA ~{_dur((len(rows) - i + 1) * gap)} ...",
                  flush=True)
            time.sleep(wait)
        last = time.time()
        src = by_id.get(row["source_id"]) or {"id": row["source_id"]}
        _fetch_video(m, row, src, bool(src.get("fallback")), st,
                     f"({i}/{len(rows)})", sub_sleep)
        if st.throttled:
            print(f"\nsync --backfill: {st.streak_429} rate limits in a row -- stopping; "
                  f"try again in a few hours (the rest stay queued).")
            break
    t = st.tally
    print(f"\nsync --backfill: {st.new} retried ({t['ok']} ok, {t['skipped']} no captions, "
          f"{t['unavailable']} unavailable, {t['failed']} still failing)")
    return 0

HEARTBEAT_S = 60          # multi-worker extract: report in-flight videos after this much silence
from .config import EVAL_TIMEOUT_S as _CALL_TIMEOUT_S   # noqa: E402
ENRICH_CALL_DEADLINE_S = 3 * _CALL_TIMEOUT_S + 60    # a model call still unanswered after this is given up on
from .config import LLM_TIMEOUT_S as _REQUEST_TIMEOUT_S   # noqa: E402
# extract: a Document with no new step for this long is given up on. One honest request ends within LLM_TIMEOUT_S
# and every retry or backoff reports a step, so only a request that trickles bytes (never trips the timeout) stalls
EXTRACT_STALL_S = _REQUEST_TIMEOUT_S + 300


def _sweep_partial_files() -> None:
    """Remove temp files a killed run left behind. Only called while holding the
    run lock, so nothing else can be mid-write. Finished files are never touched:
    every writer here writes <tmp> then renames, and yt-dlp writes <name>.part
    (subtitles) or <name>.<rand>.tmp (info.json) then renames."""
    from .config import WEB_RAW
    n = 0
    for d in (RAW, TRANSCRIPTS, METADATA, PAGES, REPORTS, EXTRACT_FAILURES):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.name.endswith((".part", ".tmp", ".ytdl")):
                f.unlink(missing_ok=True)
                n += 1
    for f in (WEB_RAW.rglob("*.tmp") if WEB_RAW.is_dir() else []):     # saved web pages, one folder per site
        f.unlink(missing_ok=True)
        n += 1
    if n:
        print(f"(removed {n} partial file(s) left by an interrupted run)", flush=True)


def _unreadable_json(path: Path, required_key: str) -> str | None:
    """None if `path` is a JSON object with `required_key`; else what is wrong.
    Files are written atomically now, but a crash in an older version, a disk
    error or a hand edit can still leave one damaged -- that must send the doc
    back to the stage that produces the file, not fail forever downstream."""
    if not path.exists():
        return "missing"
    try:
        obj = json.loads(path.read_text())
    except (ValueError, UnicodeDecodeError):
        return "corrupt (not valid JSON)"
    if not isinstance(obj, dict) or required_key not in obj:
        return f"incomplete (no '{required_key}')"
    return None


def _item(i: int, n: int, doc_id: str, title: str | None) -> None:
    """Per-video progress line, same shape as sync's; the caller appends the result."""
    print(f"    ({i}/{n}) {doc_id}  {(title or '')[:44]:44} ... ", end="", flush=True)


def _dur(s: float) -> str:
    s = int(s)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _ready(m: Manifest, stage: str, upstream: set[str], limit: int) -> tuple[list[str], int]:
    """pending(stage) split into (ready, not_ready), with --limit applied to READY docs.

    Limiting before filtering meant `extract --limit 3` could pick three docs
    whose upstream stage hadn't finished and do nothing at all.
    """
    pend = m.pending(stage)
    ready = [d for d in pend if d in upstream]
    return (ready[:limit] if limit else ready), len(pend) - len(ready)


def _only(ids: set[str], args) -> set[str]:
    """`--doc PREFIX` (repeatable): only Documents whose id starts with one of them. A Book's
    ISBN picks all its Chapters (`extract --doc 9780753550304` pilots one Book)."""
    prefixes = getattr(args, "doc", None) or []
    return {d for d in ids if d.startswith(tuple(prefixes))} if prefixes else ids


def _step_failed(m: Manifest, stage: str, doc_id: str, e: Exception) -> str:
    """One talk's unexpected error: record it and move on, so a single bad talk can't stop
    the Step (or the scheduled run) for every other talk. Parked after STEP_FAILURE_CAP runs."""
    status = m.fail(doc_id, stage, f"{type(e).__name__}: {e}")
    print(f"FAILED ({type(e).__name__}: {str(e)[:100]})"
          + (" -- parked; `ytbrain " + stage + " --retry-failed` to try again" if status == "parked"
             else " -- retried next run"), flush=True)
    return status


def cmd_clean(args) -> int:
    """Raw files -> transcripts: srt -> ~10-25s utterances (talks); page -> paragraphs (articles)."""
    from . import sources as S
    m = Manifest(MANIFEST_DB)
    n = skipped = refetch = errors = 0
    if getattr(args, "retry_failed", False) and (k := m.unpark("clean")):
        print(f"clean: retrying {k} parked Document(s)", flush=True)
    # yt-dlp writes subtitle files in place, so an interrupted fetch can leave
    # a truncated .srt on disk. The doc row exists (written at enumeration), so
    # pending() offers it -- cleaning it would bank a partial transcript as
    # 'ok'. Wait until sync has settled the fetch stage for that doc.
    todo, deferred = _ready(m, "clean", m.stage_settled_ids("fetch"), args.limit)
    print(f"clean: {len(todo)} to clean" + (f", {deferred} waiting on sync" if deferred else ""),
          flush=True)
    started = time.time()
    for i, doc_id in enumerate(todo, 1):
        doc = m.get_document(doc_id)
        _item(i, len(todo), doc_id, doc["title"] if doc else None)
        try:
            outcome = S.adapter_for_doc_type(doc["doc_type"] if doc else None).clean(m, doc_id, doc)
        except Exception as e:                       # noqa: BLE001 -- isolate one bad talk
            _step_failed(m, "clean", doc_id, e)
            errors += 1
            continue
        n += outcome == "ok"
        skipped += outcome == "skipped"
        refetch += outcome == "refetch"
        if i % 25 == 0 and i < len(todo):
            per = (time.time() - started) / i
            print(f"    clean: {i}/{len(todo)}, ETA {_dur(per * (len(todo) - i))}", flush=True)
    msg = f"\nclean: {n} Document(s) cleaned, {skipped} skipped (no captions or text, or too short)"
    if refetch:
        msg += f", {refetch} sent back to sync (caption file missing)"
    if errors:
        msg += f", {errors} failed (see above)"
    if deferred:
        msg += f", {deferred} deferred (fetch incomplete -- re-run sync)"
    print(msg)
    return 0


def _clean_one(m: Manifest, doc_id: str, doc) -> str:
    info_path = RAW / f"{doc_id}.info.json"
    sel = fetch.select_caption(doc_id, info_path if info_path.exists() else None)
    if not sel["caption_path"]:
        if m.stage_status(doc_id, "fetch") != "ok":
            # fetch settled as skipped: the video has no captions; nothing to clean
            m.mark(StageState(doc_id, "clean", "skipped", error="no captions"))
            print("NO CAPTIONS", flush=True)
            return "skipped"
        # fetch said ok, so a caption existed: the file was deleted or never
        # landed. Settling clean as 'skipped' would lose the video for good
        # -- send it back to sync instead.
        m.mark(StageState(doc_id, "fetch", "stale", error="caption file missing on disk"))
        print("CAPTION FILE MISSING -- re-fetched on next sync", flush=True)
        return "refetch"
    h = fetch.file_hash(sel["caption_path"])
    events = captions.dedup_rolling(captions.parse_srt(Path(sel["caption_path"]).read_text()))
    utts = captions.restore_punctuation(captions.merge_utterances(events))
    words = sum(len(u.text.split()) for u in utts)
    if words < MIN_TRANSCRIPT_WORDS:
        m.mark(StageState(doc_id, "clean", "skipped", input_hash=h,
                          error=f"transcript too short ({words} words)"))
        print(f"TOO SHORT ({words} words) -- not extracted", flush=True)
        return "skipped"
    rec = captions.to_transcript(utts, {
        "doc_id": doc_id,
        "title": doc["title"] if doc else None,
        "series": doc["series"] if doc else None,
        "published_at": doc["published_at"] if doc else None,
        "caption_kind": sel["caption_kind"]})
    body = pages.canonical_json(rec)
    out_hash = hashlib.sha256(body.encode()).hexdigest()[:16]
    prev = m.stage(doc_id, "clean")
    # New captions (a re-fetch, human captions replacing auto ones) mean a new transcript:
    # the record extracted from the old one, and its evidence timestamps, are out of date.
    # Mark extract stale first, so a crash after writing can't leave the old record 'ok'.
    changed = prev is not None and (
        (prev["output_hash"] and prev["output_hash"] != out_hash)
        or (not prev["output_hash"] and prev["input_hash"] and prev["input_hash"] != h))
    if changed and m.stage_status(doc_id, "extract") is not None:
        m.mark(StageState(doc_id, "extract", "stale", error="transcript changed"))
    pages.atomic_write_text(TRANSCRIPTS / f"{doc_id}.json", body)
    m.mark(StageState(doc_id, "clean", "ok", input_hash=h, output_hash=out_hash, attempts=1))
    m.succeeded(doc_id, "clean")
    print(f"{sel['caption_kind']} · {len(utts)} utterances"
          + (" · transcript changed: re-extract queued" if changed else ""), flush=True)
    return "ok"


def cmd_extract(args) -> int:
    """One LLM call per video; falls back to per-chapter only when it must."""
    from .config import (EXTRACT_VIDEO_BACKOFF_S, EXTRACT_VIDEO_RETRIES, LLM_BACKEND,
                         LLM_BASE_URL, LLM_MODEL, LLM_WORKERS)
    from .extract import runner
    m = Manifest(MANIFEST_DB)
    n = failed = 0
    if getattr(args, "retry_failed", False) and (k := m.unpark("extract")):
        print(f"extract: retrying {k} parked Document(s)", flush=True)
    from .extract import versions
    # ADR-0018: only a breaking release (or an unknown stamp) makes a record stale; a compatible
    # one leaves it valid, so a prompt change no longer re-extracts the whole Library
    redo = m.invalidate_versions("extract", versions.is_current,
                                 "made by a prompt version a breaking release replaced")
    if redo:
        print(f"extract: {redo} record(s) were made by a prompt version a breaking release replaced; "
              f"they will be re-extracted")
    todo, waiting = _ready(m, "extract", _only(m.stage_settled_ids("clean", ("ok",)), args), args.limit)
    if todo and LLM_BACKEND == "openai":
        # Same up-front check for LM Studio / vLLM / hosted endpoints.
        import httpx
        base = os.environ.get("YTBRAIN_LLM_BASE_URL", "http://localhost:8000/v1")
        try:
            r = httpx.get(f"{base}/models", timeout=10,
                          headers={"Authorization": f"Bearer {os.environ.get('YTBRAIN_LLM_API_KEY', 'not-needed')}"})
            ids = {x.get("id") for x in r.json().get("data", [])}
        except Exception as e:
            print(f"extract: LLM endpoint not reachable at {base} ({e}).\n"
                  f"  LM Studio: Developer tab -> Start Server (default http://localhost:1234/v1).",
                  file=sys.stderr)
            return 1
        # Some endpoints list ids with a prefix (Gemini: "models/gemini-...")
        # but accept the bare name in requests.
        if ids and LLM_MODEL not in ids and f"models/{LLM_MODEL}" not in ids:
            print(f"extract: model '{LLM_MODEL}' not served at {base}. "
                  f"Set YTBRAIN_LLM_MODEL to one of: {sorted(ids)}", file=sys.stderr)
            return 1
        # Being *listed* is not being *callable*: NVIDIA's catalog lists models
        # that 404 ("Function not found for account") or queue indefinitely on
        # the free tier. One tiny real call proves the path end to end.
        print(f"extract: checking {LLM_MODEL} responds ...", end=" ", flush=True)
        # A 5-token ping answers in 1-3s when the endpoint is healthy, so a
        # short timeout per try fails fast; a few tries ride out a brief queue.
        per_try = float(os.environ.get("YTBRAIN_LLM_PREFLIGHT_S", "20"))
        body = {"model": LLM_MODEL, "max_tokens": 5,
                "messages": [{"role": "user", "content": "Say ok"}]}
        if os.environ.get("YTBRAIN_LLM_EXTRA_BODY"):
            body.update(json.loads(os.environ["YTBRAIN_LLM_EXTRA_BODY"]))
        auth = {"Authorization": f"Bearer {os.environ.get('YTBRAIN_LLM_API_KEY', 'not-needed')}"}
        t0 = time.time()
        r = None
        for attempt in range(1, 5):
            try:
                r = httpx.post(f"{base}/chat/completions", json=body, timeout=per_try, headers=auth)
            except httpx.TimeoutException:
                print(f"no answer in {per_try:.0f}s (try {attempt}/4) ...", end=" ", flush=True)
                r = None
                continue
            # A rate limit or a 5xx is transient: back off and ask again instead of
            # declaring the model uncallable (a busy minute must not cancel a whole run).
            if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < 4:
                wait = min(60.0, float(r.headers.get("retry-after") or 0) or 5.0 * 2 ** (attempt - 1))
                print(f"HTTP {r.status_code}, retrying in {wait:.0f}s ...", end=" ", flush=True)
                time.sleep(wait)
                continue
            break
        if r is None:
            print(f"\nextract: '{LLM_MODEL}' is queued or down right now; nothing was marked "
                  f"failed. Re-run in a few minutes or pick another model "
                  f"(python ops/probe_models.py).", file=sys.stderr)
            return 1
        if r.status_code >= 400:
            print(f"HTTP {r.status_code}.\nextract: '{LLM_MODEL}' is listed but not callable: "
                  f"{r.text[:200]}", file=sys.stderr)
            return 1
        print(f"ok ({time.time() - t0:.1f}s)", flush=True)
    if todo and LLM_BACKEND == "ollama":
        # Fail once, up front: otherwise every video fails in 0s and all of
        # them are marked 'failed' before you notice Ollama isn't running.
        import httpx
        try:
            tags = httpx.get(f"{LLM_BASE_URL}/api/tags", timeout=5).json()
        except Exception as e:
            print(f"extract: Ollama not reachable at {LLM_BASE_URL} ({e}).\n"
                  f"  Start it with `ollama serve` in another terminal.", file=sys.stderr)
            return 1
        have = {t.get("name") for t in tags.get("models", [])}
        if LLM_MODEL not in have and f"{LLM_MODEL}:latest" not in have:
            print(f"extract: model '{LLM_MODEL}' is not pulled. Run `ollama pull {LLM_MODEL}`"
                  f" (or set YTBRAIN_LLM_MODEL). Available: {sorted(have) or 'none'}",
                  file=sys.stderr)
            return 1
    from .config import LLM_MAX_RPM
    workers = max(1, int(getattr(args, "workers", 0) or LLM_WORKERS))
    endpoint = os.environ.get("YTBRAIN_LLM_BASE_URL", LLM_BASE_URL)
    local = LLM_BACKEND == "ollama" or runner.is_local_endpoint(endpoint)
    pacing = "no rate pacing (local server)" if local else f"<= {LLM_MAX_RPM:g} requests/min shared"
    print(f"extract: {len(todo)} to extract with {LLM_BACKEND}/{LLM_MODEL}, "
          f"{workers} worker(s), {pacing}"
          + (f", {waiting} not cleaned yet (no transcript or text: see `ytbrain status`)" if waiting else "")
          + (f", {p} parked (`--retry-failed`)" if (p := m.parked("extract")) else ""),
          flush=True)
    if workers > 1 and local:
        print(f"  note: a local server only runs these in parallel if it allows it "
              f"(LM Studio: Max Concurrent Predictions >= {workers}); otherwise they just queue.",
              flush=True)

    # Jobs are prepared (and anything already broken is recorded) on the main
    # thread; workers only make LLM calls. Every manifest/file write stays on
    # the main thread, in completion order, so SQLite has one writer and
    # Ctrl+C semantics are unchanged: finished videos are saved, in-flight
    # ones were never marked and are simply redone next run.
    jobs = []
    for doc_id in todo:
        doc = dict(m.get_document(doc_id) or {})   # sqlite3.Row has no .get()
        problem = _unreadable_json(TRANSCRIPTS / f"{doc_id}.json", "utterances")
        if problem:                      # clean said ok, but the file is gone or damaged
            m.mark(StageState(doc_id, "clean", "stale", error=f"transcript {problem}"))
            print(f"    {doc_id}  transcript {problem} -- sent back to clean", flush=True)
            failed += 1
            continue
        jobs.append((doc_id, doc))

    total = len(jobs)
    live = workers == 1              # 1 worker: stream each step as it happens
    work: queue.Queue = queue.Queue()
    done: queue.Queue = queue.Queue()
    stop = threading.Event()
    # Live mode (1 worker) streams each step to the terminal, so the worker
    # must not start printing the next video until the main thread has
    # written this one's result line.
    printed = threading.Semaphore(0)
    in_flight: dict[str, tuple[float, list[str]]] = {}   # doc_id -> (start, steps so far)
    progress: dict[str, tuple[int, float]] = {}          # doc_id -> (steps seen, when the last new one appeared)
    abandoned: set[str] = set()                          # Documents given up on while a call never answered
    titles = {d: doc.get("title") for d, doc in jobs}
    for i, job in enumerate(jobs, 1):
        work.put((i, *job))
    # ADR-0018: the prompt variant per Document, from the Domains you declared for it (a Source, a Book folder)
    parity = versions.parity_passed()
    known = _doc_domains(m, [j[0] for j in jobs], declared=True)
    variant_of = {j[0]: versions.variant_for(None, known.get(j[0]), parity) for j in jobs}
    if (n_neutral := sum(1 for v in variant_of.values() if v == "neutral")):
        print(f"extract: {n_neutral} of {len(jobs)} Document(s) with the subject-neutral prompt "
              f"({versions.stamp('neutral')})", flush=True)

    def worker() -> None:
        while not stop.is_set():
            try:
                i, doc_id, doc = work.get_nowait()
            except queue.Empty:
                return
            steps: list[str] = []
            if live:
                _item(i, total, doc_id, doc.get("title"))
                on_step = lambda s: (steps.append(s), print(f"{s} · ", end="", flush=True))
            else:
                on_step = steps.append
            info_path = RAW / f"{doc_id}.info.json"
            t0 = time.time()
            in_flight[doc_id] = (t0, steps)
            record, err = None, None
            # Whole-video retries with exponential backoff, on top of the
            # per-request retries inside the runner: they cover what outlasts
            # those (a provider down for several minutes, repeated timeouts).
            for attempt in range(EXTRACT_VIDEO_RETRIES + 1):
                try:
                    tr = json.loads((TRANSCRIPTS / f"{doc_id}.json").read_text())
                    talk = (tr.get("source_kind") or "talk") == "talk"
                    record = runner.extract_video(
                        tr,
                        {"doc_id": doc_id, "title": doc["title"], "series": doc["series"],
                         "provenance": doc["provenance"], "published_at": doc["published_at"],
                         "duration_s": doc["duration_s"], "caption_kind": doc["caption_kind"],
                         "url": doc.get("url") or tr.get("url"), "source_kind": tr.get("source_kind") or "talk",
                         "speaker": tr.get("speaker"), "private": tr.get("private"),
                         "page_labels": tr.get("page_labels"), "prompt_variant": variant_of.get(doc_id, "startup")},
                        fetch.uploader_chapters(info_path if info_path.exists() else None) if talk
                        else tr.get("chapters") or [],          # an article's or a Chapter's headings
                        on_step=on_step,
                    )
                    err = None
                    break
                except (runner.BackendUnavailable, runner.RequestRejected) as e:
                    # bad key/model/credits: stop the run; a rejected request (400/422): the
                    # same request gets the same answer. Neither is worth a video retry.
                    err = e
                    break
                except Exception as e:
                    err = e
                    # The model could not produce valid JSON even after repairs
                    # and a chapter split: same prompt, near-same answer -- a
                    # retry would only burn tokens. Leave it for a later run
                    # (e.g. `invalidate extract --only-flagged` with another model).
                    if "failed validation" in str(e) or attempt == EXTRACT_VIDEO_RETRIES \
                            or stop.is_set():
                        break
                    wait = EXTRACT_VIDEO_BACKOFF_S * 2 ** attempt * random.uniform(0.8, 1.2)
                    on_step(f"video attempt {attempt + 1} failed ({str(e)[:60]}); retrying in {wait:.0f}s")
                    stop.wait(wait)
            in_flight.pop(doc_id, None)
            done.put((doc_id, doc, record, err, steps, time.time() - t0))
            if live:
                printed.acquire()

    # daemon threads: Ctrl+C exits at once instead of waiting on in-flight calls
    threads = [threading.Thread(target=worker, daemon=True, name=f"extract-{k}")
               for k in range(min(workers, total))]
    started = time.time()
    for t in threads:
        t.start()

    k = 0
    last_news = time.time()
    while k < total:
        try:                                             # short waits keep Ctrl+C responsive
            doc_id, doc, record, err, steps, dt = done.get(timeout=0.5)
            last_news = time.time()
            if doc_id in abandoned:
                continue                                 # a late answer to a Document already given up on
        except queue.Empty:
            if not any(t.is_alive() for t in threads) and done.empty():
                break                                    # workers gone (should not happen)
            now = time.time()
            for dd, (st_, stp) in list(in_flight.items()):    # one stalled request must not hold the whole run
                seen, since = progress.get(dd, (-1, now))
                if len(stp) != seen:
                    progress[dd] = (len(stp), now)
                elif now - since > EXTRACT_STALL_S and dd not in abandoned:
                    abandoned.add(dd)
                    in_flight.pop(dd, None)
                    progress.pop(dd, None)
                    k += 1
                    if not live:
                        _item(k, total, dd, titles[dd])
                    why = f"no progress for {_dur(now - since)}: the provider never answered"
                    m.fail(dd, "extract", why)
                    print(f"FAILED {_dur(now - st_)}: {why} (the next run retries it)", flush=True)
                    failed += 1
                    threads.append(threading.Thread(target=worker, daemon=True, name=f"extract-{len(threads)}"))
                    threads[-1].start()                  # its worker is stuck: a new one keeps the pace
            # Multi-worker lines print only when a video finishes; a slow one
            # (long transcript, provider retries/backoff) would look like a hang.
            if not live and time.time() - last_news >= HEARTBEAT_S:
                last_news = time.time()
                for d, (st_, stp) in list(in_flight.items()):
                    tail = " · ".join(stp[-2:]) or "waiting for the model"
                    print(f"    ... still working on {d} ({_dur(time.time() - st_)}): {tail}",
                          flush=True)
            continue
        k += 1
        if not live:
            _item(k, total, doc_id, doc.get("title"))
            for s_ in steps:
                print(f"{s_} · ", end="")
        if isinstance(err, runner.BackendUnavailable):
            stop.set()
            from . import runstatus
            runstatus.record("network" if runstatus.classify(err) == "network" else "endpoint", str(err))
            print("STOPPED", flush=True)
            print(f"\nextract: the LLM endpoint refused further work -- {err}\n"
                  f"  Videos in flight were not marked failed. Fix the model / key / credits "
                  f"(or wait for the quota to reset) and re-run.", file=sys.stderr)
            print(f"\nextract: {n} records saved before stopping")
            return 1
        if err is not None:
            # A rejected request or an answer that failed validation even after repairs won't
            # change on the next run with the same model: park it at once (`--retry-failed`
            # after changing model or prompt). Anything else is parked after STEP_FAILURE_CAP runs.
            permanent = isinstance(err, runner.RequestRejected) or "failed validation" in str(err)
            parked = m.fail(doc_id, "extract", str(err)[:300], permanent=permanent) == "parked"
            if isinstance(err, runner.ExtractionFailed):   # keep the call log for diagnosis
                pages.atomic_write_text(EXTRACT_FAILURES / f"{doc_id}.json", json.dumps(
                    {"doc_id": doc_id, "title": doc.get("title"), "error": str(err),
                     "model": LLM_MODEL, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "calls": err.calls}, indent=2))
            print(f"FAILED {_dur(dt)}: {str(err)[:120]}"
                  + (" -- parked (`ytbrain extract --retry-failed` to try again)" if parked else ""),
                  flush=True)
            failed += 1
            printed.release()
            continue
        # A fresh record has not been verified, even if an older one was: without this,
        # re-extracting (new model/prompt) left verify 'ok' and the new record's evidence
        # was never checked. Marked BEFORE the record is written, so a kill in between
        # can only cause a harmless re-verify, never an unverified record marked verified.
        if m.stage_status(doc_id, "verify") is not None:
            m.mark(StageState(doc_id, "verify", "stale", error="re-extracted"))
        rec = json.loads(record.model_dump_json())
        pages.write_record(rec)
        m.mark(StageState(doc_id, "extract", "ok", attempts=1,
                          version=versions.stamp_of(rec.get("extraction_meta"), SCHEMA_VERSION)))
        m.succeeded(doc_id, "extract")
        (EXTRACT_FAILURES / f"{doc_id}.json").unlink(missing_ok=True)   # an old failure log
        n += 1
        per = (time.time() - started) / k                # wall-clock per video: reflects parallelism
        print(f"ok {_dur(dt)}  [{_dur(per)}/video overall, ETA {_dur(per * (total - k))}]",
              flush=True)
        printed.release()
    print(f"\nextract: {n} records, {failed} failed in {_dur(time.time() - started)}"
          + (f" (failed ones retry on the next run)" if failed else ""))
    return 0


def _verify_one(m: Manifest, doc_id: str, verify_record) -> str | None:
    """Verify one record; returns its validation status, or None if it was sent back."""
    mpath, tpath = METADATA / f"{doc_id}.json", TRANSCRIPTS / f"{doc_id}.json"
    if (problem := _unreadable_json(tpath, "utterances")):
        m.mark(StageState(doc_id, "clean", "stale", error=f"transcript {problem}"))
        print(f"transcript {problem} -- sent back to clean", flush=True)
        return None
    if (problem := _unreadable_json(mpath, "extraction_meta")):
        m.mark(StageState(doc_id, "extract", "stale", error=f"metadata {problem}"))
        print(f"metadata {problem} -- sent back to extract", flush=True)
        return None
    rec = json.loads(mpath.read_text())
    report = verify_record(rec, json.loads(tpath.read_text())["utterances"])
    pages.write_record(rec)
    pages.write_page(rec)
    status = rec["extraction_meta"]["validation_status"]
    why = "; ".join([f"{report['failed']} unmatched"] + report["issues"])
    m.mark(StageState(doc_id, "verify", "ok", attempts=1,
                      error=None if status == "pass" else f"{status}: {why}"))
    m.succeeded(doc_id, "verify")
    print(f"{status} · {report['checked'] - report['failed']}/{report['checked']} spans grounded"
          + "".join(f" · {i}" for i in report["issues"]), flush=True)
    return status


def cmd_verify(args) -> int:
    """Locate every evidence quote in the transcript; derive its timestamp."""
    from .verify import verify_record
    m = Manifest(MANIFEST_DB)
    n = flagged = 0
    if getattr(args, "retry_failed", False) and (k := m.unpark("verify")):
        print(f"verify: retrying {k} parked Document(s)", flush=True)
    todo, waiting = _ready(m, "verify", _only(m.stage_settled_ids("extract", ("ok",)), args), args.limit)
    print(f"verify: {len(todo)} to check"
          + (f", {waiting} without a record yet (not cleaned, or extract pending/failed)"
             if waiting else ""), flush=True)
    errors = 0
    for i, doc_id in enumerate(todo, 1):
        doc = dict(m.get_document(doc_id) or {})   # sqlite3.Row has no .get()
        _item(i, len(todo), doc_id, doc.get("title"))
        try:
            status = _verify_one(m, doc_id, verify_record)
        except Exception as e:                       # noqa: BLE001 -- isolate one bad talk
            _step_failed(m, "verify", doc_id, e)
            errors += 1
            continue
        if status is not None:
            flagged += status != "pass"
            n += 1
    print(f"\nverify: {n} records checked, {flagged} flagged" + (f", {errors} failed (see above)" if errors else ""))
    return 0


def cmd_refresh(args) -> int:
    """Re-apply Series and provenance from sources.yaml and yt-dlp metadata to
    documents, transcripts, records and pages. No network, no LLM; idempotent.

    Series and provenance are given fields, never generated, so renaming a
    Source or fixing how they are derived must not cost a re-extraction.
    """
    m = Manifest(MANIFEST_DB)
    by_id = {s["id"]: s for s in (_sources().get("sources") or [])}
    docs = [dict(d) for d in m.documents()]
    print(f"refresh: {len(docs)} document(s), {len(by_id)} source(s) in sources.yaml", flush=True)
    changed_docs = changed_files = orphan = 0
    for d in docs:
        src = by_id.get(d["source_id"])
        if src is None:
            orphan += 1                  # its Source was removed from sources.yaml: leave as is
            continue
        info_path = RAW / f"{d['doc_id']}.info.json"
        want = {"series": _series_for(src),
                "provenance": _provenance_for(src, fetch.info_fields(info_path)) or d["provenance"]}
        if src.get("fallback") and d["series"]:
            want["series"] = d["series"]          # a specific Series always wins [fallback guard]
        if any(d[k] != v for k, v in want.items()):
            m.upsert_document(d["doc_id"], d["source_id"], **want)
            changed_docs += 1
        for path, keys in ((TRANSCRIPTS / f"{d['doc_id']}.json", ("series",)),
                           (METADATA / f"{d['doc_id']}.json", ("series", "provenance"))):
            if not path.exists():
                continue
            try:
                rec = json.loads(path.read_text())
            except ValueError:
                continue                          # damaged: the owning Step repairs it
            if all(rec.get(k) == want[k] for k in keys):
                continue
            rec.update({k: want[k] for k in keys})
            pages.atomic_write_text(path, pages.canonical_json(rec))
            if path.parent == METADATA:
                pages.write_page(rec)
            changed_files += 1
    msg = f"refresh: {changed_docs} document(s) and {changed_files} file(s) updated"
    if orphan:
        msg += f"; {orphan} document(s) belong to Sources no longer in sources.yaml (left unchanged)"
    print(msg)
    return 0


INDEX_MISSING_EXTRA = 3        # cmd_index return code: optional extra not installed


def cmd_index(args) -> int:
    """Build the Knowledge index from verified records (local embeddings, no LLM)."""
    from .config import EMBED_MAX_SEQ, EMBED_MODEL, ITEMS_VERSION
    from .index import SPLIT_VERSION, chunked_differently
    from .knowledge.items import build_items
    m = Manifest(MANIFEST_DB)
    candidates = m.stage_settled_ids("verify", ("ok",)) & m.stage_settled_ids("extract", ("ok",))
    version = f"{EMBED_MODEL}|items{ITEMS_VERSION}"

    def input_hash(doc_id: str) -> str:
        h = hashlib.sha256()
        for p in (METADATA / f"{doc_id}.json", TRANSCRIPTS / f"{doc_id}.json"):
            h.update(p.read_bytes() if p.exists() else b"-")
        # Documents whose Passages SPLIT_VERSION changed (a unit too long for the embedding window,
        # or one that closed a short chunk) re-index once; the rest of the corpus doesn't
        t = TRANSCRIPTS / f"{doc_id}.json"
        if t.exists() and chunked_differently(json.loads(t.read_text()).get("utterances") or []):
            h.update(SPLIT_VERSION.encode())
        return h.hexdigest()[:16]

    if getattr(args, "retry_failed", False) and (k := m.unpark("index")):
        print(f"index: retrying {k} parked Document(s)", flush=True)
    order = {r["doc_id"]: i for i, r in enumerate(m.documents("1=1 ORDER BY published_at DESC"))}
    todo = sorted((d for d in candidates if m.stage_status(d, "index") != "parked"
                   and m.needs(d, "index", input_hash(d), version)),
                  key=lambda d: order.get(d, 10**9))
    stale = len(todo)
    if args.limit:
        todo = todo[:args.limit]
    later = f", {stale - len(todo)} left for a later run" if stale > len(todo) else ""
    print(f"index: {len(todo)} to index with {EMBED_MODEL}, "
          f"{len(candidates) - stale} already current{later}", flush=True)

    try:
        from .knowledge.store import KnowledgeStore
        store = KnowledgeStore()
    except RuntimeError as e:
        print(f"index: {e}", file=sys.stderr)
        return INDEX_MISSING_EXTRA
    if store.embed_model() not in (None, EMBED_MODEL):
        # a different model's vectors (and maybe dimension): start the table over
        print(f"index: embedding model changed ({store.embed_model()} -> {EMBED_MODEL}); "
              f"rebuilding the whole index", flush=True)
        store.db.drop_table(store.name)
        store._t = None
        todo = sorted(candidates, key=lambda d: order.get(d, 10**9))
    tags = _doc_tags(m, candidates)
    from . import tagging
    tagged = tagging.tagged_docs()               # the tag Step owns these Documents' Domains (ADR-0017)
    if (n_tagged := store.retag({d: v for d, v in tags.items() if d not in tagged})):
        print(f"index: re-tagged {n_tagged} Document(s) with their Domains", flush=True)
    if not todo:
        removed = store.delete_documents_except(candidates)
        if removed:
            print(f"index: removed {removed} Document(s) no longer verified")
        if removed or not store.fulltext_current():
            # also after an interrupted build: without it, keyword search silently disappears
            print("index: building full-text index ...", end=" ", flush=True)
            store.build_fulltext_index()
            print("ok", flush=True)
        return 0

    item_tags = tagging.stored_item_domains()
    print(f"index: loading {EMBED_MODEL} (the first run downloads it) ...", end=" ", flush=True)
    t0 = time.time()
    try:
        from .knowledge.embed import load_embedder
        embed = load_embedder(EMBED_MODEL, getattr(args, "device", None))
    except RuntimeError as e:
        print(f"\nindex: {e}", file=sys.stderr)
        from .knowledge.embed import MISSING_EXTRA
        return INDEX_MISSING_EXTRA if MISSING_EXTRA in str(e) else 1
    except Exception as e:                       # download / memory / device problems
        print(f"\nindex: could not load {EMBED_MODEL}: {e}", file=sys.stderr)
        return 1
    print(f"ok ({time.time() - t0:.0f}s, {getattr(embed, 'device', '?')})", flush=True)

    n = items_total = too_long = 0
    started = time.time()
    errors = 0
    for i, doc_id in enumerate(todo, 1):
        mpath, tpath = METADATA / f"{doc_id}.json", TRANSCRIPTS / f"{doc_id}.json"
        if (problem := _unreadable_json(mpath, "extraction_meta")):
            m.mark(StageState(doc_id, "extract", "stale", error=f"metadata {problem}"))
            print(f"    {doc_id}  metadata {problem} -- sent back to extract", flush=True)
            continue
        record = json.loads(mpath.read_text())
        transcript = None if _unreadable_json(tpath, "utterances") else json.loads(tpath.read_text())
        _item(i, len(todo), doc_id, record.get("title_raw"))
        try:
            rows = _index_rows(record, transcript, build_items, embed, store, EMBED_MODEL, tags, item_tags)
        except Exception as e:                       # noqa: BLE001 -- isolate one bad talk
            _step_failed(m, "index", doc_id, e)
            errors += 1
            continue
        # the embedder silently truncates past its window; count texts at risk
        # (1.6 tokens/word is a pessimistic bound for English)
        too_long += sum(1 for r in rows if len(r["indexable"].split()) * 1.6 > EMBED_MAX_SEQ)
        m.mark(StageState(doc_id, "index", "ok", input_hash=input_hash(doc_id), version=version,
                          attempts=1))
        m.succeeded(doc_id, "index")
        kinds = sum(1 for r in rows if r["kind"] == "advice")
        per = (time.time() - started) / i
        print(f"{len(rows)} items ({kinds} advice)  [{_dur(per)}/talk, ETA {_dur(per * (len(todo) - i))}]",
              flush=True)
        n += 1
        items_total += len(rows)
    removed = store.delete_documents_except(candidates)
    print("index: building full-text index ...", end=" ", flush=True)
    store.build_fulltext_index()
    print("ok", flush=True)
    print(f"\nindex: {n} Document(s), {items_total} items indexed in {_dur(time.time() - started)}"
          + (f"; {errors} failed (see above)" if errors else "")
          + (f"; removed {removed} no longer verified" if removed else "")
          + f"; {store.count()} items in the index")
    if too_long:
        print(f"note: {too_long} text(s) may exceed the {EMBED_MAX_SEQ}-token embedding window "
              f"and were truncated; raise EMBED_MAX_SEQ in config.py or shorten Passages")
    return 0


def _index_rows(record, transcript, build_items, embed, store, model: str, tags=None, item_tags=None) -> list[dict]:
    """Embed one Document's items and replace them in the index (one unit: delete + add). An item the
    tag Step already tagged keeps its Domains (`item_tags`); a new one starts with the configured ones
    until the next `ytbrain tag`."""
    domains, source_id = (tags or {}).get(record["doc_id"], (None, ""))
    rows = build_items(record, transcript, domains, source_id)
    for r in rows:
        if item_tags and r["item_id"] in item_tags:
            r["domains"] = list(item_tags[r["item_id"]])
    vectors = embed([r["indexable"] for r in rows]) if rows else []
    for r, v in zip(rows, vectors):
        r["vector"], r["embed_model"] = v, model
    store.replace_document(record["doc_id"], rows, getattr(embed, "dim", len(vectors[0]) if vectors else 0))
    return rows


def _load_search(device, rerank: bool = True):
    """(store, embed, reranker) for eval, or raise RuntimeError with a readable reason.
    The reranker (2+ GB) is loaded only when the configuration uses it."""
    from .config import EMBED_MODEL
    from .knowledge.embed import load_embedder, load_reranker
    from .knowledge.store import KnowledgeStore
    store = KnowledgeStore()
    if not store.ready:
        raise RuntimeError("knowledge index not built -- run `ytbrain index`")
    embed = load_embedder(store.embed_model() or EMBED_MODEL, device)
    return store, embed, (load_reranker(device=device) if rerank else None)


def _ts(iso: str | None) -> float | None:
    import datetime as _dt
    if not iso:
        return None
    try:
        return _dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _staleness_notes(split: str, config: str, run_mtime: float | None, index_at: str | None,
                     pack_built: str | None) -> list[str]:
    """Why a saved run (or the pack it searched) may not reflect the current knowledge: new
    Documents indexed after it was made. Judging such a run grades only what it retrieved."""
    from .eval.run import CONFIGS
    notes, idx = [], _ts(index_at)
    is_pack = CONFIGS.get(config, {}).get("backend") == "pack"
    if is_pack and idx and (_ts(pack_built) or 0) < idx:
        notes.append(f"the Knowledge pack (built {pack_built}) is older than the index ({index_at}): "
                     f"run `ytbrain pack build` first")
    if idx and run_mtime is not None and run_mtime < idx:
        notes.append(f"the saved {config} run is older than the index ({index_at}): run "
                     f"`ytbrain eval run --set {split} --config {config}` first, so the new Documents are in it")
    return notes


def _knowledge_times() -> tuple[str | None, str | None]:
    """(last index write, pack built_at) for the staleness notes."""
    from .config import PACK_DIR
    row = Manifest(MANIFEST_DB).db.execute(
        "SELECT MAX(updated_at) FROM stage_state WHERE stage='index' AND status='ok'").fetchone()
    try:
        built = json.loads((PACK_DIR / "pack.json").read_text()).get("built_at")
    except (OSError, ValueError):
        built = None
    return (row[0] if row else None), built


def _load_pack(path=None, rerank: bool = True):
    """(store, embed, reranker) over a Knowledge pack with the ONNX models it was built for."""
    from founder_coach.models import for_pack
    from founder_coach.pack import PackStore
    from .config import PACK_DIR
    store = PackStore(path or PACK_DIR, verify=True)
    embed, reranker = for_pack(store.meta, rerank=rerank)
    return store, embed, reranker


def _book_overrides() -> dict[str, dict]:
    """Per-file corrections from every pdf_books Source in sources.yaml (none when there is no
    sources.yaml), so `books inspect` reports what `sync` will register."""
    from . import sources as S
    if not SOURCES.exists():
        return {}
    try:
        return {name: fields or {} for s in S.load(SOURCES) if s["type"] == "pdf_books"
                for name, fields in (s.get("books") or {}).items()}
    except S.SourceConfigError as e:
        sys.exit(f"sources.yaml: {e}")


def _tag_inputs(cmd: str):
    """(store, rows, vectors, embed) for the tag commands, or an exit code with the reason printed."""
    from .config import EMBED_MODEL
    try:
        from .knowledge.embed import load_embedder
        from .knowledge.store import KnowledgeStore
        store = KnowledgeStore()
    except RuntimeError as e:
        print(f"{cmd}: {e}", file=sys.stderr)
        return INDEX_MISSING_EXTRA
    if not store.ready:
        print(f"{cmd}: no knowledge index yet: run `ytbrain index` first", file=sys.stderr)
        return 1
    rows, vectors = store.tag_inputs()
    try:
        embed = load_embedder(store.embed_model() or EMBED_MODEL, None)
    except Exception as e:                       # noqa: BLE001 -- download / memory / device problems
        print(f"{cmd}: could not load the embedding model: {e}", file=sys.stderr)
        return 1
    return store, rows, vectors, embed


def _tag_model() -> str:
    from .config import LLM_MODEL
    return os.environ.get("YTBRAIN_TAG_MODEL") or LLM_MODEL


def cmd_tag(args) -> int:
    """Tag Passages and items with Domains by what they say (ADR-0017): no re-extraction, no re-embedding."""
    from . import tagging as T
    from .config import EMBED_MODEL
    got = _tag_inputs("tag")
    if isinstance(got, int):
        return got
    store, rows, vectors, embed = got
    reg = _domains()
    m = Manifest(MANIFEST_DB)
    docs = sorted({r["doc_id"] for r in rows})
    hints = {d: v[0] for d, v in _doc_tags(m, docs, default=False).items()}
    model, s, db = _tag_model(), T.load_settings(), T.TagDB()
    ask = None if args.no_llm else T.llm_tiebreak(reg, model)
    print(f"tag: {len(rows)} item(s) in {len(docs)} Document(s) against {len(reg.names)} Domain(s) "
          f"(floor {s.floor}, close {s.close}, share {s.share})", flush=True)
    res = T.run(rows, vectors, reg, embed, hints, s, ask, db, model, args.max_cost, args.dry_run,
                retry_undecided=getattr(args, "retry_undecided", False))
    print(f"tag: {res.close_calls} close call(s): {res.cached} from the cache, {res.asked} asked, "
          f"{res.unresolved} kept every candidate" + (f" · ${res.cost:.3f} on {model}" if res.cost else ""))
    if res.undecided:
        print(f"tag: {res.undecided} close call(s) the model already left undecided keep every candidate (cached; "
              f"`--retry-undecided` asks again)")
    if res.unresolved:
        print(f"tag: counts below are an upper bound: {res.unresolved} unresolved close call(s) count for every "
              f"candidate Domain")
    if args.dry_run:
        if res.est_calls:
            print(f"tag: a real run would ask {model} {res.est_calls} time(s) (~${res.est_calls * T.EST_COST_PER_CALL:.2f} "
                  f"estimated; cap it with --max-cost, or --no-llm to keep every candidate)")
    per = Counter(d for t in res.docs.values() for d in t.domains)
    print("tag: Documents per Domain: " + ", ".join(f"{d} {n}" for d, n in sorted(per.items(), key=lambda x: -x[1])))
    if res.awaiting_review:
        print(f"tag: {res.awaiting_review} high-tier tag(s) await your review (they stay out of packs until "
              f"confirmed): " + ("`ytbrain domains review` after a real run (`ytbrain tag --no-apply` stores them)"
                                  if args.dry_run else "`ytbrain domains review`"))
    if res.unplaced:
        print(f"tag: {len(res.unplaced)} Document(s) matched no Domain by content (they keep their configured "
              f"Domain or the default); `ytbrain domains propose` groups such content into proposed Domains")
    if args.dry_run:
        print("tag: dry run, nothing changed")
        return 0
    why = "--no-apply" if args.no_apply else (T.gate(reg, s) if args.gated else None)
    meta = {**T.meta_for(reg, s, store.embed_model() or EMBED_MODEL, model), "applied": "0" if why else "1"}
    db.replace(res.items, res.docs, meta)
    if why:
        print(f"tag: stored for review (`ytbrain domains why|review|sample`), not applied to the index: {why}")
        return 0
    n = store.set_item_domains({i: t.domains for i, t in res.items.items()})
    print(f"tag: {n} item(s) changed Domains in the index (in place; nothing re-extracted or re-embedded)")
    if n:
        store.build_fulltext_index()
        print("tag: rebuild the pack to ship them: `ytbrain pack build` (or `ytbrain ops plugin`)")
    return 0


def _domains_tagging(args, reg) -> int:
    """`domains why | review | propose | sample | alias`."""
    import csv
    from . import domains as D
    from . import tagging as T
    if args.domains_cmd == "alias":
        D.add_alias(args.name, args.alias)
        print(f"domains: {args.alias!r} is now another name for `{args.name}`: a proposal or folder of that name "
              f"means `{args.name}`. Run `ytbrain tag` so items scored against the new alias are re-tagged.")
        return 0
    if not T.TAGS_DB.exists() and args.domains_cmd != "propose":
        print("domains: nothing tagged yet: run `ytbrain tag` first", file=sys.stderr)
        return 1
    db = T.TagDB()
    m = Manifest(MANIFEST_DB)
    titles = {r["doc_id"]: r["title"] or r["doc_id"] for r in m.documents("1=1")}
    if args.domains_cmd == "why":
        rows = [r for r in db.docs() if r["doc_id"].startswith(args.doc)]
        if not rows:
            print(f"domains why: no tagged Document starts with {args.doc!r}", file=sys.stderr)
            return 1
        if len(rows) > 1:
            print(f"domains why: {len(rows)} Documents start with {args.doc!r}; say more:")
            for r in rows[:20]:
                print(f"  {r['doc_id']}  {titles.get(r['doc_id'], '')[:60]}")
            return 1
        r = rows[0]
        print(f"{r['doc_id']}  {titles.get(r['doc_id'], '')}")
        print(f"  Domains: {', '.join(json.loads(r['domains']))} (from {r['origin']})"
              + (f" · suggested, awaiting review: {', '.join(json.loads(r['suggested']))}" if json.loads(r['suggested']) else ""))
        shares = json.loads(r["shares"])
        if shares:
            print("  share of its Passages: " + ", ".join(f"{d} {v:.0%}" for d, v in sorted(shares.items(), key=lambda x: -x[1])))
        print(f"  configured (hint): {', '.join(json.loads(r['hints'])) or 'none'}")
        for it in db.items_of(r["doc_id"])[: args.items]:
            sc = json.loads(it["scores"])
            print(f"  {it['item_id'][:40]:40} {','.join(json.loads(it['domains'])):24} {it['origin']:10} "
                  + " ".join(f"{d}={v:.2f}" for d, v in sc.items()))
        return 0
    if args.domains_cmd == "review":
        if args.accept or args.reject:
            for verb, specs in (("confirmed", args.accept or []), ("rejected", args.reject or [])):
                for spec in specs:
                    scope, _, domain = spec.rpartition(":")
                    if not scope or domain not in reg or reg.get(domain).risk_tier != "high":
                        print(f"domains review: {spec!r}: give SOURCE:DOMAIN (or doc:DOC_ID:DOMAIN) with a high-tier "
                              f"Domain", file=sys.stderr)
                        return 2
                    key = scope if scope.startswith("doc:") else f"source:{scope}"
                    (db.confirm if verb == "confirmed" else db.reject)([key], domain)
                    print(f"domains review: {verb} `{domain}` for {key}")
            print("domains review: run `ytbrain tag` to apply (cached: no model calls), then `ytbrain pack build`")
            return 0
        groups: dict[tuple[str, str], list[str]] = {}
        for r in db.docs():
            for d in json.loads(r["suggested"]):
                groups.setdefault((r["source_id"], d), []).append(r["doc_id"])
        if not groups:
            print("domains review: no high-tier tags await review")
            return 0
        for (sid, d), docs in sorted(groups.items(), key=lambda x: -len(x[1])):
            eg = "; ".join(titles.get(x, x)[:50] for x in docs[:3])
            print(f"  {d} · source {sid} · {len(docs)} Document(s), e.g. {eg}")
            print(f"      yes for all: ytbrain domains review --accept {sid}:{d}   no for all: --reject {sid}:{d}   "
                  f"(one Document: doc:<doc_id>:{d})")
        return 0
    if args.domains_cmd == "sample":
        got = _tag_inputs("domains sample")
        if isinstance(got, int):
            return got
        _store, rows, _v, _e = got
        texts = {r["item_id"]: r.get("text") or "" for r in rows}
        items, docs = T.sample_sheets(db, titles, texts, args.n, args.docs, args.seed)
        stamp = time.strftime("%Y%m%d")
        paths = []
        for name, cols, data in (("items", T.SHEET_COLUMNS, items), ("documents", T.DOC_COLUMNS, docs)):
            p = REPORTS / f"domain-labels-{name}-{stamp}.csv"
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerows(data)
            paths.append(p)
        print(f"domains sample: {len(items)} item(s) in {paths[0]}, {len(docs)} Document(s) in {paths[1]}")
        print("  Fill the `label` column with the Domain(s) each one is really about, separated by | "
              "(e.g. `gtm|startup`); leave a row empty to skip it. Then: "
              f"ytbrain eval tags --items {paths[0].name} --documents {paths[1].name}")
        return 0
    if args.domains_cmd == "propose":
        from pydantic import BaseModel

        from .eval import llm
        got = _tag_inputs("domains propose")
        if isinstance(got, int):
            return got
        _store, rows, vectors, embed = got
        origins = {r["item_id"]: r["origin"] for r in db.db.execute("SELECT item_id, origin FROM item_tags")}

        class Prop(BaseModel):
            name: str
            description: str = ""
            examples: list[str] = []
            risk_tier: str = "medium"
            relation: str = "new"
            existing: str | None = None

        def ask_json(prompt):
            a = llm.ask(prompt, Prop, _tag_model())
            return a.value.model_dump() if a.value else None
        props = T.propose(rows, vectors, reg, embed, ask_json, origins, min_size=args.min_size)
        if not props:
            print("domains propose: no group of unplaced content is big enough to suggest a Domain")
            return 0
        for p in props:
            print(f"\n  {p.name} ({p.size} items) · {p.verdict} · nearest existing: {p.nearest} ({p.nearest_score:.2f})")
            print(f"    {p.description}")
            for x in p.texts:
                print(f"    e.g. {' '.join(x.split())[:110]}")
            if p.verdict == "new":
                ex = " ".join(f'--example "{e}"' for e in p.examples)
                print(f"    add it: ytbrain domains add {p.name} --risk {p.risk_tier} --description \"{p.description}\" {ex}")
            elif p.verdict.startswith("alias of "):
                print(f"    or keep it as another name: ytbrain domains alias {p.verdict.split()[-1]} \"{p.name}\"")
        print("\n  The risk tier is your call (it decides when the coach must decline). Nothing was added.")
        return 0
    return 2


def cmd_domains(args) -> int:
    """`domains list | add | ignore`: the Library's Domains in domains.yaml (CONTEXT.md: Domain). Adding one is a
    choice with a required risk tier; a books folder only becomes a Domain this way (decision #23)."""
    from . import domains as D
    try:
        if args.domains_cmd in ("why", "review", "propose", "sample", "alias"):
            return _domains_tagging(args, D.load())
        if args.domains_cmd == "add":
            reg = D.add_domain(args.name, risk_tier=args.risk, description=args.description,
                               examples=args.example or [], freshness=args.freshness, web_policy=args.web_policy)
            name = D.folder_key(args.name)
            print(f"domains: added `{name}` ({args.risk} risk) to {D.DOMAINS_FILE.name}; Books in a `{name}/` "
                  f"folder now belong to it. Run `ytbrain tag` (tags by content, in place, no re-extraction) and "
                  f"`ytbrain pack build` so the coach sees it.")
            return 0
        if args.domains_cmd == "ignore":
            D.ignore_folder(args.folder)
            print(f"domains: `{args.folder}/` only sorts files now (ignore_folders); its Books keep their Source's "
                  f"Domains and sync no longer warns about it")
            return 0
        reg = D.load()
    except D.DomainConfigError as e:
        print(f"domains: {e}", file=sys.stderr)
        return 2
    from .books.adapter import stray_book_folders
    print(f"{D.DOMAINS_FILE.name}: {len(reg.names)} Domain(s), default `{reg.default}`")
    for name in reg.names:
        d = reg.get(name)
        fresh = "evergreen" if d.half_life_days is None else f"{d.half_life_days} days"
        print(f"  {name:16} {d.risk_tier:6} risk · {fresh:9} · web {d.web_policy:13} · {d.description[:70]}")
        if d.aliases:
            print(f"  {'':16} also: {', '.join(d.aliases)}")
    if reg.ignore_folders:
        print(f"  ignore_folders: {', '.join(reg.ignore_folders)}")
    stray = stray_book_folders()
    for folder, n in stray.items():
        print(f"  not a Domain yet: {folder}/ ({n} Book(s)): `ytbrain domains add {D.folder_key(folder)} --risk ... "
              f"--description \"...\"` or `ytbrain domains ignore {folder}`")
    return 0


def cmd_books(args) -> int:
    """`books inspect`: probe, parse (cached), resolve metadata and split PDFs into Chapters, and
    report; with --expect, check them against expected values (ADR-0014, docs/books-plan.md)."""
    import json as _json
    from .books.inspect import compare, render, report
    from .books.parse import parser_for
    from .config import BOOKS_PARSED
    paths: list[Path] = []
    for raw in args.paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            paths += sorted(q for q in p.rglob("*") if q.is_file() and q.suffix.lower() == ".pdf")
        elif p.exists():
            paths.append(p)
        else:
            print(f"books: no such file or folder: {p}", file=sys.stderr)
            return 2
    if not paths:
        print("books: no PDF files found", file=sys.stderr)
        return 2
    expected = None
    if args.expect:
        import yaml
        try:
            expected = (yaml.safe_load(Path(args.expect).expanduser().read_text()) or {}).get("books") or {}
        except (OSError, yaml.YAMLError) as e:
            print(f"books: can't read {args.expect}: {e}", file=sys.stderr)
            return 2
    overrides = _book_overrides()
    code, out, mismatches = 0, [], 0
    try:
        parser = parser_for(args.parser)                   # one parser for the run: a model loads once
        for p in paths:
            r = report(p, BOOKS_PARSED, overrides.get(p.name), parser=parser, reparse=args.reparse)
            code = code or (0 if r["ok"] else 1)
            if expected is not None:
                want = expected.get(p.name)
                r["expect"] = compare(r, want) if want is not None else ["no expectations for this file"]
                mismatches += bool(want is None or r["expect"])
            out.append(r)
            if not args.json:
                text = render(r)
                if expected is not None:
                    text += "\n" + ("  expected: all match" if not r["expect"] else
                                     "\n".join(f"  EXPECTED MISMATCH: {x}" for x in r["expect"]))
                print(text + "\n")
    except RuntimeError as e:                               # a missing extra
        print(f"books: {e}", file=sys.stderr)
        return 2
    if expected is not None:
        missing = sorted(set(expected) - {p.name for p in paths})
        for name in missing:
            print(f"EXPECTED BOOK NOT FOUND: {name}")
        mismatches += len(missing)
        print(f"books: {len(paths) - sum(bool(r.get('expect')) for r in out)}/{len(paths)} books match their expectations"
              + (f"; {len(missing)} expected book(s) missing" if missing else ""))
        code = code or (1 if mismatches else 0)
    if args.json:
        print(_json.dumps(out, indent=1, ensure_ascii=False))
    return code


def cmd_pack(args) -> int:
    """Build or inspect the Knowledge pack the coach plugin ships (ADR-0009)."""
    import json as _json
    from founder_coach import pack as P
    from .config import EMBED_MODEL, PACK_DIR, PACK_EMBED_MODEL, PACK_RERANK_MODEL
    from .pack import build_pack, describe
    from . import packs as PK
    from .config import DATA
    try:
        the_pack = PK.load(getattr(args, "for_pack", None) or PK.DEFAULT_PACK, known_domains=_domains().names,
                           check_skills=False)
    except PK.PackError as e:
        print(f"pack: {e}", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser() if args.out else \
        the_pack.pack_dir(bool(getattr(args, "include_private", False)), DATA)
    if args.pack_cmd == "info":
        manifest_path = P.resolve(out).with_name(P.MANIFEST_FILE)
        if not manifest_path.exists():
            print(f"pack: nothing at {out} -- run `ytbrain pack build`", file=sys.stderr)
            return 1
        manifest = _json.loads(manifest_path.read_text())
        print(describe(manifest))
        try:
            P.PackStore(out, verify=True).close()
            print("  checksum OK")
        except RuntimeError as e:
            print(f"pack: {e} -- rebuild with `ytbrain pack build`", file=sys.stderr)
            return 1
        return 0
    from founder_coach.models import load_embedder
    from .knowledge.store import KnowledgeStore
    from .lock import LockBusy, exclusive
    try:
        store = KnowledgeStore()
        if not store.ready:
            raise RuntimeError("knowledge index not built -- run `ytbrain index`")
        embedder = load_embedder(args.embed_model or PACK_EMBED_MODEL, device=args.device)
        print(f"pack: embedding on {embedder.device}", flush=True)
    except RuntimeError as e:
        print(f"pack: {e}", file=sys.stderr)
        return 1
    rerank = args.rerank_model or PACK_RERANK_MODEL
    try:
        # its own lock: a pack build only reads the index, so it may run beside `eval build`
        with exclusive(out / ".build.lock"):
            kinds = P.ALL_KINDS if args.with_passages else P.KINDS
            if args.with_passages:
                print("pack: including Passages (transcript excerpts): private beta only, "
                      "never a public pack (ADR-0009)", flush=True)
            if args.include_private:
                print("pack: including private items (your Books): for your own coach only; "
                      "scripts/release.py refuses this pack (ADR-0014)", flush=True)
            manifest = build_pack(store, embedder, out, rerank_model=rerank, batch=args.batch, kinds=kinds,
                                  domains=the_pack.domains, pack_id=the_pack.id,
                                  include_private=args.include_private,
                                  route={} if args.route else None,
                                  calibration=_saved_calibration(),
                                  say=lambda m: print(m, flush=True),
                                  source_info={"index_items": store.count(),
                                               "index_embed_model": store.embed_model() or EMBED_MODEL})
    except LockBusy as e:
        print(f"pack: another `pack build` is already writing to {out} ({e})", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\npack: interrupted. Embedded batches are cached; re-run `ytbrain pack build` "
              "to continue.", file=sys.stderr)
        return 130
    except RuntimeError as e:
        print(f"pack: {e}", file=sys.stderr)
        return 1
    print(describe(manifest))
    print(f"  written to {out}. Score it with `ytbrain eval run --set dev --config pack --compare full`.")
    return 0



def _talk_info() -> dict[str, dict]:
    out = {}
    for p in METADATA.glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except ValueError:
            continue
        out[p.stem] = {"title": r.get("title_canonical") or r.get("title_raw") or "",
                       "speaker": r.get("speaker") or "", "published_at": r.get("published_at") or "",
                       "url": r.get("url") or ""}
    return out


LATENCY_P95_S = 1.5        # M5/M6 exit: search p95 on a 16 GB M2, from the plugin's own runtime and pack


def _eval_latency(args) -> int:
    """Search latency the way the plugin runs it: the runtime's search on a Knowledge pack, ONNX models, one query
    at a time after a warm-up; real questions from the Tuning sets. Writes data/eval/latency.json."""
    import statistics
    from founder_coach.search import search
    from .config import EVAL_DATA, EVAL_DIR
    from .eval.files import load_split
    try:
        store, embed, reranker = _load_pack(args.pack or None, rerank=not args.no_rerank)
    except Exception as e:                       # noqa: BLE001 -- missing pack or models: say which
        print(f"eval latency: couldn't open the pack: {e}", file=sys.stderr)
        return 1
    qs = [q["text"] for q in load_split(EVAL_DIR, "dev")[0] if q.get("text")][: args.n]
    if not qs:
        print("eval latency: no Tuning questions to time", file=sys.stderr)
        return 1
    for q in qs[:3]:                             # model load and caches: the first searches aren't what a Founder waits on
        search(store, embed, q, reranker=reranker)
    times = []
    for q in qs:
        t0 = time.perf_counter()
        search(store, embed, q, reranker=reranker)
        times.append(time.perf_counter() - t0)
    times.sort()
    p50 = statistics.median(times)
    p95 = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]
    res = {"queries": len(times), "p50_s": round(p50, 3), "p95_s": round(p95, 3), "max_s": round(times[-1], 3),
           "items": store.count(), "reranker": bool(reranker), "pack": str(args.pack or "data/pack"),
           "gate_p95_s": LATENCY_P95_S, "passed": p95 <= LATENCY_P95_S,
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    from .pages import atomic_write_text
    EVAL_DATA.mkdir(parents=True, exist_ok=True)
    atomic_write_text(EVAL_DATA / "latency.json", json.dumps(res, indent=2))
    print(f"eval latency: {len(times)} searches over {store.count()} items"
          + (" with the reranker" if reranker else "") + f": p50 {p50:.2f}s, p95 {p95:.2f}s, max {times[-1]:.2f}s "
          f"(gate p95 <= {LATENCY_P95_S}s) -> {'PASS' if res['passed'] else 'FAIL'}")
    return 0 if res["passed"] else 1


def _eval_parity(args) -> int:
    """The subject-neutral prompt against the startup one on startup talks (ADR-0018, L5): shadow extractions,
    the Library untouched. Passing makes new founder content use the neutral prompt."""
    from .config import EVAL_DATA
    from .eval import parity as PA
    from .extract import runner, versions
    from .verify import verify_record
    m = Manifest(MANIFEST_DB)
    ok = sorted(m.stage_settled_ids("extract", ("ok",)) & m.stage_settled_ids("verify", ("ok",)))
    known = _doc_domains(m, ok)
    talks = []
    for d in ok:
        try:
            rec = json.loads((METADATA / f"{d}.json").read_text())
        except (OSError, ValueError):
            continue
        if (rec.get("source_kind") or "talk") != "talk" or \
                (rec.get("extraction_meta") or {}).get("prompt_variant", "startup") != "startup":
            continue
        if known.get(d) and not known[d] <= versions.FOUNDER_DOMAINS:
            continue
        talks.append(d)
    docs = PA.sample(talks, args.n)
    if not docs:
        print("eval parity: no startup talk extracted with the startup prompt to compare against", file=sys.stderr)
        return 1

    def load(d):
        return (json.loads((METADATA / f"{d}.json").read_text()), json.loads((TRANSCRIPTS / f"{d}.json").read_text()))

    def extract(tr, old):
        d = old["doc_id"]
        doc = dict(m.get_document(d) or {})
        info = RAW / f"{d}.info.json"
        meta = {"doc_id": d, "title": doc.get("title") or old.get("title_raw") or "", "series": doc.get("series"),
                "provenance": doc.get("provenance"), "published_at": doc.get("published_at"),
                "duration_s": doc.get("duration_s"), "caption_kind": doc.get("caption_kind"),
                "url": doc.get("url") or old.get("url"), "source_kind": "talk", "prompt_variant": "neutral"}
        rec = runner.extract_video(tr, meta, fetch.uploader_chapters(info if info.exists() else None))
        return json.loads(rec.model_dump_json())
    try:
        summary = PA.run(docs, load, extract, verify_record, EVAL_DATA / "parity", versions.stamp("neutral"),
                         args.max_cost)
    except (runner.BackendUnavailable, runner.RequestRejected) as e:
        print(f"eval parity: the model endpoint refused: {e}", file=sys.stderr)
        return 1
    from .pages import atomic_write_text
    atomic_write_text(versions.parity_file(), json.dumps(summary, indent=2))
    if summary["n"]:
        print(f"eval parity: {summary['n']} talk(s): verifier pass rate {summary['neutral_pass_rate']:.1%} neutral vs "
              f"{summary['startup_pass_rate']:.1%} startup; Verified items {summary['neutral_items']} vs "
              f"{summary['startup_items']} · ${summary.get('cost', 0):.3f}")
    print(f"eval parity: {'PASS: new founder content uses the neutral prompt' if summary['passed'] else 'FAIL'}"
          + (f" ({summary['why']})" if summary.get("why") else ""))
    return 0 if summary["passed"] else 1


def _eval_tags(args) -> int:
    """The tagger against your labelled sheets (`ytbrain domains sample`), or the best thresholds for them."""
    from . import tagging as T
    from .config import EVAL_DATA
    reg = _domains()
    def find(name):
        p = Path(name)
        return p if p.exists() else REPORTS / name
    items, bad = T.read_labels(find(args.items), "item_id", reg)
    docs, bad2 = (T.read_labels(find(args.documents), "doc_id", reg) if args.documents else ({}, []))
    for b in bad + bad2:
        print(f"eval tags: {b}", file=sys.stderr)
    if not items:
        print("eval tags: no labelled rows in the sheet (fill the `label` column)", file=sys.stderr)
        return 2
    if not T.TAGS_DB.exists():
        print("eval tags: nothing tagged yet: run `ytbrain tag` first", file=sys.stderr)
        return 1
    db = T.TagDB()
    if args.tune:
        got = _tag_inputs("eval tags")
        if isinstance(got, int):
            return got
        _store, rows, vectors, embed = got
        m = Manifest(MANIFEST_DB)
        hints = {d: v[0] for d, v in _doc_tags(m, sorted({r["doc_id"] for r in rows}), default=False).items()}
        ranked = T.tune(rows, vectors, reg, embed, hints, items, docs, db, _tag_model())
        print("eval tags: best settings on your labels (cached tie-breaks only):")
        for s, r in ranked[:5]:
            print(f"  floor {s.floor} close {s.close} share {s.share}: precision {r['precision']:.0%}, recall "
                  f"{r['recall']:.0%}, high-tier missed {len(r['high_missed'])}, documents "
                  f"{(r['documents'] or 0):.0%}{' PASS' if r['passed'] else ''}")
        if args.save:
            T.save_settings(ranked[0][0])
            print(f"eval tags: saved to {T.SETTINGS_FILE}; run `ytbrain tag` to apply")
        return 0
    r = T.score_labels(items, docs, *T.current(db), reg)
    g = T.GATE
    print(f"eval tags: {r['items']} item(s): precision {r['precision']:.0%} (gate {g['precision']:.0%}), "
          f"recall {r['recall']:.0%} (gate {g['recall']:.0%}), high-tier labels missed {len(r['high_missed'])} (gate 0)")
    for x in r["high_missed"][:10]:
        print(f"  missed: {x}")
    if r["documents"] is not None:
        print(f"eval tags: {r['documents_n']} Document(s): {r['documents']:.0%} have every labelled Domain and at most "
              f"one extra (gate {g['documents']:.0%})")
    if r["missing"]:
        print(f"eval tags: {len(r['missing'])} labelled id(s) are no longer tagged (re-indexed or dropped); skipped")
    meta = {k: db.meta(k) for k in ("tagger_version", "domain_list", "settings", "tiebreak_model")}
    EVAL_DATA.mkdir(parents=True, exist_ok=True)
    from .pages import atomic_write_text
    atomic_write_text(EVAL_DATA / "tags.json", json.dumps({**r, **meta, "items_sheet": str(args.items),
                                                            "documents_sheet": str(args.documents or "")}, indent=2))
    print(f"eval tags: {'PASS' if r['passed'] else 'FAIL'}" + ("" if r["passed"] else
          " (`ytbrain eval tags --tune` finds better thresholds on these labels)"))
    return 0 if r["passed"] else 1


def cmd_eval(args) -> int:
    """Build, run or inspect the eval benchmark (docs/eval-spec.md)."""
    import datetime as dt
    from .config import EVAL_DATA, EVAL_DIR, EVAL_PRIVATE
    from .eval.splits import TUNING_SPLITS
    from .eval.db import EvalDB
    if args.eval_cmd == "tags":
        return _eval_tags(args)
    if args.eval_cmd == "parity":
        return _eval_parity(args)
    if args.eval_cmd == "latency":
        return _eval_latency(args)
    if args.eval_cmd == "choice":
        import shutil as _sh
        from .eval import coach
        plugins = [Path(x).resolve() for x in (args.plugin or [])]     # the host runs from another folder
        if len(plugins) < 2 or not all((x / ".claude-plugin" / "plugin.json").exists() for x in plugins):
            print("eval choice: pass two or more assembled plugins, e.g. --plugin dist/plugin-private "
                  "--plugin dist/coding-coach-private", file=sys.stderr)
            return 1
        if not _sh.which("claude"):
            print("eval choice: the `claude` CLI isn't on PATH", file=sys.stderr)
            return 1
        _, code = coach.run_choice(plugins, limit=args.limit, workers=args.workers,
                                   model=None if args.model == "default" else (args.model or coach.DEFAULT_MODEL))
        return code
    if args.eval_cmd == "coach":
        import shutil as _sh
        from .eval import build, coach
        plugin = (Path(args.plugin) if args.plugin else ROOT / "dist" / "plugin").resolve()   # the host runs from another folder
        if not (plugin / ".claude-plugin" / "plugin.json").exists() or not (plugin / "pack").exists():
            print(f"eval coach: no assembled plugin at {plugin}; run "
                  f"`python scripts/assemble_plugin.py --pack data/pack --check` first", file=sys.stderr)
            return 1
        if not _sh.which("claude"):
            print("eval coach: the `claude` CLI isn't on PATH (it runs the cases as a Founder would)", file=sys.stderr)
            return 1
        target = coach.use_plugin(plugin)
        if target["pack"] != "founder":
            print(f"eval coach: {target['id']} (the {target['pack']} Pack)")
        env = build.Env(db=EvalDB(EVAL_DATA / "eval.db"))
        err = build.preflight(env, generator=False)     # the coach eval uses only the judges
        if err:
            print(f"eval coach: {err}", file=sys.stderr)
            return 1
        host = None
        if args.host == "openrouter":
            try:
                host = coach.openrouter_host(args.model)
            except ValueError as e:
                print(f"eval coach: {e}", file=sys.stderr)
                return 1
            print(f"eval coach: host Claude Code through OpenRouter on {args.model or coach.DEFAULT_OPENROUTER_MODEL} "
                  f"(paid per token, counted in --max-cost), at most {args.max_turns} turns per call")
            model = None
        else:
            model = coach.DEFAULT_MODEL if args.model is None else None if args.model == "default" else args.model
            print(f"eval coach: host model {model or 'your Claude Code default'} on your Claude plan, at most "
                  f"{args.max_turns} turns per call"
                  + ("" if model != "haiku" else " (sign a release off with --model sonnet)"))
        _, code = coach.run_gates(env, args.gate or ["g2", "g4", "g5", "g6", "g7", "g8"], plugin,
                                  limit=args.limit or None, max_cost=args.max_cost, model=model,
                                  max_turns=args.max_turns, host=host, workers=args.workers)
        return code
    if args.eval_cmd == "status":
        for label, path in (("benchmark", EVAL_DATA / "eval.db"), ("smoke", EVAL_DATA / "smoke" / "eval.db")):
            if label == "smoke" and not path.exists():
                continue
            db = EvalDB(path)
            for split in (*TUNING_SPLITS, "test"):
                counts = defaultdict(int)
                for r in db.questions(split):
                    counts[r["status"]] += 1
                t = db.target(split)
                goal = f" · target {t['target']} ({len(t['docs'])} Documents)" if t else ""
                print(f"{label} {split}: {dict(counts) or 'nothing yet'}{goal} · spent ${db.spent(split):.3f}")
        return 0
    from .eval.run import CONFIGS
    if args.eval_cmd == "rescore":
        from .eval.run import baseline_run, render, rescore
        try:
            if args.refresh_baseline:
                src = baseline_run(EVAL_DATA / "runs", args.set, args.config)
                if src is None:
                    print(f"eval: no saved {args.set} baseline {args.config} (or its run file) to refresh",
                          file=sys.stderr)
                    return 1
                result, code = rescore(args.set, args.config, run=src, name=args.config, save_baseline=True)
                print(f"eval rescore: baseline {args.config} re-scored from {src.name} against the current labels")
                return 0
            result, code = rescore(args.set, args.config, baseline=args.compare,
                                   save_baseline=args.save_baseline, run=args.run and Path(args.run),
                                   name=args.as_name)
        except RuntimeError as e:
            print(f"eval: {e}", file=sys.stderr)
            return 1
        print(render(result))
        return _eval_exit(code)
    if args.eval_cmd == "judge":
        from .eval import build
        from .eval.files import load_set
        from .eval.run import latest_run
        paths = {}
        for c in args.config:
            p = latest_run(EVAL_DATA / "runs", args.set, c)
            if p is None:
                print(f"eval: no saved {args.set} run for {c} -- run `ytbrain eval run --set {args.set} "
                      f"--config {c}` first", file=sys.stderr)
                return 1
            paths[c] = p
        index_at, built = _knowledge_times()
        stale = [n for c, p in paths.items()
                 for n in _staleness_notes(args.set, c, p.stat().st_mtime, index_at, built)]
        for n in dict.fromkeys(stale):
            print(f"eval judge: note: {n}", file=sys.stderr)
        existing = load_set(EVAL_DIR, args.set)[0]
        freeze = (existing[0].get("valid_as_of") if existing else None) or dt.date.today().isoformat()
        env = build.Env(workers=args.workers or build.EVAL_WORKERS, manifest=Manifest(MANIFEST_DB),
                        db=EvalDB(EVAL_DATA / "eval.db"), root=EVAL_DIR, visibility=_visibility())
        err = build.preflight(env)
        if err:
            print(f"eval: {err}", file=sys.stderr)
            return 1
        return build.judge_runs(env, args.set, paths, depth=args.depth, max_cost=args.max_cost,
                                talk_info=_talk_info(), freeze_date=freeze)
    try:
        if args.eval_cmd in ("gap", "calibrate"):
            store, embed, reranker = _load_pack(args.pack, rerank=False)     # coverage is the pack's behaviour
        elif args.eval_cmd == "route":
            store, embed, reranker = (_load_pack(args.pack, rerank=False) if args.pack
                                      else _load_search(args.device, rerank=False))
        elif args.eval_cmd == "run" and CONFIGS[args.config].get("backend") == "pack":
            if not args.pack:
                index_at, built = _knowledge_times()
                for n in _staleness_notes(args.set, args.config, None, index_at, built):
                    print(f"eval run: note: {n}", file=sys.stderr)
            store, embed, reranker = _load_pack(args.pack, rerank=CONFIGS[args.config]["rerank"])
        else:
            store, embed, reranker = _load_search(
                args.device, rerank=args.eval_cmd != "run" or CONFIGS[args.config]["rerank"])
    except RuntimeError as e:
        print(f"eval: {e}", file=sys.stderr)
        return 1
    if args.eval_cmd in ("gap", "calibrate"):
        return _eval_coverage(args, store, embed)
    if args.eval_cmd == "route":
        from .eval import route as R
        from .eval.files import load_set
        queries, _ = load_set(EVAL_DIR, args.set, EVAL_PRIVATE)
        single = R.questions(queries, R.doc_domains(store))
        if len({d for it in single for d in it.expected}) < 2:
            print("eval route: the questions' Documents are all in one Domain; there is nothing to route yet "
                  "(add Sources or books in a second Domain, then `ytbrain index`)", file=sys.stderr)
            return 1
        composed = R.compose(single, args.composed)
        print(f"eval route: {len(single)} questions, {len(composed)} composed prompts", flush=True)
        say = lambda m: print(m, flush=True)
        rep = R.report(single, R.collect(store, embed, single, say=say), composed,
                       R.collect(store, embed, composed, say=say))
        print(R.render(rep))
        return 0
    smoke = EVAL_DATA / "smoke"            # --limit / --smoke: a scratch benchmark, never released
    if args.eval_cmd == "run":
        from .eval.run import evaluate, render
        try:
            result, code = evaluate(store, embed, reranker, args.set, args.config,
                                    root=smoke / "eval" if args.smoke else EVAL_DIR,
                                    out_dir=(smoke if args.smoke else EVAL_DATA) / "runs",
                                    baseline=args.compare, save_baseline=args.save_baseline,
                                    label=args.label, private=smoke / "private" if args.smoke else EVAL_PRIVATE,
                                    only_docs=_pack_docs_arg(args))
        except RuntimeError as e:
            print(f"eval: {e}", file=sys.stderr)
            return 1
        print(render(result))
        return _eval_exit(code)
    from .eval import build
    if args.set == "test":
        print("eval build --set test: the Holdout set arrives in M2c (collectors not built yet)",
              file=sys.stderr)
        return 1
    env = build.Env(store=store, embed=embed, reranker=reranker, workers=args.workers or build.EVAL_WORKERS,
                    manifest=Manifest(MANIFEST_DB),
                    db=EvalDB((smoke if args.limit else EVAL_DATA) / "eval.db"),
                    root=smoke / "eval" if args.limit else EVAL_DIR, visibility=_visibility(),
                    private=smoke / "private" if args.limit else EVAL_PRIVATE)
    if args.limit:
        print(f"eval: smoke build of {args.limit} questions in {smoke} (not the released benchmark)")
    err = build.preflight(env)
    if err:
        print(f"eval: {err}", file=sys.stderr)
        return 1
    # the set's "valid as of" date is fixed at its first release; rebuilding later keeps it
    from .eval.files import load_split
    released = [] if args.limit else load_split(EVAL_DIR, "dev")[0]
    freeze = (released[0].get("valid_as_of") if released else None) or dt.date.today().isoformat()
    if args.set is None and not args.limit:
        return build.build_all(env, max_cost=args.max_cost, talk_info=_talk_info(), freeze_date=freeze,
                               top_up=args.top_up)
    return build.build_dev(env, max_cost=args.max_cost, limit=args.limit or None,
                           talk_info=_talk_info(), freeze_date=freeze, split=args.set or "dev",
                           top_up=args.top_up)


def _eval_exit(code: int) -> int:
    if code == 1:
        print("\neval: regression gate FAILED", file=sys.stderr)
    elif code == 3:
        print("\neval: comparison INCONCLUSIVE (see the verdict above); exit code 3", file=sys.stderr)
    return code


def cmd_search(args) -> int:
    """Search the Knowledge index (hybrid + rerank) -- for debugging and eval."""
    from .config import EMBED_MODEL
    from .knowledge.search import search
    if args.pack is not None:
        try:
            store, embed, reranker = _load_pack(args.pack or None, rerank=not args.no_rerank)
        except RuntimeError as e:
            print(f"search: {e}", file=sys.stderr)
            return 1
    else:
        try:
            from .knowledge.embed import load_embedder, load_reranker
            from .knowledge.store import KnowledgeStore
            store = KnowledgeStore()
        except RuntimeError as e:
            print(f"search: {e}", file=sys.stderr)
            return 1
        if not store.ready:
            print("search: knowledge index not built -- run `ytbrain index`", file=sys.stderr)
            return 1
        model = store.embed_model() or EMBED_MODEL
        if model != EMBED_MODEL:
            print(f"note: index was built with {model}; searching with it. Run `ytbrain index` "
                  f"to rebuild with {EMBED_MODEL}.", file=sys.stderr)
        try:
            embed = load_embedder(model, args.device)
            reranker = None if args.no_rerank else load_reranker(device=args.device)
        except RuntimeError as e:
            print(f"search: {e}", file=sys.stderr)
            return 1
    results = search(store, embed, args.query, stage=args.stage, kinds=args.kind or None,
                     topics=args.topic or None, require_stage=args.require_stage,
                     top_k=args.top_k, reranker=reranker)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0
    for n, r in enumerate(results, 1):
        who = f" — {r['speaker']}" if r.get("speaker") else ""
        year = (r.get("published_at") or "")[:4]
        print(f"{n:2}. [{r['kind']}] {r['score']:.2f}  {r['title']}{who} ({year})")
        print(f"    {r['text'][:300]}")
        if r.get("evidence"):
            print(f"    “{r['evidence'][:200]}”")
        print(f"    {r['deep_link']}")
    if not results:
        print("no results")
    return 0


def cmd_pages(args) -> int:
    """Regenerate the markdown projection from canonical JSON."""
    m = Manifest(MANIFEST_DB)
    verified = m.stage_settled_ids("verify", ("ok",))
    files = sorted(METADATA.glob("*.json"))
    # Only Verified knowledge is shown: a record that verify hasn't checked yet (fresh from
    # extract) gets no page until it has been; verify writes it then.
    files, waiting = [f for f in files if f.stem in verified], [f for f in files if f.stem not in verified]
    print(f"pages: regenerating {len(files)} markdown file(s)"
          + (f"; {len(waiting)} record(s) wait for `ytbrain verify`" if waiting else ""), flush=True)
    bad = 0
    for i, p in enumerate(files, 1):
        try:
            pages.write_page(json.loads(p.read_text()))
        except Exception as e:                       # noqa: BLE001 -- one damaged record, not all pages
            bad += 1
            print(f"    {p.stem}: FAILED ({type(e).__name__}: {str(e)[:100]}) -- re-run verify for it",
                  flush=True)
        if i % 50 == 0 or i == len(files):      # fast step: a line per 50, not per file
            print(f"    {i}/{len(files)}", flush=True)
    print(f"pages: {len(files) - bad} markdown files regenerated" + (f", {bad} failed" if bad else ""))
    return 0


def cmd_run(args) -> int:
    """The scheduled job: the whole pipeline, idempotent, one run at a time."""
    m = Manifest(MANIFEST_DB)
    m.close_abandoned_runs()                    # we hold the run lock: any 'running' row is dead
    run_id = m.start_run("run")
    stages = (cmd_sync, cmd_clean, cmd_extract, cmd_verify)
    try:
        t_run = time.time()
        for si, fn in enumerate(stages, 1):
            name = fn.__name__.removeprefix("cmd_")
            print(f"\n=== [{si}/{len(stages) + 1}] {name} · {_dur(time.time() - t_run)} elapsed ===", flush=True)
            rc = fn(args)
            if rc != 0 and fn is cmd_sync:
                m.end_run(run_id, "failed", error="sync failed for all sources")
                return rc
            if rc != 0 and fn is cmd_extract:
                # endpoint refused (key/model/credits/quota): still verify what
                # was extracted, but the scheduled run must not look healthy
                cmd_verify(args)
                m.end_run(run_id, "failed", error="extract stopped: LLM endpoint unreachable or refused (key/model/credits/quota)")
                return rc
        # index last and isolated: everything above is already checkpointed, and
        # a machine without the optional extra still gets a healthy run
        print(f"\n=== [{len(stages) + 1}/{len(stages) + 1}] index · {_dur(time.time() - t_run)} elapsed ===",
              flush=True)
        rc = cmd_index(args)
        if rc == INDEX_MISSING_EXTRA:
            print("run: index skipped (optional extra not installed)")
        elif rc != 0:
            m.end_run(run_id, "failed", error="index failed; sync..verify results are saved")
            return rc
        m.end_run(run_id, "ok", summary=json.dumps(m.stats()))
        print(f"\nrun: finished in {_dur(time.time() - t_run)}", flush=True)
        return 0
    except KeyboardInterrupt:                   # Ctrl+C / SIGTERM: resumable, but not "running" forever
        m.end_run(run_id, "interrupted", error="stopped by the user or the system; re-run to resume")
        raise
    except Exception as e:
        m.end_run(run_id, "failed", error=str(e)[:500])
        raise


# --------------------------------------------------------------------------
# Acceptance (decision Q10)
# --------------------------------------------------------------------------

def cmd_report(args) -> int:
    """Automated acceptance numbers (record rate, evidence pass rate) plus recent runs."""
    m = Manifest(MANIFEST_DB)
    every = m.documents()
    docs = [d for d in every if (d.get("doc_type") or "youtube") == "youtube"]      # talks
    articles = {d["doc_id"] for d in every if d.get("doc_type") == "web"}
    captioned = [d for d in docs if d["caption_kind"] in ("human", "auto")]
    # A metadata file on disk is not necessarily current: re-extraction that
    # was interrupted or failed leaves the previous model's record in place.
    current = m.stage_settled_ids("extract", ("ok",))
    all_files = list(METADATA.glob("*.json"))
    records = [p for p in all_files if p.stem in current]
    stale_files = len(all_files) - len(records)
    talk_ids = {d["doc_id"] for d in docs}
    record_rate = sum(p.stem in talk_ids for p in records) / len(captioned) if captioned else 0.0

    checked = failed = 0
    by_status: dict[str, int] = defaultdict(int)
    for p in records:
        rec = json.loads(p.read_text())
        ver = (rec.get("extraction_meta") or {}).get("verification") or {}
        checked += ver.get("checked", 0)
        failed += ver.get("failed", 0)
        by_status[(rec.get("extraction_meta") or {}).get("validation_status", "?")] += 1
    span_rate = (checked - failed) / checked if checked else 0.0

    print("ACCEPTANCE\n")
    print(f"  videos known              {len(docs)}")
    print(f"  with captions             {len(captioned)}")
    print(f"  no captions fetched       {len(docs) - len(captioned)}")
    fetch_rows = m.db.execute("SELECT status, error FROM stage_state WHERE stage='fetch' "
                              "AND status IN ('failed', 'skipped')").fetchall()
    gaps: dict[str, int] = defaultdict(int)
    for r in fetch_rows:
        err = r["error"] or ""
        why = fetch.permanent_failure(err)
        gaps["unavailable (" + why.lower() + ")" if why else
             "no English captions" if r["status"] == "skipped" else
             "rate-limited, retried by the next sync" if "429" in err else
             "fetch failed, retried by the next sync"] += 1
    fetched = {r[0] for r in m.db.execute("SELECT doc_id FROM stage_state WHERE stage='fetch'")}
    unfetched = sum(d["doc_id"] not in fetched for d in docs)
    if unfetched:
        gaps["not fetched yet (next sync)"] += unfetched
    for why, k in sorted(gaps.items(), key=lambda x: -x[1]):
        print(f"    {k:5}  {why}")
    if articles:
        cleaned = m.stage_settled_ids("clean", ("ok",))
        kinds: dict[str, int] = defaultdict(int)
        for r in m.db.execute("SELECT doc_id, error FROM stage_state WHERE stage='clean' AND status='skipped'"):
            if r["doc_id"] in articles:
                e = r["error"] or ""
                kinds["video pages (their talks come from YouTube)" if "video page" in e else
                      "index/listing pages" if "index page" in e or "listing" in e else
                      "too short" if "too short" in e else "other"] += 1
        waiting = len(articles) - len(articles & cleaned) - sum(kinds.values())
        print(f"  web pages saved           {len(articles)}")
        print(f"    {len(articles & cleaned):5}  articles cleaned ({sum(p.stem in articles for p in records)} with records)")
        for why, k in sorted(kinds.items(), key=lambda x: -x[1]):
            print(f"    {k:5}  skipped: {why}")
        if waiting > 0:
            print(f"    {waiting:5}  not cleaned yet (next clean)")
    print(f"  metadata records          {len(records)}"
          + (f"  (+{stale_files} outdated on disk, excluded -- re-run extract)" if stale_files else ""))
    _gate("record rate", record_rate, ACCEPT_MIN_RECORD_RATE)
    _gate("evidence span pass rate", span_rate, ACCEPT_MIN_SPAN_PASS_RATE)
    print(f"\n  validation status         {dict(sorted(by_status.items()))}")
    issues = [(r["doc_id"], r["error"]) for r in m.db.execute(
        "SELECT doc_id, error FROM stage_state WHERE stage='verify' AND status='ok' "
        "AND error IS NOT NULL ORDER BY error")]
    for d, e in issues[:30]:
        print(f"    {d}  {e}")
    if len(issues) > 30:
        print(f"    ... and {len(issues) - 30} more")
    failed_x = m.db.execute("SELECT doc_id, error FROM stage_state WHERE stage='extract' "
                            "AND status='failed'").fetchall()
    if failed_x:
        print(f"\n  extract failures          {len(failed_x)}")
        for r in failed_x:
            log = EXTRACT_FAILURES / f"{r['doc_id']}.json"
            print(f"    {r['doc_id']}  {(r['error'] or '')[:90]}"
                  + (f"\n      call log: {log.relative_to(ROOT)}" if log.exists() else ""))
    if issues or failed_x:
        print("\n  Redo these: ytbrain invalidate extract --only-flagged && ytbrain extract "
              "&& ytbrain verify && ytbrain index")
    print("\n  Remaining manual gate: `ytbrain sample --n 10`, read them against the\n"
          "  videos, and confirm zero fabricated advice.\n")

    print("LAST RUNS\n")
    for r in m.last_runs(3):
        print(f"  {r['started_at']}  {r['command']:8} {r['status']:8} {r['error'] or ''}")
    return 0


def _gate(label: str, value: float, threshold: float) -> None:
    mark = "PASS" if value >= threshold else "FAIL"
    print(f"  {label:25} {value:6.1%}  (>= {threshold:.0%})  {mark}")


def cmd_sample(args) -> int:
    """Stratified review sheet for the human read.

    Stratified, not uniform: quality dies on auto-captioned videos, and if the
    corpus is mostly human-captioned a uniform draw of 10 barely touches the
    failure mode that matters. Samples across caption_kind and series, with a
    fixed seed so the check is reproducible across prompt versions.
    """
    m = Manifest(MANIFEST_DB)
    strata: dict[tuple, list[str]] = defaultdict(list)
    for p in METADATA.glob("*.json"):
        if getattr(args, "doc", None) and not p.stem.startswith(tuple(args.doc)):
            continue
        rec = json.loads(p.read_text())
        # every source kind is its own stratum, then caption kind and series within it
        strata[(source_kinds.of_record(rec).name, rec.get("caption_kind"), rec.get("series"))].append(rec["doc_id"])
    if not strata:
        print("no records yet", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    picks: list[str] = []
    keys = sorted(strata, key=lambda k: tuple(str(x) for x in k))
    while len(picks) < args.n and any(strata[k] for k in keys):
        for k in keys:                       # round-robin: every stratum represented
            if strata[k] and len(picks) < args.n:
                picks.append(strata[k].pop(rng.randrange(len(strata[k]))))

    out = [f"# Acceptance sample (n={len(picks)}, seed={args.seed})", "",
           "For each record: check it against its source (as its heading says) and answer —",
           "is every takeaway actually supported, and is anything here invented?", "",
           "| verdict | notes |", "|---|---|", "| | |", ""]
    for doc_id in picks:
        rec = json.loads((METADATA / f"{doc_id}.json").read_text())
        kind = source_kinds.of_record(rec)
        out += [f"## {rec.get('title_canonical') or rec.get('title_raw')} (`{doc_id}`)", "",
                f"- {kind.name}: {kind.review_hint}",
                f"- caption_kind: **{rec.get('caption_kind')}** · series: {rec.get('series')} "
                f"· status: {(rec.get('extraction_meta') or {}).get('validation_status')}",
                f"- {kind.link_name}: {rec.get('url') or '(no link: your own copy)'}", "",
                "**Verdict:** _(ok / minor / fabricated)_", "",
                # reviewer sees withheld claims too, flagged -- judging them is the point
                pages.render_markdown(rec, show_unverified=True).split("---", 2)[-1].strip(),
                "", "---", ""]

    path = REPORTS / f"sample-seed{args.seed}-n{len(picks)}.md"
    pages.atomic_write_text(path, "\n".join(out))
    print(f"wrote {path}")
    print("strata covered:", {"/".join(str(x) for x in k): len(v) for k, v in sorted(strata.items(), key=str)})
    return 0


def cmd_ops(args) -> int:
    """The whole loop in one resumable command: ingest, eval when the index changed, the plugin when
    it passed (docs/ops.md)."""
    from . import ops
    return ops.main(ops.Options(plan=args.plan, max_cost=args.max_cost if args.max_cost is not None
                                else ops.EVAL_MAX_COST, max_extract=args.max_extract,
                                sync=not args.no_sync, coach=args.coach and not args.skip_coach, restart=args.restart,
                                force=args.force, dry_run=args.dry_run, notify=not args.no_notify,
                                version=args.plugin_version))


def cmd_status(args) -> int:
    """Per-stage counts from the manifest, and the extraction versions records were made with."""
    m = Manifest(MANIFEST_DB)
    print(m.export())
    from .extract import versions
    by: dict[str, int] = {}
    for raw, n in m.versions("extract").items():
        variant, version = versions.parse_stamp(raw)
        key = f"{variant}@{version}" if version else "(no version recorded)"
        by[key] = by.get(key, 0) + n
    if by:
        print("extract versions (ADR-0018):")
        for key, n in sorted(by.items()):
            state = ("latest" if versions.is_latest(key) else
                     "valid, behind the latest (`ytbrain upgrade --dry-run` to see the cost)" if versions.is_current(key)
                     else "replaced by a breaking release: the next `ytbrain extract` redoes them")
            print(f"  {key}: {n} record(s) · {state}")
    enriched = m.versions("enrich")
    if enriched:
        print("enrich (ADR-0018): " + ", ".join(f"{v}: {n}" for v, n in enriched.items()))
    return 0


def _doc_domains(m, doc_ids, declared: bool = False) -> dict[str, set[str]]:
    """What is known of each Document's Domains before (re-)extracting it: its configured hints and its last
    tags (applied or stored for review); enrich takes both. declared=True: only what you declared (a Source's
    Domains, a Book's folder), which is how the prompt variant is chosen before the neutral prompt's parity
    check: content tags alone never switch a Document's prompt, so no talk or essay is re-extracted (L1)."""
    from . import tagging
    out = {d: set(v[0]) for d, v in _doc_tags(m, doc_ids, default=False).items()}
    if declared:
        return out
    if tagging.TAGS_DB.exists():
        db = tagging.TagDB()
        try:
            for r in db.docs():
                if r["doc_id"] in out:
                    out[r["doc_id"]] |= set(json.loads(r["domains"])) | set(json.loads(r["suggested"]))
        finally:
            db.close()
    return out


def cmd_enrich(args) -> int:
    """Add Rules (and Facts to records without any) for investment, coding and system-design Documents (ADR-0018)."""
    from . import enrich as E
    from .extract import runner
    m = Manifest(MANIFEST_DB)
    ok = sorted(_only(m.stage_settled_ids("extract", ("ok",)) & m.stage_settled_ids("verify", ("ok",)), args))
    known = _doc_domains(m, ok)
    todo = []
    for d in ok:
        if not E.wants(known.get(d)):
            continue
        try:
            rec = json.loads((METADATA / f"{d}.json").read_text())
        except (OSError, ValueError):
            continue
        if m.stage_status(d, "enrich") != "parked" and m.needs(d, "enrich", E.record_identity(rec), E.STAMP):
            todo.append((d, rec))
    if args.limit:
        todo = todo[:args.limit]
    # estimate: a record's own extraction cost (enrich reads the same text), else its size at the median rate
    sizes, own = {}, {}
    for d, rec in todo:
        try:
            tr = json.loads((TRANSCRIPTS / f"{d}.json").read_text())
            sizes[d] = sum(len(u.get("text", "")) for u in tr.get("utterances") or [])
        except (OSError, ValueError):
            sizes[d] = 0
        costs = [c.get("cost") for c in (rec.get("extraction_meta") or {}).get("calls") or []
                 if isinstance(c.get("cost"), (int, float))]
        own[d] = sum(costs) if costs else None
    rates = sorted(own[d] / sizes[d] for d, _ in todo if own[d] is not None and sizes[d])
    rate = rates[len(rates) // 2] if rates else None
    est = sum(own[d] if own[d] is not None else (sizes[d] * rate if rate is not None else 0) for d, _ in todo)
    print(f"enrich: {len(todo)} Document(s) in {', '.join(sorted(E.ENRICH_DOMAINS))} to enrich ({E.STAMP}); "
          f"~${est:.2f} estimated from their extraction" + ("" if rate is not None or not todo else " (no past cost)"))
    if args.dry_run or not todo:
        if args.dry_run:
            print("enrich: dry run, nothing changed")
        return 0
    model = os.environ.get("YTBRAIN_ENRICH_MODEL") or _tag_model()
    ask = E.llm_ask(model)
    from .config import LLM_BACKEND, LLM_BASE_URL, LLM_MAX_RPM, LLM_WORKERS
    workers = max(1, int(getattr(args, "workers", 0) or LLM_WORKERS))
    endpoint = os.environ.get("YTBRAIN_LLM_BASE_URL", LLM_BASE_URL)
    local = LLM_BACKEND == "ollama" or runner.is_local_endpoint(endpoint)
    pacing = "no rate pacing (local server)" if local else f"<= {LLM_MAX_RPM:g} requests/min shared"
    print(f"enrich: {len(todo)} to enrich with {model}, {workers} worker(s), {pacing}"
          + (f", spend cap ${args.max_cost:g}" if args.max_cost is not None else ""), flush=True)
    if workers > 1 and local:
        print(f"  note: a local server only runs these in parallel if it allows it "
              f"(LM Studio: Max Concurrent Predictions >= {workers}); otherwise they just queue.", flush=True)

    # Same shape as `extract`: workers only make model calls; every manifest and file write stays on this
    # thread, in completion order, so SQLite has one writer. Ctrl+C: finished Documents are saved and the ones in
    # flight were never marked, so the next run does them. The spend cap stops new calls the moment the calls made
    # add up to it (counted when a call returns, not when this thread gets to it); those in flight finish.
    total = len(todo)
    live = workers == 1              # 1 worker: print each Document before its call, as extract does
    titles = {d: str(rec.get("title_canonical") or rec.get("title") or "") for d, rec in todo}
    work: queue.Queue = queue.Queue()
    done: queue.Queue = queue.Queue()
    stop = threading.Event()
    printed = threading.Semaphore(0)
    in_flight: dict[str, float] = {}
    abandoned: set[str] = set()      # calls given up on (a provider that trickles bytes never trips a timeout)
    paid, paid_lock = [0.0], threading.Lock()
    over_cap = lambda: args.max_cost is not None and paid[0] >= args.max_cost
    for i, (d, rec) in enumerate(todo, 1):
        work.put((i, d, rec))

    def worker() -> None:
        while not (stop.is_set() or over_cap()):
            try:
                i, d, rec = work.get_nowait()
            except queue.Empty:
                return
            if live:
                _item(i, total, d, titles[d])
            t0 = time.time()
            in_flight[d] = t0
            answer, cost, err = None, 0.0, None
            try:
                tr = json.loads((TRANSCRIPTS / f"{d}.json").read_text())
                prompt, cls = E.build_prompt(rec, tr)
                answer, cost = ask(prompt, cls)
            except Exception as e:                       # noqa: BLE001 -- one Document, not the run
                err = e
            in_flight.pop(d, None)
            with paid_lock:
                paid[0] += cost or 0.0
            done.put((d, rec, answer, cost or 0.0, err, time.time() - t0))
            if live:
                printed.acquire()

    # daemon threads: Ctrl+C exits at once instead of waiting on in-flight calls
    threads = [threading.Thread(target=worker, daemon=True, name=f"enrich-{k}") for k in range(min(workers, total))]
    started = time.time()
    for t in threads:
        t.start()
    spent, n, failed, k = 0.0, 0, 0, 0
    last_news = time.time()
    while k < total:
        try:                                             # short waits keep Ctrl+C responsive
            d, rec, answer, cost, err, dt = done.get(timeout=0.5)
            last_news = time.time()
            if d in abandoned:
                continue                                 # a late answer to a call already given up on
        except queue.Empty:
            if not any(t.is_alive() for t in threads) and done.empty():
                break                                    # the spend cap or a refusal stopped the workers
            now = time.time()
            for dd, st_ in list(in_flight.items()):      # one hung call must not hold the whole run
                if now - st_ > ENRICH_CALL_DEADLINE_S and dd not in abandoned:
                    abandoned.add(dd)
                    in_flight.pop(dd, None)
                    k += 1
                    if not live:
                        _item(k, total, dd, titles[dd])
                    _step_failed(m, "enrich", dd, TimeoutError(f"no answer after {_dur(now - st_)}"))
                    failed += 1
                    threads.append(threading.Thread(target=worker, daemon=True, name=f"enrich-{len(threads)}"))
                    threads[-1].start()                  # its worker is stuck: a new one keeps the pace
            if not live and time.time() - last_news >= HEARTBEAT_S:
                last_news = time.time()
                for dd, st_ in list(in_flight.items()):
                    print(f"    ... still working on {dd} ({_dur(time.time() - st_)}): waiting for the model",
                          flush=True)
            continue
        k += 1
        if not live:
            _item(k, total, d, titles[d])
        if isinstance(err, runner.BackendUnavailable):
            stop.set()
            from . import runstatus
            runstatus.record("network" if runstatus.classify(err) == "network" else "endpoint", str(err))
            print("STOPPED", flush=True)
            print(f"\nenrich: the model endpoint refused further work -- {err}\n"
                  f"  Documents in flight were not marked failed. Fix the model / key / credits "
                  f"(or wait for the quota to reset) and re-run.", file=sys.stderr)
            print(f"\nenrich: {n} Document(s) enriched before stopping, ${spent:.3f}")
            return 1
        spent += cost
        if err is not None or answer is None:
            _step_failed(m, "enrich", d, err or ValueError("no valid answer after a repair"))
            failed += 1
        else:
            new = E.apply(rec, answer, model, cost)
            if m.stage_status(d, "verify") is not None:    # before the write: a crash can only cause a harmless re-verify
                m.mark(StageState(d, "verify", "stale", error="enriched: new quotes to check"))
            pages.write_record(new)
            m.mark(StageState(d, "enrich", "ok", input_hash=E.record_identity(new), version=E.STAMP, attempts=1))
            n += 1
            print(f"{len(new.get('rules') or [])} rule(s), {new['extraction_meta']['enrich']['facts_added']} fact(s) added"
                  f" · ${cost:.3f} · {_dur(dt)}", flush=True)
        printed.release()
    if over_cap() and k < total:
        print(f"enrich: stopped at the spend cap (${spent:.3f}); {total - k} Document(s) wait for the next run")
    print(f"\nenrich: {n} Document(s) enriched, {failed} failed, ${spent:.3f} in {_dur(time.time() - started)}"
          + (" (failed ones retry on the next run)" if failed else "")
          + "; run `ytbrain verify && ytbrain index` to check their quotes and index them")
    return 0


def _upgrade_candidates(m: Manifest, args) -> list[dict]:
    """Valid records that are not what extracting them today would produce: an older version of
    their variant, or a variant new content of their kind no longer uses (ADR-0018). Each with
    the cost of its last extraction when the record kept it, and its text size."""
    from .extract import versions
    rows = m.db.execute(
        "SELECT s.doc_id, s.version FROM stage_state s JOIN documents d ON d.doc_id = s.doc_id "
        "WHERE s.stage='extract' AND s.status='ok' AND d.tombstoned_at IS NULL ORDER BY s.doc_id").fetchall()
    keep = _only({r["doc_id"] for r in rows}, args)
    known = _doc_domains(m, sorted(keep), declared=True)
    parity = versions.parity_passed()
    out = []
    for r in rows:
        if r["doc_id"] not in keep:
            continue
        variant, version = versions.parse_stamp(r["version"])
        have = f"{variant}@{version}"
        if not versions.is_current(have):
            continue                                   # stale: `ytbrain extract` redoes it anyway
        try:
            rec = json.loads((METADATA / f"{r['doc_id']}.json").read_text())
        except (OSError, ValueError):
            rec = {}
        want = versions.stamp(versions.variant_for(rec.get("source_kind"), known.get(r["doc_id"]), parity))
        if have == want or (getattr(args, "variant", None) and variant != args.variant):
            continue
        costs = [c.get("cost") for c in (rec.get("extraction_meta") or {}).get("calls") or []
                 if isinstance(c.get("cost"), (int, float))]
        try:
            t = json.loads((TRANSCRIPTS / f"{r['doc_id']}.json").read_text())
            chars = sum(len(u.get("text", "")) for u in t.get("utterances") or [])
        except (OSError, ValueError):
            chars = 0
        out.append({"doc_id": r["doc_id"], "have": have, "want": want,
                    "cost": sum(costs) if costs else None, "chars": chars})
    return out


def cmd_upgrade(args) -> int:
    """Re-extract valid records made by an older prompt version, on purpose and within a spend cap."""
    m = Manifest(MANIFEST_DB)
    cands = _upgrade_candidates(m, args)
    if not cands:
        print("upgrade: every record is on the prompt version a new extraction would use; nothing to do")
        return 0
    # estimate: a record's own last extraction cost, else its size at the median cost per character
    rates = sorted(c["cost"] / c["chars"] for c in cands if c["cost"] is not None and c["chars"])
    rate = rates[len(rates) // 2] if rates else None
    for c in cands:
        c["estimate"] = c["cost"] if c["cost"] is not None else (c["chars"] * rate if rate is not None else None)
    unknown = sum(1 for c in cands if c["estimate"] is None)
    if unknown and args.max_cost is not None:
        print(f"upgrade: {unknown} record(s) have no past extraction cost to estimate from, so --max-cost "
              f"can't be honoured; narrow the run with --doc or --variant, or leave out --max-cost")
        return 1
    chosen, total = [], 0.0
    for c in cands:
        est = c["estimate"] or 0.0
        if args.max_cost is not None and total + est > args.max_cost:
            break
        chosen.append(c)
        total += est
    groups: dict[tuple[str, str], int] = {}
    for c in chosen:
        groups[(c["have"], c["want"])] = groups.get((c["have"], c["want"]), 0) + 1
    for (have, want), n in sorted(groups.items()):
        print(f"upgrade: {n} record(s) {have} -> {want}")
    est = f"~${total:.2f}" + (" (part unknown: no past cost)" if unknown else "")
    left = len(cands) - len(chosen)
    print(f"upgrade: {len(chosen)} record(s), {est} estimated from their last extraction"
          + (f"; {left} more stay as they are (over --max-cost ${args.max_cost:g})" if left else ""))
    if args.dry_run or not chosen:
        if args.dry_run:
            print("upgrade: dry run, nothing changed")
        return 0
    n = m.invalidate_docs("extract", [c["doc_id"] for c in chosen], "upgrade to the latest prompt version")
    print(f"upgrade: marked {n} record(s) for re-extraction: run `ytbrain extract && ytbrain verify && "
          f"ytbrain index` (or `ytbrain ops ingest --no-sync`)")
    return 0


def cmd_invalidate(args) -> int:
    """Mark one stage stale (all docs, or --only-flagged) so only it re-runs."""
    m = Manifest(MANIFEST_DB)
    source = getattr(args, "source", None)
    if source or args.only_flagged:
        # --source and --only-flagged narrow each other: both given = the flagged ones of that source
        ids = [r["doc_id"] for r in m.db.execute("SELECT doc_id FROM documents WHERE source_id=?", (source,))] \
            if source else None
        if args.only_flagged:
            flagged = m.flagged_ids()
            ids = flagged if ids is None else sorted(set(ids) & set(flagged))
        what = " ".join(x for x in (f"of {source}" if source else "", "flagged/failed" if args.only_flagged else "") if x)
        n = m.invalidate_docs(args.stage, ids, args.reason or f"re-run: {what}")
        print(f"marked {n} document(s) {what} stale at '{args.stage}' -- re-run `ytbrain {args.stage}`"
              + (" (then `ytbrain verify`)" if args.only_flagged else ""))
        return 0
    n = m.invalidate_stage(args.stage, args.reason or "manual")
    print(f"marked {n} documents stale at '{args.stage}' — re-run that stage only")
    return 0


def cmd_drop(args) -> int:
    """Take one Source's Documents out of the knowledge (they leave the index at the next
    `ytbrain index`); their files stay, so re-enabling the Source costs no re-extraction."""
    m = Manifest(MANIFEST_DB)
    ids = [r["doc_id"] for r in m.db.execute(
        "SELECT doc_id FROM documents WHERE source_id=? AND tombstoned_at IS NULL", (args.source,))]
    if not ids:
        print(f"drop: no live documents from source {args.source!r}")
        return 1
    n = m.tombstone(ids)
    print(f"drop: {n} document(s) of {args.source} removed from the knowledge -- run `ytbrain index` "
          f"(and `ytbrain pack build`) to take them out of search; also set `enabled: false` for the "
          f"source in sources.yaml so the next sync doesn't bring them back")
    return 0


# --------------------------------------------------------------------------

def cmd_claude(args) -> int:
    """Start the local Claude Code CLI for plugin development, on your Claude plan with the
    token-saving model (default haiku). --openrouter runs it through OpenRouter instead (paid per
    token; the key comes from .env, never the command line).

    For `claude plugin eval` and live `--plugin-dir dist/plugin` sessions: ytbrain claude -- <args>."""
    from .eval import coach
    if not shutil.which("claude"):
        print("claude: the Claude Code CLI isn't on PATH (npm install -g @anthropic-ai/claude-code)", file=sys.stderr)
        return 1
    rest = list(args.claude_args or [])
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if args.openrouter:
        try:
            host = coach.openrouter_host(args.model if args.model and "/" in args.model else None)
        except ValueError as e:
            print(f"claude: {e}", file=sys.stderr)
            return 1
        env = {**os.environ, **host["env"]}
        print(f"claude: Claude Code through OpenRouter on "
              f"{args.model if args.model and '/' in args.model else coach.DEFAULT_OPENROUTER_MODEL} "
              f"(paid per token from your OpenRouter credit)", file=sys.stderr)
    else:
        model = args.model or coach.DEFAULT_MODEL
        env = {**os.environ, "ANTHROPIC_MODEL": model}
        print(f"claude: Claude Code on your Claude plan, model {model} (--model sonnet to change)", file=sys.stderr)
    env.update(coach_env_file_vars())
    return subprocess.run(["claude", *rest], env=env).returncode


def coach_env_file_vars() -> dict[str, str]:
    """Point founder-coach at this repo's .env wherever Claude Code starts it. A live session
    already inherits the variables; `claude plugin eval` withholds all but EVAL_* ones from its
    sandbox, so the path also goes in as EVAL_<PREFIX>ENV_FILE (founder_coach/settings.py; the
    sandbox keeps its own data folder)."""
    from founder_coach import product
    from .config import DOTENV
    if os.environ.get("YTBRAIN_DOTENV") == "0" or not DOTENV.is_file():
        return {}
    name = product.env_name("ENV_FILE")
    return {"EVAL_" + name: os.environ.get(name) or str(DOTENV)}


MUTATING = {"sync", "clean", "extract", "verify", "index", "pages", "refresh", "run", "invalidate", "eval", "drop",
            "upgrade", "tag", "enrich"}


def rerun_command(argv=None) -> str:
    """The command as it was typed (`--set dev-private`, `--config full`, ...), for resume hints:
    a hint that drops an option resumes a different job."""
    return shlex.join(["ytbrain", *(sys.argv[1:] if argv is None else argv)])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ytbrain", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, limit_help=None):
        sp = sub.add_parser(name, help=(fn.__doc__ or "").strip().split("\n")[0])
        if limit_help:
            sp.add_argument("--limit", type=int, default=0, help=limit_help)
        sp.set_defaults(func=fn, limit=0, workers=0)
        return sp

    # Discover with dual methods
    sp = add("discover", cmd_discover)
    sp.add_argument("--method", choices=["api", "ytdlp"], default="ytdlp",
                    help="discovery method: api (manual YouTube API) or ytdlp (automatic)")
    sp.add_argument("--channel", default=None,
                    help="channel ID or @handle (for ytdlp method; default: @ycombinator)")

    for name in ("sync", "run"):
        sp = add(name, cmd_sync if name == "sync" else cmd_run,
                 "sync: max videos listed per playlist" +
                 ("; clean/extract/verify: max videos per stage" if name == "run" else ""))
        sp.add_argument("--force", action="store_true",
                        help="re-fetch captions even for videos already fetched")
        sp.add_argument("--reconcile", action="store_true",
                        help="tombstone videos removed upstream (weekly)")
        sp.add_argument("--source", default=None, metavar="ID",
                        help="only this source (its id in sources.yaml)")
        sp.add_argument("--type", default=None, choices=SYNC_TYPES, dest="source_type",
                        help="only YouTube playlists or only websites (the sync step)")
        if name == "sync":
            sp.add_argument("--strict-domains", action="store_true",
                            help="exit 1 when a book folder is not a declared Domain (the books are still "
                                 "registered; `ytbrain ops` sets it)")
            sp.add_argument("--backfill", action="store_true",
                            help="only retry videos whose caption fetch failed (e.g. HTTP 429), "
                                 "slowly, without listing playlists")
            sp.add_argument("--per-hour", type=int, default=0,
                            help="--backfill pace (default 60 videos/hour)")
            sp.add_argument("--sleep-subtitles", type=float, default=None, metavar="SECONDS",
                            help="pause before EACH subtitle file (default 10; --backfill 15)")
        if name == "run":
            sp.add_argument("--device", choices=["auto", "mps", "cpu", "cuda"],
                            help="PyTorch device for the index Step")
            sp.add_argument("--workers", type=int, default=0,
                            help="parallel LLM calls in the extract stage (default: YTBRAIN_LLM_WORKERS or 1)")
    RETRY_HELP = "also retry talks parked after failing this Step repeatedly"
    add("clean", cmd_clean, "max videos to clean").add_argument("--retry-failed", action="store_true", help=RETRY_HELP)
    DOC_HELP = "only Documents whose id starts with PREFIX (repeatable); a Book's ISBN picks its Chapters"
    sp = add("extract", cmd_extract, "max videos to extract")
    sp.add_argument("--retry-failed", action="store_true", help=RETRY_HELP)
    sp.add_argument("--workers", type=int, default=0,
                    help="parallel LLM calls (default: YTBRAIN_LLM_WORKERS or 1)")
    sp.add_argument("--doc", action="append", metavar="PREFIX", help=DOC_HELP)
    sp = add("verify", cmd_verify, "max records to verify")
    sp.add_argument("--retry-failed", action="store_true", help=RETRY_HELP)
    sp.add_argument("--doc", action="append", metavar="PREFIX", help=DOC_HELP)
    sp = add("index", cmd_index, "max Documents to index")
    sp.add_argument("--retry-failed", action="store_true", help=RETRY_HELP)
    sp.add_argument("--device", choices=["auto", "mps", "cpu", "cuda"],
                    help="PyTorch device (default: YTBRAIN_DEVICE or auto = mps > cuda > cpu)")
    sp = add("search", cmd_search)
    sp.add_argument("query")
    sp.add_argument("--stage", choices=["pre-idea", "idea", "mvp", "pmf", "growth",
                                        "fundraising", "scaling", "exit"],
                    help="the Founder's Stage: boosts matching items")
    sp.add_argument("--require-stage", action="store_true", help="only items tagged with --stage")
    sp.add_argument("--kind", action="append", choices=["advice", "takeaway", "summary", "fact", "rule", "passage"],
                    help="restrict to these kinds (repeatable)")
    from .extract.schema import Category
    sp.add_argument("--topic", action="append", choices=[c.value for c in Category],
                    help="restrict to talks in these categories (repeatable)")
    sp.add_argument("--top-k", type=int, default=8)
    sp.add_argument("--no-rerank", action="store_true", help="skip the cross-encoder reranker")
    sp.add_argument("--device", choices=["auto", "mps", "cpu", "cuda"],
                    help="PyTorch device (default: YTBRAIN_DEVICE or auto = mps > cuda > cpu)")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--pack", nargs="?", const="", default=None, metavar="PATH",
                    help="search the Knowledge pack (ONNX models, no torch) instead of the index; "
                         "PATH defaults to data/pack")
    add("pages", cmd_pages)
    add("refresh", cmd_refresh)
    add("report", cmd_report)
    sp = add("sample", cmd_sample)
    sp.add_argument("--n", type=int, default=10, help="records in the review sheet")
    sp.add_argument("--seed", type=int, default=ACCEPT_SAMPLE_SEED, help="fixed for reproducibility")
    sp.add_argument("--doc", action="append", metavar="PREFIX", help=DOC_HELP)
    add("status", cmd_status)
    sp = add("enrich", cmd_enrich, "max Documents to enrich")
    sp.add_argument("--dry-run", action="store_true", help="how many Documents and the estimated cost; change nothing")
    sp.add_argument("--workers", type=int, default=0,
                    help="parallel LLM calls (default: YTBRAIN_LLM_WORKERS or 1, as for extract)")
    sp.add_argument("--max-cost", type=float, default=None, metavar="USD", help="stop at this spend")
    sp.add_argument("--doc", action="append", metavar="PREFIX", help=DOC_HELP)
    sp = add("tag", cmd_tag)
    sp.add_argument("--dry-run", action="store_true", help="show what would change and what the tie-breaks would cost")
    sp.add_argument("--max-cost", type=float, default=None, metavar="USD", help="stop asking the tie-break model at this spend")
    sp.add_argument("--no-llm", action="store_true", help="no tie-break calls: close calls keep every candidate")
    sp.add_argument("--retry-undecided", action="store_true",
                    help="ask again about close calls the model already left undecided (cached otherwise)")
    sp.add_argument("--no-apply", action="store_true", help="store the tags for review and labelling; leave the index's Domains")
    sp.add_argument("--gated", action="store_true", help="apply only when `ytbrain eval tags` passed for this tagger, "
                                                          "Domain list and thresholds (what `ytbrain ops` runs)")
    sp = add("upgrade", cmd_upgrade)
    sp.add_argument("--dry-run", action="store_true", help="show what would be re-extracted and its cost; change nothing")
    sp.add_argument("--max-cost", type=float, default=None, metavar="USD",
                    help="re-extract only as many records as fit this estimated spend")
    sp.add_argument("--variant", default=None, metavar="NAME", help="only records of this prompt variant")
    sp.add_argument("--doc", action="append", metavar="PREFIX", help=DOC_HELP)
    sp = add("invalidate", cmd_invalidate)
    sp.add_argument("stage", choices=["fetch", "clean", "extract", "verify", "index"],
                    help="stage to re-run")
    sp.add_argument("--reason", default="")
    sp.add_argument("--only-flagged", action="store_true",
                    help="only documents whose verify result was flagged/failed")
    sp.add_argument("--source", default=None, metavar="ID", help="only documents of this source")
    sp = add("drop", cmd_drop)
    sp.add_argument("--source", required=True, metavar="ID", help="the source whose Documents leave the knowledge")

    cc = sub.add_parser("claude", help="run the local Claude Code CLI for plugin work, on your Claude plan with "
                                       "Haiku: ytbrain claude -- <claude args>")
    cc.add_argument("--model", default=None, metavar="MODEL",
                    help="haiku (default; YTBRAIN_COACH_MODEL changes it), sonnet, opus; with --openrouter an "
                         "OpenRouter id (default anthropic/claude-haiku-4.5)")
    cc.add_argument("--openrouter", action="store_true",
                    help="through OpenRouter instead of your Claude plan (paid per token; key YTBRAIN_COACH_HOST_KEY)")
    cc.add_argument("claude_args", nargs=argparse.REMAINDER, help="arguments for claude, after --")
    cc.set_defaults(func=cmd_claude)
    sp = sub.add_parser("eval", help="build, run or inspect the eval benchmark (docs/eval-spec.md)")
    sp.set_defaults(func=cmd_eval, limit=0, workers=0)
    esub = sp.add_subparsers(dest="eval_cmd", required=True)
    eb = esub.add_parser("build", help="build or resume one split (resumable, spend-capped)")
    from .eval.splits import SETS as _eval_sets, TUNING_SPLITS as _tuning
    eb.add_argument("--set", choices=[*_tuning, "test"], default=None,
                    help="one Tuning split (default: every split in turn): dev (from Talks), dev-articles (from "
                         "Articles), dev-chapters (public Books), dev-private (your private Sources; never released)")
    eb.add_argument("--top-up", action="store_true",
                    help="grow each split to its target (at most 20%% of its Documents) with questions written "
                         "from Documents added since it was last seeded")
    eb.add_argument("--max-cost", type=float, default=None,
                    help="USD cap for this split's LLM spend, cumulative across runs (default 5)")
    eb.add_argument("--workers", type=int, default=0, help="parallel LLM calls (default 8)")
    eb.add_argument("--limit", type=int, default=0,
                    help="smoke test: N questions in a scratch benchmark under data/eval/smoke")
    er = esub.add_parser("run", help="score a search configuration")
    er.add_argument("--set", choices=list(_eval_sets), default="all",
                    help="all (default): every Tuning split together, one row per source kind; or one split")
    from .eval.run import CONFIGS as _eval_configs
    er.add_argument("--config", choices=list(_eval_configs),
                    default="full", help="pack configs search the Knowledge pack (see --pack)")
    er.add_argument("--pack", default=None, metavar="PATH",
                    help="Knowledge pack for pack configs (default data/pack)")
    er.add_argument("--only-pack-docs", action="store_true",
                    help="score only the questions seeded from the pack's own Documents (a Pack other than founder)")
    er.add_argument("--compare", metavar="CONFIG", help="compare with the saved baseline of CONFIG")
    er.add_argument("--save-baseline", action="store_true", help="store this result as the baseline")
    er.add_argument("--smoke", action="store_true", help="score the scratch benchmark from build --limit")
    er.add_argument("--label", default=None, metavar="NAME",
                    help="name this variant (e.g. arctic-passages): results are saved as <config>-<NAME>, "
                         "so `eval rescore --config pack-no-rerank-arctic-passages` finds them")
    ert = esub.add_parser("route", help="score the Domain router on single and composed (two-Domain) questions, "
                                        "with a margin sweep (read-only, no LLM)")
    ert.add_argument("--set", choices=list(_eval_sets), default="all")
    ert.add_argument("--pack", default=None, metavar="PATH", help="score a Knowledge pack instead of the full index")
    ert.add_argument("--composed", type=int, default=100, metavar="N", help="composed prompts to build (default 100)")
    ecal = esub.add_parser("calibrate", help="fit the curve that turns similarity into P(relevant) from your graded labels, "
                                             "so `coverage` means something (read-only apart from data/eval/calibration.json; no LLM)")
    ecal.add_argument("--set", choices=list(_eval_sets), default="all")
    ecal.add_argument("--pack", default=None, metavar="PATH", help="the Knowledge pack to fit for (default data/pack)")
    ecal.add_argument("--k", type=int, default=10, help="top hits per question to use (default 10)")
    ecal.add_argument("--no-save", action="store_true", help="report only")
    ecal.add_argument("--force", action="store_true", help="save even if the fitted curve is not better than the provisional one")
    egap = esub.add_parser("gap", help="how often coverage is wrong: Gap questions the coach would answer, answerable "
                                       "questions it would call a Gap, and where to move the border (no LLM)")
    egap.add_argument("--set", choices=list(_eval_sets), default="all")
    egap.add_argument("--pack", default=None, metavar="PATH", help="the Knowledge pack to measure (default data/pack)")
    egap.add_argument("--questions", default=None, metavar="FILE", help="your Gap questions (default data/eval/gap-questions.txt)")
    egap.add_argument("--only-pack-docs", action="store_true",
                      help="answerable questions only from the pack's own Documents (a Pack other than founder)")
    egap.add_argument("--tune", action="store_true", help="save the recommended shift for the next `pack build`")
    esub.add_parser("status", help="progress and spend of each split")
    elat = esub.add_parser("latency", help="search p95 the way the plugin runs it (its runtime, a Knowledge pack, "
                                            "ONNX models); gate 1.5 s")
    elat.add_argument("--pack", default=None, metavar="PATH", help="the pack to time (default data/pack)")
    elat.add_argument("--n", type=int, default=100, help="questions to time (default 100)")
    elat.add_argument("--no-rerank", action="store_true", help="time without the reranker even if the pack names one")
    epa = esub.add_parser("parity", help="the subject-neutral extraction prompt vs the startup one on startup talks "
                                         "(shadow extractions; the Library is untouched)")
    epa.add_argument("--n", type=int, default=30, help="talks to compare (default 30)")
    epa.add_argument("--max-cost", type=float, default=1.0, metavar="USD", help="spend cap (default $1)")
    etg = esub.add_parser("tags", help="the tag Step against your labelled sheets (`ytbrain domains sample`): "
                                       "precision, recall, high-tier misses, Documents")
    etg.add_argument("--items", required=True, metavar="CSV", help="the filled item sheet (a path, or a name in data/reports)")
    etg.add_argument("--documents", default=None, metavar="CSV", help="the filled Document sheet")
    etg.add_argument("--tune", action="store_true", help="score every threshold setting on these labels, best first")
    etg.add_argument("--save", action="store_true", help="with --tune: keep the best for the next `ytbrain tag`")
    ech = esub.add_parser("choice", help="P12: with several coaches installed, does each prompt reach the right one "
                                         "(the real host, your Claude plan; no judges)")
    ech.add_argument("--plugin", action="append", metavar="DIR", required=True,
                     help="an assembled plugin; pass two or more (dist/plugin-private, dist/coding-coach-private, ...)")
    ech.add_argument("--limit", type=int, default=0, help="first N prompts (a quick check)")
    ech.add_argument("--model", default=None, metavar="MODEL", help="host model (default haiku; 'default' for yours)")
    ech.add_argument("--workers", type=int, default=0,
                     help="host runs at a time (default: YTBRAIN_COACH_WORKERS or 1; each is a `claude -p` on your plan)")
    ec = esub.add_parser("coach", help="gates G2/G4/G5/G6/G7: the coach's answers and memory through the real host "
                                        "(`claude -p` with the assembled plugin), graded by the judges")
    ec.add_argument("--gate", action="append", choices=["g2", "g4", "g5", "g6", "g7", "g8"],
                    help="repeatable (default: all four)")
    ec.add_argument("--plugin", default=None, metavar="DIR", help="assembled plugin (default dist/plugin)")
    ec.add_argument("--limit", type=int, default=0, help="first N cases per gate (a quick check)")
    ec.add_argument("--model", default=None, metavar="MODEL",
                    help="haiku (default; YTBRAIN_COACH_MODEL changes it), sonnet, opus or 'default' (your Claude Code "
                         "default); with --host openrouter an OpenRouter id. Results are kept per model.")
    ec.add_argument("--max-turns", type=int, default=12, help="agentic turns per host call (default 12)")
    ec.add_argument("--workers", type=int, default=0,
                    help="cases at a time (default: YTBRAIN_COACH_WORKERS or 1; each is a `claude -p` on your plan)")
    ec.add_argument("--host", choices=["claude", "openrouter"], default="claude",
                    help="claude (default): the local Claude Code on your Claude plan, with the token-saving setup "
                         "(Haiku, turn cap, no repo context). openrouter: through OpenRouter, paid per token (key "
                         "YTBRAIN_COACH_HOST_KEY); --model is then an OpenRouter id (default anthropic/claude-haiku-4.5)")
    ec.add_argument("--max-cost", type=float, default=None, help="USD cap for judge calls (default 5)")
    ej = esub.add_parser("judge", help="grade the Moments a saved run retrieved that no judge has seen "
                                       "(pool extension); re-releases the labels as a new minor version")
    ej.add_argument("--set", choices=list(_eval_sets), default="all",
                    help="all (default): every Tuning split together, one row per source kind; or one split")
    ej.add_argument("--config", action="append", required=True,
                    help="configuration whose latest saved run to add (repeatable)")
    ej.add_argument("--depth", type=int, default=10, help="top Moments per question to add (default 10)")
    ej.add_argument("--max-cost", type=float, default=None,
                    help="USD cap for this split's LLM spend, cumulative across runs (default 5)")
    ej.add_argument("--workers", type=int, default=0, help="parallel LLM calls (default 8)")
    es = esub.add_parser("rescore", help="score a saved run again against the current labels (no search)")
    es.add_argument("--set", choices=list(_eval_sets), default="all",
                    help="all (default): every Tuning split together, one row per source kind; or one split")
    es.add_argument("--config", required=True)
    es.add_argument("--compare", metavar="CONFIG", help="compare with the saved baseline of CONFIG")
    es.add_argument("--save-baseline", action="store_true")
    es.add_argument("--run", metavar="FILE", help="a saved run file (in data/eval/runs) instead of the newest one")
    es.add_argument("--as", dest="as_name", metavar="NAME",
                    help="score (and with --save-baseline, save) under this name, e.g. full-prebooks")
    es.add_argument("--refresh-baseline", action="store_true",
                    help="re-score the saved baseline CONFIG from its own run against the current labels "
                         "(after `eval judge` released new ones), keeping its questions")

    sp = sub.add_parser("ops", help="the whole loop, resumable: ingest, eval when the index changed, "
                                    "the plugin when it passed (docs/ops.md)")
    sp.add_argument("plan", nargs="?", default="all", choices=["all", "ingest", "eval", "plugin"])
    sp.add_argument("--max-cost", type=float, default=None,
                    help="USD cap on this run's eval and coach-judge spend (default 5); extraction is billed "
                         "by your endpoint, see --max-extract")
    sp.add_argument("--max-extract", type=int, default=0, metavar="N", help="extract at most N Documents this run")
    sp.add_argument("--no-sync", action="store_true", help="skip the network sync (work on what is already fetched)")
    sp.add_argument("--coach", action="store_true",
                    help="also run the coach eval on the plugin (before a release): only the gates whose inputs changed")
    sp.add_argument("--skip-coach", action="store_true", help=argparse.SUPPRESS)   # the default now; kept for old scripts
    sp.add_argument("--restart", action="store_true", help="start over instead of resuming the last run")
    sp.add_argument("--force", action="store_true", help="run eval and the plugin even if nothing changed or the "
                                                         "last verdict wasn't a pass")
    sp.add_argument("--dry-run", action="store_true", help="show what is due and the steps, run nothing")
    sp.add_argument("--version", choices=["patch", "minor", "skip"], default=None, dest="plugin_version",
                    help="the new plugin build's version, without the question (default: ask, patch after 60 s "
                         "or with no terminal)")
    sp.add_argument("--no-notify", action="store_true", help="no macOS notifications (data/ops/last-run.md is "
                                                              "still written)")
    sp.set_defaults(func=cmd_ops)

    sp = sub.add_parser("domains", help="the Library's Domains (domains.yaml): list, add one, or ignore a sorting folder")
    sp.set_defaults(func=cmd_domains, limit=0, workers=0, domains_cmd="list")
    dsub = sp.add_subparsers(dest="domains_cmd")
    dsub.add_parser("list", help="the declared Domains, ignored folders, and book folders that are not a Domain yet")
    da = dsub.add_parser("add", help="declare a Domain (a books folder of the same name then belongs to it)")
    da.add_argument("name", help="lowercase letters, digits and dashes; a folder name such as `GTM` becomes `gtm`")
    da.add_argument("--risk", required=True, choices=["low", "medium", "high"],
                    help="how much harm a wrong answer can do: high declines below strong coverage (finance, legal, medical)")
    da.add_argument("--description", required=True, help="one line: what the Domain is about (the host chooses by it)")
    da.add_argument("--example", action="append", metavar="QUESTION", help="an example question (repeatable)")
    da.add_argument("--freshness", default="evergreen", type=lambda v: v if v == "evergreen" else int(v),
                    help="`evergreen` (default) or a half-life in days for how fast advice ages")
    da.add_argument("--web-policy", default="when_gap", choices=["never", "when_gap", "always_latest"])
    di = dsub.add_parser("ignore", help="mark a books folder as one that only sorts files (ignore_folders)")
    di.add_argument("folder")
    dl = dsub.add_parser("alias", help="another name for a Domain (`go-to-market` is `gtm`)")
    dl.add_argument("name", help="the Domain")
    dl.add_argument("alias", help="the other name")
    dw = dsub.add_parser("why", help="why a Document has its Domains: shares, hints, each item's scores")
    dw.add_argument("doc", metavar="DOC_ID_PREFIX")
    dw.add_argument("--items", type=int, default=20, help="items to list (default 20)")
    dr = dsub.add_parser("review", help="high-tier tags awaiting your confirmation, grouped by Source")
    dr.add_argument("--accept", action="append", metavar="SOURCE:DOMAIN",
                    help="confirm a group (repeatable); doc:DOC_ID:DOMAIN for one Document")
    dr.add_argument("--reject", action="append", metavar="SOURCE:DOMAIN",
                    help="never suggest this Domain there again (repeatable); doc:DOC_ID:DOMAIN for one Document")
    dp = dsub.add_parser("propose", help="group content no Domain fits and propose Domains, after the duplicate checks")
    dp.add_argument("--min-size", type=int, default=15, help="smallest group worth a Domain (default 15 items)")
    ds = dsub.add_parser("sample", help="labelling sheets for `ytbrain eval tags` (items and Documents)")
    ds.add_argument("--n", type=int, default=100, help="items (default 100)")
    ds.add_argument("--docs", type=int, default=30, help="Documents (default 30)")
    ds.add_argument("--seed", type=int, default=7)

    sp = sub.add_parser("books", help="PDF Books (ADR-0014): inspect how a PDF would be split into Chapters")
    sp.set_defaults(func=cmd_books, limit=0, workers=0)
    bsub = sp.add_subparsers(dest="books_cmd", required=True)
    bi = bsub.add_parser("inspect", help="probe, parse (cached by file hash) and split PDFs; no LLM calls")
    bi.add_argument("paths", nargs="+", metavar="PDF_OR_FOLDER")
    bi.add_argument("--json", action="store_true", help="the report as JSON")
    bi.add_argument("--reparse", action="store_true", help="ignore the cached parse")
    bi.add_argument("--parser", choices=["pdfium", "docling"], default="pdfium",
                    help="pdfium (default: model-free, about a second per book) or docling (parked: layout "
                         "models, needs the pdf-docling extra)")
    bi.add_argument("--expect", metavar="YAML", default=None,
                    help="compare with expected metadata and chapters (e.g. data/books/expected.yaml); exit 1 on a mismatch")

    sp = sub.add_parser("pack", help="build or inspect the Knowledge pack the coach plugin ships")
    sp.set_defaults(func=cmd_pack, limit=0, workers=0)
    psub = sp.add_subparsers(dest="pack_cmd", required=True)
    pb = psub.add_parser("build", help="embed every Verified item into one SQLite file (resumable)")
    pb.add_argument("--embed-model", default=None,
                    help="fastembed ONNX model (default YTBRAIN_PACK_EMBED_MODEL or BAAI/bge-base-en-v1.5)")
    pb.add_argument("--rerank-model", default=None,
                    help="reranker the runtime should use, or 'none' "
                         "(default YTBRAIN_PACK_RERANK_MODEL or none: measured slower and no better, CHANGELOG 2026-09-25)")
    pb.add_argument("--batch", type=int, default=64, help="texts per embedding batch")
    pb.add_argument("--device", choices=["cpu", "coreml", "cuda"], default="cpu",
                    help="ONNX Runtime device for embedding (default cpu; coreml = Apple GPU/Neural "
                         "Engine, experimental: compare speed on a small run first)")
    pb.add_argument("--with-passages", action="store_true",
                    help="also ship Passages (transcript excerpts, ~2x the size): private beta only (ADR-0009)")
    pb.add_argument("--include-private", action="store_true",
                    help="also pack private Sources (your own Books): for your own coach only, never released")
    pb.add_argument("--route", action="store_true",
                    help="turn on the server's automatic Domain routing for this pack (off by default: the host "
                         "chooses Domains until `eval run --config pack-route` shows it doesn't lose)")
    pi = psub.add_parser("info", help="describe the pack and verify its checksum")
    for x in (pb, pi):
        x.add_argument("--for", dest="for_pack", default=None, metavar="PACK",
                       help="the Pack (packs/<id>/pack.toml): its Domains' items only (default founder)")
        x.add_argument("--out", default=None, metavar="DIR",
                       help="pack folder (default: data/pack, data/pack-private with --include-private; "
                            "data/packs/<id>/... for another Pack)")
    for e in (eb, er, ert):
        e.add_argument("--device", choices=["auto", "mps", "cpu", "cuda"])

    args = p.parse_args(argv)
    import signal

    def _term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _term)
    # reading commands never wait for (or block) a long build: `eval status`, and `eval rescore`
    # (scores a saved run; its result file is written atomically)
    read_only = args.cmd == "eval" and getattr(args, "eval_cmd", "") in ("status", "rescore", "coach", "route", "gap", "calibrate") \
        and not getattr(args, "save_baseline", False) and not getattr(args, "refresh_baseline", False)
    read_only = read_only or (args.cmd in ("upgrade", "tag", "enrich") and args.dry_run) or \
        (args.cmd == "eval" and getattr(args, "eval_cmd", "") in ("tags", "parity", "latency"))
    if args.cmd not in MUTATING or read_only:
        return args.func(args)
    try:
        with exclusive():
            _sweep_partial_files()
            return args.func(args)
    except KeyboardInterrupt:
        # Every finished video is already committed to the manifest; the one
        # in flight was never marked, so it is simply redone. Nothing to clean up.
        from . import runstatus
        runstatus.record("interrupted", "Ctrl+C")
        print(f"\n\n{args.cmd}: interrupted. Finished work is saved; "
              f"re-run `{rerun_command(argv)}` to resume where it stopped.", file=sys.stderr)
        return 130
    except LockBusy as e:
        print(f"{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
