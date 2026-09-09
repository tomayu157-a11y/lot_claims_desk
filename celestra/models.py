"""Domain models. These are the contracts shared by connectors, agents,
services and templates. Nothing here imports application code."""
from __future__ import annotations

import enum
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------
class RunMode(str, enum.Enum):
    FULL = "full"          # every agent, by dependency wave
    SINGLE = "single"      # one agent, dependencies satisfied from cache


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentStatus(str, enum.Enum):
    QUEUED = "queued"
    RESEARCHING = "researching"
    SYNTHESISING = "synthesising"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class Confidence(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    REQUIRES_INPUT = "requires_input"
    REJECTED = "rejected"

    @property
    def label(self) -> str:
        return {
            "high": "High Confidence",
            "medium": "Medium Confidence",
            "requires_input": "Requires Input",
            "rejected": "Rejected",
        }[self.value]


class EvidenceOrigin(str, enum.Enum):
    """How the evidence was obtained. Drives the SUPPLEMENTARY WEB label."""
    APPROVED_API = "approved_api"
    TARGETED_SEARCH = "targeted_search"
    LOCAL_FILE = "local_file"
    OPEN_WEB = "open_web"

    @property
    def label(self) -> str:
        return {
            "approved_api": "Approved source API",
            "targeted_search": "Targeted domain search",
            "local_file": "Local reference file",
            "open_web": "SUPPLEMENTARY WEB EVIDENCE",
        }[self.value]

    @property
    def is_supplementary(self) -> bool:
        return self is EvidenceOrigin.OPEN_WEB


class VerificationTag(str, enum.Enum):
    VERIFIED = "VERIFIED"
    GENERAL_KNOWLEDGE = "GENERAL KNOWLEDGE"
    ORIGINAL = "ORIGINAL"
    INFERENCE = "INFERENCE"
    UPDATE = "UPDATE"
    NOT_VERIFIED = "NOT VERIFIED"


class QuestionStatus(str, enum.Enum):
    PLANNED = "planned"
    RETRIEVING = "retrieving"
    SUFFICIENT = "sufficient"
    REFINING = "refining"
    WEB_FALLBACK = "web_fallback"
    INSUFFICIENT = "insufficient"
    UNANSWERED = "unanswered"


class ContradictionSeverity(str, enum.Enum):
    NOTED = "noted"
    ESCALATED = "escalated"


class ReviewAction(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    MODIFIED = "modified"
    PREFER_A = "prefer_a"
    PREFER_B = "prefer_b"
    ACKNOWLEDGED = "acknowledged"


# --------------------------------------------------------------------------
# Retrieval layer
# --------------------------------------------------------------------------
class SourceRef(BaseModel):
    """A retrievable document located by a connector."""
    source_id: str
    source_name: str
    tier: int
    url: str
    title: str = ""
    organization: str = ""
    published: str = ""
    identifiers: dict[str, str] = Field(default_factory=dict)   # pmid, pmcid, doi, nct, set_id
    snippet: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    origin: EvidenceOrigin = EvidenceOrigin.APPROVED_API

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.source_id}|{self.url}".encode()).hexdigest()[:16]


class Evidence(BaseModel):
    """One verbatim, attributable support item for exactly one question."""
    id: str = Field(default_factory=lambda: new_id("ev"))
    question_id: str
    source_id: str
    source_name: str
    organization: str = ""
    tier: int
    url: str
    title: str = ""
    published: str = ""
    quote: str                       # verbatim from the source
    context: str = ""                # surrounding text, optional
    origin: EvidenceOrigin = EvidenceOrigin.APPROVED_API
    tag: VerificationTag = VerificationTag.VERIFIED
    relevance: float = 0.0           # 0..1
    identifiers: dict[str, str] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=utcnow)

    @property
    def is_supplementary(self) -> bool:
        return self.origin.is_supplementary

    @property
    def citation(self) -> str:
        return self.organization or self.source_name


class ResearchQuestion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("q"))
    run_id: str = ""
    stage: str                       # stage_1 .. stage_6
    bucket: str                      # A..G, internal only, never rendered
    text: str
    seed_text: str = ""              # the seed question this was expanded from
    aspects: list[str] = Field(default_factory=list)
    status: QuestionStatus = QuestionStatus.PLANNED
    coverage_score: float = 0.0
    refinement_rounds: int = 0
    used_web_fallback: bool = False
    sources_attempted: list[str] = Field(default_factory=list)
    sources_answered: list[str] = Field(default_factory=list)
    unmet_reason: str = ""

    @property
    def is_answered(self) -> bool:
        return self.status is QuestionStatus.SUFFICIENT


