"""Extraction prompt variants and their versions (ADR-0018).

A record says which prompt variant and which version of it produced it. A new version is
declared, here, as either

  compatible  older records stay valid; if the record's shape changed, `upcast` reads an
              older record in the new shape (no LLM call, nothing re-run), or
  breaking    older records can't be mapped and are re-extracted.

So changing a prompt no longer re-extracts the whole Library: only a breaking release does,
and only for its own variant. `ytbrain upgrade` moves records to the latest version of
their variant on purpose, behind --dry-run and --max-cost.

Adding a release: append a Release below, pin the new prompt hashes in
tests/golden/prompt_versions.json, and bump SCHEMA_VERSION (config) when the generated
schema changed. A test fails if a prompt changes without a new release.

Stamps: the manifest stores `variant@version` for the extract Step. A bare version (the
stamps written before variants existed, e.g. "2.2.0") belongs to LEGACY_VARIANT.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

LEGACY_VARIANT = "startup"


@dataclass(frozen=True)
class Release:
    version: str
    compat: Literal["breaking", "compatible"]
    note: str
    # compatible only: reads a record of the previous release in this release's shape
    upcast: Callable[[dict], dict] | None = None


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    releases: tuple[Release, ...]               # oldest first; the first is a breaking starting point

    @property
    def latest(self) -> str:
        return self.releases[-1].version

    @property
    def floor(self) -> str:
        """The oldest version whose records are still valid: the last breaking release."""
        return [r for r in self.releases if r.compat == "breaking"][-1].version

    def knows(self, version: str) -> bool:
        return any(r.version == version for r in self.releases)

    def is_current(self, version: str) -> bool:
        """Records of this version are valid (possibly behind the latest, never stale)."""
        return self.knows(version) and _key(version) >= _key(self.floor)


VARIANTS: dict[str, Variant] = {
    "startup": Variant(
        "startup",
        "The 2.2 extraction prompt: startup-shaped Category and Stage, adapted per Source kind "
        "(talk, article, chapter).",
        (Release("2.2.0", "breaking", "extraction diagnostics, item caps, windowed fallback"),),
    ),
    "neutral": Variant(
        "neutral",
        "The subject-neutral prompt: Advice with the situation it applies under, Facts, Takeaways, free-text "
        "topics and typed entities; no Category or Stage (M6c).",
        (Release("1.0.0", "breaking", "first subject-neutral prompt"),),
    ),
}

# The founder Pack's Domains: content only in these keeps the startup prompt until the neutral one passes its
# parity check on startup talks (`ytbrain eval parity`). M6d moves this to packs/founder/pack.yaml.
FOUNDER_DOMAINS = frozenset({"startup", "gtm", "leadership", "finance"})


def _key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in version.split("."))
    except ValueError:
        return (-1,)


def variant_for(source_kind: str | None = None, domains=None, parity: bool | None = None) -> str:
    """The prompt variant a Document is extracted with now (ADR-0018). Everything once the neutral prompt
    passed its parity check on startup talks; before that, only content declared wholly outside the founder
    Pack's Domains (callers pass the declared Domains: a Book's folder, a Source's `domains`). A Document with
    any founder Domain, or none declared, keeps the startup prompt, so the founder coach's knowledge never
    changes before parity (L1: no talk or essay is re-extracted because its content looks like coding)."""
    if parity is None:
        parity = parity_passed()
    if parity:
        return "neutral"
    if domains and not set(domains) & FOUNDER_DOMAINS:
        return "neutral"
    return "startup"


def parity_file():
    from ..config import EVAL_DATA
    return EVAL_DATA / "neutral-parity.json"


def parity_passed() -> bool:
    """`ytbrain eval parity` passed for the neutral variant's latest version."""
    import json
    try:
        r = json.loads(parity_file().read_text())
    except (OSError, ValueError):
        return False
    return bool(r.get("passed")) and r.get("neutral") == stamp("neutral")


def stamp(variant: str) -> str:
    """What a fresh extraction is recorded as: the variant's latest version."""
    return f"{variant}@{VARIANTS[variant].latest}"


def parse_stamp(value: str | None) -> tuple[str, str]:
    """'startup@2.2.0' -> ('startup', '2.2.0'); a bare '2.2.0' is the legacy variant's."""
    value = (value or "").strip()
    if "@" in value:
        variant, _, version = value.partition("@")
        return variant, version
    return LEGACY_VARIANT, value


def stamp_of(meta: dict | None, default_version: str) -> str:
    """The stamp of a record from its extraction_meta (`prompt_variant`, `schema_version`)."""
    meta = meta or {}
    return f"{meta.get('prompt_variant') or LEGACY_VARIANT}@{meta.get('schema_version') or default_version}"


def is_current(value: str | None) -> bool:
    """A manifest stamp whose record is still valid. Unknown variants and versions below the
    variant's last breaking release are not."""
    variant, version = parse_stamp(value)
    v = VARIANTS.get(variant)
    return bool(v and v.is_current(version))


def is_latest(value: str | None) -> bool:
    variant, version = parse_stamp(value)
    v = VARIANTS.get(variant)
    return bool(v and version == v.latest)


def upcast(record: dict) -> dict:
    """An older record read in its variant's latest shape: each later compatible release's
    `upcast` applied in order. Pure; a record already on the latest version comes back as is."""
    meta = record.get("extraction_meta") or {}
    variant, version = parse_stamp(stamp_of(meta, ""))
    v = VARIANTS.get(variant)
    if not v or not version or not v.knows(version):
        return record
    out = record
    for r in v.releases:
        if _key(r.version) > _key(version) and r.upcast:
            out = r.upcast(out)
    return out


def problems(variants: dict[str, Variant] | None = None) -> list[str]:
    """What is wrong with a registry (empty when it is fine): checked by a test."""
    out = []
    for name, v in (variants or VARIANTS).items():
        if v.name != name:
            out.append(f"{name}: its name says {v.name!r}")
        if not v.releases:
            out.append(f"{name}: no releases")
            continue
        keys = [_key(r.version) for r in v.releases]
        if any(k == (-1,) for k in keys):
            out.append(f"{name}: versions must be numbers like 2.2.0")
        if keys != sorted(set(keys)):
            out.append(f"{name}: releases must be oldest first, each version once")
        if v.releases[0].compat != "breaking":
            out.append(f"{name}: the first release is a starting point, declared breaking")
        for r in v.releases:
            if r.upcast and r.compat != "compatible":
                out.append(f"{name} {r.version}: only a compatible release has an upcast")
            if not r.note.strip():
                out.append(f"{name} {r.version}: say what changed (note)")
    return out


def fingerprints(variant: str) -> dict[str, str]:
    """The prompt hash per Source kind for a variant, as recorded on its records."""
    import hashlib
    import json

    from . import prompts
    if variant not in VARIANTS:
        raise KeyError(variant)
    out = {k: prompts.for_kind(k, variant).hash() for k in ("talk", "article", "chapter")}
    blob = json.dumps([m.model_json_schema() for m in models(variant)], sort_keys=True)
    out["schema"] = hashlib.sha256(blob.encode()).hexdigest()[:12]     # what the model is asked to fill
    return out


def models(variant: str):
    """(what the model generates, the overview of a windowed Document) for a variant."""
    from . import schema
    return (schema.NeutralGenerated, schema.NeutralOverview) if variant == "neutral" else \
        (schema.Generated, schema.Overview)
