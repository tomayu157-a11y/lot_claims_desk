"""Business acceptance coverage for a complete selected-card reconciliation."""
from __future__ import annotations

import json
import re

import httpx
import pytest

import celestra.main as app_mod
import celestra.services.answering as answering_mod
from celestra.models import (
    Evidence,
    EvidenceOrigin,
    Insight,
    ResearchQuestion,
    Run,
    RunConfig,
    RunStatus,
    SourceRef,
    StageReport,
)
from celestra.services.azure_web_search import AzureWebSearchOutcome, WebSourceAudit
from celestra.services.insight_reconciliation import insight_card_content
from celestra.services.insight_research import answer_turn
from celestra.services.insight_web_research import InsightWebResearchGateway
from celestra.services.insight_workspace import InsightWorkspaceService
from celestra.store import Store

_WEB_QUOTE = (
    "Use a 60-day treatment-free gap to define a new line of therapy for "
    "administrative claims. Evaluate same-day combination claims together when "
    "constructing the regimen."
)


def _serialized(items):
    return [item.model_dump_json() for item in items]


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9-]+", text.lower()))


class _BusinessModel:
    """Fixture responses for the model boundaries; product logic stays real."""

    available = True

    async def complete_json(self, system, prompt, max_tokens):
        if system.startswith("You decide whether a selected insight"):
            return {
                "needs_more_sources": True,
                "search_query": "ALL claims line construction methodology",
                "reason": "Held evidence is stale and does not define the current rule.",
            }
        if system == "Plan a privacy-safe focused public-web research topic.":
            return {
                "needs_web": True,
                "search_brief": "ALL administrative claims 60-day treatment-free gap same-day combination handling",
                "reason": "The requested operational definition is absent from held evidence.",
            }
        if system == answering_mod._SYSTEM:
            return {
                "status": "answered",
                "answer": (
                    "A 60-day treatment-free gap defines the line boundary, and same-day "
                    "combination claims are assessed together for cohort and line construction."
                ),
                "aspects_covered": ["gap", "same-day combinations"],
                "support": [{"document": 0, "quote": _WEB_QUOTE, "relevance": 0.98}],
            }
        if system.startswith("You are a friendly research partner"):
            evidence_id = re.findall(r"ID: ([^ |]+)", prompt)[-1]
            return {
                "answer": (
                    "For cohort and line construction, use a 60-day treatment-free gap "
                    "and evaluate same-day combination claims together."
                ),
                "status": "answered",
                "applied": True,
                "note": "The operational rule is supported by the attributable source.",
                "support": [{"evidence_id": evidence_id, "quote": _WEB_QUOTE}],
            }
        if system.startswith("Draft a complete, evidence-grounded update"):
            evidence_id = re.findall(r"\[ID: ([^ |]+) \|", prompt)[-1]
            support = [
                {
                    "field": field,
                    "evidence_ids": [evidence_id],
                    "reason": "The attributable methodology source defines this operational rule.",
                }
                for field in (
                    "summary", "detail", "evidence_type", "evidence", "interpretation", "review_note"
                )
            ]
            return {
                "summary": "Use a 60-day treatment-free gap for ALL claims line construction.",
                "detail": (
                    "For cohort and line construction, treat a 60-day treatment-free gap as "
                    "the line boundary and evaluate same-day combination claims together."
                ),
                "evidence_type": "table",
                "evidence": {
                    "columns": ["Operational rule", "Claims handling"],
                    "rows": [
                        ["Treatment-free gap", "60 days"],
                        ["Same-day combination claims", "Evaluate together in one regimen"],
                    ],
                },
                "interpretation": (
                    "Cohort and line construction should apply the 60-day gap consistently "
                    "and keep same-day combination claims in one regimen assessment."
                ),
                "review_note": "Confirm the 60-day and same-day rules in the implementation specification.",
                "support_by_field": support,
                "change_reasons": {
                    field: "The attributable methodology source replaces the stale unsupported rule."
                    for field in (
                        "summary", "detail", "evidence_type", "evidence", "interpretation", "review_note"
                    )
                },
            }
        raise AssertionError(f"Unexpected model system prompt: {system[:80]}")


class _AzureMethodologyFixture:
    def __init__(self):
        self.briefs: list[str] = []

    async def search(self, search_brief, limit):
        self.briefs.append(search_brief)
        url = "https://public.example.org/all-claims-methodology"
        return AzureWebSearchOutcome(
            refs=[
                SourceRef(
                    source_id="all_claims_methodology",
                    source_name="ALL Claims Methodology Council",
                    organization="ALL Claims Methodology Council",
                    tier=3,
                    url=url,
                    title="Claims line construction methodology",
                    snippet=_WEB_QUOTE,
                    raw={"text": _WEB_QUOTE, "page_text": _WEB_QUOTE},
                    origin=EvidenceOrigin.OPEN_WEB,
                )
            ],
            audits=[
                WebSourceAudit(
                    "azure_web_search",
                    url,
                    [search_brief],
                    "hydrated",
                    [{"url": url}],
                    [{"url": url, "title": "Claims line construction methodology"}],
                )
            ],
            ok=True,
            tool_calls=1,
        )


