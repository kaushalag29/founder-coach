"""The tag Step: Domains from what the content says, against the controlled Domain list (ADR-0017).

Every Passage and Knowledge item already has a vector in the index. Each Domain has a profile (its
description, example questions and aliases), embedded once per run. An item's score for a Domain is
its best cosine with one of the Domain's profile texts; a Source's or Book folder's Domain (a hint)
adds a small bonus. Then, deterministically:

  best score below `floor`         nothing fits: the item takes its Document's Domains
  one Domain within `close` of it  that Domain
  two or more within `close`       a close call: a cheap model chooses among those candidates only,
                                   at temperature 0, cached by (text, candidates and their
                                   descriptions, prompt, model); unreachable or over budget: the item
                                   keeps every candidate, marked unresolved, asked again next run

A Document's Domains are those covering at least `share` of its tagged Passages (which cover all its
text), so a talk on hiring and pricing gets leadership and gtm with no summary or table of contents.

High-tier Domains (finance, investment) are only *suggested* until confirmed: by a hint (the person
put the Book in that folder or named the Domain on the Source) or by `ytbrain domains review`.
Unconfirmed, they stay out of the index's `domains`, so `pack build` never ships them.

Nothing here re-extracts or re-embeds: results go to data/tags/tags.db (with their origin and
scores, for `ytbrain domains why`) and onto the index's `domains` column in place.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA

TAGS_DIR = DATA / "tags"
TAGS_DB = TAGS_DIR / "tags.db"
SETTINGS_FILE = TAGS_DIR / "settings.json"
TAGGER_VERSION = "1"
EST_COST_PER_CALL = 0.002          # a cheap model, ~40 short texts per call: shown as an estimate only

TIEBREAK_PROMPT = """You sort short texts by subject. For each numbered text, choose the Domain(s) it is
mainly about, ONLY from that text's own candidates. Choose one; choose two only when the text is
equally about both.

Domains:
{domains}

Texts:
{texts}

