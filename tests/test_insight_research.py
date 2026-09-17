import re

import pytest

import celestra.services.answering as answering_mod
import celestra.services.insight_research as research_mod
import celestra.services.insight_web_research as web_research_mod
import celestra.services.revision as revision_mod
from celestra.models import (
    Confidence,
    Evidence,
    EvidenceOrigin,
    Insight,
    InsightWorkspaceMessage,
    ResearchQuestion,
    RunConfig,
    SourceRef,
    VerificationTag,
    WorkspaceMessageRole,
)
from celestra.services.azure_web_search import WebSourceAudit
from celestra.services.insight_research import ProposalUnsupported, ResearchContext
from celestra.services.insight_web_research import WebResearchOutcome
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
        insight=insight, questions=[question], evidence=[evidence],
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
async def test_answer_turn_frames_all_linked_questions_and_uses_held_evidence_without_web_research():
    """One insight frames every linked question, but sufficient evidence stays local."""
    class HeldEvidenceModel:
        available = True

        def __init__(self):
            self.prompts = []

        async def complete_json(self, system, prompt, max_tokens):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
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

    class NoWebResearch:
        def __init__(self):
            self.calls = 0

        async def research(self, *args, **kwargs):
            self.calls += 1
            return WebResearchOutcome()

    base = context()
    linked = ResearchQuestion(
        id="q_two", run_id="run_one", stage="stage_2", bucket="C",
        text="When does a documented regimen change begin the next treatment line?",
    )
    research_context = ResearchContext(
        insight=base.insight.model_copy(update={"question_ids": ["q_one", "q_two"]}),
        questions=[base.question, linked],
        evidence=base.evidence,
        config=base.config,
        continuity_summary=base.continuity_summary,
        messages=[],
    )
    model = HeldEvidenceModel()
    gateway = NoWebResearch()

    async def ignore_status(_):
        return None

    result = await research_mod.answer_turn(
        research_context,
        "What can the held evidence support?",
        {},
        on_status=ignore_status,
        llm_client=model,
        research_gateway=gateway,
    )

    assert gateway.calls == 0
    assert result.source_evidence_ids == ["ev_one"]
    assert all("OTHER_QUESTION_MARKER" not in prompt for prompt in model.prompts)
    assert all(base.question.text in prompt for prompt in model.prompts)
    assert all(linked.text in prompt for prompt in model.prompts)


@pytest.mark.asyncio
async def test_answer_turn_uses_gateway_refs_and_keeps_provider_audit_internal(monkeypatch):
    quote = "A regimen change may mark a new treatment line when the documented rule is met."

    class RevisionModel:
        available = True

        def __init__(self):
            self.calls = 0

        async def complete_json(self, system, prompt, max_tokens):
            self.calls += 1
            if self.calls == 1:
                return {"needs_more_sources": True, "search_query": "treatment line definition"}
            return {
                "answer": "A documented regimen change may support treatment-line review.",
                "status": "answered",
                "applied": True,
                "note": "Native web research was added.",
                "support": [{"evidence_id": "ev_azure", "quote": quote}],
            }

    class Gateway:
        def __init__(self):
            self.calls = []

        async def research(self, research_context, user_text, on_status):
            self.calls.append((research_context, user_text))
            return WebResearchOutcome(
                refs=[SourceRef(
                    source_id="azure_web_search",
                    source_name="Azure web search",
                    tier=3,
                    url="https://example.org/azure-lot",
                    title="Treatment line definition",
                    snippet=quote,
                    raw={"azure_consulted_sources": [{"url": "https://example.org/azure-lot"}]},
                    origin=EvidenceOrigin.OPEN_WEB,
                )],
                audits=[WebSourceAudit(
                    "azure_web_search", "https://example.org/azure-lot",
                    ["ALL claims line definition"], "hydrated",
                    [{"url": "https://example.org/azure-lot"}],
                    [{"url": "https://example.org/azure-lot", "title": "Treatment line definition"}],
                )],
                searched=True,
                provider="azure_web_search",
            )

    answer_batch_calls = []

    async def fake_answer_batch(question, aspects, refs, question_id, terms):
        answer_batch_calls.append((question, refs, question_id))
        return None, [Evidence(
            id="ev_azure",
            question_id=question_id,
            source_id=refs[0].source_id,
            source_name=refs[0].source_name,
            tier=refs[0].tier,
            url=refs[0].url,
            title=refs[0].title,
            quote=quote,
            origin=EvidenceOrigin.OPEN_WEB,
        )]

    monkeypatch.setattr(revision_mod, "answer_batch", fake_answer_batch)
    gateway = Gateway()

    async def ignore_status(_):
        return None

    result = await research_mod.answer_turn(
        context(), "Find stronger operational support.", {},
        on_status=ignore_status,
        llm_client=RevisionModel(),
        research_gateway=gateway,
    )

    assert gateway.calls[0][1] == "Find stronger operational support."
    assert answer_batch_calls[0][1][0].url == "https://example.org/azure-lot"
    assert result.searched is True
    assert result.source_evidence_ids == ["ev_azure"]
    assert result.sites == [{
        "url": "https://example.org/azure-lot",
        "title": "Treatment line definition",
        "scraped": True,
        "used": True,
    }]
    assert result.source_audit == {
        "https://example.org/azure-lot": {
            "search_provider": "azure_web_search",
            "search_queries": ["ALL claims line definition"],
            "hydration_status": "hydrated",
            "citation_metadata": [{
                "url": "https://example.org/azure-lot", "title": "Treatment line definition",
            }],
            "supported_answer": True,
        },
    }


