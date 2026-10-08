"""Plugin (M3b) checks, offline: manifest, MCP config, hook, skills, prompt sync, assembler.

Run: python tests/test_plugin.py   (or pytest)
The host itself is checked on the maintainer's machine:
    claude plugin validate dist/plugin --strict && claude --plugin-dir dist/plugin
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
os.environ["YTBRAIN_DOTENV"] = "0"          # hermetic: never read the developer's .env (keys, backend)
os.environ.setdefault("YTBRAIN_SOURCES_FILE", os.path.join(__import__("tempfile").mkdtemp(prefix="ytbrain-nosources-"), "sources.yaml"))   # hermetic: never read your sources.yaml (the file does not exist)
import re
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import assemble_plugin as A  # noqa: E402

PLUGIN = ROOT / "plugin"
SKILLS = PLUGIN / "skills"
TOOLS = ["coach_search", "coach_read", "coach_get_context", "coach_update_profile",
         "coach_record", "coach_update", "coach_corpus_status"]
# Agent Skills standard keys plus the three shared by Claude Code, Cursor and VS Code (phase3 §11.6)
ALLOWED_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools",
                "argument-hint", "disable-model-invocation", "user-invocable"}
# who may invoke each skill (phase3 §11.6, m3-status decision 2)
INVOCATION = {
    "coach": {"user-invocable": "false"},
    "ask": {}, "weekly-focus": {}, "check-in": {}, "project": {},
    "setup": {"disable-model-invocation": "true"},
    "status": {"disable-model-invocation": "true"},
    "export": {"disable-model-invocation": "true"},
    "forget": {"disable-model-invocation": "true"},
    "feedback": {"disable-model-invocation": "true"},
}



def _skipped(why: str) -> None:
    """A test that can't run here says so; CI sets REQUIRE_ALL_TESTS=1 and fails instead."""
    if os.environ.get("REQUIRE_ALL_TESTS") == "1":
        raise AssertionError(f"skipped in CI: {why}")
    print(f"    (skipped: {why})")

def _skills() -> dict[str, tuple[dict, str]]:
    return {d.name: A.split_skill((d / "SKILL.md").read_text(encoding="utf-8"))
            for d in sorted(SKILLS.iterdir()) if d.is_dir()}


PRODUCT = A.load_product(ROOT)


def test_manifest_takes_its_name_from_product_toml_and_matches_the_runtime_version():
    m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert m["name"] == "{{id}}" and m["displayName"] == "{{display_name}}" and m["keywords"] == ["{{keywords}}"]
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", PRODUCT["id"])
    py = tomllib.loads((PLUGIN / "pyproject.toml").read_text())
    assert m["version"] == py["project"]["version"], "bump plugin.json and pyproject.toml together"
    assert py["project"]["name"] == "{{id}}" and py["project"]["scripts"]["{{id}}"] == "founder_coach.cli:main"
    keys = [k for entry in py["tool"]["uv"]["cache-keys"] for k in entry.values()]
    assert "BUILD_ID" in keys and "pyproject.toml" in keys


def test_mcp_server_starts_from_the_plugin_with_its_pack():
    cfg = json.loads((PLUGIN / ".mcp.json").read_text())
    assert list(cfg["mcpServers"]) == ["coach"]
    s = cfg["mcpServers"]["coach"]
    assert s["command"] == "uvx"
    args = s["args"]
    assert args[:2] == ["--from", "${CLAUDE_PLUGIN_ROOT}"] and args[2:4] == ["{{id}}", "serve"]
    assert args[args.index("--pack") + 1] == "${CLAUDE_PLUGIN_ROOT}/pack"


def test_session_start_hook_is_quick_local_and_fails_open():
    h = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    assert set(h["hooks"]) == {"SessionStart"}, "no per-prompt or Stop hooks (phase3 §11.7)"
    (entry,) = h["hooks"]["SessionStart"]
    assert set(entry["matcher"].split("|")) <= {"startup", "resume", "clear", "compact", "fork"}
    (hook,) = entry["hooks"]
    assert hook["type"] == "command" and hook["timeout"] <= 10
    assert hook["command"].endswith("{{id}} hook session-start")
    assert "${CLAUDE_PLUGIN_ROOT}" in hook["command"]


def test_every_skill_has_valid_frontmatter_and_the_agreed_invocation():
    skills = _skills()
    assert set(skills) == set(INVOCATION), f"skills changed: {sorted(skills)}"
    for name, (meta, body) in skills.items():
        assert meta.get("name") == name, name
        assert set(meta) <= ALLOWED_KEYS, (name, set(meta) - ALLOWED_KEYS)
        d = meta["description"]
        assert 40 < len(d) < 1024 and "Use when" in d, name
        shouted = set(re.findall(r"\b[A-Z]{4,}\b", d)) - {"JSON", "YAML"}
        assert not shouted, f"{name}: no capitalised emphasis {shouted}"
        for key in ("user-invocable", "disable-model-invocation"):
            assert meta.get(key) == INVOCATION[name].get(key), (name, key, meta.get(key))
        assert len(body.splitlines()) < 500, name


def test_skills_name_only_real_tools_and_real_files():
    known = set(TOOLS)
    golden = ROOT / "tests" / "golden" / "coach_tools.json"
    if golden.exists():                                   # the server's own snapshot, when in the repo
        data = json.loads(golden.read_text())
        items = data if isinstance(data, list) else data.get("tools", [])
        known = {t["name"] for t in items} or known
    for d in SKILLS.iterdir():
        for f in d.rglob("*.md"):
            text = f.read_text(encoding="utf-8")
            for tool in set(re.findall(r"\bcoach_[a-z_]+\b", text)):
                assert tool in known, f"{f.relative_to(ROOT)} names unknown tool {tool}"
            for link in re.findall(r"\]\(([^)#:]+)\)", text):
                assert (f.parent / link).exists(), f"{f.relative_to(ROOT)} links to missing {link}"


