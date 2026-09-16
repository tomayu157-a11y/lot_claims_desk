"""Insight-scoped research conversation and proposal adapter."""
from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..models import (
    Evidence,
    Insight,
    InsightRevisionProposal,
    InsightWorkspaceMessage,
    ResearchQuestion,
    RunConfig,
    WorkspaceMessageRole,
)
from .llm import LLMUnavailable, llm
from .revision import revise


@dataclass
class ResearchContext:
    insight: Insight
    question: ResearchQuestion
    evidence: list[Evidence]
    config: RunConfig
    continuity_summary: str
    messages: list[InsightWorkspaceMessage]
    synonyms: list[str] = field(default_factory=list)


@dataclass
class ResearchTurnResult:
    text: str
    evidence: list[Evidence]
    citations: list[str]
    source_evidence_ids: list[str]
    searched: bool
    note: str
    sites: list[dict] = field(default_factory=list)


@dataclass
class ProposalDraft:
    proposal: InsightRevisionProposal
    evidence: list[Evidence]
    sites: list[dict] = field(default_factory=list)


def _completed_transcript(messages: list[InsightWorkspaceMessage]) -> str:
    rows = []
    for message in messages:
        if message.state.value == "completed" and message.content:
            rows.append(f"{message.role.value.upper()}: {message.content}")
    return "\n\n".join(rows)


def _instruction(context: ResearchContext, user_text: str) -> str:
    transcript = _completed_transcript(context.messages)
    return (
        "Continue the insight-scoped research conversation. Answer the latest request; "
        "do not use or infer content from other insights.\n\n"
        f"Continuity summary:\n{context.continuity_summary or '(none)'}\n\n"
        f"Recent conversation:\n{transcript or '(none)'}\n\n"
        f"Latest user request:\n{user_text}"
    )


async def answer_turn(
    context: ResearchContext,
    user_text: str,
    registry: dict,
    on_status: Callable[[str], Awaitable[None]],
    llm_client=None,
) -> ResearchTurnResult:
    result = await revise(
        context.insight,
        context.question,
        context.evidence,
        _instruction(context, user_text),
        context.config,
        registry,
        context.synonyms,
        on_status=on_status,
        llm_client=llm_client or llm,
    )
    return ResearchTurnResult(
        text=result.text or result.note,
        evidence=result.evidence,
        citations=result.citations,
        source_evidence_ids=[item.id for item in result.evidence],
        searched=result.searched,
        note=result.note,
        sites=result.sites,
    )


async def build_proposal(
    context: ResearchContext, registry: dict, llm_client=None
) -> ProposalDraft:
    basis = [
        message
        for message in context.messages
        if (
            message.role is WorkspaceMessageRole.USER
            and message.state.value == "completed"
        )
    ]
    instruction = (
        "Rewrite the current finding from the approved insight-scoped conversation.\n\n"
        f"Continuity summary:\n{context.continuity_summary or '(none)'}\n\n"
        f"Conversation:\n{_completed_transcript(context.messages) or '(none)'}"
    )
    result = await revise(
        context.insight,
        context.question,
        context.evidence,
        instruction,
        context.config,
        registry,
        context.synonyms,
        llm_client=llm_client or llm,
    )
    if not result.text:
        raise ValueError(result.note or "The evidence did not support an update.")

    digest = hashlib.sha256(context.insight.summary.encode()).hexdigest()
    proposal = InsightRevisionProposal(
        proposed_summary=result.text[:400],
        change_note=(result.note or "Finding rewritten from the conversation.")[:400],
        basis_message_ids=[message.id for message in basis],
        source_ids=[item.id for item in result.evidence],
        web_sites=result.sites,
        base_summary_digest=digest,
    )
    return ProposalDraft(
        proposal=proposal,
        evidence=result.evidence,
        sites=result.sites,
    )


def _section(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value)
    return str(value)


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
    if not isinstance(result, dict) or not result.get("summary"):
        raise LLMUnavailable("model did not return a structured research summary")

    required = ("instructions", "citations", "unresolved", "applied_revisions")
    if any(not result.get(field) for field in required):
        raise LLMUnavailable("model did not return a complete structured research summary")

    return "\n\n".join(
        [
            f"Summary:\n{result['summary']}",
            f"Instructions:\n{_section(result['instructions'])}",
            f"Citations:\n{_section(result['citations'])}",
            f"Unresolved:\n{_section(result['unresolved'])}",
            f"Applied revisions:\n{_section(result['applied_revisions'])}",
        ]
    )
