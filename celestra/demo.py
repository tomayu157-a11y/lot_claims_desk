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
from .models import EvidenceOrigin, Run, RunConfig, RunMode, SourceRef
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
    await orch.Orchestrator(run, build_registry()).execute()
    return store.get_run(run.id) or run


def seed_sync(indication_key: str = "CLL") -> Run:
    return asyncio.run(seed(indication_key))
