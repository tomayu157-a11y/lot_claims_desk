"""Canonical full-card snapshots and digests for insight revisions."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass

from ..models import (
    Confidence,
    Evidence,
    Insight,
    InsightCardContent,
    InsightCardFieldChange,
    InsightFieldSupport,
    InsightRevisionProposal,
    VerificationTag,
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

EDITORIAL_CARD_FIELDS = (
    "summary",
    "detail",
    "evidence_type",
    "evidence",
    "interpretation",
    "review_note",
)


class CardProposalInvalid(ValueError):
    """Raised when a generated card cannot be reconciled safely."""


@dataclass(frozen=True)
class DerivedCardState:
    covered: bool
    input_reason: str
    confidence: Confidence
    tag: VerificationTag
    used_web_fallback: bool


class InsightCardReconciler:
    """Build one deterministic full-card proposal from selected evidence only."""

    def propose(
        self,
        insight: Insight,
        selected_evidence: list[Evidence],
        editorial_content: dict[str, object],
        support_by_field: list[InsightFieldSupport],
        change_reasons: dict[str, str],
        basis_message_ids: list[str],
    ) -> InsightRevisionProposal:
        self._validate_editorial_content(editorial_content, change_reasons)
        validate_evidence_payload(
            str(editorial_content["evidence_type"]), editorial_content["evidence"],
        )
        before = insight_card_content(insight)
        support = self._validated_support(selected_evidence, support_by_field)
        candidate = InsightCardContent(**editorial_content).model_copy(update={
            field: getattr(before, field)
            for field in MUTABLE_CARD_FIELDS
            if field not in EDITORIAL_CARD_FIELDS
        })
        initial_diff = diff_card_content(before, candidate)
        self._validate_editorial_reasons(initial_diff.changes, change_reasons)

        supported_ids = self._active_support_ids(selected_evidence, support)
        unsupported_fields = self.unsupported_factual_fields(before, candidate, support)
        covered = not unsupported_fields
        input_reason = "" if covered else "Evidence support is required for the proposed factual update."
        evidence_by_id = {item.id: item for item in selected_evidence}
        preserved_ids = [item_id for item_id in before.evidence_ids if item_id in evidence_by_id]
        active_ids = _stable_unique([*preserved_ids, *supported_ids])
        active_evidence = [
            evidence_by_id[item_id] for item_id in active_ids if item_id in evidence_by_id
        ]
        derived = self.derive_state(covered, input_reason, active_evidence)
        after = candidate.model_copy(update={
            "covered": derived.covered,
            "input_reason": derived.input_reason,
            "evidence_ids": active_ids,
            "source_ids": _stable_unique([
                *(
                    before.source_ids
                    if preserved_ids
                    else []
                ),
                *(item.source_id for item in active_evidence),
            ]),
            "used_web_fallback": (
                before.used_web_fallback if preserved_ids else False
            ) or derived.used_web_fallback,
        })
        diff = diff_card_content(before, after)
        if not diff.changes:
            raise CardProposalInvalid(
                "The conversation and evidence do not support a material card update."
            )
        support_models = [support[field] for field in EDITORIAL_CARD_FIELDS if field in support]
        changes = [
            change.model_copy(update={
                "evidence_ids": list(support.get(change.field, InsightFieldSupport(field=change.field)).evidence_ids),
                "reason": change_reasons.get(change.field, ""),
            })
            for change in diff.changes
        ]
        return InsightRevisionProposal(
            proposed_summary=after.summary,
            change_note=_proposal_note(change_reasons, diff.changes),
            basis_message_ids=basis_message_ids,
            source_ids=after.evidence_ids,
            web_sites=[
                {
                    "url": item.url,
                    "title": item.title,
                    "scraped": item.is_supplementary,
                    "used": True,
                }
                for item in active_evidence
                if item.is_supplementary
            ],
            before_content=before,
            after_content=after,
            changed_fields=changes,
            unchanged_fields=diff.unchanged_fields,
            support_by_field=support_models,
            change_reasons=change_reasons,
            applyable=not unsupported_fields,
            unsupported_factual_fields=unsupported_fields,
            base_summary_digest=hashlib.sha256(insight.summary.encode()).hexdigest(),
            base_content_digest=insight_card_digest(insight),
        )

    @staticmethod
    def derive_state(
        covered: bool, input_reason: str, active_evidence: list[Evidence],
    ) -> DerivedCardState:
        if not covered:
            return DerivedCardState(
                covered=False,
                input_reason=input_reason,
                confidence=Confidence.REQUIRES_INPUT,
                tag=VerificationTag.NOT_VERIFIED,
                used_web_fallback=any(item.is_supplementary for item in active_evidence),
            )
        tag = (
            VerificationTag.GENERAL_KNOWLEDGE
            if active_evidence and all(item.is_supplementary for item in active_evidence)
            else VerificationTag.VERIFIED
        )
        return DerivedCardState(
            covered=True,
            input_reason="",
            confidence=Confidence.READY,
            tag=tag,
            used_web_fallback=any(item.is_supplementary for item in active_evidence),
        )

    @staticmethod
    def _validate_editorial_content(
        editorial_content: dict[str, object], change_reasons: dict[str, str],
    ) -> None:
        expected = set(EDITORIAL_CARD_FIELDS)
        if set(editorial_content) != expected:
            raise CardProposalInvalid("The model did not return a complete card proposal.")
        if set(change_reasons) - expected:
            raise CardProposalInvalid("The model returned invalid change reasons.")
        if not all(isinstance(editorial_content[field], str) for field in EDITORIAL_CARD_FIELDS if field != "evidence"):
            raise CardProposalInvalid("The model did not return a complete card proposal.")

    @staticmethod
    def _validate_editorial_reasons(
        changes: list[InsightCardFieldChange], change_reasons: dict[str, str],
    ) -> None:
        missing = [
            change.field
            for change in changes
            if not change_reasons.get(change.field, "").strip()
        ]
        if missing:
            raise CardProposalInvalid("Every changed editorial field requires a reason.")

    @staticmethod
    def _validated_support(
        selected_evidence: list[Evidence], support_by_field: list[InsightFieldSupport],
    ) -> dict[str, InsightFieldSupport]:
        evidence_by_id = {item.id: item for item in selected_evidence if item.quote.strip()}
        support: dict[str, InsightFieldSupport] = {}
        for item in support_by_field:
            if item.field not in EDITORIAL_CARD_FIELDS or item.field in support:
                raise CardProposalInvalid("The model returned invalid field support.")
            if any(not evidence_id or evidence_id not in evidence_by_id for evidence_id in item.evidence_ids):
                raise CardProposalInvalid("The model referenced evidence outside this insight.")
            support[item.field] = item
        return support

    @staticmethod
    def _active_support_ids(
        selected_evidence: list[Evidence], support: dict[str, InsightFieldSupport],
    ) -> list[str]:
        selected_ids = {
            evidence_id
            for item in support.values()
            for evidence_id in item.evidence_ids
        }
        return [item.id for item in selected_evidence if item.id in selected_ids]

    @staticmethod
    def active_evidence(
        after: InsightCardContent, selected_evidence: list[Evidence],
    ) -> list[Evidence]:
        active_ids = set(after.evidence_ids)
        return [item for item in selected_evidence if item.id in active_ids]

    @staticmethod
    def unsupported_factual_fields(
        before: InsightCardContent,
        after: InsightCardContent,
        support_by_field: dict[str, InsightFieldSupport] | list[InsightFieldSupport],
    ) -> list[str]:
        """Return non-removal factual changes that lack server-validated support.

        A factual removal is allowed to leave the affected content empty and
        require input. A non-empty factual replacement must identify support.
        Review notes and evidence-format presentation changes are editorial.
        """
        support = (
            support_by_field
            if isinstance(support_by_field, dict)
            else {item.field: item for item in support_by_field}
        )
        unsupported: list[str] = []
        for change in diff_card_content(before, after).changes:
            if change.field not in {"summary", "detail", "evidence", "interpretation"}:
                continue
            if not InsightCardReconciler._is_factual_replacement(change):
                continue
            if not support.get(change.field, InsightFieldSupport(field=change.field)).evidence_ids:
                unsupported.append(change.field)
        return unsupported

    @staticmethod
    def _is_factual_replacement(change: InsightCardFieldChange) -> bool:
        if change.field == "evidence":
            if _is_empty(change.after):
                return False
            return not change.item_changes or any(
                item.get("kind") in {"added", "changed"} for item in change.item_changes
            )
        return isinstance(change.after, str) and bool(change.after.strip())


def _stable_unique(values: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


def _proposal_note(
    change_reasons: dict[str, str], changes: list[InsightCardFieldChange],
) -> str:
    reasons = [change_reasons.get(change.field, "").strip() for change in changes]
    return next((reason for reason in reasons if reason), "Full-card update proposed from the scoped conversation.")


def insight_card_content(insight: Insight) -> InsightCardContent:
    """Return exactly the content a full-card revision may replace."""
    return InsightCardContent(**{
        field: deepcopy(getattr(insight, field)) if field == "evidence" else getattr(insight, field)
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
    if any(not _non_empty_text(cell) for row in rows for cell in row):
        raise ValueError("table row values must be non-empty strings")


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
