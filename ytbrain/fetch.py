"""Acquisition: enumerate a playlist and pull captions only (no video bytes).

OPERATIONAL NOTE: YouTube is unreachable from Anthropic's cloud container and
from the Cowork device VM - both fail `Tunnel connection failed: 403 Forbidden`
at the egress proxy (measured). Run this natively on the Mac, on residential
egress. It will also be unreliable on AWS/GCP/Azure IPs.
"""
from __future__ import annotations

import re

import functools
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .config import POT_PROVIDER_URL, POT_SCRIPT_HOME, RAW, YT_SLEEP_SUBTITLES, YTDLP_PROXY

YTDLP = "yt-dlp"
SUB_FORMAT = "srt/best"     # native srt; NOT --convert-subs (see captions.py)


def _run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


# yt-dlp retries 5xx and dropped connections itself, but by default with NO
# delay between tries. These make its own retries back off exponentially
# (http: 2, 4, 8 ... capped 60s; page extraction: 5, 10, 20 ... capped 120s).
# It never retries HTTP 429 on a subtitle download -- sync handles that.
YTDLP_RETRY_ARGS = ["--retries", "5", "--extractor-retries", "3",
                    "--retry-sleep", "http:exp=2:60", "--retry-sleep", "extractor:exp=5:120"]


# --- making yt-dlp look like a real client ---------------------------------
# Since yt-dlp 2025.11.12 full YouTube support needs an external JavaScript
# runtime plus the yt-dlp-ejs package (issue #15012); without them it falls
# back to fewer clients. yt-dlp enables only Deno by default, so another
# runtime found on PATH is switched on explicitly. A PO-token provider plugin
# (bgutil) is picked up by yt-dlp on its own once installed and its server runs.

_RUNTIMES = (("deno", None), ("node", "node"), ("bun", "bun"), ("qjs", "quickjs"))


@functools.lru_cache(maxsize=1)
def js_runtime() -> tuple[str | None, tuple[str, ...]]:
    """(runtime found on PATH, yt-dlp args that enable it)."""
    for exe, flag in _RUNTIMES:
        if shutil.which(exe):
            return exe, (("--js-runtimes", flag) if flag else ())
    return None, ()


def ytdlp_common_args() -> list[str]:
    """Arguments every yt-dlp call gets: JS runtime, proxy, PO-token provider URL."""
    args = list(js_runtime()[1])
    if YTDLP_PROXY:
        args += ["--proxy", YTDLP_PROXY]
    if os.environ.get("YTBRAIN_POT_PROVIDER_URL"):
        args += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}"]
    if os.environ.get("YTBRAIN_POT_SCRIPT_HOME"):
        args += ["--extractor-args", f"youtubepot-bgutilscript:server_home={POT_SCRIPT_HOME}"]
    return args


def _masked(url: str) -> str:
    """scheme://host:port without credentials."""
    u = urlsplit(url if "://" in url else f"http://{url}")
    return f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")


def _listening(url: str, timeout: float = 1.0) -> bool:
    u = urlsplit(url)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout):
            return True
    except OSError:
        return False


def _installed(dist: str) -> bool:
    from importlib.metadata import PackageNotFoundError, version
    try:
        version(dist)
        return True
    except PackageNotFoundError:
        return False


def environment_report() -> list[str]:
    """One line per thing that affects how YouTube treats our requests."""
    out = []
    if importlib.util.find_spec("yt_dlp_ejs") is None:
        out.append('WARNING: yt-dlp-ejs is missing, so YouTube support is degraded. '
                   'Fix: uv pip install -U "yt-dlp[default]"')
    runtime = js_runtime()[0]
    if runtime is None:
        out.append("WARNING: no JavaScript runtime on PATH (deno, node, bun or qjs), so "
                   "YouTube support is degraded. Fix: brew install deno")
    else:
        out.append(f"yt-dlp JavaScript runtime: {runtime}")
    if _installed("bgutil-ytdlp-pot-provider"):
        if _listening(POT_PROVIDER_URL):
            out.append(f"PO tokens: bgutil server at {_masked(POT_PROVIDER_URL)}")
        elif Path(POT_SCRIPT_HOME).is_dir():
            out.append(f"PO tokens: bgutil script mode ({POT_SCRIPT_HOME})")
        else:
            out.append(f"WARNING: the bgutil PO-token plugin is installed but finds neither a "
                       f"server at {_masked(POT_PROVIDER_URL)} nor a script-mode checkout at "
                       f"{POT_SCRIPT_HOME}. See README: 'Optional: a PO-token provider'.")
    if YTDLP_PROXY:
        out.append(f"yt-dlp proxy: {_masked(YTDLP_PROXY)} (from YTBRAIN_YTDLP_PROXY)")
    return out


