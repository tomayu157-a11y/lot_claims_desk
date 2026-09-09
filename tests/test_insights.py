"""Insight cards written by the model from the stage document, with the
evidence rule as the floor for Requires Input."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.models import (
    AnswerStatus, Evidence, EvidenceOrigin, InsightTable, ResearchQuestion, RunConfig,
    StageReport,
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
    cfg = RunConfig(indication="Chronic Lymphocytic Leukemia", indication_key="CLL")
    qs = [
        ResearchQuestion(id="q1", stage="stage_1", bucket="A", text="Incidence of CLL in the US?",
                         seed_text="Epidemiology (incidence, prevalence)", answer_text="4.7 per 100,000.",
                         answer_status=AnswerStatus.ANSWERED, answer_citations=["NCI SEER"]),
        ResearchQuestion(id="q2", stage="stage_1", bucket="A", text="Diagnostic criteria for CLL?",
                         seed_text="Diagnostic criteria", answer_text="≥5x10^9/L clonal B cells.",
                         answer_status=AnswerStatus.ANSWERED, answer_citations=["iwCLL"]),
        ResearchQuestion(id="q3", stage="stage_1", bucket="A", text="Staging systems?",
                         seed_text="Staging", answer_text="Rai and Binet.",
                         answer_status=AnswerStatus.PARTIAL, used_web_fallback=True),
    ]
    ev = [
        Evidence(id="e1", question_id="q1", source_id="seer", source_name="NCI SEER", tier=1,
                 url="https://seer.cancer.gov/x", quote="The rate is 4.7 per 100,000 per year."),
        Evidence(id="e2", question_id="q2", source_id="iwcll", source_name="iwCLL", tier=2,
                 url="https://ash.org/x", quote="Diagnosis requires ≥5x10^9/L clonal B lymphocytes."),
        Evidence(id="e3", question_id="q3", source_id="open_web", source_name="Open Web", tier=3,
                 url="https://blog.example/x", quote="Rai and Binet staging systems are used widely.",
                 origin=EvidenceOrigin.OPEN_WEB),
    ]
    report = StageReport(
        stage="stage_1", bucket="A", name="Clinical foundation", core_question="Who gets it?",
        agent_name="Clinical Landscape Agent", synthesis="CLL is a disease of older adults.",
        tables=[InsightTable(title="Epidemiology snapshot", columns=["Metric", "Value"],
                             rows=[["Incidence", "4.7 per 100,000"]], question_ids=["q1"]),
                InsightTable(title="Diagnostic criteria", columns=["Criterion", "Threshold"],
                             rows=[["Clonal B cells", "≥5x10^9/L"]], question_ids=["q2"])],
        takeaways=["Incidence 4.7 per 100,000."],
    )
    return cfg, qs, ev, report


def run(reply):
    cfg, qs, ev, report = fixture()
    fake = FakeLLM(reply)
    gen.llm = fake
    cards = asyncio.run(gen.generate("run_t", cfg, "stage_1", "A", report, qs, ev, [], "Clinical"))
    return cards, fake


def main() -> int:
    print("\n== cards from the document ==")
    cards, fake = run({"cards": [
        {"title": "Incidence 4.7 per 100,000", "finding": "SEER reports 4.7 per 100,000 per year.",
         "detail": "Age-adjusted, US.", "table_titles": ["Epidemiology snapshot"], "questions": [1],
         "status": "ready", "input_reason": ""},
        {"title": "Diagnosis needs 5x10^9/L clonal B cells", "finding": "iwCLL threshold.",
         "detail": "", "table_titles": ["diagnostic criteria"], "questions": ["Q2"],
         "status": "ready", "input_reason": ""},
        {"title": "Staging systems", "finding": "Rai and Binet are used.", "detail": "",
         "table_titles": [], "questions": [3], "status": "ready", "input_reason": ""},
        {"title": "Age at diagnosis", "finding": "Older adults.", "detail": "", "table_titles": [],
         "questions": [1, 2], "status": "requires_input",
         "input_reason": "The document does not state the median age."},
    ]})
    check("four cards produced", len(cards) == 4, str(len(cards)))
    prompt = fake.prompts[0]
    check("prompt carries the document's Q&A, tables and expected outputs",
          "Q1." in prompt and "TABLE: Epidemiology snapshot" in prompt and "Deliverable:" in prompt)
    c1, c2, c3, c4 = cards
    check("card links its table by exact title", c1.table_titles == ["Epidemiology snapshot"])
    check("card matches a table title case-insensitively", c2.table_titles == ["Diagnostic criteria"])
    check("card links its question and evidence", c1.question_ids == ["q1"] and c1.evidence_ids == ["e1"]
          and c1.source_ids == ["seer"])
    check("'Q2' index form accepted", c2.question_ids == ["q2"])
    check("ready when a vetted source answered", c1.confidence.value == "ready" and not c1.input_reason)
    check("evidence rule overrides the model: web-only is Requires Input",
          c3.confidence.value == "requires_input" and "open-web" in c3.input_reason.lower(),
          c3.input_reason)
    check("model may flag Requires Input with its own reason",
          c4.confidence.value == "requires_input" and "median age" in c4.input_reason)
    check("multi-question card unions evidence", set(c4.evidence_ids) == {"e1", "e2"})
    check("web-fallback flag carried", c3.used_web_fallback and not c1.used_web_fallback)
    check("verification tag: verified when all linked answered, inference otherwise",
          c1.tag.value == "VERIFIED" and c3.tag.value == "INFERENCE")
    check("category from the agent", all(c.category == "Clinical" for c in cards))

    print("\n== failure modes fall back ==")
    cards, _ = run({"cards": []})
    check("no cards -> empty, so the caller falls back", cards == [])
    cards, _ = run({"cards": [{"title": "Only one", "finding": "x", "questions": [1]}]})
    check("too few cards -> empty", cards == [])
    cards, _ = run("garbage")
    check("garbage -> empty", cards == [])
    gen.llm = type("Off", (), {"available": False})()
    cfg, qs, ev, report = fixture()
    cards = asyncio.run(gen.generate("run_t", cfg, "stage_1", "A", report, qs, ev, [], "Clinical"))
    check("no model -> empty", cards == [])

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
