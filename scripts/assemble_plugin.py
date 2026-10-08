"""Assemble the installable plugin of one Pack: plugin/ + its skills + founder_coach/ + its Knowledge pack.

    python scripts/assemble_plugin.py --pack PATH [--for founder] [--out dist/plugin] [--check] [--zip]

One plugin per Pack (ADR-0016): `--for <id>` reads packs/<id>/pack.toml for the product identity, the skills
it ships (its own packs/<id>/skills/<name>/ first, then plugin/skills/<name>/), the MCP prompts and the
runtime's Pack settings (Domains, memory modules). The founder Pack is the default and builds exactly
today's plugin.

Steps, each safe to re-run:
1. Regenerate founder_coach/product.json from product.toml (the product's name, ADR-0012) and
   founder_coach/playbooks/*.md (the MCP prompts) from the skills, the single source.
2. Build the plugin in a temporary folder next to --out: plugin files with the product's
   {{placeholders}} filled in, the runtime package (no caches), the pack, and a BUILD_ID stamp
   so uv reinstalls the runtime.
3. Swap it into place: the previous build is kept as <out>.previous until the new one is in.

plugin/ is a template (its files say `{{id}}` where the product id goes), so load the
assembled dist/plugin in Claude Code, not plugin/ itself.
With --check, import the assembled runtime and open the assembled pack before swapping,
so a broken build never replaces a working one.

With --zip, also write dist/<id>-<version>.plugin: the one file to upload in Cowork (Customize > Plugins).

Local test:  claude plugin validate dist/plugin --strict && claude --plugin-dir dist/plugin
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_SKILLS = ("ask", "weekly-focus", "check-in", "setup")   # the founder Pack's MCP prompts (its pack.toml `prompts`)
# = founder_coach.pack's names; not imported, so the assembler runs without the runtime's dependencies
PACK_FILE, MANIFEST_FILE = "knowledge.sqlite", "pack.json"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".pytest_cache")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
RENDERED = {".md", ".json", ".toml", ".yaml", ".yml", ".txt"}   # plugin files that may hold {{placeholders}}
PRODUCT_FIELDS = ("id", "display_name", "description", "author", "license")
SHARED_FIELDS = ("author", "license")     # product.toml: what every Pack shares
DERIVED_FIELDS = ("env_prefix",)          # {{env_prefix}}: the runtime's environment variables, e.g. ACME_COACH_


def env_prefix(pid: str) -> str:
    """The same prefix founder_coach.product.ENV_PREFIX derives from the id."""
    return pid.upper().replace("-", "_") + "_"


class AssembleError(RuntimeError):
    pass


# ---- the product's identity (product.toml, ADR-0012) ---------------------------------------

def _packs():
    """ytbrain.packs, standard library only, imported from this checkout."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from ytbrain import packs
    return packs


def load_product(root: Path = ROOT, pack_id: str | None = None, check_skills: bool = False) -> dict:
    """One Pack's product identity (packs/<id>/pack.toml [product], ADR-0012 + ADR-0016) with what every Pack
    shares (product.toml: author, license, repos), checked. `prod["pack"]` is the Pack itself."""
    P = _packs()
    pack_id = pack_id or P.DEFAULT_PACK
    path = root / "product.toml"
    if not path.exists():
        raise AssembleError(f"missing {path}: it holds what every Pack shares (author, license, repos)")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise AssembleError(f"{path} is not valid TOML: {e}") from None
    shared = dict(data.get("product") or {})
    if moved := [k for k in ("id", "display_name", "description", "keywords") if k in shared]:
        raise AssembleError(f"{path}: {', '.join(moved)} moved to packs/<id>/pack.toml [product] (one per Pack)")
    try:
        pack = P.load(pack_id, root, check_skills=check_skills)
    except P.PackError as e:
        raise AssembleError(str(e)) from None
    prod = {**{k: shared.get(k) for k in SHARED_FIELDS}, **pack.product}
    prod["repos"] = dict(data.get("repos") or {})
    prod["pack"] = pack
    missing = [f for f in PRODUCT_FIELDS if not isinstance(prod.get(f), str) or not prod[f].strip()]
    if missing:
        raise AssembleError(f"{path} / packs/{pack_id}/pack.toml: [product] needs {', '.join(missing)}")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", prod["id"]):
        raise AssembleError(f"{path}: id {prod['id']!r} must be lowercase letters, digits and hyphens "
                            "(it becomes the install id, the CLI and ~/.<id>)")
    if not isinstance(prod.get("keywords", []), list) or not all(isinstance(k, str) for k in prod.get("keywords", [])):
        raise AssembleError(f"{path}: keywords must be a list of strings")
    prod.setdefault("keywords", [])
    return prod


