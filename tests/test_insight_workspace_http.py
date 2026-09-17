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
    InsightCardContent,
    InsightCardFieldChange,
    InsightFieldSupport,
    InsightRevisionProposal,
    ResearchQuestion,
    Run,
    RunConfig,
    RunStatus,
    WorkspaceMessageRole,
)
from celestra.services.insight_reconciliation import insight_card_digest
from celestra.services.insight_research import ProposalDraft, ResearchTurnResult
from celestra.services.insight_workspace import InsightWorkspaceService
from celestra.services.llm import LLMUnavailable
from celestra.store import Store


async def _answer(context, user_text, registry, on_status, llm_client):
    await on_status("checking_evidence")
    await on_status("searching_web_azure")
    await on_status("azure_unavailable_trying_firecrawl")
    await on_status("reading_validating_sources")
    await on_status("research_completed")
    return ResearchTurnResult(
        text="Operational line rules need a treatment-free gap and regimen-change definition.",
        evidence=context.evidence,
        citations=["Selected source"],
        source_evidence_ids=[item.id for item in context.evidence],
        searched=False,
        note="",
        sites=[],
        source_audit={
            context.evidence[0].url: {
                "search_provider": "azure_web_search",
                "search_queries": ["sanitized research brief"],
                "hydration_status": "hydrated",
                "citation_metadata": [{"url": context.evidence[0].url}],
            },
        },
    )


async def _summary(prior, messages, llm_client):
    return "Research continuity retained."


async def _proposal(context, registry, llm_client):
    supplementary = Evidence(
        id="ev_workspace_proposed",
        question_id=context.question.id,
        source_id="supplementary_source",
        source_name="Supplementary source",
        tier=3,
        url="https://example.org/proposed",
        quote="Supplementary evidence supports an updated operational rule.",
        origin=EvidenceOrigin.OPEN_WEB,
    )
    evidence = [*context.evidence, supplementary]
    before = InsightCardContent(
        summary=context.insight.summary,
        detail=context.insight.detail,
        evidence_type=context.insight.evidence_type,
        evidence=context.insight.evidence,
        interpretation=context.insight.interpretation,
        review_note=context.insight.review_note,
        covered=context.insight.covered,
        input_reason=context.insight.input_reason,
        evidence_ids=context.insight.evidence_ids,
        source_ids=context.insight.source_ids,
        used_web_fallback=context.insight.used_web_fallback,
    )
    after = before.model_copy(update={
        "summary": "Proposed operational finding from scoped research.",
        "detail": "The selected evidence defines the full operational rule.",
        "evidence_type": "steps",
        "evidence": ["Apply a treatment-free gap before a regimen change."],
        "interpretation": "Use the rule consistently for cohort construction.",
        "review_note": "Confirm the implementation date with the reviewer.",
        "evidence_ids": [item.id for item in evidence],
        "source_ids": [item.source_id for item in evidence],
        "used_web_fallback": True,
    })
    return ProposalDraft(
        proposal=InsightRevisionProposal(
            id="wprop_http",
            proposed_summary="Proposed operational finding from scoped research.",
            change_note="The selected evidence now defines the operational rule.",
            basis_message_ids=[
                message.id for message in context.messages
                if message.role is WorkspaceMessageRole.USER
            ],
            source_ids=[item.id for item in evidence],
            base_summary_digest=hashlib.sha256(context.insight.summary.encode()).hexdigest(),
            before_content=before,
            after_content=after,
            changed_fields=[
                InsightCardFieldChange(
                    field="summary",
                    kind="changed",
                    before=before.summary,
                    after=after.summary,
                    evidence_ids=[item.id for item in evidence],
                    reason="The selected evidence now defines the operational rule.",
                ),
            ],
            support_by_field=[
                InsightFieldSupport(field=field, evidence_ids=[item.id for item in evidence])
                for field in ("summary", "detail", "evidence", "interpretation")
            ],
            unchanged_fields=[],
            base_content_digest=insight_card_digest(context.insight),
        ),
        evidence=evidence,
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
        identifiers={"member_id": "MEM-privacy-test"},
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
    statuses = [
        json.loads(frame.split("\ndata: ", 1)[1])["detail"]
        for frame in frames if frame.startswith("event: research_status")
    ]
    assert statuses == [
        "checking_evidence", "searching_web_azure", "azure_unavailable_trying_firecrawl",
        "reading_validating_sources", "research_completed",
    ]
    assert text.index("azure_unavailable_trying_firecrawl") < text.index("event: answer_completed")
    completion = json.loads(next(
        frame.split("\ndata: ", 1)[1] for frame in frames
        if frame.startswith("event: answer_completed")
    ))
    assert completion["sources"]
    assert {"id", "source_id", "source_name", "url", "quote"} <= set(completion["sources"][0])
    assert not ({"search_provider", "search_queries", "hydration_status", "citation_metadata"}
                & set(completion["sources"][0]))
    assert "sanitized research brief" not in text
    assert "identifiers" not in completion["sources"][0]
    assert "MEM-privacy-test" not in text


@pytest.mark.asyncio
async def test_workspace_routes_keep_their_methods_and_ignore_client_supplied_revision_state(client, seeded):
    """A route change that trusts body-supplied card data must fail this contract."""
    run_id, insight_id, _ = seeded
    proposal = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposal",
        json={"card": {"summary": "Client supplied"}, "authorization": True},
    )
    assert proposal.status_code == 200
    assert set(proposal.json()) == {"proposal_id", "proposal_html"}

    applied = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposals/{proposal.json()['proposal_id']}/apply",
        json={
            "card": {"summary": "Client supplied"},
            "authorization": True,
            "digest": "client-digest",
            "sources": ["client-source"],
        },
    )
    assert applied.status_code == 200
    assert set(applied.json()) == {"card_html", "workspace_html"}
    assert "Client supplied" not in applied.json()["card_html"]


