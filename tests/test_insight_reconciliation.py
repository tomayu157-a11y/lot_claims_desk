import hashlib
import json

import pytest

from celestra.models import (
    Confidence,
    Evidence,
    EvidenceOrigin,
    Insight,
    InsightCardContent,
    InsightFieldSupport,
    ReviewAction,
    VerificationTag,
)
from celestra.services.insight_reconciliation import (
    CardProposalInvalid,
    InsightCardReconciler,
    diff_card_content,
    insight_card_content,
    insight_card_digest,
    validate_evidence_payload,
)


def selected_evidence(*, origin=EvidenceOrigin.APPROVED_API) -> Evidence:
    return Evidence(
        id="ev_selected", question_id="question_one", source_id="source_selected",
        source_name="Selected source", tier=2, url="https://example.org/selected",
        quote="Treatment changes can indicate a new line.", origin=origin,
    )


def full_editorial(**updates) -> dict[str, object]:
    content: dict[str, object] = {
        "summary": "Updated summary",
        "detail": "",
        "evidence_type": "steps",
        "evidence": ["Observe treatment", "Apply the revised rule"],
        "interpretation": "Updated interpretation",
        "review_note": "",
    }
    content.update(updates)
    return content


def test_reconciler_builds_a_complete_snapshot_and_reports_step_removal():
    insight = populated_insight(
        evidence_type="steps",
        evidence=["Observe treatment", "Remove stale step"],
    )
    evidence = selected_evidence()

    proposal = InsightCardReconciler().propose(
        insight,
        [evidence],
        full_editorial(),
        [
            InsightFieldSupport(field="summary", evidence_ids=[evidence.id]),
            InsightFieldSupport(field="detail", evidence_ids=[evidence.id]),
            InsightFieldSupport(field="evidence", evidence_ids=[evidence.id]),
            InsightFieldSupport(field="interpretation", evidence_ids=[evidence.id]),
        ],
        {
            "summary": "The selected evidence supports the revised finding.",
            "detail": "The stale detail was removed.",
            "evidence": "The revised rule replaces the stale step.",
            "interpretation": "The interpretation now follows the selected evidence.",
            "review_note": "The stale note was removed.",
        },
        ["wmsg_one"],
    )

    assert proposal.after_content.model_dump() == {
        "summary": "Updated summary",
        "detail": "",
        "evidence_type": "steps",
        "evidence": ["Observe treatment", "Apply the revised rule"],
        "interpretation": "Updated interpretation",
        "review_note": "",
        "covered": True,
        "input_reason": "",
        "evidence_ids": ["ev_selected"],
        "source_ids": ["source_selected"],
        "used_web_fallback": False,
    }
    evidence_change = next(change for change in proposal.changed_fields if change.field == "evidence")
    assert evidence_change.item_changes == [
        {"kind": "removed", "value": "Remove stale step", "occurrence": 0},
        {"kind": "added", "value": "Apply the revised rule", "occurrence": 0},
    ]


def test_reconciler_rejects_unknown_support_instead_of_verifying_it():
    with pytest.raises(CardProposalInvalid, match="outside this insight"):
        InsightCardReconciler().propose(
            populated_insight(), [selected_evidence()], full_editorial(),
            [InsightFieldSupport(field="summary", evidence_ids=["ev_other_workspace"])],
            {"summary": "Unsupported evidence must not be accepted."}, [],
        )


def test_reconciler_rejects_selected_evidence_without_an_exact_quote():
    unverifiable = selected_evidence().model_copy(update={"id": "ev_unverifiable", "quote": ""})

    with pytest.raises(CardProposalInvalid, match="outside this insight"):
        InsightCardReconciler().propose(
            populated_insight(), [unverifiable], full_editorial(),
            [InsightFieldSupport(field="summary", evidence_ids=[unverifiable.id])],
            {"summary": "A quote is required before support can be verified."}, [],
        )


