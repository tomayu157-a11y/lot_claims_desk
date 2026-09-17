from pathlib import Path

import pytest
from pydantic import ValidationError

from celestra.models import (
    AppliedInsightRevision,
    AppliedRevisionResult,
    EvidenceOrigin,
    Insight,
    InsightCardContent,
    InsightCardFieldChange,
    InsightFieldSupport,
    InsightRevisionProposal,
    InsightWorkspace,
    InsightWorkspaceMessage,
    InsightWorkspaceSource,
    WorkspaceEvent,
    WorkspaceMessageRole,
    WorkspaceMessageState,
    workspace_id,
)
from celestra.services.insight_reconciliation import is_complete_card_proposal
from celestra.store import Store


def workspace(run_id: str = "run_one", insight_id: str = "ins_one") -> InsightWorkspace:
    source = InsightWorkspaceSource(
        id="ev_one", question_id="q_one", source_id="open_web", source_name="Open web",
        tier=3, url="https://example.org/lot", title="LoT methods",
        quote="A treatment-free gap may define a new line.", origin=EvidenceOrigin.OPEN_WEB,
    )
    message = InsightWorkspaceMessage(
        id="wmsg_one", role=WorkspaceMessageRole.USER,
        state=WorkspaceMessageState.COMPLETED, content="Research line changes.",
    )
    return InsightWorkspace(
        id=workspace_id(run_id, insight_id), run_id=run_id,
        insight_id=insight_id, messages=[message], sources=[source],
    )


def test_workspace_id_is_stable_and_scoped():
    assert workspace_id("run_one", "ins_one") == workspace_id("run_one", "ins_one")
    assert workspace_id("run_one", "ins_one") != workspace_id("run_one", "ins_two")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://"])
def test_workspace_source_rejects_non_http_or_hostless_url(url):
    with pytest.raises(ValidationError):
        InsightWorkspaceSource(
            id="ev_bad", question_id="q_one", source_id="bad", source_name="Bad", tier=3,
            url=url, quote="unsafe",
        )


def test_workspace_source_accepts_http_url_with_hostname():
    source = InsightWorkspaceSource(
        id="ev_valid", question_id="q_one", source_id="valid", source_name="Valid", tier=3,
        url="https://example.org/lot", quote="safe",
    )
    assert source.url == "https://example.org/lot"


def test_workspace_round_trips_and_isolated_by_run_and_insight(tmp_path: Path):
    store = Store(tmp_path / "workspace.db")
    first = workspace()
    second = workspace("run_one", "ins_two")
    store.save_insight_workspace(first)
    store.save_insight_workspace(second)
    assert store.get_insight_workspace("run_one", "ins_one") == first
    assert store.get_insight_workspace("run_one", "ins_two") == second
    assert store.get_insight_workspace("run_two", "ins_one") is None


def test_deleting_run_deletes_its_workspaces(tmp_path: Path):
    store = Store(tmp_path / "workspace.db")
    store.save_insight_workspace(workspace())
    store.delete_run("run_one")
    assert store.get_insight_workspace("run_one", "ins_one") is None


def test_workspace_requires_canonical_id_and_updates_the_same_pair(tmp_path: Path):
    with pytest.raises(ValidationError):
        InsightWorkspace(id="iws_other", run_id="run_one", insight_id="ins_one")

    store = Store(tmp_path / "workspace.db")
    first = workspace()
    updated = first.model_copy(update={"continuity_summary": "Updated research context."})
    store.save_insight_workspace(first)
    store.save_insight_workspace(updated)
    assert store.get_insight_workspace("run_one", "ins_one") == updated


