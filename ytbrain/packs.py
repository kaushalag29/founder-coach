"""Pack definitions: packs/<id>/pack.toml (ADR-0015, ADR-0016; M6d).

A Pack is one coach built from the shared Library: which Domains it answers from, which memory modules it
keeps, which skills it ships, and its product identity (install id, display name, the description the
plugin browser shows). One plugin is assembled per Pack (scripts/assemble_plugin.py --for <id>).

product.toml keeps what every Pack shares (author, license, the GitHub repos); each pack.toml carries its
own [product] table. Standard library only: the assembler loads it without the runtime's dependencies.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKS_DIR = ROOT / "packs"
DEFAULT_PACK = "founder"
# memory beyond the core (profile, feedback, usage); holdings: positions snapshots for the investor Pack (M6g)
MODULES = ("goals", "commitments", "checkins", "decisions", "holdings")
# Facts about the person a Pack may read from, and offer to save to, the Common profile (ADR-0016). Equal to
# founder_coach.domain.COMMON_FIELDS (tests/test_packs.py checks): this module stays standard-library only.
COMMON_FIELDS = ("timezone", "name", "role", "answer_style")
# Kinds of profile field the runtime validates (founder_coach/store.py _profile_value)
FIELD_KINDS = ("text", "int", "enum", "stage", "metrics", "places", "tz", "weekday", "allocation", "percent", "symbols")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,39}")
BUILDS = ("private", "release")
_ID = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
PRODUCT_FIELDS = ("id", "display_name", "description")


class PackError(ValueError):
    pass


@dataclass(frozen=True)
class Pack:
    id: str
    domains: tuple[str, ...]
    modules: tuple[str, ...]
    skills: tuple[str, ...]
    prompts: tuple[str, ...]
    builds: tuple[str, ...]
    product: dict
    words: dict = field(default_factory=dict)
    dir: Path = PACKS_DIR
    common_fields: tuple[str, ...] = ()      # none unless the Pack lists them: memory never crosses by default
    profile: tuple[dict, ...] = ()           # [[profile]]: the facts a Project keeps (name, kind, description, ...)
    required: tuple[str, ...] = ()           # the profile fields setup must fill before the coach stops offering it
    runtime: dict = field(default_factory=dict)   # [runtime]: instructions, setup_nudge, coach_name, replace

    @property
    def product_id(self) -> str:
        return self.product["id"]

    def pack_dir(self, private: bool, data: Path) -> Path:
        """Where `ytbrain pack build --for <id>` writes. The founder Pack keeps today's folders (data/pack,
        data/pack-private) so existing installs, evals and ops runs see no move; others get data/packs/<id>/."""
        if self.id == DEFAULT_PACK:
            return data / ("pack-private" if private else "pack")
        return data / "packs" / self.id / ("pack-private" if private else "pack")

    def dist_dir(self, private: bool, dist: Path) -> Path:
        if self.id == DEFAULT_PACK:
            return dist / ("plugin-private" if private else "plugin")
        return dist / (f"{self.product_id}-private" if private else self.product_id)

    def skill_file(self, name: str, root: Path = ROOT) -> Path:
        """A skill's source: the Pack's own folder first (packs/<id>/skills/<name>/), then the shared plugin/skills/."""
        own = self.dir / "skills" / name / "SKILL.md"
        return own if own.exists() else root / "plugin" / "skills" / name / "SKILL.md"


def _strs(v, where: str) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise PackError(f"{where} must be a list of names")
    return tuple(dict.fromkeys(x.strip() for x in v))


def _profile(raw, where: str) -> tuple[dict, ...]:
    """[[profile]] tables, in order: what one Project of this Pack remembers about its subject."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PackError(f"{where}: profile must be [[profile]] tables")
    out, seen = [], set()
    for i, f in enumerate(raw, 1):
        name = f.get("name") if isinstance(f, dict) else None
        if not isinstance(name, str) or not _FIELD.fullmatch(name) or name in seen:
            raise PackError(f"{where}: profile field {i} needs a unique lower_case `name`")
        kind = f.get("kind", "text")
        if kind not in FIELD_KINDS:
            raise PackError(f"{where}: profile field {name!r}: kind is one of {', '.join(FIELD_KINDS)}")
        desc = f.get("description")
        if not isinstance(desc, str) or not desc.strip():
            raise PackError(f"{where}: profile field {name!r} needs a description (the coach reads it)")
        spec = {"name": name, "kind": kind, "description": desc, "stale": bool(f.get("stale", True))}
        if kind == "enum":
            vals = f.get("values")
            if not isinstance(vals, list) or len(vals) < 2 or not all(isinstance(v, str) and v for v in vals):
                raise PackError(f"{where}: enum field {name!r} needs `values`, a list of two or more names")
            spec["values"] = [v.strip().lower() for v in vals]
        if extra := set(f) - {"name", "kind", "description", "stale", "values"}:
            raise PackError(f"{where}: profile field {name!r}: unknown key(s) {', '.join(sorted(extra))}")
        seen.add(name)
        out.append(spec)
    return tuple(out)


