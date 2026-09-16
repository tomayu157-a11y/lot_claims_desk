import pytest

import celestra.services.answering as answering_mod
import celestra.services.insight_research as research_mod
import celestra.services.revision as revision_mod
from celestra.models import (
    Evidence,
    EvidenceOrigin,
    Insight,
    InsightWorkspaceMessage,
    ResearchQuestion,
    RunConfig,
    SourceRef,
    WorkspaceMessageRole,
)
from celestra.services.insight_research import ResearchContext
from celestra.services.llm import LLMUnavailable


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
async def test_revise_skips_evidence_status_for_an_empty_instruction():
    class AvailableModel:
        available = True

        async def complete_json(self, *args, **kwargs):
            raise AssertionError("empty instructions must not evaluate held evidence")

    statuses = []

    async def record_status(name):
        statuses.append(name)

    result = await revision_mod.revise(
        context().insight,
        context().question,
        context().evidence,
        "   ",
        context().config,
        {},
        on_status=record_status,
        llm_client=AvailableModel(),
    )

    assert statuses == []
    assert result.provider_unavailable is False


@pytest.mark.asyncio
async def test_revise_skips_evidence_status_when_the_model_is_unavailable():
    class UnavailableModel:
        available = False

    statuses = []

    async def record_status(name):
        statuses.append(name)

    result = await revision_mod.revise(
        context().insight,
        context().question,
        context().evidence,
        "Use the held evidence.",
        context().config,
        {},
        on_status=record_status,
        llm_client=UnavailableModel(),
    )

    assert statuses == []
    assert result.provider_unavailable is True


@pytest.mark.asyncio
async def test_revise_reports_evidence_check_immediately_before_held_evidence_evaluation():
    class HeldEvidenceModel:
        available = True

        async def complete_json(self, system, prompt, max_tokens):
            calls.append("held_evidence_evaluated")
            if len(calls) == 2:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": "Treatment gaps can inform operational review.",
                "status": "answered",
                "applied": True,
                "note": "Held evidence was retained.",
            }

    calls = []

    async def record_status(name):
        calls.append(name)

    result = await revision_mod.revise(
        context().insight,
        context().question,
        context().evidence,
        "Use the held evidence.",
        context().config,
        {},
        on_status=record_status,
        llm_client=HeldEvidenceModel(),
    )

    assert calls[:2] == ["checking_evidence", "held_evidence_evaluated"]
    assert result.text == "Treatment gaps can inform operational review."


@pytest.mark.asyncio
async def test_answer_turn_uses_real_revision_with_held_evidence():
    class HeldEvidenceModel:
        available = True

        def __init__(self):
            self.calls = []

        async def complete_json(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            if len(self.calls) == 1:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": "Treatment gaps can inform operational review.",
                "status": "answered",
                "applied": True,
                "note": "Held evidence was retained.",
            }

    model = HeldEvidenceModel()
    statuses = []

    async def record_status(name):
        statuses.append(name)

    result = await research_mod.answer_turn(
        context(), "What rules can we operationalize?", {},
        on_status=record_status, llm_client=model,
    )
    assert statuses == ["checking_evidence"]
    assert len(model.calls) == 2
    assert "What rules can we operationalize?" in model.calls[0][1]
    assert "Earlier work retained source ev_one" in model.calls[0][1]
    assert result.text == "Treatment gaps can inform operational review."
    assert result.source_evidence_ids == ["ev_one"]
    assert result.citations == ["Crossref"]


@pytest.mark.asyncio
async def test_answer_turn_prompt_requires_a_brief_refusal_for_unrelated_requests():
    class HeldEvidenceModel:
        available = True

        def __init__(self):
            self.calls = []

        async def complete_json(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            if len(self.calls) == 1:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": "Treatment gaps can inform operational review.",
                "status": "answered",
                "applied": True,
                "note": "Held evidence was retained.",
            }

    model = HeldEvidenceModel()

    async def ignore_status(_):
        return None

    await research_mod.answer_turn(
        context(), "Plan my vacation.", {}, on_status=ignore_status, llm_client=model,
    )

    assert "briefly refuse" in model.calls[0][1]


