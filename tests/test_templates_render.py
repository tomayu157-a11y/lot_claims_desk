"""Render every Celestra template against realistic fake context.

The app cannot be started yet (routes are still being written), so this test
exercises the templates directly: a Jinja2 environment over
``celestra/templates`` with the same filters and globals the app will register,
fed with objects built from the REAL models in ``celestra.models``.

It also enforces the vocabulary rule: the internal grouping word must never
reach rendered output. The execution units are AGENTS in the UI.
"""
from __future__ import annotations

import html
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from celestra.models import (  # noqa: E402
    AgentState,
    AgentStatus,
    Confidence,
    Contradiction,
    ContradictionSeverity,
    Evidence,
    EvidenceOrigin,
    Insight,
    InsightTable,
    QAMetrics,
    ReviewAction,
    Run,
    RunConfig,
    RunMode,
    RunStatus,
    StageReport,
    VerificationTag,
)

TEMPLATES = ROOT / "celestra" / "templates"
STATIC = ROOT / "celestra" / "static"

TAG_RE = re.compile(
    r"\[(VERIFIED|GENERAL KNOWLEDGE|ORIGINAL|INFERENCE|NOT VERIFIED|UPDATE(?:[^\]]*)?)\]"
)


def tagify(value):
    """Stub of the filter the app will register: escape, then pill the tags."""
    if value is None:
        return Markup("")
    escaped = html.escape(str(value), quote=False)

    def repl(match):
        label = match.group(1)
        slug = label.split("—")[0].strip().lower().replace(" ", "-")
        return f'<span class="vtag vtag-{slug}">[{label}]</span>'

    return Markup(TAG_RE.sub(repl, escaped))


def url_for(name, **params):
    """Stub. Templates use literal paths, this only guards against surprises."""
    if name == "static":
        return "/static/" + str(params.get("path") or params.get("filename") or "")
    tail = "/".join(str(v) for v in params.values())
    return f"/{name}/{tail}".rstrip("/")


@pytest.fixture(scope="module")
def env() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    environment.filters["tagify"] = tagify
    environment.globals["url_for"] = url_for
    return environment


# ---------------------------------------------------------------------------
# fake data
# ---------------------------------------------------------------------------
NOW = datetime(2026, 9, 1, 5, 49, tzinfo=timezone.utc)

AGENT_SPECS = [
    ("A", "clinical-landscape", "Clinical Landscape Agent",
     "Disease context, patient journey, clinical events", "leaf", ["stage_1"], [], 1),
    ("C", "treatment-evidence", "Treatment Evidence Agent",
     "Therapies, treatment settings, regulatory evidence", "pill", ["stage_2"], [], 1),
    ("B", "diagnostic-footprint", "Diagnostic Footprint Agent",
     "Claims signals, diagnosis & procedure codes", "microscope", ["stage_3"], ["A"], 2),
    ("D", "treatment-logic", "Treatment Logic Agent",
     "Treatment patterns, episode logic, LOT rules", "flow", ["stage_4"], ["C"], 2),
    ("E", "patient-journey", "Patient Journey Agent",
     "Discontinuation, monitoring, response and outcomes", "route", ["stage_5"], ["B", "C", "D"], 3),
    ("F", "information-synthesis", "Information Synthesis Agent",
     "Cross-agent analysis, key insights and gaps", "sparkle", ["stage_6"], ["A", "B", "C", "D", "E"], 4),
]

STATUSES = [
    AgentStatus.COMPLETE, AgentStatus.RESEARCHING, AgentStatus.SYNTHESISING,
    AgentStatus.QUEUED, AgentStatus.BLOCKED, AgentStatus.FAILED,
]


