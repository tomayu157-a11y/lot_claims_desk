"""Detects disagreements between sources answering the same question.

Nothing here resolves a conflict. Both sides are recorded as their source
stated them and handed to a human. That is a deliberate product rule: an
automated merge of two clinical claims destroys the very signal an SME needs.
"""
from __future__ import annotations

import itertools
import logging
import re

from ..models import Contradiction, ContradictionSeverity, Evidence, ResearchQuestion
from ..settings import get_thresholds
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.contradictions")

_SYSTEM = (
    "You compare two verbatim claims from different sources answering the same clinical "
    "research question. You decide whether they genuinely disagree about the same fact, "
    "or merely describe different facts. You never decide which is correct."
)

# A number with an optional unit that matters in this domain.
_TIER_GAP_REASON = (
    "Sources sit at materially different evidence tiers; the lower-tier source must not "
    "override the higher-tier source."
)
_SAME_CONCEPT_REASON = (
    "Sources make differing assertions about the same concept; requires SME adjudication."
)

_NUMBER = re.compile(r"\b(\d[\d,]*\.?\d*)\s*(%|percent|per\s+100,?000|per\s+year)?")

# What kind of quantity is being stated. Two numbers are comparable only when
# they are the same kind: an incidence rate against another incidence rate, not
# against a gene-usage frequency that happens to share a unit.
_MEASURE_KINDS = {
    "incidence": "incidence", "rate of new": "incidence", "new cases": "incidence",
    "prevalence": "prevalence", "living with": "prevalence",
    "survival": "survival", "surviving": "survival",
    "mortality": "mortality", "deaths": "mortality", "died": "mortality",
    "death rate": "mortality",
    "diagnosed": "cases", "estimated new": "cases", "cases": "cases",
    "median": "median", "average": "mean", "mean": "mean",
    "frequency": "frequency", "usage": "frequency",
    "proportion": "proportion", "share": "proportion",
    "response": "response", "remission": "remission",
    "risk": "risk", "odds": "risk", "hazard": "risk",
}


def _measurements(text: str) -> list[tuple[float, str, frozenset[str]]]:
    """Quantities in a quote, each with its unit and the kind of thing measured.

    The kind word can sit on either side of the number ("the rate was 4.7 per
    100,000", "about 6,250 new cases"), so the window spans both. Bare
    four-digit years are skipped: a sentence naming 2025 and another naming
    2026 is not a disagreement about a quantity.
    """
    out: list[tuple[float, str, frozenset[str]]] = []
    for match in _NUMBER.finditer(text):
        raw, unit = match.group(1), (match.group(2) or "").lower().strip()
        try:
            number = float(raw.replace(",", ""))
        except ValueError:
            continue
        if not unit and "," not in raw and 1900 <= number <= 2100 and number.is_integer():
            continue
        window = text[max(0, match.start() - 120): match.end() + 60].lower()
        kinds = frozenset(kind for word, kind in _MEASURE_KINDS.items() if word in window)
        if kinds:
            out.append((number, unit, kinds))
    return out


def _numeric_conflict(a: str, b: str, delta_pct: float) -> bool:
    """True only when both quotes state the same kind of quantity in the same
    unit and disagree beyond the tolerance.

    The caller has already established that the two quotes address the same
    question and share a subject, so this decides only whether the numbers are
    comparable. It is deliberately conservative: a missed conflict is
    recoverable by a reviewer reading the evidence, while a fabricated one
    spends their attention and devalues the register real conflicts sit in.
    """
    for va, ua, ka in _measurements(a):
        for vb, ub, kb in _measurements(b):
            if ua != ub or not (ka & kb):
                continue
            if va == 0 and vb == 0:
                continue
            if abs(va - vb) / max(abs(va), abs(vb)) * 100 >= delta_pct:
                return True
    return False


def _shared_subject(a: str, b: str) -> bool:
    wa = {w for w in re.findall(r"[a-z]{5,}", a.lower())}
    wb = {w for w in re.findall(r"[a-z]{5,}", b.lower())}
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.28


