import asyncio
import hashlib
import json
import sqlite3

import pytest
from fastapi import HTTPException

import celestra.main as main_mod
import celestra.services.insight_web_research as web_research_mod
from celestra.models import (
    AnswerStatus,
    Confidence,
    Evidence,
    EvidenceOrigin,
    Insight,
    InsightRevisionProposal,
    InsightWorkspaceMessage,
    InsightWorkspaceSource,
    ResearchQuestion,
    ReviewAction,
    Run,
    RunConfig,
    RunStatus,
    WorkspaceEventType,
    WorkspaceMessageRole,
    WorkspaceMessageState,
    workspace_id,
)
from celestra.services.insight_research import ProposalDraft, ResearchTurnResult
from celestra.services.insight_workspace import InsightWorkspaceService
from celestra.store import InsightRevisionCommit, Store


def seeded_store(tmp_path):
    store = Store(tmp_path / "workspace.db")
    run = Run(
        id="run_workspace",
        config=RunConfig(indication="ALL", indication_key="ALL"),
        status=RunStatus.COMPLETED,
    )
    selected = Insight(
        id="ins_selected",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        category="Logic",
        title="Selected LoT Insight",
        summary="SELECTED_INSIGHT_MARKER: gaps and regimen changes need definitions.",
        question_ids=["q_selected"],
        evidence_ids=["ev_selected"],
    )
    other = Insight(
        id="ins_other",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        category="Logic",
        title="Other LoT Insight",
        summary="OTHER_INSIGHT_SECRET: never share this text.",
        question_ids=["q_other"],
        evidence_ids=["ev_other"],
    )
    selected_question = ResearchQuestion(
        id="q_selected",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        text="Which gap and regimen-change rules are needed?",
    )
    other_question = ResearchQuestion(
        id="q_other",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        text="OTHER_QUESTION_SECRET: how is unrelated evidence handled?",
    )
    selected_evidence = Evidence(
        id="ev_selected",
        question_id=selected_question.id,
        source_id="selected_source",
        source_name="Selected source",
        tier=1,
        url="https://example.org/selected",
        quote="Selected evidence describes operational treatment gaps.",
        origin=EvidenceOrigin.APPROVED_API,
    )
    other_evidence = Evidence(
        id="ev_other",
        question_id=other_question.id,
        source_id="other_source",
        source_name="Other source",
        tier=1,
        url="https://example.org/other",
        quote="OTHER_EVIDENCE_SECRET: unrelated evidence.",
        origin=EvidenceOrigin.APPROVED_API,
    )
    store.save_run(run)
    store.save_insights(run.id, [selected, other])
    store.save_questions(run.id, [selected_question, other_question])
    store.save_evidence(run.id, [selected_evidence, other_evidence])
    return store, run, selected, other


async def fake_answer(context, user_text, registry, on_status, llm_client):
    await on_status("checking_evidence")
    return ResearchTurnResult(
        text="Operational rules require gap and regimen-change definitions.",
        evidence=context.evidence,
        citations=["Selected source"],
        source_evidence_ids=[item.id for item in context.evidence],
        searched=False,
        note="",
        sites=[],
    )


async def fake_summary(prior, messages, llm_client):
    return "Goal and citations retained."


def long_completed_messages():
    return [
        InsightWorkspaceMessage(
            id=f"wmsg_long_{index}",
            role=WorkspaceMessageRole.USER if index % 2 == 0 else WorkspaceMessageRole.ASSISTANT,
            state=WorkspaceMessageState.COMPLETED,
            content=f"Turn {index}: " + ("context " * 12),
        )
        for index in range(8)
    ]


@pytest.mark.asyncio
async def test_send_persists_turn_and_never_loads_other_insight(tmp_path):
    store, run, selected, other = seeded_store(tmp_path)
    captured = {}

    async def answer(context, user_text, registry, on_status, llm_client):
        captured["title"] = context.insight.title
        captured["prompt_text"] = " ".join(
            [context.insight.summary, context.continuity_summary]
            + [message.content for message in context.messages]
            + [evidence.quote for evidence in context.evidence]
        )
        await on_status("checking_evidence")
        return ResearchTurnResult(
            text="Operational rules require gap and regimen-change definitions.",
            evidence=context.evidence,
            citations=["Selected source"],
            source_evidence_ids=[context.evidence[0].id],
            searched=False,
            note="",
            sites=[],
        )

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    events = [event async for event in service.send(run.id, selected.id, "What is missing?")]
    saved = service.load(run.id, selected.id)
    assert [event.type for event in events] == [
        WorkspaceEventType.MESSAGE_SAVED,
        WorkspaceEventType.RESEARCH_STATUS,
        WorkspaceEventType.ANSWER_DELTA,
        WorkspaceEventType.ANSWER_COMPLETED,
    ]
    assert [message.state for message in saved.messages] == [
        WorkspaceMessageState.COMPLETED,
        WorkspaceMessageState.COMPLETED,
    ]
    assert captured["title"] == selected.title
    assert "OTHER_INSIGHT_SECRET" not in captured["prompt_text"]
    assert "OTHER_EVIDENCE_SECRET" not in captured["prompt_text"]
    assert service.load(run.id, other.id).messages == []


