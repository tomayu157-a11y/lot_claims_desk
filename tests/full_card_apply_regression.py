"""Deterministic script-test coverage for a narrow full-card Apply."""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import httpx

import celestra.main as app_mod
import celestra.services.orchestrator as orch_mod
from celestra.models import (
    Evidence,
    EvidenceOrigin,
    Insight,
    InsightCardContent,
    InsightFieldSupport,
    InsightRevisionProposal,
    ResearchQuestion,
    Run,
    RunConfig,
    RunStatus,
    StageReport,
)
from celestra.services import qa as qa_mod
from celestra.services.insight_reconciliation import (
    diff_card_content,
    insight_card_content,
    insight_card_digest,
)
from celestra.services.insight_research import ProposalDraft
from celestra.services.insight_workspace import InsightWorkspaceService
from celestra.store import Store


async def assert_full_card_apply_regression() -> None:
    """Exercise selected-card Apply in review, export, and approved-report views."""
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "full-card-apply.db")
        run = Run(
            id="run_full_card_regression",
            config=RunConfig(indication="ALL", indication_key="ALL"),
            status=RunStatus.COMPLETED,
        )
        run.agents = orch_mod.build_agent_states(["A", "C"])
        selected = Insight(
            id="ins_full_card_selected",
            run_id=run.id,
            stage="stage_1",
            bucket="C",
            category="Logic",
            title="Deterministic line rule",
            summary="Use a 90-day gap.",
            detail="The legacy line rule uses 90 days.",
            evidence_type="table",
            evidence={
                "columns": ["Rule", "Value"],
                "rows": [["Treatment-free gap", "90 days"]],
            },
            interpretation="The cohort applies the legacy gap.",
            review_note="Review the legacy rule.",
            question_ids=["q_full_card_selected"],
            evidence_ids=["ev_full_card_selected"],
            source_ids=["full_card_methodology"],
        )
        other = Insight(
            id="ins_full_card_other",
            run_id=run.id,
            stage="stage_1",
            bucket="C",
            category="Logic",
            title="Other card",
            summary="OTHER_CARD_BYTE_IDENTICAL",
            question_ids=["q_full_card_other"],
            evidence_ids=["ev_full_card_other"],
        )
        question = ResearchQuestion(
            id="q_full_card_selected",
            run_id=run.id,
            stage="stage_1",
            bucket="C",
            text="How should a treatment line be constructed?",
        )
        other_question = ResearchQuestion(
            id="q_full_card_other",
            run_id=run.id,
            stage="stage_1",
            bucket="C",
            text="Which unrelated finding stays unchanged?",
        )
        evidence = Evidence(
            id="ev_full_card_selected",
            question_id=question.id,
            source_id="full_card_methodology",
            source_name="Deterministic methodology",
            tier=1,
            url="https://example.org/full-card-methodology",
            quote="A 60-day treatment-free gap defines the new treatment line.",
            origin=EvidenceOrigin.APPROVED_API,
        )
        other_evidence = Evidence(
            id="ev_full_card_other",
            question_id=other_question.id,
            source_id="other_methodology",
            source_name="Other methodology",
            tier=1,
            url="https://example.org/other-methodology",
            quote="Other evidence remains unchanged.",
            origin=EvidenceOrigin.APPROVED_API,
        )
        report = StageReport(
            id="stage_full_card",
            run_id=run.id,
            stage="stage_1",
            bucket="C",
            name="Deterministic stage report",
            core_question=question.text,
            agent_name="Methodology",
            answers=[{"question": question.text, "answer": "Original stage answer."}],
            tables=[{"title": "Deterministic table", "columns": ["Rule"], "rows": [["Original"]]}],
        )
        store.save_run(run)
        store.save_insights(run.id, [selected, other])
        store.save_questions(run.id, [question, other_question])
        store.save_evidence(run.id, [evidence, other_evidence])
        store.save_stage_reports(run.id, [report])

        async def proposal_builder(context, registry, llm_client):
            before = insight_card_content(context.insight)
            after = InsightCardContent(
                summary="Use a 60-day gap.",
                detail="The reconciled line rule uses a 60-day treatment-free gap.",
                evidence_type="table",
                evidence={
                    "columns": ["Rule", "Value"],
                    "rows": [["Treatment-free gap", "60 days"]],
                },
                interpretation="Cohort construction applies the 60-day gap.",
                review_note="Confirm the 60-day rule.",
                evidence_ids=[evidence.id],
                source_ids=[evidence.source_id],
            )
            diff = diff_card_content(before, after)
            return ProposalDraft(
                proposal=InsightRevisionProposal(
                    id="wprop_full_card_regression",
                    proposed_summary=after.summary,
                    change_note="The supported 60-day rule replaces the legacy rule.",
                    source_ids=[evidence.id],
                    base_summary_digest=hashlib.sha256(context.insight.summary.encode()).hexdigest(),
                    before_content=before,
                    after_content=after,
                    changed_fields=diff.changes,
                    unchanged_fields=diff.unchanged_fields,
                    support_by_field=[
                        InsightFieldSupport(field=field, evidence_ids=[evidence.id])
                        for field in ("summary", "detail", "evidence", "interpretation")
                    ],
                    applyable=True,
                    unsupported_factual_fields=[],
                    base_content_digest=insight_card_digest(context.insight),
                ),
                evidence=context.evidence,
            )

        service = InsightWorkspaceService(
            store=store,
            registry_factory=dict,
            proposal_builder=proposal_builder,
            llm_client=object(),
            lock_registry={},
        )
        before_questions = [item.model_dump_json() for item in store.get_questions(run.id)]
        before_reports = [item.model_dump_json() for item in store.get_stage_reports(run.id)]
        before_other = store.get_insight(run.id, other.id).model_dump_json()
        previous_store = app_mod.store
        previous_service = app_mod._insight_workspace_service
        previous_complete_json = qa_mod.llm.complete_json

        async def deterministic_qa_model_response(*_args, **_kwargs) -> dict[str, object]:
            return {
                "readiness": "Deterministic approval readiness for regression coverage.",
                "sme_checklist": ["Verify the approved evidence before use."],
            }

        app_mod.store = store
        app_mod._insight_workspace_service = lambda: service
        qa_mod.llm.complete_json = deterministic_qa_model_response
        try:
            transport = httpx.ASGITransport(app=app_mod.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                proposal = await client.post(
                    f"/runs/{run.id}/insights/{selected.id}/workspace/proposal", json={},
                )
                assert proposal.status_code == 200
                proposal_id = proposal.json()["proposal_id"]
                expected_content = service.load(run.id, selected.id).pending_proposal.after_content
                assert expected_content is not None

                applied = await client.post(
                    f"/runs/{run.id}/insights/{selected.id}/workspace/proposals/{proposal_id}/apply",
                    json={},
                )
                assert applied.status_code == 200
                assert "Use a 60-day gap." in applied.json()["card_html"]
                assert "90-day" not in applied.json()["card_html"]

                review = await client.get(f"/runs/{run.id}/review")
                assert review.status_code == 200
                assert "Use a 60-day gap." in review.text

                exported = await client.get(f"/runs/{run.id}/export")
                assert exported.status_code == 200
                exported_card = next(card for card in exported.json()["insights"] if card["id"] == selected.id)
                assert insight_card_content(Insight.model_validate(exported_card)) == expected_content

                approval = await client.post(f"/runs/{run.id}/approve")
                assert approval.status_code == 303
                assert approval.headers["location"].endswith("/report")

                approved_run = store.get_run(run.id)
                assert approved_run.status is RunStatus.APPROVED
                assert approved_run.is_locked
                assert approved_run.approved_at is not None
                approval_qa = store.get_qa(run.id)
                assert approval_qa is not None
                assert any(item["check"] == "Reviewer sign-off" for item in approval_qa.checklist)

                approved_report = await client.get(f"/runs/{run.id}/findings")
                assert approved_report.status_code == 200
                assert "Use a 60-day gap." in approved_report.text

            applied_card = store.get_insight(run.id, selected.id)
            assert insight_card_content(applied_card) == expected_content
            assert applied_card.review_action.value == "modified"
            assert ["Treatment-free gap", "90 days"] not in applied_card.evidence["rows"]
            assert [item.model_dump_json() for item in store.get_questions(run.id)] == before_questions
            assert [item.model_dump_json() for item in store.get_stage_reports(run.id)] == before_reports
            assert store.get_insight(run.id, other.id).model_dump_json() == before_other
        finally:
            app_mod.store = previous_store
            app_mod._insight_workspace_service = previous_service
            qa_mod.llm.complete_json = previous_complete_json
            store._conn().close()