def make_agents() -> list[AgentState]:
    agents = []
    for i, (letter, key, name, tagline, ic, stages, deps, wave) in enumerate(AGENT_SPECS):
        agents.append(AgentState(
            bucket=letter, key=key, name=name, tagline=tagline, icon=ic,
            stages=stages, depends_on=deps, status=STATUSES[i], wave=wave,
            progress=[1.0, 0.42, 0.75, 0.0, 0.0, 0.3][i],
            message="Querying NCI PDQ for adult ALL diagnostic criteria…",
            questions_total=5, questions_answered=[5, 2, 4, 0, 0, 1][i],
            evidence_count=[28, 11, 19, 0, 0, 3][i],
            sources_used=["NCI PDQ", "SEER"],
            started_at=NOW, finished_at=NOW + timedelta(minutes=4),
            error="Connector timed out after 30s" if STATUSES[i] is AgentStatus.FAILED else "",
        ))
    return agents


def make_run(agents: list[AgentState]) -> Run:
    return Run(
        id="run_2f8c1a0b9d31",
        reference="RUN-62CEEF6A",
        config=RunConfig(
            drug_brand="Blinatumomab (Blincyto)",
            indication="Acute Lymphoblastic Leukemia (ALL)",
            indication_key="ALL",
            geography="United States",
            objective="Build Claims Line of Therapy",
            target_population="Adult patients with newly diagnosed ALL",
            additional_context="Focus on systemic therapy; exclude paediatric protocols.",
            mode=RunMode.FULL,
            selected_agent=None,
            research_cutoff="2026-09-01",
        ),
        status=RunStatus.COMPLETED,
        agents={a.bucket: a for a in agents},
        created_at=NOW, started_at=NOW, finished_at=NOW + timedelta(minutes=12),
        approved_at=None,
    )


def make_evidence() -> list[Evidence]:
    return [
        Evidence(
            id="ev_1", question_id="q_1", source_id="nci_pdq",
            source_name="NCI PDQ", organization="National Cancer Institute (NCI)", tier=1,
            url="https://www.cancer.gov/types/leukemia/hp/adult-all-treatment-pdq",
            title="Adult Acute Lymphoblastic Leukemia Treatment (PDQ)",
            published="2026-04-11",
            quote=("Estimated new cases of acute lymphoblastic leukemia in the United States in "
                   "2025 are 6,100, with 1,400 estimated deaths."),
            context="Section: Incidence and Mortality",
            origin=EvidenceOrigin.APPROVED_API, tag=VerificationTag.VERIFIED,
            relevance=0.94, identifiers={"pmid": "26389240"}, retrieved_at=NOW,
        ),
        Evidence(
            id="ev_2", question_id="q_1", source_id="seer",
            source_name="NCI SEER", organization="NCI SEER", tier=1,
            url="https://seer.cancer.gov/statfacts/html/alyl.html",
            title="Cancer Stat Facts: Acute Lymphocytic Leukemia", published="2026-01-02",
            quote=("In 2023, there were an estimated 126,118 people living with acute lymphocytic "
                   "leukemia in the United States."),
            origin=EvidenceOrigin.TARGETED_SEARCH, tag=VerificationTag.VERIFIED,
            relevance=0.88, identifiers={}, retrieved_at=NOW,
        ),
        Evidence(
            id="ev_3", question_id="q_2", source_id="open_web_1",
            source_name="Hematology review blog", organization="", tier=5,
            url="https://example.org/adult-all-overview",
            title="Adult ALL overview", published="",
            quote="Roughly one in five adults with ALL carries the Philadelphia chromosome.",
            origin=EvidenceOrigin.OPEN_WEB, tag=VerificationTag.NOT_VERIFIED,
            relevance=0.41, identifiers={}, retrieved_at=NOW,
        ),
    ]


