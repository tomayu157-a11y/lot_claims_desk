import re

import pytest

import celestra.services.answering as answering_mod
import celestra.services.insight_research as research_mod
import celestra.services.revision as revision_mod
from celestra.connectors.base import ConnectorResult
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
        source_name="Crossref", organization="Clinical Standards Council",
        tier=2, url="https://example.org/source",
        title="Claims Line Construction Standard", published="2026-06-01",
        quote="Treatment changes can indicate a new line.",
        relevance=0.92,
    )
    return ResearchContext(
        insight=insight, question=question, evidence=[evidence],
        config=RunConfig(
            indication="ALL", indication_key="ALL",
            objective="Build a claims line-of-therapy algorithm",
            target_population="Adults with ALL", geography="United States",
        ),
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
                "support": [{
                    "evidence_id": "ev_one",
                    "quote": "Treatment changes can indicate a new line.",
                }],
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
                "support": [{
                    "evidence_id": "ev_one",
                    "quote": "Treatment changes can indicate a new line.",
                }],
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
    assert result.citations == ["Clinical Standards Council"]


@pytest.mark.asyncio
async def test_answer_turn_passes_selected_card_project_and_source_metadata_to_model():
    class PromptModel:
        available = True

        def __init__(self):
            self.calls = []

        async def complete_json(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            if len(self.calls) == 1:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": "The selected card is Line-of-Therapy Rules.",
                "status": "answered",
                "applied": True,
                "note": "",
                "support": [],
            }

    model = PromptModel()

    async def ignore_status(_):
        return None

    await research_mod.answer_turn(
        context(), "What is the title of this card?", {},
        on_status=ignore_status, llm_client=model,
    )

    for _, prompt in model.calls:
        assert "Title: Line-of-Therapy Rules" in prompt
        assert "Category: Logic" in prompt
        assert "Objective: Build a claims line-of-therapy algorithm" in prompt
        assert "Target population: Adults with ALL" in prompt
        assert "Geography: United States" in prompt

    assert "Claims Line Construction Standard" in model.calls[0][1]
    assert "Clinical Standards Council" in model.calls[0][1]
    assert "Tier: 2" in model.calls[0][1]
    assert "Published: 2026-06-01" in model.calls[0][1]
    assert "Relevance: 0.92" in model.calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        ("What geography does this project cover?", "This project covers the United States."),
        ("Which stage is this finding in?", "This finding is in stage 1."),
    ],
)
async def test_answer_turn_allows_source_free_selected_metadata_answers(user_text, answer):
    class MetadataModel:
        available = True

        def __init__(self):
            self.calls = 0

        async def complete_json(self, system, prompt, max_tokens):
            self.calls += 1
            if self.calls == 1:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": answer,
                "status": "answered",
                "applied": True,
                "note": "",
                "support": [],
            }

    async def ignore_status(_):
        return None

    result = await research_mod.answer_turn(
        context(), user_text, {}, on_status=ignore_status, llm_client=MetadataModel(),
    )

    assert result.text == answer
    assert result.source_evidence_ids == []


@pytest.mark.asyncio
async def test_answer_prompt_describes_capabilities_instead_of_repeating_the_finding():
    class PromptModel:
        available = True

        def __init__(self):
            self.calls = []

        async def complete_json(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            if len(self.calls) == 1:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": "I can explain, challenge, research, and help update this finding.",
                "status": "answered",
                "applied": True,
                "note": "",
                "support": [],
            }

    model = PromptModel()

    async def ignore_status(_):
        return None

    await research_mod.answer_turn(
        context(), "What can you help me with here?", {},
        on_status=ignore_status, llm_client=model,
    )

    assert "describe your capabilities" in model.calls[1][0]
    assert "help draft an update" in model.calls[1][0]


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
                "support": [{
                    "evidence_id": "ev_one",
                    "quote": "Treatment changes can indicate a new line.",
                }],
            }

    model = HeldEvidenceModel()

    async def ignore_status(_):
        return None

    await research_mod.answer_turn(
        context(), "Plan my vacation.", {}, on_status=ignore_status, llm_client=model,
    )

    assert "briefly refuse" in model.calls[0][1]


@pytest.mark.asyncio
async def test_search_triage_prompt_honors_explicit_requests_for_more_evidence():
    class PromptModel:
        available = True

        def __init__(self):
            self.calls = []

        async def complete_json(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            if len(self.calls) == 1:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": "I can help research that finding.",
                "status": "answered",
                "applied": True,
                "note": "",
                "support": [{
                    "evidence_id": "ev_one",
                    "quote": "Treatment changes can indicate a new line.",
                }],
            }

    model = PromptModel()

    async def ignore_status(_):
        return None

    await research_mod.answer_turn(
        context(), "Find more evidence on the web.", {},
        on_status=ignore_status, llm_client=model,
    )

    assert "explicitly asks" in model.calls[0][0]
    assert "more evidence" in model.calls[0][0]


