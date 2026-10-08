"""Projects and the Common profile (ADR-0016, M6e; acceptance C1-C5 and the G7 isolation cases): memory kept
per Project, a session that asks which one, sharing only on a yes, the one-time move of the old store, and the
command line. Each test gets a fresh machine (its own OS home and engine home): nothing touches ~/."""
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_coach import Clock, _Models, _pack, _skipped   # noqa: E402  (sets the hermetic environment first)
import test_pack  # noqa: E402,F401  (imported now: its import-time settings must not land inside a test machine)

from founder_coach import product                        # noqa: E402
from founder_coach.projects import ProjectError, Projects, Workspace, slug   # noqa: E402
from founder_coach.store import FounderStore             # noqa: E402

PINNED = (product.env_name("HOME"), product.env_name("PROJECT"), product.env_name("MODELS"))


@contextlib.contextmanager
def _machine():
    """A fresh machine: its own OS home (so ~/.founder-coach is a temp folder) and engine home, and no data
    folder pinned (Projects are in use)."""
    keys = ("HOME", "YTBRAIN_HOME", *PINNED)
    saved = {k: os.environ.get(k) for k in keys}
    user = Path(tempfile.mkdtemp(prefix="coach-machine-")) / "user"
    user.mkdir()
    os.environ["HOME"] = str(user)
    os.environ["YTBRAIN_HOME"] = str(user / ".ytbrain")
    for k in PINNED:
        os.environ.pop(k, None)
    try:
        yield user
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _serve(user: Path, fn, clock=None, home=None):
    """Run `fn(call)` against one server session; call(tool, args) -> (structured or None, text, is_error)."""
    try:
        import anyio
        from mcp import Client
    except ImportError:
        _skipped("optional dependency not installed")
        return False
    from founder_coach.server import create_server
    tmp = Path(tempfile.mkdtemp())
    srv = create_server(pack=_pack(tmp), home=home, clock=clock or Clock(), models=_Models(), start_models=False,
                        engine_home=user / ".ytbrain")

    async def main():
        async with Client(srv) as c:
            async def call(tool, args=None):
                r = await c.call_tool(tool, args or {})
                text = " ".join(getattr(x, "text", "") for x in r.content)
                return (None if r.is_error else r.structured_content), text, r.is_error
            await fn(call)
    anyio.run(main)
    return True


def _rid(n=[0]):                                         # noqa: B006 -- a counter on purpose
    n[0] += 1
    return f"req-{n[0]}"


# --- names --------------------------------------------------------------------------------------------------
def test_project_names_are_unique_ignoring_case_and_a_rename_keeps_the_folder():
    root = Path(tempfile.mkdtemp())
    ps = Projects(root)
    a = ps.create("Acme Inc.")
    assert a["id"] == "acme-inc" and (root / "projects" / "acme-inc" / "project.json").exists()
    for dup in ("acme inc.", "  ACME   Inc. "):
        try:
            ps.create(dup)
            raise AssertionError("a second Project with the same name")
        except ProjectError as e:
            assert "already a Project" in str(e)
    b = ps.create("Acme-Inc")                           # a different name with the same slug: a new folder
    assert b["id"] == "acme-inc-2"
    assert ps.get("ACME INC.")["id"] == "acme-inc" and ps.get("acme-inc-2")["name"] == "Acme-Inc"
    r = ps.rename("acme-inc", "Acme Robotics")
    assert r["id"] == "acme-inc" and ps.get("acme robotics")["id"] == "acme-inc"
    assert slug("Startup's Café!") == "startups-cafe" and slug("!!!") == "project"
    for bad in ("", " ", "x" * 81):
        try:
            ps.create(bad)
            raise AssertionError(f"accepted {bad!r}")
        except ProjectError:
            pass
    (root / "projects" / "acme-inc-2" / "project.json").write_text("{broken")
    assert [p["id"] for p in ps.list()] == ["acme-inc"], "a folder without a store or a label isn't a Project"
    FounderStore(root / "projects" / "acme-inc-2").close()
    assert {p["id"] for p in ps.list()} == {"acme-inc", "acme-inc-2"}, "a damaged label never hides a Project"