def test_workspace_models_reject_unknown_fields():
    with pytest.raises(ValidationError):
        InsightWorkspaceSource(
            id="ev_one", question_id="q_one", source_id="open_web", source_name="Open web",
            tier=3, url="https://example.org/lot", quote="Known quote.", unexpected="value",
        )
    with pytest.raises(ValidationError):
        InsightWorkspaceMessage(role=WorkspaceMessageRole.USER, unexpected="value")


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: InsightWorkspace(
                id=workspace_id("run_one", "ins_one"), run_id="run_one", insight_id="ins_one",
                unexpected_workspace="value",
            ),
            id="workspace",
        ),
        pytest.param(
            lambda: InsightRevisionProposal(
                proposed_summary="Proposed.", change_note="Reason.", base_summary_digest="digest",
                unexpected_proposal="value",
            ),
            id="revision-proposal",
        ),
        pytest.param(
            lambda: AppliedInsightRevision(
                proposal_id="wprop_one", previous_summary="Before.", applied_summary="After.",
                unexpected_applied_revision="value",
            ),
            id="applied-revision",
        ),
        pytest.param(
            lambda: AppliedRevisionResult(
                insight=Insight(
                    stage="stage_1", bucket="A", category="Clinical", title="Finding",
                    summary="Summary.",
                ),
                workspace=workspace(),
                unexpected_result="value",
            ),
            id="applied-revision-result",
        ),
        pytest.param(
            lambda: WorkspaceEvent(type="message_saved", unexpected_event="value"),
            id="workspace-event",
        ),
    ],
)
def test_remaining_workspace_models_reject_unknown_fields(factory):
    with pytest.raises(ValidationError):
        factory()


def test_workspace_rejects_duplicate_and_dangling_source_references():
    source = workspace().sources[0]
    with pytest.raises(ValidationError):
        InsightWorkspace(
            id=workspace_id("run_one", "ins_one"), run_id="run_one", insight_id="ins_one",
            sources=[source, source],
        )

    message = InsightWorkspaceMessage(role=WorkspaceMessageRole.USER, source_ids=["ev_missing"])
    with pytest.raises(ValidationError):
        InsightWorkspace(
            id=workspace_id("run_one", "ins_one"), run_id="run_one", insight_id="ins_one",
            messages=[message], sources=[source],
        )

    proposal = InsightRevisionProposal(
        proposed_summary="Revised finding.", change_note="Updated research.",
        base_summary_digest="digest", source_ids=["ev_missing"],
    )
    with pytest.raises(ValidationError):
        InsightWorkspace(
            id=workspace_id("run_one", "ins_one"), run_id="run_one", insight_id="ins_one",
            sources=[source], pending_proposal=proposal,
        )

    revision = AppliedInsightRevision(
        proposal_id="wprop_one", previous_summary="Before.", applied_summary="After.",
        source_ids=["ev_missing"],
    )
    with pytest.raises(ValidationError):
        InsightWorkspace(
            id=workspace_id("run_one", "ins_one"), run_id="run_one", insight_id="ins_one",
            sources=[source], applied_revisions=[revision],
        )


def test_workspace_round_trips_continuity_proposal_and_applied_revision(tmp_path: Path):
    source = workspace().sources[0]
    message = InsightWorkspaceMessage(
        id="wmsg_one", role=WorkspaceMessageRole.ASSISTANT,
        content="The research supports a revised interpretation.", source_ids=[source.id],
    )
    proposal = InsightRevisionProposal(
        id="wprop_one", proposed_summary="Revised finding.", change_note="Updated research.",
        basis_message_ids=[message.id], source_ids=[source.id], base_summary_digest="digest",
    )
    revision = AppliedInsightRevision(
        id="wrev_one", proposal_id="wprop_previous", previous_summary="Before.",
        applied_summary="After.", source_ids=[source.id],
    )
    item = InsightWorkspace(
        id=workspace_id("run_one", "ins_one"), run_id="run_one", insight_id="ins_one",
        messages=[message], sources=[source], continuity_summary="Prior research context.",
        summarized_through_message_id=message.id, pending_proposal=proposal,
        applied_revisions=[revision],
    )
    store = Store(tmp_path / "workspace.db")
    store.save_insight_workspace(item)
    assert store.get_insight_workspace("run_one", "ins_one") == item


def test_workspace_loads_a_legacy_summary_only_pending_proposal_as_incomplete():
    legacy_workspace_json = {
        "id": workspace_id("run_one", "ins_one"),
        "run_id": "run_one",
        "insight_id": "ins_one",
        "pending_proposal": {
            "id": "wprop_legacy",
            "proposed_summary": "Legacy proposed summary",
            "change_note": "Legacy reason",
            "basis_message_ids": [],
            "source_ids": [],
            "web_sites": [],
            "base_summary_digest": "legacy-digest",
        },
    }

    workspace = InsightWorkspace.model_validate(legacy_workspace_json)

    assert workspace.pending_proposal is not None
    assert workspace.pending_proposal.proposed_summary == "Legacy proposed summary"
    assert workspace.pending_proposal.after_content is None
    assert is_complete_card_proposal(workspace.pending_proposal) is False