def enumerate_playlist(playlist_id: str, limit: int | None = None) -> list[dict]:
    """Cheap flat enumeration: ids + titles, no page renders, no downloads."""
    args = [YTDLP, "--flat-playlist", "--no-warnings", *YTDLP_RETRY_ARGS, *ytdlp_common_args(),
            "--print", "%(id)s\t%(title)s\t%(duration)s",
            f"https://www.youtube.com/playlist?list={playlist_id}"]
    if limit:
        args[1:1] = ["--playlist-end", str(limit)]
    proc = _run(args)
    if proc.returncode != 0:
        raise RuntimeError(f"enumeration failed for {playlist_id}: {proc.stderr[-500:]}")
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            rows.append({"doc_id": parts[0], "title": parts[1],
                         "duration_s": _int_or_none(parts[2] if len(parts) > 2 else None)})
    return rows


# yt-dlp errors that retrying cannot fix. Everything else (HTTP 429, network,
# PO-token) stays 'failed' and is retried by the next sync. Playlists keep
# listing private/removed videos (flat enumeration shows their title as "NA"),
# so without this they were re-requested on every run -- wasted requests that
# push the whole run toward rate limits.
PERMANENT_ERRORS = (
    "Private video",
    "Video unavailable",
    "This video has been removed",
    "account associated with this video has been terminated",
    "members-only",
)


def permanent_failure(stderr: str | None) -> str | None:
    """The matching PERMANENT_ERRORS entry, or None if a retry might succeed."""
    low = (stderr or "").lower()
    return next((e for e in PERMANENT_ERRORS if e.lower() in low), None)


def failure_reason(stderr: str | None) -> str:
    """One short human label for a failed fetch."""
    if "429" in (stderr or ""):
        return "rate limited, HTTP 429"
    lines = [l for l in (stderr or "").strip().splitlines() if l.strip()]
    return re.sub(r"^ERROR: (\[\w+\] \S+: )?", "", lines[-1])[:80] if lines else "unknown error"


# Exact English tracks only. The old default `en.*` also matched YouTube's
# machine-TRANSLATED tracks (en-en, en-tr-<id>, en-ar-<id> ...): extra requests
# per video, redundant text, and a 429 on any one of them failed the whole video.
DEFAULT_SUB_LANGS = "en,en-orig,en-US,en-GB"


def fetch_captions(doc_id: str, sub_langs: str = DEFAULT_SUB_LANGS, sleep: float = 1.5,
                   sleep_subtitles: float = YT_SLEEP_SUBTITLES) -> dict:
    """Pull captions + info.json for one video. No video, no audio.

    Both --write-subs (human) and --write-auto-subs (machine) are requested in
    one pass because yt-dlp has no flag expressing "prefer human, fall back to
    auto" (open request: yt-dlp issue #9371). The preference is applied
    afterwards by reading info.json - see `select_caption`.
    """
    args = [
        YTDLP, "--skip-download", "--no-warnings",
        "--write-info-json", "--write-subs", "--write-auto-subs",
        "--sub-langs", sub_langs, "--sub-format", SUB_FORMAT,
        # No --download-archive: resume is the manifest's job. An archive hit
        # makes yt-dlp exit 0 with no files, which would read as "no captions"
        # and be settled for good. --no-continue: never append to a .part left
        # by an interrupted run; always download the subtitle afresh.
        "--no-continue",
        "--sleep-requests", str(sleep), "--sleep-subtitles", str(sleep_subtitles),
        *YTDLP_RETRY_ARGS, *ytdlp_common_args(),
        "-o", str(RAW / "%(id)s.%(ext)s"),
        f"https://www.youtube.com/watch?v={doc_id}",
    ]
    try:
        proc = _run(args)
    except subprocess.TimeoutExpired as e:
        # yt-dlp's own retry sleeps under throttling can outlast the timeout: treat it as a
        # transient failure (retried next sync), never as a crash of the whole sync
        proc = subprocess.CompletedProcess(args, 124, "", f"ERROR: yt-dlp timed out after {e.timeout:.0f}s")
    info_path = RAW / f"{doc_id}.info.json"
    selection = select_caption(doc_id, info_path if info_path.exists() else None)
    return {
        "doc_id": doc_id,
        "returncode": proc.returncode,
        "stderr_tail": proc.stderr[-500:] if proc.returncode else "",
        "info_path": str(info_path) if info_path.exists() else None,
        **selection,
    }