# --- C1 / G7: isolation --------------------------------------------------------------------------------------
def test_each_project_keeps_its_own_memory_and_a_session_asks_which_one():
    with _machine() as user:
        async def first(call):
            ctx, _, _ = await call("coach_get_context")
            assert ctx["project"] is None and [n["kind"] for n in ctx["nudges"]] == ["setup"]
            out, _, err = await call("coach_project", {"action": "create", "name": "Acme"})
            assert not err and out["active"] == {"id": "acme", "name": "Acme"}
            res, _, err = await call("coach_update_profile", {"changes": {"company": "Acme", "stage": "mvp"},
                                                              "request_id": _rid()})
            assert not err and res["saved_to"] == "Project Acme (acme)"
            await call("coach_record", {"entry": {"kind": "goal", "text": "10 design partners"}, "request_id": _rid()})
            await call("coach_record", {"entry": {"kind": "decision", "text": "Charge from day one",
                                                  "reasoning": "signal"}, "request_id": _rid()})
            out, _, _ = await call("coach_project", {"action": "create", "name": "Beta Labs"})
            assert out["active"]["id"] == "beta-labs" and len(out["projects"]) == 2
            ctx, text, _ = await call("coach_get_context")
            assert ctx["project"]["id"] == "beta-labs" and ctx["goals"] == [] and ctx["recent_decisions"] == []
            assert "company" not in ctx["profile"] and "design partners" not in text
            await call("coach_record", {"entry": {"kind": "goal", "text": "Ship v1"}, "request_id": _rid()})
            out, _, _ = await call("coach_project", {"action": "summary", "project": "acme"})
            assert out["summary"]["profile"]["company"] == "Acme"
            assert [g["text"] for g in out["summary"]["goals"]] == ["10 design partners"]
            ctx, _, _ = await call("coach_get_context")
            assert [g["text"] for g in ctx["goals"]] == ["Ship v1"], "a summary reads; it never copies"
            out, _, _ = await call("coach_project", {"action": "switch", "project": "acme"})
            ctx, _, _ = await call("coach_get_context")
            assert [g["text"] for g in ctx["goals"]] == ["10 design partners"] and ctx["profile"]["stage"] == "mvp"
            assert len(ctx["projects"]) == 2 and any(p.get("active") for p in ctx["projects"])
            _, text, err = await call("coach_project", {"action": "switch", "project": "gamma"})
            assert err and "no Project 'gamma'" in text
            st, _, _ = await call("coach_corpus_status")
            assert st["store"]["project"] == {"id": "acme", "name": "Acme"} and st["store"]["projects"] == 2
        assert _serve(user, first) is not False

        async def second(call):                          # a new session: two Projects, none chosen (R1)
            ctx, text, _ = await call("coach_get_context")
            assert ctx["project"] is None and ctx["goals"] == [] and ctx["profile"] == {}
            assert [n["kind"] for n in ctx["nudges"]] == ["choose_project"] and "beta-labs (Beta Labs)" in text
            _, text, err = await call("coach_record", {"entry": {"kind": "goal", "text": "x"}, "request_id": _rid()})
            assert err and "Which Project is this about?" in text and "nothing was read or saved" in text
            _, text, err = await call("coach_update_profile", {"changes": {"stage": "pmf"}, "request_id": _rid()})
            assert err and "Which Project" in text
            out, _, _ = await call("coach_project", {"action": "switch", "project": "BETA LABS"})
            assert out["active"]["id"] == "beta-labs"
            ctx, _, _ = await call("coach_get_context")
            assert [g["text"] for g in ctx["goals"]] == ["Ship v1"]
            out, _, _ = await call("coach_project", {"action": "rename", "name": "Beta"})
            assert out["active"] == {"id": "beta-labs", "name": "Beta"}
        _serve(user, second)
        root = user / ".ytbrain" / "founder"
        assert (root / "projects" / "acme" / "founder.db").exists() and (root / "projects" / "beta-labs" / "FOUNDER.md").exists()
        assert (root / "last-project").read_text() == "beta-labs"
        assert not (user / ".founder-coach").exists(), "nothing is written to the old data folder"


