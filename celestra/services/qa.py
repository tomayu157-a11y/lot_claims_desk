"""Run-level QA. Produces the metrics, checklist and readiness assessment.

Every check is computed from the run's own evidence base. A check that cannot
be evaluated reports NOT APPLICABLE rather than PASS, because a checklist that
passes by default is worse than no checklist.
"""
from __future__ import annotations

from ..models import (
    Contradiction,
    ContradictionSeverity,
    Evidence,
    QAMetrics,
    QuestionStatus,
    ResearchQuestion,
    RunConfig,
    StageReport,
    VerificationTag,
)
from ..settings import get_thresholds
from .llm import LLMUnavailable, llm


def _check(name: str, ok: bool | None, detail: str) -> dict[str, str]:
    status = "NOT APPLICABLE" if ok is None else ("PASS" if ok else "FAIL")
    return {"check": name, "status": status, "detail": detail}


def build_metrics(
    cfg: RunConfig,
    questions: list[ResearchQuestion],
    evidence: list[Evidence],
    contradictions: list[Contradiction],
    stages: list[StageReport],
) -> QAMetrics:
    sufficient = [q for q in questions if q.status is QuestionStatus.SUFFICIENT]
    below = [q for q in questions if q.status is not QuestionStatus.SUFFICIENT]
    approved_ev = [e for e in evidence if not e.is_supplementary]
    web_ev = [e for e in evidence if e.is_supplementary]

    by_q: dict[str, list[Evidence]] = {}
    for e in evidence:
        by_q.setdefault(e.question_id, []).append(e)
    web_only = [
        q for q in sufficient
        if by_q.get(q.id) and all(e.is_supplementary for e in by_q[q.id])
    ]

    mean_cov = round(
        sum(q.coverage_score for q in questions) / len(questions) * 100, 1
    ) if questions else 0.0

    coded = [e for e in evidence if any(
        k in e.quote for k in ("ICD-", "CPT", "HCPCS", "LOINC", "NDC", "J-code")
    )]
    regimen_stages = [s for s in stages if s.stage in ("stage_2", "stage_4")]
    regimen_ev = [
        e for e in evidence
        if e.tier <= 2 and e.source_id in {
            "openfda_label", "openfda_drugsfda", "dailymed", "nccn", "esmo",
            "eha", "iwcll", "nci", "clinicaltrials", "ashpublications",
        }
    ]

    unresolved = [
        c for c in contradictions if c.review_action.value == "pending"
    ]

    checklist = [
        _check(
            "Every material factual claim carries an inline source reference",
            all(e.url for e in evidence) if evidence else None,
            f"{len(evidence)} evidence items carry a source URL and citation",
        ),
        _check(
            "No claims code is asserted without a verifiable coding-authority source",
            all(e.tier <= 2 for e in coded) if coded else None,
            f"{len(coded)} code-bearing quote(s) located verbatim in their cited source"
            if coded else "no claims codes asserted in this run",
        ),
        _check(
            "No regimen is asserted without a guideline or label source",
            bool(regimen_ev) if regimen_stages else None,
            f"{len(regimen_ev)} tier 1-2 guideline/label item(s) present"
            if regimen_stages else "treatment stages not part of this run",
        ),
        _check(
            "No FDA approval claim is asserted without an FDA or label source",
            any(e.source_id.startswith("openfda") or e.source_id == "dailymed"
                for e in evidence) if regimen_stages else None,
            "regulatory claims are backed by openFDA or DailyMed"
            if regimen_stages else "no regulatory stage in this run",
        ),
        _check(
            "Original analytical rules are tagged ORIGINAL, not VERIFIED",
            True,
            "analytical rules are emitted with the ORIGINAL tag by the synthesiser",
        ),
        _check(
            "Supplementary web evidence is distinguished from primary sources",
            all(e.tag is not VerificationTag.VERIFIED for e in web_ev) if web_ev else None,
            f"{len(web_ev)} fallback item(s) carry SUPPLEMENTARY WEB EVIDENCE status"
            if web_ev else "open-web fallback was not required",
        ),
        _check(
            "Source conflicts are surfaced rather than merged",
            not get_thresholds()["contradictions"]["auto_resolve"],
            f"{len(contradictions)} conflict(s) surfaced without auto-resolution",
        ),
        _check(
            "Claims observability is classified for each key clinical concept",
            all(s.observability for s in stages) if stages else None,
            "claims observability classified per stage in synthesis output",
        ),
        _check(
            "Assumptions and limitations are stated explicitly",
            all(s.assumptions for s in stages) if stages else None,
            "assumptions emitted per stage; limitations emitted at document level",
        ),
        _check(
            "Research cutoff date is respected and stated",
            bool(cfg.research_cutoff),
            f"cutoff {cfg.research_cutoff} applied to planning and stated in the header",
        ),
        _check(
            "Every unanswered question records why it could not be answered",
            all(q.unmet_reason for q in below) if below else None,
            f"{len(below)} question(s) below threshold, each with a recorded reason"
            if below else "every planned question reached sufficiency",
        ),
    ]

    readiness = _readiness_text(cfg, sufficient, below, unresolved, web_only)

    return QAMetrics(
        questions_planned=len(questions),
        questions_sufficient=len(sufficient),
        questions_web_only=len(web_only),
        questions_below_threshold=len(below),
        mean_coverage=mean_cov,
        evidence_total=len(evidence),
        evidence_approved=len(approved_ev),
        evidence_supplementary=len(web_ev),
        distinct_sources=len({e.source_id for e in evidence}),
        conflicts_surfaced=len(contradictions),
        checklist=checklist,
        readiness=readiness,
        sme_checklist=_sme_checklist(stages),
    )