@pytest.mark.asyncio
async def test_send_context_keeps_all_and_only_selected_linked_questions_in_order(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    linked = ResearchQuestion(
        id="q_linked",
        run_id=run.id,
        stage=selected.stage,
        bucket=selected.bucket,
        text="How should a documented treatment gap be operationalized?",
    )
    store.save_questions(run.id, [linked])
    store.save_insights(run.id, [selected.model_copy(update={
        "question_ids": ["q_linked", "q_selected"],
    })])
    captured = []

    async def answer(context, user_text, registry, on_status, llm_client):
        captured.extend(question.id for question in context.questions)
        return await fake_answer(context, user_text, registry, on_status, llm_client)

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    _ = [event async for event in service.send(run.id, selected.id, "Use linked questions")]

    assert captured == ["q_linked", "q_selected"]


@pytest.mark.asyncio
async def test_twenty_four_insights_keep_workspace_context_and_transcripts_isolated(tmp_path):
    """A selected workspace must never expose another insight's private context."""
    store, run, selected, other = seeded_store(tmp_path)
    insights = [selected, other]
    for index in range(3, 25):
        insight_id = f"ins_{index:02d}"
        question_id = f"q_{index:02d}"
        evidence_id = f"ev_{index:02d}"
        private_marker = (
            "INSIGHT_24_PRIVATE_TEXT: do not leak this content."
            if index == 24
            else f"Insight {index} private content."
        )
        insights.append(
            Insight(
                id=insight_id,
                run_id=run.id,
                stage="stage_2",
                bucket="C",
                category="Logic",
                title=f"Insight {index}",
                summary=private_marker,
                question_ids=[question_id],
                evidence_ids=[evidence_id],
            )
        )
        store.save_questions(
            run.id,
            [
                ResearchQuestion(
                    id=question_id,
                    run_id=run.id,
                    stage="stage_2",
                    bucket="C",
                    text=f"Question for insight {index}",
                )
            ],
        )
        store.save_evidence(
            run.id,
            [
                Evidence(
                    id=evidence_id,
                    question_id=question_id,
                    source_id=f"source_{index}",
                    source_name=f"Source {index}",
                    tier=1,
                    url=f"https://example.org/{index}",
                    quote=f"Evidence for insight {index}",
                )
            ],
        )
    store.save_insights(run.id, insights)
    captured_contexts = {}

    async def capture_context(context, user_text, registry, on_status, llm_client):
        captured_contexts[user_text] = " ".join(
            [context.insight.summary, context.question.text]
            + [message.content for message in context.messages]
            + [evidence.quote for evidence in context.evidence]
        )
        await on_status("checking_evidence")
        return ResearchTurnResult(
            text="Workspace response.",
            evidence=context.evidence,
            citations=["Selected source"],
            source_evidence_ids=[item.id for item in context.evidence],
            searched=False,
            note="",
            sites=[],
        )

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=capture_context,
        summarizer=fake_summary,
        llm_client=object(),
    )
    first_stream = service.send(run.id, insights[0].id, "Research the first finding.")
    second_stream = service.send(run.id, insights[1].id, "Research the second finding.")
    _ = [event async for event in first_stream]
    _ = [event async for event in second_stream]

    assert len({workspace_id(run.id, insight.id) for insight in insights}) == 24
    assert service.load(run.id, insights[0].id).messages != []
    assert service.load(run.id, insights[1].id).messages != []
    assert all(service.load(run.id, insight.id).messages == [] for insight in insights[2:])
    assert "INSIGHT_24_PRIVATE_TEXT" not in captured_contexts["Research the first finding."]


@pytest.mark.asyncio
async def test_workspace_transcripts_are_not_exported_and_are_deleted_with_the_run(tmp_path, monkeypatch):
    """Supporting chat state stays local and is removed with its project."""
    store, run, selected, _ = seeded_store(tmp_path)
    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=fake_answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    _ = [event async for event in service.send(run.id, selected.id, "Keep this transcript local.")]
    monkeypatch.setattr(main_mod, "store", store)

    exported = main_mod._export_bundle(run)

    assert "insight_workspaces" not in exported
    assert "Keep this transcript local." not in json.dumps(exported)
    store.delete_run(run.id)
    assert store.get_insight_workspace(run.id, selected.id) is None
    assert store.get_run(run.id) is None


@pytest.mark.asyncio
async def test_send_marks_pending_assistant_failed_on_provider_error(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)

    async def failing_answer(context, user_text, registry, on_status, llm_client):
        raise RuntimeError("provider unavailable")

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=failing_answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    events = [event async for event in service.send(run.id, selected.id, "Research this")]
    workspace = service.load(run.id, selected.id)
    assert events[-1].type is WorkspaceEventType.ERROR
    assert workspace.messages[-1].state is WorkspaceMessageState.FAILED
    assert workspace.messages[-1].error == "Research could not be completed. Try again."
    assert store.get_insight(run.id, selected.id).summary == selected.summary