@pytest.mark.asyncio
async def test_answer_prompt_is_friendly_and_does_not_repeat_the_finding_for_greetings():
    class PromptModel:
        available = True

        def __init__(self):
            self.calls = []

        async def complete_json(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            if len(self.calls) == 1:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": "Hi! What would you like to explore about this finding?",
                "status": "answered",
                "applied": True,
                "note": "",
                "support": [],
            }

    model = PromptModel()

    async def ignore_status(_):
        return None

    await research_mod.answer_turn(
        context(), "Hi", {}, on_status=ignore_status, llm_client=model,
    )

    assert "friendly research partner" in model.calls[1][0]
    assert "brief and natural" in model.calls[1][0]
    assert "without restating the finding" in model.calls[1][0]


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
                return {"needs_more_sources": False, "search_query": "treatment line gaps"}
            evidence_id = re.search(
                r"\[ID: ([^ |]+).*?\] A treatment change after a sustained gap",
                prompt,
            ).group(1)
            return {
                "answer": "A sustained treatment gap can support line review.",
                "status": "partial",
                "applied": True,
                "note": "Open-web evidence was added.",
                "support": [{
                    "evidence_id": evidence_id,
                    "quote": quote,
                }],
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
            self.queries = []
            self.ref = SourceRef(
                source_id="open_web",
                source_name="Open Web",
                tier=3,
                url="https://example.org/open-web",
                title="Treatment line evidence",
                snippet=quote,
                origin=EvidenceOrigin.OPEN_WEB,
            )

        async def discover(self, ctx, limit):
            self.queries.append((ctx.extra["search_query"], limit))
            return ConnectorResult(source_id="open_web", refs=[self.ref])

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
    assert web.queries[0][0] == "treatment line gaps"
    assert result.searched is True
    assert any(item.source_id == "open_web" for item in result.evidence)
    assert result.sites == [{
        "url": "https://example.org/open-web",
        "title": "Treatment line evidence",
        "scraped": True,
        "used": True,
    }]


@pytest.mark.asyncio
async def test_answer_turn_discloses_connector_failure_for_requested_web_research():
    class RevisionModel:
        available = True

        def __init__(self):
            self.calls = 0

        async def complete_json(self, system, prompt, max_tokens):
            self.calls += 1
            if self.calls == 1:
                return {"needs_more_sources": True, "search_query": "treatment line gaps"}
            return {
                "answer": "The held evidence only indicates that treatment changes can matter.",
                "status": "partial",
                "applied": True,
                "note": "",
                "support": [{
                    "evidence_id": "ev_one",
                    "quote": "Treatment changes can indicate a new line.",
                }],
            }

    class FailedOpenWeb:
        async def discover(self, ctx, limit):
            return ConnectorResult.failure(
                "open_web",
                "Firecrawl credits are exhausted (402). Web search is off for the rest "
                "of this session; questions stay unanswered. Top up the account on the "
                "provider dashboard and restart to re-enable it.",
            )

    async def ignore_status(_):
        return None

    result = await research_mod.answer_turn(
        context(), "Search the web for stronger evidence.",
        {"open_web": FailedOpenWeb()}, on_status=ignore_status,
        llm_client=RevisionModel(),
    )

    assert result.searched is True
    assert "couldn't complete the requested web research" in result.text.lower()
    assert "configured web search service has no remaining credits" in result.text.lower()
    assert "top up" not in result.text.lower()


@pytest.mark.asyncio
async def test_answer_turn_preserves_search_failure_when_no_evidence_supports_an_answer():
    class FailureOnlyModel:
        available = True

        def __init__(self):
            self.calls = 0

        async def complete_json(self, system, prompt, max_tokens):
            self.calls += 1
            if self.calls == 1:
                return {"needs_more_sources": True, "search_query": "missing evidence"}
            return {
                "answer": "I could not complete the requested research.",
                "status": "partial",
                "applied": False,
                "note": "",
                "support": [],
            }

    class FailedOpenWeb:
        async def discover(self, ctx, limit):
            return ConnectorResult.failure(
                "open_web", "Firecrawl credits are exhausted (402).",
            )

    async def ignore_status(_):
        return None

    result = await research_mod.answer_turn(
        context(), "Search the web for missing evidence.",
        {"open_web": FailedOpenWeb()}, on_status=ignore_status,
        llm_client=FailureOnlyModel(),
    )

    assert result.searched is True
    assert result.text == (
        "I couldn't complete the requested web research: "
        "the configured web search service has no remaining credits."
    )


@pytest.mark.asyncio
async def test_answer_turn_rejects_unverified_model_support():
    class UnsupportedModel:
        available = True

        def __init__(self):
            self.calls = 0

        async def complete_json(self, system, prompt, max_tokens):
            self.calls += 1
            if self.calls == 1:
                return {"needs_more_sources": False, "search_query": ""}
            return {
                "answer": "A fabricated threshold defines a new treatment line.",
                "status": "answered",
                "applied": True,
                "note": "",
                "support": [{
                    "evidence_id": "ev_unknown",
                    "quote": "A fabricated threshold defines a new treatment line.",
                }],
            }

    async def ignore_status(_):
        return None

    result = await research_mod.answer_turn(
        context(), "What threshold defines a new line?", {},
        on_status=ignore_status, llm_client=UnsupportedModel(),
    )

    assert result.text == "The available evidence does not support a grounded answer."
    assert result.source_evidence_ids == []


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
                "support": [{
                    "evidence_id": "ev_one",
                    "quote": "Treatment changes can indicate a new line.",
                }],
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
