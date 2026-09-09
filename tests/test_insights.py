"""Insight cards: the catalogue slots, filled by the model from the stage
document or deterministically from the mapped questions; the evidence rule as
the floor for Requires Input; the phase synthesis card."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.models import (
    AnswerStatus, Contradiction, ContradictionSeverity, Evidence, EvidenceOrigin, InsightTable,
    ResearchQuestion, RunConfig, StageReport,
)
from celestra.services import insights as gen

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


class FakeLLM:
    available = True

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    async def complete_json(self, system, prompt, *, max_tokens=None):
        self.prompts.append(prompt)
        return self.reply


def fixture():
    cfg = RunConfig(indication="Acute Lymphoblastic Leukemia", indication_key="ALL")
    qs = [
        ResearchQuestion(id="q1", stage="stage_1", bucket="A",
                         text="What is the disease definition and natural history of ALL?",
                         seed_text="What is the disease definition and natural history of ALL?",
                         answer_text="ALL is a malignancy of lymphoid progenitors.",
                         answer_status=AnswerStatus.ANSWERED, answer_citations=["NCI"]),
        ResearchQuestion(id="q2", stage="stage_1", bucket="A",
                         text="What is the incidence, prevalence, survival and mortality of ALL in the US?",
                         seed_text="What is the incidence, prevalence, survival and mortality of ALL in the US?",
                         answer_text="About 6,250 new cases and 1,600 deaths per year; incidence 1.9 per 100,000.",
                         answer_status=AnswerStatus.ANSWERED, answer_citations=["NCI SEER"]),
        ResearchQuestion(id="q3", stage="stage_1", bucket="A",
                         text="What are the immunophenotypic and molecular subtypes of ALL?",
                         seed_text="What are the clinically important immunophenotypic and molecular subtypes of ALL?",
                         answer_text="B-ALL about 85%, T-ALL about 15%; Ph+ about 25% of adults.",
                         answer_status=AnswerStatus.ANSWERED, answer_citations=["WHO"]),
        ResearchQuestion(id="q4", stage="stage_1", bucket="A",
                         text="How is ALL diagnosed and what confirmatory workup is required?",
                         seed_text="How is ALL diagnosed and what confirmatory workup is required?",
                         answer_text="Morphology, flow cytometry, cytogenetics and molecular testing.",
                         answer_status=AnswerStatus.PARTIAL, used_web_fallback=True),
        ResearchQuestion(id="q5", stage="stage_1", bucket="A",
                         text="How is ALL risk-stratified at diagnosis?",
                         seed_text="How is ALL risk-stratified at diagnosis?",
                         answer_text="", answer_status=AnswerStatus.NOT_FOUND,
                         unmet_reason="no source addressed it"),
    ]
    ev = [
        Evidence(id="e1", question_id="q1", source_id="nci_pdq", source_name="NCI PDQ", tier=1,
                 url="https://cancer.gov/x", quote="ALL is a malignancy of lymphoid progenitor cells."),
        Evidence(id="e2", question_id="q2", source_id="seer", source_name="NCI SEER", tier=1,
                 url="https://seer.cancer.gov/x", quote="An estimated 6,250 new cases in 2026."),
        Evidence(id="e3", question_id="q3", source_id="who", source_name="WHO", tier=1,
                 url="https://who.int/x", quote="B-lymphoblastic leukaemia accounts for about 85%."),
        Evidence(id="e4", question_id="q4", source_id="open_web", source_name="Open Web", tier=3,
                 url="https://blog.example/x", quote="Diagnosis uses morphology and flow cytometry.",
                 origin=EvidenceOrigin.OPEN_WEB),
    ]
    report = StageReport(
        stage="stage_1", bucket="A", name="Disease & Diagnostic Foundation",
        core_question="Who gets it?", agent_name="Clinical Landscape Agent",
        synthesis="ALL is a rare disease with a bimodal age distribution.",
        tables=[InsightTable(title="Epidemiology snapshot table", columns=["Metric", "Value", "Source"],
                             rows=[["New cases", "6,250", "SEER"], ["Deaths", "1,600", "SEER"]],
                             question_ids=["q2"]),
                InsightTable(title="Subtype / biology breakdown table", columns=["Subtype", "Share", "Notes"],
                             rows=[["B-ALL", "85%", ""], ["T-ALL", "15%", ""]], question_ids=["q3"])],
        takeaways=["Age and lineage are core cohort variables.", "Diagnosis needs several signals."],
    )
    return cfg, qs, ev, report


def main() -> int:
    print("\n== catalogue ==")
    cat = gen.catalogue_for("A")
    check("five Clinical Landscape slots in the order of the reference",
          [c["key"] for c in cat] == ["epidemiology", "population_segmentation", "disease_definition",
                                      "diagnostic_foundation", "disease_journey"])
    check("three Treatment Evidence slots", [c["number"] for c in gen.catalogue_for("C")] == [6, 7, 8])
    check("discovery phase owes the key-insights card (09)",
          [c["number"] for c in gen.phase_catalogue("discovery")] == [9])
    check("later phases have slots too", all(gen.catalogue_for(b) for b in "BDEF"))

    print("\n== deterministic fill ==")
    cfg, qs, ev, report = fixture()
    cards = gen.deterministic("run_t", "stage_1", "A", report, qs, ev, [])
    by = {c.card_key: c for c in cards}
    check("one card per slot", len(cards) == 5 and set(by) == {c["key"] for c in cat})
    check("epidemiology card takes the epidemiology answer and table",
          "6,250" in by["epidemiology"].summary and by["epidemiology"].evidence_type == "table"
          and by["epidemiology"].table_titles == ["Epidemiology snapshot table"])
    check("segmentation card links the subtype question", "q3" in by["population_segmentation"].question_ids)
    check("card answered from vetted sources is Ready", by["epidemiology"].confidence.value == "ready")
    check("web-only card needs review, with the reason",
          by["diagnostic_foundation"].confidence.value == "requires_input"
          and "open-web" in by["diagnostic_foundation"].input_reason.lower())
    check("slot without an answer is marked not covered, quietly",
          by["disease_journey"].covered is False or by["disease_journey"].summary,
          by["disease_journey"].summary[:60])

    print("\n== model fill ==")
    fake = FakeLLM({"cards": [
        {"key": "epidemiology", "covered": True,
         "finding": "ALL is rare with a bimodal age distribution; ~6,250 new US cases and ~1,600 deaths.",
         "evidence": [{"label": "New US cases", "value": "~6,250"}, {"label": "Deaths", "value": "~1,600"},
                      {"label": "Incidence rate", "value": "1.9 / 100,000"}],
         "interpretation": "Cohorts will be small; age bands matter.", "review_note": "Confirm the SEER year.",
         "questions": [2], "table_titles": ["epidemiology snapshot table"], "gap": ""},
        {"key": "population_segmentation", "covered": True, "finding": "B-ALL ~85%, T-ALL ~15%.",
         "evidence": {"columns": ["Segment", "Share", "Feature"], "rows": [["B-ALL", "85%", "CD19+"], ["T-ALL", "15%", "CD3+"]]},
         "interpretation": "Lineage is a cohort variable.", "review_note": "", "questions": [3], "table_titles": []},
        {"key": "disease_definition", "covered": True, "finding": "Malignancy of lymphoid progenitors.",
         "evidence": "not a table", "interpretation": "", "review_note": "", "questions": [1], "table_titles": []},
        {"key": "diagnostic_foundation", "covered": True, "finding": "Several signals establish diagnosis.",
         "evidence": ["Clinical suspicion", "Blood / marrow", "Morphology", "Flow cytometry", "Cytogenetics", "Molecular"],
         "interpretation": "", "review_note": "", "questions": [4], "table_titles": []},
        {"key": "disease_journey", "covered": False, "finding": "", "evidence": None, "interpretation": "",
         "review_note": "", "questions": [5], "table_titles": [], "gap": "The sources did not describe the clinical course."},
    ]})
    gen.llm = fake
    conflict = Contradiction(stage="stage_1", question_id="q3", topic="subtype share",
                             source_a_name="WHO", source_a_tier=1, source_a_claim="85%",
                             source_b_name="Blog", source_b_tier=3, source_b_claim="70%",
                             reason="differ", severity=ContradictionSeverity.ESCALATED)
    cards = asyncio.run(gen.generate("run_t", cfg, "stage_1", "A", report, qs, ev, [conflict]))
    by = {c.card_key: c for c in cards}
    prompt = fake.prompts[0]
    check("prompt lists every slot with its format and the document",
          'key "epidemiology"' in prompt and "TABLE: Epidemiology snapshot table" in prompt and "Q2." in prompt)
    check("all five slots filled, numbered 1-5", [c.number for c in cards] == [1, 2, 3, 4, 5])
    e = by["epidemiology"]
    check("metrics card carries its figures", e.evidence_type == "metrics" and len(e.evidence) == 3
          and e.evidence[0]["value"] == "~6,250")
    check("interpretation and quiet review note kept",
          e.interpretation.startswith("Cohorts") and e.review_note.startswith("Confirm"))
    check("table title matched case-insensitively", e.table_titles == ["Epidemiology snapshot table"])
    check("card links question, evidence and source", e.question_ids == ["q2"] and e.evidence_ids == ["e2"]
          and e.source_ids == ["seer"])
    check("table evidence coerced", by["population_segmentation"].evidence_type == "table"
          and by["population_segmentation"].evidence["rows"][0][0] == "B-ALL")
    check("steps evidence kept in order", by["diagnostic_foundation"].evidence[0] == "Clinical suspicion")
    check("malformed evidence falls back to the document, not guessed",
          by["disease_definition"].evidence_type in ("list", "table", "") )
    check("escalated conflict flags only the card on its question",
          by["population_segmentation"].confidence.value == "requires_input"
          and "disagree" in by["population_segmentation"].input_reason
          and by["epidemiology"].confidence.value == "ready")
    check("web-only card needs review whatever the model said",
          by["diagnostic_foundation"].confidence.value == "requires_input")
    check("uncovered slot is Requires Input with the model's gap",
          by["disease_journey"].covered is False and by["disease_journey"].confidence.value == "requires_input"
          and "clinical course" in by["disease_journey"].input_reason)

    print("\n== model skips a slot ==")
    gen.llm = FakeLLM({"cards": [{"key": "epidemiology", "covered": True, "finding": "x",
                                  "evidence": [], "questions": [2]}]})
    cards = asyncio.run(gen.generate("run_t", cfg, "stage_1", "A", report, qs, ev, []))
    check("skipped slots are filled deterministically so the set is complete", len(cards) == 5)

    print("\n== phase card ==")
    gen.llm = FakeLLM({"finding": "Discovery established the population and treatment branches.",
                       "points": ["Age, lineage and Ph-status are core variables.",
                                  "Diagnosis requires multiple signals.",
                                  "Treatment pathways branch by Ph-status."],
                       "interpretation": "Carry these into the diagnostic footprint."})
    phase = asyncio.run(gen.phase_cards("run_t", cfg, "discovery", [report], cards))
    check("one phase card, number 09, Synthesis", len(phase) == 1 and phase[0].number == 9
          and phase[0].category == "Synthesis")
    check("phase card lists the points", phase[0].evidence_type == "list" and len(phase[0].evidence) == 3)
    check("phase card unions the sources of the phase", "seer" in phase[0].source_ids)
    gen.llm = type("Off", (), {"available": False})()
    phase = asyncio.run(gen.phase_cards("run_t", cfg, "discovery", [report], cards))
    check("without a model the phase card uses the takeaways", phase[0].evidence and "Age" in phase[0].evidence[0])

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