@pytest.mark.asyncio
async def test_azure_and_firecrawl_completion_payloads_share_one_private_free_source_shape(
    client, monkeypatch, seeded,
):
    """Provider audit must not create a provider-specific or identifying SSE source payload."""
    run_id, insight_id, store = seeded

    def answerer_for(provider):
        async def answer(context, user_text, registry, on_status, llm_client):
            await on_status("searching_web_azure" if provider == "azure_web_search" else "azure_unavailable_trying_firecrawl")
            return ResearchTurnResult(
                text="Provider-neutral research result.",
                evidence=context.evidence,
                citations=[],
                source_evidence_ids=[item.id for item in context.evidence],
                searched=True,
                note="",
                sites=[],
                source_audit={
                    context.evidence[0].url: {
                        "search_provider": provider,
                        "search_queries": ["sanitized query secret"],
                        "hydration_status": "hydrated",
                        "citation_metadata": [{"raw_response": "provider raw secret"}],
                        "claims_context": "claims context secret",
                    },
                },
            )
        return answer

    source_keys = []
    for provider in ("azure_web_search", "firecrawl"):
        monkeypatch.setattr(
            app_mod, "_insight_workspace_service",
            lambda provider=provider: _service(store, answerer_for(provider)),
        )
        response = await client.post(
            f"/runs/{run_id}/insights/{insight_id}/workspace/messages",
            json={"message": f"Research through {provider}."},
        )
        completion = next(
            json.loads(frame.split("\ndata: ", 1)[1])
            for frame in response.text.split("\n\n")
            if frame.startswith("event: answer_completed")
        )
        source_keys.append(set(completion["sources"][0]))
        assert not ({"identifiers", "search_provider", "search_queries", "hydration_status",
                    "citation_metadata", "claims_context", "raw_response"}
                    & set(completion["sources"][0]))
        assert "sanitized query secret" not in response.text
        assert "provider raw secret" not in response.text
        assert "claims context secret" not in response.text
        assert "MEM-privacy-test" not in response.text

    assert source_keys[0] == source_keys[1]


@pytest.mark.asyncio
async def test_proposal_then_apply_refreshes_card_sources_and_evidence_panel(client, seeded):
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
    assert 'data-sources="2"' in applied.json()["card_html"]
    assert 'data-source-key="selected_source"' in applied.json()["card_html"]
    assert 'data-source-key="supplementary_source"' in applied.json()["card_html"]
    assert "ev_workspace_http" not in applied.json()["card_html"]

    evidence = await client.get(f"/runs/{run_id}/insights/{insight_id}/evidence")
    assert evidence.status_code == 200
    assert "2 items" in evidence.text
    assert "Supplementary evidence supports an updated operational rule." in evidence.text