def make_insights() -> list[Insight]:
    specs = [
        ("Patient Population", "Adults with metastatic NSCLC receiving systemic treatment.",
         "Clinical", "stage_1", Confidence.HIGH, VerificationTag.VERIFIED,
         ["nci_pdq", "seer", "acs", "ash"], ReviewAction.APPROVED, ""),
        ("Disease Journey", "Diagnosis → Biomarker Testing → Treatment → Progression.",
         "Journey", "stage_5", Confidence.HIGH, VerificationTag.INFERENCE,
         ["nci_pdq", "acs"], ReviewAction.PENDING, ""),
        ("Combination Therapy", "Combination therapy is frequently used in first-line treatment.",
         "Treatment", "stage_2", Confidence.MEDIUM, VerificationTag.VERIFIED,
         ["dailymed", "nccn", "acs"], ReviewAction.MODIFIED,
         "For this analysis, focus only on patients receiving systemic therapy."),
        ("Progression Detection",
         "Disease progression cannot be consistently observed directly from claims.",
         "Diagnostic", "stage_3", Confidence.REQUIRES_INPUT, VerificationTag.ORIGINAL,
         ["cms", "loinc"], ReviewAction.PENDING, ""),
        ("Deprecated code family", "ICD-9 mapping could not be confirmed in any coding authority.",
         "Logic", "stage_4", Confidence.REJECTED, VerificationTag.NOT_VERIFIED,
         [], ReviewAction.PENDING, ""),
    ]
    out = []
    for i, (title, summary, cat, stage, conf, tag, sids, action, user_input) in enumerate(specs):
        out.append(Insight(
            id=f"ins_{i}", run_id="run_2f8c1a0b9d31", stage=stage, bucket="A",
            category=cat, title=title, summary=summary,
            detail=("Combination therapies (e.g., pembrolizumab + chemotherapy) are commonly used "
                    "in first-line treatment for metastatic disease [VERIFIED]."),
            confidence=conf, tag=tag, evidence_ids=["ev_1", "ev_2"], source_ids=sids,
            question_ids=["q_1"], used_web_fallback=(i == 3), review_action=action,
            user_input=user_input, impacted_insight_ids=["ins_1"], created_at=NOW,
        ))
    return out


def make_contradictions() -> list[Contradiction]:
    return [
        Contradiction(
            id="con_1", run_id="run_2f8c1a0b9d31", stage="stage_1",
            topic="overall prevalence and mortality statistics in the us adult population",
            source_a_name="National Cancer Institute (NCI)", source_a_tier=1,
            source_a_claim=("Estimated new cases of acute lymphoblastic leukemia in the United "
                            "States in 2025 are 6,100, with 1,400 estimated deaths."),
            source_a_url="https://www.cancer.gov/types/leukemia/hp/adult-all-treatment-pdq",
            source_b_name="American Cancer Society", source_b_tier=3,
            source_b_claim=("The American Cancer Society estimates for 2026 are about 6,250 new "
                            "cases and about 1,600 deaths."),
            source_b_url="https://www.cancer.org/cancer/types/acute-lymphocytic-leukemia",
            reason=("Sources sit at materially different evidence tiers; the lower-tier source "
                    "must not override the higher-tier source."),
            severity=ContradictionSeverity.ESCALATED, review_action=ReviewAction.PENDING,
            reviewer_note="", created_at=NOW,
        ),
        Contradiction(
            id="con_2", run_id="run_2f8c1a0b9d31", stage="stage_1",
            topic="2025/2026 US estimated new cases and deaths for ALL",
            source_a_name="National Cancer Institute (NCI)", source_a_tier=1,
            source_a_claim="6,100 new cases and 1,400 deaths in 2025.",
            source_b_name="American Cancer Society", source_b_tier=3,
            source_b_claim="About 6,250 new cases and about 1,600 deaths in 2026.",
            reason="Divergence is due to different projected calendar years.",
            severity=ContradictionSeverity.NOTED, review_action=ReviewAction.ACKNOWLEDGED,
            reviewer_note="Both figures kept; SME to pick the reporting year.", created_at=NOW,
        ),
    ]


