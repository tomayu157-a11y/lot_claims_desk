import asyncio
import hashlib
import sqlite3

import pytest
from fastapi import HTTPException

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
    assert all(
        evidence.id != "ev_duplicate"
        for evidence in store.get_evidence_for(run.id, selected.question_ids[0])
    )


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


def proposal_service(store, lock_registry=None):
    return InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=fake_answer,
        summarizer=fake_summary,
        proposal_builder=deterministic_proposal,
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
    assert service.load(run.id, selected.id).pending_proposal == proposal
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
    assert result.insight.source_ids == ["ev_selected", "ev_proposed"]
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
    service = proposal_service(store)
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
    ]
    store.save_insight_workspace(workspace)
    proposal = await service.propose(run.id, selected.id)

    await service.apply(run.id, selected.id, proposal.id)

    sites = store.get_questions(run.id)[0].web_sites
    assert {site["url"] for site in sites} >= {"https://example.org/after"}
    assert "https://example.org/before" not in {site["url"] for site in sites}


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