@pytest.mark.asyncio
async def test_unsupported_factual_preview_disables_apply_and_route_preserves_state(
    client, monkeypatch, seeded,
):
    run_id, insight_id, store = seeded

    async def unsupported_proposal(context, registry, llm_client):
        draft = await _proposal(context, registry, llm_client)
        return ProposalDraft(
            proposal=draft.proposal.model_copy(update={
                "support_by_field": [],
                "applyable": False,
                "unsupported_factual_fields": ["summary", "detail", "evidence", "interpretation"],
                "after_content": draft.proposal.after_content.model_copy(update={
                    "covered": False,
                    "input_reason": "Evidence support is required for the proposed factual update.",
                }),
            }),
            evidence=draft.evidence,
            sites=draft.sites,
        )

    monkeypatch.setattr(app_mod, "_insight_workspace_service", lambda: InsightWorkspaceService(
        store=store, registry_factory=dict, answerer=_answer, summarizer=_summary,
        proposal_builder=unsupported_proposal, llm_client=object(), lock_registry={},
    ))
    proposed = await client.post(f"/runs/{run_id}/insights/{insight_id}/workspace/proposal", json={})
    assert proposed.status_code == 200
    assert "Evidence support is required before this factual update can be applied" in proposed.json()["proposal_html"]
    assert "Summary, Detail, Structured evidence, Interpretation" in proposed.json()["proposal_html"]
    assert "data-workspace-apply-url" not in proposed.json()["proposal_html"]

    before_insight = store.get_insight(run_id, insight_id).model_dump_json()
    before_evidence = [item.model_dump_json() for item in store.get_evidence(run_id)]
    before_workspace = store.get_insight_workspace(run_id, insight_id).model_dump_json()
    rejected = await client.post(
        f"/runs/{run_id}/insights/{insight_id}/workspace/proposals/{proposed.json()['proposal_id']}/apply",
        json={},
    )

    assert rejected.status_code == 409
    assert "Summary, Detail, Structured evidence, Interpretation" in rejected.text
    assert store.get_insight(run_id, insight_id).model_dump_json() == before_insight
    assert [item.model_dump_json() for item in store.get_evidence(run_id)] == before_evidence
    assert store.get_insight_workspace(run_id, insight_id).model_dump_json() == before_workspace


@pytest.mark.asyncio
async def test_proposal_reports_an_unavailable_model_without_creating_a_proposal(
    monkeypatch, seeded,
):
    """Removing the model availability guard must make this 503 contract fail."""
    run_id, insight_id, store = seeded

    class UnavailableModel:
        available = False

    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(
        app_mod,
        "_insight_workspace_service",
        lambda: InsightWorkspaceService(
            store=store,
            registry_factory=dict,
            llm_client=UnavailableModel(),
            lock_registry={},
        ),
    )
    before = store.get_insight(run_id, insight_id)
    transport = httpx.ASGITransport(app=app_mod.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as raw_client:
        response = await raw_client.post(
            f"/runs/{run_id}/insights/{insight_id}/workspace/proposal",
            json={},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "The model is unavailable. Try again."}
    assert store.get_insight(run_id, insight_id) == before
    assert store.get_insight_workspace(run_id, insight_id).pending_proposal is None


@pytest.mark.asyncio
async def test_proposal_reports_a_failed_model_provider_without_creating_a_proposal(
    monkeypatch, seeded,
):
    """A provider outage is retryable, not unsupported evidence."""
    run_id, insight_id, store = seeded

    class FailingProviderModel:
        available = True

        async def complete_json(self, *args, **kwargs):
            raise LLMUnavailable("provider timed out")

    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(
        app_mod,
        "_insight_workspace_service",
        lambda: InsightWorkspaceService(
            store=store,
            registry_factory=dict,
            llm_client=FailingProviderModel(),
            lock_registry={},
        ),
    )
    transport = httpx.ASGITransport(app=app_mod.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as raw_client:
        response = await raw_client.post(
            f"/runs/{run_id}/insights/{insight_id}/workspace/proposal",
            json={},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "The model is unavailable. Try again."}
    assert store.get_insight_workspace(run_id, insight_id).pending_proposal is None


@pytest.mark.asyncio
async def test_proposal_reports_unsupported_evidence_without_creating_a_proposal(
    monkeypatch, seeded,
):
    """Returning no supported rewrite must not become an internal-server error."""
    run_id, insight_id, store = seeded

    class UnsupportedProposalModel:
        available = True

        async def complete_json(self, *args, **kwargs):
            if "needs_more_sources" in args[1]:
                return {"needs_more_sources": False, "search_query": "", "reason": ""}
            return {"answer": "", "applied": False, "note": "More verified evidence is needed."}

    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(
        app_mod,
        "_insight_workspace_service",
        lambda: InsightWorkspaceService(
            store=store,
            registry_factory=dict,
            llm_client=UnsupportedProposalModel(),
            lock_registry={},
        ),
    )
    transport = httpx.ASGITransport(app=app_mod.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as raw_client:
        response = await raw_client.post(
            f"/runs/{run_id}/insights/{insight_id}/workspace/proposal",
            json={},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "More verified evidence is needed."}
    assert store.get_insight_workspace(run_id, insight_id).pending_proposal is None


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
        "Direct modification is no longer available. Open Chat &amp; edit, review the proposal, and apply it."
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