def test_the_first_save_makes_a_project_and_a_pinned_folder_has_none():
    with _machine() as user:
        async def go(call):
            res, _, err = await call("coach_update_profile", {"changes": {"stage": "idea"}, "request_id": _rid()})
            assert not err and res["saved_to"] == "Project My project (my-project)"
            out, _, _ = await call("coach_project", {"action": "list"})
            assert out["active"]["id"] == "my-project" and len(out["projects"]) == 1
        _serve(user, go)

        async def again(call):                           # one Project: chosen by itself next time
            ctx, _, _ = await call("coach_get_context")
            assert ctx["project"]["id"] == "my-project" and ctx["profile"]["stage"] == "idea" and ctx["projects"] is None
        _serve(user, again)

        pinned = Path(tempfile.mkdtemp())

        async def single(call):
            out, _, err = await call("coach_project", {"action": "create", "name": "X"})
            assert not err and out["mode"] == "single" and "Projects are off" in out["note"]
            res, _, _ = await call("coach_update_profile", {"changes": {"stage": "mvp"}, "request_id": _rid()})
            assert res["saved_to"] is None
            _, text, err = await call("coach_update_profile", {"changes": {"name": "Kay"}, "scope": "common",
                                                               "request_id": _rid()})
            assert err and "no Common profile" in text
            ctx, _, _ = await call("coach_get_context")
            assert ctx["project"] is None and ctx["profile"]["stage"] == "mvp"
        _serve(user, single, home=pinned)
        assert (pinned / "founder.db").exists() and not (pinned / "projects").exists()


# --- C2: the Common profile ----------------------------------------------------------------------------------
def test_the_common_profile_is_shared_only_on_a_yes_and_only_for_its_fields():
    with _machine() as user:
        async def go(call):
            await call("coach_project", {"action": "create", "name": "Acme"})
            await call("coach_update_profile", {"changes": {"name": "Kay", "company": "Acme"}, "request_id": _rid()})
            await call("coach_project", {"action": "create", "name": "Beta"})
            ctx, _, _ = await call("coach_get_context")
            assert "name" not in ctx["profile"], "a project-scope save stays in its Project"
            res, _, err = await call("coach_update_profile", {"changes": {"name": "Kay", "timezone": "Asia/Kolkata"},
                                                              "scope": "common", "request_id": _rid()})
            assert not err and res["saved_to"].startswith("Common profile") and res["profile"]["name"] == "Kay"
            ctx, _, _ = await call("coach_get_context")
            assert ctx["profile"]["name"] == "Kay" and sorted(ctx["shared_fields"]) == ["name", "timezone"]
            assert ctx["timezone"] == "Asia/Kolkata", "the shared timezone sets the Project's day and week"
            await call("coach_project", {"action": "switch", "project": "acme"})
            ctx, _, _ = await call("coach_get_context")
            assert ctx["profile"]["name"] == "Kay" and ctx["shared_fields"] == ["timezone"], \
                "the Project's own value stands until it is shared from there"
            await call("coach_update_profile", {"changes": {"name": "Kay R"}, "scope": "common", "request_id": _rid()})
            ctx, _, _ = await call("coach_get_context")
            assert ctx["profile"]["name"] == "Kay R" and "name" in ctx["shared_fields"], \
                "sharing from a Project ends its own older value"
            await call("coach_project", {"action": "switch", "project": "beta"})
            ctx, _, _ = await call("coach_get_context")
            assert ctx["profile"]["name"] == "Kay R"
            _, text, err = await call("coach_update_profile", {"changes": {"company": "Beta"}, "scope": "common",
                                                               "request_id": _rid()})
            assert err and "only timezone, name, role, answer_style" in text and "company" in text
            out, _, _ = await call("coach_project", {"action": "summary", "project": "acme"})
            assert "name" not in out["summary"]["profile"], "a summary shows the Project's own facts only"
        _serve(user, go)
        eng = user / ".ytbrain"
        assert (eng / "you.db").exists() and "Kay R" in (eng / "YOU.md").read_text()
        acme = FounderStore(eng / "founder" / "projects" / "acme")
        try:
            hist = [h["value"] for h in acme.profile_history("name")]
            assert hist == ["Kay"] and "name" not in acme._own_profile(), "kept in history, no longer current"
        finally:
            acme.close()