def make_stage() -> StageReport:
    return StageReport(
        id="stg_1", run_id="run_2f8c1a0b9d31", stage="stage_1", bucket="A",
        name="Disease and Diagnostic Foundation: Acute Lymphoblastic Leukemia (ALL) in Adults",
        core_question="Who gets the disease and how is it diagnosed?",
        agent_name="Clinical Landscape Agent",
        framework_steps=["Disease Understanding & Epidemiology Review",
                         "Diagnostic Criteria & Confirmatory Workup Review"],
        step_numbers=[1, 2],
        substeps={"2A": "Diagnostic criteria and confirmatory-workup research",
                  "2B": "Align diagnostic criteria with disease taxonomy, subtypes and population"},
        gate="Reconciliation gate — diagnostic definitions aligned with disease taxonomy",
        output_name="DiseaseDiagnosisProfile",
        what_happens=("This stage establishes the clinical definition, natural history, "
                      "epidemiology, molecular subtypes and diagnostic workup [VERIFIED]."),
        expected_output=["Epidemiology snapshot table (Metric | Value | Source)",
                         "Subtype / biology breakdown table",
                         "Diagnostic workup table", "Key takeaways"],
        synthesis=("Acute lymphoblastic leukemia (ALL) is an aggressive hematologic malignancy "
                   "characterized by uncontrolled proliferation of lymphoblasts [VERIFIED]. "
                   "Risk stratification integrates baseline clinical variables [INFERENCE]."),
        narratives=[
            {"heading": "Disease Definition and Natural History",
             "body": "ALL is an aggressive malignancy of the blood and bone marrow [VERIFIED] "
                     "[Source: NCI PDQ; ACS]."},
            {"heading": "Risk Stratification and Clinical Variables",
             "body": "Advanced age is a major adverse prognostic factor in adults [VERIFIED]."},
        ],
        tables=[
            InsightTable(
                title="Epidemiology Snapshot",
                columns=["Metric", "Value", "Source"],
                rows=[
                    ["Estimated New US Cases (2025/2026)",
                     "[VERIFIED] 6,100 (2025 NCI PDQ) to 6,250 (2026 ACS / SEER)",
                     "[Source: NCI PDQ; SEER Stat Facts; ACS]"],
                    ["Overall 5-Year Relative Survival",
                     "[VERIFIED] 73.2% (2016–2022 SEER data, all ages)",
                     "[Source: SEER Stat Facts]"],
                ],
                footnote="Statistics reflect combined pediatric and adult data where indicated.",
            ),
            InsightTable(
                title="Diagnostic Workup Table",
                columns=["Category", "Example Tests / Procedures", "Purpose"],
                rows=[["Bone Marrow Examination", "[VERIFIED] Bone marrow aspirate and biopsy",
                       "[VERIFIED] Establish the diagnosis (>=20% lymphoblasts)"]],
                footnote="",
            ),
        ],
        takeaways=[
            "Acute lymphoblastic leukemia is an aggressive lymphoid malignancy [VERIFIED].",
            "Philadelphia chromosome positivity dictates a high-risk prognosis [VERIFIED].",
        ],
        assumptions=["Published NCI PDQ and SEER statistics reflect US clinical epidemiology."],
        limitations=["Registry statistics combine paediatric and adult populations."],
        observability=[
            {"concept": "Initial ALL diagnosis and blast percentage (>=20%)",
             "classification": "PROXY SIGNAL",
             "basis": "Bone marrow pathology reports and ICD-O-3 registry codes (e.g. 9811/3)",
             "limitation": "Exact blast percentage is rarely captured in billing claims."},
            {"concept": "Measurable Residual Disease (MRD) Status",
             "classification": "PROXY SIGNAL",
             "basis": "Flow cytometry, PCR or NGS MRD assay billing",
             "limitation": "Threshold definitions are hard to standardise from claims."},
        ],
        unanswered=[{"question": "What share of adult patients receive transplant in first remission?",
                     "aspect": "post-remission therapy",
                     "reason": "No approved source reported a US-specific proportion."}],
        evidence_count=28, source_count=7, supplementary_count=0,
        tiers_represented=[1, 2, 3], created_at=NOW,
    )


