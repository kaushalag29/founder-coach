"""Refuse to commit or push secrets: scan the files git would track for API keys and .env files.

    python scripts/check_secrets.py            # the files git tracks (or would, before the first commit)

Runs in CI and as the pre-commit hook (docs/release.md). Exit 1 names each file and line,
never the secret itself. Stdlib only.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    "OpenRouter key": re.compile(r"sk-or-v1-[0-9a-f]{20,}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"),
    "Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # an assignment with a real-looking value (templates use empty values or <placeholders>)
    "key assignment": re.compile(r"(?i)\b[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)\s*=\s*['\"]?(?!<|\$|\{|your|changeme|xxx)[A-Za-z0-9_\-/+=]{16,}"),
}
PLACEHOLDER = re.compile(r"(?i)replace|your[_-]|example|changeme|placeholder|xxxx|dummy|fake|test[_-]?key")
FORBIDDEN = re.compile(r"(^|/)\.env(\.[^/]*)?$")
ALLOWED = {".env.example"}


def tracked_files() -> list[Path]:
    """What git tracks plus what it would add (untracked, not ignored)."""
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        sys.exit("check_secrets: run inside the git repo (git init first)")
    return [ROOT / f for f in out.decode().split("\0") if f]


def scan(files: list[Path], base: Path = ROOT) -> list[str]:
    problems = []
    for f in files:
        try:
            rel = f.relative_to(base).as_posix()
        except ValueError:
            rel = f.as_posix()
        if FORBIDDEN.search(rel) and f.name not in ALLOWED:
            problems.append(f"{rel}: an env file must never be committed (it is in .gitignore)")
            continue
        if not f.is_file() or f.stat().st_size > 5_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for what, pat in PATTERNS.items():
                m = pat.search(line)
                if m and not PLACEHOLDER.search(m.group(0)) and "check_secrets: allow" not in line:
                    problems.append(f"{rel}:{n}: looks like a {what}")
    return problems


def main() -> int:
    problems = scan(tracked_files())
    for p in problems:
        print(f"check_secrets: {p}", file=sys.stderr)
    if problems:
        print("check_secrets: remove these (or add `check_secrets: allow` to a line that is not a secret)",
              file=sys.stderr)
        return 1
    print("check_secrets: no secrets in the tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