def test_a_pack_that_shares_nothing_never_reads_or_writes_the_common_profile():
    saved = product.PACK.get("common_fields")
    product.PACK["common_fields"] = []
    try:
        with _machine() as user:
            c = FounderStore(user / ".ytbrain", name="you")
            c.update_profile({"name": "Kay"})
            c.close()

            async def go(call):
                await call("coach_project", {"action": "create", "name": "Fund I"})
                ctx, _, _ = await call("coach_get_context")
                assert "name" not in ctx["profile"] and ctx["shared_fields"] is None
                _, text, err = await call("coach_update_profile", {"changes": {"name": "K"}, "scope": "common",
                                                                   "request_id": _rid()})
                assert err and "shares no profile fields" in text
            _serve(user, go)
    finally:
        product.PACK["common_fields"] = saved


# --- C3: the one-time move of the old store ------------------------------------------------------------------
def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_the_old_store_moves_into_a_project_once_and_is_left_as_it_was():
    with _machine() as user:
        old = user / ".founder-coach"
        s = FounderStore(old)
        s.update_profile({"company": "Acme", "stage": "mvp", "timezone": "Europe/Berlin"})
        s.record({"kind": "goal", "text": "10 design partners"})
        s.close()
        before = _digest(old / "founder.db")
        ws = Workspace.open()
        assert ws.migration["status"] == "migrated" and ws.migration["project"] == "acme"
        assert ws.migration["timezone_to_common"] == "Europe/Berlin"
        assert _digest(old / "founder.db") == before, "the old store is the backup: never changed"
        assert [p["name"] for p in ws.projects.list()] == ["Acme"] and ws.choose() == "acme"
        home = ws.store_home("acme")
        assert (home / "backups" / "founder-migrated.db").exists() and (home / "FOUNDER.md").exists()
        common = ws.open_common()
        store, err = ws.open_store("acme", common=common)
        try:
            assert err is None and [g["text"] for g in store.goals()] == ["10 design partners"]
            assert store.profile()["company"]["value"] == "Acme" and str(store.tz().key) == "Europe/Berlin"
            assert store.shared_fields() == ["timezone"] and "timezone" not in store._own_profile()
        finally:
            store.close()
            common.close()
        again = Workspace.open()
        assert again.migration is None and len(again.projects.list()) == 1, "once, however often it starts"
        import sqlite3
        a, b = sqlite3.connect(old / "founder.db"), sqlite3.connect(home / "founder.db")
        try:
            for t in ("goals", "commitments", "decisions", "checkins", "feedback", "profile_facts"):
                assert a.execute(f"SELECT COUNT(*) FROM {t}").fetchone() == b.execute(f"SELECT COUNT(*) FROM {t}").fetchone(), t
            log_a = a.execute("SELECT at, entity, entity_id, op FROM changes ORDER BY seq").fetchall()
            log_b = b.execute("SELECT at, entity, entity_id, op FROM changes ORDER BY seq").fetchall()
            assert log_b[:len(log_a)] == log_a and [r[3] for r in log_b[len(log_a):]] == ["move_to_common"]
        finally:
            a.close()
            b.close()


