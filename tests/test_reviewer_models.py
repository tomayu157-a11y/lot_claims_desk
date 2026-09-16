"""Reviewer files on the data model: stored on the insight, routed sections
recorded on the question, old rows still load, and the upload directory and
per-question ceiling are configured."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.models import Insight, ResearchQuestion, ReviewerFile, ReviewerFileSection
from celestra.settings import DATA_DIR, UPLOAD_DIR, ensure_dirs, get_thresholds

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    print("\n== an insight carries reviewer files ==")
    rf = ReviewerFile(
        filename="Payer policy.pdf", kind="pdf", size_bytes=2048,
        markdown="# Scope\n\nText.", pages_read=10, pages_total=25,
        sections=[ReviewerFileSection(id="s1", heading="Scope", text="Text.", page=1)],
    )
    ins = Insight(stage="stage_2", bucket="C", category="Treatment", title="T", summary="S",
                  reviewer_files=[rf])
    back = Insight.model_validate_json(ins.model_dump_json())
    check("file survives a JSON round trip", back.reviewer_files[0].filename == "Payer policy.pdf")
    check("sections survive", back.reviewer_files[0].sections[0].heading == "Scope")
    check("file ids are prefixed rf_", rf.id.startswith("rf_"), rf.id)
    check("defaults are empty", rf.storage_key == "" and rf.truncated is False and rf.token_estimate == 0)

    print("\n== rows saved before this feature still load ==")
    old = ins.model_dump(mode="json")
    old.pop("reviewer_files")
    check("old insight loads with no files", Insight.model_validate(old).reviewer_files == [])
    q = ResearchQuestion(stage="stage_4", bucket="D", text="Which regimens are first line?")
    oldq = q.model_dump(mode="json")
    oldq.pop("reviewer_sections")
    oldq.pop("reviewer_sections_dropped")
    q2 = ResearchQuestion.model_validate(oldq)
    check("old question loads with no routed sections",
          q2.reviewer_sections == [] and q2.reviewer_sections_dropped == [])

    print("\n== configuration ==")
    check("uploads live under the data directory", UPLOAD_DIR == DATA_DIR / "uploads", str(UPLOAD_DIR))
    ensure_dirs()
    check("setup creates the upload directory", UPLOAD_DIR.is_dir())
    limit = get_thresholds()["limits"].get("reviewer_context_max_chars_per_question")
    check("per-question ceiling is 24,000 chars", limit == 24000, str(limit))

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