# --------------------------------------------------------------------------
# Findings layer
# --------------------------------------------------------------------------
class InsightTable(BaseModel):
    """A rendered table inside a stage report."""
    title: str
    columns: list[str]
    rows: list[list[str]]
    footnote: str = ""


class Insight(BaseModel):
    """A card in the Discovery Insights view."""
    id: str = Field(default_factory=lambda: new_id("ins"))
    run_id: str = ""
    stage: str
    bucket: str
    category: str                    # Clinical | Treatment | Diagnostic | Logic | Journey | Synthesis
    title: str
    summary: str
    detail: str = ""
    confidence: Confidence = Confidence.MEDIUM
    tag: VerificationTag = VerificationTag.VERIFIED
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    used_web_fallback: bool = False
    review_action: ReviewAction = ReviewAction.PENDING
    user_input: str = ""
    impacted_insight_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def source_count(self) -> int:
        return len(self.source_ids)


class Contradiction(BaseModel):
    id: str = Field(default_factory=lambda: new_id("con"))
    run_id: str = ""
    stage: str
    topic: str
    source_a_name: str
    source_a_tier: int
    source_a_claim: str
    source_a_url: str = ""
    source_b_name: str
    source_b_tier: int
    source_b_claim: str
    source_b_url: str = ""
    reason: str
    severity: ContradictionSeverity = ContradictionSeverity.NOTED
    review_action: ReviewAction = ReviewAction.PENDING
    reviewer_note: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class StageReport(BaseModel):
    """Everything needed to render one stage exactly like the sample document."""
    id: str = Field(default_factory=lambda: new_id("stg"))
    run_id: str = ""
    stage: str
    bucket: str
    name: str
    core_question: str
    agent_name: str
    framework_steps: list[str] = Field(default_factory=list)
    step_numbers: list[int] = Field(default_factory=list)
    substeps: dict[str, str] = Field(default_factory=dict)
    gate: str = ""
    output_name: str = ""
    what_happens: str = ""
    expected_output: list[str] = Field(default_factory=list)
    synthesis: str = ""
    narratives: list[dict[str, str]] = Field(default_factory=list)   # {heading, body}
    tables: list[InsightTable] = Field(default_factory=list)
    takeaways: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    observability: list[dict[str, str]] = Field(default_factory=list)
    unanswered: list[dict[str, str]] = Field(default_factory=list)
    evidence_count: int = 0
    source_count: int = 0
    supplementary_count: int = 0
    tiers_represented: list[int] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class AgentState(BaseModel):
    """Live state of one agent, streamed to the UI."""
    bucket: str
    key: str                         # url-safe agent key, e.g. clinical-landscape
    name: str
    tagline: str
    icon: str
    stages: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    status: AgentStatus = AgentStatus.QUEUED
    wave: int = 0
    progress: float = 0.0
    message: str = ""
    questions_total: int = 0
    questions_answered: int = 0
    evidence_count: int = 0
    sources_used: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""


class QAMetrics(BaseModel):
    questions_planned: int = 0
    questions_sufficient: int = 0
    questions_web_only: int = 0
    questions_below_threshold: int = 0
    mean_coverage: float = 0.0
    evidence_total: int = 0
    evidence_approved: int = 0
    evidence_supplementary: int = 0
    distinct_sources: int = 0
    conflicts_surfaced: int = 0
    checklist: list[dict[str, str]] = Field(default_factory=list)
    readiness: str = ""
    sme_checklist: list[str] = Field(default_factory=list)


class RunConfig(BaseModel):
    drug_brand: str = ""
    indication: str
    indication_key: str              # ALL | CLL | custom
    geography: str = "United States"
    objective: str = "Build Claims Line of Therapy"
    target_population: str = ""
    additional_context: str = ""
    mode: RunMode = RunMode.FULL
    selected_agent: str | None = None   # bucket letter when mode is SINGLE
    research_cutoff: str = ""


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    reference: str = ""
    config: RunConfig
    status: RunStatus = RunStatus.PENDING
    agents: dict[str, AgentState] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    approved_at: datetime | None = None
    # Entities discovered by completed agents, handed to downstream agents.
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or utcnow()
        return (end - self.started_at).total_seconds()
