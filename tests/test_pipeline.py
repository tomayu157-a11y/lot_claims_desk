"""Pipeline integration test with a stub connector registry.

Validates orchestration, scoring, contradiction surfacing, synthesis and QA
without touching the network, so a connector outage can never make this test
lie about the pipeline.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.connectors.base import ConnectorResult, RetrievalContext
from celestra.events import bus
from celestra.models import (
    AgentStatus,
    EvidenceOrigin,
    QuestionStatus,
    Run,
    RunConfig,
    RunMode,
    RunStatus,
    SourceRef,
)
from celestra.services import orchestrator as orch
from celestra.store import Store

FIXTURES = {
    "seer": (
        1, "NCI SEER", "https://seer.cancer.gov/statfacts/html/clyl.html",
        "Chronic Lymphocytic Leukemia — Cancer Stat Facts",
        "The overall rate of new cases of chronic lymphocytic leukemia was 4.7 per 100,000 "
        "men and women per year based on 2018-2022 cases, age-adjusted. The death rate was "
        "1.0 per 100,000 men and women per year. In 2021, there were an estimated 214,573 "
        "people living with chronic lymphocytic leukemia in the United States. Five-year "
        "relative survival for chronic lymphocytic leukemia is 88.5 percent. Diagnosis is "
        "confirmed by peripheral blood flow cytometry demonstrating a clonal B-cell "
        "population. Risk stratification uses the Rai and Binet staging systems together "
        "with IGHV mutational status and TP53 aberration testing.",
    ),
    "nci": (
        1, "National Cancer Institute (NCI)",
        "https://www.cancer.gov/types/leukemia/hp/cll-treatment-pdq",
        "Chronic Lymphocytic Leukemia Treatment (PDQ) — Health Professional Version",
        "Chronic lymphocytic leukemia is a cancer of the blood and bone marrow that usually "
        "gets worse slowly if it is not treated. Diagnosis requires a peripheral blood "
        "absolute B-lymphocyte count of at least 5,000 per microliter sustained for three "
        "months. Immunophenotyping demonstrates coexpression of CD5, CD19, CD20 and CD23. "
        "Prognostic biomarkers including IGHV mutational status, TP53 mutation and "
        "deletion 17p determine treatment selection. Estimated new cases of chronic "
        "lymphocytic leukemia in the United States in 2025 are 20,700 cases.",
    ),
    "acs": (
        3, "American Cancer Society",
        "https://www.cancer.org/cancer/types/chronic-lymphocytic-leukemia.html",
        "Key Statistics for Chronic Lymphocytic Leukemia",
        "The American Cancer Society estimates for chronic lymphocytic leukemia in the "
        "United States for 2026 are about 24,900 new cases and about 4,300 deaths. Chronic "
        "lymphocytic leukemia is a slow-growing leukemia that starts in lymphoid cells and "
        "mainly affects older adults, with an average age at diagnosis around 70 years.",
    ),
    "orphanet": (
        1, "Orphanet / Orphadata", "https://www.orpha.net/en/disease/detail/67038",
        "B-cell chronic lymphocytic leukemia (ORPHA:67038)",
        "B-cell chronic lymphocytic leukemia is indexed as ORPHA:67038 and maps to ICD-10 "
        "code C91.1 and ICD-11 code 2A82.0 in the Orphanet cross-referencing dataset. The "
        "reported prevalence class is 1-5 per 10,000 in Europe with a validated status.",
    ),
}


class StubConnector:
    def __init__(self, source_id, tier, name, url, title, text):
        self.source_id, self.tier = source_id, tier
        self.name, self.url, self.title, self.text = name, url, title, text
        self.origin = EvidenceOrigin.APPROVED_API
        self.calls = 0

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        self.calls += 1
        return ConnectorResult(
            source_id=self.source_id,
            refs=[SourceRef(
                source_id=self.source_id, source_name=self.name, tier=self.tier,
                url=self.url, title=self.title, organization=self.name,
                snippet=self.text, origin=self.origin,
                raw={"abstract": self.text},
            )],
            calls=1,
        )


class DeadConnector:
    def __init__(self, source_id, reason="credentials not configured"):
        self.source_id, self.tier = source_id, 1
        self.origin = EvidenceOrigin.APPROVED_API
        self.reason = reason

    async def discover(self, ctx, limit):
        return ConnectorResult.failure(self.source_id, self.reason)


class StubWeb:
    source_id, tier = "open_web", 5
    origin = EvidenceOrigin.OPEN_WEB

    def __init__(self):
        self.searches = 0

    async def discover(self, ctx, limit):
        return ConnectorResult(source_id="open_web")

    async def search(self, query, limit):
        self.searches += 1
        return [SourceRef(
            source_id="open_web", source_name="Open Web (Supplementary)", tier=5,
            url="https://example.org/cll-overview", title="CLL overview",
            organization="example.org", origin=EvidenceOrigin.OPEN_WEB,
        )]

    async def scrape(self, url):
        return SourceRef(
            source_id="open_web", source_name="Open Web (Supplementary)", tier=5,
            url=url, title="CLL overview", organization="example.org",
            origin=EvidenceOrigin.OPEN_WEB,
            snippet="Chronic lymphocytic leukemia treatment is generally deferred until "
                    "the disease becomes symptomatic or progressive according to widely "
                    "used criteria described across clinical references.",
        )


def build_stub_registry():
    reg = {sid: StubConnector(sid, *spec) for sid, spec in FIXTURES.items()}
    for dead in ("icd11", "loinc", "cms_icd10", "cms_hcpcs", "cms_gems",
                 "nccn", "ama_cpt", "purple_book"):
        reg[dead] = DeadConnector(dead)
    reg["open_web"] = StubWeb()
    return reg


async def main() -> int:
    tmp = Path("/tmp/celestra_test.db")
    tmp.unlink(missing_ok=True)
    import celestra.store as store_mod
    import celestra.services.orchestrator as orch_mod
    test_store = Store(tmp)
    store_mod.store = test_store
    orch_mod.store = test_store

    cfg = RunConfig(
        indication="Chronic Lymphocytic Leukemia", indication_key="CLL",
        target_population="Adult patients", mode=RunMode.SINGLE,
        selected_agent="A", research_cutoff="2026-09-09",
    )
    run = Run(config=cfg, reference="RUN-TEST01")
    test_store.save_run(run)

    seen: list[str] = []
    original = bus.publish

    async def spy(run_id, type_, **data):
        seen.append(type_)
        await original(run_id, type_, **data)

    bus.publish = spy  # type: ignore[assignment]
    registry = build_stub_registry()
    await orch.Orchestrator(run, registry).execute()
    bus.publish = original  # type: ignore[assignment]

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    run = test_store.get_run(run.id)
    questions = test_store.get_questions(run.id)
    evidence = test_store.get_evidence(run.id)
    insights = test_store.get_insights(run.id)
    stages = test_store.get_stage_reports(run.id)
    contras = test_store.get_contradictions(run.id)
    qa = test_store.get_qa(run.id)

    print("\n== pipeline ==")
    check("run completed", run.status is RunStatus.COMPLETED, run.error or "")
    check("agent completed", run.agents["A"].status is AgentStatus.COMPLETE)
    check("only the selected agent ran", list(run.agents) == ["A"], str(list(run.agents)))
    check("questions planned", len(questions) == 5, f"{len(questions)} planned")
    check("evidence extracted", len(evidence) >= 8, f"{len(evidence)} items")
    check("quotes are verbatim from fixtures",
          all(any(e.quote in spec[4] for spec in FIXTURES.values())
              for e in evidence if not e.is_supplementary))
    check("insights created", len(insights) == len(questions), f"{len(insights)}")
    check("stage report built", len(stages) == 1 and bool(stages[0].synthesis))
    check("stage tables built", len(stages[0].tables) >= 1, f"{len(stages[0].tables)} tables")
    check("takeaways present", len(stages[0].takeaways) >= 1)
    check("observability classified", len(stages[0].observability) >= 1)

    print("\n== thresholds and escalation ==")
    answered = [q for q in questions if q.status is QuestionStatus.SUFFICIENT]
    check("some questions reached sufficiency", len(answered) >= 1,
          f"{len(answered)}/{len(questions)}")
    check("unanswered questions record a reason",
          all(q.unmet_reason for q in questions if q.status is not QuestionStatus.SUFFICIENT))
    unregistered = {s for q in questions for s in q.sources_attempted} - set(FIXTURES) - {"open_web"}
    check("sources with no working connector are still recorded as attempted",
          bool(unregistered), f"{len(unregistered)} recorded: {sorted(unregistered)[:5]}")
    check("coverage scored", all(q.coverage_score >= 0 for q in questions))

    print("\n== provenance ==")
    check("every evidence item has a url and source", all(e.url and e.source_id for e in evidence))
    web = [e for e in evidence if e.is_supplementary]
    check("web evidence is tier 5 when present",
          all(e.tier == 5 for e in web), f"{len(web)} supplementary items")
    check("insights name their sources", all(i.source_ids or i.confidence.value == "rejected"
                                             for i in insights))

    print("\n== contradictions ==")
    check("conflict surfaced between NCI and ACS", len(contras) >= 1, f"{len(contras)} found")
    check("no conflict auto-resolved",
          all(c.review_action.value == "pending" for c in contras))

    print("\n== qa ==")
    check("qa metrics stored", qa is not None)
    check("qa counts match", qa.questions_planned == len(questions))
    check("checklist has no false pass",
          all(c["status"] in ("PASS", "FAIL", "NOT APPLICABLE") for c in qa.checklist))
    check("readiness written", bool(qa.readiness))

    print("\n== events ==")
    for required in ("run_started", "agent_status", "source_used", "insight_added",
                     "stage_complete", "run_complete"):
        check(f"emitted {required}", required in seen)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
