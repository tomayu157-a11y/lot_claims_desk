from pathlib import Path

import pytest
from pydantic import ValidationError

from celestra.models import (
    AppliedInsightRevision,
    AppliedRevisionResult,
    EvidenceOrigin,
    Insight,
    InsightRevisionProposal,
    InsightWorkspace,
    InsightWorkspaceMessage,
    InsightWorkspaceSource,
    WorkspaceMessageRole,
    WorkspaceMessageState,
    WorkspaceEvent,
    workspace_id,
)
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


def test_workspace_source_rejects_non_http_url():
    with pytest.raises(ValidationError):
        InsightWorkspaceSource(
            id="ev_bad", question_id="q_one", source_id="bad", source_name="Bad", tier=3,
            url="javascript:alert(1)", quote="unsafe",
        )


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