@pytest.mark.asyncio
async def test_insight_chat_reconciles_a_stale_all_rule_without_changing_other_records(
    tmp_path, monkeypatch,
):
    """A synthetic ALL rule changes one complete card, not its surrounding record set."""
    store = Store(tmp_path / "business-e2e.db")
    run = Run(
        id="run_business_e2e",
        config=RunConfig(
            indication="ALL",
            indication_key="ALL",
            objective="Construct a synthetic ALL administrative-claims cohort and treatment lines",
            population="Adults with ALL",
            geography="United States",
        ),
        status=RunStatus.COMPLETED,
    )
    unsupported_restart_row = ["Restart after discontinuation", "Treat as a new line without support"]
    selected = Insight(
        id="ins_business_selected",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        category="Logic",
        title="Synthetic ALL line-construction rule",
        summary="Use a stale 90-day treatment-free gap for line construction.",
        detail="The existing cohort rule applies a 90-day gap before a new line.",
        evidence_type="table",
        evidence={
            "columns": ["Operational rule", "Claims handling"],
            "rows": [
                ["Treatment-free gap", "90 days"],
                unsupported_restart_row,
            ],
        },
        interpretation="The 90-day gap and unsupported restart handling drive cohort construction.",
        review_note="Validate the 90-day gap and restart rule.",
        question_ids=["q_business_selected"],
        evidence_ids=["ev_business_stale"],
        source_ids=["stale_internal_method"],
    )
    other = Insight(
        id="ins_business_other",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        category="Logic",
        title="Unrelated synthetic ALL finding",
        summary="OTHER_CARD_MUST_REMAIN_BYTE_IDENTICAL",
        question_ids=["q_business_other"],
        evidence_ids=["ev_business_other"],
    )
    question = ResearchQuestion(
        id="q_business_selected",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        text="Which operational rules define ALL claims cohorts and treatment lines?",
    )
    other_question = ResearchQuestion(
        id="q_business_other",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        text="Which unrelated synthetic finding is unchanged?",
    )
    stale_evidence = Evidence(
        id="ev_business_stale",
        question_id=question.id,
        source_id="stale_internal_method",
        source_name="Stale internal method",
        tier=1,
        url="https://example.org/stale-rule",
        quote="A legacy draft used a 90-day gap and an unsupported restart rule.",
        origin=EvidenceOrigin.APPROVED_API,
    )
    other_evidence = Evidence(
        id="ev_business_other",
        question_id=other_question.id,
        source_id="other_source",
        source_name="Other source",
        tier=1,
        url="https://example.org/other",
        quote="OTHER_EVIDENCE_MUST_REMAIN_BYTE_IDENTICAL.",
        origin=EvidenceOrigin.APPROVED_API,
    )
    stage_report = StageReport(
        id="stage_business",
        run_id=run.id,
        stage="stage_2",
        bucket="C",
        name="Synthetic methodology report",
        core_question=question.text,
        agent_name="Methodology",
        answers=[{"question": question.text, "answer": "Original staged answer."}],
        tables=[{"title": "Synthetic report table", "columns": ["Rule"], "rows": [["Original"]]}],
    )
    store.save_run(run)
    store.save_insights(run.id, [selected, other])
    store.save_questions(run.id, [question, other_question])
    store.save_evidence(run.id, [stale_evidence, other_evidence])
    store.save_stage_reports(run.id, [stage_report])

    model = _BusinessModel()
    azure = _AzureMethodologyFixture()

    async def business_answerer(context, user_text, registry, on_status, llm_client):
        gateway = InsightWebResearchGateway(
            azure_client=azure,
            registry=registry,
            llm_client=model,
        )
        return await answer_turn(
            context,
            user_text,
            registry,
            on_status,
            llm_client=model,
            research_gateway=gateway,
        )

    service = InsightWorkspaceService(
        store=store,
        registry_factory=dict,
        answerer=business_answerer,
        llm_client=model,
        lock_registry={},
    )
    monkeypatch.setattr(answering_mod, "llm", model)
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(app_mod, "_insight_workspace_service", lambda: service)

    before_questions = _serialized(store.get_questions(run.id))
    before_reports = _serialized(store.get_stage_reports(run.id))
    before_other = store.get_insight(run.id, other.id).model_dump_json()
    before_selected = store.get_insight(run.id, selected.id).model_dump_json()

    transport = httpx.ASGITransport(app=app_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        opened = await client.get(f"/runs/{run.id}/insights/{selected.id}/modify")
        assert opened.status_code == 200
        assert "Synthetic ALL line-construction rule" in opened.text

        answer = await client.post(
            f"/runs/{run.id}/insights/{selected.id}/workspace/messages",
            json={"message": "Verify the operational rule and explain what must change for cohort and line construction."},
        )
        assert answer.status_code == 200
        assert "60-day" in answer.text
        assert "same-day" in answer.text
        assert "cohort and line construction" in answer.text
        assert azure.briefs == [
            "ALL administrative claims 60-day treatment-free gap same-day combination handling"
        ]
        answer_completed = next(
            json.loads(frame.split("\ndata: ", 1)[1])
            for frame in answer.text.split("\n\n")
            if frame.startswith("event: answer_completed")
        )
        answer_workspace = service.load(run.id, selected.id)
        answer_sources = {source.id: source for source in answer_workspace.sources}
        assert answer_completed["source_ids"]
        assert set(answer_completed["source_ids"]) == {
            source["id"] for source in answer_completed["sources"]
        }
        for public_source in answer_completed["sources"]:
            validated_source = answer_sources[public_source["id"]]
            assert public_source["quote"] == validated_source.quote == _WEB_QUOTE
            assert public_source["url"] == validated_source.url
            assert public_source["source_name"] == validated_source.source_name

        proposal_response = await client.post(
            f"/runs/{run.id}/insights/{selected.id}/workspace/proposal", json={},
        )
        assert proposal_response.status_code == 200
        proposal_payload = proposal_response.json()
        assert "Complete proposed finding" in proposal_payload["proposal_html"]
        assert "60-day" in proposal_payload["proposal_html"]

        # Proposal preview is read-only across the card and every protected neighbour.
        assert store.get_insight(run.id, selected.id).model_dump_json() == before_selected
        assert _serialized(store.get_questions(run.id)) == before_questions
        assert _serialized(store.get_stage_reports(run.id)) == before_reports
        assert store.get_insight(run.id, other.id).model_dump_json() == before_other

        workspace = service.load(run.id, selected.id)
        proposal = workspace.pending_proposal
        assert proposal is not None
        proposed_content = proposal.after_content.model_dump(mode="json")
        assert ["Treatment-free gap", "90 days"] not in proposed_content["evidence"]["rows"]
        assert "ev_business_stale" not in proposed_content["evidence_ids"]
        assert "stale_internal_method" not in proposed_content["source_ids"]
        evidence_by_id = {item.id: item for item in workspace.sources}
        for change in proposal.changed_fields:
            if change.field in {"summary", "detail", "evidence", "interpretation"}:
                assert change.evidence_ids
                for evidence_id in change.evidence_ids:
                    source = evidence_by_id[evidence_id]
                    assert source.quote in _WEB_QUOTE

        applied_response = await client.post(
            f"/runs/{run.id}/insights/{selected.id}/workspace/proposals/{proposal_payload['proposal_id']}/apply",
            json={},
        )
        assert applied_response.status_code == 200
        assert "60-day" in applied_response.json()["card_html"]

        reopened = await client.get(f"/runs/{run.id}/insights/{selected.id}/modify")
        assert reopened.status_code == 200
        assert "60-day" in reopened.text

        exported = await client.get(f"/runs/{run.id}/export")
        assert exported.status_code == 200

    applied = store.get_insight(run.id, selected.id)
    applied_content = insight_card_content(applied).model_dump(mode="json")
    assert applied_content == proposed_content
    assert "60" in applied.summary + applied.detail
    assert "90-day" not in applied.summary + applied.detail
    assert any("same-day" in " ".join(map(str, row)).lower() for row in applied.evidence["rows"])
    assert unsupported_restart_row not in applied.evidence["rows"]
    assert ["Treatment-free gap", "90 days"] not in applied.evidence["rows"]
    assert applied.review_note == proposed_content["review_note"]
    assert applied.evidence_ids == proposed_content["evidence_ids"]
    assert applied.source_ids == proposed_content["source_ids"]
    assert {"cohort", "line", "construction"} <= _tokens(applied.interpretation)
    assert proposed_content == next(
        insight_card_content(Insight.model_validate(card)).model_dump(mode="json")
        for card in exported.json()["insights"] if card["id"] == selected.id
    )
    assert _serialized(store.get_questions(run.id)) == before_questions
    assert _serialized(store.get_stage_reports(run.id)) == before_reports
    assert store.get_insight(run.id, other.id).model_dump_json() == before_other