def make_qa() -> QAMetrics:
    return QAMetrics(
        questions_planned=5, questions_sufficient=5, questions_web_only=0,
        questions_below_threshold=0, mean_coverage=1.0, evidence_total=28,
        evidence_approved=21, evidence_supplementary=0, distinct_sources=7,
        conflicts_surfaced=6,
        checklist=[
            {"check": "Every material factual claim carries an inline source reference",
             "status": "PASS", "detail": "28 evidence items carry a source URL and citation"},
            {"check": "No claims code is asserted without a coding-authority source",
             "status": "PASS", "detail": "All reported codes were located verbatim"},
            {"check": "Source conflicts are surfaced rather than merged",
             "status": "FAIL", "detail": "1 conflict was merged and must be re-opened"},
        ],
        readiness=("The output is ready for initial Subject Matter Expert (SME) review. "
                   "Strongest aspects include rigorous adherence to source-first constraints."),
        sme_checklist=[
            "Verify epidemiological incidence and mortality figures against SEER.",
            "Confirm immunophenotypic markers distinguishing B-cell from T-cell ALL.",
        ],
    )


@pytest.fixture(scope="module")
def context() -> dict:
    agents = make_agents()
    run = make_run(agents)
    insights = make_insights()
    stage = make_stage()
    evidence = make_evidence()
    contradictions = make_contradictions()

    source_names = {
        "nci_pdq": "NCI PDQ", "seer": "NCI SEER", "acs": "American Cancer Society",
        "ash": "ASH / Blood", "dailymed": "DailyMed", "nccn": "NCCN",
        "cms": "CMS", "loinc": "LOINC",
    }

    return {
        "active": "overview",
        "credentials": {"anthropic_api_key": False, "firecrawl_api_key": False,
                        "ncbi_api_key": True, "icd11": False, "loinc": True},
        "messages": [{"level": "ok", "text": "Discovery finished in 12 minutes."}, "Plain message."],
        "run": run,
        "runs": [run],
        "agents": agents,
        "agent_by_stage": {a.stages[0]: {"name": a.name, "icon": a.icon} for a in agents if a.stages},
        "source_chips": [
            {"key": "pubmed", "name": "PubMed", "used": True},
            {"key": "fda", "name": "FDA", "used": False},
            "NCCN", "ASCO", "EMA",
        ],
        "counts": {"high": 16, "medium": 6, "requires_input": 2, "rejected": 0,
                   "insights": 24, "sources": 18,
                   "approved": 18, "modified": 4, "user_inputs": 3, "assumptions": 2},
        "categories": [
            {"key": "clinical", "label": "Clinical", "count": 6},
            {"key": "treatment", "label": "Treatment", "count": 6},
            {"key": "diagnostic", "label": "Diagnostic", "count": 6},
            {"key": "logic", "label": "Logic", "count": 6},
        ],
        "takeaways": insights[:3],
        "insights": insights,
        "insight": insights[2],
        "source_names": source_names,
        "impacts": [
            {"agent_name": "Treatment Logic Agent",
             "description": "LOT rules will consider combination therapy as a single regimen."},
            {"agent_name": "Diagnostic Footprint Agent", "description": "No change expected."},
        ],
        "evidence": evidence,
        "contradictions": contradictions,
        "contradiction": contradictions[0],
        "stage": stage,
        "stages": [stage],
        "qa": make_qa(),
        "params": [
            {"label": "Indication", "value": "Acute Lymphoblastic Leukemia (ALL)"},
            {"label": "Population", "value": "Adult"},
            {"label": "Research cutoff", "value": "2026-09-01"},
        ],
        "executive_summary": ("This clinical desk-research deliverable establishes the disease and "
                              "diagnostic foundation for adult ALL in the United States [VERIFIED]."),
        "sources": [
            {"organization": "National Cancer Institute (NCI)", "title": "Adult ALL Treatment (PDQ)",
             "published": "Not stated", "url": "https://www.cancer.gov/types/leukemia",
             "tier": 1, "evidence_items": 13},
            {"organization": "American Cancer Society", "title": "", "published": "August 13, 2025",
             "url": "https://www.cancer.org/cancer/types/acute-lymphocytic-leukemia",
             "tier": 3, "evidence_items": 5},
        ],
        "used": [
            {"name": "NCI PDQ", "organization": "National Cancer Institute (NCI)", "tier": 1,
             "access_method": "api", "evidence_items": 13, "url": "https://www.cancer.gov",
             "domain": "cancer.gov"},
            {"name": "Hematology review blog", "tier": 5, "access_method": "firecrawl_search",
             "evidence_items": 1, "url": "https://example.org"},
        ],
        "unavailable": [
            {"name": "Orphanet", "tier": 1, "access_method": "api",
             "reason": "Returned no documents for the indication synonyms tried."},
            {"name": "LOINC", "tier": 1, "access_method": "licensed", "blocked_by": "Credentials",
             "reason": "LOINC username and password are not configured."},
        ],
        "health": {"pubmed": True, "orphanet": False,
                   "dailymed": {"status": "degraded", "latency_ms": 2400}},
        "included": [
            {"name": "Clinical Context", "icon": "leaf",
             "description": "Disease definition, patient population, journey, key events"},
            {"name": "Treatment Landscape", "icon": "pill",
             "description": "Approved therapies, treatment settings, regulatory evidence"},
        ],
        "indications": [
            {"key": "ALL", "label": "Acute Lymphoblastic Leukemia", "abbreviation": "ALL",
             "enabled": True},
            {"key": "CLL", "label": "Chronic Lymphocytic Leukemia", "abbreviation": "CLL",
             "enabled": True},
            {"key": "", "label": "Multiple Myeloma", "abbreviation": "", "enabled": False},
        ],
        "therapy_areas": [{"value": "Oncology", "enabled": True},
                          {"value": "Hematology", "enabled": False}],
        "populations": [{"value": "All", "enabled": True}],
        "objectives": [
            {"value": "Build Claims Line of Therapy", "label": "LOT claims", "enabled": True},
            {"value": "Targeting", "label": "Targeting", "enabled": False},
        ],
        "geographies": [{"value": "United States", "enabled": True},
                        {"value": "Europe", "enabled": False}],
        "llm": {"configured": False, "provider": "anthropic", "explicit": False,
                "model": "claude-opus-5", "gaps": ["ANTHROPIC_API_KEY"]},
        "thresholds": {"sufficiency": {"min_evidence_items": 3, "min_distinct_sources": 2},
                       "confidence": {"high": {"min_coverage_score": 0.8}}},
        "datasets": [
            {"name": "ICD-10-CM", "version": "2026", "rows": 74260,
             "installed_at": "2026-08-30", "available": True,
             "description": "US diagnosis code universe"},
            {"name": "LOINC", "version": "—", "rows": 0, "available": False},
        ],
        "message": "That run could not be found",
        "detail": "No run exists with reference RUN-000000.",
        "last_seq": 42,
    }


