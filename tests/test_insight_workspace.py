import asyncio

import pytest

from celestra.models import (
    Evidence,
    EvidenceOrigin,
    Insight,
    InsightWorkspaceMessage,
    ResearchQuestion,
    Run,
    RunConfig,
    RunStatus,
    WorkspaceEventType,
    WorkspaceMessageRole,
    WorkspaceMessageState,
)
from celestra.services.insight_research import ResearchTurnResult
from celestra.services.insight_workspace import InsightWorkspaceService
from celestra.store import Store


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

    async def summarizer(prior, messages, llm_client):
        calls.append([message.id for message in messages])
        return "Goal and citations retained."

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=fake_answer,
        summarizer=summarizer,
        llm_client=object(),
        active_token_limit=40,
        reduced_token_target=20,
    )
    initial = service.load(run.id, selected.id)
    initial.messages = long_completed_messages()
    store.save_insight_workspace(initial)
    before = len(initial.messages)
    [event async for event in service.send(run.id, selected.id, "Continue")]
    saved = service.load(run.id, selected.id)
    assert calls
    assert len(saved.messages) == before + 2
    assert saved.continuity_summary == "Goal and citations retained."
    assert saved.summarized_through_message_id


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
