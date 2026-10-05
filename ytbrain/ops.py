"""`ytbrain ops`: the maintainer's whole loop as one resumable command (docs/ops.md).

    ytbrain ops            # = all: ingest, then eval when the index changed, then the plugin when it passed
    ytbrain ops ingest     # sync -> clean -> extract -> verify -> one retry of newly flagged -> index
    ytbrain ops eval       # questions top-up -> search -> judge -> gate -> baseline
    ytbrain ops plugin     # packs -> assembled plugins -> validate (`--coach`: then the coach eval, before a release)

Every step is an existing `ytbrain` command run as a child process, so its output, its own resume
and its own lock are unchanged. The runner adds three things:

  - **Checkpoints.** After each step it records the step in data/ops/state.json; an interrupted or
    stopped run resumes at the first step not done (`--restart` starts over).
  - **Change detection.** Fingerprints of the index (every indexed Document's input), the labels and
    the plugin's inputs decide what is due: eval runs only when the index or labels changed since the
    last eval, the plugin only when the index or the plugin's code changed. The coach eval runs only
    with `--coach` (before a release), and then only the gates whose inputs changed (`eval coach` caches each
    gate on what it depends on). Sources are always synced (that is how new talks, pages and books are found).
  - **Gates and a spend cap.** A FAIL or INCONCLUSIVE verdict stops before the plugin; a new
    baseline is saved only after a PASS. `--max-cost` caps the eval and coach-judge spend of this
    run (each paid child gets what is left). Extraction is billed by your LLM endpoint and isn't
    metered here: the runner prints how many Documents it will extract (`--max-extract` limits them).
The scheduled `ytbrain run` never writes eval questions; `ops` does, because you asked it to.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import runstatus
from .config import DATA, EVAL_DATA, EVAL_DIR, EVAL_MAX_COST, EVAL_PRIVATE, MANIFEST_DB
from .pages import atomic_write_text

CODE = Path(__file__).resolve().parents[1]          # the repo: scripts/, plugin/, founder_coach/, dist/
STATE = DATA / "ops" / "state.json"
LAST_RUN = DATA / "ops" / "last-run.md"
VERSION_WAIT_S = 60
DOMAIN_WAIT_S = 300                 # a new Domain needs a moment of thought (risk tier, description)
COACH_ONLY = "the coach eval hasn't run on this build"
OPS_LOCK = DATA / "ops" / "ops.lock"
PLANS = ("all", "ingest", "eval", "plugin")
BASELINE = "full"                                    # the configuration every change is gated on
EXIT_FAIL, EXIT_STOPPED, EXIT_INCONCLUSIVE = 1, 2, 3
NETWORK_RETRY_S = (60, 300)                         # a network stop is retried twice: after 1, then 5 minutes
# a crash, not a request to stop: SIGINT/SIGTERM (Ctrl+C, kill) always stop the run
CRASH_SIGNALS = tuple(int(s) for s in (signal.SIGABRT, signal.SIGSEGV, getattr(signal, "SIGBUS", signal.SIGSEGV)))
DATED_KEEP = 10                                      # dated references kept (baseline-all-full-<date>.json)
FIX = {"budget": "the spend cap is used up: re-run with a higher --max-cost",
       "endpoint": "the LLM endpoint refused (key, credits, model or quota in .env): fix it, then re-run",
       "network": "the network kept failing: check the connection, then re-run",
       "plan_limit": "your Claude plan's usage limit: the coach eval resumes on the next run after the reset",
       "auth": "the Claude host is not signed in (its login expired): run `claude`, type /login, then re-run the same `ytbrain ops` command, which resumes at the coach eval",
       "interrupted": "interrupted: re-run to continue",
       "books": "a PDF was refused or failed to parse (see the sync output): fix or `skip: true` it, then re-run",
       "config": "a configuration problem (see the warning above). A book folder that is not a Domain: `ytbrain domains add NAME --risk ... --description ...` or `ytbrain domains ignore FOLDER` (ops asks when you're at the terminal), then re-run",
       "gate": "the regression gate: read the per-kind lines (`ytbrain eval rescore --config full --compare full`)",
       "unknown": "see the output above"}


@dataclass
class Options:
    plan: str = "all"
    max_cost: float = EVAL_MAX_COST
    max_extract: int = 0
    sync: bool = True
    coach: bool = False             # the coach eval (paid in plan usage): asked for with --coach, before a release
    restart: bool = False
    force: bool = False
    dry_run: bool = False
    notify: bool = True
    version: str | None = None       # patch | minor | skip: answer the version question up front


@dataclass
class Step:
    name: str                       # "<plan>:<step>", the checkpoint key
    argv: list[str] | None = None   # a ytbrain command (or a script, see `script`)
    script: bool = False
    paid: str | None = None         # the eval db spend key this step bills to (for --max-cost)
    run: Callable[[], int] | None = None    # in-process steps (bookkeeping, gates)
    note: str = ""
    # (run start time) -> None when the step's output is complete and was written by this run, else why not.
    # Asked only when the child dies on a crash signal: native libraries (ONNX, PyTorch) can abort while Python
    # shuts down, after every file is written, and the output decides whether that was harmless.
    check: Callable[[float], str | None] | None = None


@dataclass
class Ops:
    """One `ytbrain ops` run. `call` runs a child command and returns its exit code (tests fake it)."""
    opts: Options
    call: Callable[[list[str]], int] | None = None
    say: Callable[[str], None] = print
    state: dict = field(default_factory=dict)
    sleep: Callable[[float], None] = time.sleep
    ask: Callable[[str, float], str | None] | None = None    # (prompt, timeout) -> answer; None: no one there
    stray: Callable[[], dict[str, int]] | None = None        # book folders that aren't Domains (tests fake it)
    last_reason: str = ""
    last_detail: str = ""

    # ------------------------------------------------------------------ state
    def load(self) -> None:
        try:
            self.state = json.loads(STATE.read_text())
        except (OSError, ValueError):
            self.state = {}

    def save(self) -> None:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(STATE, json.dumps(self.state, indent=1, sort_keys=True))

    # ------------------------------------------------------------------ notifications
    def notify(self, title: str, message: str, log: bool = True) -> None:
        """A macOS notification (scheduled or unattended runs too) and data/ops/last-run.md."""
        if log:
            LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(LAST_RUN, f"# {title}\n\n{_now()}\n\n{message}\n")
        if self.opts.notify and self.call is None and shutil.which("osascript"):
            script = f"display notification {json.dumps(message[:240])} with title {json.dumps(title)}"
            try:
                subprocess.run(["osascript", "-e", script], timeout=10, check=False, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                pass

    # ------------------------------------------------------------------ fingerprints
    @staticmethod
    def index_fp() -> str:
        """Every indexed Document with the input it was indexed from: changes when anything is
        added, removed, re-extracted or re-chunked."""
        import sqlite3
        if not MANIFEST_DB.exists():
            return ""
        db = sqlite3.connect(str(MANIFEST_DB))
        rows = db.execute("SELECT doc_id, input_hash, version FROM stage_state WHERE stage='index' AND status='ok' "
                          "ORDER BY doc_id").fetchall()
        db.close()
        return _sha("".join(f"{d}\t{h}\t{v}\n" for d, h, v in rows))

    @staticmethod
    def labels_fp() -> str:
        files = [EVAL_DIR / "VERSION", *sorted((EVAL_PRIVATE / "trec").glob("*.qrels"))]
        return _sha("".join(p.read_text() for p in files if p.exists()))

    @staticmethod
    def plugin_fp(index: str) -> str:
        parts = [index]
        for base in ("plugin", "founder_coach"):
            for p in sorted((CODE / base).rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith("."):
                    parts.append(f"{p.relative_to(CODE)}:{hashlib.sha256(p.read_bytes()).hexdigest()}")
        for p in (CODE / "scripts" / "assemble_plugin.py", CODE / "ytbrain" / "pack.py"):
            if p.exists():
                parts.append(hashlib.sha256(p.read_bytes()).hexdigest())
        return _sha("\n".join(parts))

    # ------------------------------------------------------------------ helpers
    def spent_total(self) -> float:
        from .eval.db import EvalDB
        return EvalDB(EVAL_DATA / "eval.db").spent()

    def spent(self, key: str) -> float:
        from .eval.db import EvalDB
        return EvalDB(EVAL_DATA / "eval.db").spent(key)

    def remaining(self) -> float:
        return max(0.0, self.opts.max_cost - (self.spent_total() - self.state["run"]["spent_at_start"]))

    @staticmethod
    def splits_with_documents() -> set[str]:
        """The Tuning splits some indexed Document can write questions for (no build for an empty one)."""
        import sqlite3

        from . import source_kinds
        from .eval.splits import PRIVATE_SPLIT
        if not MANIFEST_DB.exists():
            return set()
        db = sqlite3.connect(str(MANIFEST_DB))
        docs = [r[0] for r in db.execute("SELECT doc_id FROM stage_state WHERE stage='index' AND status='ok'")]
        db.close()
        try:
            from .cli import _visibility
            vis = _visibility()
        except (Exception, SystemExit):  # noqa: BLE001 -- no sources.yaml (the CLI exits, which is not an Exception): everything public
            from .visibility import Visibility
            vis = Visibility.everything_public()
        return {PRIVATE_SPLIT if vis.is_private(d) else source_kinds.for_doc(d).eval_split for d in docs}

    @staticmethod
    def has_private() -> bool:
        try:
            from .cli import _visibility
            return bool(_visibility().private_docs)
        except (Exception, SystemExit):  # noqa: BLE001 -- no sources.yaml yet (the CLI exits): nothing private
            return False

    def child(self, step: Step) -> int:
        argv = list(step.argv or [])
        if step.paid:
            cap = self.spent(step.paid) + self.remaining()
            if self.remaining() <= 0:
                self.say(f"ops: the spend cap (${self.opts.max_cost:g}) is used up; re-run with a higher --max-cost")
                return EXIT_STOPPED
            argv += ["--max-cost", f"{cap:.4f}"]
        cmd = [sys.executable, str(CODE / argv[0]), *argv[1:]] if step.script else \
            [sys.executable, "-m", "ytbrain.cli", *argv]
        for attempt, wait in enumerate((*NETWORK_RETRY_S, None)):
            runstatus.clear()
            started = time.time()
            code = self.call(argv if not step.script else ["script", *argv]) if self.call is not None \
                else subprocess.call(cmd, cwd=str(CODE))
            if code and step.check is not None and crash_signal(code):
                why = step.check(started)
                if why is None:
                    self.say(f"ops: {step.name} crashed on exit ({crash_signal(code)}) after writing its output; "
                             "the output verifies, so the run goes on")
                    code = 0
                else:
                    self.say(f"ops: {step.name} crashed ({crash_signal(code)}) and its output doesn't verify: {why}")
            if code == 0:
                self.last_reason = ""
                return 0
            stop = runstatus.read() or {}
            self.last_reason = stop.get("reason") or ("network" if argv[:1] == ["sync"] else "unknown")
            self.last_detail = stop.get("detail", "")
            if self.last_reason != "network" or wait is None:
                return code
            self.say(f"ops: {' '.join(argv[:3])} stopped on a network problem ({self.last_detail[:120]}); "
                     f"retry {attempt + 1} of {len(NETWORK_RETRY_S)} in {wait // 60:g} min")
            self.sleep(wait)
        return code

    # ------------------------------------------------------------------ plans
    def ingest_steps(self) -> list[Step]:
        steps = []
        if self.opts.sync:
            steps.append(Step("ingest:sync", ["sync", "--strict-domains"]))
        steps += [Step("ingest:clean", ["clean"]),
                  Step("ingest:extract", ["extract", *(["--limit", str(self.opts.max_extract)]
                                                         if self.opts.max_extract else [])],
                       note="LLM calls billed by your endpoint"),
                  Step("ingest:verify", ["verify"]),
                  Step("ingest:retry-flagged", run=self.retry_flagged,
                       note="extract + verify once more, for Documents verify newly flagged"),
                  Step("ingest:index", ["index"])]
        return steps

    def retry_flagged(self) -> int:
        """Re-extract, once each, the Documents verify flagged that were never retried: a second
        attempt fixes most misquotes; what stays flagged keeps only its verified items."""
        from .lock import exclusive
        from .manifest import Manifest
        with exclusive():
            m = Manifest(MANIFEST_DB)
            done = set(self.state.get("retried", []))
            todo = [d for d in m.flagged_ids() if d not in done]
            if not todo:
                self.say("ops: no newly flagged Document to retry")
                return 0
            m.invalidate_docs("extract", todo, "ops: one retry of a flagged extraction")
        self.say(f"ops: retrying {len(todo)} flagged Document(s) once")
        for argv in (["extract"], ["verify"]):
            code = self.child(Step("retry", argv))
            if code:
                return code
        self.state["retried"] = sorted(done | set(todo))
        return 0

    def eval_steps(self) -> list[Step]:
        from .eval.splits import TUNING_SPLITS
        have = self.splits_with_documents()
        steps = [Step(f"eval:questions:{s}", ["eval", "build", "--set", s, "--top-up"], paid=s)
                 for s in TUNING_SPLITS if s in have]
        steps += [Step("eval:search", ["eval", "run", "--config", BASELINE]),
                  Step("eval:judge", ["eval", "judge", "--config", BASELINE], paid="all"),
                  Step("eval:gate", run=self.gate,
                       note="rescore: refresh the baseline, compare with it, save on PASS")]
        return steps

    def gate(self) -> int:
        """Compare the new run with the baseline on the same labels; save it as the baseline only
        when it passes. The first eval just becomes the baseline."""
        base = EVAL_DATA / "runs" / f"baseline-all-{BASELINE}.json"
        self.save_references(base)
        if not base.exists():
            self.say("ops: no baseline yet: this run becomes it")
            code = self.child(Step("save", ["eval", "rescore", "--config", BASELINE, "--save-baseline"]))
            self.state["eval"] = {"verdict": "baseline", **self._eval_fp()}
            return code
        code = self.child(Step("refresh", ["eval", "rescore", "--config", BASELINE, "--refresh-baseline"]))
        if code:
            return code
        code = self.child(Step("compare", ["eval", "rescore", "--config", BASELINE, "--compare", BASELINE]))
        if code == 0:
            code = self.child(Step("save", ["eval", "rescore", "--config", BASELINE, "--save-baseline"]))
            self.state["eval"] = {"verdict": "pass", **self._eval_fp()}
            return code
        verdict = "fail" if code == EXIT_FAIL else "inconclusive"
        self.state["eval"] = {"verdict": verdict, **self._eval_fp()}
        self.say(f"ops: the gate says {verdict.upper()}: the plugin is not rebuilt; the baseline is kept")
        return EXIT_FAIL if code == EXIT_FAIL else EXIT_INCONCLUSIVE

    def save_references(self, base: Path) -> None:
        """Before the baseline moves: keep it as a dated reference (`--compare full-2026-10-01`; the
        last DATED_KEEP), and, the first time a kind of Source is indexed, as "before <kind>" (what
        `full-prebooks` was for Books). They re-score saved runs: no new search."""
        kinds = sorted(self.indexed_kinds())
        before = self.state.get("kinds")
        self.state["kinds"] = kinds
        if not base.exists():
            return
        runs = base.parent
        data = json.loads(base.read_text())
        day = str(data.get("at") or _now())[:10]
        dated = runs / f"baseline-all-{BASELINE}-{day}.json"
        if not dated.exists():
            dated.write_text(base.read_text())
            self.say(f"ops: kept the current baseline as `{BASELINE}-{day}` (`--compare {BASELINE}-{day}`)")
        for old in sorted(runs.glob(f"baseline-all-{BASELINE}-20*.json"))[:-DATED_KEEP]:
            old.unlink()
        for kind in sorted(set(kinds) - set(before or kinds)):
            ref = runs / f"baseline-all-{BASELINE}-before-{kind}.json"
            if not ref.exists():
                ref.write_text(base.read_text())
                self.say(f"ops: first {kind} Documents indexed: kept `{BASELINE}-before-{kind}` to compare with")

    @staticmethod
    def indexed_kinds() -> set[str]:
        import sqlite3

        from . import source_kinds
        if not MANIFEST_DB.exists():
            return set()
        db = sqlite3.connect(str(MANIFEST_DB))
        docs = [r[0] for r in db.execute("SELECT doc_id FROM stage_state WHERE stage='index' AND status='ok'")]
        db.close()
        return {source_kinds.for_doc(d).name for d in docs}

    def _eval_fp(self) -> dict:
        return {"index": self.index_fp(), "labels": self.labels_fp(), "at": _now()}

    def plugin_steps(self) -> list[Step]:
        private = self.has_private()
        target = "dist/plugin-private" if private else "dist/plugin"
        steps = [Step("plugin:version", run=self.version, note="a new build gets a new version (Cowork updates on it)"),
                 Step("plugin:pack", ["pack", "build", "--out", str(DATA / "pack")],
                      check=lambda since: pack_written(DATA / "pack", since)),
                 Step("plugin:assemble", ["scripts/assemble_plugin.py", "--pack", str(DATA / "pack"), "--out",
                                          "dist/plugin", "--check", "--zip"], script=True)]
        if private:
            steps += [Step("plugin:pack-private", ["pack", "build", "--include-private", "--out",
                                                    str(DATA / "pack-private")],
                           check=lambda since: pack_written(DATA / "pack-private", since)),
                      Step("plugin:assemble-private", ["scripts/assemble_plugin.py", "--pack", str(DATA / "pack-private"),
                                                       "--out", "dist/plugin-private", "--check", "--zip"],
                           script=True)]
        shipped = DATA / ("pack-private" if private else "pack")
        steps.append(Step("plugin:coverage", run=lambda: self.coverage(shipped),
                          note="eval gap on the shipped pack: report only, never stops the run"))
        steps.append(Step("plugin:validate", run=lambda: self.validate(target), note=f"claude plugin validate {target}"))
        steps.append(Step("plugin:record", run=lambda: self.record_plugin(target), note="remember this build"))
        if self.opts.coach:
            steps.append(Step("plugin:coach", run=lambda: self.coach(target),
                              note=f"eval coach --plugin {target}: only the gates whose inputs changed"))
        return steps

    def coverage(self, pack: Path) -> int:
        """`eval gap` on the pack that ships: how often the coach would wrongly answer a Gap question or refuse
        an answerable one. A report, not a gate: with few judged questions the verdict is inconclusive, and a
        Library without a second Domain has no Gap questions that matter. Read the numbers; tune with
        `ytbrain eval calibrate` / `eval gap --tune`, which change behaviour and so stay a human's call."""
        code = self.child(Step("coverage", ["eval", "gap", "--pack", str(pack)]))
        if code:
            self.say(f"ops: eval gap exit {code} (report only): "
                     f"{'not enough Gap or answerable questions yet' if code == 1 else 'targets missed: see the table above'}; "
                     "the plugin build goes on")
        self.last_reason = ""
        return 0

    def validate(self, target: str) -> int:
        if not shutil.which("claude") or self.call is not None:
            self.say("ops: `claude plugin validate` skipped (no claude on PATH)")
            return 0
        return subprocess.call(["claude", "plugin", "validate", str(CODE / target), "--strict"])

    def record_plugin(self, target: str) -> int:
        self.state["plugin"] = {"fp": self.plugin_fp(self.index_fp()), "build": _build_id(CODE / target),
                                "path": target, "version": _release().current_version(CODE), "at": _now()}
        return 0

    def declare_domains(self) -> bool:
        """Sync stopped on book folders that aren't Domains: ask, folder by folder, to declare each one (a risk tier
        and a one-line description, both required) or to ignore it as a sorting folder. True only when every
        folder was settled; with no one at the terminal, or an empty answer, nothing changes and ops stops."""
        from . import domains as D
        try:
            stray = (self.stray or _stray_book_folders)()
        except Exception as e:                    # noqa: BLE001 -- a lookup failure must not hide the real stop
            self.say(f"ops: could not list the book folders ({e})")
            return False
        if not stray:
            return False
        ask = self.ask or _ask_tty
        for folder, n in stray.items():
            name = D.folder_key(folder)
            answer = ask(f"\nBook folder {folder}/ ({n} Book(s)) is not a declared Domain. Add `{name}` as a Domain "
                         f"with risk tier [l]ow / [m]edium / [h]igh, [i]gnore it (it only sorts files), or Enter to "
                         f"stop: ", DOMAIN_WAIT_S)
            a = (answer or "").strip().lower()
            if a in ("i", "ignore"):
                D.ignore_folder(folder)
                self.say(f"ops: {folder}/ added to ignore_folders")
                continue
            tier = {"l": "low", "low": "low", "m": "medium", "medium": "medium", "h": "high", "high": "high"}.get(a)
            if tier is None:
                if answer is None:
                    self.say("ops: no answer: domains.yaml unchanged")
                return False
            desc = ask(f"One line: what is `{name}` about (the coach chooses Domains by it)? ", DOMAIN_WAIT_S)
            if not (desc or "").strip():
                self.say("ops: no description: domains.yaml unchanged for this folder")
                return False
            try:
                D.add_domain(name, risk_tier=tier, description=desc.strip())
            except D.DomainConfigError as e:
                self.say(f"ops: {e}")
                return False
            self.say(f"ops: Domain `{name}` ({tier} risk) added to domains.yaml")
        return True

    def version(self) -> int:
        """Ask for the new build's version: patch (default), minor or skip. With no one at the
        terminal (or no answer within VERSION_WAIT_S), patch. A version you set by hand since the
        last build is kept."""
        rel = _release()
        cur = rel.current_version(CODE)
        last = (self.state.get("plugin") or {}).get("version")
        if last is not None and last != cur:
            self.say(f"ops: plugin version {cur} (set since the last build {last}): kept")
            return 0
        choice = self.opts.version
        if choice is None:
            nxt = {"patch": _bump(cur, "patch"), "minor": _bump(cur, "minor")}
            question = (f"The plugin changed since {cur}. Version for this build: [p]atch {nxt['patch']} / "
                        f"[m]inor {nxt['minor']} / [s]kip (keep {cur})? [p] ")
            self.notify("ytbrain ops: plugin version", f"Choose in the terminal within {VERSION_WAIT_S}s "
                                                       f"(default patch {nxt['patch']})", log=False)
            answer = (self.ask or _ask_tty)(question, VERSION_WAIT_S)
            choice = {"m": "minor", "minor": "minor", "s": "skip", "skip": "skip"}.get((answer or "").strip().lower(),
                                                                                      "patch")
            if answer is None:
                self.say(f"ops: no answer: patch {nxt['patch']}")
        if choice == "skip":
            self.say(f"ops: plugin version stays {cur} (Cowork won't see this build as an update)")
            return 0
        new = _bump(cur, choice)
        rel.set_version(CODE, new)
        self.say(f"ops: plugin version {cur} -> {new} (plugin.json, plugin/pyproject.toml, founder_coach)")
        return 0

    def coach(self, target: str) -> int:
        build = _build_id(CODE / target)
        ran = self.state.get("coach", {})
        if ran.get("build") == build and not ran.get("code") and not self.opts.force:
            self.say(f"ops: coach eval already passed on build {build}")
            return 0
        # (a build whose coach eval failed or stopped is run again: finished cases are cached, so only errored
        # ones cost anything, and a real FAIL is reported again instead of being forgotten)
        if self.call is None and not shutil.which("claude"):
            self.say("ops: coach eval skipped: no `claude` on PATH; it runs on the next `ytbrain ops` that has it")
            self.state["coach"] = {"build": None, "pending": "no claude", "at": _now()}
            return 0
        code = self.child(Step("coach", ["eval", "coach", "--plugin", target], paid="coach"))
        if code and self.last_reason == "plan_limit":
            self.say(f"ops: {FIX['plan_limit']}; the plugin is built")
            self.state["coach"] = {"build": None, "pending": "plan limit", "at": _now()}
            return 0
        if code and self.last_reason == "auth":
            self.state["coach"] = {"build": None, "pending": "login needed", "at": _now()}
            return code                      # ops stops with the fix; the next run does only the coach eval
        if code == 2:                        # stopped or incomplete (spend cap, errored cases): not a verdict on the build
            self.state["coach"] = {"build": None, "pending": "incomplete", "at": _now()}
            return code
        self.state["coach"] = {"build": build, "code": code, "at": _now()}
        return code

    # ------------------------------------------------------------------ what is due
    def due(self) -> list[tuple[str, list[Step], str]]:
        """(plan, its steps, why) for each plan this run should do, in order."""
        plan, out = self.opts.plan, []
        if plan in ("all", "ingest"):
            out.append(("ingest", self.ingest_steps(), "sources are synced on every run"))
        if plan in ("all", "eval"):
            out.append(("eval", self.eval_steps(), "after ingest: when the index or labels changed"))
        if plan in ("all", "plugin"):
            out.append(("plugin", self.plugin_steps(), "after a passing eval: when the index or plugin changed"))
        return out

    def eval_is_due(self) -> tuple[bool, str]:
        last = self.state.get("eval") or {}
        if self.opts.force:
            return True, "--force"
        if not last:
            return True, "never evaluated"
        if last.get("index") != self.index_fp():
            return True, "the index changed"
        if last.get("labels") != self.labels_fp():
            return True, "the labels changed"
        if last.get("verdict") in ("fail", "inconclusive"):
            return True, f"the last verdict was {last['verdict']}"
        return False, "nothing changed since the last eval"

    def plugin_is_due(self) -> tuple[bool, str]:
        last_eval = self.state.get("eval") or {}
        passed = last_eval.get("verdict") in ("pass", "baseline") and last_eval.get("index") == self.index_fp()
        if not self.opts.force and not passed:
            return False, "the current index has no passing eval (run `ytbrain ops eval`, or --force)"
        last = self.state.get("plugin") or {}
        if self.opts.force or last.get("fp") != self.plugin_fp(self.index_fp()):
            return True, "the index or the plugin changed"
        coach = self.state.get("coach", {})
        if self.opts.coach and (coach.get("build") != last.get("build") or coach.get("code")):
            return True, COACH_ONLY             # not run yet, stopped, or failed: say so again until it passes
        return False, "nothing changed since the last build"

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        self.load()
        cur = self.state.get("run")
        if cur and (self.opts.restart or cur.get("plan") != self.opts.plan):
            cur = None
        if cur is None:
            cur = {"plan": self.opts.plan, "started": _now(), "done": [], "spent_at_start": self.spent_total()}
        else:
            self.say(f"ops: resuming the {cur['plan']} run started {cur['started']} "
                     f"({len(cur['done'])} step(s) done; --restart starts over)")
        self.state["run"] = cur
        for plan, steps, _ in self.due():
            if plan == "eval" and not any(s.startswith("eval:") for s in cur["done"]):
                ok, why = self.eval_is_due()
                if not ok:
                    self.say(f"ops: eval skipped: {why}")
                    continue
                self.say(f"ops: eval: {why}")
            if plan == "plugin" and not any(s.startswith("plugin:") for s in cur["done"]):
                ok, why = self.plugin_is_due()
                if not ok:
                    self.say(f"ops: plugin skipped: {why}")
                    continue
                self.say(f"ops: plugin: {why}")
                if why == COACH_ONLY:            # the build is current: no new pack, version or zip
                    steps = [st for st in steps if st.name == "plugin:coach"]
            for i, step in enumerate(steps, 1):
                if step.name in cur["done"]:
                    continue
                what = " ".join(step.argv) + (f"  ({step.note})" if step.note else "") if step.argv else step.note
                self.say(f"\n=== ops {plan} [{i}/{len(steps)}] {step.name.split(':', 1)[1]}: {what} ===")
                if self.opts.dry_run:
                    continue
                if step.name == "ingest:extract":
                    self.say(f"ops: {_pending_extract()} Document(s) to extract")
                code = step.run() if step.run else self.child(step)
                if code and step.name == "ingest:sync" and self.last_reason == "network":
                    self.say("ops: sync kept failing on the network: continuing with what is already fetched")
                    code = 0
                if code and step.name == "ingest:sync" and self.last_reason == "config" and self.declare_domains():
                    self.say("ops: domains.yaml updated: syncing again")
                    code = self.child(step)
                if code and step.name == "ingest:sync" and self.last_reason == "books":
                    self.say(f"ops: {self.last_detail}: carrying on with the Books that did register "
                             "(see the sync output; `skip: true` in sources.yaml silences a PDF you don't want)")
                    code = 0
                if code:
                    self.save()
                    reason = "gate" if step.name == "eval:gate" and code in (EXIT_FAIL, EXIT_INCONCLUSIVE) \
                        else self.last_reason or "unknown"
                    self.say(f"\nops: stopped at {step.name} (exit {code}): {FIX.get(reason, FIX['unknown'])}. "
                             f"`ytbrain ops {self.opts.plan}` resumes here")
                    self.notify(f"ytbrain ops stopped: {step.name}",
                                f"{FIX.get(reason, FIX['unknown'])}. Resume: ytbrain ops {self.opts.plan}")
                    return code
                cur["done"].append(step.name)
                self.save()
        if self.opts.dry_run:
            self.say("\nops: dry run, nothing was run")
            return 0
        self.state["run"] = None
        self.state["last_run"] = {"plan": self.opts.plan, "finished": _now(),
                                  "spent": round(self.spent_total() - cur["spent_at_start"], 4)}
        self.save()
        ev, pl = self.state.get("eval") or {}, self.state.get("plugin") or {}
        coach = self.state.get("coach") or {}
        coach_note = ("" if self.opts.plan in ("ingest", "eval")
                      else "; coach eval pending" if self.opts.coach and coach.get("pending")
                      else "" if self.opts.coach
                      else "; coach eval not run (`ytbrain ops plugin --coach` before a release)")
        summary = (f"eval {ev.get('verdict', 'not run')}; plugin {pl.get('version', '-')} ({pl.get('path', 'not built')})"
                   + coach_note
                   + f"; eval spend ${self.state['last_run']['spent']:.3f}")
        self.say(f"\nops: {self.opts.plan} done: {summary}")
        self.notify(f"ytbrain ops {self.opts.plan} done", summary)
        return 0