def all_templates() -> list[str]:
    names = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        names.append(str(path.relative_to(TEMPLATES)).replace("\\", "/"))
    return names


PAGE_TEMPLATES = [n for n in all_templates() if not n.startswith("partials/")]


def test_expected_templates_exist():
    expected = {
        "base.html", "home.html", "new_project.html", "discovery.html", "overview.html",
        "insights.html", "contradictions.html", "stage_report.html", "report.html",
        "approval.html", "sources_panel.html", "projects.html", "settings.html", "error.html",
        "partials/insight_card.html", "partials/insight_modal.html",
        "partials/evidence_panel.html",
    }
    missing = expected - set(all_templates())
    assert not missing, f"missing templates: {sorted(missing)}"


MACRO_ONLY = {"partials/icons.html", "partials/macros.html"}


@pytest.mark.parametrize("name", all_templates())
def test_template_renders(env, context, name):
    out = env.get_template(name).render(**context)
    if name not in MACRO_ONLY:
        assert out.strip(), f"{name} rendered empty"


@pytest.mark.parametrize("name", all_templates())
def test_no_internal_vocabulary_in_output(env, context, name):
    out = env.get_template(name).render(**context)
    assert "bucket" not in out.lower(), f"{name} leaked the internal grouping word"


@pytest.mark.parametrize("name", all_templates())
def test_no_external_asset_references(env, context, name):
    """Templates may LINK to external sources (evidence urls) but must not FETCH
    stylesheets, scripts, fonts or images from anywhere off-origin."""
    out = env.get_template(name).render(**context)
    for pattern in (r'<link[^>]+href="https?://', r'<script[^>]+src="https?://',
                    r'@import\s+url\(["\']?https?://', r'<img[^>]+src="https?://',
                    r'url\(["\']?https?://'):
        assert not re.search(pattern, out), f"{name} pulls an external asset ({pattern})"


