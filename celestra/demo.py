"""Offline demo run.

Seeds a completed run from bundled fixtures so every screen has content the
moment the app starts, with no network and no credentials. It drives the real
orchestrator through stub connectors, so what you see is the actual pipeline
rather than canned screenshots.

The quotes below are real published statements, kept verbatim so the evidence,
citations and contradiction detection behave exactly as they do on a live run.
"""
from __future__ import annotations

import asyncio

from .connectors.base import ConnectorResult, RetrievalContext
from .models import EvidenceOrigin, Run, RunConfig, RunMode, RunStatus, SourceRef
from .services import orchestrator as orch
from .store import store

# source_id -> (tier, organisation, url, title, body)
FIXTURES: dict[str, tuple[int, str, str, str, str]] = {
    "seer": (
        1, "NCI SEER", "https://seer.cancer.gov/statfacts/html/clyl.html",
        "Chronic Lymphocytic Leukemia — Cancer Stat Facts",
        "Chronic lymphocytic leukemia is a cancer of the blood and bone marrow. The "
        "overall rate of new cases of chronic lymphocytic leukemia was 4.7 per 100,000 "
        "men and women per year based on 2018-2022 cases, age-adjusted. The death rate "
        "was 1.0 per 100,000 men and women per year. Five-year relative survival for "
        "chronic lymphocytic leukemia is 88.5 percent. In 2021 there were an estimated "
        "214,573 people living with chronic lymphocytic leukemia in the United States. "
        "The median age at diagnosis is 70 years, and incidence rises steeply with age.",
    ),
    "nci": (
        1, "National Cancer Institute (NCI)",
        "https://www.cancer.gov/types/leukemia/hp/cll-treatment-pdq",
        "Chronic Lymphocytic Leukemia Treatment (PDQ) — Health Professional Version",
        "Chronic lymphocytic leukemia is a cancer of the blood and bone marrow that "
        "usually gets worse slowly if it is not treated. Diagnosis requires a peripheral "
        "blood absolute B-lymphocyte count of at least 5,000 per microliter sustained "
        "for three months. Immunophenotyping by flow cytometry demonstrates coexpression "
        "of CD5, CD19, CD20 and CD23 with restricted light chain expression. Prognostic "
        "biomarkers including IGHV mutational status, TP53 mutation and deletion 17p "
        "determine treatment selection and prognosis. The age-adjusted incidence rate of "
        "chronic lymphocytic leukemia is 5.6 per 100,000 persons per year. Treatment is "
        "deferred until the disease is symptomatic or progressive.",
    ),
    "orphanet": (
        1, "Orphanet / Orphadata", "https://www.orpha.net/en/disease/detail/67038",
        "B-cell chronic lymphocytic leukemia (ORPHA:67038)",
        "B-cell chronic lymphocytic leukemia is indexed as ORPHA:67038. Terminology "
        "crosswalk: ICD-10 C91.1; ICD-11 2A82.0; MeSH D015451; UMLS C0023434. B-cell "
        "chronic lymphocytic leukemia is a type of B-cell non-Hodgkin lymphoma "
        "characterised by accumulation of small mature B lymphocytes. The reported "
        "prevalence class is 1-5 per 10,000 in Europe with a validated status.",
    ),
    "iwcll": (
        1, "iwCLL Guidelines (Blood)", "https://ashpublications.org/blood/article/131/25/2745",
        "iwCLL guidelines for diagnosis, indications for treatment, response assessment",
        "The diagnosis of chronic lymphocytic leukemia requires the presence of at least "
        "5,000 B lymphocytes per microliter in the peripheral blood. Indications for "
        "treatment include progressive marrow failure, massive or progressive "
        "splenomegaly, progressive lymphocytosis with an increase of more than 50 percent "
        "over two months, and constitutional symptoms. Response assessment requires "
        "evaluation of blood counts, physical examination and marrow assessment. "
        "Measurable residual disease is assessed by flow cytometry at a threshold of "
        "one CLL cell per 10,000 leukocytes.",
    ),
    "nlm_icd10cm": (
        1, "NLM Clinical Tables (ICD-10-CM)",
        "https://clinicaltables.nlm.nih.gov/apidoc/icd10cm/v3/doc.html",
        "ICD-10-CM codes for chronic lymphocytic leukemia",
        "ICD-10-CM code C91.10 is defined as Chronic lymphocytic leukemia of B-cell type "
        "not having achieved remission. ICD-10-CM code C91.11 is defined as Chronic "
        "lymphocytic leukemia of B-cell type in remission. ICD-10-CM code C91.12 is "
        "defined as Chronic lymphocytic leukemia of B-cell type in relapse.",
    ),
    "dailymed": (
        1, "DailyMed", "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=venclexta",
        "VENCLEXTA (venetoclax) tablet, film coated — prescribing information",
        "VENCLEXTA is indicated for the treatment of adult patients with chronic "
        "lymphocytic leukemia or small lymphocytic lymphoma. The recommended starting "
        "dose is 20 mg once daily for 7 days, followed by a weekly ramp-up over 5 weeks "
        "to the recommended daily dose of 400 mg. Tumor lysis syndrome is an important "
        "identified risk; assess tumor burden and initiate prophylaxis before the first "
        "dose. The most common adverse reactions are neutropenia, diarrhea, nausea, "
        "anemia, upper respiratory tract infection, thrombocytopenia and fatigue. "
        "Dosage should be interrupted for Grade 3 or 4 neutropenia with infection. "
        "In combination with obinutuzumab, treatment is given for a fixed duration of "
        "12 cycles. Venetoclax is administered orally with a meal and water.",
    ),
    "openfda_label": (
        1, "FDA Drug Labeling (openFDA)",
        "https://api.fda.gov/drug/label.json",
        "IMBRUVICA (ibrutinib) — Indications and Usage",
        "IMBRUVICA is a kinase inhibitor indicated for the treatment of adult patients "
        "with chronic lymphocytic leukemia or small lymphocytic lymphoma, including "
        "patients with 17p deletion. The recommended dose for chronic lymphocytic "
        "leukemia is 420 mg orally once daily until disease progression or unacceptable "
        "toxicity. Atrial fibrillation, hypertension and bleeding events are important "
        "identified risks requiring monitoring and possible dose modification. "
        "Treatment with a Bruton tyrosine kinase inhibitor is continuous rather than "
        "fixed duration.",
    ),
    "clinicaltrials": (
        2, "ClinicalTrials.gov", "https://clinicaltrials.gov/study/NCT03462719",
        "Venetoclax and Obinutuzumab in Previously Untreated CLL",
        "This phase 3 study enrolls previously untreated patients with chronic "
        "lymphocytic leukemia requiring treatment according to iwCLL criteria. Patients "
        "are randomised to fixed-duration venetoclax plus obinutuzumab or to "
        "chlorambucil plus obinutuzumab. The primary outcome measure is progression-free "
        "survival. Key secondary outcomes include undetectable minimal residual disease "
        "in peripheral blood and overall response rate. Eligibility requires treatment-"
        "naive disease and adequate organ function.",
    ),
    "acs": (
        3, "American Cancer Society",
        "https://www.cancer.org/cancer/types/chronic-lymphocytic-leukemia/about/key-statistics.html",
        "Key Statistics for Chronic Lymphocytic Leukemia",
        "The American Cancer Society estimates for chronic lymphocytic leukemia in the "
        "United States for 2026 are about 24,900 new cases and about 4,300 deaths. "
        "Chronic lymphocytic leukemia is a slow-growing leukemia that starts in lymphoid "
        "cells and mainly affects older adults, with an average age at diagnosis of "
        "around 70 years.",
    ),
}

