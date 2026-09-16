"""HTTP contract for one finding's persistent research workspace."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest
import pytest_asyncio

import celestra.main as app_mod
from celestra.models import (
    Evidence,
    EvidenceOrigin,
    Insight,
    InsightRevisionProposal,
    ResearchQuestion,
    Run,
    RunConfig,
    RunStatus,
    WorkspaceMessageRole,
)
from celestra.services.insight_research import ProposalDraft, ResearchTurnResult
from celestra.services.insight_workspace import InsightWorkspaceService
from celestra.store import Store


async def _answer(context, user_text, registry, on_status, llm_client):
    await on_status("checking_evidence")
    return ResearchTurnResult(
        text="Operational line rules need a treatment-free gap and regimen-change definition.",
        evidence=context.evidence,
        citations=["Selected source"],
        source_evidence_ids=[item.id for item in context.evidence],
        searched=False,
        note="",
        sites=[],
    )


async def _summary(prior, messages, llm_client):
    return "Research continuity retained."


async def _proposal(context, registry, llm_client):
    return ProposalDraft(
        proposal=InsightRevisionProposal(
            id="wprop_http",
            proposed_summary="Proposed operational finding from scoped research.",
            change_note="The selected evidence now defines the operational rule.",
            basis_message_ids=[
                message.id for message in context.messages
                if message.role is WorkspaceMessageRole.USER
            ],
            source_ids=[item.id for item in context.evidence],
            base_summary_digest=hashlib.sha256(context.insight.summary.encode()).hexdigest(),
        ),
        evidence=context.evidence,
        sites=[],
    )


def _service(store, answerer=_answer):
    return InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=answerer,
        summarizer=_summary,
        proposal_builder=_proposal,
        llm_client=object(),
        lock_registry={},
    )


@pytest.fixture
def seeded(tmp_path):
    store = Store(tmp_path / "workspace-http.db")
    run = Run(
        id="run_workspace_http",
        config=RunConfig(indication="ALL", indication_key="ALL"),
        status=RunStatus.COMPLETED,
    )
    insight = Insight(
        id="ins_workspace_http",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        category="Logic",
        title="Line-of-therapy rule",
        summary="Treatment gaps and regimen changes need operational definitions.",
        question_ids=["q_workspace_http"],
        evidence_ids=["ev_workspace_http"],
        source_ids=["selected_source"],
    )
    other = Insight(
        id="ins_workspace_other",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        category="Logic",
        title="Unrelated finding",
        summary="OTHER_INSIGHT_SECRET must not enter this workspace.",
        question_ids=["q_workspace_other"],
        evidence_ids=["ev_workspace_other"],
    )
    question = ResearchQuestion(
        id="q_workspace_http",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        text="Which operational line rules are needed?",
    )
    other_question = ResearchQuestion(
        id="q_workspace_other",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        text="What does unrelated evidence say?",
    )
    evidence = Evidence(
        id="ev_workspace_http",
        question_id=question.id,
        source_id="selected_source",
        source_name="Selected source",
        tier=1,
        url="https://example.org/selected",
        quote="A treatment-free gap can define a new line.",
        origin=EvidenceOrigin.APPROVED_API,
    )
    other_evidence = Evidence(
        id="ev_workspace_other",
        question_id=other_question.id,
        source_id="other_source",
        source_name="Other source",
        tier=1,
        url="https://example.org/other",
        quote="OTHER_EVIDENCE_SECRET must remain out of scope.",
        origin=EvidenceOrigin.APPROVED_API,
    )
    store.save_run(run)
    store.save_insights(run.id, [insight, other])
    store.save_questions(run.id, [question, other_question])
    store.save_evidence(run.id, [evidence, other_evidence])
    return run.id, insight.id, store


@pytest_asyncio.fixture
async def client(monkeypatch, seeded):
    _, _, store = seeded
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(
        app_mod, "_insight_workspace_service", lambda: _service(store), raising=False,
    )
    transport = httpx.ASGITransport(app=app_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.mark.asyncio
async def test_workspace_get_creates_and_resumes_one_scoped_workspace(client, seeded):
    run_id, insight_id, _ = seeded
    first = await client.get(f"/runs/{run_id}/insights/{insight_id}/modify")
    second = await client.get(f"/runs/{run_id}/insights/{insight_id}/modify")
    assert first.status_code == 200
    assert "data-insight-workspace" in first.text
    assert "OTHER_INSIGHT_SECRET" not in first.text
    assert second.status_code == 200
    assert app_mod.store.get_insight_workspace(run_id, insight_id) is not None


@pytest.mark.asyncio
async def test_workspace_render_omits_invalid_held_source_links(client, seeded):
    run_id, insight_id, store = seeded
    selected = store.get_evidence(run_id)[0]
    invalid_script = selected.model_copy(update={
        "id": "ev_workspace_script", "source_name": "Unsafe script", "url": "javascript:alert(1)",
    })
    invalid_hostless = selected.model_copy(update={
        "id": "ev_workspace_hostless", "source_name": "Hostless", "url": "http://",
    })
    insight = store.get_insight(run_id, insight_id)
    store.save_insights(run_id, [insight.model_copy(update={
        "evidence_ids": [selected.id, invalid_script.id, invalid_hostless.id],
    })])
    store.save_evidence(run_id, [selected, invalid_script, invalid_hostless])

    response = await client.get(f"/runs/{run_id}/insights/{insight_id}/modify")

    assert response.status_code == 200
    assert 'href="https://example.org/selected"' in response.text
    assert "javascript:alert(1)" not in response.text
    assert 'href="http://"' not in response.text


@pytest.mark.asyncio
async def test_message_route_streams_typed_events_in_order(client, seeded):
    run_id, insight_id, _ = seeded
    response = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/messages",
        json={"message": "Research operational line rules."},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    text = response.text
    positions = [text.index(f"event: {name}") for name in (
        "message_saved", "research_status", "answer_delta", "answer_completed",
    )]
    assert positions == sorted(positions)
    frames = [frame for frame in text.split("\n\n") if frame]
    assert all(frame.startswith("event: ") and "\ndata: " in frame for frame in frames)
    assert all(json.loads(frame.split("\ndata: ", 1)[1]) for frame in frames)


@pytest.mark.asyncio
async def test_proposal_then_apply_returns_workspace_and_card_fragments(client, seeded):
    run_id, insight_id, _ = seeded
    proposal = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposal", json={},
    )
    assert proposal.status_code == 200
    assert "PROPOSED UPDATE" in proposal.json()["proposal_html"]
    proposal_id = proposal.json()["proposal_id"]
    applied = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposals/{proposal_id}/apply",
        json={},
    )
    assert applied.status_code == 200
    assert "card_html" in applied.json() and "workspace_html" in applied.json()
    assert "Proposed operational finding" in applied.json()["card_html"]


@pytest.mark.asyncio
async def test_direct_modify_post_is_rejected_without_mutation(client, seeded):
    run_id, insight_id, _ = seeded
    before = app_mod.store.get_insight(run_id, insight_id).summary
    response = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/modify",
        json={"user_input": "Bypass preview"},
    )
    assert response.status_code == 409
    assert (
        "Direct modification is no longer available. Open Edit, review the proposal, and apply it."
        in response.text
    )
    assert app_mod.store.get_insight(run_id, insight_id).summary == before


@pytest.mark.asyncio
async def test_message_rejects_empty_or_unknown_workspace_before_streaming(client, seeded):
    run_id, insight_id, _ = seeded
    empty = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/messages", json={"message": "   "},
    )
    unknown = await client.post(
        f"/runs/{run_id}/insights/ins_missing/workspace/messages",
        json={"message": "Research this."},
    )
    assert empty.status_code == 400
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_locked_workspace_allows_research_but_rejects_proposal_and_apply(client, seeded):
    run_id, insight_id, store = seeded
    proposal = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposal", json={},
    )
    run = store.get_run(run_id)
    run.status = RunStatus.APPROVED
    store.save_run(run)
    research = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/messages",
        json={"message": "Explain the evidence."},
    )
    blocked_proposal = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposal", json={},
    )
    blocked_apply = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposals/{proposal.json()['proposal_id']}/apply",
        json={},
    )
    assert research.status_code == 200
    assert blocked_proposal.status_code == 409
    assert blocked_apply.status_code == 409


@pytest.mark.asyncio
async def test_stale_proposal_and_failed_research_have_safe_route_contracts(client, monkeypatch, seeded):
    run_id, insight_id, store = seeded
    proposal = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposal", json={},
    )
    changed = store.get_insight(run_id, insight_id)
    changed.summary = "Changed outside the proposal."
    store.save_insights(run_id, [changed])
    stale = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposals/{proposal.json()['proposal_id']}/apply",
        json={},
    )

    async def failing_answer(context, user_text, registry, on_status, llm_client):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        app_mod, "_insight_workspace_service", lambda: _service(store, failing_answer),
    )
    failed = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/messages",
        json={"message": "Research this."},
    )
    assert stale.status_code == 409
    assert "event: error\n" in failed.text
    assert failed.text.endswith("\n\n")