def test_a_damaged_or_busy_old_store_is_not_moved_and_is_tried_again():
    with _machine() as user:
        old = user / ".founder-coach"
        old.mkdir()
        (old / "founder.db").write_bytes(b"not a database at all" * 100)
        ws = Workspace.open()
        assert ws.migration["status"] == "failed" and ws.projects.list() == []
        assert not (ws.root / ".migrated.json").exists() and not (ws.root / ".migrating").exists()
        assert Workspace.open().migration["status"] == "failed", "retried at the next start"
        assert not list((ws.root / "projects").glob(".migrating-*")), "no half-made Project left behind"
    with _machine() as user:
        s = FounderStore(user / ".founder-coach")
        s.update_profile({"company": "Acme"})
        s.close()
        root = user / ".ytbrain" / "founder"
        root.mkdir(parents=True)
        (root / ".migrating").write_text("123")              # another process is moving it right now
        assert Workspace.open().migration == {"status": "busy"}
        assert not (root / "projects").exists()
        os.utime(root / ".migrating", (1, 1))               # that process died long ago: take over
        assert Workspace.open().migration["status"] == "migrated"


def test_a_pinned_data_folder_is_never_moved():
    with _machine() as user:
        s = FounderStore(user / ".founder-coach")
        s.update_profile({"company": "Acme"})
        s.close()
        ws = Workspace.open(user / ".founder-coach")
        assert ws.single and ws.migration is None and not (user / ".ytbrain").exists()


def test_the_models_stay_where_they_were_downloaded_until_the_shared_folder_exists():
    from founder_coach.models import models_dir
    with _machine() as user:
        old = user / ".founder-coach" / "models"
        old.mkdir(parents=True)
        (old / "a-model").write_text("x")
        assert models_dir() == old, "no second download after the update"
        (user / ".ytbrain" / "models").mkdir(parents=True)
        assert models_dir() == user / ".ytbrain" / "models"
    with _machine() as user:
        assert models_dir() == user / ".ytbrain" / "models"


# --- C5: the command line -------------------------------------------------------------------------------------
def _cli(user: Path, *args, input=None):
    env = {k: v for k, v in os.environ.items() if k not in PINNED}
    env.update(HOME=str(user), YTBRAIN_HOME=str(user / ".ytbrain"))
    return subprocess.run([sys.executable, "-m", "founder_coach.cli", *args], input=input, text=True,
                          capture_output=True, cwd=ROOT, timeout=60, env=env, check=False)


def test_the_command_line_works_on_one_project_at_a_time():
    with _machine() as user:
        assert _cli(user, "projects", "create", "Acme").returncode == 0
        r = _cli(user, "projects", "create", "Beta", "Labs")
        assert r.returncode == 0 and "beta-labs" in r.stdout
        r = _cli(user, "projects")
        assert "* beta-labs" in r.stdout and "  acme" in r.stdout
        s = FounderStore(user / ".ytbrain" / "founder" / "projects" / "acme")
        s.update_profile({"company": "Acme", "stage": "mvp"})
        s.close()
        st = json.loads(_cli(user, "status", "--json", "--project", "acme").stdout)
        assert "projects/acme/founder.db" in st["store"] and st["profile"]["company"] == "Acme"
        assert "acme (active)" in st["projects"]
        r = _cli(user, "export", "--project", "nope")
        assert r.returncode == 2 and "no Project 'nope'" in r.stderr
        hook = json.loads(_cli(user, "hook", "session-start", input="{}").stdout)["hookSpecificOutput"]
        assert "Project Beta Labs, the last one used" in hook["additionalContext"]
        assert _cli(user, "projects", "use", "acme").returncode == 0
        r = _cli(user, "export")
        assert r.returncode == 0 and "projects/acme/exports" in r.stdout
        r = _cli(user, "forget", "--all", "--confirm", "nope")
        assert r.returncode == 1 and "nothing deleted" in r.stdout
        r = _cli(user, "forget", "--all", "--confirm", "forget all")
        assert r.returncode == 0 and "2 Project(s)" in r.stdout
        st = json.loads(_cli(user, "status", "--json", "--project", "acme").stdout)
        assert st["profile"] == {}
        r = _cli(user, "projects", "--home", str(user / "pinned"))
        assert r.returncode == 0 and "Projects are off" in r.stdout


