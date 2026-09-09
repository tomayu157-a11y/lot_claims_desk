"""Sufficiency, coverage and confidence.

Every threshold lives in config/thresholds.yaml. Nothing here hardcodes a
number, so tuning the strictness of a run is a config change, not a code
change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import (
    Confidence,
    Contradiction,
    ContradictionSeverity,
    Evidence,
    QuestionStatus,
    ResearchQuestion,
)
from ..settings import get_source_registry, get_thresholds


def tier_weight(tier: int) -> float:
    tiers = get_source_registry()["tiers"]
    spec = tiers.get(tier) or tiers.get(str(tier)) or {}
    return float(spec.get("weight", 0.2))


@dataclass
class Sufficiency:
    ok: bool
    coverage: float
    reason: str = ""
    evidence_items: int = 0
    distinct_sources: int = 0
    primary_items: int = 0
    aspects_covered: int = 0
    aspects_total: int = 0


def _aspect_hits(question: ResearchQuestion, evidence: list[Evidence]) -> tuple[int, int]:
    """How many of the question's aspects appear in at least one quote.

    Word-level matching, not substring: matching 'age' inside 'percentage'
    would silently inflate coverage.
    """
    if not question.aspects:
        return (1, 1) if evidence else (0, 1)
    blob = " ".join(f"{e.title} {e.quote} {e.context}" for e in evidence).lower()
    tokens = set(re.findall(r"[a-z0-9]+", blob))
    hits = 0
    for aspect in question.aspects:
        words = [w for w in re.findall(r"[a-z0-9]+", aspect.lower()) if len(w) > 3]
        if not words:
            continue
        if sum(1 for w in words if w in tokens) / len(words) >= 0.5:
            hits += 1
    return hits, len(question.aspects)


def assess(question: ResearchQuestion, evidence: list[Evidence]) -> Sufficiency:
    cfg = get_thresholds()["sufficiency"]
    usable = [e for e in evidence if len(e.quote.strip()) >= cfg["min_quote_length"]]
    sources = {e.source_id for e in usable}
    primary = [e for e in usable if e.tier <= cfg["primary_tier_ceiling"]]
    hits, total = _aspect_hits(question, usable)

    aspect_ratio = hits / total if total else 0.0
    tier_component = 0.0
    if usable:
        tier_component = sum(tier_weight(e.tier) for e in usable) / len(usable)
    volume = min(len(usable) / max(cfg["min_evidence_items"], 1), 1.0)
    coverage = round(0.45 * aspect_ratio + 0.35 * tier_component + 0.20 * volume, 3)

    checks = [
        (len(usable) >= cfg["min_evidence_items"],
         f"only {len(usable)} usable evidence item(s), need {cfg['min_evidence_items']}"),
        (len(sources) >= cfg["min_distinct_sources"],
         f"only {len(sources)} distinct source(s), need {cfg['min_distinct_sources']}"),
        (len(primary) >= cfg["min_primary_tier_items"],
         f"no tier {cfg['primary_tier_ceiling']} or better source"),
        (coverage >= cfg["min_coverage_score"],
         f"coverage {coverage:.2f} below {cfg['min_coverage_score']:.2f}"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    return Sufficiency(
        ok=not failed,
        coverage=coverage,
        reason="; ".join(failed),
        evidence_items=len(usable),
        distinct_sources=len(sources),
        primary_items=len(primary),
        aspects_covered=hits,
        aspects_total=total,
    )


def confidence_for(
    question: ResearchQuestion,
    evidence: list[Evidence],
    contradictions: list[Contradiction],
) -> Confidence:
    """Confidence is a property of the evidence, not of the writing."""
    conf = get_thresholds()["confidence"]
    if not evidence:
        return Confidence.REJECTED

    s = assess(question, evidence)
    open_escalated = sum(
        1 for c in contradictions
        if c.severity is ContradictionSeverity.ESCALATED
        and c.review_action.value == "pending"
    )
    approved = [e for e in evidence if not e.is_supplementary]
    if not approved:
        # Answered only from open-web fallback. Never presented as high confidence.
        return Confidence.REQUIRES_INPUT

    primary = sum(1 for e in evidence if e.tier <= 2)
    for level, enum_value in (("high", Confidence.HIGH), ("medium", Confidence.MEDIUM)):
        spec = conf[level]
        if (
            s.coverage >= spec["min_coverage_score"]
            and s.evidence_items >= spec["min_evidence_items"]
            and primary >= spec["min_primary_tier_items"]
            and open_escalated <= spec["max_open_contradictions"]
        ):
            return enum_value
    return Confidence.REQUIRES_INPUT


def status_after_retrieval(question: ResearchQuestion, suff: Sufficiency) -> QuestionStatus:
    esc = get_thresholds()["escalation"]
    if suff.ok:
        return QuestionStatus.SUFFICIENT
    if question.refinement_rounds < esc["max_refinement_rounds"]:
        return QuestionStatus.REFINING
    if esc["enable_open_web_fallback"] and not question.used_web_fallback:
        return QuestionStatus.WEB_FALLBACK
    return QuestionStatus.INSUFFICIENT if suff.evidence_items else QuestionStatus.UNANSWERED