def crash_signal(code: int) -> str | None:
    """The crash signal a child died on, as subprocess reports it (-6) or a shell does (134), else None."""
    n = -code if code < 0 else code - 128 if code > 128 else 0
    if n in CRASH_SIGNALS:
        try:
            return signal.Signals(n).name
        except ValueError:
            return f"signal {n}"
    return None


def pack_written(out: Path, since: float) -> str | None:
    """None when `out` holds a Knowledge pack whose manifest was written at or after `since` and whose file
    matches the manifest's sha256, else why not. `pack build` swaps the file, then its manifest (via
    pack.json.next), so a fresh pack.json with no .next left over means both swaps happened."""
    from founder_coach import pack as P
    db, manifest = out / P.PACK_FILE, out / P.MANIFEST_FILE
    try:
        if manifest.stat().st_mtime < since - 1:          # 1 s: coarse file-system clocks
            return f"{manifest} is older than this run: the build never got that far"
        want = json.loads(manifest.read_text(encoding="utf-8")).get("sha256")
    except (OSError, ValueError, AttributeError) as e:
        return f"{manifest} is missing or unreadable ({e})"
    if manifest.with_name(P.MANIFEST_FILE + ".next").exists():
        return "the build stopped between writing the pack and its manifest"
    try:
        got = P.sha256_file(db)
    except OSError as e:
        return f"{db} is missing or unreadable ({e})"
    return None if want and got == want else f"{db.name} doesn't match the sha256 in {manifest.name}"