def _severity(tier_a: int, tier_b: int, numeric: bool) -> ContradictionSeverity:
    cfg = get_thresholds()["contradictions"]
    if abs(tier_a - tier_b) >= cfg["escalate_on_tier_gap"]:
        return ContradictionSeverity.ESCALATED
    return ContradictionSeverity.NOTED if not numeric else ContradictionSeverity.NOTED


def _reason(tier_a: int, tier_b: int) -> str:
    cfg = get_thresholds()["contradictions"]
    if abs(tier_a - tier_b) >= cfg["escalate_on_tier_gap"]:
        return _TIER_GAP_REASON
    return _SAME_CONCEPT_REASON


def dedupe(items: list[Contradiction]) -> list[Contradiction]:
    """Collapse repeats of the same claim pair.

    A single disagreement between two sources surfaces on every question both
    sources answered. Reviewers need one decision per disagreement, not one per
    question, so the pair of claims is the identity.
    """
    out: list[Contradiction] = []
    seen: set[tuple[str, str]] = set()
    for c in sorted(items, key=lambda x: (x.severity is not ContradictionSeverity.ESCALATED,)):
        key = tuple(sorted((c.source_a_claim[:90].lower(), c.source_b_claim[:90].lower())))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def detect_deterministic(
    question: ResearchQuestion, evidence: list[Evidence]
) -> list[Contradiction]:
    cfg = get_thresholds()["contradictions"]
    found: list[Contradiction] = []
    seen: set[tuple[str, str]] = set()

    for a, b in itertools.combinations(evidence, 2):
        if a.source_id == b.source_id:
            continue
        pair = tuple(sorted((a.id, b.id)))
        if pair in seen:
            continue
        numeric = _numeric_conflict(a.quote, b.quote, cfg["escalate_on_numeric_delta_pct"])
        tier_gap = abs(a.tier - b.tier) >= cfg["escalate_on_tier_gap"]
        if not (numeric or (tier_gap and _shared_subject(a.quote, b.quote))):
            continue
        seen.add(pair)
        hi, lo = (a, b) if a.tier <= b.tier else (b, a)
        found.append(
            Contradiction(
                run_id=question.run_id,
                stage=question.stage,
                question_id=question.id,
                topic=question.seed_text.strip(" ?").lower() or question.text[:90],
                source_a_name=f"{hi.citation} (tier {hi.tier})",
                source_a_tier=hi.tier,
                source_a_claim=hi.quote,
                source_a_url=hi.url,
                source_b_name=f"{lo.citation} (tier {lo.tier})",
                source_b_tier=lo.tier,
                source_b_claim=lo.quote,
                source_b_url=lo.url,
                reason=_reason(hi.tier, lo.tier),
                severity=_severity(hi.tier, lo.tier, numeric),
            )
        )
    return found


async def detect(
    question: ResearchQuestion, evidence: list[Evidence]
) -> list[Contradiction]:
    candidates = detect_deterministic(question, evidence)
    if not candidates or not llm.available:
        return candidates

    # The model's only job is to discard false positives and write a better
    # reason. It can never mark a real conflict resolved.
    try:
        payload = [
            {
                "id": c.id,
                "topic": c.topic,
                "a": {"source": c.source_a_name, "claim": c.source_a_claim},
                "b": {"source": c.source_b_name, "claim": c.source_b_claim},
            }
            for c in candidates
        ]
        verdicts = await llm.complete_json(
            _SYSTEM,
            f"Question: {question.text}\n\nCandidate disagreements:\n{payload}\n\n"
            "Return JSON: [{\"id\": str, \"is_real_disagreement\": bool, "
            "\"reason\": str}]. is_real_disagreement is false when the two claims "
            "describe different facts, different years, or different populations "
            "rather than contradicting each other. reason explains the divergence in "
            "one sentence. Never state which source is correct.",
            max_tokens=2000,
        )
        by_id = {str(v.get("id")): v for v in (verdicts or [])}
        kept: list[Contradiction] = []
        for c in candidates:
            v = by_id.get(c.id)
            if v is None:
                kept.append(c)
                continue
            if not v.get("is_real_disagreement", True):
                continue
            if reason := str(v.get("reason") or "").strip():
                c.reason = reason
            kept.append(c)
        return kept
    except LLMUnavailable:
        return candidates