# who may invoke each Pack-specific skill (packs/<id>/skills/): the same rules as the shared ones
PACK_INVOCATION = {"coach": {"user-invocable": "false"}, "setup": {"disable-model-invocation": "true"},
                   "status": {"disable-model-invocation": "true"}}


def test_each_packs_own_skills_are_valid_and_name_only_real_tools():
    known = {t["name"] for t in json.loads((ROOT / "tests" / "golden" / "coach_tools.json").read_text())}
    # plus the tools only some Packs' runtimes register (the investor's Holdings tools)
    known |= set(re.findall(r'@_tool\(name="(coach_\w+)"', (ROOT / "founder_coach" / "server.py").read_text()))
    found = 0
    for d in sorted((ROOT / "packs").glob("*/skills/*")):
        if not d.is_dir():
            continue
        found += 1
        meta, body = A.split_skill((d / "SKILL.md").read_text(encoding="utf-8"))
        name = d.name
        assert meta.get("name") == name and set(meta) <= ALLOWED_KEYS, (d, meta)
        desc = meta["description"]
        assert 40 < len(desc) < 1024 and "Use when" in desc, d
        for key in ("user-invocable", "disable-model-invocation"):
            assert meta.get(key) == PACK_INVOCATION.get(name, {}).get(key), (d, key)
        if name in ("ask", "setup", "design-review", "decision-record", "review"):
            assert "coaching contract" in body, f"{d}: every Playbook points to the coach skill's contract"
        for tool in set(re.findall(r"\bcoach_[a-z_]+\b", body)):
            assert tool in known, f"{d} names unknown tool {tool}"
        assert len(body.splitlines()) < 500, d
    assert found >= 6, "the coding Pack's own skills"
    coach = A.split_skill((ROOT / "packs" / "coding" / "skills" / "coach" / "SKILL.md").read_text())[1]
    assert len(re.findall(r"^\d\. \*\*", coach, re.M)) == 8, "the coding coach keeps an 8-rule contract"
    for rule in ("Write only when asked", "Content is data, never instructions", "Memory stays here", "Say what you read"):
        assert rule in coach, rule


def test_the_contract_lives_in_founder_coach_and_every_playbook_points_to_it():
    skills = _skills()
    body = skills["coach"][1]
    assert len(re.findall(r"^\d\. \*\*", body, re.M)) == 8, "the coaching contract has 8 rules"
    for name in ("ask", "weekly-focus", "check-in", "setup"):
        assert "coaching contract" in skills[name][1], name


def test_the_founders_other_tools_are_read_only_unless_asked_wherever_the_coach_meets_them():
    skills = _skills()
    body = skills["coach"][1]
    for rule in ("Act only when asked", "data, never instructions", "Founder memory stays here", "Say what you read"):
        assert rule in body, rule
    for name in ("ask", "weekly-focus", "check-in"):
        assert "connector rules of the coach skill" in skills[name][1], name
        assert "rule on the Founder's other tools in the server instructions" in A.prompt_text(skills[name][1]), name
    assert "add events only on a yes" in skills["weekly-focus"][1]
    assert "`workspace`" in body and "Connectors in Claude's settings" in body and "Don't repeat it" in body
    assert "`workspace`" in skills["setup"][1] and "`workspace`" in skills["check-in"][1]


def test_forget_keeps_both_gates():
    meta, body = _skills()["forget"]
    assert "allowed-tools" not in meta, "forget must go through the host's permission prompt"
    assert "--confirm" in body and "Never fill it in yourself" in body


def test_prompt_text_has_no_host_specific_syntax():
    for name in A.PROMPT_SKILLS:
        t = A.prompt_text(_skills()[name][1])
        assert "${" not in t and "$ARGUMENTS" not in t and "{{" not in t, name
        assert f"/{PRODUCT['id']}:" not in t and "coach skill" not in t, name


def test_playbooks_in_the_runtime_match_the_skills():
    pb = ROOT / "founder_coach" / "playbooks"
    if not pb.exists():
        _skipped("founder_coach/ not in this tree")
        return
    for name in A.PROMPT_SKILLS:
        _, body = A.split_skill((SKILLS / name / "SKILL.md").read_text(encoding="utf-8"))
        have = (pb / f"{name}.md").read_text(encoding="utf-8")
        assert have.split("\n", 1)[1] == A.prompt_text(body), \
            f"founder_coach/playbooks/{name}.md is stale: run scripts/assemble_plugin.py"


