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
        store, err = open_store(args.home)
        if store is None:
            err = err or ""
            text = ("Founder coach: the Founder's memory store can't be read. Nothing has been deleted; "
                    f"run `{product.ID} restore` in a terminal to restore the latest backup. Search still works."
                    if f"{product.ID} restore" in err else
                    "Founder coach: the Founder's memory store is newer than this coach; update the plugin."
                    if "newer than this coach" in err else "")      # anything else: stay quiet
        else:
            try:
                text = hook_text(store)
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
    store, err = open_store(args.home)
    if store is None:
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
    return 0 if store is not None else 1


def _open_or_explain(home):
    """FounderStore for export/forget, or None after printing why (a damaged store)."""
    from .store import FounderStore, StoreError
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


def _forget(args) -> int:
    if args.confirm is None and not sys.stdin.isatty():
        print(f"{product.ID}: refusing to forget without a terminal to confirm on. Pass --confirm with the "
              "company name exactly as saved (or 'forget' if none is saved).", file=sys.stderr)
        return 2
    store = _open_or_explain(args.home)
    if store is None:
        return 1
    prof = store.profile()
    word = (prof.get("company") or {}).get("value") or "forget"
    counts = {t: store.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("goals", "commitments", "decisions", "checkins")}
    print(f"This deletes everything the founder coach remembers in {store.home}:")
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
    for x in (s, w, st):
        x.add_argument("--pack", default=None, help="Knowledge pack folder or file "
                                                     f"(default ${product.ENV_PREFIX}PACK, then the plugin's pack/, the "
                                                     f"repo's data/pack, then ~/.{product.ID}/pack)")
    for x in (s, h, w, st, e, f, fb, u, r):
        x.add_argument("--home", default=None, help=f"data folder (default ${product.ENV_PREFIX}HOME or ~/.{product.ID})")
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
    return {"hook": _hook, "warmup": _warmup, "status": _status, "export": _export, "forget": _forget,
            "feedback": _feedback, "usage": _usage, "restore": _restore}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
