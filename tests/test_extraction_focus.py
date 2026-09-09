"""Deterministic extraction must be question-specific.

Regression for the defect where an incidence figure headlined every card in a
stage: the disease name and the numbers in an epidemiology sentence outscored
a genuine diagnosis sentence for a question about diagnosis.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.models import SourceRef
from celestra.services.extraction import build_terms, extract_deterministic
from celestra.services.ranking import rank_deterministic

SYN = ["acute lymphoblastic leukemia", "acute lymphocytic leukemia", "ALL"]
TEXT = (
    "Acute lymphoblastic leukemia is a cancer of the blood and bone marrow. "
    "At a glance: Estimated New Cases in 2026: 6,250; percent of all new cancer cases: 0.3 percent. "
    "The rate of new cases of acute lymphocytic leukemia was 1.9 per 100,000 men and women per year. "
    "Diagnosis of acute lymphoblastic leukemia requires a bone marrow aspirate and biopsy showing at "
    "least 20 percent lymphoblasts, with immunophenotyping by flow cytometry to confirm lineage. "
    "Cytogenetic and molecular testing for the Philadelphia chromosome and KMT2A rearrangement is "
    "part of the confirmatory workup at diagnosis. "
    "Five-year relative survival for acute lymphocytic leukemia is 73.2 percent."
)
REF = SourceRef(source_id="seer", source_name="NCI SEER", tier=1, url="https://seer.cancer.gov/x",
                title="Acute Lymphocytic Leukemia — Cancer Stat Facts", snippet=TEXT)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def top_quote(question: str, aspects: list[str]) -> str:
    terms = build_terms(question, aspects, SYN)
    ev = extract_deterministic(REF, "q", terms, max_quotes=3)
    return ev[0].quote if ev else ""


diag_q = "How is ALL diagnosed and what confirmatory workup is required?"
diag_a = ["diagnosis", "confirmatory workup", "immunophenotyping", "cytogenetics"]
epi_q = "What is the incidence, prevalence, survival and mortality of ALL in the United States?"
epi_a = ["incidence", "prevalence", "survival", "mortality"]

print("\n== term tiers ==")
ts = build_terms(diag_q, diag_a, SYN)
check("disease words are context, not focus",
      "leukemia" in ts.context and "leukemia" not in ts.focus, str(sorted(ts.focus)))
check("question words are focus", {"diagnosed", "workup", "immunophenotyping"} <= ts.focus,
      str(sorted(ts.focus)))
check("diagnosis question is not quantitative", not ts.quantitative)
check("epidemiology question is quantitative", build_terms(epi_q, epi_a, SYN).quantitative)

print("\n== headline quotes ==")
d, e = top_quote(diag_q, diag_a), top_quote(epi_q, epi_a)
DIAG_WORDS = ("diagnosis", "workup", "immunophenotyping", "bone marrow", "cytogenetic")
STAT_WORDS = ("per 100,000", "Estimated New Cases", "survival", "percent")
check("diagnosis question headlines a diagnosis sentence",
      any(w in d for w in DIAG_WORDS) and "Estimated New Cases" not in d
      and "per 100,000" not in d, d[:80])
check("epidemiology question headlines a statistic",
      any(w in e for w in STAT_WORDS) and "workup" not in e, e[:80])
check("the two questions do not share a headline", d != e)

print("\n== ranking ==")
refs = [
    SourceRef(source_id="seer", source_name="NCI SEER", tier=1, url="https://seer.cancer.gov/x",
              title="Acute Lymphocytic Leukemia — Cancer Stat Facts",
              snippet="Estimated new cases in 2026: 6,250. The rate of new cases of acute "
                      "lymphocytic leukemia was 1.9 per 100,000 per year. Five-year relative "
                      "survival is 73.2 percent."),
    SourceRef(source_id="nci", source_name="NCI", tier=1, url="https://cancer.gov/x",
              title="Adult ALL Treatment (PDQ): Diagnosis and staging",
              snippet="Diagnostic workup: bone marrow aspirate, flow cytometry immunophenotyping, "
                      "cytogenetics and molecular testing at diagnosis."),
    SourceRef(source_id="acs", source_name="ACS", tier=3, url="https://cancer.org/x",
              title="Key statistics for acute lymphocytic leukemia",
              snippet="About 6,250 new cases and about 1,600 deaths from ALL in 2026."),
]
ranked = rank_deterministic(refs, build_terms(diag_q, diag_a, SYN))
check("diagnosis question ranks the diagnosis document first",
      ranked[0][0].source_id == "nci", [r.source_id for r, _ in ranked].__str__())
ranked = rank_deterministic(refs, build_terms(epi_q, epi_a, SYN))
check("epidemiology question does not rank the diagnosis document first",
      ranked[0][0].source_id != "nci", [r.source_id for r, _ in ranked].__str__())

print(f"\n{len(failures)} failure(s)")
raise SystemExit(1 if failures else 0)