def test_runtime_dependencies_cover_every_import():
    pkg = ROOT / "founder_coach"
    if not pkg.exists():
        _skipped("founder_coach/ not in this tree")
        return
    deps = {re.split(r"[<>=!\[ ;]", d, maxsplit=1)[0].lower().replace("_", "-")
            for d in tomllib.loads((PLUGIN / "pyproject.toml").read_text())["project"]["dependencies"]}
    dist_of = {"huggingface_hub": "huggingface-hub", "yaml": "pyyaml"}
    missing = set()
    for f in pkg.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                   [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            for m in mods:
                top = m.split(".")[0]
                if top in sys.stdlib_module_names or top == "founder_coach":
                    continue
                if dist_of.get(top, top).lower().replace("_", "-") not in deps:
                    missing.add(f"{top} ({f.name})")
    assert not missing, f"imported but not in plugin/pyproject.toml: {sorted(missing)}"
    assert not any(m.startswith(("ytbrain", "torch")) for m in missing), "ADR-0010"


def _fake_pack(tmp: Path) -> Path:
    pack = tmp / "pack"
    pack.mkdir()
    (pack / "knowledge.sqlite").write_bytes(b"sqlite")
    (pack / "pack.json").write_text("{}")
    return pack


def _fake_repo(tmp: Path) -> Path:
    import shutil
    repo = tmp / "repo"
    shutil.copytree(PLUGIN, repo / "plugin")
    shutil.copy2(ROOT / "product.toml", repo / "product.toml")
    shutil.copytree(ROOT / "packs", repo / "packs")
    (repo / "founder_coach" / "__pycache__").mkdir(parents=True)
    (repo / "founder_coach" / "__init__.py").write_text("")
    (repo / "founder_coach" / "__pycache__" / "x.pyc").write_bytes(b"0")
    return repo


def test_a_pack_definition_is_checked_and_says_where_its_builds_go():
    sys.path.insert(0, str(ROOT))
    from ytbrain import packs as PK
    founder = PK.load("founder", ROOT, known_domains=["startup", "gtm", "leadership", "finance", "coding"])
    assert founder.product_id == PRODUCT["id"] and set(founder.prompts) <= set(founder.skills)
    assert founder.modules == ("goals", "commitments", "checkins", "decisions")
    data, dist = Path("/d"), Path("/dist")
    assert founder.pack_dir(False, data) == data / "pack" and founder.pack_dir(True, data) == data / "pack-private"
    assert founder.dist_dir(True, dist) == dist / "plugin-private", "the founder Pack keeps today's folders"
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        (root / "plugin" / "skills" / "ask").mkdir(parents=True)
        (root / "plugin" / "skills" / "ask" / "SKILL.md").write_text("---\nname: ask\n---\nx\n")
        (root / "packs" / "demo").mkdir(parents=True)
        base = ('[pack]\nid = "demo"\ndomains = ["coding"]\nskills = ["ask"]\n{extra}'
                '[product]\nid = "demo-coach"\ndisplay_name = "Demo"\ndescription = "d"\n')
        for extra, why in (('modules = ["goals", "habits"]\n', "unknown module"),
                           ('prompts = ["greet"]\n', "prompts must be skills"),
                           ('builds = ["public"]\n', "builds are")):
            (root / "packs" / "demo" / "pack.toml").write_text(base.format(extra=extra))
            try:
                PK.load("demo", root)
                raise AssertionError(why)
            except PK.PackError as e:
                assert why in str(e), (why, str(e))
        (root / "packs" / "demo" / "pack.toml").write_text(base.format(extra=""))
        try:
            PK.load("demo", root, known_domains=["startup"])
            raise AssertionError("an undeclared Domain")
        except PK.PackError as e:
            assert "not in domains.yaml" in str(e)
        demo = PK.load("demo", root, known_domains=["coding"])
        assert demo.builds == ("private",) and demo.modules == ()
        assert demo.pack_dir(True, data) == data / "packs" / "demo" / "pack-private"
        assert demo.dist_dir(True, dist) == dist / "demo-coach-private" and PK.all_ids(root) == ["demo"]


def test_another_pack_assembles_its_own_skills_identity_and_runtime_settings():
    """One plugin per Pack (ADR-0016): its skills (its own folder first), its words, its prompts and its Pack
    settings reach its build only; the founder Pack's files in the repo are untouched."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = _fake_repo(tmp)
        pack = _fake_pack(tmp)
        demo = repo / "packs" / "demo"
        (demo / "skills" / "greet").mkdir(parents=True)
        (demo / "skills" / "greet" / "SKILL.md").write_text(
            "---\nname: greet\ndescription: Greets the {{person}}. Use when they say hi.\n---\n"
            "Say hello to the {{person}} and offer /{{id}}:ask.\n")
        (demo / "pack.toml").write_text(
            '[pack]\nid = "demo"\ndomains = ["coding", "system-design"]\nmodules = ["decisions"]\n'
            'skills = ["ask", "status", "greet"]\nprompts = ["greet"]\n'
            '[product]\nid = "demo-coach"\ndisplay_name = "Demo Coach"\ndescription = "A demo."\nkeywords = ["demo"]\n'
            '[words]\nperson = "Engineer"\n')
        founder_before = sorted(p.name for p in (repo / "founder_coach").rglob("*") if p.is_file())
        out = tmp / "dist" / "demo-coach"
        r = A.assemble(repo, pack, out, pack_id="demo")
        assert r["id"] == "demo-coach" and r["pack"] == "demo" and r["regenerated"] == []
        assert sorted(p.name for p in (out / "skills").iterdir()) == ["ask", "greet", "status"]
        greet = (out / "skills" / "greet" / "SKILL.md").read_text()
        assert "Greets the Engineer" in greet and "/demo-coach:ask" in greet
        pj = json.loads((out / "founder_coach" / "product.json").read_text())
        assert pj["id"] == "demo-coach" and pj["pack"] == {"id": "demo", "domains": ["coding", "system-design"],
                                                           "modules": ["decisions"], "common_fields": [],
                                                           "prompts": ["greet"]}
        plays = sorted(p.name for p in (out / "founder_coach" / "playbooks").glob("*.md"))
        assert "greet.md" in plays and "ask.md" not in plays, plays
        assert json.loads((out / ".claude-plugin" / "plugin.json").read_text())["name"] == "demo-coach"
        assert sorted(p.name for p in (repo / "founder_coach").rglob("*") if p.is_file()) == founder_before


def test_assembler_builds_swaps_and_keeps_the_previous_build():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = _fake_repo(tmp)
        pack = _fake_pack(tmp)
        (pack / "embed-cache.db").write_bytes(b"build-only cache")
        out = tmp / "dist" / "plugin"
        r1 = A.assemble(repo, pack, out)
        assert sorted(r1["regenerated"]) == sorted(["product.json"] + [f"{n}.md" for n in A.PROMPT_SKILLS])
        for need in (".claude-plugin/plugin.json", ".mcp.json", "hooks/hooks.json", "pyproject.toml",
                     "skills/ask/SKILL.md", "founder_coach/playbooks/ask.md", "pack/knowledge.sqlite",
                     "pack/pack.json", "BUILD_ID"):
            assert (out / need).exists(), need
        assert not list(out.rglob("__pycache__")), "caches never ship"
        assert (repo / "plugin" / "evals").is_dir() and not (out / "evals").exists(), "the eval suite never ships"
        ev = A.assemble(repo, pack, tmp / "dist" / "plugin-eval", with_evals=True)
        assert (Path(ev["out"]) / "evals" / "mocks" / "coach" / "coach_search.md").exists()
        assert not (out / "pack" / "embed-cache.db").exists(), "the embedding cache never ships"
        assert (out / "BUILD_ID").read_text().strip() == r1["build_id"]
        (out / "marker").write_text("first")
        r2 = A.assemble(repo, pack, out)
        assert r2["regenerated"] == [], "unchanged skills rewrite nothing"
        shipped = [p for p in out.rglob("*") if p.is_file() and p.suffix in A.RENDERED
                   and p.relative_to(out).parts[0] not in ("founder_coach", "pack")]
        left = [str(p.relative_to(out)) for p in shipped if "{{" in p.read_text()]
        assert shipped and not left, f"placeholders left in the build: {left}"
        m = json.loads((out / ".claude-plugin" / "plugin.json").read_text())
        assert m["name"] == PRODUCT["id"] and m["keywords"] == PRODUCT["keywords"]
        assert f"/{PRODUCT['id']}:check-in" in (out / "skills" / "setup" / "SKILL.md").read_text()
        assert PRODUCT["id"] in tomllib.loads((out / "pyproject.toml").read_text())["project"]["scripts"]
        ev_out = Path(ev["out"])
        assert f"mcp__plugin_{PRODUCT['id']}_coach__coach_search" in \
            (ev_out / "evals" / "ask-triggers-on-startup-question" / "graders" / "searched.md").read_text()
        assert (out.with_name("plugin.previous") / "marker").exists() and not (out / "marker").exists()
        assert not [p for p in out.parent.iterdir() if p.name.startswith(".plugin.building")]


def test_zip_packages_the_shipped_plugin_for_cowork():
    import zipfile
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, pack = _fake_repo(tmp), _fake_pack(tmp)
        (pack / "embed-cache.db").write_bytes(b"build-only cache")
        out = tmp / "dist" / "plugin"
        A.assemble(repo, pack, out)
        (out / "founder_coach" / "stray.pyc").write_bytes(b"0")
        (out / ".DS_Store").write_bytes(b"0")
        z = A.zip_plugin(out)
        m = json.loads((out / ".claude-plugin" / "plugin.json").read_text())
        assert z == (tmp / "dist" / f"{m['name']}-{m['version']}.plugin").resolve()
        names = zipfile.ZipFile(z).namelist()
        for need in (".claude-plugin/plugin.json", ".mcp.json", "skills/setup/SKILL.md", "pack/knowledge.sqlite", "BUILD_ID"):
            assert need in names, need
        assert not [n for n in names if n.endswith((".pyc", ".DS_Store")) or "embed-cache" in n or n.startswith("evals/")]
        assert zipfile.ZipFile(z).testzip() is None
        assert A.zip_plugin(out) == z, "re-zipping overwrites the same file"
        (out / "pack" / "pack.json").write_text(json.dumps({"private_items": 12}))
        assert A.private_items(out) == 12
        assert A.zip_plugin(out).name == f"{m['name']}-{m['version']}-private.plugin", "a private pack says so"


def test_assembler_refuses_a_missing_pack_and_a_failed_check_without_touching_the_last_build():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = _fake_repo(tmp)
        out = tmp / "dist" / "plugin"
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()) as err:        # the refusal is expected: keep the run quiet
            assert A.main(["--root", str(repo), "--pack", str(tmp / "nope"), "--out", str(out)]) == 2
        assert "no Knowledge pack" in err.getvalue()
        assert not out.exists()
        pack = _fake_pack(tmp) / "knowledge.sqlite"             # a pack given as its file
        A.assemble(repo, pack, out)
        (out / "marker").write_text("good build")
        try:
            A.assemble(repo, pack, out, check=True)      # the fake runtime has no pack module
        except A.AssembleError as e:
            assert "failed its check" in str(e)
        else:
            raise AssertionError("check should fail on the fake runtime")
        assert (out / "marker").read_text() == "good build", "a failed build never replaces a working one"
        assert not [p for p in out.parent.iterdir() if p.name.startswith(".plugin.building")]


def test_assembler_check_opens_a_real_pack_with_the_real_runtime_and_refuses_a_tampered_one():
    try:
        import numpy  # noqa: F401  -- the runtime's own dependency
        sys.path.insert(0, str(ROOT / "tests"))
        from test_pack import HashEmbed, _build
    except ImportError:
        _skipped("runtime or pipeline dependencies not installed")
        return
    import shutil
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = tmp / "repo"
        shutil.copytree(PLUGIN, repo / "plugin")
        shutil.copy2(ROOT / "product.toml", repo / "product.toml")
        shutil.copytree(ROOT / "packs", repo / "packs")
        shutil.copytree(ROOT / "founder_coach", repo / "founder_coach", ignore=A.IGNORE)
        pack = tmp / "pack"
        _build(pack, emb=HashEmbed())
        out = tmp / "dist" / "plugin"
        A.assemble(repo, pack, out, check=True)
        assert (out / "pack" / "knowledge.sqlite").exists()
        assert not list(out.rglob("__pycache__")), "the check must not leave caches in the build"
        manifest = pack / "pack.json"
        data = json.loads(manifest.read_text())
        data["sha256"] = "0" * 64
        manifest.write_text(json.dumps(data))
        try:
            A.assemble(repo, pack, out, check=True)
        except A.AssembleError as e:
            assert "corrupt or was changed" in str(e), str(e)
        else:
            raise AssertionError("a pack failing its checksum was assembled")
        assert (out / "pack" / "pack.json").read_text() != manifest.read_text(), "the good build stays"


def test_a_pack_without_its_manifest_is_refused():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = _fake_repo(tmp)
        pack = _fake_pack(tmp)
        (pack / "pack.json").unlink()
        try:
            A.assemble(repo, pack, tmp / "dist" / "plugin")
        except A.AssembleError as e:
            assert "pack.json" in str(e)
        else:
            raise AssertionError("assembled without a manifest")


def test_the_product_id_lives_only_in_product_toml():
    """ADR-0012: renaming is one edit to product.toml plus a rebuild. So nothing that ships spells
    the id out: plugin/ uses {{id}}, the runtime reads founder_coach/product.json."""
    pid, env = PRODUCT["id"], PRODUCT["id"].upper().replace("-", "_") + "_"
    for f in PLUGIN.rglob("*"):
        if f.is_file() and f.suffix in A.RENDERED:
            text = f.read_text(encoding="utf-8")
            assert pid not in text and env not in text, f"{f.relative_to(ROOT)} spells out the id: use {{{{id}}}}"
    import io, tokenize
    for f in (ROOT / "founder_coach").rglob("*.py"):
        toks = list(tokenize.generate_tokens(io.StringIO(f.read_text(encoding="utf-8")).readline))
        for i, tok in enumerate(toks):
            if tok.type != tokenize.STRING or (pid not in tok.string and env not in tok.string):
                continue
            prev = next((t for t in reversed(toks[:i]) if t.type not in (tokenize.NL, tokenize.COMMENT)), None)
            docstring = prev is None or prev.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
            assert docstring, f"{f.relative_to(ROOT)}:{tok.start[0]} spells out the id: use founder_coach.product"


def test_product_json_and_the_dev_cli_match_product_toml():
    have = (ROOT / "founder_coach" / "product.json").read_text(encoding="utf-8")
    assert have == A.product_json(PRODUCT), "founder_coach/product.json is stale: run scripts/assemble_plugin.py"
    root_py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    if "scripts" in root_py.get("project", {}):
        assert PRODUCT["id"] in root_py["project"]["scripts"], "rename the dev CLI in pyproject.toml too"
        assert "product.json" in " ".join(root_py["tool"]["setuptools"]["package-data"]["founder_coach"])


def test_product_toml_and_pack_toml_are_checked_and_unknown_placeholders_never_ship():
    """product.toml holds what every Pack shares; each Pack's identity is in packs/<id>/pack.toml (ADR-0016)."""
    pack_ok = ('[pack]\nid = "founder"\ndomains = ["startup"]\nskills = ["ask"]\n'
               '[product]\nid = "{pid}"\ndisplay_name = "x"\ndescription = "x"\n')
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        (root / "packs" / "founder").mkdir(parents=True)
        (root / "plugin" / "skills" / "ask").mkdir(parents=True)
        (root / "plugin" / "skills" / "ask" / "SKILL.md").write_text("---\nname: ask\n---\nx\n")
        shared = '[product]\nauthor = "x"\nlicense = "x"\n'
        for product, pack, why in ((shared, pack_ok.format(pid="Bad Name"), "lowercase"),
                                   ('[product]\nid = "ok"\nauthor = "x"\nlicense = "x"\n', pack_ok.format(pid="ok"),
                                    "moved to packs"),
                                   ("not toml [", pack_ok.format(pid="ok"), "not valid TOML"),
                                   (shared, pack_ok.format(pid="ok").replace('skills = ["ask"]', 'skills = ["nope"]'),
                                    "no source for skill"),
                                   (shared, pack_ok.format(pid="ok").replace('id = "founder"', 'id = "other"'),
                                    "must be 'founder'")):
            (root / "product.toml").write_text(product)
            (root / "packs" / "founder" / "pack.toml").write_text(pack)
            try:
                A.load_product(root, check_skills=True)
            except A.AssembleError as e:
                assert why in str(e), (why, str(e))
            else:
                raise AssertionError(f"accepted a bad product.toml or pack.toml ({why})")
        (root / "product.toml").write_text(shared)
        (root / "packs" / "founder" / "pack.toml").write_text(pack_ok.format(pid="ok"))
        assert A.load_product(root)["id"] == "ok"
    assert A.render('{"d": "{{description}}"}', {**PRODUCT, "description": 'say "hi"'}, quote=True) == '{"d": "say \\"hi\\""}'
    try:
        A.render("run /{{idd}}:ask", PRODUCT)
    except A.AssembleError as e:
        assert "unknown placeholder" in str(e)
    else:
        raise AssertionError("an unknown placeholder must stop the build")


def test_release_commits_tags_refuses_a_released_version_and_maps_a_renamed_id():
    try:
        import numpy  # noqa: F401  -- release always runs the assembler's --check on the real runtime
        sys.path.insert(0, str(ROOT / "tests"))
        from test_pack import HashEmbed, _build
    except ImportError:
        _skipped("runtime or pipeline dependencies not installed")
        return
    import os, shutil, subprocess
    import release as R
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com"}
    old_env = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo = tmp / "repo"
            shutil.copytree(PLUGIN, repo / "plugin")
            shutil.copy2(ROOT / "product.toml", repo / "product.toml")
            shutil.copytree(ROOT / "packs", repo / "packs")
            shutil.copytree(ROOT / "founder_coach", repo / "founder_coach", ignore=A.IGNORE)
            cur = R.current_version(repo)       # the repo's own version moves with every `ytbrain ops plugin`: start this copy at 0.1.0
            for path in R.version_files(repo).values():
                path.write_text(path.read_text(encoding="utf-8").replace(f'"{cur}"', '"0.1.0"'), encoding="utf-8")
            assert R.current_version(repo) == "0.1.0"
            pack = tmp / "pack"
            _build(pack, emb=HashEmbed())
            mk = tmp / "marketplace"
            subprocess.run(["git", "init", "-q", "-b", "main", str(mk)], check=True)
            say = lambda m: None
            r1 = R.release(repo, pack, mk, validate=False, say=say)
            pid = PRODUCT["id"]
            assert r1["tag"] == f"{pid}--v0.1.0"
            m = json.loads((mk / ".claude-plugin" / "marketplace.json").read_text())
            assert m["plugins"][0]["name"] == pid and m["plugins"][0]["source"] == f"./plugins/{pid}"
            assert "version" not in m["plugins"][0], "plugin.json is the one place for the version"
            assert json.loads((mk / "plugins" / pid / ".claude-plugin" / "plugin.json").read_text())["name"] == pid
            assert f"/plugin install {pid}@" in (mk / "README.md").read_text()
            assert pid in subprocess.run(["git", "-C", str(mk), "tag"], capture_output=True, text=True).stdout
            try:
                R.release(repo, pack, mk, validate=False, say=say)
            except R.ReleaseError as e:
                assert "already released" in str(e)
            else:
                raise AssertionError("released the same version twice")
            try:
                R.release(repo, pack, mk, version="0.0.9", validate=False, say=say)
            except R.ReleaseError as e:
                assert "must be higher" in str(e)
            else:
                raise AssertionError("accepted a lower version")
            assert R.current_version(repo) == "0.1.0", "a failed release puts the version files back"
            head = subprocess.run(["git", "-C", str(mk), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
            real_validate = R._validate
            R._validate = lambda m: (_ for _ in ()).throw(R.ReleaseError("claude plugin validate failed: bad entry"))
            try:
                R.release(repo, pack, mk, version="0.1.5", validate=True, say=say)
            except R.ReleaseError as e:
                assert "validate failed" in str(e)
            else:
                raise AssertionError("released despite a failed validation")
            finally:
                R._validate = real_validate
            st = subprocess.run(["git", "-C", str(mk), "status", "--porcelain"], capture_output=True, text=True).stdout
            assert st == "" and head == subprocess.run(["git", "-C", str(mk), "rev-parse", "HEAD"],
                                                      capture_output=True, text=True).stdout, "the clone is put back"
            assert R.current_version(repo) == "0.1.0", "and so are the version files"
            subprocess.run(["git", "-C", str(mk), "remote", "add", "origin", str(tmp / "nowhere.git")], check=True)
            r15 = R.release(repo, pack, mk, version="0.1.5", push=True, validate=False, say=say)
            assert not r15["pushed"] and any("push failed" in w for w in r15["warnings"]), "committed, push reported"
            assert "0.1.5" in subprocess.run(["git", "-C", str(mk), "tag"], capture_output=True, text=True).stdout
            pt = repo / "packs" / "founder" / "pack.toml"
            pt.write_text(pt.read_text().replace(f'id = "{pid}"', 'id = "seedcoach"'))
            r2 = R.release(repo, pack, mk, version="0.2.0", validate=False, say=say)
            m = json.loads((mk / ".claude-plugin" / "marketplace.json").read_text())
            assert r2["tag"] == "seedcoach--v0.2.0" and m["renames"] == {pid: "seedcoach"}
            assert not (mk / "plugins" / pid).exists() and (mk / "plugins" / "seedcoach").is_dir()
            assert '"version": "0.2.0"' in (repo / "plugin" / ".claude-plugin" / "plugin.json").read_text()
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_a_skill_without_frontmatter_is_rejected():
    try:
        A.split_skill("# no frontmatter\n")
    except A.AssembleError:
        return
    raise AssertionError("expected AssembleError")


def test_eval_mocks_carry_the_real_tool_schemas():
    """The mocked server shows the model the real tools: plugin/evals/mocks/coach/_tools.json
    equals the server's schema snapshot (the assembler rewrites it in every --with-evals build)."""
    want = A.tools_list_from_golden(ROOT / "tests" / "golden" / "coach_tools.json")
    have = json.loads((PLUGIN / "evals" / "mocks" / "coach" / "_tools.json").read_text())
    assert have == want, "stale: run scripts/assemble_plugin.py --with-evals, or copy its _tools.json"
    assert all("readOnlyHint" in t.get("annotations", {}) for t in have["tools"])

def test_the_env_prefix_placeholder_matches_the_runtime():
    """Skills name runtime settings as {{env_prefix}}USAGE=0: the same prefix the runtime reads."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import assemble_plugin as A
    from founder_coach import product
    prod = A.load_product(ROOT)
    assert A.render("{{env_prefix}}USAGE=0", prod) == product.ENV_PREFIX + "USAGE=0"
    assert A.render("{{env_prefix}}X", {**prod, "id": "acme-coach"}) == "ACME_COACH_X"



def test_the_founder_profile_lives_in_its_pack_and_the_runtime_keeps_one_copy_in_step():
    """packs/founder/pack.toml [[profile]] is what product.json carries; founder_coach.domain keeps the same
    list for a product.json from before Packs, and the engine's Common-profile fields agree on both sides."""
    from founder_coach import domain as D
    from ytbrain import packs
    p = packs.load("founder", check_skills=False)
    assert {f["name"]: (f["kind"], f["description"]) for f in p.profile} == D.FOUNDER_PROFILE
    assert tuple(f["name"] for f in p.profile if not f["stale"]) == D.FOUNDER_NEVER_STALE
    assert p.required == ("company", "stage") and packs.COMMON_FIELDS == D.COMMON_FIELDS
    for bad, why in (('[[profile]]\nname = "x"\nkind = "colour"\ndescription = "d"\n', "kind is one of"),
                     ('[[profile]]\nname = "x"\nkind = "enum"\ndescription = "d"\n', "needs `values`"),
                     ('[[profile]]\nname = "X y"\ndescription = "d"\n', "unique lower_case"),
                     ('[runtime]\ncolour = "x"\n', "unknown key")):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "packs" / "x").mkdir(parents=True)
            (root / "packs" / "x" / "pack.toml").write_text(
                '[pack]\nid = "x"\ndomains = ["coding"]\nskills = ["ask"]\n[product]\nid = "x-coach"\n'
                'display_name = "X"\ndescription = "d"\n' + bad)
            try:
                packs.load("x", root=root, check_skills=False)
                raise AssertionError(f"accepted: {bad}")
            except packs.PackError as e:
                assert why in str(e), (why, str(e))


def test_every_module_sentence_a_pack_may_rewrite_is_in_the_runtimes_text():
    """MODULE_TEXT rewrites exact sentences of the tool texts for a Pack without a memory module, in order: for every
    Pack (packs/*/pack.toml), each rule that applies to it must find its sentence, so a wording change in the runtime
    can't silently leave a coach mentioning a record it can't save."""
    try:
        from founder_coach.server import MODULE_TEXT, create_server
    except ImportError:
        return _skipped("optional dependency not installed")
    sys.path.insert(0, str(ROOT))
    from ytbrain import packs as PK
    srv = create_server(pack=Path(tempfile.mkdtemp()) / "none", home=Path(tempfile.mkdtemp()), start_models=False)
    texts = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("title", "description") and isinstance(v, str):
                    texts.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for t in srv._tool_manager._tools.values():
        texts.extend([t.title or "", t.description or ""])
        walk(t.parameters)
    used = set()
    for pid in PK.all_ids():
        modules = set(PK.load(pid, check_skills=False).modules)
        blob = "\n\x00\n".join(texts)
        for i, (module, old, new) in enumerate(MODULE_TEXT):
            if module in modules:
                continue
            assert old in blob, (pid, module, old)
            blob = blob.replace(old, new)
            used.add(i)
    assert used == set(range(len(MODULE_TEXT))), "a rule no Pack needs is dead text"


_DUMP_TOOLS = """
import json, sys, anyio
sys.path.insert(0, ".")
from founder_coach import domain as D, nudges as N, product
from founder_coach.server import create_server
from founder_coach.store import FounderStore
from mcp import Client
srv = create_server(pack="pack", home=sys.argv[1], start_models=False)
async def main():
    async with Client(srv) as c:
        init = await c.list_tools()
        prompts = (await c.list_prompts()).prompts
        tools = [t.model_dump(mode="json") for t in init.tools]
        s = FounderStore(sys.argv[1])
        hook = N.hook_text(s)
        s.close()
        print(json.dumps({"tools": tools, "prompts": [p.name for p in prompts], "hook": hook,
                          "instructions": srv._lowlevel_server.instructions, "id": product.ID,
                          "required": list(D.REQUIRED), "fields": list(D.PROFILE_FIELDS)}))
anyio.run(main)
"""


def _dump_runtime(build: Path, home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_HOME")}
    out = subprocess.run([sys.executable, "-c", _DUMP_TOOLS, str(home)], cwd=build, capture_output=True, text=True,
                         timeout=120, env={**env, "YTBRAIN_DOTENV": "0"}, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.splitlines()[-1])


def test_the_coding_pack_builds_a_plugin_that_speaks_its_own_words():
    """M6f: the coding plugin's tools, prompts, hook and instructions carry no founder vocabulary, its record kinds
    are its modules', its skills come from its own folder first, and every phrase it rewrites exists in the
    shared runtime's text (so a rewording there can't silently leave a founder phrase in the coding coach)."""
    try:
        import numpy  # noqa: F401
        import mcp  # noqa: F401
        sys.path.insert(0, str(ROOT / "tests"))
        from test_pack import HashEmbed, _build
    except ImportError:
        return _skipped("runtime or pipeline dependencies not installed")
    import re
    import shutil
    sys.path.insert(0, str(ROOT))
    from ytbrain import packs as PK
    coding = PK.load("coding", ROOT)
    assert coding.product_id == "coding-coach" and coding.modules == ("goals", "decisions")
    assert coding.builds == ("private",) and set(coding.prompts) <= set(coding.skills)
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = tmp / "repo"
        shutil.copytree(PLUGIN, repo / "plugin")
        shutil.copy2(ROOT / "product.toml", repo / "product.toml")
        shutil.copytree(ROOT / "packs", repo / "packs")
        shutil.copytree(ROOT / "founder_coach", repo / "founder_coach", ignore=A.IGNORE)
        pack = tmp / "pack"
        _build(pack, emb=HashEmbed())
        out = tmp / "coding-coach"
        r = A.assemble(repo, pack, out, pack_id="coding", check=True)
        assert r["id"] == "coding-coach"
        skills = sorted(p.name for p in (out / "skills").iterdir())
        assert skills == sorted(coding.skills), skills
        own = (out / "skills" / "coach" / "SKILL.md").read_text()
        assert "Coding coach" in own and "{{" not in own, "the Pack's own coach skill, rendered"
        for f in (out / "skills").rglob("SKILL.md"):
            text = f.read_text()
            assert not re.search(r"\bFounder|founder coach|Stage\b|Check-in|Commitment", text.split("---", 2)[2]), \
                (f.parent.name, re.findall(r".{40}(?:Founder|Stage|Check-in|Commitment).{20}", text)[:3])
        got = _dump_runtime(out, tmp / "home")
        blob = json.dumps(got["tools"]) + got["instructions"] + got["hook"]
        for word in ("Founder", "YC talk", "Commitment", "Check-in", "company", "commitments"):
            assert word not in blob, (word, re.findall(".{60}" + word + ".{30}", blob)[:2])
        assert got["prompts"] == ["ask", "setup", "design-review", "decision-record"]
        assert got["required"] == ["system", "stack"] and "lifecycle" in got["fields"] and "stage" not in got["fields"]
        record = next(x for x in got["tools"] if x["name"] == "coach_record")
        assert sorted(record["input_schema"]["$defs"]) == ["DecisionIn", "GoalIn"], "only the Pack's record kinds"
        assert got["hook"].startswith("Coding coach: No system profile yet")
        founder = _dump_runtime(ROOT, tmp / "home-founder")            # the shared runtime as written
        text = json.dumps(founder["tools"]) + founder["instructions"] + founder["hook"]
        for phrase in coding.runtime["replace"]:
            assert phrase in text or json.dumps(phrase)[1:-1] in text, f"[runtime.replace] {phrase!r} isn't in the runtime"



def test_the_investor_pack_builds_a_plugin_with_holdings_tools_and_no_founder_words():
    """M6g: the investor plugin registers coach_holdings, coach_review and coach_split (no other plugin does), offers
    only Decisions as records, shares nothing with the Common profile (C3) and speaks its own words."""
    try:
        import numpy  # noqa: F401
        import mcp  # noqa: F401
        sys.path.insert(0, str(ROOT / "tests"))
        from test_pack import HashEmbed, _build
    except ImportError:
        return _skipped("runtime or pipeline dependencies not installed")
    import shutil
    sys.path.insert(0, str(ROOT))
    from ytbrain import packs as PK
    inv = PK.load("investor", ROOT)
    assert inv.product_id == "investor-coach" and inv.modules == ("holdings", "decisions") and inv.common_fields == ()
    assert inv.builds == ("private",) and inv.required == ("goal", "targets")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = tmp / "repo"
        shutil.copytree(PLUGIN, repo / "plugin")
        shutil.copy2(ROOT / "product.toml", repo / "product.toml")
        shutil.copytree(ROOT / "packs", repo / "packs")
        shutil.copytree(ROOT / "founder_coach", repo / "founder_coach", ignore=A.IGNORE)
        pack = tmp / "pack"
        _build(pack, emb=HashEmbed())
        out = tmp / "investor-coach"
        A.assemble(repo, pack, out, pack_id="investor", check=True)
        assert sorted(p.name for p in (out / "skills").iterdir()) == sorted(inv.skills)
        got = _dump_runtime(out, tmp / "home")
        names = [x["name"] for x in got["tools"]]
        assert {"coach_holdings", "coach_review", "coach_split"} <= set(names)
        blob = json.dumps(got["tools"]) + got["instructions"] + got["hook"]
        for word in ("Founder", "YC talk", "Commitment", "Check-in", "commitments", "company", "Goal,"):
            assert word not in blob, (word, re.findall(".{60}" + word + ".{30}", blob)[:2])
        record = next(x for x in got["tools"] if x["name"] == "coach_record")
        assert record["input_schema"]["properties"]["entry"].get("$ref", "").endswith("DecisionIn") or \
            "DecisionIn" in json.dumps(record["input_schema"]), "Decisions only"
        assert "GoalIn" not in json.dumps(record["input_schema"])
        assert got["prompts"] == ["ask", "setup", "review"] and got["required"] == ["goal", "targets"]
        assert "targets" in got["fields"] and "company" not in got["fields"]
        assert got["hook"].startswith("Investor coach: No Investment Policy Statement yet")
        sys.path.insert(0, str(ROOT / "tests"))
        from test_invest import VANGUARD
        csv_file = tmp / "ira.csv"
        csv_file.write_text(VANGUARD)
        env = {k: v for k, v in os.environ.items() if not k.endswith("_HOME")}
        env.update(YTBRAIN_DOTENV="0", INVESTOR_COACH_HOME=str(tmp / "cli-home"))
        cli = lambda *a: subprocess.run([sys.executable, "-m", "founder_coach.cli", *a], cwd=out, env=env,   # noqa: E731
                                        capture_output=True, text=True, timeout=60, check=False)
        r = cli("holdings", "import", str(csv_file), "--as-of", "2026-10-04")
        assert r.returncode == 0 and "imported 12345678 as of 2026-10-04: 3 position(s), $42,500.50" in r.stdout, r
        assert "no asset class yet for: BND, VTI" in r.stdout
        assert cli("holdings", "label", "BND=bonds", "VTI=us_equity").returncode == 0
        r = cli("holdings")
        assert "12345678" in r.stdout and "$42,500.50" in r.stdout
        r = cli("holdings", "import", str(csv_file))
        assert r.returncode == 1 and "pass as_of" in r.stderr
        founder = subprocess.run([sys.executable, "-m", "founder_coach.cli", "holdings", "--home", str(tmp / "f")],
                                 cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False)
        assert founder.returncode == 2 and "keeps no Holdings" in founder.stderr
        founder = _dump_runtime(ROOT, tmp / "home-founder")
        assert not {"coach_holdings", "coach_review", "coach_split"} & {x["name"] for x in founder["tools"]}, \
            "the founder coach's tools are unchanged"
        text = json.dumps(founder["tools"]) + founder["instructions"] + founder["hook"]
        for phrase in inv.runtime["replace"]:
            assert phrase in text or json.dumps(phrase)[1:-1] in text, f"[runtime.replace] {phrase!r} isn't in the runtime"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:                                   # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