@pytest.mark.asyncio
async def test_answer_turn_returns_a_safe_privacy_explanation_without_persistable_sources():
    class RevisionModel:
        available = True

        def __init__(self):
            self.calls = 0

        async def complete_json(self, system, prompt, max_tokens):
            self.calls += 1
            if self.calls == 1:
                return {"needs_more_sources": True, "search_query": "treatment line definition"}
            return {
                "answer": "Treatment changes can inform operational review.",
                "status": "partial",
                "applied": False,
                "note": "",
                "support": [{
                    "evidence_id": "ev_one",
                    "quote": "Treatment changes can indicate a new line.",
                }],
            }

    class UnsafeGateway:
        async def research(self, research_context, user_text, on_status):
            return WebResearchOutcome(
                ok=False,
                reason="Web research was not started because the request was not safe.",
            )

    async def ignore_status(_):
        return None

    result = await research_mod.answer_turn(
        context(), "Search for a patient-specific claim.", {},
        on_status=ignore_status,
        llm_client=RevisionModel(),
        research_gateway=UnsafeGateway(),
    )

    assert "was not started because the request was not safe" in result.text
    assert result.source_evidence_ids == ["ev_one"]
    assert [item.id for item in result.evidence] == ["ev_one"]
    assert result.searched is False
    assert result.retryable is False
    assert result.source_audit == {}


