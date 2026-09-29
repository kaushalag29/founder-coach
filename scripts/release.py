"""Publish a beta release: build the plugin and commit it to the marketplace repo (docs/release.md).

    python scripts/release.py --pack data/pack --marketplace ../founder-coach-marketplace [--version 0.1.1] [--push]

The marketplace repo (github.com/<owner>/<marketplace> in product.toml, private) is what beta
testers add in Claude Code. One release:
1. Checks: the marketplace clone is a clean git repo, this repo has no uncommitted changes
   (--allow-dirty to override), no secrets in either, and the version isn't released yet.
2. Sets --version (if given) in plugin.json, the plugin's pyproject.toml and founder_coach.
   Claude Code only updates testers whose installed version differs, so every release needs
   a new version.
3. Assembles the plugin with --check (scripts/assemble_plugin.py) into dist/release/.
4. Replaces plugins/<id>/ in the marketplace clone with it and writes marketplace.json (no
   version there: plugin.json is the one place) and a README with the install steps. A changed
   product id is added to `renames`, so existing installs migrate instead of breaking.
5. Validates the marketplace with `claude plugin validate`, commits, tags <id>--v<version>, and
   with --push pushes both. Without --push it prints the push command.
Nothing in the marketplace clone changes until every check has passed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assemble_plugin as A      # noqa: E402
import check_secrets as S       # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
GITHUB_FILE_LIMIT, GITHUB_FILE_WARN = 100 * 2**20, 50 * 2**20


class ReleaseError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ReleaseError(f"git {' '.join(args)} failed in {repo}: {e}") from None
    if check and r.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed in {repo}: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout.strip()


def _is_repo(path: Path) -> bool:
    return path.is_dir() and subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                                            capture_output=True).returncode == 0


# ---- version -------------------------------------------------------------------------------

def version_files(root: Path) -> dict[str, Path]:
    return {"plugin.json": root / "plugin" / ".claude-plugin" / "plugin.json",
            "pyproject.toml": root / "plugin" / "pyproject.toml",
            "__init__.py": root / "founder_coach" / "__init__.py"}


def current_version(root: Path) -> str:
    return json.loads(version_files(root)["plugin.json"].read_text())["version"]


def set_version(root: Path, new: str) -> None:
    """Write the version in all three places (tests check they agree)."""
    old = current_version(root)
    if not SEMVER.match(new):
        raise ReleaseError(f"--version must look like 1.2.3, got {new!r}")
    if tuple(map(int, SEMVER.match(new).groups())) <= tuple(map(int, SEMVER.match(old).groups())):
        raise ReleaseError(f"--version {new} must be higher than the current {old}")
    f = version_files(root)
    edits = {f["plugin.json"]: (f'"version": "{old}"', f'"version": "{new}"'),
             f["pyproject.toml"]: (f'version = "{old}"', f'version = "{new}"'),
             f["__init__.py"]: (f'__version__ = "{old}"', f'__version__ = "{new}"')}
    for path, (a, b) in edits.items():
        text = path.read_text(encoding="utf-8")
        if text.count(a) != 1:
            raise ReleaseError(f"can't find {a!r} exactly once in {path}; set the version by hand")
    for path, (a, b) in edits.items():
        path.write_text(path.read_text(encoding="utf-8").replace(a, b), encoding="utf-8")


# ---- marketplace files --------------------------------------------------------------------

def marketplace_json(prod: dict, existing: dict | None) -> dict:
    pid, name = prod["id"], prod["repos"].get("marketplace") or f"{prod['id']}-marketplace"
    renames = dict((existing or {}).get("renames") or {})
    for entry in (existing or {}).get("plugins") or []:
        old = entry.get("name")
        if old and old != pid and old not in renames:
            renames[old] = pid                  # append-only history: existing installs migrate
    for old, new in list(renames.items()):      # an earlier rename now points on to the current id
        if new not in (None, pid) and new in renames:
            renames[old] = renames[new]
    out = {"name": name, "owner": {"name": prod["author"]},
           "description": f"{prod['display_name']} private beta: {prod['description']}",
           "plugins": [{"name": pid, "source": f"./plugins/{pid}", "description": prod["description"],
                        "category": "productivity", "tags": prod.get("keywords", [])}]}
    if renames:
        out["renames"] = renames
    return out


def readme(prod: dict, version: str) -> str:
    owner, name, pid = prod["repos"].get("owner", "<owner>"), prod["repos"].get("marketplace"), prod["id"]
    return f"""# {prod['display_name']} (private beta)