def product_json(prod: dict) -> str:
    """What founder_coach/product.json holds: only what the runtime needs (its identity and its Pack)."""
    pack = prod["pack"]
    return json.dumps({"_generated": f"from packs/{pack.id}/pack.toml by scripts/assemble_plugin.py; edit that file",
                       "display_name": prod["display_name"], "id": prod["id"],
                       "pack": _packs().runtime_config(pack)}, indent=2) + "\n"


def sync_product_json(root: Path, prod: dict) -> bool:
    dest = root / "founder_coach" / "product.json"
    new = product_json(prod)
    if dest.exists() and dest.read_text(encoding="utf-8") == new:
        return False
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, dest)
    return True


def render(text: str, prod: dict, where: str = "text", quote: bool = False) -> str:
    """Fill {{field}} placeholders from product.toml. quote=True escapes values for a JSON or
    TOML string. An unknown placeholder is an error, never shipped."""
    words = dict(prod["pack"].words) if prod.get("pack") else {}
    known = PRODUCT_FIELDS + DERIVED_FIELDS + tuple(words)

    def one(m: re.Match) -> str:
        key = m.group(1)
        if key not in known:
            raise AssembleError(f"{where}: unknown placeholder {{{{{key}}}}} (known: {', '.join(known)})")
        v = env_prefix(prod["id"]) if key == "env_prefix" else words[key] if key in words else prod[key]
        return json.dumps(v)[1:-1] if quote else v
    return PLACEHOLDER.sub(one, text)


def render_tree(build: Path, prod: dict, skip: tuple[str, ...] = ("founder_coach", "pack")) -> int:
    """Fill the placeholders in every text file of an assembled plugin (not the runtime or pack)."""
    n = 0
    for p in sorted(build.rglob("*")):
        rel = p.relative_to(build)
        if not p.is_file() or p.suffix not in RENDERED or rel.parts[0] in skip:
            continue
        text = p.read_text(encoding="utf-8")
        if "{{" not in text:
            continue
        if rel.as_posix() == ".claude-plugin/plugin.json":     # keywords is a list, not a string
            m = json.loads(text)
            if m.get("keywords") == ["{{keywords}}"]:
                m["keywords"] = prod["keywords"]
            text = json.dumps(m, indent=2, ensure_ascii=False) + "\n"
        p.write_text(render(text, prod, str(rel), quote=p.suffix in (".json", ".toml")), encoding="utf-8")
        n += 1
    return n


# ---- skills -> MCP prompt text -------------------------------------------------------------

def split_skill(text: str) -> tuple[dict[str, str], str]:
    """Frontmatter (flat `key: value` lines only, which is all the skills use) and body."""
    m = FRONTMATTER.match(text)
    if not m:
        raise AssembleError("SKILL.md must start with a --- frontmatter block")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if line.strip():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[m.end():].lstrip("\n")


