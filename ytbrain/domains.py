"""Domains: the named subject areas of the Library (CONTEXT.md: Domain; docs/library-and-packs-plan.md).

`domains.yaml` declares them; a Source tags its Documents with `domains: [...]`, and a Book in a
folder named after a Domain belongs to it. Tags are configuration, not content: changing one
re-tags the index in place (`ytbrain index`), it never re-fetches or re-extracts anything.

A Document may belong to several Domains, and a question may touch several: search takes a list
of Domains and matches an item in any of them.

Resolution order for a Document (`resolve`): the Book's own `domains` in sources.yaml, then the
nearest folder named after a Domain, then the Source's `domains`, then the registry's default.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from founder_coach.search import DEFAULT_DOMAIN

DOMAINS_FILE = Path(os.environ.get("YTBRAIN_DOMAINS_FILE") or Path(__file__).resolve().parents[1] / "domains.yaml")
RISK_TIERS = ("low", "medium", "high")
WEB_POLICIES = ("never", "when_gap", "always_latest")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class DomainConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Domain:
    name: str
    description: str = ""
    examples: tuple[str, ...] = ()
    risk_tier: str = "low"
    half_life_days: int | None = None          # None: evergreen
    web_policy: str = "when_gap"


@dataclass(frozen=True)
class Registry:
    domains: dict[str, Domain] = field(default_factory=dict)
    default: str = DEFAULT_DOMAIN
    ignore_folders: tuple[str, ...] = ()       # book sub-folders that are only for sorting, not Domains

    @property
    def names(self) -> list[str]:
        return list(self.domains)

    def __contains__(self, name: str) -> bool:
        return name in self.domains

    def get(self, name: str) -> Domain:
        return self.domains[name]

    def check(self, names, where: str) -> list[str]:
        """`names` as a clean list, or DomainConfigError naming the unknown ones and the known ones."""
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, (list, tuple)) or not all(isinstance(n, str) for n in names):
            raise DomainConfigError(f"{where}: domains must be a list of Domain names")
        out = list(dict.fromkeys(n.strip() for n in names if n.strip()))
        unknown = [n for n in out if n not in self.domains]
        if unknown:
            raise DomainConfigError(f"{where}: unknown Domain {', '.join(map(repr, unknown))}; "
                                    f"declared in domains.yaml: {', '.join(self.names)}")
        return out


def builtin() -> Registry:
    """What exists without a domains.yaml: the one Domain every older index already belongs to."""
    return Registry({DEFAULT_DOMAIN: Domain(DEFAULT_DOMAIN, "Starting and running an early-stage startup.",
                                            risk_tier="medium")}, DEFAULT_DOMAIN)


def parse(doc: dict | None) -> Registry:
    doc = doc or {}
    raw = doc.get("domains")
    if not isinstance(raw, dict) or not raw:
        raise DomainConfigError("domains.yaml: `domains:` must map each Domain name to its settings")
    out: dict[str, Domain] = {}
    for name, cfg in raw.items():
        where = f"domains.yaml: {name!r}"
        if not isinstance(name, str) or not _NAME.match(name):
            raise DomainConfigError(f"{where}: a Domain name is lowercase letters, digits and dashes (max 40)")
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            raise DomainConfigError(f"{where}: settings must be a mapping")
        unknown = set(cfg) - {"description", "examples", "risk_tier", "freshness", "web_policy"}
        if unknown:
            raise DomainConfigError(f"{where}: unknown setting(s) {sorted(unknown)}")
        tier = cfg.get("risk_tier", "low")
        if tier not in RISK_TIERS:
            raise DomainConfigError(f"{where}: risk_tier must be one of {', '.join(RISK_TIERS)}")
        policy = cfg.get("web_policy", "when_gap")
        if policy not in WEB_POLICIES:
            raise DomainConfigError(f"{where}: web_policy must be one of {', '.join(WEB_POLICIES)}")
        fresh = cfg.get("freshness", "evergreen")
        if fresh == "evergreen":
            half = None
        elif isinstance(fresh, int) and not isinstance(fresh, bool) and fresh > 0:
            half = fresh
        else:
            raise DomainConfigError(f"{where}: freshness is `evergreen` or a number of days (the half-life)")
        examples = cfg.get("examples") or []
        if not isinstance(examples, list) or not all(isinstance(e, str) and e.strip() for e in examples):
            raise DomainConfigError(f"{where}: examples must be a list of questions")
        out[name] = Domain(name, str(cfg.get("description") or "").strip(), tuple(e.strip() for e in examples),
                           tier, half, policy)
    default = doc.get("default") or next(iter(out))
    if default not in out:
        raise DomainConfigError(f"domains.yaml: default {default!r} is not one of the declared Domains")
    ignore = doc.get("ignore_folders") or []
    if not isinstance(ignore, list) or not all(isinstance(n, str) and n.strip() for n in ignore):
        raise DomainConfigError("domains.yaml: ignore_folders must be a list of folder names")
    return Registry(out, default, tuple(dict.fromkeys(n.strip() for n in ignore)))


def load(path: Path | None = None) -> Registry:
    """The registry from domains.yaml; the built-in single-Domain one when the file doesn't exist."""
    import yaml
    path = Path(path) if path else DOMAINS_FILE
    if not path.exists():
        return builtin()
    try:
        return parse(yaml.safe_load(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as e:
        raise DomainConfigError(f"{path.name}: not valid YAML ({e})") from None


def check_sources(registry: Registry, sources: list[dict]) -> None:
    """Every Domain a Source (or one of its Books) names must be declared."""
    for src in sources:
        where = f"source {src.get('id')}"
        if "domains" in src:
            registry.check(src["domains"], where)
        for fname, fields in (src.get("books") or {}).items():
            if "domains" in (fields or {}):
                registry.check(fields["domains"], f"{where}, book {fname!r}")


def folder_domain(path: str | Path | None, registry: Registry, roots: list[Path] | tuple = ()) -> str | None:
    """The Domain named by the nearest folder above a file (data/books/leadership/x.pdf -> leadership).
    With `roots` (the Source's own folders) only folders below the one holding the file count, so a
    parent directory that happens to be called `finance` doesn't tag every Book. A plan written before
    the repo was moved still matches by the last two parts of the Source's folder (`data/books`)."""
    if not path:
        return None
    path = Path(path)
    parts = path.parts[:-1]
    if roots:
        parts = None
        for root in roots:
            try:
                parts = path.relative_to(root).parts[:-1]
                break
            except ValueError:
                pass
        if parts is None:
            for root in roots:
                tail = root.parts[-2:]
                for i in range(len(path.parts) - len(tail), -1, -1):
                    if tuple(path.parts[i:i + len(tail)]) == tuple(tail):
                        parts = path.parts[i + len(tail):-1]
                        break
                if parts is not None:
                    break
        if parts is None:
            return None
    for part in reversed(parts):
        if folder_key(part) in registry:
            return folder_key(part)
    return None


def folder_key(folder: str) -> str:
    """The Domain name a folder stands for: `GTM`, `System Design` and `system_design` are `gtm` and
    `system-design`, so a folder's capitals or spaces never make its Books miss their Domain."""
    return re.sub(r"[\s_]+", "-", folder.strip().lower())


def stray_folders(registry: Registry, source: dict, files, roots) -> dict[str, int]:
    """Folders below a Source's root that hold Books but are not a declared Domain: {folder: Books}.
    Their Books silently fall back to the Source's Domains or the default, which is how a typo
    (`system-desing/`) or an undeclared `coding/` mis-tags a Book. Not counted: a Book that names its
    own `domains`, a Book with a declared Domain folder above it, and `ignore_folders` in domains.yaml."""
    out: dict[str, int] = {}
    own = source.get("books") or {}
    # book_files() returns resolved paths; a Source folder reached through a symlink (macOS /var -> /private/var,
    # a linked books folder) must match them too, or the warning and --strict-domains stay silent
    roots = list(dict.fromkeys([*roots, *(Path(r).resolve() for r in roots)]))
    for f in files:
        if (own.get(Path(f).name) or {}).get("domains") or folder_domain(f, registry, roots):
            continue
        rel = None
        for root in roots:
            try:
                rel = Path(f).relative_to(root).parts[:-1]
                break
            except ValueError:
                pass
        if not rel or any(folder_key(part) in {folder_key(i) for i in registry.ignore_folders} for part in rel):
            continue
        out[rel[0]] = out.get(rel[0], 0) + 1
    return out


def resolve(registry: Registry, source: dict | None = None, book_path: str | Path | None = None,
            roots: list[Path] | tuple = ()) -> list[str]:
    """The Domains of one Document (see the module docstring for the order)."""
    source = source or {}
    if book_path:
        fields = (source.get("books") or {}).get(Path(book_path).name) or {}
        if fields.get("domains"):
            return registry.check(fields["domains"], f"book {Path(book_path).name!r}")
        folder = folder_domain(book_path, registry, roots)
        if folder:
            return [folder]
    if source.get("domains"):
        return registry.check(source["domains"], f"source {source.get('id')}")
    return [registry.default]


# ----------------------------------------------------------------------------- editing domains.yaml
DEFAULT_FILE_HEAD = """# The Library's Domains (CONTEXT.md: Domain). `ytbrain domains add` appends to this file.
default: startup
ignore_folders: []
domains:
  startup:
    description: "Starting and running an early-stage startup."
    risk_tier: medium
"""


def _q(text: str) -> str:
    import json
    return json.dumps(text, ensure_ascii=False)      # a JSON string is a valid YAML double-quoted scalar


def _write_checked(path: Path, text: str) -> Registry:
    """Parse the new text before it replaces the file (atomically): a mistake never leaves a broken domains.yaml."""
    import yaml
    from .pages import atomic_write_text
    try:
        reg = parse(yaml.safe_load(text))
    except yaml.YAMLError as e:
        raise DomainConfigError(f"{path.name}: the edit would not be valid YAML ({e}); nothing was changed") from None
    atomic_write_text(path, text)
    return reg


def add_domain(name: str, *, risk_tier: str, description: str, examples: list[str] | tuple = (),
               freshness: int | str = "evergreen", web_policy: str = "when_gap", path: Path | None = None) -> Registry:
    """Append one Domain to domains.yaml, keeping every comment and the order of the others. The risk tier and a
    one-line description are required: the tier decides how much evidence a confident answer needs (a `high`
    Domain declines below strong coverage), and the description is what the host reads to choose Domains."""
    path = Path(path) if path else DOMAINS_FILE
    name = folder_key(name)
    if not _NAME.match(name):
        raise DomainConfigError(f"{name!r}: a Domain name is lowercase letters, digits and dashes (max 40)")
    if risk_tier not in RISK_TIERS:
        raise DomainConfigError(f"risk tier must be one of {', '.join(RISK_TIERS)}")
    if web_policy not in WEB_POLICIES:
        raise DomainConfigError(f"web policy must be one of {', '.join(WEB_POLICIES)}")
    if not (description or "").strip():
        raise DomainConfigError("a one-line description is required: the host chooses Domains by it")
    text = path.read_text(encoding="utf-8") if path.exists() else DEFAULT_FILE_HEAD
    current = load(path) if path.exists() else parse(__import__("yaml").safe_load(text))
    if name in current:
        raise DomainConfigError(f"{name!r} is already a Domain in {path.name}")
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(r"^domains:\s*(#.*)?$", l)), None)
    if start is None:
        raise DomainConfigError(f"{path.name}: no top-level `domains:` block to add to")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[A-Za-z_][\w-]*\s*:", lines[i])), len(lines))
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1                                         # before trailing blank lines and column-0 comments
    child = next((l for l in lines[start + 1:end] if l.strip() and not l.lstrip().startswith("#")), "  x:")
    ind = child[:len(child) - len(child.lstrip())] or "  "
    block = [f"{ind}{name}:", f"{ind * 2}description: {_q(description.strip())}"]
    exs = [e.strip() for e in examples if e and e.strip()]
    if exs:
        block += [f"{ind * 2}examples:"] + [f"{ind * 3}- {_q(e)}" for e in exs]
    block += [f"{ind * 2}risk_tier: {risk_tier}", f"{ind * 2}freshness: {freshness}", f"{ind * 2}web_policy: {web_policy}"]
    new = "\n".join(lines[:end] + block + lines[end:]) + "\n"
    reg = _write_checked(path, new)
    if name not in reg:
        raise DomainConfigError(f"{path.name}: {name!r} did not land in `domains:`")   # never reached when parsed
    return reg