@pytest.mark.asyncio
async def test_build_proposal_rejects_a_privacy_blocked_web_research_outcome(monkeypatch):
    class RevisionModel:
        available = True

        def __init__(self):
            self.calls = 0

        async def complete_json(self, system, prompt, max_tokens):
            self.calls += 1
            if self.calls == 1:
                return {"needs_more_sources": True, "search_query": "treatment line definition"}
            return {
                "answer": "Treatment changes can inform operational review.",
                "status": "partial",
                "applied": False,
                "note": "",
                "support": [{
                    "evidence_id": "ev_one",
                    "quote": "Treatment changes can indicate a new line.",
                }],
            }

    class UnsafeGateway:
        def __init__(self, **kwargs):
            pass

        async def research(self, research_context, user_text, on_status):
            return WebResearchOutcome(
                ok=False,
                reason="Web research was not started because the request was not safe.",
            )

    monkeypatch.setattr(web_research_mod, "InsightWebResearchGateway", UnsafeGateway)

    with pytest.raises(research_mod.ProposalUnsupported):
        await research_mod.build_proposal(context(), {}, llm_client=RevisionModel())


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

    class FakeGateway:
        def __init__(self):
            self.calls = []
            self.ref = SourceRef(
                source_id="open_web",
                source_name="Open Web",
                tier=3,
                url="https://example.org/open-web",
                title="Treatment line evidence",
                snippet=quote,
                origin=EvidenceOrigin.OPEN_WEB,
            )

        async def research(self, research_context, user_text, on_status):
            self.calls.append((research_context, user_text))
            await on_status("searching_web_azure")
            return WebResearchOutcome(refs=[self.ref], searched=True, provider="azure_web_search")

    revision_model = RevisionModel()
    web = FakeGateway()
    monkeypatch.setattr(answering_mod, "llm", AnsweringModel())
    statuses = []

    async def record_status(name):
        statuses.append(name)

    result = await research_mod.answer_turn(
        context(), "Find missing operational support.", {"open_web": web},
        on_status=record_status, llm_client=revision_model, research_gateway=web,
    )
    assert statuses == ["checking_evidence", "searching_web_azure"]
    assert "Find missing operational support." in revision_model.calls[0][1]
    assert len(revision_model.calls) == 2
    assert web.calls[0][1] == "Find missing operational support."
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

    class FailedGateway:
        async def research(self, research_context, user_text, on_status):
            return WebResearchOutcome(
                searched=True,
                provider="azure_web_search+firecrawl",
                ok=False,
                retryable=True,
                reason="Web research is unavailable right now.",
            )

    async def ignore_status(_):
        return None

    result = await research_mod.answer_turn(
        context(), "Search the web for stronger evidence.",
        {}, on_status=ignore_status, llm_client=RevisionModel(),
        research_gateway=FailedGateway(),
    )

    assert result.searched is True
    assert "couldn't complete the requested web research" in result.text.lower()
    assert "web research is unavailable right now" in result.text.lower()
    assert result.retryable is True


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

    class FailedGateway:
        async def research(self, research_context, user_text, on_status):
            return WebResearchOutcome(
                searched=True,
                provider="azure_web_search+firecrawl",
                ok=False,
                retryable=True,
                reason="Web research is unavailable right now.",
            )

    async def ignore_status(_):
        return None

    result = await research_mod.answer_turn(
        context(), "Search the web for missing evidence.",
        {}, on_status=ignore_status, llm_client=FailureOnlyModel(),
        research_gateway=FailedGateway(),
    )

    assert result.searched is True
    assert result.text == (
        "I couldn't complete the requested web research: "
        "Web research is unavailable right now."
    )
    assert result.retryable is True


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
async def test_build_proposal_creates_a_complete_supported_card_from_the_selected_context():
    """A missing editorial field or support must not produce a partial Apply snapshot."""
    class FullCardModel:
        available = True

        def __init__(self):
            self.calls = []

        async def complete_json(self, system, prompt, max_tokens):
            self.calls.append((system, prompt))
            return {
                "summary": "Treatment changes can trigger a line-of-therapy review.",
                "detail": "Apply the review consistently to the selected claims population.",
                "evidence_type": "metrics",
                "evidence": [
                    {"label": "Observed change", "value": "Treatment change"},
                ],
                "interpretation": "Use treatment changes as a review signal, not a final determination.",
                "review_note": "Confirm the operational threshold with an SME.",
                "support_by_field": [
                    {"field": "summary", "evidence_ids": ["ev_one"]},
                    {"field": "detail", "evidence_ids": ["ev_one"]},
                    {"field": "evidence", "evidence_ids": ["ev_one"]},
                    {"field": "interpretation", "evidence_ids": ["ev_one"]},
                ],
                "change_reasons": {
                    "summary": "The held evidence supports the revised finding.",
                    "detail": "The conversation requests consistent application.",
                    "evidence_type": "The metric view makes the finding reviewable.",
                    "evidence": "The metric makes the finding reviewable.",
                    "interpretation": "The evidence supports a review signal.",
                    "review_note": "The threshold remains an SME decision.",
                },
            }

    model = FullCardModel()
    ctx = context()
    ctx.messages.append(InsightWorkspaceMessage(
        id="wmsg_user", role=WorkspaceMessageRole.USER,
        content="Focus on rules that can be implemented in claims.",
    ))
    draft = await research_mod.build_proposal(ctx, {}, llm_client=model)
    proposal = draft.proposal

    assert proposal.after_content.model_dump() == {
        "summary": "Treatment changes can trigger a line-of-therapy review.",
        "detail": "Apply the review consistently to the selected claims population.",
        "evidence_type": "metrics",
        "evidence": [{"label": "Observed change", "value": "Treatment change"}],
        "interpretation": "Use treatment changes as a review signal, not a final determination.",
        "review_note": "Confirm the operational threshold with an SME.",
        "covered": True,
        "input_reason": "",
        "evidence_ids": ["ev_one"],
        "source_ids": ["crossref"],
        "used_web_fallback": False,
    }
    assert proposal.proposed_summary == proposal.after_content.summary
    assert draft.proposal.basis_message_ids == ["wmsg_user"]
    assert [item.id for item in draft.evidence] == ["ev_one"]
    assert "Focus on rules" in model.calls[0][1]
    assert "[ID: ev_one" in model.calls[0][1]
    assert proposal.changed_fields[0].field == "summary"
    assert proposal.support_by_field[0].evidence_ids == ["ev_one"]
    assert proposal.base_content_digest
    assert draft.derived.confidence is Confidence.READY
    assert draft.derived.tag is VerificationTag.VERIFIED
    assert proposal.web_sites == []
    assert draft.sites == []
    assert draft.source_audit == {}