def test_reconciler_orders_active_evidence_and_source_ids_by_selected_context():
    first = selected_evidence().model_copy(update={"id": "ev_first", "source_id": "source_one"})
    second = selected_evidence().model_copy(update={"id": "ev_second", "source_id": "source_one"})
    support = [
        InsightFieldSupport(field=field, evidence_ids=[second.id, first.id])
        for field in ("summary", "detail", "evidence", "interpretation")
    ]

    proposal = InsightCardReconciler().propose(
        populated_insight(), [first, second], full_editorial(), support, {}, [],
    )

    assert proposal.after_content.evidence_ids == ["ev_first", "ev_second"]
    assert proposal.after_content.source_ids == ["source_one"]


def test_reconciler_marks_missing_replacement_support_as_requiring_input():
    proposal = InsightCardReconciler().propose(
        populated_insight(), [selected_evidence()], full_editorial(), [], {}, [],
    )

    assert proposal.after_content.covered is False
    assert proposal.after_content.input_reason == "Evidence support is required for the proposed factual update."
    derived = InsightCardReconciler().derive_state(
        proposal.after_content.covered,
        proposal.after_content.input_reason,
        [],
    )
    assert derived.confidence is Confidence.REQUIRES_INPUT
    assert derived.tag is VerificationTag.NOT_VERIFIED


def test_reconciler_derives_general_knowledge_for_supplementary_only_support():
    supplementary = selected_evidence(origin=EvidenceOrigin.OPEN_WEB)
    proposal = InsightCardReconciler().propose(
        populated_insight(), [supplementary], full_editorial(),
        [
            InsightFieldSupport(field="summary", evidence_ids=[supplementary.id]),
            InsightFieldSupport(field="detail", evidence_ids=[supplementary.id]),
            InsightFieldSupport(field="evidence", evidence_ids=[supplementary.id]),
            InsightFieldSupport(field="interpretation", evidence_ids=[supplementary.id]),
        ],
        {}, [],
    )

    derived = InsightCardReconciler().derive_state(
        proposal.after_content.covered,
        proposal.after_content.input_reason,
        [supplementary],
    )
    assert derived.confidence is Confidence.READY
    assert derived.tag is VerificationTag.GENERAL_KNOWLEDGE
    assert proposal.after_content.used_web_fallback is True


def test_reconciler_rejects_a_byte_for_byte_equivalent_card_snapshot():
    insight = populated_insight()
    current = insight_card_content(insight)

    with pytest.raises(
        CardProposalInvalid,
        match="The conversation and evidence do not support a material card update.",
    ):
        InsightCardReconciler().propose(
            insight, [],
            {
                "summary": current.summary,
                "detail": current.detail,
                "evidence_type": current.evidence_type,
                "evidence": current.evidence,
                "interpretation": current.interpretation,
                "review_note": current.review_note,
            },
            [], {}, [],
        )


def populated_insight(**updates) -> Insight:
    values = {
        "id": "ins_fixed",
        "run_id": "run_fixed",
        "stage": "stage_fixed",
        "bucket": "bucket_fixed",
        "category": "category_fixed",
        "title": "Fixed title",
        "summary": "Current summary",
        "detail": "Current detail",
        "number": 7,
        "card_key": "fixed-card-key",
        "evidence_type": "metrics",
        "evidence": [{"label": "Median gap", "value": "60 days"}],
        "interpretation": "Current interpretation",
        "review_note": "Current review note",
        "covered": False,
        "confidence": Confidence.REQUIRES_INPUT,
        "input_reason": "Current input reason",
        "tag": VerificationTag.NOT_VERIFIED,
        "evidence_ids": ["ev_one", "ev_two"],
        "source_ids": ["source_one", "source_two"],
        "question_ids": ["question_one"],
        "used_web_fallback": True,
        "review_action": ReviewAction.MODIFIED,
        "user_input": "Fixed user input",
        "revision_note": "Fixed revision note",
        "reviewer_input": "Fixed reviewer input",
        "impacted_insight_ids": ["ins_other"],
        "table_titles": ["Fixed table"],
    }
    values.update(updates)
    return Insight(**values)