def prompt_text(skill_body: str, prod: dict | None = None) -> str:
    """A skill body rewritten for hosts that have MCP prompts but no skills: no Claude Code
    substitutions, the contract pointed at the server instructions, the product id filled in."""
    prod = prod or load_product()
    t = skill_body
    t = t.replace("the coaching contract of the coach skill", "the coaching contract in the server instructions")
    t = t.replace("the coverage rules of the coach skill", "the coverage rule in the server instructions")
    t = t.replace("the connector rules of the coach skill", "the rule on the Founder's other tools in the server instructions")
    t = t.replace("the coach skill's stages reference", "the Stage list in coach_update_profile")
    t = re.sub(r'uvx --from "\$\{CLAUDE_SKILL_DIR\}/\.\./\.\." \{\{id\}\}', "{{id}}", t)
    t = t.replace("$ARGUMENTS", "{arguments}")
    t = re.sub(r"/\{\{id\}\}:([a-z-]+)", r"the \1 prompt", t)
    t = render(t, prod, "prompt text")
    left = re.search(r"\$\{[^}]*\}", t)
    if left:
        raise AssembleError(f"unsubstituted host variable left in prompt text: {left.group(0)}")
    return t


def generate_playbooks(root: Path, prod: dict | None = None, out_dir: Path | None = None) -> list[Path]:
    """The Pack's MCP prompts from its skills (the single source), into founder_coach/playbooks/ (the founder
    Pack, kept in the repo) or `out_dir` (another Pack's build)."""
    prod = prod or load_product(root)
    pack = prod["pack"]
    out_dir = out_dir or root / "founder_coach" / "playbooks"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.md"):
        if old.stem not in pack.prompts and old.read_text(encoding="utf-8").startswith("<!-- generated from"):
            old.unlink()                                 # another Pack's generated prompt: never served here
    written = []
    for name in pack.prompts:
        src = pack.skill_file(name, root)
        if not src.exists():
            raise AssembleError(f"missing skill {src}")
        _, body = split_skill(src.read_text(encoding="utf-8"))
        rel = src.relative_to(root).as_posix()
        header = f"<!-- generated from {rel} by scripts/assemble_plugin.py; edit the skill -->\n"
        dest = out_dir / f"{name}.md"
        new = header + prompt_text(body, prod)
        if not dest.exists() or dest.read_text(encoding="utf-8") != new:
            tmp = dest.with_suffix(".md.tmp")
            tmp.write_text(new, encoding="utf-8")
            os.replace(tmp, dest)
            written.append(dest)
    return written


# ---- build ---------------------------------------------------------------------------------