def _pending_extract() -> int:
    import sqlite3
    if not MANIFEST_DB.exists():
        return 0
    db = sqlite3.connect(str(MANIFEST_DB))
    n = db.execute("SELECT COUNT(*) FROM documents d JOIN stage_state c ON c.doc_id=d.doc_id AND c.stage='clean' "
                   "AND c.status='ok' LEFT JOIN stage_state e ON e.doc_id=d.doc_id AND e.stage='extract' "
                   "WHERE d.tombstoned_at IS NULL AND (e.status IS NULL OR e.status!='ok')").fetchone()[0]
    db.close()
    return int(n)


def _release():
    """scripts/release.py, for its version helpers (one place that knows the three version files)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ytbrain_release", CODE / "scripts" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split(".")[:3])
    return f"{major}.{minor + 1}.0" if part == "minor" else f"{major}.{minor}.{patch + 1}"


def _stray_book_folders() -> dict[str, int]:
    from .books.adapter import stray_book_folders
    return stray_book_folders()


def _ask_tty(question: str, timeout: float) -> str | None:
    """An answer typed within `timeout` seconds, or None (no terminal, or no answer)."""
    if not sys.stdin.isatty():
        return None
    import select
    print(question, end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        print()
        return None
    return sys.stdin.readline()


def _build_id(plugin: Path) -> str:
    p = plugin / "BUILD_ID"
    return p.read_text().strip() if p.exists() else ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def main(opts: Options) -> int:
    from .lock import LockBusy, exclusive
    try:
        with exclusive(OPS_LOCK):
            return Ops(opts).run()
    except LockBusy:
        print("ops: another `ytbrain ops` is running", file=sys.stderr)
        return EXIT_STOPPED
    except KeyboardInterrupt:
        print(f"\n\nops: interrupted. Finished steps are saved; `ytbrain ops {opts.plan}` resumes.",
              file=sys.stderr)
        return 130


__all__ = ["PLANS", "Ops", "Options", "main"]
