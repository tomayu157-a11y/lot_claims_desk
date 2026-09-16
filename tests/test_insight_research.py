from types import SimpleNamespace

import pytest

import celestra.services.insight_research as research_mod
from celestra.models import (
    AnswerStatus,
    Evidence,
    Insight,
    InsightWorkspaceMessage,
    ResearchQuestion,
    RunConfig,
    WorkspaceMessageRole,
)
from celestra.services.insight_research import ResearchContext
from celestra.services.revision import RevisionResult


def context() -> ResearchContext:
    insight = Insight(
        id="ins_one", run_id="run_one", stage="stage_2", bucket="C",
        category="Logic", title="Line-of-Therapy Rules",
        summary="Not covered by the sources consulted.", question_ids=["q_one"],
    )
    question = ResearchQuestion(
        id="q_one", run_id="run_one", stage="stage_2", bucket="C",
        text="How are treatment lines defined?",
    )
    evidence = Evidence(
        id="ev_one", question_id="q_one", source_id="crossref",
        source_name="Crossref", tier=2, url="https://example.org/source",
        quote="Treatment changes can indicate a new line.",
    )
    return ResearchContext(
        insight=insight, question=question, evidence=[evidence],
        config=RunConfig(indication="ALL", indication_key="ALL"),
        continuity_summary="Earlier work retained source ev_one.", messages=[],
    )


@pytest.mark.asyncio
async def test_answer_turn_reuses_revision_and_forwards_status(monkeypatch):
    seen = {}

    async def fake_revise(insight, question, evidence, instruction, cfg, registry,
                          synonyms=None, on_status=None, llm_client=None):
        seen["instruction"] = instruction
        await on_status("checking_evidence")
        await on_status("searching_web")
        result = RevisionResult()
        result.text = "Treatment gaps and regimen changes are common operational concepts."
        result.status = AnswerStatus.ANSWERED
        result.evidence = evidence
        result.citations = ["Crossref"]
        result.searched = True
        result.sites = [{"url": "https://example.org/consulted", "scraped": True}]
        return result

    monkeypatch.setattr(research_mod, "revise", fake_revise)
    statuses = []

    async def record_status(name):
        statuses.append(name)

    result = await research_mod.answer_turn(
        context(), "What rules can we operationalize?", {},
        on_status=record_status, llm_client=SimpleNamespace(),
    )
    assert statuses == ["checking_evidence", "searching_web"]
    assert "What rules can we operationalize?" in seen["instruction"]
    assert "Earlier work retained source ev_one" in seen["instruction"]
    assert result.searched is True
    assert result.source_evidence_ids == ["ev_one"]
    assert result.sites[0]["scraped"] is True


@pytest.mark.asyncio
async def test_build_proposal_uses_completed_user_messages(monkeypatch):
    captured = {}

    async def fake_revise(insight, question, evidence, instruction, cfg, registry,
                          synonyms=None, on_status=None, llm_client=None):
        captured["instruction"] = instruction
        result = RevisionResult()
        result.text = "Use gaps, substitutions, and restart logic; validate thresholds."
        result.note = "Added operational concepts and retained the SME caveat."
        result.evidence = evidence
        result.citations = ["Crossref"]
        return result

    monkeypatch.setattr(research_mod, "revise", fake_revise)
    ctx = context()
    ctx.messages.append(InsightWorkspaceMessage(
        id="wmsg_user", role=WorkspaceMessageRole.USER,
        content="Focus on rules that can be implemented in claims.",
    ))
    draft = await research_mod.build_proposal(ctx, {}, llm_client=SimpleNamespace())
    assert draft.proposal.proposed_summary.startswith("Use gaps")
    assert draft.proposal.basis_message_ids == ["wmsg_user"]
    assert [item.id for item in draft.evidence] == ["ev_one"]
    assert "Focus on rules" in captured["instruction"]


@pytest.mark.asyncio
async def test_summary_contract_retains_required_fields():
    class FakeLLM:
        async def complete_json(self, system, prompt, max_tokens):
            return {
                "summary": "Goal: operational rules. Citation: ev_one.",
                "instructions": ["Keep thresholds study-specific"],
                "citations": ["ev_one"],
                "unresolved": ["SME threshold validation"],
                "applied_revisions": ["Revision one added gap logic"],
            }

    summary = await research_mod.summarize_context("", context().messages, FakeLLM())
    assert "operational rules" in summary
    assert "ev_one" in summary
    assert "SME threshold validation" in summary