def test_card_snapshot_and_digest_cover_only_mutable_card_content():
    insight = populated_insight()

    assert insight_card_content(insight).model_dump() == {
        "summary": "Current summary",
        "detail": "Current detail",
        "evidence_type": "metrics",
        "evidence": [{"label": "Median gap", "value": "60 days"}],
        "interpretation": "Current interpretation",
        "review_note": "Current review note",
        "covered": False,
        "input_reason": "Current input reason",
        "evidence_ids": ["ev_one", "ev_two"],
        "source_ids": ["source_one", "source_two"],
        "used_web_fallback": True,
    }

    base = insight_card_digest(insight)
    assert insight_card_digest(insight.model_copy(update={"summary": "Revised summary"})) != base
    assert insight_card_digest(insight.model_copy(update={"evidence_ids": ["ev_two", "ev_one"]})) != base
    assert insight_card_digest(insight.model_copy(update={"title": "Different fixed title"})) == base
    assert insight_card_digest(insight.model_copy(update={"review_action": ReviewAction.APPROVED})) == base


def test_card_digest_is_the_compact_sorted_json_hash_without_reordering_lists():
    insight = populated_insight(evidence=[{"value": "60 days", "label": "Median gap"}])
    expected_payload = {
        "summary": "Current summary",
        "detail": "Current detail",
        "evidence_type": "metrics",
        "evidence": [{"label": "Median gap", "value": "60 days"}],
        "interpretation": "Current interpretation",
        "review_note": "Current review note",
        "covered": False,
        "input_reason": "Current input reason",
        "evidence_ids": ["ev_one", "ev_two"],
        "source_ids": ["source_one", "source_two"],
        "used_web_fallback": True,
    }
    expected = hashlib.sha256(
        json.dumps(expected_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    assert insight_card_digest(insight) == expected
    assert insight_card_digest(insight) != insight_card_digest(
        insight.model_copy(update={"evidence_ids": ["ev_two", "ev_one"]})
    )


@pytest.mark.parametrize(
    ("evidence_type", "evidence"),
    [
        ("", None),
        ("metrics", [{"label": "Median gap", "value": "60 days"}]),
        ("table", {"columns": ["Rule", "Value"], "rows": [["Gap", "60 days"]]}),
        ("steps", ["Observe treatment", "Apply gap rule"]),
        ("list", ["Pharmacy claim", "Medical claim"]),
    ],
)
def test_evidence_payload_accepts_only_the_declared_card_shape(evidence_type, evidence):
    assert validate_evidence_payload(evidence_type, evidence) == evidence


@pytest.mark.parametrize(
    ("evidence_type", "evidence"),
    [
        ("metrics", [{"label": "", "value": "60 days"}]),
        ("metrics", [{"label": "Median gap", "value": "", "extra": "no"}]),
        ("table", {"columns": ["Rule"]}),
        ("table", {"rows": [["Gap", "60 days"]]}),
        ("table", {"columns": ["Rule", "Value"], "rows": [["Gap"]]}),
        ("table", {"columns": ["Rule"], "rows": [["Gap"]], "extra": True}),
        ("steps", ["Observe treatment", ""]),
        ("list", {"item": "Pharmacy claim"}),
        ("", []),
    ],
)
def test_evidence_payload_rejects_mismatched_or_incomplete_card_shapes(evidence_type, evidence):
    with pytest.raises(ValueError):
        validate_evidence_payload(evidence_type, evidence)


def test_evidence_payload_rejects_a_wrong_container_type():
    with pytest.raises(TypeError):
        validate_evidence_payload("metrics", {"label": "Median gap", "value": "60 days"})


def test_table_evidence_rejects_blank_row_cells():
    with pytest.raises(ValueError):
        validate_evidence_payload("table", {"columns": ["Rule"], "rows": [[""]]})


def test_card_snapshot_does_not_share_the_insight_evidence_object():
    insight = populated_insight(evidence=[{"label": "Median gap", "value": "60 days"}])
    content = insight_card_content(insight)

    insight.evidence.append({"label": "New gap", "value": "90 days"})

    assert content.evidence == [{"label": "Median gap", "value": "60 days"}]


def test_diff_reports_scalar_and_source_link_additions_removals_and_changes():
    before = InsightCardContent(
        summary="", detail="Stale detail", interpretation="Earlier interpretation",
        evidence_ids=["ev_one"], source_ids=["source_one", "source_two"],
    )
    after = InsightCardContent(
        summary="Updated summary", detail="", interpretation="Updated interpretation",
        evidence_ids=["ev_one", "ev_two"], source_ids=["source_two"],
    )

    result = diff_card_content(before, after)

    assert result.removed is True
    assert result.unchanged_fields == [
        "evidence_type", "evidence", "review_note", "covered", "input_reason", "used_web_fallback",
    ]
    assert [(change.field, change.kind) for change in result.changes] == [
        ("summary", "added"),
        ("detail", "removed"),
        ("interpretation", "changed"),
        ("evidence_ids", "changed"),
        ("source_ids", "changed"),
    ]
    assert result.changes[3].item_changes == [
        {"kind": "added", "value": "ev_two", "occurrence": 0},
    ]
    assert result.changes[4].item_changes == [
        {"kind": "removed", "value": "source_one", "occurrence": 0},
    ]


def test_diff_tracks_metrics_by_label_and_occurrence_without_losing_edits():
    before = InsightCardContent(
        summary="Finding", evidence_type="metrics",
        evidence=[
            {"label": "Median gap", "value": "60 days"},
            {"label": "Median gap", "value": "90 days"},
            {"label": "Retired", "value": "1"},
        ],
    )
    after = before.model_copy(update={"evidence": [
        {"label": "Median gap", "value": "61 days"},
        {"label": "Median gap", "value": "90 days"},
        {"label": "Added", "value": "2"},
    ]})

    result = diff_card_content(before, after)

    evidence_change = result.changes[0]
    assert evidence_change.field == "evidence"
    assert evidence_change.kind == "changed"
    assert evidence_change.item_changes == [
        {
            "kind": "changed", "identity": {"label": "Median gap", "occurrence": 0},
            "before": {"label": "Median gap", "value": "60 days"},
            "after": {"label": "Median gap", "value": "61 days"},
        },
        {
            "kind": "removed", "identity": {"label": "Retired", "occurrence": 0},
            "before": {"label": "Retired", "value": "1"}, "after": None,
        },
        {
            "kind": "added", "identity": {"label": "Added", "occurrence": 0},
            "before": None, "after": {"label": "Added", "value": "2"},
        },
    ]


@pytest.mark.parametrize("evidence_type", ["steps", "list"])
def test_diff_tracks_ordered_steps_and_lists_by_value_and_occurrence(evidence_type):
    before = InsightCardContent(
        summary="Finding", evidence_type=evidence_type, evidence=["Keep", "Remove", "Keep"],
    )
    after = before.model_copy(update={"evidence": ["Keep", "Keep", "Add"]})

    result = diff_card_content(before, after)

    assert result.removed is True
    assert result.changes[0].item_changes == [
        {"kind": "removed", "value": "Remove", "occurrence": 0},
        {"kind": "added", "value": "Add", "occurrence": 0},
    ]


def test_diff_tracks_table_rows_by_value_and_occurrence_without_row_ids():
    before = InsightCardContent(
        summary="Finding", evidence_type="table",
        evidence={"columns": ["Rule", "Value"], "rows": [["Gap", "60 days"], ["Remove", "1"]]},
    )
    after = before.model_copy(update={"evidence": {
        "columns": ["Rule", "Value"], "rows": [["Gap", "61 days"], ["Added", "2"]],
    }})

    result = diff_card_content(before, after)

    assert result.removed is True
    assert result.changes[0].item_changes == [
        {
            "kind": "changed", "identity": {"row": ["Gap", "60 days"], "occurrence": 0},
            "before": ["Gap", "60 days"], "after": ["Gap", "61 days"],
        },
        {
            "kind": "removed", "identity": {"row": ["Remove", "1"], "occurrence": 0},
            "before": ["Remove", "1"], "after": None,
        },
        {
            "kind": "added", "identity": {"row": ["Added", "2"], "occurrence": 0},
            "before": None, "after": ["Added", "2"],
        },
    ]