{prod['description']}

Version {version}. This repository is generated by `scripts/release.py` in the development repo;
don't edit it by hand.

## Install (Claude Code)

You need read access to this private repository, `git`, and `uv`
(`curl -LsSf https://astral.sh/uv/install.sh | sh`). Git must reach GitHub without prompting:
an SSH key loaded in ssh-agent, or `gh auth login && gh auth setup-git`.

In a Claude Code session:

```
/plugin marketplace add {owner}/{name}
/plugin install {pid}@{name}
```

Then run `/{pid}:setup`. Your data stays on your machine in `~/.{pid}`.

## Updates

In `/plugin` → Marketplaces → {name}, choose **Enable auto-update**, or run
`/plugin marketplace update {name}` when a new version is announced.

## Feedback

After an answer that missed, run `/{pid}:feedback` with what was wrong. It saves on your machine;
`/{pid}:feedback` can also write the file to send back, and offers the usage log too (which tools
ran, when and how long, never your words). Both leave your machine only if you send them.
"""


def _check_sizes(tree: Path) -> list[str]:
    warnings = []
    for p in tree.rglob("*"):
        if p.is_file():
            size = p.stat().st_size
            if size >= GITHUB_FILE_LIMIT:
                raise ReleaseError(f"{p} is {size / 2**20:.0f} MB: GitHub refuses files over 100 MB "
                                   "(and Git LFS content never reaches Claude Code's clone)")
            if size >= GITHUB_FILE_WARN:
                warnings.append(f"{p.name} is {size / 2**20:.0f} MB (GitHub warns above 50 MB)")
    return warnings


def _validate(marketplace: Path) -> str:
    claude = shutil.which("claude")
    if not claude:
        raise ReleaseError("`claude` isn't on PATH: install Claude Code, or pass --no-validate")
    r = subprocess.run([claude, "plugin", "validate", str(marketplace)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise ReleaseError(f"claude plugin validate failed:\n{(r.stdout + r.stderr).strip()[-2000:]}")
    return (r.stdout.strip().splitlines() or ["ok"])[-1]


# ---- release -------------------------------------------------------------------------------

def release(root: Path, pack: Path, marketplace: Path, version: str | None = None, push: bool = False,
            allow_dirty: bool = False, validate: bool = True, say=print) -> dict:
    root, marketplace = root.resolve(), marketplace.expanduser().resolve()
    prod = A.load_product(root)
    pid = prod["id"]

    # 1. checks, before anything changes
    if not _is_repo(marketplace):
        owner, name = prod["repos"].get("owner"), prod["repos"].get("marketplace")
        raise ReleaseError(f"{marketplace} isn't a git clone of the marketplace repo. Create the private repo "
                           f"{owner}/{name} on GitHub, then: git clone git@github.com:{owner}/{name}.git {marketplace}")
    if _git(marketplace, "status", "--porcelain"):
        raise ReleaseError(f"{marketplace} has uncommitted changes; commit or discard them first")
    dev_sha = None
    if _is_repo(root):
        if _git(root, "status", "--porcelain") and not allow_dirty:
            raise ReleaseError("this repo has uncommitted changes: commit first, so the release matches a commit "
                               "(or pass --allow-dirty)")
        problems = S.scan([root / f for f in _git(root, "ls-files", "-z").split("\0") if f], base=root)
        if problems:
            raise ReleaseError("secrets in the repo: " + "; ".join(problems))
        dev_sha = _git(root, "rev-parse", "--short", "HEAD", check=False) or None
    ver = version or current_version(root)
    tag = f"{pid}--v{ver}"
    if _git(marketplace, "tag", "--list", tag):
        raise ReleaseError(f"{tag} is already released: pass --version with a higher version "
                           "(testers only get an update when the version changes)")

    # 2-3. version, then build and check; a failure puts the version files back
    saved = {p: p.read_text(encoding="utf-8") for p in version_files(root).values()}
    try:
        if version:
            set_version(root, version)
        out = root / "dist" / "release" / pid
        res = A.assemble(root, pack, out, check=True)
        problems = S.scan([p for p in out.rglob("*") if p.is_file()], base=out)
        if problems:
            raise ReleaseError("secrets in the built plugin: " + "; ".join(problems))
        warnings = _check_sizes(out)
    except BaseException:
        for path, text in saved.items():
            path.write_text(text, encoding="utf-8")
        raise

    if _is_repo(root):                          # the build regenerated files the release commit should hold
        changed = [l[3:] for l in _git(root, "status", "--porcelain", "--", "founder_coach/product.json",
                                       "founder_coach/playbooks").splitlines()]
        if changed:
            warnings.append(f"generated files changed during the build, commit them: {', '.join(changed)}")

    # 4. write the marketplace and validate it; a failure puts the clone (and the version files)
    # back as they were, so a release either commits completely or leaves no trace
    try:
        mk = _write_marketplace(prod, marketplace, out, ver)
        checked = _validate(marketplace) if validate else "skipped (--no-validate)"
    except BaseException:
        _git(marketplace, "reset", "-q", "--hard", check=False)     # fails harmlessly before the first commit
        _git(marketplace, "clean", "-fdq", check=False)
        for path, text in saved.items():
            path.write_text(text, encoding="utf-8")
        raise

    # 5. commit, tag, push
    _git(marketplace, "add", "-A")
    msg = f"{prod['display_name']} {ver}\n\nbuild {res['build_id']}" + (f"\ndev {dev_sha}" if dev_sha else "")
    _git(marketplace, "commit", "-q", "-m", msg)
    _git(marketplace, "tag", "-a", tag, "-m", f"{prod['display_name']} {ver}")
    pushed = False
    if push:
        try:
            _git(marketplace, "push", "--follow-tags", "origin", "HEAD")
            pushed = True
        except ReleaseError as e:               # committed and tagged: keep it, say how to finish
            warnings.append(f"push failed, the release is committed and tagged locally: {e}")
    say(f"release: {pid} {ver} (build {res['build_id']}) committed to {marketplace} and tagged {tag}; "
        f"validate: {checked}")
    for w in warnings:
        say(f"  warning: {w}")
    if not pushed:
        say(f"  next: git -C {shlex_quote(str(marketplace))} push --follow-tags origin HEAD")
    if version:
        say(f"  the version files in this repo changed to {ver}: commit them "
            f"(git commit -am 'Release {ver}')")
    return {"version": ver, "tag": tag, "build_id": res["build_id"], "pushed": pushed, "marketplace": mk,
            "warnings": warnings}


def _write_marketplace(prod: dict, marketplace: Path, out: Path, ver: str) -> dict:
    pid = prod["id"]
    mj = marketplace / ".claude-plugin" / "marketplace.json"
    existing = json.loads(mj.read_text()) if mj.exists() else None
    mk = marketplace_json(prod, existing)
    for entry in (existing or {}).get("plugins") or []:          # a renamed plugin's old folder goes
        old_dir = marketplace / "plugins" / str(entry.get("name"))
        if entry.get("name") != pid and old_dir.is_dir():
            shutil.rmtree(old_dir)
    dest = marketplace / "plugins" / pid
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(out, dest)
    mj.parent.mkdir(parents=True, exist_ok=True)
    mj.write_text(json.dumps(mk, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (marketplace / "README.md").write_text(readme(prod, ver), encoding="utf-8")
    (marketplace / ".gitignore").write_text(".DS_Store\n__pycache__/\n*.pyc\n", encoding="utf-8")
    return mk


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pack", type=Path, required=True, help="the Knowledge pack folder from `ytbrain pack build`")
    ap.add_argument("--marketplace", type=Path, required=True, help="your clone of the marketplace repo")
    ap.add_argument("--version", default=None, help="the new version, e.g. 0.1.1 (required after the first release)")
    ap.add_argument("--push", action="store_true", help="push the commit and tag to GitHub")
    ap.add_argument("--allow-dirty", action="store_true", help="release with uncommitted changes in this repo")
    ap.add_argument("--no-validate", action="store_true", help="skip `claude plugin validate`")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        release(args.root, args.pack, args.marketplace, version=args.version, push=args.push,
                allow_dirty=args.allow_dirty, validate=not args.no_validate)
    except (ReleaseError, A.AssembleError) as e:
        print(f"release: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