BLOCKED = {
    "ama_cpt": "licensed connector not configured",
    "nccn": "licensed connector not configured",
    "icd11": "credentials not configured",
    "loinc": "credentials not configured",
    "cms_icd10": "reference file not installed: icd10cm",
    "cms_hcpcs": "reference file not installed: hcpcs",
}


class _StubSource:
    origin = EvidenceOrigin.APPROVED_API

    def __init__(self, source_id: str, spec: tuple[int, str, str, str, str]) -> None:
        self.source_id = source_id
        self.tier, self.name, self.url, self.title, self.body = spec

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        return ConnectorResult(
            source_id=self.source_id,
            refs=[SourceRef(
                source_id=self.source_id, source_name=self.name, tier=self.tier,
                url=self.url, title=self.title, organization=self.name,
                snippet=self.body, raw={"abstract": self.body}, origin=self.origin,
            )],
            calls=1,
        )


class _Blocked:
    origin = EvidenceOrigin.APPROVED_API
    tier = 1

    def __init__(self, source_id: str, reason: str) -> None:
        self.source_id, self.reason = source_id, reason

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        return ConnectorResult.failure(self.source_id, self.reason)


class _NoWeb:
    """Open-web fallback is unavailable offline, and says so rather than hanging."""
    source_id, tier = "open_web", 5
    origin = EvidenceOrigin.OPEN_WEB

    async def discover(self, ctx, limit):
        return ConnectorResult.failure("open_web", "offline demo: web fallback disabled")

    async def search(self, query: str, limit: int) -> list[SourceRef]:
        return []

    async def scrape(self, url: str):
        return None


def build_registry() -> dict:
    reg: dict = {sid: _StubSource(sid, spec) for sid, spec in FIXTURES.items()}
    reg.update({sid: _Blocked(sid, reason) for sid, reason in BLOCKED.items()})
    reg["open_web"] = _NoWeb()
    return reg


async def seed(indication_key: str = "CLL") -> Run:
    """Create and execute a demo run. Returns the finished run."""
    from .settings import get_questions

    label = get_questions()["indications"][indication_key]["label"]
    cfg = RunConfig(
        drug_brand="Venclexta (venetoclax)",
        indication=label,
        indication_key=indication_key,
        geography="United States",
        objective="Build Claims Line of Therapy",
        target_population=f"Adult patients with {indication_key}",
        additional_context="Offline demo run seeded from bundled fixtures.",
        mode=RunMode.FULL,
        research_cutoff=orch.default_cutoff(),
    )
    run = Run(config=cfg, reference=orch.new_reference())
    run.agents = orch.build_agent_states(orch.all_buckets())
    store.save_run(run)
    registry = build_registry()
    await orch.Orchestrator(run, registry).execute()

    # A full run pauses at the human review gate after the first wave. The
    # demo exists to populate every screen, so it plays the reviewer and
    # continues; a live run stops there and waits for a person.
    paused = store.get_run(run.id)
    if paused is not None and paused.status is RunStatus.AWAITING_REVIEW:
        await orch.Orchestrator(paused, registry).resume()
    return store.get_run(run.id) or run


def seed_sync(indication_key: str = "CLL") -> Run:
    return asyncio.run(seed(indication_key))
