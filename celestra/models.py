"""Domain models. These are the contracts shared by connectors, agents,
services and templates. Nothing here imports application code."""
from __future__ import annotations

import enum
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def workspace_id(run_id: str, insight_id: str) -> str:
    digest = hashlib.sha1(f"{run_id}|{insight_id}".encode()).hexdigest()[:16]
    return f"iws_{digest}"


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------
class RunMode(str, enum.Enum):
    FULL = "full"          # every agent, by dependency wave
    SINGLE = "single"      # one agent, dependencies satisfied from cache


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    # Paused at the human review gate; a Continue action resumes it.
    AWAITING_REVIEW = "awaiting_review"
    # Every agent has finished. The document exists but is a draft until a
    # reviewer approves it.
    COMPLETED = "completed"
    # The reviewer signed the document off. The run is locked from then on.
    APPROVED = "approved"
    FAILED = "failed"
    CANCELLED = "cancelled"


# The linear flow every run follows. Each run is in exactly one of these at a
# time; `Run.phase` derives it from the status so the UI can never disagree
# with the orchestrator about where the run is.
PHASES: list[dict[str, str]] = [
    {"key": "discovery", "name": "Discovery",
     "description": "Clinical landscape and treatment evidence"},
    {"key": "review", "name": "Review gate",
     "description": "Decide the discovery findings before they propagate"},
    {"key": "mapping", "name": "Mapping & Synthesis",
     "description": "Diagnostic footprint, treatment logic, patient journey, synthesis"},
    {"key": "approval", "name": "Final approval",
     "description": "Resolve what still needs input, then sign off"},
    {"key": "approved", "name": "Approved document",
     "description": "The signed-off research document"},
]


class AgentStatus(str, enum.Enum):
    QUEUED = "queued"
    RESEARCHING = "researching"
    SYNTHESISING = "synthesising"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class Confidence(str, enum.Enum):
    """Exactly two states, because a reviewer needs exactly one answer to the
    question "do I have to do something here?".

    READY          a vetted source answered it and nothing is unresolved
    REQUIRES_INPUT a person must act: no vetted source answered it, or two
                   sources disagree and nobody has decided, or nothing was
                   found at all
    """
    READY = "ready"
    REQUIRES_INPUT = "requires_input"

    @property
    def label(self) -> str:
        return {"ready": "Ready", "requires_input": "Requires Input"}[self.value]

    @classmethod
    def _missing_(cls, value: object):
        # Runs stored before the two-state model used four values. Map them
        # rather than refuse to load a project someone already ran.
        legacy = {"high": cls.READY, "medium": cls.READY, "rejected": cls.REQUIRES_INPUT}
        return legacy.get(str(value).lower())


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
    APPROVED = "approved"        # accepted as generated
    MODIFIED = "modified"        # rewritten by the model on the reviewer's instruction
    INPUT_ADDED = "input_added"  # reviewer attached their own knowledge to it
    PREFER_A = "prefer_a"
    PREFER_B = "prefer_b"
    ACKNOWLEDGED = "acknowledged"

    @property
    def label(self) -> str:
        return {
            "pending": "Awaiting decision",
            "approved": "Approved",
            "modified": "Revised",
            "input_added": "Input added",
            "prefer_a": "Source A preferred",
            "prefer_b": "Source B preferred",
            "acknowledged": "Acknowledged",
        }[self.value]

    @property
    def is_decided(self) -> bool:
        return self is not ReviewAction.PENDING


class WorkspaceMessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class WorkspaceMessageState(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkspaceEventType(str, enum.Enum):
    MESSAGE_SAVED = "message_saved"
    RESEARCH_STATUS = "research_status"
    ANSWER_DELTA = "answer_delta"
    ANSWER_COMPLETED = "answer_completed"
    ERROR = "error"


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


class AnswerStatus(str, enum.Enum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    NOT_FOUND = "not_found"


class Answer(BaseModel):
    """What the model concluded for one question from one batch of documents.

    The unit the final document is built from. An answer is only as good as
    the quotes under it, so it carries the evidence ids that support it and is
    discarded if none of them survive verification.
    """
    id: str = Field(default_factory=lambda: new_id("ans"))
    run_id: str = ""
    question_id: str
    stage: str = ""
    status: AnswerStatus = AnswerStatus.NOT_FOUND
    text: str = ""                    # the answer itself, prose
    aspects_covered: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)   # organisation names, display order
    origin: EvidenceOrigin = EvidenceOrigin.APPROVED_API
    batch_index: int = 0
    round_index: int = 0
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_supplementary(self) -> bool:
        return self.origin.is_supplementary


class ResearchQuestion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("q"))
    run_id: str = ""
    stage: str                       # stage_1 .. stage_6
    bucket: str                      # A..G, internal only, never rendered
    text: str
    seed_text: str = ""              # the seed question this was expanded from
    aspects: list[str] = Field(default_factory=list)
    # Model-written aspects name what an answer must contain. Heuristic ones
    # are only the question's own words, so they are scored far more gently.
    aspects_from_model: bool = False
    status: QuestionStatus = QuestionStatus.PLANNED
    coverage_score: float = 0.0
    refinement_rounds: int = 0
    used_web_fallback: bool = False
    sources_attempted: list[str] = Field(default_factory=list)
    sources_answered: list[str] = Field(default_factory=list)
    unmet_reason: str = ""
    # The consolidated answer, merged from every batch that answered it.
    answer_text: str = ""
    answer_status: AnswerStatus = AnswerStatus.NOT_FOUND
    answer_citations: list[str] = Field(default_factory=list)
    # Open-web pages consulted during fallback: {url, title, used}. Recorded
    # even when a page contributed nothing, so the trail is auditable.
    web_sites: list[dict[str, Any]] = Field(default_factory=list)
    # Reviewer-file sections routed to this question ("{file_id}.{section_id}"),
    # in priority order, and those the per-question ceiling left out.
    reviewer_sections: list[str] = Field(default_factory=list)
    reviewer_sections_dropped: list[str] = Field(default_factory=list)

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
    # Which research questions this table answers. Links a table to the
    # insight cards derived from the same questions.
    question_ids: list[str] = Field(default_factory=list)


class ReviewerFileSection(BaseModel):
    """One routable piece of a reviewer file: a heading section, or a block
    of about 2,000 characters where the file has no usable headings."""
    id: str                          # s1, s2, ... in reading order within the file
    heading: str
    text: str
    page: int | None = None          # PDF page the section starts on


class ReviewerFile(BaseModel):
    """A file the reviewer attached with Add Input.

    Its Markdown is reviewer context for the agents that run afterwards,
    routed to the questions it bears on. It is never evidence and never cited.
    """
    id: str = Field(default_factory=lambda: new_id("rf"))
    filename: str                    # the reviewer's name for it, display only
    kind: str                        # pdf | docx | txt | md
    size_bytes: int
    storage_key: str = ""
    markdown: str
    sections: list[ReviewerFileSection] = Field(default_factory=list)
    pages_read: int | None = None
    pages_total: int | None = None
    truncated: bool = False          # the ~20k-token cap cut the text
    token_estimate: int = 0
    added_at: datetime = Field(default_factory=utcnow)


class Insight(BaseModel):
    """One review card.

    Cards are fixed slots defined by the agent's objective (config/
    insight_cards.yaml) and filled from the agent's finished stage document.
    Five parts: what we found (summary), the evidence block, what it means
    (interpretation), the sources, and the reviewer's decision.
    """
    id: str = Field(default_factory=lambda: new_id("ins"))
    run_id: str = ""
    stage: str
    bucket: str
    category: str                    # Clinical | Treatment | Diagnostic | Logic | Journey | Synthesis
    title: str
    summary: str                     # 1. what we found
    detail: str = ""
    number: int = 0                  # position in the document-wide card sequence
    card_key: str = ""               # slot in insight_cards.yaml
    evidence_type: str = ""          # metrics | table | steps | list | ""
    evidence: Any = None             # 2. the card's own table / metrics / pathway
    interpretation: str = ""         # 3. what the evidence means
    review_note: str = ""            # what a reviewer should verify, in one line
    covered: bool = True             # False when the sources did not cover this slot
    confidence: Confidence = Confidence.READY
    # Why the finding needs a person, in words. Empty when it is ready.
    input_reason: str = ""
    tag: VerificationTag = VerificationTag.VERIFIED
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    used_web_fallback: bool = False
    review_action: ReviewAction = ReviewAction.PENDING
    # Modify: the instruction the reviewer gave the model, and what it did.
    user_input: str = ""
    revision_note: str = ""
    # Add Input: knowledge the reviewer attached. It is never rewritten; it is
    # printed in the document and handed to the agents that run afterwards.
    reviewer_input: str = ""
    # Add Input files. Up to two per card; routed by section to the questions
    # of the agents that run afterwards.
    reviewer_files: list[ReviewerFile] = Field(default_factory=list)
    reviewed_at: datetime | None = None
    impacted_insight_ids: list[str] = Field(default_factory=list)
    # Titles of the stage-report tables built from this insight's questions.
    table_titles: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def source_count(self) -> int:
        return len(self.source_ids)

    @property
    def needs_decision(self) -> bool:
        """True while a person still has to act on this finding."""
        return self.confidence is Confidence.REQUIRES_INPUT and not self.review_action.is_decided


class InsightWorkspaceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The workspace source id is the Evidence id. Message and proposal
    # source_ids therefore use one stable identifier end to end.
    id: str
    question_id: str
    source_id: str
    source_name: str
    organization: str = ""
    tier: int
    url: str
    title: str = ""
    published: str = ""
    quote: str
    context: str = ""
    origin: EvidenceOrigin = EvidenceOrigin.APPROVED_API
    tag: VerificationTag = VerificationTag.VERIFIED
    relevance: float = 0.0
    identifiers: dict[str, str] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=utcnow)

    @field_validator("url")
    @classmethod
    def http_urls_only(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("workspace source URL must use http or https with a hostname")
        return value


class InsightWorkspaceMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: new_id("wmsg"))
    role: WorkspaceMessageRole
    state: WorkspaceMessageState = WorkspaceMessageState.COMPLETED
    content: str = ""
    source_ids: list[str] = Field(default_factory=list)
    used_web_fallback: bool = False
    web_sites: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class InsightRevisionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: new_id("wprop"))
    proposed_summary: str
    change_note: str
    basis_message_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    web_sites: list[dict[str, Any]] = Field(default_factory=list)
    base_summary_digest: str
    created_at: datetime = Field(default_factory=utcnow)


class AppliedInsightRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: new_id("wrev"))
    proposal_id: str
    previous_summary: str
    applied_summary: str
    source_ids: list[str] = Field(default_factory=list)
    applied_at: datetime = Field(default_factory=utcnow)


class InsightWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    run_id: str
    insight_id: str
    messages: list[InsightWorkspaceMessage] = Field(default_factory=list)
    sources: list[InsightWorkspaceSource] = Field(default_factory=list)
    continuity_summary: str = ""
    summarized_through_message_id: str = ""
    pending_proposal: InsightRevisionProposal | None = None
    applied_revisions: list[AppliedInsightRevision] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_identity_and_source_references(self) -> InsightWorkspace:
        if self.id != workspace_id(self.run_id, self.insight_id):
            raise ValueError("workspace id must match its run and insight")

        source_ids = [source.id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("workspace sources must have unique ids")

        known_source_ids = set(source_ids)
        reference_sets = [message.source_ids for message in self.messages]
        if self.pending_proposal:
            reference_sets.append(self.pending_proposal.source_ids)
        reference_sets.extend(revision.source_ids for revision in self.applied_revisions)
        if any(source_id not in known_source_ids
               for references in reference_sets for source_id in references):
            raise ValueError("workspace source references must identify workspace sources")
        return self


class AppliedRevisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    insight: Insight
    workspace: InsightWorkspace


class WorkspaceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: WorkspaceEventType
    message_id: str = ""
    text: str = ""
    detail: str = ""
    source_ids: list[str] = Field(default_factory=list)
    sources: list[InsightWorkspaceSource] = Field(default_factory=list)


class Contradiction(BaseModel):
    id: str = Field(default_factory=lambda: new_id("con"))
    run_id: str = ""
    stage: str
    # The question on which the disagreement surfaced. Lets a conflict flag
    # the one card it concerns rather than every card in the stage.
    question_id: str = ""
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
    # Every question in this stage with its cited answer. The spine of the
    # final document; the prose and tables above are drawn from these.
    answers: list[dict[str, Any]] = Field(default_factory=list)
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
    executive_summary: str = ""
    research_method: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RunConfig(BaseModel):
    therapy_area: str = "Oncology"
    drug_brand: str = ""
    population: str = "All"
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
    # What the person calls this project. Defaults to the indication.
    name: str = ""
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
    # Human review gate. The run pauses after `review_after_wave` and resumes
    # from `resume_from_wave` once a reviewer continues it. 0 disables the gate.
    review_after_wave: int = 1
    resume_from_wave: int = 0
    reviewed_at: datetime | None = None

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.config.indication

    @property
    def phase(self) -> str:
        """Which step of the flow the run is in. Derived, never stored."""
        s = self.status
        if s is RunStatus.AWAITING_REVIEW:
            return "review"
        if s is RunStatus.COMPLETED:
            return "approval"
        if s is RunStatus.APPROVED:
            return "approved"
        if s in (RunStatus.FAILED, RunStatus.CANCELLED):
            return "failed"
        if self.config.mode is RunMode.SINGLE:
            return "discovery" if (self.config.selected_agent or "A") in ("A", "C") else "mapping"
        return "mapping" if self.resume_from_wave > 1 else "discovery"

    @property
    def is_live(self) -> bool:
        return self.status in (RunStatus.PENDING, RunStatus.RUNNING)

    @property
    def is_locked(self) -> bool:
        """Approved runs accept no further edits."""
        return self.status is RunStatus.APPROVED

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or utcnow()
        return (end - self.started_at).total_seconds()