@pytest.mark.asyncio
async def test_answer_turn_searches_web_after_triage_with_real_revision(monkeypatch):
    quote = "A treatment change after a sustained gap can indicate a new treatment line in claims data."

    class RevisionModel:
        available = True

        def __init__(self):
            self.calls = []

        async def complete_json(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            if len(self.calls) == 1:
                return {"needs_more_sources": True, "search_query": "treatment line gaps"}
            return {
                "answer": "A sustained treatment gap can support line review.",
                "status": "partial",
                "applied": True,
                "note": "Open-web evidence was added.",
            }

    class AnsweringModel:
        available = True

        async def complete_json(self, system, prompt, max_tokens):
            return {
                "status": "partial",
                "answer": "A sustained gap can indicate a new treatment line.",
                "aspects_covered": [],
                "support": [{"document": 0, "quote": quote, "relevance": 0.9}],
            }

    class FakeOpenWeb:
        def __init__(self):
            self.searches = []
            self.scrapes = []
            self.ref = SourceRef(
                source_id="open_web",
                source_name="Open Web",
                tier=3,
                url="https://example.org/open-web",
                title="Treatment line evidence",
                snippet=quote,
                origin=EvidenceOrigin.OPEN_WEB,
            )

        async def search(self, query, limit):
            self.searches.append((query, limit))
            return [self.ref]

        async def scrape(self, url):
            self.scrapes.append(url)
            return self.ref

    revision_model = RevisionModel()
    web = FakeOpenWeb()
    monkeypatch.setattr(answering_mod, "llm", AnsweringModel())
    statuses = []

    async def record_status(name):
        statuses.append(name)

    result = await research_mod.answer_turn(
        context(), "Find missing operational support.", {"open_web": web},
        on_status=record_status, llm_client=revision_model,
    )
    assert statuses == ["checking_evidence", "searching_web"]
    assert "Find missing operational support." in revision_model.calls[0][1]
    assert len(revision_model.calls) == 2
    assert web.searches[0][0] == "treatment line gaps"
    assert web.scrapes == ["https://example.org/open-web"]
    assert result.searched is True
    assert any(item.source_id == "open_web" for item in result.evidence)
    assert result.sites == [{
        "url": "https://example.org/open-web",
        "title": "Treatment line evidence",
        "scraped": True,
        "used": True,
    }]


@pytest.mark.asyncio
async def test_build_proposal_uses_real_revision_and_completed_user_messages():
    class HeldEvidenceModel:
        available = True

        def __init__(self):
            self.calls = []

        async def complete_json(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            if len(self.calls) == 1:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": "Use gaps, substitutions, and restart logic; validate thresholds.",
                "status": "answered",
                "applied": True,
                "note": "Added operational concepts and retained the SME caveat.",
            }

    model = HeldEvidenceModel()
    ctx = context()
    ctx.messages.append(InsightWorkspaceMessage(
        id="wmsg_user", role=WorkspaceMessageRole.USER,
        content="Focus on rules that can be implemented in claims.",
    ))
    draft = await research_mod.build_proposal(ctx, {}, llm_client=model)
    assert draft.proposal.proposed_summary.startswith("Use gaps")
    assert draft.proposal.basis_message_ids == ["wmsg_user"]
    assert [item.id for item in draft.evidence] == ["ev_one"]
    assert "Focus on rules" in model.calls[0][1]
    assert len(model.calls) == 2


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "summary": ["not plain text"],
            "instructions": ["Keep thresholds study-specific"],
            "citations": ["ev_one"],
            "unresolved": ["SME threshold validation"],
            "applied_revisions": ["Revision one added gap logic"],
        },
        {
            "summary": "Goal: operational rules.",
            "instructions": "Keep thresholds study-specific",
            "citations": ["ev_one"],
            "unresolved": ["SME threshold validation"],
            "applied_revisions": ["Revision one added gap logic"],
        },
        {
            "summary": "Goal: operational rules.",
            "instructions": ["Keep thresholds study-specific"],
            "citations": [""],
            "unresolved": ["SME threshold validation"],
            "applied_revisions": ["Revision one added gap logic"],
        },
    ],
)
async def test_summary_contract_rejects_malformed_required_fields(payload):
    class FakeLLM:
        async def complete_json(self, system, prompt, max_tokens):
            return payload

    with pytest.raises(LLMUnavailable):
        await research_mod.summarize_context("", context().messages, FakeLLM())