def test_the_hook_asks_which_project_when_several_and_none_was_used():
    with _machine() as user:
        root = user / ".ytbrain" / "founder"
        ps = Projects(root)
        ps.create("Acme")
        ps.create("Beta")
        out = json.loads(_cli(user, "hook", "session-start", input="{}").stdout)["hookSpecificOutput"]
        assert "2 Projects (Acme, Beta)" in out["additionalContext"] and "ask which Project" in out["additionalContext"]
        assert not (root / "projects" / "acme" / "founder.db").exists(), "the hook reads nothing it wasn't told to"


def test_a_save_proposed_for_one_project_is_never_written_to_another():
    with _machine() as user:
        async def go(call):
            await call("coach_project", {"action": "create", "name": "Acme"})
            await call("coach_project", {"action": "create", "name": "Beta"})        # switched before the yes
            _, text, err = await call("coach_record", {"entry": {"kind": "goal", "text": "10 clinics"},
                                                       "request_id": _rid(), "project": "acme"})
            assert err and "proposed for Project Acme (acme), but the active Project is Beta (beta)" in text
            _, text, err = await call("coach_update_profile", {"changes": {"stage": "mvp"}, "project": "nope",
                                                               "request_id": _rid()})
            assert err and "no Project 'nope'" in text
            res, _, err = await call("coach_record", {"entry": {"kind": "goal", "text": "Ship v1"},
                                                      "request_id": _rid(), "project": "beta"})
            assert not err and res["ids"]
            ctx, _, _ = await call("coach_get_context")
            _, _, err = await call("coach_update", {"id": ctx["goals"][0]["id"], "status": "met", "project": "acme",
                                                    "request_id": _rid()})
            assert err
            out, _, _ = await call("coach_project", {"action": "summary", "project": "acme"})
            assert out["summary"]["goals"] == [], "nothing reached Acme"
        _serve(user, go)


def test_forgetting_one_project_leaves_the_others_byte_identical():
    with _machine() as user:
        root = user / ".ytbrain" / "founder"
        ps = Projects(root)
        homes = {}
        for name in ("Acme", "Beta"):
            homes[name] = ps.store_home(ps.create(name)["id"])
            s = FounderStore(homes[name])
            s.update_profile({"company": name, "stage": "mvp"})
            s.record({"kind": "goal", "text": f"{name} goal"})
            s.close()
        before = {f.relative_to(homes["Beta"]): _digest(f) for f in homes["Beta"].rglob("*") if f.is_file()}
        r = _cli(user, "forget", "--project", "acme", "--confirm", "Acme")
        assert r.returncode == 0, r.stderr
        r = _cli(user, "export", "--project", "acme", "--out", str(user / "out"))
        data = json.loads((user / "out" / "founder-export.json").read_text())
        assert data["goals"] == [] and data["profile"] == {}
        after = {f.relative_to(homes["Beta"]): _digest(f) for f in homes["Beta"].rglob("*") if f.is_file()}
        assert after == before, "another Project's files are untouched"


def test_two_coaches_write_the_common_profile_at_once():
    with _machine() as user:
        eng = user / ".ytbrain"
        code = ("import sys; from pathlib import Path; from founder_coach.store import FounderStore\n"
                "s = FounderStore(Path(sys.argv[1]), name='you')\n"
                "for i in range(25):\n"
                "    s.update_profile({'answer_style': f'{sys.argv[2]} {i}'}, request_id=f'{sys.argv[2]}-{i}')\n"
                "s.close()\n")
        env = {k: v for k, v in os.environ.items() if k not in PINNED}
        procs = [subprocess.Popen([sys.executable, "-c", code, str(eng), who], cwd=ROOT, env=env,
                                  stderr=subprocess.PIPE, text=True) for who in ("founder", "coding")]
        errs = [p.communicate(timeout=120)[1] for p in procs]
        assert all(p.returncode == 0 for p in procs), errs
        s = FounderStore(eng, name="you")
        try:
            assert len(s.profile_history("answer_style")) == 50 and s.check()["ok"]
        finally:
            s.close()



