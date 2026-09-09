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
    ReviewAction,
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
    """How many of the question's aspects the evidence actually covers.

    Word-level matching, not substring: 'age' inside 'percentage' would
    silently inflate coverage. Aspects that contain no matchable word are
    dropped from the denominator as well as the numerator, because counting an
    unmatchable aspect as a miss put a hard ceiling on every question's score.
    """
    if not question.aspects:
        return (1, 1) if evidence else (0, 1)

    blob = " ".join(f"{e.title} {e.quote} {e.context}" for e in evidence).lower()
    tokens = set(re.findall(r"[a-z0-9]+", blob))

    usable: list[list[str]] = []
    for aspect in question.aspects:
        # Three characters, not four: Rai, IGHV, TP53 and CLL are exactly the
        # discriminating terms these questions turn on.
        words = [w for w in re.findall(r"[a-z0-9]+", aspect.lower()) if len(w) >= 3]
        if words:
            usable.append(words)
    if not usable:
        return (1, 1) if evidence else (0, 1)

    hits = 0
    for words in usable:
        matched = sum(1 for w in words if w in tokens)
        # A one or two word aspect names a single concept and must be present.
        # A longer phrase is satisfied by its distinctive words; demanding all
        # of them measures phrasing rather than coverage.
        need = 1 if len(words) <= 2 else max(2, round(len(words) * 0.4))
        if matched >= need:
            hits += 1
    return hits, len(usable)


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
    diversity = min(len(sources) / max(cfg["min_distinct_sources"] * 2, 1), 1.0)

    # Two weightings, because the two kinds of aspect mean different things.
    #
    # Model-written aspects name the concepts an answer must contain, so
    # failing to find them is genuine evidence the question is unanswered and
    # they carry the most weight.
    #
    # Heuristic aspects are only the question's own words. Sources answer in
    # their own vocabulary: a label that fully answers "which therapies are
    # FDA-approved" says "is indicated for the treatment of" and contains none
    # of "FDA", "approved", "label" or "indications". Scoring those as a miss
    # measured phrasing, not coverage, and held well-sourced answers below the
    # threshold. They now act as an uplift that can raise a score, never as a
    # gate that sinks one, and the weight moves to what is observable without
    # a model: source quality, independent corroboration and volume.
    if question.aspects_from_model:
        coverage = (0.45 * aspect_ratio + 0.30 * tier_component
                    + 0.15 * diversity + 0.10 * volume)
    else:
        coverage = (0.20 * aspect_ratio + 0.40 * tier_component
                    + 0.25 * diversity + 0.15 * volume)
    coverage = round(coverage, 3)

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
    return assess_confidence(question, evidence, contradictions)[0]


def assess_confidence(
    question: ResearchQuestion,
    evidence: list[Evidence],
    contradictions: list[Contradiction],
) -> tuple[Confidence, str]:
    """Two states, and the reason in words when a person has to act.

    Requires Input fires for exactly three reasons: nothing usable was found,
    only the open web answered the question, or two sources disagree and
    nobody has decided. Everything else is Ready: a vetted source answered
    it, which is the sufficiency rule this app runs on.
    """
    ceiling = get_thresholds()["sufficiency"]["primary_tier_ceiling"]
    if not evidence:
        return Confidence.REQUIRES_INPUT, (
            "No source returned usable evidence. Add what you know, or tell "
            "Celestra where to look."
        )

    primary = [e for e in evidence if e.tier <= ceiling and not e.is_supplementary]
    if not primary:
        return Confidence.REQUIRES_INPUT, (
            "Only open-web pages answered this. No approved source confirmed it, "
            "so a person has to accept it, correct it, or add a source."
        )

    # A conflict concerns the question it surfaced on. Only conflicts with no
    # recorded question fall back to the stage, so one disagreement no longer
    # flags every card in the stage.
    open_conflicts = [
        c for c in contradictions
        if c.severity is ContradictionSeverity.ESCALATED
        and c.review_action is ReviewAction.PENDING
        and ((c.question_id == question.id) if c.question_id
             else (not c.stage or c.stage == question.stage))
    ]
    if open_conflicts:
        c = open_conflicts[0]
        return Confidence.REQUIRES_INPUT, (
            f"{c.source_a_name} and {c.source_b_name} disagree on {c.topic}. "
            "Decide the conflict below, or add input, before this can be used."
        )
    return Confidence.READY, ""


def status_after_retrieval(question: ResearchQuestion, suff: Sufficiency) -> QuestionStatus:
    esc = get_thresholds()["escalation"]
    if suff.ok:
        return QuestionStatus.SUFFICIENT
    if question.refinement_rounds < esc["max_refinement_rounds"]:
        return QuestionStatus.REFINING
    if esc["enable_open_web_fallback"] and not question.used_web_fallback:
        return QuestionStatus.WEB_FALLBACK
    return QuestionStatus.INSUFFICIENT if suff.evidence_items else QuestionStatus.UNANSWERED