def _tree_hash(*paths: Path) -> str:
    h = hashlib.sha256()
    for base in paths:
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file()
                                                   and "__pycache__" not in p.parts and p.suffix != ".pyc")
        for p in files:
            h.update(str(p.relative_to(base.parent)).encode())
            h.update(p.read_bytes() if p.stat().st_size < 50_000_000 else str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()[:12]


def _copy_pack(pack: Path, dest: Path) -> None:
    """Only the pack file and its manifest: a pack folder from `ytbrain pack build` also holds
    the (large, build-only) embedding cache, which must not ship."""
    src = pack / PACK_FILE if pack.is_dir() else pack
    manifest = src.with_name(MANIFEST_FILE)
    for need in (src, manifest):
        if not need.exists():
            raise AssembleError(f"missing {need}: pass the folder `ytbrain pack build` wrote "
                                f"({PACK_FILE} + {MANIFEST_FILE})")
    dest.mkdir(parents=True)
    shutil.copy2(src, dest / PACK_FILE)
    shutil.copy2(manifest, dest / MANIFEST_FILE)


def _check(build: Path) -> None:
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "from founder_coach.pack import PackStore, find_pack;"
            "p = PackStore(find_pack(sys.argv[1] + '/pack'), verify=True);"
            "print('pack ok:', p.count(), 'items')")
    # -B: importing the runtime from the build must not leave __pycache__ in what ships
    try:
        r = subprocess.run([sys.executable, "-B", "-c", code, str(build)], capture_output=True, text=True,
                           timeout=300, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        raise AssembleError("the assembled plugin's check took over 5 minutes (a very slow disk?); "
                            "the previous build is untouched") from None
    if r.returncode != 0:
        raise AssembleError(f"the assembled plugin failed its check:\n{(r.stderr or r.stdout).strip()[-2000:]}")


def tools_list_from_golden(golden: Path) -> dict:
    """The server's tool schemas (the tests' snapshot) as a tools/list response, for the eval
    suite's mocked server: the model then sees the real tools, descriptions and hints."""
    camel = lambda k: re.sub(r"_([a-z])", lambda m: m.group(1).upper(), k)
    tools = []
    for t in json.loads(golden.read_text()):
        tool = {"name": t["name"], "description": t["description"], "inputSchema": t["input"]}
        if t.get("output"):
            tool["outputSchema"] = t["output"]
        if t.get("annotations"):
            tool["annotations"] = {camel(k): v for k, v in t["annotations"].items() if v is not None}
        tools.append(tool)
    return {"tools": tools}


def assemble(root: Path, pack: Path, out: Path, check: bool = False, with_evals: bool = False,
             pack_id: str | None = None) -> dict:
    root, out = root.resolve(), out.resolve()
    if not pack.exists():
        raise AssembleError(f"no Knowledge pack at {pack}; build it with `ytbrain pack build` or pass --pack")
    for need in ("plugin/.claude-plugin/plugin.json", "plugin/pyproject.toml", "founder_coach/__init__.py"):
        if not (root / need).exists():
            raise AssembleError(f"missing {need} under {root}")
    prod = load_product(root, pack_id, check_skills=True)
    the_pack = prod["pack"]
    default = the_pack.id == _packs().DEFAULT_PACK
    # the founder Pack's product.json and prompts live in the repo (the dev CLI and tests read them); another
    # Pack's are written into its build only
    regenerated = ((["product.json"] if sync_product_json(root, prod) else []) +
                   [p.name for p in generate_playbooks(root, prod)]) if default else []

    out.parent.mkdir(parents=True, exist_ok=True)
    build = out.with_name(f".{out.name}.building-{os.getpid()}")
    if build.exists():
        shutil.rmtree(build)
    try:
        # plugin/evals/ (the `claude plugin eval` suite) never ships to Founders; --with-evals
        # builds a separate copy to run it against
        ignore = IGNORE if with_evals else shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".DS_Store", ".pytest_cache", "evals")
        shutil.copytree(root / "plugin", build, ignore=ignore)
        if (build / "skills").exists():
            shutil.rmtree(build / "skills")              # the Pack's own skill list, from its sources
        for name in the_pack.skills:
            shutil.copytree(the_pack.skill_file(name, root).parent, build / "skills" / name, ignore=IGNORE)
        golden = root / "tests" / "golden" / "coach_tools.json"
        if with_evals and golden.exists() and (build / "evals").is_dir():
            mocks = build / "evals" / "mocks" / "coach"
            mocks.mkdir(parents=True, exist_ok=True)
            (mocks / "_tools.json").write_text(json.dumps(tools_list_from_golden(golden), indent=1) + "\n")
        render_tree(build, prod)
        shutil.copytree(root / "founder_coach", build / "founder_coach", ignore=IGNORE)
        if not default:
            (build / "founder_coach" / "product.json").write_text(product_json(prod), encoding="utf-8")
            generate_playbooks(root, prod, build / "founder_coach" / "playbooks")
        _copy_pack(pack, build / "pack")
        stamp = (f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-"
                 f"{_tree_hash(root / 'product.toml', the_pack.dir, root / 'plugin', root / 'founder_coach')}")
        (build / "BUILD_ID").write_text(stamp + "\n", encoding="utf-8")
        if check:
            _check(build)
        previous = out.with_name(out.name + ".previous")
        if previous.exists():
            shutil.rmtree(previous)
        if out.exists():
            os.replace(out, previous)
        os.replace(build, out)
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)
    return {"out": str(out), "build_id": stamp, "regenerated": regenerated, "id": prod["id"], "pack": the_pack.id}