# --- G7: the coach evaluation's grading (offline) ------------------------------------------------------------
def test_g7_cases_are_well_formed_and_graded_on_every_projects_store():
    from ytbrain.eval import coach as C
    cases = C.load_cases("g7")
    assert len(cases) >= 2 and all(c["id"].startswith("g7-") for c in cases)
    for c in cases:
        turns = sum(len(x["turns"]) for x in c["sessions"])
        assert all(1 <= t["turn"] <= turns for t in c.get("turn_checks", [])), c["id"]
        assert len(c["sessions"]) >= 2, "isolation needs a new session"
    with _machine() as user:
        eng = user / ".ytbrain"
        ps = Projects(eng / "founder")
        for name, stage, goal in (("Acme Dental", "mvp", "10 paying clinics by 2026-12-15"),
                                  ("Kitebook", "idea", "50 waitlist signups by 2026-11-30")):
            s = FounderStore(ps.store_home(ps.create(name)["id"]))
            s.update_profile({"company": name, "stage": stage})
            s.record({"kind": "goal", "text": goal})
            s.close()
        c = FounderStore(eng, name="you")
        c.update_profile({"name": "Kay"})
        c.close()
        two = next(x for x in cases if x["id"] == "g7-two-companies-stay-apart")
        got = C.check_projects(eng, two["checks"], "2026-10-09T18:00:00+05:30")
        assert all(x["ok"] for x in got), got
        shared = next(x for x in cases if x["id"] == "g7-name-shared-only-on-a-yes")
        assert all(x["ok"] for x in C.check_projects(eng, shared["checks"], "2026-10-06T10:00:00+05:30"))
        s = FounderStore(ps.store_home("acme-dental"))
        s.record({"kind": "goal", "text": "500 waitlist signups"})      # Kitebook's fact in Acme's memory
        s.close()
        bad = {x["company"]: x["ok"] for x in C.check_projects(eng, two["checks"], "2026-10-09T18:00:00+05:30")
               if x["check"] == "goals"}
        assert bad == {"Acme Dental": False, "Kitebook": True}
    log = [{"coach": "Which one: Acme Dental or Kitebook?", "writes": [], "calls": []},
           {"coach": "Acme has 10 paying clinics as its goal.", "writes": [], "calls": []},
           {"coach": "Saved.", "writes": ["coach_update_profile"],
            "calls": [{"tool": "coach_update_profile", "input": {"changes": {"name": "Kay"}, "scope": "common"}}]}]
    ask = {"turn": 1, "asks_project": True, "names": ["Acme", "Kitebook"], "not_mentions": ["clinics"],
           "no_memory_writes": True}
    assert C.turn_check(log, ask)["ok"]
    assert not C.turn_check(log, {**ask, "turn": 2})["ok"], "answering from one Project without asking fails"
    assert not C.turn_check(log, {"turn": 3, "no_common_write": True})["ok"]
    assert not C.turn_check(log, {"turn": 9, "mentions": ["x"]})["ok"], "a turn that never ran fails"


if __name__ == "__main__":
    import inspect
    import logging
    import traceback
    logging.disable(logging.WARNING)            # the SDK logs every expected tool error to stderr
    fns = [f for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        try:
            f()
            print(f"  PASS  {f.__name__}")
        except Exception as e:                   # noqa: BLE001 -- report every test, not just the first
            failed += 1
            print(f"  FAIL  {f.__name__}: {type(e).__name__}: {str(e)[:300]}")
            if os.environ.get("VERBOSE"):
                traceback.print_exc()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