def ignore_folder(folder: str, path: Path | None = None) -> Registry:
    """Add a folder to `ignore_folders` (a folder that only sorts files, not a Domain), keeping comments."""
    import yaml
    path = Path(path) if path else DOMAINS_FILE
    folder = folder.strip().strip("/")
    if not folder:
        raise DomainConfigError("a folder name is required")
    text = path.read_text(encoding="utf-8") if path.exists() else DEFAULT_FILE_HEAD
    have = list((yaml.safe_load(text) or {}).get("ignore_folders") or [])
    if folder_key(folder) in {folder_key(f) for f in have}:
        return parse(yaml.safe_load(text))
    flow = "ignore_folders: [" + ", ".join(_q(f) for f in have + [folder]) + "]"
    lines = text.splitlines()
    i = next((k for k, l in enumerate(lines) if re.match(r"^ignore_folders\s*:", l)), None)
    if i is None:
        j = next((k for k, l in enumerate(lines) if re.match(r"^domains:\s*(#.*)?$", l)), len(lines))
        lines[j:j] = [flow]
    else:
        k = i + 1
        while k < len(lines) and re.match(r"^\s+-\s", lines[k]):      # a block list's items
            k += 1
        comment = re.search(r"\s+#.*$", lines[i]) if "[" in lines[i] or k == i + 1 else None
        lines[i:k] = [flow + (comment.group(0) if comment else "")]
    return _write_checked(path, "\n".join(lines) + "\n")
