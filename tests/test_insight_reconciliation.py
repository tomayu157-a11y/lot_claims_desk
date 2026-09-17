import hashlib
import json

import pytest

from celestra.models import (
    Confidence,
    Insight,
    InsightCardContent,
    ReviewAction,
    VerificationTag,
)
from celestra.services.insight_reconciliation import (
    diff_card_content,
    insight_card_content,
    insight_card_digest,
    validate_evidence_payload,
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
