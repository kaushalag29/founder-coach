"""The runtime's command line, named after the product id (phase3-plan §11.5, ADR-0012).

  serve               the stdio MCP server (what the plugin starts)
  hook session-start  one line of context when something is due; silent otherwise; never fails
  warmup              download and load the search models once, with progress
  status              pack, models, store (with its integrity check) and what's due
  export              everything the coach remembers, as JSON + Markdown
  forget              delete it all (typed confirmation; one final backup unless --no-backup)
  feedback            list the Founder's Feedback, or export it as one file to send
  usage               the local usage log: a summary, an export to send, or clear it
  restore             replace a damaged store with a backup; the old file is set aside, never deleted

Export, forget and restore are deliberately not MCP tools, so no model can wipe or roll back
the Founder's memory.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from . import domain as D
from . import product


def _hook(args) -> int:
    """SessionStart hook: fail-open (always exit 0), fast (no models, no MCP), quiet."""
    try:
        try:
            import select
            if not sys.stdin.isatty() and select.select([sys.stdin], [], [], 0.2)[0]:
                sys.stdin.read()                       # the host's event JSON; nothing in it is needed
        except Exception:                              # noqa: BLE001
            pass
        from .nudges import hook_text
        from .store import open_store
        if args.home is None:                          # Projects, none chosen: say so, read nothing
            ps = args.workspace.projects.list() if args.workspace and args.workspace.projects else []
            text = (f"{D.COACH_NAME}: {len(ps)} Projects (" + ", ".join(p["name"] for p in ps[:5])
                    + product.reword("). Before reading or saving anything about the Founder, ask which Project "
                                     "this conversation is about.") if ps else f"{D.COACH_NAME}: {D.SETUP_NUDGE}")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
            return 0
        store, err = open_store(args.home)
        if store is None:
            err = err or ""
            if f"{product.ID} restore" in err:
                text = (f"{D.COACH_NAME}: " + product.reword("the Founder's memory store can't be read. Nothing has "
                                                             "been deleted; ")
                        + f"run `{product.ID} restore` in a terminal to restore the latest backup. Search still works.")
            elif "newer than this coach" in err:
                text = f"{D.COACH_NAME}: " + product.reword("the Founder's memory store is newer than this coach; "
                                                            "update the plugin.")
            else:
                text = ""                                  # anything else: stay quiet
        else:
            try:
                text = hook_text(store)
                many = args.workspace and args.workspace.projects and len(args.workspace.projects.list()) > 1
                if text and many:
                    name = args.workspace.describe(args.project_id)["name"]
                    text = text.replace(f"{D.COACH_NAME}: ", f"{D.COACH_NAME} (Project {name}, the last one used; "
                                                           "confirm it's the one this conversation is about): ", 1)
            finally:
                store.close()
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    except Exception:                                  # noqa: BLE001 -- a hook must never break a session
        pass
    return 0


def _warmup(args) -> int:
    from .models import models_dir
    from .pack import PackStore, find_pack
    from .server import use_reranker
    path = find_pack(args.pack)
    try:
        pk = PackStore(path, verify="cached")
    except RuntimeError as e:
        print(f"{product.ID}: {e}", file=sys.stderr)
        return 1
    rerank = use_reranker(pk.meta)
    print(f"{product.ID}: loading {pk.meta['embed_model']}" + (f" and {pk.meta['rerank_model']}" if rerank else "")
          + f" (first time: downloads to {models_dir()}) ...", flush=True)
    t0 = time.time()
    try:
        from .models import for_pack
        embed, reranker = for_pack(pk.meta, rerank=rerank)
        embed(["warm-up query"])
        if reranker:
            reranker("warm-up query", ["warm-up passage"])
    except RuntimeError as e:
        print(f"{product.ID}: {e}", file=sys.stderr)
        return 1
    print(f"{product.ID}: search models ready in {time.time() - t0:.0f}s", flush=True)
    return 0


def _status(args) -> int:
    from .models import models_dir
    from .nudges import nudges
    from .pack import PackStore, find_pack
    from .store import open_store
    path = find_pack(args.pack)
    out: dict = {"pack": str(path)}
    try:
        pk = PackStore(path, verify="cached")
        m = pk.meta
        out["pack_items"] = f"{m['items']} items from {m['talks']} talks ({m['years'][0]}-{m['years'][1]}), " \
                            f"built {m['built_at'][:10]}, {m['embed_model']}, checksum ok"
        pk.close()
    except (RuntimeError, KeyError, IndexError) as e:
        out["pack_items"] = f"unavailable: {e}"
    md = models_dir()
    have = [p.name for p in md.iterdir()] if md.is_dir() else []
    from . import settings
    out["settings"] = (", ".join(f"{f['path']} ({f['count']} used)" for f in settings.LOADED)
                       or f"environment only (no .env found: ./.env, ~/.{product.ID}/.env or ${product.ENV_PREFIX}ENV_FILE)")
    out["models"] = (f"{md} ({len(have)} downloaded)" if have else
                     f"{md} (not downloaded yet: run `{product.ID} warmup`; search uses keywords until then)")
    ws = args.workspace
    if ws is not None and not ws.single:
        ps = ws.projects.list()
        out["projects"] = (", ".join(p["id"] + (" (active)" if p["id"] == args.project_id else "") for p in ps)
                           or "none yet (setup makes the first)")
        if ws.migration:
            out["migration"] = ws.migration.get("error") or f"moved {ws.migration.get('from')} into Project " \
                                                             f"{ws.migration.get('project')}"
        common = ws.engine / "you.db"
        out["common"] = str(common) if common.exists() else "nothing shared yet"
    if args.home is None:
        out["store"] = "no Project chosen: pass --project (or run `projects use <id>`)" if ws.projects.list() \
            else "no Project yet"
        store = None
    else:
        store, err = open_store(args.home)
    if args.home is None:
        pass
    elif store is None:
        out["store"] = err
        out["integrity"] = "failed"
    else:
        try:
            out["store"] = str(store.path)
            out["integrity"] = store.check()["detail"]
            out["profile"] = {f: v["value"] for f, v in store.profile().items()}
            out["due"] = [n["message"] for n in nudges(store)]
            if product.env("USAGE", "1") == "0":
                out["usage"] = f"off ({product.ENV_PREFIX}USAGE=0)"
            else:
                out["usage"] = f"on: {store.usage_summary(30)['calls']} call(s) in the last 30 days, kept " \
                               f"{product.env('USAGE_DAYS', '90')} days " \
                               f"(`{product.ID} usage summary`)"
        finally:
            store.close()
    if args.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
    else:
        for k, v in out.items():
            print(f"{k:11} {v}")
    return 0 if store is not None or args.home is None else 1


def _open_or_explain(home):
    """FounderStore for export/forget, or None after printing why (a damaged store, or no Project chosen)."""
    from .store import FounderStore, StoreError
    if home is None:
        print(f"{product.ID}: {_which_project()}", file=sys.stderr)
        return None
    try:
        return FounderStore(home)
    except StoreError as e:
        print(f"{product.ID}: {e}", file=sys.stderr)
        return None


def _export(args) -> int:
    store = _open_or_explain(args.home)
    if store is None:
        return 1
    try:
        paths = store.export(args.out)
    except OSError as e:
        print(f"{product.ID}: couldn't write the export ({e}); pass --out with a folder you can write to",
              file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"{product.ID}: exported to {paths['json']} and {paths['markdown']}")
    return 0


def _forget_all(args) -> int:
    """Every Project of this coach and the Common profile, after one confirmation."""
    from .store import FounderStore, StoreError
    ws = args.workspace
    if ws is None or ws.single:
        print(f"{product.ID}: --all needs Projects (it isn't used with --home or {product.ENV_PREFIX}HOME)",
              file=sys.stderr)
        return 2
    ps = ws.projects.list()
    common = ws.engine / "you.db"
    print(f"This deletes everything the coach remembers in {len(ps)} Project(s): "
          + (", ".join(p["name"] for p in ps) or "none")
          + (", and the Common profile your coaches share (name, role, timezone, answer style)" if common.exists() else ""))
    print("  " + ("and all backups." if args.no_backup else "(one final backup of each is kept so this can be undone)."))
    answer = args.confirm if args.confirm is not None else input("Type 'forget all' to confirm: ")
    if answer.strip() != "forget all":
        print(f"{product.ID}: not confirmed; nothing deleted")
        return 1
    failed = 0
    for p in ps:
        try:
            store = FounderStore(ws.store_home(p["id"]))
        except StoreError as e:
            print(f"{product.ID}: Project {p['id']} skipped: {e}", file=sys.stderr)
            failed += 1
            continue
        try:
            store.forget(keep_backup=not args.no_backup)
        finally:
            store.close()
    if common.exists():
        try:
            c = FounderStore(ws.engine, name="you")
            try:
                c.forget(keep_backup=not args.no_backup)
            finally:
                c.close()
        except StoreError as e:
            print(f"{product.ID}: the Common profile was skipped: {e}", file=sys.stderr)
            failed += 1
    print(f"{product.ID}: everything the coach remembered is deleted ({len(ps) - failed} Project(s)"
          + (" and the Common profile" if common.exists() else "") + "); the Project folders stay, empty")
    return 1 if failed else 0


def _forget(args) -> int:
    if args.confirm is None and not sys.stdin.isatty():
        print(f"{product.ID}: refusing to forget without a terminal to confirm on. Pass --confirm with the "
              f"{D.REQUIRED[0]} exactly as saved (or 'forget' if none is saved; 'forget all' with --all).",
              file=sys.stderr)
        return 2
    if args.all:
        return _forget_all(args)
    store = _open_or_explain(args.home)
    if store is None:
        return 1
    prof = store.profile()
    word = str((prof.get(D.REQUIRED[0]) or {}).get("value") or "forget")    # the company, the system ...
    counts = {t: store.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("goals", "commitments", "decisions", "checkins")}
    where = f"Project {args.workspace.describe(args.project_id)['name']} ({store.home})" \
        if args.project_id else str(store.home)
    print(f"This deletes everything the {D.COACH_NAME.lower()} remembers in {where}:")
    print("  profile, " + ", ".join(f"{n} {t}" for t, n in counts.items()) + ", FOUNDER.md and the change log")
    print("  " + ("and all backups." if args.no_backup else "(one final backup is kept in backups/ so this can be undone)."))
    answer = args.confirm if args.confirm is not None else input(f"Type {word!r} to confirm: ")
    if answer.strip() != word:
        print(f"{product.ID}: not confirmed; nothing deleted")
        store.close()
        return 1
    res = store.forget(keep_backup=not args.no_backup)
    print(f"{product.ID}: everything the coach remembered is deleted (the store at {res['deleted']} is now empty; "
          f"a running coach sees that at once)"
          + (f"; final backup at {res['backup']}" if res["backup"] else "")
          + (f"; {res['backups_removed']} backup file(s) removed" if res.get("backups_removed") else ""))
    return 0


def _restore(args) -> int:
    from .store import StoreError, list_backups, restore
    if args.home is None:
        print(f"{product.ID}: {_which_project()}", file=sys.stderr)
        return 1
    if args.list:
        backups = list_backups(args.home)
        if not backups:
            print(f"{product.ID}: no backups yet")
            return 1
        for b in backups:
            print(f"{b['label']:32} {b['modified']}  {b['size'] / 1024:7.1f} KB  "
                  + ("ok" if b["ok"] else f"FAILS CHECK: {b['detail']}"))
        return 0
    try:
        res = restore(args.home, args.backup)
    except StoreError as e:
        print(f"{product.ID}: {e}", file=sys.stderr)
        return 1
    print(f"{product.ID}: restored {res['store']} from {res['restored_from']} (integrity {res['check']})")
    if res["set_aside"]:
        print(f"  the previous files were moved, not deleted, to {res['set_aside']}")
    if res.get("in_place"):
        print("  a running coach sees the restored data at once")
    else:
        print("  start a new session (or restart the host) so the coach reopens the store")
    return 0


def _feedback(args) -> int:
    store = _open_or_explain(args.home)
    if store is None:
        return 1
    try:
        if args.action == "list":
            rows = store.feedback_list()
            for r in rows:
                print(f"{r['id']}  {r['created_at']}  {r['category']:14}  {' '.join(r['question'].split())[:70]}")
            print(f"{product.ID}: {len(rows)} Feedback record(s)")
            return 0
        res = store.export_feedback(args.out)
    except OSError as e:
        print(f"{product.ID}: couldn't write the Feedback file ({e}); pass --out with a folder you can write to",
              file=sys.stderr)
        return 1
    finally:
        store.close()
    if not res["count"]:
        print(f"{product.ID}: no Feedback saved yet (use /{product.ID}:feedback after an answer that missed)")
        return 0
    print(f"{product.ID}: {res['count']} Feedback record(s) written to {res['path']}\n"
          f"  Only this file leaves your machine, and only if you send it. It holds your questions and the "
          f"coach's answers, nothing else from your store.")
    return 0


def _usage(args) -> int:
    store = _open_or_explain(args.home)
    if store is None:
        return 1
    try:
        if args.action == "clear":
            if not args.yes:
                print(f"{product.ID}: this deletes the usage log (nothing else); pass --yes to confirm",
                      file=sys.stderr)
                return 2
            n = store.clear_usage()
            print(f"{product.ID}: usage log cleared ({n} event(s) deleted)")
            return 0
        if args.action == "export":
            res = store.export_usage(args.out)
            print(f"{product.ID}: {res['count']} usage event(s) written to {res['path']}\n"
                  f"  Only this file leaves your machine, and only if you send it. It holds which tools ran, "
                  f"when, how long they took and how they went"
                  + (" -- and your search text, which you opted in to" if any(
                      "query" in r["detail"] for r in store.usage_rows()) else " -- none of your words") + ".")
            return 0
        s = store.usage_summary(args.days)
    except OSError as e:
        print(f"{product.ID}: couldn't write the usage file ({e}); pass --out with a folder you can write to",
              file=sys.stderr)
        return 1
    finally:
        store.close()
    if args.json:
        print(json.dumps(s, indent=1))
        return 0
    state = "off" if product.env("USAGE", "1") == "0" else "on"
    print(f"{product.ID}: {s['calls']} call(s) in {s['runs']} session(s) over the last {s['days']} days "
          f"(usage log {state}; {product.ENV_PREFIX}USAGE=0 turns it off)")
    if s["calls"]:
        print(f"  {'tool':22} {'calls':>5} {'errors':>6} {'empty':>5} {'gaps':>5} {'keyword':>7} {'p50 ms':>7} {'p95 ms':>7}")
        for name, t in s["tools"].items():
            print(f"  {name:22} {t['calls']:5} {t['errors']:6} {t['empty']:5} {t['gaps']:5} {t['keyword']:7} "
                  f"{t['p50_ms']:7} {t['p95_ms']:7}")
    return 0


_WS = {}


def _which_project() -> str:
    ws = _WS.get("ws")
    ps = ws.projects.list() if ws is not None and ws.projects is not None else []
    if not ps:
        return "no Project yet: run setup in the coach (or `projects create <name>`)"
    return ("several Projects, none chosen: pass --project with one of " + ", ".join(p["id"] for p in ps)
            + f" (or `{product.ID} projects use <id>`)")


def _holdings(args) -> int:
    """The investor Pack's Holdings from the command line: import a positions CSV, label symbols, list, review."""
    from .invest import ImportProblem, latest_per_account, money, read_positions
    from .store import StoreError
    if not product.has("holdings"):
        print(f"{product.ID}: this coach keeps no Holdings (its Pack's memory modules: "
              f"{', '.join(product.PACK.get('modules') or []) or 'none'})", file=sys.stderr)
        return 2
    store = _open_or_explain(args.home)
    if store is None:
        return 1
    try:
        if args.action == "import":
            if len(args.args) != 1:
                print(f"{product.ID}: holdings import <positions.csv> [--account NAME] [--as-of YYYY-MM-DD]",
                      file=sys.stderr)
                return 2
            snaps = read_positions(args.args[0], account=args.account, as_of=args.as_of, today=store.today())
            res = store.import_holdings(snaps, Path(args.args[0]).name)
            for x in res["imported"]:
                print(f"imported {x['account']} as of {x['as_of']}: {x['positions']} position(s), "
                      f"{money(x['total_cents'])}")
            for x in res["skipped"]:
                print(f"already imported: {x['account']} as of {x['as_of']}")
            if res["unlabelled"]:
                print(f"no asset class yet for: {', '.join(res['unlabelled'])} -- label each with "
                      f"`{product.ID} holdings label SYMBOL=CLASS ...` (classes: us_equity, intl_equity, bonds, "
                      "cash, real_estate, other)")
        elif args.action == "label":
            pairs = dict(a.split("=", 1) for a in args.args if "=" in a)
            if not pairs or len(pairs) != len(args.args):
                print(f"{product.ID}: holdings label SYMBOL=CLASS ... e.g. VTI=us_equity BND=bonds", file=sys.stderr)
                return 2
            print("labelled: " + ", ".join(f"{k}={v}" for k, v in store.label_assets(pairs)["labelled"].items()))
        elif args.action == "review":
            print(json.dumps(store.holdings_review(), indent=1))
        else:
            snaps, _ = store.holdings()
            for x in latest_per_account(snaps):
                print(f"{x['account']:24} as of {x['as_of']}  {money(x['total_cents']):>16}  ({x['broker']})")
            print(f"{product.ID}: {len(latest_per_account(snaps))} account(s)")
    except (ImportProblem, StoreError) as e:
        print(f"{product.ID}: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def _projects(args) -> int:
    from .projects import ProjectError
    ws = args.workspace
    if ws.single:
        print(f"{product.ID}: Projects are off: {product.ENV_PREFIX}HOME (or --home) names one data folder ({ws.root})")
        return 0 if args.action == "list" else 2
    try:
        if args.action == "create":
            p = ws.projects.create(" ".join(args.args))
            ws.projects.set_last(p["id"])
            print(f"{product.ID}: created Project {p['id']} ({p['name']}); the command line now uses it")
        elif args.action == "rename":
            if len(args.args) < 2:
                print(f"{product.ID}: projects rename <id> <new name>", file=sys.stderr)
                return 2
            p = ws.projects.rename(args.args[0], " ".join(args.args[1:]))
            print(f"{product.ID}: Project {p['id']} is now named {p['name']!r} (its memory is unchanged)")
        elif args.action == "use":
            if not args.args:
                print(f"{product.ID}: projects use <id or name>", file=sys.stderr)
                return 2
            p = ws.projects.need(" ".join(args.args))
            ws.projects.set_last(p["id"])
            print(f"{product.ID}: the command line and the session hook now use Project {p['id']} ({p['name']}); "
                  "the coach still asks in a session when there are several")
        else:
            ps = ws.projects.list()
            last = ws.projects.last()
            for p in ps:
                print(f"{'*' if p['id'] == last else ' '} {p['id']:24} {p['name']:32} {p['created'][:10]}")
            print(f"{product.ID}: {len(ps)} Project(s) in {ws.projects.dir}" + ("; * = last used" if last else ""))
    except ProjectError as e:
        print(f"{product.ID}: {e}", file=sys.stderr)
        return 1
    return 0


def _resolve(args) -> int | None:
    """Where this command's memory is: args.home becomes the chosen Project's folder (or the one data folder;
    None when Projects are in use and none is chosen). A wrong --project stops here."""
    from .projects import ProjectError, Workspace
    ws = Workspace.open(args.home)
    _WS["ws"] = args.workspace = ws
    try:
        pid = ws.choose(getattr(args, "project", None), use_last=True)
    except ProjectError as e:
        print(f"{product.ID}: {e}", file=sys.stderr)
        return 2
    args.project_id = pid
    args.home = ws.root if ws.single else (ws.store_home(pid) if pid else None)
    return None


def main(argv: list[str] | None = None) -> int:
    from . import __version__
    p = argparse.ArgumentParser(prog=product.ID, description=f"{product.ID}: the runtime's command line",
                                epilog=f"Settings ({product.ENV_PREFIX}*) come from the environment, then "
                                       f"${product.ENV_PREFIX}ENV_FILE, ./.env, and ~/.{product.ID}/.env "
                                       f"(only {product.ENV_PREFIX}* lines are read).")
    p.add_argument("--version", action="version", version=f"{product.ID} {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the stdio MCP server")
    h = sub.add_parser("hook", help="host hooks")
    h.add_argument("event", choices=["session-start"])
    w = sub.add_parser("warmup", help="download and load the search models once")
    st = sub.add_parser("status", help="pack, models, store and what's due")
    st.add_argument("--json", action="store_true")
    e = sub.add_parser("export", help="everything the coach remembers, as JSON + Markdown")
    e.add_argument("--out", default=None, help=f"folder (default ~/.{product.ID}/exports/<time>)")
    f = sub.add_parser("forget", help="delete everything the coach remembers")
    f.add_argument("--no-backup", action="store_true", help="also delete the backups")
    f.add_argument("--all", action="store_true", help="every Project of this coach and the Common profile "
                                                      "(confirm with 'forget all')")
    f.add_argument("--confirm", default=None, metavar="COMPANY",
                   help="the company name exactly as saved ('forget' if none is saved), instead of typing it at "
                        "the prompt; required when there is no terminal. A mismatch deletes nothing.")
    fb = sub.add_parser("feedback", help="Feedback you saved with the feedback command: list it, or export it to send")
    fb.add_argument("action", choices=["export", "list"])
    fb.add_argument("--out", default=None, help=f"folder for the export (default ~/.{product.ID}/exports)")
    u = sub.add_parser("usage", help="the local usage log: summary, export (to send) or clear")
    u.add_argument("action", choices=["summary", "export", "clear"])
    u.add_argument("--days", type=int, default=30, help="summary window (default 30)")
    u.add_argument("--json", action="store_true")
    u.add_argument("--out", default=None, help=f"folder for the export (default ~/.{product.ID}/exports)")
    u.add_argument("--yes", action="store_true", help="confirm clear")
    r = sub.add_parser("restore", help="replace a damaged store with a backup (the old file is set aside, not deleted)")
    r.add_argument("backup", nargs="?", default=None,
                   help="a backup's label or file from --list (default: the newest one that passes its check)")
    r.add_argument("--list", action="store_true", help="list backups, newest first, with their integrity check")
    hd = sub.add_parser("holdings", help="the investor coach's Holdings: import a positions CSV, label, list, review")
    hd.add_argument("action", nargs="?", default="list", choices=["list", "import", "label", "review"])
    hd.add_argument("args", nargs="*", help="import <file.csv> · label SYMBOL=CLASS ...")
    hd.add_argument("--account", default=None, help="import: the account's name when the file has none")
    hd.add_argument("--as-of", default=None, help="import: YYYY-MM-DD when the file doesn't say")
    pj = sub.add_parser("projects", help="list, create, rename, or pick the Project the command line uses")
    pj.add_argument("action", nargs="?", default="list", choices=["list", "create", "rename", "use"])
    pj.add_argument("args", nargs="*", help="create <name> · rename <id> <new name> · use <id or name>")
    for x in (s, w, st):
        x.add_argument("--pack", default=None, help="Knowledge pack folder or file "
                                                     f"(default ${product.ENV_PREFIX}PACK, then the plugin's pack/, the "
                                                     f"repo's data/pack, then ~/.{product.ID}/pack)")
    for x in (s, h, w, st, e, f, fb, u, r, pj, hd):
        x.add_argument("--home", default=None, help=f"one data folder, without Projects (default "
                                                    f"${product.ENV_PREFIX}HOME; unset: the Projects in ~/.ytbrain)")
    for x in (h, st, e, f, fb, u, r, hd):
        x.add_argument("--project", default=None, help="the Project's id or name (default: the only one, else the "
                                                       f"last used, else ${product.ENV_PREFIX}PROJECT)")
    args = p.parse_args(argv)
    if getattr(args, "home", None):
        # --home is the whole data folder: everything that reads the home (the models folder, the
        # pack memo) must agree with the store, so it becomes this process's home setting
        import os
        from pathlib import Path
        os.environ[product.env_name("HOME")] = str(Path(args.home).expanduser())
    from . import settings
    settings.load()                     # <PREFIX>* lines from .env files; the environment (and --home) win
    if args.cmd == "serve":
        from .server import serve
        serve(pack=args.pack, home=args.home)
        return 0
    if args.cmd != "warmup":
        try:
            stop = _resolve(args)
        except Exception:                              # noqa: BLE001 -- a hook must never break a session
            if args.cmd == "hook":
                return 0
            raise
        if stop is not None:
            return 0 if args.cmd == "hook" else stop
    return {"hook": _hook, "warmup": _warmup, "status": _status, "export": _export, "forget": _forget,
            "feedback": _feedback, "usage": _usage, "restore": _restore, "projects": _projects,
            "holdings": _holdings}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