def test_source_names_are_rendered_on_insight_cards(env, context):
    out = env.get_template("insights.html").render(**context)
    assert "NCI PDQ" in out and "American Cancer Society" in out


def test_supplementary_web_evidence_is_flagged(env, context):
    out = env.get_template("partials/evidence_panel.html").render(**context)
    assert "Supplementary web evidence" in out


def test_contradictions_are_not_auto_resolved(env, context):
    out = env.get_template("contradictions.html").render(**context)
    assert "auto-resolved" in out
    for label in ("Prefer A", "Prefer B", "Acknowledge both"):
        assert label in out


def test_confidence_chip_labels(env, context):
    out = env.get_template("overview.html").render(**context)
    for label in ("High Confidence", "Medium Confidence", "Require Input", "Rejected"):
        assert label in out


def test_verification_tags_become_pills(env, context):
    out = env.get_template("stage_report.html").render(**context)
    assert 'class="vtag vtag-verified">[VERIFIED]' in out
    assert 'vtag-inference">[INFERENCE]' in out


def test_agent_names_not_letters(env, context):
    out = env.get_template("discovery.html").render(**context)
    for agent in make_agents():
        assert agent.name in out


# ---------------------------------------------------------------------------
# static asset checks
# ---------------------------------------------------------------------------
def test_css_braces_balanced():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}"), "unbalanced braces in app.css"
    depth = 0
    for ch in css:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            assert depth >= 0, "closing brace before opening brace in app.css"
    assert depth == 0


def test_static_has_no_external_references():
    for path in list((STATIC).rglob("*.css")) + list((STATIC).rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, f"{path} references a remote host"


def test_no_bucket_word_in_sources():
    for path in list(TEMPLATES.rglob("*.html")) + list(STATIC.rglob("*.css")) + list(STATIC.rglob("*.js")):
        text = path.read_text(encoding="utf-8").lower()
        # the word may appear only inside a Jinja comment explaining the rule
        stripped = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
        stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
        assert "bucket" not in stripped, f"{path} mentions the internal grouping word"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


@pytest.fixture(scope="module")
def sparse_context(context) -> dict:
    """Same contract, but every collection empty: exercises the empty states."""
    run = Run(
        id="run_empty", reference="", config=RunConfig(
            indication="Chronic Lymphocytic Leukemia (CLL)", indication_key="CLL",
            mode=RunMode.SINGLE, selected_agent="clinical-landscape"),
        status=RunStatus.PENDING, agents={},
    )
    stage = StageReport(stage="stage_2", bucket="C", name="Treatment evidence",
                        core_question="", agent_name="Treatment Evidence Agent")
    sparse = dict(context)
    sparse.update({
        "active": None, "credentials": {}, "messages": [], "run": run, "runs": [],
        "agents": [], "agent_by_stage": {}, "source_chips": [], "counts": {},
        "categories": [], "takeaways": [], "insights": [], "source_names": {},
        "impacts": [], "evidence": [], "contradictions": [], "stage": stage, "stages": [],
        "qa": None, "params": {}, "sources": [], "used": [], "unavailable": [],
        "health": None, "included": [], "indications": [], "objectives": [],
        "geographies": [], "therapy_areas": [], "populations": [],
        "llm": {"configured": True, "provider": "azure_openai", "explicit": True,
                "model": "my-deployment", "gaps": []},
        "thresholds": {}, "datasets": [], "last_seq": 0,
    })
    return sparse


@pytest.mark.parametrize("name", all_templates())
def test_template_renders_with_empty_collections(env, sparse_context, name):
    out = env.get_template(name).render(**sparse_context)
    assert "bucket" not in out.lower()
    if name not in MACRO_ONLY:
        assert out.strip(), f"{name} rendered empty"