def select_caption(doc_id: str, info_path: Path | str | None) -> dict:
    """Choose the caption file, preferring a human track over a machine one.

    This CANNOT be done from filenames: yt-dlp writes both human and automatic
    English tracks as `<id>.en.srt`, so a glob-and-sort picks arbitrarily. The
    authoritative signal is info.json, where human tracks are listed under
    `subtitles` and machine ones under `automatic_captions`; both keys can be
    populated for the same video.

    Returns {caption_path, caption_kind, caption_lang}.
    """
    none_result = {"caption_path": None, "caption_kind": "none", "caption_lang": None}
    if not info_path or not Path(info_path).exists():
        return none_result
    try:
        info = json.loads(Path(info_path).read_text())
    except (json.JSONDecodeError, OSError):
        return none_result

    for key, kind in (("subtitles", "human"), ("automatic_captions", "auto")):
        langs = info.get(key) or {}
        for lang in _english_langs(langs):
            path = _caption_file(doc_id, lang)
            if path:
                return {"caption_path": str(path), "caption_kind": kind, "caption_lang": lang}
    # The track is advertised but the file is absent (PO-token failures can
    # produce this silently). Report it rather than pretending we have captions.
    advertised = bool(_english_langs(info.get("subtitles") or {})
                      or _english_langs(info.get("automatic_captions") or {}))
    return {**none_result, "caption_kind": "missing_file" if advertised else "none"}


def _english_langs(langs: dict) -> list[str]:
    """English variants, plain `en` first so it wins over `en-US`/`en-orig`."""
    found = [k for k in langs if k == "en" or k.startswith("en-") or k.startswith("en.")]
    return sorted(found, key=lambda k: (k != "en", len(k), k))


def _caption_file(doc_id: str, lang: str) -> Path | None:
    for ext in ("srt", "vtt"):
        p = RAW / f"{doc_id}.{lang}.{ext}"
        if p.exists():
            return p
    return None


def uploader_chapters(info_path: Path | str | None) -> list[dict]:
    """Uploader-defined chapters from info.json, when the video has them.

    Exact and human-authored, so strictly better than LLM inference - and they
    remove an LLM pass entirely for that subset (decision Q5).
    Returns [] when absent, which is the signal to chapterize with the model.
    """
    if not info_path or not Path(info_path).exists():
        return []
    try:
        info = json.loads(Path(info_path).read_text())
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for i, ch in enumerate(info.get("chapters") or []):
        if ch.get("title") is None or ch.get("start_time") is None:
            continue
        out.append({
            "chapter_id": f"ch{i + 1:02d}",
            "title": str(ch["title"]).strip(),
            "start_ms": int(float(ch["start_time"]) * 1000),
            "end_ms": int(float(ch["end_time"]) * 1000) if ch.get("end_time") else None,
            "source": "uploader",
        })
    return out


def info_fields(info_path: Path | str | None) -> dict:
    """The metadata we take from yt-dlp rather than from the model."""
    if not info_path or not Path(info_path).exists():
        return {}
    try:
        info = json.loads(Path(info_path).read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    upload = info.get("upload_date")     # YYYYMMDD
    return {
        "title": info.get("title"),
        "duration_s": info.get("duration"),
        "published_at": f"{upload[:4]}-{upload[4:6]}-{upload[6:]}" if upload else None,
        "uploader": info.get("uploader"),
        "channel_id": info.get("channel_id"),
    }


def channel_rss_url(channel_id: str) -> str:
    """Free change-detection feed (last ~15 uploads, no API quota)."""
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def file_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()[:16]


def _int_or_none(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