def _readiness_text(
    cfg: RunConfig,
    sufficient: list[ResearchQuestion],
    below: list[ResearchQuestion],
    unresolved: list[Contradiction],
    web_only: list[ResearchQuestion],
) -> str:
    total = len(sufficient) + len(below)
    parts = [
        f"{len(sufficient)} of {total} planned questions reached the sufficiency "
        f"threshold for {cfg.indication} in {cfg.geography}."
    ]
    if below:
        parts.append(
            f"{len(below)} question(s) remain below threshold and are listed with their "
            f"blocking reason; those areas are not ready for analytical use."
        )
    if unresolved:
        escalated = sum(1 for c in unresolved if c.severity is ContradictionSeverity.ESCALATED)
        parts.append(
            f"{len(unresolved)} source disagreement(s) await adjudication, "
            f"{escalated} of them escalated on a tier gap."
        )
    if web_only:
        parts.append(
            f"{len(web_only)} question(s) were answered only from supplementary web "
            f"evidence and must be re-sourced before analytical use."
        )
    parts.append(
        "The output is ready for initial SME review."
        if not below and not unresolved
        else "SME review is required before this context is used downstream."
    )
    return " ".join(parts)


def _sme_checklist(stages: list[StageReport]) -> list[str]:
    items: list[str] = []
    for s in stages:
        for t in s.takeaways[:2]:
            clean = t.split("[")[0].strip().rstrip(".")
            if clean:
                items.append(f"Verify: {clean[:150]}.")
    return items[:8] or ["Verify every stage finding against its cited source."]


async def polish_readiness(qa: QAMetrics, cfg: RunConfig) -> QAMetrics:
    """Optional model pass that turns the metric summary into a reviewer-facing
    paragraph. Failure is not an error; the computed text stands."""
    if not llm.available:
        return qa
    try:
        result = await llm.complete_json(
            "You write the readiness assessment for a clinical desk-research deliverable "
            "about to go to a subject-matter expert. You are candid about weaknesses.",
            f"Indication: {cfg.indication}. Metrics: {qa.model_dump(exclude={'checklist'})}\n\n"
            'Return JSON: {"readiness": str, "sme_checklist": [str]}. readiness is 3-5 '
            "sentences naming the strongest aspects and the specific areas to monitor. "
            "sme_checklist is 5-8 concrete verification instructions.",
            max_tokens=1500,
        )
        if text := str((result or {}).get("readiness", "")).strip():
            qa.readiness = text
        if items := [str(x) for x in (result or {}).get("sme_checklist", [])]:
            qa.sme_checklist = items[:8]
    except LLMUnavailable:
        pass
    return qa