@pytest.mark.asyncio
async def test_build_proposal_allows_a_supported_link_only_update():
    """Validated support may add active evidence even when editorial content is unchanged."""
    class LinkOnlyModel:
        available = True

        async def complete_json(self, system, prompt, max_tokens):
            return {
                "summary": "Not covered by the sources consulted.",
                "detail": "",
                "evidence_type": "",
                "evidence": None,
                "interpretation": "",
                "review_note": "",
                "support_by_field": [{"field": "summary", "evidence_ids": ["ev_one"]}],
                "change_reasons": {},
            }

    draft = await research_mod.build_proposal(context(), {}, llm_client=LinkOnlyModel())

    assert draft.proposal.after_content.evidence_ids == ["ev_one"]
    assert draft.proposal.after_content.source_ids == ["crossref"]
    assert draft.proposal.changed_fields[-2].field == "evidence_ids"
    assert [item.id for item in draft.evidence] == ["ev_one"]


@pytest.mark.asyncio
async def test_build_proposal_rejects_a_changed_editorial_field_without_a_reason():
    class MissingReasonModel:
        available = True

        async def complete_json(self, system, prompt, max_tokens):
            return {
                "summary": "Treatment changes can trigger a review.",
                "detail": "",
                "evidence_type": "",
                "evidence": None,
                "interpretation": "",
                "review_note": "",
                "support_by_field": [{"field": "summary", "evidence_ids": ["ev_one"]}],
                "change_reasons": {},
            }

    with pytest.raises(ProposalUnsupported, match="reason"):
        await research_mod.build_proposal(context(), {}, llm_client=MissingReasonModel())


@pytest.mark.asyncio
async def test_build_proposal_prompt_includes_the_complete_current_mutable_snapshot():
    class PromptModel:
        available = True

        def __init__(self):
            self.prompt = ""

        async def complete_json(self, system, prompt, max_tokens):
            self.prompt = prompt
            return {
                "summary": "Treatment changes can trigger a review.",
                "detail": "",
                "evidence_type": "",
                "evidence": None,
                "interpretation": "",
                "review_note": "",
                "support_by_field": [{"field": "summary", "evidence_ids": ["ev_one"]}],
                "change_reasons": {"summary": "The selected evidence supports the update."},
            }

    ctx = context()
    ctx.insight = ctx.insight.model_copy(update={
        "evidence_ids": ["ev_existing"],
        "source_ids": ["source_existing"],
        "used_web_fallback": True,
    })
    model = PromptModel()

    await research_mod.build_proposal(ctx, {}, llm_client=model)

    assert '"evidence_ids": ["ev_existing"]' in model.prompt
    assert '"source_ids": ["source_existing"]' in model.prompt
    assert '"used_web_fallback": true' in model.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["summary", "detail", "evidence_type", "evidence", "interpretation", "review_note"])
async def test_build_proposal_rejects_a_partial_editorial_snapshot(missing_field):
    """Dropping any generated field must fail rather than retaining stale card content."""
    class PartialCardModel:
        available = True

        async def complete_json(self, system, prompt, max_tokens):
            output = {
                "summary": "Treatment changes can trigger a review.",
                "detail": "",
                "evidence_type": "",
                "evidence": None,
                "interpretation": "",
                "review_note": "",
                "support_by_field": [{"field": "summary", "evidence_ids": ["ev_one"]}],
                "change_reasons": {"summary": "The evidence supports this change."},
            }
            del output[missing_field]
            return output

    with pytest.raises(ProposalUnsupported):
        await research_mod.build_proposal(context(), {}, llm_client=PartialCardModel())


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