def _runtime(raw, where: str) -> dict:
    """[runtime]: what the shared runtime says for this Pack. instructions (the MCP server's whole instructions),
    setup_nudge (the Nudge while the profile is empty), coach_name ("Founder coach"), replace (phrases of the
    shared runtime's tool texts, e.g. "Founder" = "Engineer", each of which must occur there: tested)."""
    raw = dict(raw or {})
    if extra := set(raw) - {"instructions", "setup_nudge", "coach_name", "replace"}:
        raise PackError(f"{where}: [runtime] has unknown key(s) {', '.join(sorted(extra))}")
    for k in ("instructions", "setup_nudge", "coach_name"):
        if k in raw and (not isinstance(raw[k], str) or not raw[k].strip()):
            raise PackError(f"{where}: [runtime] {k} must be text")
    rep = raw.get("replace", {})
    if not isinstance(rep, dict) or not all(isinstance(k, str) and k and isinstance(v, str) for k, v in rep.items()):
        raise PackError(f"{where}: [runtime.replace] maps phrases to phrases")
    return raw


def load(pack_id: str = DEFAULT_PACK, root: Path = ROOT, known_domains=None, check_skills: bool = True) -> Pack:
    """packs/<id>/pack.toml checked: unknown modules, builds, skills without a source or Domains not in
    domains.yaml (when `known_domains` is given) are errors, never shipped."""
    pdir = root / "packs" / pack_id
    path = pdir / "pack.toml"
    if not path.exists():
        have = sorted(p.parent.name for p in (root / "packs").glob("*/pack.toml"))
        raise PackError(f"no Pack {pack_id!r} ({path} is missing); Packs: {', '.join(have) or 'none'}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise PackError(f"{path} is not valid TOML: {e}") from None
    p, prod, words = dict(data.get("pack") or {}), dict(data.get("product") or {}), dict(data.get("words") or {})
    where = f"packs/{pack_id}/pack.toml"
    if p.get("id") != pack_id:
        raise PackError(f"{where}: [pack] id must be {pack_id!r} (the folder name)")
    domains = _strs(p.get("domains"), f"{where}: domains")
    if known_domains is not None and (bad := [d for d in domains if d not in set(known_domains)]):
        raise PackError(f"{where}: Domains not in domains.yaml: {', '.join(bad)}")
    modules = _strs(p.get("modules", []), f"{where}: modules") if p.get("modules") else ()
    if bad := [m for m in modules if m not in MODULES]:
        raise PackError(f"{where}: unknown module(s) {', '.join(bad)}; known: {', '.join(MODULES)}")
    common = _strs(p["common_fields"], f"{where}: common_fields") if p.get("common_fields") else ()
    if bad := [f for f in common if f not in COMMON_FIELDS]:
        raise PackError(f"{where}: common_fields are {', '.join(COMMON_FIELDS)}, not {', '.join(bad)}")
    builds = _strs(p.get("builds", ["private"]), f"{where}: builds")
    if bad := [b for b in builds if b not in BUILDS]:
        raise PackError(f"{where}: builds are {', '.join(BUILDS)}, not {', '.join(bad)}")
    skills = _strs(p.get("skills"), f"{where}: skills")
    prompts = _strs(p.get("prompts", []), f"{where}: prompts") if p.get("prompts") else ()
    if bad := [s for s in prompts if s not in skills]:
        raise PackError(f"{where}: prompts must be skills it ships: {', '.join(bad)}")
    missing = [f for f in PRODUCT_FIELDS if not isinstance(prod.get(f), str) or not prod[f].strip()]
    if missing:
        raise PackError(f"{where}: [product] needs {', '.join(missing)}")
    if not _ID.fullmatch(prod["id"]):
        raise PackError(f"{where}: product id {prod['id']!r} must be lowercase letters, digits and hyphens")
    if not isinstance(prod.get("keywords", []), list):
        raise PackError(f"{where}: keywords must be a list")
    if not all(isinstance(v, str) for v in words.values()):
        raise PackError(f"{where}: [words] values must be text")
    profile = _profile(data.get("profile"), where)
    names = [f["name"] for f in profile]
    required = _strs(p["required"], f"{where}: required") if p.get("required") else ()
    if bad := [f for f in required if f not in names]:
        raise PackError(f"{where}: required field(s) not in [[profile]]: {', '.join(bad)}")
    runtime = _runtime(data.get("runtime"), where)
    pack = Pack(pack_id, domains, modules, skills, prompts, builds, prod, words, pdir, common, profile, required,
                runtime)
    if check_skills and (gone := [s for s in skills if not pack.skill_file(s, root).exists()]):
        raise PackError(f"{where}: no source for skill(s) {', '.join(gone)} (packs/{pack_id}/skills/<name>/SKILL.md "
                        f"or plugin/skills/<name>/SKILL.md)")
    return pack


def all_ids(root: Path = ROOT) -> list[str]:
    return sorted(p.parent.name for p in (root / "packs").glob("*/pack.toml"))


def runtime_config(pack: Pack) -> dict:
    """What the plugin's runtime reads about its Pack (written into founder_coach/product.json)."""
    out = {"id": pack.id, "domains": list(pack.domains), "modules": list(pack.modules),
           "common_fields": list(pack.common_fields), "prompts": list(pack.prompts)}
    if pack.profile:
        out["profile"] = [dict(f) for f in pack.profile]
    if pack.required:
        out["required"] = list(pack.required)
    if pack.runtime:
        out["runtime"] = dict(pack.runtime)
    return out
