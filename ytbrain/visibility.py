"""Visibility (CONTEXT.md: Private Source): which Documents may leave your machine.

The one source of truth is the Source's configuration: a Source with `distribute: false` (your
PDF Books today, any private Source tomorrow) is private, and so is every Document it registered.
Nothing decides privacy from a Document's kind or id format (ADR-0014, amendment 2026-09-30):
the eval release, the private eval overlay and the pack all ask a `Visibility`.

Fail closed: when the known Documents are given, an unknown Document counts as private, so a
label whose Document the manifest has never seen is never released.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PUBLIC, PRIVATE = "public", "private"


def source_is_private(src: dict) -> bool:
    """A Source is private unless it may be distributed (`distribute` defaults to true)."""
    return src.get("distribute", True) is False


@dataclass(frozen=True)
class Visibility:
    private_docs: frozenset[str] = field(default_factory=frozenset)
    known_docs: frozenset[str] | None = None     # None: every Document not listed private is public

    @classmethod
    def everything_public(cls) -> Visibility:
        return cls()

    @classmethod
    def from_sources(cls, documents: list[dict], sources: list[dict]) -> Visibility:
        """From manifest rows ({doc_id, source_id}) and the normalized sources.yaml entries."""
        private_sources = {s["id"] for s in sources if source_is_private(s)}
        return cls(private_docs=frozenset(d["doc_id"] for d in documents if d.get("source_id") in private_sources),
                   known_docs=frozenset(d["doc_id"] for d in documents))

    @classmethod
    def from_manifest(cls, manifest, sources: list[dict]) -> Visibility:
        """Every Document the manifest has seen, tombstoned ones included (still known, still public)."""
        rows = [dict(r) for r in manifest.db.execute("SELECT doc_id, source_id FROM documents")]
        return cls.from_sources(rows, sources)

    def of(self, doc_id: str) -> str:
        return PRIVATE if self.is_private(doc_id) else PUBLIC

    def is_private(self, doc_id: str) -> bool:
        if doc_id in self.private_docs:
            return True
        return self.known_docs is not None and doc_id not in self.known_docs

    def is_private_moment(self, mid: str) -> bool:
        from .eval.moments import parse_moment_id
        return self.is_private(parse_moment_id(mid)[0])
