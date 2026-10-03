"""The phase-1 metadata contract.

Trimmed from the original 15-field schema (decision Q6). Cut: `quotable_claims`
(folded into highlights), `speaker_confidence` (speaker is now a plain optional
string from the title), `prerequisites` and `related_video_ids` (both need a
cross-video pass that is not in phase 1), `content_type`, `subcategory`.

Kept, and worth defending: `evidence_span` on every generated assertion, plus
fuzzy verification. It adds no runtime dependency and it is the only automated
thing standing between the coach and confidently invented advice.

Fields that feed the phase-2 graph - `stage_relevance`, `category`, `entities`,
`advice_atoms` - are retained deliberately, so building that layer later is a
projection over metadata we already have rather than a re-extraction.

NOTE: the model never emits a timestamp. It emits a verbatim quote; verify.py
locates it in the transcript and derives the timestamp. That property is what
makes single-call-per-video extraction safe.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..config import SCHEMA_VERSION


class Stage(str, Enum):
    PRE_IDEA = "pre-idea"
    IDEA = "idea"
    MVP = "mvp"
    PMF = "pmf"
    GROWTH = "growth"
    FUNDRAISING = "fundraising"
    SCALING = "scaling"
    EXIT = "exit"


class Category(str, Enum):
    IDEATION = "ideation-problem-selection"
    CUSTOMER_DISCOVERY = "customer-discovery"
    PRODUCT_MVP = "product-mvp"
    PMF = "product-market-fit"
    GROWTH = "growth-distribution"
    MARKETING = "marketing-gtm"
    SALES = "sales"
    FUNDRAISING = "fundraising"
    FINANCE_METRICS = "finance-metrics"
    HIRING = "hiring-team"
    COFOUNDER = "cofounder-dynamics"
    LEGAL_EQUITY = "legal-equity"
    OPERATIONS = "operations-execution"
    PSYCHOLOGY = "founder-psychology"
    # Added in schema 2.1.0: on a 5-video sample most talks were AI/industry
    # outlooks or a founder's own journey, and landed in "other" for want of a slot.
    AI_TECH = "ai-and-tech-trends"
    FOUNDER_STORY = "founder-story"
    OTHER = "other"          # required escape hatch, logged for taxonomy review


Confidence = Literal["high", "medium", "low", "unknown"]


class Chapter(BaseModel):
    chapter_id: str
    title: str
    start_ms: int
    end_ms: Optional[int] = None
    source: Literal["uploader", "llm"] = "llm"
    summary: Optional[str] = None


class Highlight(BaseModel):
    """A takeaway plus the quote that supports it."""
    text: str = Field(..., description="Paraphrased, actionable takeaway")
    evidence_span: str = Field(..., description="Near-verbatim quote from the transcript")
    timestamp_ms: Optional[int] = Field(None, description="Derived by verify.py, never from the model")
    match_score: Optional[float] = Field(None, description="Fuzzy match vs transcript; set by verify.py")
    confidence: Confidence = "medium"


class AdviceAtom(BaseModel):
    """One atomic, imperative recommendation. The phase-2 graph node."""
    atom_id: str
    text: str
    applies_to_stage: list[Stage] = Field(default_factory=list)
    evidence_span: str
    timestamp_ms: Optional[int] = None
    match_score: Optional[float] = None
    confidence: Confidence = "medium"


class Entities(BaseModel):
    people: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    books: list[str] = Field(default_factory=list)
    yc_jargon: list[str] = Field(default_factory=list)


class ExtractionMeta(BaseModel):
    model: str
    backend: str
    schema_version: str = SCHEMA_VERSION
    prompt_hash: Optional[str] = None
    passes: int = 1
    chapter_source: Literal["uploader", "llm", "none"] = "none"
    processed_at: Optional[str] = None
    validation_status: Literal["pass", "flagged", "failed", "pending"] = "pending"
    verification: dict = Field(default_factory=dict)
    # 2.2.0 diagnostics: every LLM call made for this record ({kind, ok, truncated, error}),
    # and the local grounding self-check ({found, checked, rate, retried, kept}).
    calls: list[dict] = Field(default_factory=list)
    grounding: dict = Field(default_factory=dict)


class Overview(BaseModel):
    """Document-level fields re-derived when a long talk was extracted in windows,
    so the summary covers the whole talk instead of its first window."""
    title_canonical: str = Field(..., description="Normalised topic title")
    speaker: Optional[str] = Field(None, description="Best-effort from title/context; null if unclear")
    category: "Category"
    stage_relevance: list["Stage"] = Field(default_factory=list)
    summary: str = Field(..., description="~150 word abstract of the WHOLE talk")


class Generated(BaseModel):
    """Exactly what the model is asked to produce - nothing given, nothing derived.

    Keeping this separate from VideoMetadata matters: it is the JSON Schema
    handed to the constrained decoder, and every given field we leave out of it
    is one less thing the model can contradict or hallucinate.
    """
    title_canonical: str = Field(..., description="Normalised topic title")
    speaker: Optional[str] = Field(None, description="Best-effort from title/context; null if unclear")
    category: Category
    stage_relevance: list[Stage] = Field(default_factory=list)
    summary: str = Field(..., description="~150 word abstract")
    highlights: list[Highlight] = Field(default_factory=list)
    advice_atoms: list[AdviceAtom] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)
    unknowns_and_gaps: list[str] = Field(default_factory=list)


class ChapterList(BaseModel):
    """Pass-A output, used only when the uploader defined no chapters."""
    chapters: list[Chapter]


class VideoMetadata(Generated):
    """The on-disk record: given fields + generated fields + provenance."""
    doc_id: str
    title_raw: str
    url: str
    series: Optional[str] = None
    provenance: str = "yc-official"
    published_at: Optional[str] = None
    duration_s: Optional[int] = None
    caption_kind: Literal["human", "auto", "asr", "none", "missing_file"] = "auto"
    # Given, not generated (ADR-0013): what the Document is and what its positions mean. The
    # defaults are what every record written before websites means, so they need no re-extract.
    source_kind: Literal["talk", "article", "chapter"] = "talk"
    locator: Literal["time", "paragraph", "page"] = "time"
    # Books (ADR-0014): a Private Source never leaves this machine in a pack, and a Chapter's
    # printed page numbers ({pdf page: label}) name its Citations when the PDF has them.
    private: bool = False
    page_labels: dict[str, str] = Field(default_factory=dict)
    chapters: list[Chapter] = Field(default_factory=list)
    extraction_meta: ExtractionMeta