Answer with JSON only: {{"answers": [{{"n": 1, "domains": ["<a candidate>"]}}, ...]}}, one entry per text."""
TIEBREAK_VERSION = "tb1-" + hashlib.sha256(TIEBREAK_PROMPT.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Settings:
    floor: float = 0.45          # best score below this: nothing fits
    close: float = 0.02          # Domains within this of the best are candidates
    share: float = 0.2           # a Document's Domain covers at least this share of its tagged Passages
    min_passages: int = 2        # ...and at least this many of them, unless it covers half the Document: in a short
                                 # essay one stray Passage would otherwise decide a Domain (20 % of five)
    hint_bonus: float = 0.02     # added to a hinted Domain's score
    batch: int = 40              # texts per tie-break call
    workers: int = 8             # tie-break calls in flight at once (speed only: never part of the result)

    def key(self) -> str:
        return json.dumps({k: v for k, v in self.__dict__.items() if k != "workers"}, sort_keys=True)


def load_settings(path: Path | None = None) -> Settings:
    p = Path(path) if path else SETTINGS_FILE
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return Settings()
    known = {k: raw[k] for k in Settings.__dataclass_fields__ if k in raw}
    return Settings(**known)


def save_settings(s: Settings, path: Path | None = None) -> None:
    from .pages import atomic_write_text
    p = Path(path) if path else SETTINGS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(s.__dict__, indent=2, sort_keys=True) + "\n")


# ----------------------------------------------------------------------------- scoring
def profile_texts(reg) -> list[tuple[str, str]]:
    """(Domain, text) pairs content is scored against: its description and example questions. Aliases are
    names, not content: a one-word text ("investing", "sales") lies close to too many passages, so they only
    match names, folders and proposals (the first dry run on the real Library tagged 37 % of items as close
    calls and suggested `investment` for fundraising talks while aliases were scored)."""
    out = []
    for d in reg.domains.values():
        texts = [t for t in (d.description, *d.examples) if t.strip()] or [d.name, *d.aliases]
        out += [(d.name, t) for t in texts]
    return out


def _unit(v) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def score_matrix(vectors, profiles: list[tuple[str, list[float]]], names: list[str]):
    """scores[i][name] = best cosine of item i with one of the Domain's profile vectors. Uses numpy when
    present (the index extra always has it); plain Python otherwise (tests)."""
    try:
        import numpy as np
        V = np.array(vectors, dtype="float32")          # a copy: the input may be a read-only Arrow view
        V /= np.linalg.norm(V, axis=1, keepdims=True).clip(1e-9)
        P = np.asarray([p for _, p in profiles], dtype="float32")
        P /= np.linalg.norm(P, axis=1, keepdims=True).clip(1e-9)
        S = V @ P.T
        cols = {n: [j for j, (d, _) in enumerate(profiles) if d == n] for n in names}
        best = {n: S[:, c].max(axis=1) for n, c in cols.items() if c}
        return [{n: float(best[n][i]) for n in best} for i in range(len(V))]
    except ImportError:
        P = [(d, _unit(p)) for d, p in profiles]
        out = []
        for v in vectors:
            u = _unit(v)
            row: dict[str, float] = {}
            for d, p in P:
                s = sum(a * b for a, b in zip(u, p))
                row[d] = max(row.get(d, -1.0), s)
            out.append(row)
        return out


def candidates(scores: dict[str, float], hints: set[str], s: Settings) -> list[str]:
    """The Domains an item may belong to: [] when nothing fits, else every Domain within `close` of the
    best (hints get a bonus first). Ties broken by name, so the order is deterministic."""
    if not scores:
        return []
    adj = {d: v + (s.hint_bonus if d in hints else 0.0) for d, v in scores.items()}
    best = max(adj.values())
    if best < s.floor:
        return []
    return sorted((d for d, v in adj.items() if v >= s.floor and v >= best - s.close),
                  key=lambda d: (-adj[d], d))


# ----------------------------------------------------------------------------- the store
class TagDB:
    """data/tags/tags.db: the last run's tags with their origin, the tie-break cache and confirmations."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else TAGS_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS item_tags (item_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL,
                domains TEXT NOT NULL, suggested TEXT NOT NULL, origin TEXT NOT NULL, scores TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS item_tags_doc ON item_tags(doc_id);
            CREATE TABLE IF NOT EXISTS doc_tags (doc_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                domains TEXT NOT NULL, suggested TEXT NOT NULL, origin TEXT NOT NULL, shares TEXT NOT NULL,
                hints TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tiebreak (key TEXT PRIMARY KEY, domains TEXT NOT NULL, model TEXT NOT NULL,
                at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS confirmations (scope TEXT NOT NULL, domain TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY (scope, domain));
            CREATE TABLE IF NOT EXISTS rejections (scope TEXT NOT NULL, domain TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY (scope, domain));
        """)

    def meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def cached(self, keys: list[str]) -> dict[str, list[str]]:
        out = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = "SELECT key, domains FROM tiebreak WHERE key IN (" + ",".join("?" * len(chunk)) + ")"
            out.update({r["key"]: json.loads(r["domains"]) for r in self.db.execute(q, chunk)})
        return out

    def cache(self, rows: list[tuple[str, list[str]]], model: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.db.executemany("INSERT OR REPLACE INTO tiebreak VALUES (?,?,?,?)",
                            [(k, json.dumps(v), model, now) for k, v in rows])
        self.db.commit()

    def confirmations(self) -> set[tuple[str, str]]:
        return {(r["scope"], r["domain"]) for r in self.db.execute("SELECT scope, domain FROM confirmations")}

    def rejections(self) -> set[tuple[str, str]]:
        return {(r["scope"], r["domain"]) for r in self.db.execute("SELECT scope, domain FROM rejections")}

    def reject(self, scopes: list[str], domain: str) -> int:
        """A person's "no": the tagger never suggests this Domain in these scopes again (and a confirmation
        of the same scope is withdrawn, the later answer wins)."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.db:
            self.db.executemany("DELETE FROM confirmations WHERE scope=? AND domain=?", [(s, domain) for s in scopes])
            cur = self.db.executemany("INSERT OR IGNORE INTO rejections VALUES (?,?,?)", [(s, domain, now) for s in scopes])
        return cur.rowcount

    def confirm(self, scopes: list[str], domain: str) -> int:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.db:
            self.db.executemany("DELETE FROM rejections WHERE scope=? AND domain=?", [(s, domain) for s in scopes])
            cur = self.db.executemany("INSERT OR IGNORE INTO confirmations VALUES (?,?,?)",
                                      [(s, domain, now) for s in scopes])
        return cur.rowcount

    def replace(self, items: dict, docs: dict, meta: dict) -> None:
        """The whole run's results in one transaction: a reader sees the old tags or the new, never half."""
        with self.db:
            self.db.execute("DELETE FROM item_tags")
            self.db.execute("DELETE FROM doc_tags")
            self.db.executemany("INSERT INTO item_tags VALUES (?,?,?,?,?,?)", [
                (i, t.doc_id, json.dumps(list(t.domains)), json.dumps(list(t.suggested)), t.origin,
                 json.dumps(t.scores)) for i, t in items.items()])
            self.db.executemany("INSERT INTO doc_tags VALUES (?,?,?,?,?,?,?)", [
                (d, t.source_id, json.dumps(list(t.domains)), json.dumps(list(t.suggested)), t.origin,
                 json.dumps(t.shares), json.dumps(list(t.hints))) for d, t in docs.items()])
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", list(meta.items()))

    @property
    def applied(self) -> bool:
        """The last run's tags are on the index (not just stored for review with --no-apply)."""
        return self.meta("applied") == "1"

    def tagged_docs(self) -> set[str]:
        return {r["doc_id"] for r in self.db.execute("SELECT doc_id FROM doc_tags")}

    def item_domains(self, doc_id: str | None = None) -> dict[str, tuple[str, ...]]:
        q, a = ("SELECT item_id, domains FROM item_tags WHERE doc_id=?", (doc_id,)) if doc_id else \
            ("SELECT item_id, domains FROM item_tags", ())
        return {r["item_id"]: tuple(json.loads(r["domains"])) for r in self.db.execute(q, a)}

    def doc(self, doc_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM doc_tags WHERE doc_id=?", (doc_id,)).fetchone()

    def docs(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM doc_tags ORDER BY doc_id"))

    def items_of(self, doc_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM item_tags WHERE doc_id=? ORDER BY item_id", (doc_id,)))

    def close(self) -> None:
        self.db.close()


def stored_item_domains(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """item_id -> Domains from the last tag run ({} before the first). `ytbrain index` keeps them on
    items it re-embeds, so re-indexing a Document doesn't undo its tags."""
    p = Path(path) if path else TAGS_DB
    if not p.exists():
        return {}
    db = TagDB(p)
    try:
        return db.item_domains() if db.applied else {}
    finally:
        db.close()


def tagged_docs(path: Path | None = None) -> set[str]:
    p = Path(path) if path else TAGS_DB
    if not p.exists():
        return set()
    db = TagDB(p)
    try:
        return db.tagged_docs() if db.applied else set()
    finally:
        db.close()


# ----------------------------------------------------------------------------- the run
@dataclass
class ItemTag:
    doc_id: str
    domains: tuple[str, ...]
    suggested: tuple[str, ...] = ()
    origin: str = "embedding"      # embedding | tiebreak | unresolved | inherited | document
    scores: dict = field(default_factory=dict)


@dataclass
class DocTag:
    source_id: str
    domains: tuple[str, ...]
    suggested: tuple[str, ...] = ()
    origin: str = "passages"       # passages | items | hint | default
    shares: dict = field(default_factory=dict)
    hints: tuple[str, ...] = ()


@dataclass
class Result:
    items: dict[str, ItemTag]
    docs: dict[str, DocTag]
    close_calls: int = 0           # items needing a tie-break
    asked: int = 0                 # of them, answered by the model this run
    cached: int = 0                # of them, answered from the cache
    unresolved: int = 0            # of them, kept every candidate (model unreachable, over budget, bad answer)
    undecided: int = 0             # of them, ones the model already left undecided (cached; not asked again)
    calls: int = 0
    cost: float = 0.0
    est_calls: int = 0             # dry run: calls the uncached close calls would need

    @property
    def awaiting_review(self) -> int:
        return sum(len(d.suggested) for d in self.docs.values())

    @property
    def unplaced(self) -> list[str]:
        """Documents whose Domains came from no content at all (only a hint or the default)."""
        return sorted(d for d, t in self.docs.items() if t.origin in ("hint", "default"))


def tiebreak_key(text: str, cands: list[str], reg, model: str) -> str:
    """Only what the answer depends on: a new, unrelated Domain doesn't re-ask anything."""
    desc = [[d, reg.get(d).description, list(reg.get(d).examples)] + ([reg.get(d).not_about] if reg.get(d).not_about else [])
            for d in sorted(cands)]
    blob = json.dumps([text, desc, TIEBREAK_VERSION, model], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


AskBatch = Callable[[list[tuple[str, list[str]]]], tuple[list[list[str] | None], float, int]]


def llm_tiebreak(reg, model: str) -> AskBatch:
    """A batch of (text, candidates) -> ([chosen Domains or None per text], cost, calls), via the same
    OpenAI-compatible endpoint and backoff as extraction (ytbrain/eval/llm.py)."""
    from pydantic import BaseModel

    from .eval import llm

    class One(BaseModel):
        n: int
        domains: list[str]

    class Batch(BaseModel):
        answers: list[One]

    def ask(batch):
        names = sorted({d for _, c in batch for d in c})
        doms = "\n".join(f"- {d}: {reg.get(d).description}" + (f" Not about: {reg.get(d).not_about}" if reg.get(d).not_about else "")
                         for d in names)
        texts = "\n\n".join(f"[{i}] candidates: {', '.join(c)}\n{t[:700]}" for i, (t, c) in enumerate(batch, 1))
        a = llm.ask(TIEBREAK_PROMPT.format(domains=doms, texts=texts), Batch, model)
        out: list[list[str] | None] = [None] * len(batch)
        for ans in (a.value.answers if a.value else []):
            if 1 <= ans.n <= len(batch):
                chosen = [d for d in dict.fromkeys(ans.domains) if d in batch[ans.n - 1][1]]
                out[ans.n - 1] = chosen or None
        return out, a.cost, a.calls
    return ask


def run(rows: list[dict], vectors, reg, embed: Callable[[list[str]], list], hints: dict[str, list[str]],
        settings: Settings | None = None, ask: AskBatch | None = None, db: TagDB | None = None,
        model: str = "", max_cost: float | None = None, dry_run: bool = False, say=print,
        retry_undecided: bool = False) -> Result:
    """Tag every item in `rows` with `vectors` (KnowledgeStore.tag_inputs). `hints`: doc_id -> configured
    Domains (never the registry default). `ask`: the tie-break model, or None to leave close calls
    unresolved. Pure apart from `db` (cache reads always; writes only when not a dry run)."""
    s = settings or Settings()
    names = reg.names
    prof = profile_texts(reg)
    pvecs = embed([t for _, t in prof])
    scores = score_matrix(vectors, [(d, v) for (d, _), v in zip(prof, pvecs)], names) if len(rows) else []
    confirmed = db.confirmations() if db else set()
    rejected = db.rejections() if db else set()

    cands: dict[str, list[str]] = {}
    for r, sc in zip(rows, scores):
        cands[r["item_id"]] = candidates(sc, set(hints.get(r["doc_id"]) or ()), s)
    close = [r for r in rows if len(cands[r["item_id"]]) > 1]
    keys = {r["item_id"]: tiebreak_key(r.get("text") or "", cands[r["item_id"]], reg, model) for r in close}
    have = db.cached(list(keys.values())) if db else {}
    res = Result({}, {}, close_calls=len(close))
    chosen: dict[str, tuple[list[str], str]] = {}
    todo = []
    for r in close:
        k = keys[r["item_id"]]
        if k in have and set(have[k]) <= set(cands[r["item_id"]]) and have[k]:
            chosen[r["item_id"]] = (have[k], "tiebreak")
            res.cached += 1
        elif k in have and not have[k] and not retry_undecided:
            res.undecided += 1                 # the model already said it can't choose: keep both, don't pay again
        else:
            todo.append(r)
    res.est_calls = math.ceil(len(todo) / s.batch) if todo else 0
    if todo and ask and not dry_run:
        # In parallel, with a progress line, each batch's answers cached the moment they arrive: an interrupt
        # or a failure loses at most the batches in flight, and the next run asks only what is left.
        from .eval.llm import Budget, run_parallel
        from .extract.runner import BackendUnavailable
        budget = Budget(max_cost if max_cost is not None else float("inf"))
        batches = [todo[i:i + s.batch] for i in range(0, len(todo), s.batch)]
        failed = {"n": 0, "why": ""}

        def work(part):
            result = ask([(r.get("text") or "", cands[r["item_id"]]) for r in part])
            budget.add(result[1])                         # in the worker: the next batch sees the spend at once
            return result

        def on_result(part, result, err):
            if err is not None:
                failed["n"] += 1
                failed["why"] = failed["why"] or f"{type(err).__name__}: {str(err)[:120]}"
                return
            answers, cost, calls = result
            res.cost += cost
            res.calls += calls
            new = []
            for r, a in zip(part, answers):
                if a:
                    chosen[r["item_id"]] = (a, "tiebreak")
                    res.asked += 1
                new.append((keys[r["item_id"]], list(a or [])))     # [] = undecided: cached too, asked again
                                                                    # only with --retry-undecided
            if db and new:
                db.cache(new, model)
        say(f"tag: asking {model} about {len(todo)} close call(s) in {len(batches)} batch(es), "
            f"{s.workers} at a time" + (f", up to ${max_cost:g}" if max_cost is not None else ""))
        try:
            stopped = run_parallel(batches, work, on_result, workers=s.workers, budget=budget, label="tag tie-breaks")
        except BackendUnavailable as e:                 # bad key, model or credit: nothing more can work
            stopped = f"backend: {e}"
        if failed["n"]:
            say(f"tag: tie-break unavailable for {failed['n']} batch(es) ({failed['why']}); their close calls keep "
                f"every candidate until the next run")
        if stopped:
            say(f"tag: stopped asking ({stopped}); the close calls not answered keep every candidate until the "
                f"next run (answers so far are cached)")

    # items first (a None means: nothing fits, take the Document's Domains later)
    by_doc: dict[str, list[dict]] = {}
    for r, sc in zip(rows, scores):
        by_doc.setdefault(r["doc_id"], []).append(r)
        c = cands[r["item_id"]]
        top = dict(sorted(sc.items(), key=lambda kv: -kv[1])[:3])
        top = {d: round(v, 4) for d, v in top.items()}
        if not c:
            res.items[r["item_id"]] = ItemTag(r["doc_id"], (), origin="inherited", scores=top)
        elif len(c) == 1:
            res.items[r["item_id"]] = ItemTag(r["doc_id"], (c[0],), scores=top)
        elif r["item_id"] in chosen:
            res.items[r["item_id"]] = ItemTag(r["doc_id"], tuple(chosen[r["item_id"]][0]), origin="tiebreak",
                                              scores=top)
        else:
            res.items[r["item_id"]] = ItemTag(r["doc_id"], tuple(c), origin="unresolved", scores=top)
            res.unresolved += 1

    def is_confirmed(domain: str, doc_id: str, source_id: str, hinted: set[str]) -> bool:
        return (reg.get(domain).risk_tier != "high" or domain in hinted
                or (f"doc:{doc_id}", domain) in confirmed or (f"source:{source_id}", domain) in confirmed)

    def is_rejected(domain: str, doc_id: str, source_id: str, hinted: set[str]) -> bool:
        """You said no for this Document or Source (a hint, which you configured yourself, still wins)."""
        return domain not in hinted and ((f"doc:{doc_id}", domain) in rejected or
                                         (f"source:{source_id}", domain) in rejected)

    for doc_id, items in by_doc.items():
        source_id = next((r.get("source_id") for r in items if r.get("source_id")), "") or ""
        hinted = set(hints.get(doc_id) or ())
        psg = [res.items[r["item_id"]] for r in items if r.get("kind") == "passage"]
        tagged = [t for t in psg if t.domains]
        counts = Counter(d for t in tagged for d in t.domains)
        shares = {d: round(n / len(tagged), 3) for d, n in counts.items()} if tagged else {}
        need = min(s.min_passages, len(tagged))
        doms = sorted((d for d, v in shares.items() if v >= s.share and (counts[d] >= need or v >= 0.5)),
                      key=lambda d: (-shares[d], d))
        origin = "passages"
        if not doms:
            other = Counter(d for r in items if r.get("kind") != "passage" for d in res.items[r["item_id"]].domains)
            if other:
                top = max(other.values())
                doms, origin = sorted(d for d, n in other.items() if n == top), "items"
        if not doms:
            doms, origin = (sorted(hinted), "hint") if hinted else ([reg.default], "default")
        doms = [d for d in doms if not is_rejected(d, doc_id, source_id, hinted)]
        ok = tuple(d for d in doms if is_confirmed(d, doc_id, source_id, hinted))
        sugg = tuple(d for d in doms if d not in ok)
        if not ok:
            ok = tuple(sorted(hinted)) or (reg.default,)
        res.docs[doc_id] = DocTag(source_id, ok, sugg, origin, shares, tuple(sorted(hinted)))
        for r in items:
            t = res.items[r["item_id"]]
            if not t.domains:
                t.domains = ok
            elif r.get("kind") == "summary":
                t.domains, t.origin = ok, "document"
            keep = tuple(d for d in t.domains if is_confirmed(d, doc_id, source_id, hinted))
            t.suggested = tuple(d for d in t.domains if d not in keep and not is_rejected(d, doc_id, source_id, hinted))
            t.domains = keep or ok
    return res


def gate(reg, s: Settings, path: Path | None = None) -> str | None:
    """None when your labelled set passed for this tagger, Domain list and settings (data/eval/tags.json),
    else why not. `ytbrain ops` applies tags to the index only then: untuned thresholds never reach a pack."""
    from .config import EVAL_DATA
    p = Path(path) if path else EVAL_DATA / "tags.json"
    try:
        r = json.loads(p.read_text())
    except (OSError, ValueError):
        return "no `ytbrain eval tags` result yet (ytbrain domains sample, label the sheets, ytbrain eval tags)"
    if not r.get("passed"):
        return "the last `ytbrain eval tags` failed its gate"
    if r.get("tagger_version") != TAGGER_VERSION or r.get("domain_list") != reg.fingerprint() \
            or r.get("settings") != s.key():
        return "the tagger, the Domain list or the thresholds changed since the last passing `ytbrain eval tags`"
    return None


def meta_for(reg, s: Settings, embed_model: str, model: str) -> dict:
    return {"tagger_version": TAGGER_VERSION, "domain_list": reg.fingerprint(), "settings": s.key(),
            "embed_model": embed_model, "tiebreak_model": model, "tiebreak_prompt": TIEBREAK_VERSION,
            "tagged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ----------------------------------------------------------------------------- new Domains (`domains propose`)
PROPOSE_PROMPT = """These texts from a personal knowledge Library fit none of its Domains well. Read them
and say what single subject they share, then compare it with the existing Domains.

Existing Domains (name: description; other names):
{domains}

Texts:
{texts}

Answer with JSON only:
{{"name": "<a short lowercase name for the shared subject>", "description": "<one line: what it covers>",
 "examples": ["<three questions a person would ask about it>"], "risk_tier": "low|medium|high",
 "relation": "same|narrower|new", "existing": "<the existing Domain it is the same as or narrower than, or null>"}}
"relation" is "same" when an existing Domain already covers this subject under another name, "narrower" when
it is one part of an existing Domain, "new" only when no existing Domain covers it. Give "risk_tier" high when a
wrong answer could cost money, health or legal trouble."""


def clusters(vectors, ids: list[str], join: float = 0.75, min_size: int = 15, top: int = 10) -> list[list[int]]:
    """Deterministic leader clustering of unit vectors (items in id order; a vector joins the closest
    cluster whose centroid is at least `join` away by cosine, else starts one). The `top` biggest with
    at least `min_size` members, as lists of row positions."""
    import numpy as np
    V = np.array(vectors, dtype="float32")
    V /= np.linalg.norm(V, axis=1, keepdims=True).clip(1e-9)
    order = sorted(range(len(ids)), key=lambda i: ids[i])
    sums: list = []
    members: list[list[int]] = []
    for i in order:
        if sums:
            C = np.stack(sums)
            C = C / np.linalg.norm(C, axis=1, keepdims=True).clip(1e-9)
            sims = C @ V[i]
            j = int(sims.argmax())
            if sims[j] >= join:
                sums[j] = sums[j] + V[i]
                members[j].append(i)
                continue
        sums.append(V[i].copy())
        members.append([i])
    big = [m for m in members if len(m) >= min_size]
    return sorted(big, key=lambda m: (-len(m), min(ids[i] for i in m)))[:top]


@dataclass
class Proposal:
    size: int
    name: str
    description: str
    examples: list[str]
    risk_tier: str
    verdict: str                   # new | alias of X | narrower than X | already X
    nearest: str
    nearest_score: float
    texts: list[str]


def propose(rows: list[dict], vectors, reg, embed, ask_json: Callable[[str], dict | None], tags: dict[str, str],
            join: float = 0.75, min_size: int = 15, meaning: float = 0.85) -> list[Proposal]:
    """Group the items nothing fit (`tags`: item_id -> origin) and run the four checks on each group's
    proposed subject (ADR-0017): normalised name, aliases, meaning (embedding), and the model's own
    same/narrower/new against the whole list. Nothing is added: the person decides."""
    import numpy as np

    from . import domains as D
    pick = [i for i, r in enumerate(rows) if tags.get(r["item_id"]) == "inherited" and r.get("kind") != "summary"]
    if not pick:
        return []
    V = np.array(vectors, dtype="float32")[pick]
    ids = [rows[i]["item_id"] for i in pick]
    prof = profile_texts(reg)
    P = np.array(embed([t for _, t in prof]), dtype="float32")
    P /= np.linalg.norm(P, axis=1, keepdims=True).clip(1e-9)
    listing = "\n".join(f"- {d.name}: {d.description}" + (f" Not about: {d.not_about}" if d.not_about else "")
                        + (f" (also: {', '.join(d.aliases)})" if d.aliases else "") for d in reg.domains.values())
    out = []
    for group in clusters(V, ids, join, min_size):
        G = V[group] / np.linalg.norm(V[group], axis=1, keepdims=True).clip(1e-9)
        c = G.mean(axis=0)
        near = sorted(range(len(group)), key=lambda k: -float(G[k] @ c))[:8]
        texts = [(rows[pick[group[k]]].get("text") or "")[:500] for k in near]
        ans = ask_json(PROPOSE_PROMPT.format(domains=listing, texts="\n\n".join(f"[{n}] {t}" for n, t in enumerate(texts, 1))))
        if not ans or not str(ans.get("name") or "").strip():
            continue
        name = D.folder_key(str(ans["name"]))
        desc = str(ans.get("description") or "").strip()
        dv = np.array(embed([desc or name])[0], dtype="float32")
        dv /= max(float(np.linalg.norm(dv)), 1e-9)
        sims = P @ dv
        j = int(sims.argmax())
        nearest, score = prof[j][0], float(sims[j])
        if (owner := reg.lookup(name)):                                      # checks 1 and 2: spelling, aliases
            verdict = f"already {owner}"
        elif score >= meaning:                                               # check 3: same meaning
            verdict = f"alias of {nearest}"
        elif ans.get("relation") in ("same", "narrower") and ans.get("existing") in reg:   # check 4: the model
            verdict = ("alias of " if ans["relation"] == "same" else "narrower than ") + ans["existing"]
        else:
            verdict = "new"
        out.append(Proposal(len(group), name, desc, [str(e) for e in (ans.get("examples") or [])][:3],
                            str(ans.get("risk_tier") or "medium"), verdict, nearest, round(score, 3), texts[:3]))
    return out


# ----------------------------------------------------------------------------- the labelled set (`eval tags`)
SHEET_COLUMNS = ["item_id", "kind", "title", "text", "predicted", "suggested", "label"]
DOC_COLUMNS = ["doc_id", "title", "predicted", "suggested", "label"]


def _pick(keys: list[str], n: int, seed: int) -> list[str]:
    """A fixed sample: the n keys with the smallest hash under the seed (same input, same sample)."""
    return sorted(keys, key=lambda k: hashlib.sha256(f"{seed}:{k}".encode()).hexdigest())[:n]


def sample_sheets(db: TagDB, titles: dict[str, str], texts: dict[str, str], n_items: int = 100, n_docs: int = 30,
                  seed: int = 7):
    """(item rows, document rows) for the labelling sheets: items spread over kinds, every predicted
    Domain represented, never summaries (they take their Document's Domains)."""
    items = [r for r in db.db.execute("SELECT * FROM item_tags WHERE origin != 'document' ORDER BY item_id")]
    by_dom: dict[str, list] = {}
    for r in items:
        for d in json.loads(r["domains"]) + json.loads(r["suggested"]):
            by_dom.setdefault(d, []).append(r)
    chosen: dict[str, sqlite3.Row] = {}
    per = max(1, n_items // max(1, 2 * len(by_dom)))
    for d in sorted(by_dom):                                   # half spread over Domains, so rare ones are seen
        for k in _pick([r["item_id"] for r in by_dom[d]], per, seed):
            chosen.setdefault(k, next(r for r in by_dom[d] if r["item_id"] == k))
    rest = [r["item_id"] for r in items if r["item_id"] not in chosen]
    rid = {r["item_id"]: r for r in items}
    for k in _pick(rest, max(0, n_items - len(chosen)), seed):
        chosen[k] = rid[k]
    item_rows = []
    for k in sorted(chosen)[:n_items]:
        r = chosen[k]
        item_rows.append({"item_id": k, "kind": k.split(":", 1)[0], "title": titles.get(r["doc_id"], r["doc_id"]),
                          "text": " ".join((texts.get(k) or "").split())[:500],
                          "predicted": "|".join(json.loads(r["domains"])),
                          "suggested": "|".join(json.loads(r["suggested"])), "label": ""})
    docs = [r for r in db.docs()]
    doc_rows = []
    for k in _pick([r["doc_id"] for r in docs], n_docs, seed):
        r = next(x for x in docs if x["doc_id"] == k)
        doc_rows.append({"doc_id": k, "title": titles.get(k, k), "predicted": "|".join(json.loads(r["domains"])),
                         "suggested": "|".join(json.loads(r["suggested"])), "label": ""})
    return item_rows, sorted(doc_rows, key=lambda r: r["doc_id"])


GATE = {"precision": 0.90, "recall": 0.85, "high_missed": 0, "documents": 0.90}


def current(db: TagDB) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """The last run's tags: (item_id -> Domains and suggestions, doc_id -> the same)."""
    items = {r["item_id"]: set(json.loads(r["domains"])) | set(json.loads(r["suggested"]))
             for r in db.db.execute("SELECT * FROM item_tags")}
    docs = {r["doc_id"]: set(json.loads(r["domains"])) | set(json.loads(r["suggested"])) for r in db.docs()}
    return items, docs


def from_result(res: Result) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    return ({i: set(t.domains) | set(t.suggested) for i, t in res.items.items()},
            {d: set(t.domains) | set(t.suggested) for d, t in res.docs.items()})


def score_labels(item_labels: dict[str, set[str]], doc_labels: dict[str, set[str]],
                 got_items: dict[str, set[str]], got_docs: dict[str, set[str]], reg) -> dict:
    """Tags against your labels. An item's tags are its Domains plus its suggested ones (a suggestion is
    shown for review, so it counts toward recall and toward no high-tier miss)."""
    tp = fp = fn = 0
    missed_high, missing = [], []
    for item_id, want in item_labels.items():
        if item_id not in got_items:
            missing.append(item_id)
            continue
        have = got_items[item_id]
        tp += len(have & want)
        fp += len(have - want)
        fn += len(want - have)
        missed_high += [f"{item_id}: {d}" for d in sorted(want - have) if d in reg and reg.get(d).risk_tier == "high"]
    docs_ok = docs_n = 0
    for doc_id, want in doc_labels.items():
        if doc_id not in got_docs:
            missing.append(doc_id)
            continue
        docs_n += 1
        docs_ok += int(want <= got_docs[doc_id] and len(got_docs[doc_id] - want) <= 1)
    n = sum(1 for i in item_labels if i in got_items)
    out = {"items": n, "precision": round(tp / (tp + fp), 4) if tp + fp else 0.0,
           "recall": round(tp / (tp + fn), 4) if tp + fn else 0.0, "high_missed": missed_high,
           "documents": round(docs_ok / docs_n, 4) if docs_n else None, "documents_n": docs_n, "missing": missing}
    out["passed"] = bool(n and out["precision"] >= GATE["precision"] and out["recall"] >= GATE["recall"]
                         and not missed_high and (out["documents"] is None or out["documents"] >= GATE["documents"]))
    return out


TUNE_GRID = {"floor": [0.35, 0.40, 0.45, 0.50, 0.55, 0.60], "close": [0.01, 0.02, 0.04], "share": [0.1, 0.2, 0.3],
             "min_passages": [1, 2]}


def tune(rows, vectors, reg, embed, hints, item_labels, doc_labels, db: TagDB | None, model: str,
         base: Settings | None = None) -> list[tuple[Settings, dict]]:
    """Every setting in TUNE_GRID scored against your labels, with cached tie-breaks only (no model call),
    best first: passing the gate first, then the higher of precision + recall, then the lower floor."""
    base = base or load_settings()
    cache: dict[str, list[float]] = {}

    def emb(texts):
        missing = [t for t in texts if t not in cache]
        if missing:
            cache.update(zip(missing, embed(missing)))
        return [cache[t] for t in texts]
    out = []
    for floor in TUNE_GRID["floor"]:
        for close in TUNE_GRID["close"]:
            for share in TUNE_GRID["share"]:
                for minp in TUNE_GRID["min_passages"]:
                    s = Settings(floor=floor, close=close, share=share, min_passages=minp,
                                 hint_bonus=base.hint_bonus, batch=base.batch)
                    res = run(rows, vectors, reg, emb, hints, s, None, db, model, dry_run=True, say=lambda m: None)
                    out.append((s, score_labels(item_labels, doc_labels, *from_result(res), reg)))
    return sorted(out, key=lambda x: (not x[1]["passed"], -(x[1]["precision"] + x[1]["recall"]), x[0].floor))


def read_labels(path: Path, key: str, reg) -> tuple[dict[str, set[str]], list[str]]:
    """{id: labelled Domains} from a filled sheet (rows with an empty label are skipped), and problems:
    a label that isn't a Domain or one of its aliases."""
    import csv
    out, bad = {}, []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get("label") or "").strip()
            if not raw:
                continue
            names = set()
            for part in raw.replace(",", "|").split("|"):
                if not part.strip():
                    continue
                d = reg.lookup(part)
                if d:
                    names.add(d)
                else:
                    bad.append(f"{row.get(key)}: {part.strip()!r} is not a Domain")
            if names:
                out[row[key]] = names
    return out, bad