def test_workspace_normalizes_a_legacy_complete_requires_input_proposal_to_read_only():
    """A pre-support-state factual proposal must not regain an enabled Apply action."""
    source = workspace().sources[0]
    before = InsightCardContent(
        summary="Before", detail="Before detail", evidence_type="metrics",
        evidence=[{"label": "Current", "value": "1"}], interpretation="Before interpretation",
        review_note="", covered=True, input_reason="", evidence_ids=[source.id],
        source_ids=[source.source_id], used_web_fallback=False,
    )
    after = before.model_copy(update={
        "summary": "Unsupported factual replacement",
        "covered": False,
        "input_reason": "Evidence support is required for the proposed factual update.",
    })
    legacy_workspace_json = workspace().model_dump(mode="json")
    legacy_workspace_json["pending_proposal"] = {
        "id": "wprop_legacy_complete",
        "proposed_summary": after.summary,
        "source_ids": [source.id],
        "before_content": before.model_dump(mode="json"),
        "after_content": after.model_dump(mode="json"),
        "base_content_digest": "legacy-content-digest",
    }

    loaded = InsightWorkspace.model_validate(legacy_workspace_json)
    serialized = loaded.model_dump(mode="json")
    reloaded = InsightWorkspace.model_validate(serialized)

    assert loaded.pending_proposal is not None
    assert is_complete_card_proposal(loaded.pending_proposal) is True
    assert loaded.pending_proposal.applyable is False
    assert loaded.pending_proposal.unsupported_factual_fields == []
    assert serialized["pending_proposal"]["applyable"] is False
    assert serialized["pending_proposal"]["unsupported_factual_fields"] == []
    assert reloaded.pending_proposal == loaded.pending_proposal


def test_workspace_round_trips_a_complete_card_proposal_and_applied_history(tmp_path: Path):
    before = InsightCardContent(
        summary="Before", detail="", evidence_type="", evidence=None, interpretation="",
        review_note="", covered=True, input_reason="", evidence_ids=[], source_ids=["source_one"],
        used_web_fallback=False,
    )
    after = InsightCardContent(
        summary="After", detail="", evidence_type="", evidence=None, interpretation="",
        review_note="", covered=True, input_reason="", evidence_ids=[], source_ids=["source_two"],
        used_web_fallback=False,
    )
    change = InsightCardFieldChange(
        field="summary", kind="changed", before="Before", after="After",
        evidence_ids=["ev_one"], reason="New evidence changes the finding.",
    )
    support = InsightFieldSupport(
        field="summary", evidence_ids=["ev_one"], reason="Direct support.",
    )
    proposal = InsightRevisionProposal(
        id="wprop_complete", before_content=before, after_content=after,
        changed_fields=[change], unchanged_fields=["detail"], support_by_field=[support],
        change_reasons={"summary": "New evidence changes the finding."},
        applyable=True, unsupported_factual_fields=[],
        base_content_digest="content-digest",
    )
    revision = AppliedInsightRevision(
        id="wrev_complete", proposal_id=proposal.id, before_content=before, after_content=after,
        changed_fields=[change],
        unchanged_fields=["detail"], support_by_field=[support],
        change_reasons={"summary": "New evidence changes the finding."},
        base_content_digest="content-digest", source_ids=["ev_one"],
    )
    source = workspace().sources[0]
    item = InsightWorkspace(
        id=workspace_id("run_one", "ins_one"), run_id="run_one", insight_id="ins_one",
        sources=[source], pending_proposal=proposal, applied_revisions=[revision],
    )

    store = Store(tmp_path / "workspace.db")
    store.save_insight_workspace(item)
    loaded = store.get_insight_workspace("run_one", "ins_one")

    assert loaded == item
    assert loaded is not None
    assert is_complete_card_proposal(loaded.pending_proposal) is True
    assert loaded.pending_proposal.applyable is True
    assert loaded.pending_proposal.unsupported_factual_fields == []
    assert loaded.applied_revisions[0].after_content == after


def test_partial_card_snapshots_are_not_complete_apply_proposals():
    proposal = InsightRevisionProposal(
        before_content=InsightCardContent(summary="Before"),
        after_content=InsightCardContent(summary="After"),
        base_content_digest="content-digest",
    )

    assert is_complete_card_proposal(proposal) is False