@pytest.mark.asyncio
async def test_large_context_is_summarized_without_deleting_transcript(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    calls = []
    captured = {}

    async def summarizer(prior, messages, llm_client):
        calls.append([message.id for message in messages])
        return "Goal and citations retained."

    async def answer(context, user_text, registry, on_status, llm_client):
        captured["active_message_ids"] = [message.id for message in context.messages]
        captured["active_content"] = context.continuity_summary + "\n".join(
            message.content for message in context.messages
        )
        return await fake_answer(context, user_text, registry, on_status, llm_client)

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=summarizer,
        llm_client=object(),
        active_token_limit=40,
        reduced_token_target=120,
    )
    initial = service.load(run.id, selected.id)
    initial.messages = long_completed_messages()
    store.save_insight_workspace(initial)
    before = len(initial.messages)
    [event async for event in service.send(run.id, selected.id, "Continue")]
    saved = service.load(run.id, selected.id)
    assert calls
    assert len(saved.messages) == before + 2
    assert [message.content for message in saved.messages[:before]] == [
        message.content for message in initial.messages
    ]
    assert saved.continuity_summary == "Goal and citations retained."
    assert saved.summarized_through_message_id
    assert captured["active_message_ids"] == [
        "wmsg_long_4",
        "wmsg_long_5",
        "wmsg_long_6",
        "wmsg_long_7",
        saved.messages[-2].id,
        saved.messages[-1].id,
    ]
    assert max(1, len(captured["active_content"]) // 4) <= 120


@pytest.mark.asyncio
async def test_impossible_context_budget_fails_before_answerer_and_keeps_transcript(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    answerer_calls = []

    async def answer(context, user_text, registry, on_status, llm_client):
        answerer_calls.append(user_text)
        return await fake_answer(context, user_text, registry, on_status, llm_client)

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=fake_summary,
        llm_client=object(),
        active_token_limit=40,
        reduced_token_target=20,
    )
    initial = service.load(run.id, selected.id)
    initial.messages = long_completed_messages()
    store.save_insight_workspace(initial)
    original_content = [message.content for message in initial.messages]

    events = [event async for event in service.send(run.id, selected.id, "Continue")]
    saved = service.load(run.id, selected.id)

    assert answerer_calls == []
    assert events[-1].type is WorkspaceEventType.ERROR
    assert [message.content for message in saved.messages[: len(original_content)]] == original_content
    assert saved.messages[-2].content == "Continue"
    assert saved.messages[-1].state is WorkspaceMessageState.FAILED
    assert saved.messages[-1].error == "Research could not be completed. Try again."


@pytest.mark.asyncio
async def test_irreducible_six_message_budget_skips_summarizer_and_answerer(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    summarizer_calls = []
    answerer_calls = []

    async def summarizer(prior, messages, llm_client):
        summarizer_calls.append((prior, [message.id for message in messages]))
        raise AssertionError("summarizer must not run for an irreducible context")

    async def answer(context, user_text, registry, on_status, llm_client):
        answerer_calls.append(user_text)
        raise AssertionError("answerer must not run for an irreducible context")

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=summarizer,
        llm_client=object(),
        active_token_limit=40,
        reduced_token_target=20,
    )
    initial = service.load(run.id, selected.id)
    prior_boundary = InsightWorkspaceMessage(
        id="wmsg_prior_boundary",
        role=WorkspaceMessageRole.ASSISTANT,
        state=WorkspaceMessageState.COMPLETED,
        content="Earlier completed conversation.",
    )
    initial.messages = [prior_boundary, *long_completed_messages()]
    initial.continuity_summary = "Prior continuity summary."
    initial.summarized_through_message_id = prior_boundary.id
    store.save_insight_workspace(initial)
    original_messages = [message.model_copy(deep=True) for message in initial.messages]

    events = [event async for event in service.send(run.id, selected.id, "Continue")]
    saved = service.load(run.id, selected.id)

    assert summarizer_calls == []
    assert answerer_calls == []
    assert events[-1].type is WorkspaceEventType.ERROR
    assert saved.continuity_summary == "Prior continuity summary."
    assert saved.summarized_through_message_id == prior_boundary.id
    assert saved.messages[: len(original_messages)] == original_messages
    assert saved.messages[-1].state is WorkspaceMessageState.FAILED
    assert saved.messages[-1].error == "Research could not be completed. Try again."


@pytest.mark.asyncio
async def test_tuple_duplicate_source_uses_existing_canonical_workspace_source(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    workspace = InsightWorkspaceService(store, dict).load(run.id, selected.id)
    workspace.sources.append(
        InsightWorkspaceSource(
            id="ev_canonical",
            question_id=selected.question_ids[0],
            source_id="open_web",
            source_name="Open web",
            tier=3,
            url="https://example.org/canonical",
            quote="Canonical scoped evidence.",
            origin=EvidenceOrigin.OPEN_WEB,
        )
    )
    store.save_insight_workspace(workspace)

    async def answer(context, user_text, registry, on_status, llm_client):
        duplicate = Evidence(
            id="ev_duplicate",
            question_id=selected.question_ids[0],
            source_id="open_web",
            source_name="Open web",
            tier=3,
            url="https://example.org/canonical",
            quote="Canonical scoped evidence.",
            origin=EvidenceOrigin.OPEN_WEB,
        )
        return ResearchTurnResult(
            text="Reused canonical source.",
            evidence=[duplicate],
            citations=["Open web"],
            source_evidence_ids=[duplicate.id],
            searched=True,
            note="",
            sites=[],
            source_audit={
                duplicate.url: {
                    "search_provider": "azure_web_search",
                    "search_queries": ["ALL claims line definition"],
                    "hydration_status": "hydrated",
                    "citation_metadata": [{"url": duplicate.url}],
                    "supported_answer": True,
                },
            },
        )

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    events = [event async for event in service.send(run.id, selected.id, "Reuse source")]
    saved = service.load(run.id, selected.id)

    assert saved.messages[-1].source_ids == ["ev_canonical"]
    assert events[-1].source_ids == ["ev_canonical"]
    assert [source.id for source in saved.sources] == ["ev_canonical"]
    assert saved.sources[0].search_provider == "azure_web_search"
    assert saved.sources[0].supported_answer is True
    assert all(
        evidence.id != "ev_duplicate"
        for evidence in store.get_evidence_for(run.id, selected.question_ids[0])
    )


@pytest.mark.asyncio
async def test_send_persists_provider_audit_without_exposing_it_in_sse(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    discovered = Evidence(
        id="ev_azure",
        question_id=selected.question_ids[0],
        source_id="azure_web_search",
        source_name="Azure web search",
        tier=3,
        url="https://example.org/azure-lot",
        quote="A regimen change may mark a new treatment line when the documented rule is met.",
        origin=EvidenceOrigin.OPEN_WEB,
    )

    async def answer(context, user_text, registry, on_status, llm_client):
        return ResearchTurnResult(
            text="A documented regimen change may support treatment-line review.",
            evidence=[*context.evidence, discovered],
            citations=["Azure web search"],
            source_evidence_ids=[discovered.id],
            searched=True,
            note="",
            sites=[{
                "url": discovered.url,
                "title": "Treatment line definition",
                "scraped": True,
                "used": True,
            }],
            source_audit={
                discovered.url: {
                    "search_provider": "azure_web_search",
                    "search_queries": ["ALL claims line definition"],
                    "hydration_status": "hydrated",
                    "citation_metadata": [{"url": discovered.url}],
                    "supported_answer": True,
                },
            },
        )

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    events = [event async for event in service.send(run.id, selected.id, "Find support")]
    saved = service.load(run.id, selected.id)
    source = next(item for item in saved.sources if item.id == discovered.id)
    payload = events[-1].model_dump(mode="json")

    assert source.search_provider == "azure_web_search"
    assert source.search_queries == ["ALL claims line definition"]
    assert source.hydration_status == "hydrated"
    assert source.citation_metadata == [{"url": discovered.url}]
    assert source.supported_answer is True
    assert "source_audit" not in json.dumps(payload)
    assert "search_provider" not in json.dumps(payload)
    assert "citation_metadata" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_send_marks_combined_provider_failure_retryable_without_mutating_the_card(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)

    async def answer(context, user_text, registry, on_status, llm_client):
        return ResearchTurnResult(
            text="Web research is unavailable right now.",
            evidence=context.evidence,
            citations=[],
            source_evidence_ids=[],
            searched=True,
            note="Web research is unavailable right now.",
            sites=[],
            retryable=True,
        )

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    events = [event async for event in service.send(run.id, selected.id, "Find stronger support")]
    workspace = service.load(run.id, selected.id)

    assert events[-1].type is WorkspaceEventType.ERROR
    assert workspace.messages[-1].state is WorkspaceMessageState.FAILED
    assert workspace.sources == []
    assert store.get_insight(run.id, selected.id).summary == selected.summary


@pytest.mark.asyncio
async def test_send_marks_a_gateway_exception_retryable_without_exposing_its_text(tmp_path, monkeypatch):
    class Model:
        available = True

        def __init__(self):
            self.calls = 0

        async def complete_json(self, system, prompt, max_tokens):
            self.calls += 1
            if self.calls == 1:
                return {"needs_more_sources": True, "search_query": "treatment line definition"}
            return {
                "answer": "The held evidence supports an operational review.",
                "status": "partial",
                "applied": False,
                "note": "",
                "support": [{
                    "evidence_id": "ev_selected",
                    "quote": "Selected evidence describes operational treatment gaps.",
                }],
            }

    class RaisingGateway:
        def __init__(self, **kwargs):
            pass

        async def research(self, research_context, user_text, on_status):
            raise RuntimeError("provider token and raw exception text")

    store, run, selected, _ = seeded_store(tmp_path)
    monkeypatch.setattr(web_research_mod, "InsightWebResearchGateway", RaisingGateway)
    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        summarizer=fake_summary,
        llm_client=Model(),
    )

    events = [event async for event in service.send(run.id, selected.id, "Find stronger support")]
    workspace = service.load(run.id, selected.id)

    assert events[-1].type is WorkspaceEventType.ERROR
    assert workspace.messages[-1].state is WorkspaceMessageState.FAILED
    assert "provider token" not in json.dumps(events[-1].model_dump())
    assert store.get_insight(run.id, selected.id).summary == selected.summary


@pytest.mark.asyncio
async def test_discovered_source_is_available_to_the_next_turn_without_official_promotion(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    seen_evidence_ids = []

    async def answer(context, user_text, registry, on_status, llm_client):
        seen_evidence_ids.append([item.id for item in context.evidence])
        if len(seen_evidence_ids) == 1:
            discovered = Evidence(
                id="ev_workspace",
                question_id=selected.question_ids[0],
                source_id="open_web",
                source_name="Open web",
                tier=3,
                url="https://example.org/new",
                quote="New scoped evidence.",
                origin=EvidenceOrigin.OPEN_WEB,
            )
            return ResearchTurnResult(
                text="Found a source.",
                evidence=[discovered],
                citations=["Open web"],
                source_evidence_ids=[discovered.id],
                searched=True,
                note="",
                sites=[],
            )
        return ResearchTurnResult(
            text="Reused it.",
            evidence=context.evidence,
            citations=["Open web"],
            source_evidence_ids=[item.id for item in context.evidence],
            searched=False,
            note="",
            sites=[],
        )

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    [event async for event in service.send(run.id, selected.id, "Find evidence")]
    assert all(item.id != "ev_workspace" for item in store.get_evidence_for(run.id, selected.question_ids[0]))
    [event async for event in service.send(run.id, selected.id, "Use that source")]
    assert "ev_workspace" in seen_evidence_ids[1]


@pytest.mark.asyncio
async def test_send_skips_malformed_historical_evidence_urls(tmp_path):
    """A malformed legacy source must not prevent a completed research turn."""
    store, run, selected, _ = seeded_store(tmp_path)
    valid = store.get_evidence_for(run.id, selected.question_ids[0])[0]
    invalid_scheme = valid.model_copy(update={
        "id": "ev_invalid_scheme", "url": "javascript:alert(1)",
    })
    invalid_hostless = valid.model_copy(update={
        "id": "ev_invalid_hostless", "url": "http://",
    })

    async def answer(context, user_text, registry, on_status, llm_client):
        await on_status("checking_evidence")
        return ResearchTurnResult(
            text="The valid evidence supports the requested rule.",
            evidence=[valid, invalid_scheme, invalid_hostless],
            citations=["Selected source"],
            source_evidence_ids=[valid.id, invalid_scheme.id, invalid_hostless.id],
            searched=False,
            note="",
            sites=[],
        )

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    events = [event async for event in service.send(run.id, selected.id, "Use legacy evidence")]
    workspace = service.load(run.id, selected.id)

    assert events[-1].type is WorkspaceEventType.ANSWER_COMPLETED
    assert events[-1].source_ids == [valid.id]
    assert workspace.messages[-1].state is WorkspaceMessageState.COMPLETED
    assert workspace.messages[-1].source_ids == [valid.id]
    assert [source.id for source in workspace.sources] == [valid.id]


@pytest.mark.asyncio
async def test_research_status_arrives_before_answer_finishes(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    release = asyncio.Event()

    async def blocked_answer(context, user_text, registry, on_status, llm_client):
        await on_status("searching_web")
        await release.wait()
        return ResearchTurnResult(
            text="Finished research.",
            evidence=context.evidence,
            citations=["Selected source"],
            source_evidence_ids=[item.id for item in context.evidence],
            searched=True,
            note="",
            sites=[],
        )

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=blocked_answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    stream = service.send(run.id, selected.id, "Research this")
    assert (await anext(stream)).type is WorkspaceEventType.MESSAGE_SAVED
    status = await asyncio.wait_for(anext(stream), timeout=0.5)
    assert status.type is WorkspaceEventType.RESEARCH_STATUS
    assert status.detail == "searching_web"
    release.set()
    assert [event async for event in stream][-1].type is WorkspaceEventType.ANSWER_COMPLETED


@pytest.mark.asyncio
async def test_cancelled_stream_marks_pending_assistant_failed_and_stops_producer(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def blocked_answer(context, user_text, registry, on_status, llm_client):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=blocked_answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    stream = service.send(run.id, selected.id, "Research this")
    await anext(stream)
    next_event = asyncio.create_task(anext(stream))
    await asyncio.wait_for(started.wait(), timeout=0.5)
    next_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_event
    await asyncio.wait_for(stopped.wait(), timeout=0.5)
    workspace = service.load(run.id, selected.id)
    assert workspace.messages[-1].state is WorkspaceMessageState.FAILED
    assert workspace.messages[-1].error == "Research could not be completed. Try again."


@pytest.mark.asyncio
async def test_closed_stream_marks_pending_assistant_failed_and_stops_producer(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    release = asyncio.Event()
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def blocked_answer(context, user_text, registry, on_status, llm_client):
        await on_status("searching_web")
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=blocked_answer,
        summarizer=fake_summary,
        llm_client=object(),
    )
    stream = service.send(run.id, selected.id, "Research this")
    await anext(stream)
    assert (await anext(stream)).type is WorkspaceEventType.RESEARCH_STATUS
    await asyncio.wait_for(started.wait(), timeout=0.5)
    await stream.aclose()
    await asyncio.wait_for(stopped.wait(), timeout=0.5)
    workspace = service.load(run.id, selected.id)
    assert workspace.messages[-1].state is WorkspaceMessageState.FAILED
    assert workspace.messages[-1].error == "Research could not be completed. Try again."


async def deterministic_proposal(context, registry, llm_client):
    supplementary = Evidence(
        id="ev_proposed",
        question_id=context.question.id,
        source_id="supplementary_source",
        source_name="Supplementary source",
        tier=3,
        url="https://example.org/proposed",
        quote="Supplementary evidence supports an operational update.",
        origin=EvidenceOrigin.OPEN_WEB,
    )
    return ProposalDraft(
        proposal=InsightRevisionProposal(
            id="wprop_deterministic",
            proposed_summary="Proposed operational finding.",
            change_note="Updated from the scoped research conversation.",
            basis_message_ids=[
                message.id
                for message in context.messages
                if message.role is WorkspaceMessageRole.USER
            ],
            source_ids=[context.evidence[0].id, supplementary.id],
            web_sites=[
                {"url": "https://example.org/proposed", "title": "Proposed", "used": True},
                {"url": "https://example.org/consulted", "title": "Consulted", "used": False},
            ],
            base_summary_digest=hashlib.sha256(context.insight.summary.encode()).hexdigest(),
        ),
        evidence=[*context.evidence, supplementary],
        sites=[
            {"url": "https://example.org/proposed", "title": "Proposed", "used": True},
            {"url": "https://example.org/consulted", "title": "Consulted", "used": False},
        ],
    )


def proposal_service(
    store,
    lock_registry=None,
    proposal_builder=deterministic_proposal,
    answerer=fake_answer,
):
    return InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answerer,
        summarizer=fake_summary,
        proposal_builder=proposal_builder,
        llm_client=object(),
        lock_registry=lock_registry if lock_registry is not None else {},
    )


def seeded_store_with_stage_report(tmp_path):
    store, run, selected, other = seeded_store(tmp_path)
    question = store.get_questions(run.id)[0]
    from celestra.models import StageReport

    store.save_stage_reports(
        run.id,
        [
            StageReport(
                id="stg_selected",
                run_id=run.id,
                stage=question.stage,
                bucket=question.bucket,
                name="Selected stage",
                core_question=question.text,
                agent_name="Research",
                answers=[
                    {
                        "question": question.text,
                        "seed": question.seed_text or question.text,
                        "answer": question.answer_text,
                        "status": question.answer_status.value,
                        "citations": question.answer_citations,
                    }
                ],
            )
        ],
    )
    return store, run, selected, other


def revision_commit(store, run, selected):
    question = next(
        item for item in store.get_questions(run.id) if item.id == selected.question_ids[0]
    )
    report = next(item for item in store.get_stage_reports(run.id) if item.stage == question.stage)
    workspace = proposal_service(store).load(run.id, selected.id)
    changed = selected.model_copy(deep=True)
    changed.summary = "Committed operational finding."
    return InsightRevisionCommit(
        insight=changed,
        question=question,
        stage_reports=[report],
        new_evidence=[],
        workspace=workspace,
        expected_summary_digest=hashlib.sha256(selected.summary.encode()).hexdigest(),
    )


@pytest.mark.asyncio
async def test_proposal_is_persisted_without_mutating_insight(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    service = proposal_service(store)
    proposal = await service.propose(run.id, selected.id)
    assert proposal.proposed_summary == "Proposed operational finding."
    assert store.get_insight(run.id, selected.id).summary == selected.summary
    workspace = service.load(run.id, selected.id)
    assert workspace.pending_proposal == proposal
    assert workspace.pending_proposal.source_ids == ["ev_selected", "ev_proposed"]
    assert [source.id for source in workspace.sources] == ["ev_selected", "ev_proposed"]
    assert all(item.id != "ev_proposed" for item in store.get_evidence(run.id))


@pytest.mark.asyncio
async def test_two_successive_proposals_can_be_applied(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    service = proposal_service(store)
    first = await service.propose(run.id, selected.id)
    first_result = await service.apply(run.id, selected.id, first.id)
    second = await service.propose(run.id, selected.id)
    second_result = await service.apply(run.id, selected.id, second.id)
    workspace = service.load(run.id, selected.id)
    assert first_result.insight.summary == "Proposed operational finding."
    assert second_result.insight.summary == "Proposed operational finding."
    assert len(workspace.applied_revisions) == 2
    assert workspace.pending_proposal is None
    assert service._research_context(run.id, selected.id, workspace).insight.summary == second_result.insight.summary


@pytest.mark.asyncio
async def test_stale_proposal_is_rejected_without_mutation(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    service = proposal_service(store)
    proposal = await service.propose(run.id, selected.id)
    changed = store.get_insight(run.id, selected.id)
    changed.summary = "Changed outside the proposal."
    store.save_insights(run.id, [changed])
    with pytest.raises(HTTPException) as exc:
        await service.apply(run.id, selected.id, proposal.id)
    assert exc.value.status_code == 409
    assert store.get_insight(run.id, selected.id).summary == "Changed outside the proposal."
    assert service.load(run.id, selected.id).pending_proposal.id == proposal.id


@pytest.mark.asyncio
async def test_locked_run_allows_send_but_rejects_proposal_and_apply(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    run.status = RunStatus.APPROVED
    store.save_run(run)
    service = proposal_service(store)
    assert [event async for event in service.send(run.id, selected.id, "Explain this")]
    with pytest.raises(HTTPException) as exc:
        await service.propose(run.id, selected.id)
    assert exc.value.status_code == 409


def test_atomic_commit_rolls_back_when_stage_report_write_fails(tmp_path):
    store, run, selected, _ = seeded_store_with_stage_report(tmp_path)
    before = store.get_insight(run.id, selected.id)
    connection = store._conn()
    connection.execute(
        "CREATE TRIGGER reject_stage_update BEFORE UPDATE ON stage_reports "
        "BEGIN SELECT RAISE(ABORT, 'forced rollback'); END"
    )
    with pytest.raises(sqlite3.DatabaseError, match="forced rollback"):
        store.commit_insight_revision(revision_commit(store, run, selected))
    assert store.get_insight(run.id, selected.id) == before
    assert store.get_insight_workspace(run.id, selected.id).applied_revisions == []


@pytest.mark.asyncio
async def test_apply_promotes_proposal_sources_and_audited_sites_only_after_apply(tmp_path):
    store, run, selected, _ = seeded_store_with_stage_report(tmp_path)
    service = proposal_service(store)
    proposal = await service.propose(run.id, selected.id)
    before_question = store.get_questions(run.id)[0]
    assert before_question.web_sites == []
    assert all(item.id != "ev_proposed" for item in store.get_evidence_for(run.id, before_question.id))

    result = await service.apply(run.id, selected.id, proposal.id)
    question = store.get_questions(run.id)[0]
    report = store.get_stage_reports(run.id)[0]
    assert result.insight.source_ids == ["selected_source", "supplementary_source"]
    assert result.insight.evidence_ids == ["ev_selected", "ev_proposed"]
    assert {item.id for item in store.get_evidence_for(run.id, question.id)} == {
        "ev_selected", "ev_proposed"
    }
    assert question.answer_text == "Proposed operational finding."
    assert question.answer_status is AnswerStatus.ANSWERED
    assert question.web_sites == [
        {"url": "https://example.org/proposed", "title": "Proposed", "used": True},
        {"url": "https://example.org/consulted", "title": "Consulted", "used": False},
    ]
    assert report.answers[0]["answer"] == "Proposed operational finding."
    assert result.insight.review_action is ReviewAction.MODIFIED
    assert result.insight.confidence is Confidence.READY


@pytest.mark.asyncio
async def test_apply_updates_only_the_matching_stage_report_answer_row(tmp_path):
    store, run, selected, _ = seeded_store_with_stage_report(tmp_path)
    report = store.get_stage_reports(run.id)[0]
    report.answers.insert(
        0,
        {
            "question": "An unrelated question in the same stage",
            "seed": "",
            "answer": "Unchanged answer.",
            "status": "not_found",
            "citations": [],
        },
    )
    store.save_stage_reports(run.id, [report])
    service = proposal_service(store)
    proposal = await service.propose(run.id, selected.id)

    await service.apply(run.id, selected.id, proposal.id)

    rows = store.get_stage_reports(run.id)[0].answers
    assert rows[0]["answer"] == "Unchanged answer."
    assert rows[1]["answer"] == "Proposed operational finding."


@pytest.mark.asyncio
async def test_apply_promotes_assistant_site_audits_after_the_basis_message(tmp_path):
    store, run, selected, _ = seeded_store_with_stage_report(tmp_path)

    async def proposal_from_first_basis(context, registry, llm_client):
        draft = await deterministic_proposal(context, registry, llm_client)
        return ProposalDraft(
            proposal=draft.proposal.model_copy(
                update={"basis_message_ids": ["wmsg_basis"]}
            ),
            evidence=draft.evidence,
            sites=draft.sites,
        )

    service = proposal_service(store, proposal_builder=proposal_from_first_basis)
    workspace = service.load(run.id, selected.id)
    workspace.messages = [
        InsightWorkspaceMessage(
            role=WorkspaceMessageRole.ASSISTANT,
            content="Earlier research.",
            web_sites=[{"url": "https://example.org/before", "used": False}],
        ),
        InsightWorkspaceMessage(
            id="wmsg_basis",
            role=WorkspaceMessageRole.USER,
            content="Revise this finding with the latest research.",
        ),
        InsightWorkspaceMessage(
            role=WorkspaceMessageRole.ASSISTANT,
            content="Later research consulted a page without a quote.",
            web_sites=[{"url": "https://example.org/after", "used": False}],
        ),
        InsightWorkspaceMessage(
            role=WorkspaceMessageRole.USER,
            content="Research a separate follow-up topic.",
        ),
        InsightWorkspaceMessage(
            role=WorkspaceMessageRole.ASSISTANT,
            content="Separate follow-up research.",
            web_sites=[{"url": "https://example.org/unrelated", "used": False}],
        ),
    ]
    store.save_insight_workspace(workspace)
    proposal = await service.propose(run.id, selected.id)

    await service.apply(run.id, selected.id, proposal.id)

    sites = store.get_questions(run.id)[0].web_sites
    assert {site["url"] for site in sites} >= {"https://example.org/after"}
    assert "https://example.org/before" not in {site["url"] for site in sites}
    assert "https://example.org/unrelated" not in {site["url"] for site in sites}


@pytest.mark.asyncio
async def test_concurrent_apply_serializes_shared_workspace_lock(tmp_path):
    store, run, selected, _ = seeded_store_with_stage_report(tmp_path)
    shared_locks = {}
    proposer = proposal_service(store, shared_locks)
    proposal = await proposer.propose(run.id, selected.id)
    first = proposal_service(store, shared_locks)
    second = proposal_service(store, shared_locks)

    results = await asyncio.gather(
        first.apply(run.id, selected.id, proposal.id),
        second.apply(run.id, selected.id, proposal.id),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    conflict = next(result for result in results if isinstance(result, Exception))
    assert isinstance(conflict, HTTPException)
    assert conflict.status_code == 409
    workspace = proposer.load(run.id, selected.id)
    assert len(workspace.applied_revisions) == 1
    assert workspace.pending_proposal is None


@pytest.mark.asyncio
async def test_proposal_lifecycle_serializes_send_without_losing_messages(tmp_path):
    store, run, selected, _ = seeded_store(tmp_path)
    proposal_started = asyncio.Event()
    release_proposal = asyncio.Event()
    answer_started = asyncio.Event()
    release_answer = asyncio.Event()

    async def blocked_proposal(context, registry, llm_client):
        proposal_started.set()
        await release_proposal.wait()
        return await deterministic_proposal(context, registry, llm_client)

    async def blocked_answer(context, user_text, registry, on_status, llm_client):
        answer_started.set()
        await release_answer.wait()
        return await fake_answer(context, user_text, registry, on_status, llm_client)

    shared_locks = {}
    proposer = proposal_service(
        store,
        lock_registry=shared_locks,
        proposal_builder=blocked_proposal,
    )
    sender = proposal_service(
        store,
        lock_registry=shared_locks,
        answerer=blocked_answer,
    )
    proposal_task = asyncio.create_task(proposer.propose(run.id, selected.id))
    await asyncio.wait_for(proposal_started.wait(), timeout=0.5)

    async def collect_send():
        return [event async for event in sender.send(run.id, selected.id, "Keep this turn.")]

    send_task = asyncio.create_task(collect_send())
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(answer_started.wait()), timeout=0.05)
    finally:
        release_proposal.set()
        proposal = await proposal_task
        await asyncio.wait_for(answer_started.wait(), timeout=0.5)
        release_answer.set()
        events = await asyncio.wait_for(send_task, timeout=0.5)

    workspace = proposer.load(run.id, selected.id)
    assert proposal.id == workspace.pending_proposal.id
    assert [message.content for message in workspace.messages] == [
        "Keep this turn.",
        "Operational rules require gap and regimen-change definitions.",
    ]
    assert events[-1].type is WorkspaceEventType.ANSWER_COMPLETED


@pytest.mark.asyncio
async def test_proposal_replacement_serializes_apply_and_preserves_workspace_state(tmp_path):
    store, run, selected, _ = seeded_store_with_stage_report(tmp_path)
    proposal_started = asyncio.Event()
    release_replacement = asyncio.Event()
    proposal_count = 0

    async def staged_proposal(context, registry, llm_client):
        nonlocal proposal_count
        proposal_count += 1
        draft = await deterministic_proposal(context, registry, llm_client)
        proposal_id = "wprop_initial" if proposal_count == 1 else "wprop_replacement"
        if proposal_count == 2:
            proposal_started.set()
            await release_replacement.wait()
        return ProposalDraft(
            proposal=draft.proposal.model_copy(update={"id": proposal_id}),
            evidence=draft.evidence,
            sites=draft.sites,
        )

    shared_locks = {}
    proposer = proposal_service(
        store,
        lock_registry=shared_locks,
        proposal_builder=staged_proposal,
    )
    applier = proposal_service(store, lock_registry=shared_locks)
    await proposer.propose(run.id, selected.id)
    messages = [event async for event in applier.send(run.id, selected.id, "Keep this turn.")]
    assert messages[-1].type is WorkspaceEventType.ANSWER_COMPLETED

    replacement_task = asyncio.create_task(proposer.propose(run.id, selected.id))
    await asyncio.wait_for(proposal_started.wait(), timeout=0.5)
    apply_task = asyncio.create_task(applier.apply(run.id, selected.id, "wprop_initial"))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(apply_task), timeout=0.05)
    finally:
        release_replacement.set()
        replacement = await replacement_task
        (apply_result,) = await asyncio.gather(apply_task, return_exceptions=True)

    assert isinstance(apply_result, HTTPException)
    assert apply_result.status_code == 409
    workspace = proposer.load(run.id, selected.id)
    assert workspace.pending_proposal.id == replacement.id
    assert len(workspace.applied_revisions) == 0
    assert [message.content for message in workspace.messages] == [
        "Keep this turn.",
        "Operational rules require gap and regimen-change definitions.",
    ]
    assert store.get_insight(run.id, selected.id).summary == selected.summary


@pytest.mark.asyncio
async def test_proposal_lifecycle_lock_does_not_block_different_insight_send(tmp_path):
    store, run, selected, other = seeded_store(tmp_path)
    proposal_started = asyncio.Event()
    release_proposal = asyncio.Event()

    async def blocked_proposal(context, registry, llm_client):
        proposal_started.set()
        await release_proposal.wait()
        return await deterministic_proposal(context, registry, llm_client)

    service = proposal_service(
        store,
        lock_registry={},
        proposal_builder=blocked_proposal,
    )
    proposal_task = asyncio.create_task(service.propose(run.id, selected.id))
    await asyncio.wait_for(proposal_started.wait(), timeout=0.5)
    try:
        other_events = await asyncio.wait_for(
            _collect_workspace_events(
                service.send(run.id, other.id, "Research the other insight.")
            ),
            timeout=0.5,
        )
    finally:
        release_proposal.set()
        await proposal_task

    assert other_events[-1].type is WorkspaceEventType.ANSWER_COMPLETED


async def _collect_workspace_events(stream):
    return [event async for event in stream]
