"""Persistence. SQLite with pydantic documents.

A run is a small object graph that is written far more often than it is
queried in complex ways, so documents beat a normalised schema here. Every
table is keyed by run_id so a run can be loaded or deleted atomically.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from .models import (
    Answer,
    Contradiction,
    Evidence,
    Insight,
    InsightWorkspace,
    QAMetrics,
    ResearchQuestion,
    Run,
    RunStatus,
    StageReport,
)
from .services.insight_reconciliation import insight_card_digest
from .settings import DATA_DIR

T = TypeVar("T")

_FIXED_INSIGHT_REVISION_FIELDS = (
    "id",
    "run_id",
    "stage",
    "bucket",
    "category",
    "title",
    "number",
    "card_key",
    "question_ids",
    "table_titles",
    "reviewer_input",
    "reviewer_files",
    "created_at",
    "impacted_insight_ids",
)


class StaleInsightRevision(Exception):
    """The selected finding or pending proposal changed before Apply."""


def insight_snapshot_digest(insight: Insight) -> str:
    """Return a stable digest of the complete persisted insight document."""
    payload = insight.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InsightCardRevisionCommit:
    insight: Insight
    new_evidence: list[Evidence]
    workspace: InsightWorkspace
    expected_content_digest: str
    expected_insight_digest: str

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    reference TEXT,
    status TEXT,
    indication TEXT,
    created_at TEXT,
    doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, question_id TEXT,
    source_id TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS insights (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contradictions (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stage_reports (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS answers (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, question_id TEXT, stage TEXT,
    doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qa (
    run_id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS insight_workspaces (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    insight_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    doc TEXT NOT NULL,
    UNIQUE(run_id, insight_id)
);
CREATE INDEX IF NOT EXISTS ix_questions_run ON questions(run_id);
CREATE INDEX IF NOT EXISTS ix_evidence_run ON evidence(run_id);
CREATE INDEX IF NOT EXISTS ix_evidence_q ON evidence(question_id);
CREATE INDEX IF NOT EXISTS ix_insights_run ON insights(run_id);
CREATE INDEX IF NOT EXISTS ix_contra_run ON contradictions(run_id);
CREATE INDEX IF NOT EXISTS ix_stages_run ON stage_reports(run_id);
CREATE INDEX IF NOT EXISTS ix_answers_run ON answers(run_id);
CREATE INDEX IF NOT EXISTS ix_answers_q ON answers(question_id);
CREATE INDEX IF NOT EXISTS ix_insight_workspaces_run ON insight_workspaces(run_id);
"""


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (DATA_DIR / "celestra.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @staticmethod
    def _dump(obj: Any) -> str:
        return obj.model_dump_json()

    # -- runs ------------------------------------------------------------
    def save_run(self, run: Run) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs(id,reference,status,indication,created_at,doc) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "reference=excluded.reference,status=excluded.status,doc=excluded.doc",
                (run.id, run.reference, run.status.value, run.config.indication,
                 run.created_at.isoformat(), self._dump(run)),
            )

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn().execute("SELECT doc FROM runs WHERE id=?", (run_id,)).fetchone()
        return Run.model_validate_json(row["doc"]) if row else None

    def list_runs(self, limit: int = 50) -> list[Run]:
        rows = self._conn().execute(
            "SELECT doc FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Run.model_validate_json(r["doc"]) for r in rows]

    def delete_run(self, run_id: str) -> None:
        with self._conn() as c:
            for t in ("questions", "evidence", "answers", "insights",
                      "contradictions", "stage_reports", "qa", "insight_workspaces"):
                c.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
            c.execute("DELETE FROM runs WHERE id=?", (run_id,))

    # -- generic document tables ----------------------------------------
    def _save_many(self, table: str, run_id: str, items: Iterable[Any],
                   extra_cols: tuple[str, ...] = ()) -> None:
        with self._conn() as connection:
            self._save_many_in_transaction(connection, table, run_id, items, extra_cols)

    def _save_many_in_transaction(
        self,
        connection: sqlite3.Connection,
        table: str,
        run_id: str,
        items: Iterable[Any],
        extra_cols: tuple[str, ...] = (),
    ) -> None:
        rows = []
        for it in items:
            extras = tuple(getattr(it, col, "") for col in extra_cols)
            rows.append((it.id, run_id, *extras, self._dump(it)))
        if not rows:
            return
        cols = ("id", "run_id", *extra_cols, "doc")
        ph = ",".join("?" * len(cols))
        assign = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        connection.executemany(
            f"INSERT INTO {table}({','.join(cols)}) VALUES({ph}) "
            f"ON CONFLICT(id) DO UPDATE SET {assign}",
            rows,
        )

    def _load_many(self, table: str, run_id: str, model: type[T], where: str = "") -> list[T]:
        sql = f"SELECT doc FROM {table} WHERE run_id=?{where}"
        rows = self._conn().execute(sql, (run_id,)).fetchall()
        return [model.model_validate_json(r["doc"]) for r in rows]  # type: ignore[attr-defined]

    def save_questions(self, run_id: str, items: Iterable[ResearchQuestion]) -> None:
        self._save_many("questions", run_id, items, ("stage",))

    def get_questions(self, run_id: str) -> list[ResearchQuestion]:
        return self._load_many("questions", run_id, ResearchQuestion)

    def save_evidence(self, run_id: str, items: Iterable[Evidence]) -> None:
        self._save_many("evidence", run_id, items, ("question_id", "source_id"))

    def get_evidence(self, run_id: str) -> list[Evidence]:
        return self._load_many("evidence", run_id, Evidence)

    def get_evidence_for(self, run_id: str, question_id: str) -> list[Evidence]:
        rows = self._conn().execute(
            "SELECT doc FROM evidence WHERE run_id=? AND question_id=?", (run_id, question_id)
        ).fetchall()
        return [Evidence.model_validate_json(r["doc"]) for r in rows]

    def save_answers(self, run_id: str, items: Iterable[Answer]) -> None:
        self._save_many("answers", run_id, items, ("question_id", "stage"))

    def get_answers(self, run_id: str) -> list[Answer]:
        return self._load_many("answers", run_id, Answer)

    def get_answers_for(self, run_id: str, question_id: str) -> list[Answer]:
        rows = self._conn().execute(
            "SELECT doc FROM answers WHERE run_id=? AND question_id=?", (run_id, question_id)
        ).fetchall()
        return [Answer.model_validate_json(r["doc"]) for r in rows]

    def save_insights(self, run_id: str, items: Iterable[Insight]) -> None:
        self._save_many("insights", run_id, items, ("stage",))

    def save_insight_if_unchanged(
        self,
        insight: Insight,
        expected_insight_digest: str,
    ) -> None:
        """Save one reviewer-owned card change only when its read snapshot remains current."""
        connection = self._conn()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT doc FROM insights WHERE run_id=? AND id=?",
                (insight.run_id, insight.id),
            ).fetchone()
            if row is None:
                raise StaleInsightRevision("Insight no longer exists")
            current = Insight.model_validate_json(row["doc"])
            if insight_snapshot_digest(current) != expected_insight_digest:
                raise StaleInsightRevision("Insight changed")
            connection.execute(
                "INSERT INTO insights(id,run_id,stage,doc) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,"
                "stage=excluded.stage,doc=excluded.doc",
                (insight.id, insight.run_id, insight.stage, self._dump(insight)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def commit_reviewer_input_if_unchanged(
        self,
        insight: Insight,
        expected_insight_digest: str,
        run: Run,
        reports: Iterable[StageReport],
    ) -> None:
        """Atomically attach reviewer context, its report rows, and one finding."""
        connection = self._conn()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT doc FROM insights WHERE run_id=? AND id=?",
                (insight.run_id, insight.id),
            ).fetchone()
            if row is None:
                raise StaleInsightRevision("Insight no longer exists")
            current = Insight.model_validate_json(row["doc"])
            if insight_snapshot_digest(current) != expected_insight_digest:
                raise StaleInsightRevision("Insight changed")
            run_row = connection.execute(
                "SELECT doc FROM runs WHERE id=?", (run.id,),
            ).fetchone()
            if run_row is None:
                raise StaleInsightRevision("Run no longer exists")
            current_run = Run.model_validate_json(run_row["doc"])
            incoming_entries = [
                entry
                for entry in (run.context.get("reviewer_inputs") or [])
                if isinstance(entry, dict) and entry.get("insight_id") == insight.id
            ]
            if incoming_entries:
                current_entries = [
                    entry
                    for entry in (current_run.context.get("reviewer_inputs") or [])
                    if isinstance(entry, dict) and entry.get("insight_id") != insight.id
                ]
                current_entries.append(incoming_entries[-1])
                current_run.context["reviewer_inputs"] = current_entries
            connection.execute(
                "INSERT INTO runs(id,reference,status,indication,created_at,doc) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "reference=excluded.reference,status=excluded.status,doc=excluded.doc",
                (current_run.id, current_run.reference, current_run.status.value,
                 current_run.config.indication, current_run.created_at.isoformat(),
                 self._dump(current_run)),
            )
            for report in reports:
                report_row = connection.execute(
                    "SELECT doc FROM stage_reports WHERE run_id=? AND id=?",
                    (run.id, report.id),
                ).fetchone()
                if report_row is None:
                    self._save_many_in_transaction(
                        connection, "stage_reports", run.id, [report], ("stage",),
                    )
                    continue
                current_report = StageReport.model_validate_json(report_row["doc"])
                for incoming_answer in report.answers:
                    if incoming_answer.get("reviewer_input") != insight.reviewer_input:
                        continue
                    question = incoming_answer.get("question")
                    seed = incoming_answer.get("seed")
                    for current_answer in current_report.answers:
                        if (
                            question and current_answer.get("question") == question
                        ) or (
                            seed and current_answer.get("seed") == seed
                        ):
                            current_answer["reviewer_input"] = insight.reviewer_input
                self._save_many_in_transaction(
                    connection, "stage_reports", run.id, [current_report], ("stage",),
                )
            connection.execute(
                "INSERT INTO insights(id,run_id,stage,doc) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,"
                "stage=excluded.stage,doc=excluded.doc",
                (insight.id, insight.run_id, insight.stage, self._dump(insight)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def import_run_if_current(
        self,
        run: Run,
        questions: Iterable[ResearchQuestion],
        evidence: Iterable[Evidence],
        answers: Iterable[Answer],
        insights: Iterable[Insight],
        contradictions: Iterable[Contradiction],
        reports: Iterable[StageReport],
        qa: QAMetrics | None,
    ) -> None:
        """Import one bundle atomically, refusing a changed completed finding."""
        questions = list(questions)
        evidence = list(evidence)
        answers = list(answers)
        insights = list(insights)
        contradictions = list(contradictions)
        reports = list(reports)
        connection = self._conn()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT doc FROM runs WHERE id=?", (run.id,),
            ).fetchone()
            if existing_row is not None:
                existing = Run.model_validate_json(existing_row["doc"])
                if existing.status is RunStatus.COMPLETED:
                    current_cards = {
                        item.id: insight_snapshot_digest(item)
                        for item in self.get_insights(run.id)
                    }
                    imported_cards = {
                        item.id: insight_snapshot_digest(item)
                        for item in insights
                    }
                    if current_cards != imported_cards:
                        raise StaleInsightRevision("Completed insight changed since export")
            connection.execute(
                "INSERT INTO runs(id,reference,status,indication,created_at,doc) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "reference=excluded.reference,status=excluded.status,doc=excluded.doc",
                (run.id, run.reference, run.status.value, run.config.indication,
                 run.created_at.isoformat(), self._dump(run)),
            )
            self._save_many_in_transaction(connection, "questions", run.id, questions, ("stage",))
            self._save_many_in_transaction(
                connection, "evidence", run.id, evidence, ("question_id", "source_id"),
            )
            self._save_many_in_transaction(
                connection, "answers", run.id, answers, ("question_id", "stage"),
            )
            self._save_many_in_transaction(connection, "insights", run.id, insights, ("stage",))
            self._save_many_in_transaction(
                connection, "contradictions", run.id, contradictions, ("stage",),
            )
            self._save_many_in_transaction(
                connection, "stage_reports", run.id, reports, ("stage",),
            )
            if qa is not None:
                connection.execute(
                    "INSERT INTO qa(run_id,doc) VALUES(?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET doc=excluded.doc",
                    (run.id, self._dump(qa)),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def get_insights(self, run_id: str) -> list[Insight]:
        return self._load_many("insights", run_id, Insight)

    def get_insight(self, run_id: str, insight_id: str) -> Insight | None:
        row = self._conn().execute(
            "SELECT doc FROM insights WHERE run_id=? AND id=?", (run_id, insight_id)
        ).fetchone()
        return Insight.model_validate_json(row["doc"]) if row else None

    def save_insight_workspace(self, workspace: InsightWorkspace) -> None:
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO insight_workspaces(id,run_id,insight_id,updated_at,doc) "
                "VALUES(?,?,?,?,?) ON CONFLICT(run_id,insight_id) DO UPDATE SET "
                "id=excluded.id,"
                "updated_at=excluded.updated_at,doc=excluded.doc",
                (workspace.id, workspace.run_id, workspace.insight_id,
                 workspace.updated_at.isoformat(), self._dump(workspace)),
            )

    def get_insight_workspace(self, run_id: str, insight_id: str) -> InsightWorkspace | None:
        row = self._conn().execute(
            "SELECT doc FROM insight_workspaces WHERE run_id=? AND insight_id=?",
            (run_id, insight_id),
        ).fetchone()
        return InsightWorkspace.model_validate_json(row["doc"]) if row else None

    def commit_insight_card_revision(self, commit: InsightCardRevisionCommit) -> None:
        """Atomically persist one selected card, its new evidence, and its workspace."""
        connection = self._conn()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT doc FROM insights WHERE run_id=? AND id=?",
                (commit.insight.run_id, commit.insight.id),
            ).fetchone()
            if row is None:
                raise StaleInsightRevision("Insight no longer exists")
            current = Insight.model_validate_json(row["doc"])
            if insight_card_digest(current) != commit.expected_content_digest:
                raise StaleInsightRevision("Insight content changed")
            if insight_snapshot_digest(current) != commit.expected_insight_digest:
                raise StaleInsightRevision("Insight changed")
            if any(
                getattr(commit.insight, field) != getattr(current, field)
                for field in _FIXED_INSIGHT_REVISION_FIELDS
            ):
                raise ValueError("Insight card fixed fields changed")

            workspace_row = connection.execute(
                "SELECT doc FROM insight_workspaces WHERE run_id=? AND insight_id=?",
                (commit.insight.run_id, commit.insight.id),
            ).fetchone()
            if workspace_row is None:
                raise StaleInsightRevision("Insight workspace no longer exists")
            current_workspace = InsightWorkspace.model_validate_json(workspace_row["doc"])
            if (
                commit.workspace.run_id != current_workspace.run_id
                or commit.workspace.insight_id != current_workspace.insight_id
                or commit.workspace.id != current_workspace.id
            ):
                raise ValueError("Commit workspace must identify the selected insight")
            applied_proposal_id = (
                commit.workspace.applied_revisions[-1].proposal_id
                if commit.workspace.applied_revisions else ""
            )
            if (
                not applied_proposal_id
                or current_workspace.pending_proposal is None
                or current_workspace.pending_proposal.id != applied_proposal_id
            ):
                raise StaleInsightRevision("Insight proposal changed")

            new_evidence_ids: set[str] = set()
            for evidence in commit.new_evidence:
                if evidence.id in new_evidence_ids:
                    raise ValueError("New evidence IDs must be unique")
                new_evidence_ids.add(evidence.id)
                if evidence.question_id not in commit.insight.question_ids:
                    raise ValueError("New evidence must belong to a linked insight question")
                question_row = connection.execute(
                    "SELECT 1 FROM questions WHERE id=? AND run_id=?",
                    (evidence.question_id, commit.insight.run_id),
                ).fetchone()
                if question_row is None:
                    raise ValueError("New evidence question must belong to the insight run")
                evidence_row = connection.execute(
                    "SELECT 1 FROM evidence WHERE id=?",
                    (evidence.id,),
                ).fetchone()
                if evidence_row is not None:
                    raise ValueError("New evidence ID already exists")

            connection.execute(
                "INSERT INTO insights(id,run_id,stage,doc) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,"
                "stage=excluded.stage,doc=excluded.doc",
                (commit.insight.id, commit.insight.run_id, commit.insight.stage,
                 self._dump(commit.insight)),
            )
            for evidence in commit.new_evidence:
                connection.execute(
                    "INSERT INTO evidence(id,run_id,question_id,source_id,doc) "
                    "VALUES(?,?,?,?,?)",
                    (evidence.id, commit.insight.run_id, evidence.question_id,
                     evidence.source_id, self._dump(evidence)),
                )
            connection.execute(
                "INSERT INTO insight_workspaces(id,run_id,insight_id,updated_at,doc) "
                "VALUES(?,?,?,?,?) ON CONFLICT(run_id,insight_id) DO UPDATE SET "
                "id=excluded.id,updated_at=excluded.updated_at,doc=excluded.doc",
                (commit.workspace.id, commit.workspace.run_id, commit.workspace.insight_id,
                 commit.workspace.updated_at.isoformat(), self._dump(commit.workspace)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def save_contradictions(self, run_id: str, items: Iterable[Contradiction]) -> None:
        self._save_many("contradictions", run_id, items, ("stage",))

    def get_contradictions(self, run_id: str) -> list[Contradiction]:
        return self._load_many("contradictions", run_id, Contradiction)

    def get_contradiction(self, run_id: str, cid: str) -> Contradiction | None:
        row = self._conn().execute(
            "SELECT doc FROM contradictions WHERE run_id=? AND id=?", (run_id, cid)
        ).fetchone()
        return Contradiction.model_validate_json(row["doc"]) if row else None

    def save_stage_reports(self, run_id: str, items: Iterable[StageReport]) -> None:
        self._save_many("stage_reports", run_id, items, ("stage",))

    def get_stage_reports(self, run_id: str) -> list[StageReport]:
        reports = self._load_many("stage_reports", run_id, StageReport)
        return sorted(reports, key=lambda r: r.stage)

    def save_qa(self, run_id: str, qa: QAMetrics) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO qa(run_id,doc) VALUES(?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET doc=excluded.doc",
                (run_id, self._dump(qa)),
            )

    def get_qa(self, run_id: str) -> QAMetrics | None:
        row = self._conn().execute("SELECT doc FROM qa WHERE run_id=?", (run_id,)).fetchone()
        return QAMetrics.model_validate_json(row["doc"]) if row else None


store = Store()
