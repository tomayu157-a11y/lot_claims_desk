"""Canonical full-card snapshots and digests for insight revisions."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..models import (
    Insight,
    InsightCardContent,
    InsightCardFieldChange,
    InsightRevisionProposal,
)

MUTABLE_CARD_FIELDS = (
    "summary",
    "detail",
    "evidence_type",
    "evidence",
    "interpretation",
    "review_note",
    "covered",
    "input_reason",
    "evidence_ids",
    "source_ids",
    "used_web_fallback",
)


def insight_card_content(insight: Insight) -> InsightCardContent:
    """Return exactly the content a full-card revision may replace."""
    return InsightCardContent(**{
        field: getattr(insight, field)
        for field in MUTABLE_CARD_FIELDS
    })


def insight_card_digest(insight: Insight) -> str:
    """Return the stable digest used to detect a stale full-card proposal."""
    payload = insight_card_content(insight).model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_evidence_payload(evidence_type: str, evidence: object) -> object:
    """Reject card evidence that does not match its declared display shape."""
    if evidence_type == "":
        if evidence is not None:
            raise ValueError("empty evidence_type requires no evidence payload")
        return evidence
    if evidence_type == "metrics":
        _validate_metrics(evidence)
    elif evidence_type == "table":
        _validate_table(evidence)
    elif evidence_type in {"steps", "list"}:
        _validate_text_items(evidence)
    else:
        raise ValueError("evidence_type must be metrics, table, steps, list, or empty")
    return evidence


def _validate_metrics(evidence: object) -> None:
    if not isinstance(evidence, list):
        raise TypeError("metrics evidence must be a list")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"label", "value"}:
            raise ValueError("metric items must contain only label and value")
        if not _non_empty_text(item["label"]) or not _non_empty_text(item["value"]):
            raise ValueError("metric labels and values must be non-empty strings")


def _validate_table(evidence: object) -> None:
    if not isinstance(evidence, dict) or set(evidence) != {"columns", "rows"}:
        raise ValueError("table evidence must contain only columns and rows")
    columns = evidence["columns"]
    rows = evidence["rows"]
    if not isinstance(columns, list) or not columns or not all(_non_empty_text(item) for item in columns):
        raise ValueError("table columns must be a non-empty list of non-empty strings")
    if not isinstance(rows, list) or not rows:
        raise ValueError("table rows must be a non-empty list")
    if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        raise ValueError("every table row must match the column width")


def _validate_text_items(evidence: object) -> None:
    if not isinstance(evidence, list) or not all(_non_empty_text(item) for item in evidence):
        raise ValueError("steps and lists must contain only non-empty strings")


def _non_empty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class CardContentDiff:
    changes: list[InsightCardFieldChange]
    unchanged_fields: list[str]
    removed: bool


def diff_card_content(before: InsightCardContent, after: InsightCardContent) -> CardContentDiff:
    """Describe every mutable-card difference without assigning synthetic item ids."""
    changes: list[InsightCardFieldChange] = []
    for field in MUTABLE_CARD_FIELDS:
        before_value = getattr(before, field)
        after_value = getattr(after, field)
        if before_value == after_value:
            continue
        item_changes = _item_changes(
            field, before.evidence_type, after.evidence_type, before_value, after_value,
        )
        changes.append(InsightCardFieldChange(
            field=field,
            kind=_change_kind(before_value, after_value),
            before=before_value,
            after=after_value,
            item_changes=item_changes,
        ))
    unchanged_fields = [
        field for field in MUTABLE_CARD_FIELDS
        if getattr(before, field) == getattr(after, field)
    ]
    removed = any(
        change.kind == "removed"
        or any(item["kind"] == "removed" for item in change.item_changes)
        for change in changes
    )
    return CardContentDiff(changes=changes, unchanged_fields=unchanged_fields, removed=removed)


def is_complete_card_proposal(proposal: InsightRevisionProposal | None) -> bool:
    """Return whether a proposal carries every contract needed for a full-card Apply."""
    if proposal is None or not proposal.base_content_digest:
        return False
    if proposal.before_content is None or proposal.after_content is None:
        return False
    expected_fields = set(MUTABLE_CARD_FIELDS)
    return (
        expected_fields.issubset(proposal.before_content.model_fields_set)
        and expected_fields.issubset(proposal.after_content.model_fields_set)
    )


def _change_kind(before: object, after: object) -> str:
    if _is_empty(before) and not _is_empty(after):
        return "added"
    if not _is_empty(before) and _is_empty(after):
        return "removed"
    return "changed"


def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == []


def _item_changes(
    field: str,
    before_type: str,
    after_type: str,
    before: object,
    after: object,
) -> list[dict[str, object]]:
    if field in {"evidence_ids", "source_ids"}:
        return _value_item_changes(before, after)
    if field != "evidence" or before_type != after_type:
        return []
    if before_type == "metrics":
        return _metric_item_changes(before, after)
    if before_type == "table":
        return _table_item_changes(before, after)
    if before_type in {"steps", "list"}:
        return _value_item_changes(before, after)
    return []


def _value_item_changes(before: object, after: object) -> list[dict[str, object]]:
    if not isinstance(before, list) or not isinstance(after, list):
        return []
    before_identities = _value_identities(before)
    after_identities = _value_identities(after)
    return [
        {"kind": "removed", "value": value, "occurrence": occurrence}
        for value, occurrence in before_identities
        if (value, occurrence) not in after_identities
    ] + [
        {"kind": "added", "value": value, "occurrence": occurrence}
        for value, occurrence in after_identities
        if (value, occurrence) not in before_identities
    ]


def _value_identities(values: list[object]) -> list[tuple[object, int]]:
    occurrences: dict[str, int] = {}
    identities: list[tuple[object, int]] = []
    for value in values:
        key = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        identities.append((value, occurrence))
    return identities


def _metric_item_changes(before: object, after: object) -> list[dict[str, object]]:
    if not isinstance(before, list) or not isinstance(after, list):
        return []
    before_items = _metric_identities(before)
    after_items = _metric_identities(after)
    changes: list[dict[str, object]] = []
    for identity, before_item in before_items.items():
        after_item = after_items.get(identity)
        if after_item is None:
            changes.append({
                "kind": "removed", "identity": _metric_identity(identity),
                "before": before_item, "after": None,
            })
        elif before_item != after_item:
            changes.append({
                "kind": "changed", "identity": _metric_identity(identity),
                "before": before_item, "after": after_item,
            })
    for identity, after_item in after_items.items():
        if identity not in before_items:
            changes.append({
                "kind": "added", "identity": _metric_identity(identity),
                "before": None, "after": after_item,
            })
    return changes


def _metric_identities(items: list[object]) -> dict[tuple[str, int], dict[str, object]]:
    occurrences: dict[str, int] = {}
    identified: dict[tuple[str, int], dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            return {}
        label = item["label"]
        occurrence = occurrences.get(label, 0)
        occurrences[label] = occurrence + 1
        identified[(label, occurrence)] = item
    return identified


def _metric_identity(value: tuple[str, int]) -> dict[str, object]:
    return {"label": value[0], "occurrence": value[1]}


def _table_item_changes(before: object, after: object) -> list[dict[str, object]]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    before_rows = before.get("rows")
    after_rows = after.get("rows")
    if before.get("columns") != after.get("columns") or not isinstance(before_rows, list) or not isinstance(after_rows, list):
        return []

    remaining_after = list(enumerate(after_rows))
    changes: list[dict[str, object]] = []
    for before_index, before_row in enumerate(before_rows):
        exact_index = next(
            (index for index, (_, after_row) in enumerate(remaining_after) if after_row == before_row),
            None,
        )
        if exact_index is not None:
            remaining_after.pop(exact_index)
            continue
        edited_index = next(
            (
                index
                for index, (_, after_row) in enumerate(remaining_after)
                if _table_row_key(after_row) == _table_row_key(before_row)
            ),
            None,
        )
        identity = _table_row_identity(before_rows, before_index)
        if edited_index is None:
            changes.append({"kind": "removed", "identity": identity, "before": before_row, "after": None})
            continue
        _, after_row = remaining_after.pop(edited_index)
        changes.append({"kind": "changed", "identity": identity, "before": before_row, "after": after_row})
    for after_index, after_row in remaining_after:
        changes.append({
            "kind": "added", "identity": _table_row_identity(after_rows, after_index),
            "before": None, "after": after_row,
        })
    return changes


def _table_row_key(row: object) -> object:
    return row[0] if isinstance(row, list) and row else row


def _table_row_identity(rows: list[object], index: int) -> dict[str, object]:
    row = rows[index]
    occurrence = sum(1 for value in rows[:index] if value == row)
    return {"row": row, "occurrence": occurrence}
