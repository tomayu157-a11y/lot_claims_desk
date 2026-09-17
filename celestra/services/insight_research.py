"""Insight-scoped research conversation and proposal adapter."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ..models import (
    Evidence,
    Insight,
    InsightFieldSupport,
    InsightRevisionProposal,
    InsightWorkspaceMessage,
    ResearchQuestion,
    RunConfig,
    WorkspaceMessageRole,
)
from .insight_reconciliation import (
    EDITORIAL_CARD_FIELDS,
    CardProposalInvalid,
    DerivedCardState,
    InsightCardReconciler,
    insight_card_content,
)
from .llm import LLMUnavailable, llm
from .revision import revise


class ProposalUnsupported(RuntimeError):
    """Raised when held and supplementary evidence cannot support an update."""


class _FieldSupportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal[
        "summary", "detail", "evidence_type", "evidence", "interpretation", "review_note",
    ]
    evidence_ids: list[str]
    reason: str = ""


class _FullCardProposalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    detail: str
    evidence_type: str
    evidence: Any
    interpretation: str
    review_note: str
    support_by_field: list[_FieldSupportPayload]
    change_reasons: dict[str, str]


@dataclass
class ResearchContext:
    insight: Insight
    evidence: list[Evidence]
    config: RunConfig
    continuity_summary: str
    messages: list[InsightWorkspaceMessage]
    questions: list[ResearchQuestion] = field(default_factory=list)
    question: ResearchQuestion | None = None
    synonyms: list[str] = field(default_factory=list)
    planning_claims_context: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.questions:
            if self.question is None:
                raise ValueError("an insight research context needs a linked research question")
            self.questions = [self.question]
        if self.question is None:
            self.question = self.questions[0]


@dataclass
class ResearchTurnResult:
    text: str
    evidence: list[Evidence]
    citations: list[str]
    source_evidence_ids: list[str]
    searched: bool
    note: str
    sites: list[dict] = field(default_factory=list)
    source_audit: dict[str, dict[str, Any]] = field(default_factory=dict)
    retryable: bool = False


@dataclass
class ProposalDraft:
    proposal: InsightRevisionProposal
    evidence: list[Evidence]
    sites: list[dict] = field(default_factory=list)
    source_audit: dict[str, dict[str, Any]] = field(default_factory=dict)
    derived: DerivedCardState | None = None


def _completed_transcript(messages: list[InsightWorkspaceMessage]) -> str:
    rows = []
    for message in messages:
        if message.state.value == "completed" and message.content:
            rows.append(f"{message.role.value.upper()}: {message.content}")
    return "\n\n".join(rows)


def _instruction(context: ResearchContext, user_text: str) -> str:
    transcript = _completed_transcript(context.messages)
    insight = context.insight
    config = context.config
    linked_questions = "\n".join(
        f"- Question: {question.text}\n  Aspects: {', '.join(question.aspects) or '(none)'}\n"
        f"  Current answer: {question.answer_text or '(none)'}"
        for question in context.questions
    )
    return (
        "Continue the selected insight's research conversation and answer the latest request. "
        "The held evidence is a starting point, not a reason to stop: when the user asks for "
        "more evidence, a definition, verification, or a relevant missing detail, research it "
        "with the available sources. Do not use or infer content from other insights. If the "
        "latest request is unrelated to this insight-scoped research, briefly refuse it and "
        "invite a relevant question. Be concise, friendly, and direct.\n\n"
        "Selected insight metadata:\n"
        f"Title: {insight.title}\n"
        f"Category: {insight.category}\n"
        f"Stage: {insight.stage}\n"
        f"Current finding: {insight.summary}\n"
        f"Detail: {insight.detail or '(none)'}\n"
        f"Interpretation: {insight.interpretation or '(none)'}\n"
        f"Review note: {insight.review_note or '(none)'}\n\n"
        "Project context:\n"
        f"Indication: {config.indication}\n"
        f"Objective: {config.objective}\n"
        f"Population: {config.population}\n"
        f"Target population: {config.target_population or '(not specified)'}\n"
        f"Geography: {config.geography}\n"
        f"Research cutoff: {config.research_cutoff or '(not specified)'}\n"
        f"Additional context: {config.additional_context or '(none)'}\n\n"
        f"Linked research questions:\n{linked_questions}\n\n"
        f"Continuity summary:\n{context.continuity_summary or '(none)'}\n\n"
        f"Recent conversation:\n{transcript or '(none)'}\n\n"
        f"Latest user request:\n{user_text}"
    )


def _requires_evidence(user_text: str) -> bool:
    text = " ".join((user_text or "").lower().split())
    if not text:
        return True
    if text in {"hi", "hello", "hey", "thanks", "thank you"}:
        return False
    if any(phrase in text for phrase in ("what can you", "how can you help", "what did i ask")):
        return False
    grounding_terms = (
        "evidence", "source", "citation", "study", "literature", "web", "definition",
    )
    if any(term in text for term in grounding_terms):
        return True
    metadata_terms = (
        "title", "category", "stage", "indication", "objective", "population",
        "geography", "research cutoff", "cutoff date", "additional context", "review note",
    )
    if any(term in text for term in metadata_terms):
        return False
    return not (
        any(term in text for term in ("card", "insight", "finding", "project"))
        and any(term in text for term in ("what", "which", "tell me", "describe", "summarize"))
    )


async def answer_turn(
    context: ResearchContext,
    user_text: str,
    registry: dict,
    on_status: Callable[[str], Awaitable[None]],
    llm_client=None,
    research_gateway=None,
) -> ResearchTurnResult:
    question = context.questions[0]
    result = await revise(
        context.insight,
        question,
        context.evidence,
        _instruction(context, user_text),
        context.config,
        registry,
        context.synonyms,
        on_status=on_status,
        llm_client=llm_client or llm,
        latest_user_request=user_text,
        evidence_required=_requires_evidence(user_text),
        question_framing="\n\n".join(question.text for question in context.questions),
        research_context=context,
        research_gateway=research_gateway,
    )
    return ResearchTurnResult(
        text=result.text or result.note,
        evidence=result.evidence,
        citations=result.citations,
        source_evidence_ids=result.support_evidence_ids,
        searched=result.searched,
        note=result.note,
        sites=result.sites,
        source_audit=result.source_audit,
        retryable=result.retryable,
    )


async def build_proposal(
    context: ResearchContext, registry: dict, llm_client=None
) -> ProposalDraft:
    model = llm_client or llm
    if not model.available:
        raise LLMUnavailable("The model is unavailable. Try again.")

    basis = [
        message
        for message in context.messages
        if (
            message.role is WorkspaceMessageRole.USER
            and message.state.value == "completed"
        )
    ]
    try:
        raw_payload = await model.complete_json(
            "Draft a complete, evidence-grounded update for the selected insight card. "
            "Use concise business language. Return only the requested JSON. Do not set "
            "identity, covered, input_reason, confidence, tag, source ids, evidence ids, "
            "web state, review state, or any structural metadata.",
            _proposal_instruction(context),
            max_tokens=3000,
        )
        payload = _FullCardProposalPayload.model_validate(raw_payload)
    except ValidationError as exc:
        note = raw_payload.get("note") if isinstance(raw_payload, dict) else ""
        raise ProposalUnsupported(note or "The model did not return a complete card proposal.") from exc

    support = [
        InsightFieldSupport(
            field=item.field,
            evidence_ids=item.evidence_ids,
            reason=item.reason or payload.change_reasons.get(item.field, ""),
        )
        for item in payload.support_by_field
    ]
    editorial_content = {
        field: getattr(payload, field)
        for field in EDITORIAL_CARD_FIELDS
    }
    try:
        reconciler = InsightCardReconciler()
        proposal = reconciler.propose(
            context.insight,
            context.evidence,
            editorial_content,
            support,
            payload.change_reasons,
            [message.id for message in basis],
        )
    except (CardProposalInvalid, TypeError, ValueError) as exc:
        raise ProposalUnsupported(str(exc)) from exc
    return ProposalDraft(
        proposal=proposal,
        evidence=reconciler.active_evidence(proposal.after_content, context.evidence),
        sites=proposal.web_sites,
        derived=reconciler.derive_state(
            proposal.after_content.covered,
            proposal.after_content.input_reason,
            reconciler.active_evidence(proposal.after_content, context.evidence),
        ),
    )


def _proposal_instruction(context: ResearchContext) -> str:
    current = insight_card_content(context.insight).model_dump(mode="json")
    evidence_blocks = "\n\n".join(
        f"[ID: {item.id} | Source: {item.source_name} | URL: {item.url}] {item.quote}"
        for item in context.evidence
        if item.quote.strip()
    )
    return (
        "Create one complete replacement for the editorial content of this selected card. "
        "Every factual statement in a changed summary, detail, evidence, or interpretation "
        "must list supporting Evidence IDs from the blocks below. Use empty strings or null "
        "evidence deliberately when removing stale editorial content; never omit a field.\n\n"
        f"Current mutable card snapshot:\n{json.dumps(current, ensure_ascii=False)}\n\n"
        f"Continuity summary:\n{context.continuity_summary or '(none)'}\n\n"
        f"Selected conversation:\n{_completed_transcript(context.messages) or '(none)'}\n\n"
        f"Selected Evidence:\n{evidence_blocks or '(none)'}\n\n"
        "Return JSON exactly shaped as: "
        '{"summary": str, "detail": str, "evidence_type": str, "evidence": object|null, '
        '"interpretation": str, "review_note": str, "support_by_field": '
        '[{"field": str, "evidence_ids": [str], "reason": str}], '
        '"change_reasons": {"field": str}}. '
        "evidence_type is metrics, table, steps, list, or an empty string."
    )


def _section(value: list[str]) -> str:
    return "\n".join(f"- {item}" for item in value)


async def summarize_context(
    continuity_summary: str,
    messages: list[InsightWorkspaceMessage],
    llm_client=None,
) -> str:
    model = llm_client or llm
    result = await model.complete_json(
        "Summarize an insight-scoped research conversation for its next turn. "
        "Keep only material relevant to this insight and retain citations, unresolved "
        "items, and applied revisions.",
        f"Prior continuity summary:\n{continuity_summary or '(none)'}\n\n"
        f"Completed conversation:\n{_completed_transcript(messages) or '(none)'}\n\n"
        'Return JSON with non-empty fields: {"summary": str, "instructions": [str], '
        '"citations": [str], "unresolved": [str], "applied_revisions": [str]}.',
        max_tokens=1000,
    )
    if not isinstance(result, dict):
        raise LLMUnavailable("model did not return a structured research summary")

    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise LLMUnavailable("model did not return a structured research summary")

    required = ("instructions", "citations", "unresolved", "applied_revisions")
    sections: dict[str, list[str]] = {}
    for section_name in required:
        value = result.get(section_name)
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise LLMUnavailable("model did not return a complete structured research summary")
        sections[section_name] = [item.strip() for item in value]

    return "\n\n".join(
        [
            f"Summary:\n{summary.strip()}",
            f"Instructions:\n{_section(sections['instructions'])}",
            f"Citations:\n{_section(sections['citations'])}",
            f"Unresolved:\n{_section(sections['unresolved'])}",
            f"Applied revisions:\n{_section(sections['applied_revisions'])}",
        ]
    )