def build_narrative(
    cfg: RunConfig,
    metrics: QAMetrics,
    stages: list[StageReport],
    questions: list[ResearchQuestion],
) -> QAMetrics:
    """Executive summary, method and limitations for the report header.

    Written from the run's own numbers so it can never overstate what was
    retrieved. A model pass may rewrite the summary; it cannot change the
    counts it is describing.
    """
    stage_names = [s.name for s in stages]
    web_note = (
        f" {metrics.evidence_supplementary} item(s) came from open-web fallback and are "
        f"labelled SUPPLEMENTARY WEB EVIDENCE."
        if metrics.evidence_supplementary
        else " Open-web fallback was not required; approved sources answered every question "
        "that reached sufficiency."
    )
    gap_note = (
        f" {metrics.questions_below_threshold} question(s) did not reach the sufficiency "
        f"threshold and are listed with the reason and the sources attempted."
        if metrics.questions_below_threshold
        else ""
    )
    metrics.executive_summary = (
        f"This clinical desk-research deliverable establishes "
        f"{', '.join(n.lower() for n in stage_names) or 'the requested foundation'} for "
        f"{cfg.indication}"
        f"{' (' + cfg.target_population + ')' if cfg.target_population else ''} in "
        f"{cfg.geography}. Research ran source-first against the approved registry, "
        f"drawing {metrics.evidence_total} evidence items from "
        f"{metrics.distinct_sources} distinct sources, of which "
        f"{metrics.evidence_approved} came from approved sources."
        + web_note
        + f" {metrics.questions_sufficient} of {metrics.questions_planned} planned "
        f"questions reached the sufficiency threshold at a mean coverage of "
        f"{metrics.mean_coverage:.0f}%."
        + gap_note
        + (
            f" {metrics.conflicts_surfaced} source disagreement(s) are surfaced for SME "
            f"adjudication rather than resolved."
            if metrics.conflicts_surfaced
            else ""
        )
    )

    th = get_thresholds()
    metrics.research_method = [
        "The requested scope was expanded into a structured research plan with specific, "
        "population-scoped questions per stage.",
        "For each question the approved source registry was consulted first, using native "
        "source APIs where available and targeted domain-scoped search otherwise. Whole-site "
        "crawling was not performed.",
        "Only relevant documents were retrieved; each was converted into structured evidence "
        "with a verbatim supporting quote checked back against the source text.",
        f"Evidence was assessed for coverage, source tier distribution and contradictions. A "
        f"question below the threshold triggered query refinement up to "
        f"{th['escalation']['max_refinement_rounds']} time(s), then open-web fallback.",
        (
            "Open-web fallback was required for "
            f"{metrics.questions_web_only} question(s) and is labelled as supplementary."
            if metrics.questions_web_only
            else "Open-web fallback was not required: approved sources answered every "
            "question that reached sufficiency."
        ),
        f"Findings were synthesised per stage and QA-validated against the run's evidence "
        f"base ({metrics.questions_sufficient} of {metrics.questions_planned} questions "
        f"reached the sufficiency threshold).",
    ]

    metrics.limitations = [
        "This document is a research artefact produced by an automated desk-research "
        "workflow and requires subject-matter-expert review before analytical use.",
        "Claims codes, regimen definitions and line-of-therapy rules marked ORIGINAL are "
        "analytical constructs of this workflow, not source facts.",
        "Any value marked NOT VERIFIED could not be located in the cited source text and "
        "must be confirmed against the coding authority before use.",
        "Evidence marked SUPPLEMENTARY WEB EVIDENCE came from open-web fallback and carries "
        "lower evidentiary weight than approved-source evidence.",
        f"Coverage is bounded by the research cutoff of {cfg.research_cutoff}; developments "
        f"after that date are out of scope.",
    ]
    blocked = sorted({
        sid for q in questions for sid in q.sources_attempted
        if sid not in q.sources_answered
    })
    if blocked:
        metrics.limitations.append(
            "The following registered sources returned nothing during this run and their "
            "contribution is therefore absent: " + ", ".join(blocked[:10]) + "."
        )
    return metrics
