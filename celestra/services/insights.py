"""Insight cards, generated from the finished stage document.

A card is what a reviewer reads instead of the whole document. It is written
by the model from the stage report itself: the answers to the questions the
agent asked, the tables it built, and the synthesis it wrote, judged against
the outputs the framework expects from that agent. Each card names the
tables it draws on, the questions it rests on, and whether a person has to
act on it.

The model classifies; the evidence has the final word. A card the model calls
Ready is still Requires Input when the questions under it were answered only
from the open web, or not at all, or carry an undecided escalated conflict.

Without a model, or when the call fails, the deterministic path in the
orchestrator produces one card per question from its answer, so a run never
ends up with no cards.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..models import (
    AnswerStatus,
    Confidence,
    Contradiction,
    Evidence,
    Insight,
    ResearchQuestion,
    RunConfig,
    StageReport,
    VerificationTag,
)
from ..settings import get_framework, get_questions
from .llm import LLMUnavailable, llm
from .scoring import assess_confidence

log = logging.getLogger("celestra.insights")

MIN_CARDS = 3
MAX_CARDS = 8

_SYSTEM = (
    "You turn one stage of a clinical desk-research document into review cards for a "
    "subject-matter expert. Every card must be traceable to the document you are given: "
    "you never add a fact, figure, drug, code or guideline that the document does not "
    "state. You write for a reviewer who will approve, revise or add input to each card."
)


def _table_text(table, max_rows: int = 6) -> str:
    rows = [" | ".join(str(c)[:80] for c in r) for r in table.rows[:max_rows]]
    more = f"\n  ... {len(table.rows) - max_rows} more rows" if len(table.rows) > max_rows else ""
    return (f"TABLE: {table.title}\n  columns: {' | '.join(table.columns)}\n  "
            + "\n  ".join(rows) + more)


def _document_text(report: StageReport, questions: list[ResearchQuestion]) -> str:
    parts: list[str] = []
    if report.synthesis:
        parts.append("SYNTHESIS:\n" + report.synthesis[:2500])
    for n in report.narratives[:6]:
        parts.append(f"{n.get('heading', 'Section')}:\n{str(n.get('body', ''))[:1200]}")
    parts.append("QUESTIONS AND ANSWERS:")
    for i, q in enumerate(questions, start=1):
        answer = q.answer_text or f"(not answered: {q.unmet_reason or 'no source addressed it'})"
        cites = ", ".join(q.answer_citations[:4]) or "no citation"
        parts.append(f"Q{i}. {q.seed_text or q.text}\n   status={q.answer_status.value}; "
                     f"sources: {cites}\n   {answer[:900]}")
    for t in report.tables[:8]:
        parts.append(_table_text(t))
    if report.takeaways:
        parts.append("TAKEAWAYS:\n- " + "\n- ".join(str(t)[:300] for t in report.takeaways[:6]))
    if report.assumptions:
        parts.append("ASSUMPTIONS:\n- " + "\n- ".join(str(a)[:200] for a in report.assumptions[:6]))
    if report.unanswered:
        parts.append("UNANSWERED:\n- " + "\n- ".join(
            f"{u.get('question', '')[:160]} ({u.get('reason', '')[:120]})"
            for u in report.unanswered[:6]))
    return "\n\n".join(parts)


def _expected_outputs(stage: str, bucket: str) -> list[str]:
    meta = get_questions()["stage_meta"].get(stage, {})
    spec = get_framework()["buckets"].get(bucket, {})
    out = [str(x) for x in (meta.get("expected_output") or [])]
    if spec.get("output"):
        out.append(f"Deliverable: {spec['output']}")
    return out


def _match_title(candidate: str, titles: list[str]) -> str | None:
    c = re.sub(r"\W+", " ", candidate.lower()).strip()
    if not c:
        return None
    for t in titles:
        if re.sub(r"\W+", " ", t.lower()).strip() == c:
            return t
    for t in titles:
        tl = re.sub(r"\W+", " ", t.lower()).strip()
        if c in tl or tl in c:
            return t
    return None


def _clean_indices(raw: Any, count: int) -> list[int]:
    out: list[int] = []
    for v in (raw if isinstance(raw, list) else [raw]):
        try:
            n = int(str(v).strip().lstrip("Qq"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= count and n not in out:
            out.append(n)
    return out


async def generate(
    run_id: str, cfg: RunConfig, stage: str, bucket: str, report: StageReport,
    questions: list[ResearchQuestion], evidence: list[Evidence],
    contradictions: list[Contradiction], category: str,
) -> list[Insight]:
    """Cards for one stage, written by the model from the stage document.
    Returns [] when no model is configured or the call fails, so the caller
    can fall back to one card per question."""
    if not llm.available or not questions:
        return []

    expected = _expected_outputs(stage, bucket)
    fw = get_framework()["buckets"][bucket]
    meta = get_questions()["stage_meta"].get(stage, {})
    table_titles = [t.title for t in report.tables]
    prompt = (
        f"Indication: {cfg.indication}. Geography: {cfg.geography}. "
        f"Objective: {cfg.objective}.\n"
        f"Agent: {fw['agent_name']} — {fw['name']}.\n"
        f"Stage: {meta.get('name', stage)}. Core question: {meta.get('core_question', '')}\n"
        "Outputs this agent is expected to deliver:\n- " + "\n- ".join(expected) + "\n\n"
        "THE DOCUMENT FOR THIS STAGE:\n" + _document_text(report, questions) + "\n\n"
        f"Write between {MIN_CARDS} and {MAX_CARDS} review cards that together cover the "
        "important findings of this stage, judged against the expected outputs above. "
        "Every table in the document must be referenced by at least one card. Prefer "
        "cards that state a specific, decision-relevant fact (a rate, a criterion, a "
        "code, an approval, a rule) over generic summaries. A card may rest on more "
        "than one question when the document connects them.\n\n"
        'Return JSON: {"cards": [{"title": str, "finding": str, "detail": str, '
        '"table_titles": [str], "questions": [int], "status": "ready"|"requires_input", '
        '"input_reason": str}]}.\n'
        "- title: a short heading, at most 10 words, like a table heading.\n"
        "- finding: 2-3 sentences, the finding itself, with the figure or rule stated. "
        "Only what the document says.\n"
        "- detail: 1-2 sentences of supporting specifics or caveats from the document; "
        "empty string if none.\n"
        "- table_titles: exact titles from the document's TABLE lines this card draws on.\n"
        "- questions: the Q numbers this card rests on.\n"
        "- status: requires_input when the document itself says the point is unanswered, "
        "assumed, conflicting between sources, or answered only from the open web; "
        "otherwise ready.\n"
        "- input_reason: when requires_input, one sentence saying what a reviewer must "
        "supply or decide; empty string otherwise."
    )
    try:
        result = await llm.complete_json(_SYSTEM, prompt, max_tokens=3500)
    except LLMUnavailable:
        return []
    except Exception:  # noqa: BLE001 - the deterministic path covers the failure
        log.exception("insight generation failed for %s/%s", run_id, stage)
        return []

    raw_cards = (result or {}).get("cards") if isinstance(result, dict) else None
    if not isinstance(raw_cards, list) or not raw_cards:
        log.warning("insight generation returned no cards for %s/%s", run_id, stage)
        return []

    ev_by_q: dict[str, list[Evidence]] = {}
    for e in evidence:
        ev_by_q.setdefault(e.question_id, []).append(e)

    out: list[Insight] = []
    seen_titles: set[str] = set()
    for card in raw_cards[:MAX_CARDS]:
        if not isinstance(card, dict):
            continue
        title = re.sub(r"\s+", " ", str(card.get("title") or "")).strip()[:90]
        finding = re.sub(r"\s+", " ", str(card.get("finding") or "")).strip()
        if not title or not finding or title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())

        q_idx = _clean_indices(card.get("questions"), len(questions))
        linked = [questions[i - 1] for i in q_idx] or list(questions[:1])
        titles: list[str] = []
        for t in (card.get("table_titles") or []):
            m = _match_title(str(t), table_titles)
            if m and m not in titles:
                titles.append(m)
        if not titles:
            # Tables record which questions fed them; use that join.
            wanted = {q.id for q in linked}
            titles = [t.title for t in report.tables if set(t.question_ids) & wanted]

        own_evidence: list[Evidence] = []
        for q in linked:
            own_evidence += ev_by_q.get(q.id, [])
        own_evidence.sort(key=lambda e: (e.tier, -e.relevance))
        source_ids: list[str] = []
        for e in own_evidence:
            if e.source_id not in source_ids:
                source_ids.append(e.source_id)

        # The evidence rule is the floor: whichever of the two says a person
        # must act, wins. The reason shown is the one that fired first.
        conf, reason = Confidence.READY, ""
        for q in linked:
            c, r = assess_confidence(q, ev_by_q.get(q.id, []), contradictions)
            if c is Confidence.REQUIRES_INPUT:
                conf, reason = c, r
                break
        model_says = str(card.get("status") or "").strip().lower() == "requires_input"
        model_reason = re.sub(r"\s+", " ", str(card.get("input_reason") or "")).strip()[:300]
        if conf is Confidence.READY and model_says:
            conf = Confidence.REQUIRES_INPUT
            reason = model_reason or "The document marks this point as unresolved."
        elif conf is Confidence.REQUIRES_INPUT and model_reason and model_reason not in reason:
            reason = f"{reason} {model_reason}"[:400]

        answered_all = all(q.answer_status is AnswerStatus.ANSWERED for q in linked)
        out.append(Insight(
            run_id=run_id, stage=stage, bucket=bucket, category=category,
            title=title, summary=finding[:600],
            detail=re.sub(r"\s+", " ", str(card.get("detail") or "")).strip()[:500],
            confidence=conf, input_reason=reason,
            tag=VerificationTag.VERIFIED if answered_all and own_evidence
            else VerificationTag.INFERENCE,
            evidence_ids=[e.id for e in own_evidence],
            source_ids=source_ids,
            question_ids=[q.id for q in linked],
            used_web_fallback=any(q.used_web_fallback for q in linked),
            table_titles=titles,
        ))
    if len(out) < MIN_CARDS:
        log.warning("insight generation produced %d card(s) for %s/%s; using per-question "
                    "fallback", len(out), run_id, stage)
        return []
    log.info("stage %s: %d insight cards from the document", stage, len(out))
    return out
