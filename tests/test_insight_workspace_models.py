from pathlib import Path

import pytest
from pydantic import ValidationError

from celestra.models import (
    AppliedInsightRevision,
    AppliedRevisionResult,
    EvidenceOrigin,
    InsightRevisionProposal,
    InsightWorkspace,
    InsightWorkspaceMessage,
    InsightWorkspaceSource,
    WorkspaceMessageRole,
    WorkspaceMessageState,
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