def private_items(out: Path) -> int:
    """How many private items (your Books, ADR-0014) the assembled plugin's pack holds (0 if unknown)."""
    try:
        return int(json.loads((out / "pack" / MANIFEST_FILE).read_text(encoding="utf-8")).get("private_items") or 0)
    except (OSError, ValueError, AttributeError):
        return 0


def zip_plugin(out: Path) -> Path:
    """Package an assembled plugin as <id>-<version>.plugin next to it (a zip with .claude-plugin/ at its root),
    the file Cowork's Plugins page uploads. Written in place, so a mounted folder that forbids renames works too.
    A pack with private items is named <id>-<version>-private.plugin: it is for your own account only."""
    out = out.resolve()
    manifest = json.loads((out / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    tag = "-private" if private_items(out) else ""
    target = out.parent / f"{manifest['name']}-{manifest['version']}{tag}.plugin"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(out):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name not in (".DS_Store",) and not name.endswith(".pyc"):
                    f = Path(root) / name
                    z.write(f, f.relative_to(out).as_posix())
    return target


def _pack_env_name(root: Path = ROOT, pack_id: str | None = None) -> str:
    return load_product(root, pack_id)["id"].upper().replace("-", "_") + "_PACK"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--for", dest="pack_id", default=None)
    pack_id = pre.parse_known_args(argv)[0].pack_id
    try:
        env_pack = _pack_env_name(ROOT, pack_id)
    except AssembleError as e:
        print(f"assemble: {e}", file=sys.stderr)
        return 2
    ap.add_argument("--pack", type=Path, default=Path(os.environ[env_pack]) if os.environ.get(env_pack) else None,
                    help=f"the Knowledge pack (file or folder) from `ytbrain pack build` (default: ${env_pack})")
    ap.add_argument("--for", dest="pack_id", default=None, metavar="PACK",
                    help="the Pack to assemble (packs/<id>/pack.toml; default: founder)")
    ap.add_argument("--out", type=Path, default=None, help="default: dist/plugin for founder, dist/<product id> otherwise")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    ap.add_argument("--check", action="store_true", help="import the runtime and verify the pack before swapping in")
    ap.add_argument("--with-evals", action="store_true",
                    help="include plugin/evals/ (for `claude plugin eval`); use a separate --out, e.g. dist/plugin-eval")
    ap.add_argument("--zip", action="store_true",
                    help="also write dist/<id>-<version>.plugin, the file Cowork's Plugins page uploads")
    args = ap.parse_args(argv)
    if args.pack is None:
        print(f"assemble: pass --pack PATH (or set {env_pack})", file=sys.stderr)
        return 2
    if args.out is None:
        try:
            args.out = load_product(args.root, args.pack_id)["pack"].dist_dir(False, ROOT / "dist")
        except AssembleError as e:
            print(f"assemble: {e}", file=sys.stderr)
            return 2
    try:
        res = assemble(args.root, args.pack, args.out, check=args.check, with_evals=args.with_evals,
                       pack_id=args.pack_id)
    except AssembleError as e:
        print(f"assemble: {e}", file=sys.stderr)
        return 2
    regen = f"; regenerated {', '.join(res['regenerated'])}" if res["regenerated"] else ""
    q = shlex.quote(res["out"])                # paths with spaces must survive a copy-paste
    print(f"assemble: {res['out']} (build {res['build_id']}){regen}\n"
          f"  next: claude plugin validate {q} --strict && claude --plugin-dir {q}")
    private = private_items(Path(res["out"]))
    if private:
        print(f"  PRIVATE: the pack holds {private} private items (your Books): install it on your own account "
              f"only; never share or release it (scripts/release.py refuses it)")
    if args.zip:
        print(f"  cowork: {shlex.quote(str(zip_plugin(Path(res['out']))))} (upload in Customize > Plugins)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
