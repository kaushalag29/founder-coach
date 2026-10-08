"""The enrich Step: Rules, and Facts for records that have none, for the Domains whose Packs use them
(ADR-0018; M6 plan Q3: investment, coding, system-design).

Separately versioned from extraction: enriching never re-extracts, and changing this prompt re-runs only
enrich. It reads the Document's text, asks for Rules (requirements stated by an authority, with their
conditions) and, when the record has no Facts yet (a record from the startup prompt), Facts; every one
carries a quote. The record gets them (ids r01.., f01..), its `verify` is marked stale so their quotes are
checked like any other, and `index` then turns the Verified ones into `rule` and `fact` items. A record
extracted again later starts over (enrich runs on it again).
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable

from pydantic import BaseModel, Field

ENRICH_DOMAINS = frozenset({"investment", "coding", "system-design"})   # M6d: packs that use Rules/Facts
ENRICH_RELEASE = "1.0.0"

ENRICH_PROMPT = """\
Read this {noun} and extract its RULES{and_facts}, for a personal knowledge base.

{noun_title}: {title}
{series_label}: {series}

Hard rules:
- A rule is a requirement stated by an authority (a regulation, a standard, an
  official guide, or the author stating what must or must not be done): "A fund
  must disclose its expense ratio in the prospectus". Give each a short unique
  `rule_id` like "r01", the condition it applies under in `applies_when` (or null),
  and who states it in `authority` (or null). Advice or opinion is not a rule.{facts_rule}
- EVERY item MUST carry `evidence_span`: ONE contiguous passage of 10-40 words copied
  exactly from the text below. If you cannot quote support, leave the item out.
- Do NOT output page numbers or timestamps.
- At most {max_items} rules{max_facts}; the most important, not every sentence. An
  empty list is a good answer when the text holds none.

Return JSON matching this schema:
{schema}

TEXT:
{text}
"""

FACTS_RULE = """
- A fact is a declarative statement the text presents as true ("Index funds
  track a market index"), one each, with a short unique `fact_id` like "f01"."""

PROMPT_VERSION = ENRICH_RELEASE + "-" + hashlib.sha256((ENRICH_PROMPT + FACTS_RULE).encode()).hexdigest()[:8]
STAMP = f"enrich@{PROMPT_VERSION}"


class _Rule(BaseModel):
    rule_id: str
    text: str
    applies_when: str | None = None
    authority: str | None = None
    evidence_span: str
    confidence: str = "medium"


class _Fact(BaseModel):
    fact_id: str
    text: str
    evidence_span: str
    confidence: str = "medium"


class RulesOnly(BaseModel):
    rules: list[_Rule] = Field(default_factory=list)


class RulesAndFacts(BaseModel):
    rules: list[_Rule] = Field(default_factory=list)
    facts: list[_Fact] = Field(default_factory=list)


def wants(domains) -> bool:
    return bool(set(domains or ()) & ENRICH_DOMAINS)


def record_identity(record: dict) -> str:
    """What enrich's result depends on: the extraction it adds to (unchanged by enriching it)."""
    em = record.get("extraction_meta") or {}
    return hashlib.sha256(json.dumps([record.get("doc_id"), em.get("processed_at"), em.get("prompt_hash"),
                                      em.get("schema_version")]).encode()).hexdigest()[:16]


def build_prompt(record: dict, transcript: dict, max_items: int = 12) -> tuple[str, type[BaseModel]]:
    from .extract import prompts
    kind = record.get("source_kind") or "talk"
    p = prompts.for_kind(kind)
    facts = not record.get("facts")
    model = RulesAndFacts if facts else RulesOnly
    noun = {"chapter": "book chapter", "article": "article"}.get(kind, "talk")
    prompt = ENRICH_PROMPT.format(
        noun=noun, noun_title={"chapter": "Chapter", "article": "Article"}.get(kind, "Talk"),
        title=record.get("title_canonical") or record.get("title_raw") or "",
        series_label="Book" if kind == "chapter" else "Series", series=record.get("series") or "(none)",
        and_facts=" and FACTS" if facts else "", facts_rule=FACTS_RULE if facts else "",
        max_items=max_items, max_facts=f" and {max_items} facts" if facts else "",
        schema=json.dumps(model.model_json_schema(), indent=2),
        text=p.format(transcript.get("utterances") or []))
    return prompt, model


def apply(record: dict, answer: BaseModel, model: str, cost: float) -> dict:
    """The record with the answer's rules (and facts, when it had none), ids renumbered."""
    out = dict(record)
    out["rules"] = [{**r.model_dump(), "rule_id": f"r{n:02d}"} for n, r in enumerate(answer.rules, 1)]
    if isinstance(answer, RulesAndFacts) and not record.get("facts"):
        out["facts"] = [{**f.model_dump(), "fact_id": f"f{n:02d}"} for n, f in enumerate(answer.facts, 1)]
    em = dict(out.get("extraction_meta") or {})
    em["enrich"] = {"version": PROMPT_VERSION, "model": model, "cost": round(cost, 6),
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "rules": len(out["rules"]), "facts_added": len(out.get("facts") or []) if "facts" in out
                    and not record.get("facts") else 0}
    out["extraction_meta"] = em
    return out


Ask = Callable[[str, type[BaseModel]], tuple[BaseModel | None, float]]


def llm_ask(model: str) -> Ask:
    from .eval import llm

    def ask(prompt, cls):
        a = llm.ask(prompt, cls, model)
        return a.value, a.cost
    return ask
