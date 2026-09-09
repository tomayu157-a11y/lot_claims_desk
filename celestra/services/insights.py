"""Insight cards: fixed slots defined by each agent's objective, filled from
the agent's finished stage document.

The slots live in config/insight_cards.yaml. For each one the model writes:
what we found, the card's own evidence block (figures, a table, a pathway or
a list), what it means, and one quiet line saying what a reviewer should
check. Without a model, the slot is filled from the research questions that
map to it: their answers, their evidence and the stage tables.

The model never decides on its own that a card needs a person. The evidence
rule does: a card resting on a question answered only from the open web, or
not at all, or carrying an undecided escalated conflict, is Requires Input.
A slot the sources did not cover is Requires Input too, because only a
reviewer can supply it.
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
from ..settings import get_framework, get_insight_cards, get_questions
from .llm import LLMUnavailable, llm
from .scoring import assess_confidence

log = logging.getLogger("celestra.insights")

CATEGORY_BY_BUCKET = {
    "A": "Clinical", "C": "Treatment", "B": "Diagnostic",
    "D": "Logic", "E": "Journey", "F": "Synthesis", "G": "Validation",
}

_SYSTEM = (
    "You fill fixed review cards for a clinical desk-research document. Each card has "
    "a defined objective and an evidence format. You write only what the supplied "
    "document states: never a fact, figure, drug, code or guideline it does not contain. "
    "Where the document does not cover a card, you say so instead of writing around it. "
    "Your reader is a subject-matter expert who will approve, edit or add to each card."
)


# -- catalogue ---------------------------------------------------------------
def catalogue_for(bucket: str) -> list[dict]:
    return [dict(c) for c in (get_insight_cards().get("buckets") or {}).get(bucket, [])]


def phase_catalogue(phase: str) -> list[dict]:
    return [dict(c) for c in (get_insight_cards().get("phase_cards") or {}).get(phase, [])]


def _hits(text: str, hints: list[str]) -> bool:
    low = (text or "").lower()
    return any(h.lower() in low for h in hints or [])


def _questions_for(card: dict, questions: list[ResearchQuestion]) -> list[ResearchQuestion]:
    return [q for q in questions if _hits(f"{q.seed_text} {q.text}", card.get("question_hints"))]


def _tables_for(card: dict, report: StageReport):
    return [t for t in report.tables if _hits(t.title, card.get("table_hints"))]


# -- document text for the model -------------------------------------------
def _table_text(table, max_rows: int = 8) -> str:
    rows = [" | ".join(str(c)[:90] for c in r) for r in table.rows[:max_rows]]
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
                     f"sources: {cites}\n   {answer[:1000]}")
    for t in report.tables[:8]:
        parts.append(_table_text(t))
    if report.takeaways:
        parts.append("TAKEAWAYS:\n- " + "\n- ".join(str(t)[:300] for t in report.takeaways[:6]))
    if report.assumptions:
        parts.append("ASSUMPTIONS:\n- " + "\n- ".join(str(a)[:200] for a in report.assumptions[:6]))
    return "\n\n".join(parts)


def _card_spec_text(card: dict) -> str:
    et = card.get("evidence_type", "list")
    if et == "metrics":
        shape = ('"evidence": [{"label": str, "value": str}] using labels such as '
                 + "; ".join(card.get("metrics") or []) + " (only those the document states)")
    elif et == "table":
        shape = ('"evidence": {"columns": ' + str(card.get("columns") or ["Item", "Detail"])
                 + ', "rows": [[str, ...]]} with 3-8 rows')
    elif et == "steps":
        shape = '"evidence": [str] — 4-8 ordered steps, each at most 6 words'
    else:
        shape = '"evidence": [str] — 3-6 bullet points, each one sentence'
    return (f'- key "{card["key"]}": {card["title"]}. Objective: {card.get("objective", "")}. '
            f"Evidence format: {shape}.")


# -- helpers -----------------------------------------------------------------
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


def _clean_text(v: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def _clean_evidence(evidence_type: str, raw: Any) -> Any:
    """Coerce whatever the model returned into the card's evidence shape.
    Anything that does not fit is dropped, never guessed."""
    if evidence_type == "metrics":
        out = []
        for item in (raw if isinstance(raw, list) else []):
            if isinstance(item, dict) and item.get("label") and item.get("value") not in (None, ""):
                out.append({"label": _clean_text(item["label"], 60),
                            "value": _clean_text(item["value"], 60)})
        return out[:6]
    if evidence_type == "table":
        if not isinstance(raw, dict):
            return None
        cols = [_clean_text(c, 40) for c in (raw.get("columns") or []) if _clean_text(c, 40)]
        rows = []
        for r in (raw.get("rows") or []):
            if isinstance(r, list) and any(_clean_text(c, 120) for c in r):
                cells = [_clean_text(c, 120) for c in r][: len(cols) or None]
                cells += [""] * (len(cols) - len(cells))
                rows.append(cells)
        return {"columns": cols, "rows": rows[:10]} if cols and rows else None
    items = [_clean_text(x, 200) for x in (raw if isinstance(raw, list) else []) if _clean_text(x, 200)]
    return items[:8] or None


def _evidence_from_report(card: dict, report: StageReport, linked: list[ResearchQuestion],
                          own_evidence: list[Evidence],
                          used_tables: set[str] | None = None,
                          used_quotes: set[str] | None = None) -> tuple[str, Any]:
    """Deterministic evidence block: a matching stage table not already shown
    on another card, otherwise quotes not already shown on another card. The
    same table under three cards reads as padding and hides how much distinct
    evidence there is."""
    used_tables = used_tables if used_tables is not None else set()
    used_quotes = used_quotes if used_quotes is not None else set()
    tables = _tables_for(card, report) + [
        t for t in report.tables if set(t.question_ids) & {q.id for q in linked}
    ]
    for t in tables:
        if t.title in used_tables:
            continue
        used_tables.add(t.title)
        return "table", {"columns": list(t.columns), "rows": [list(r) for r in t.rows[:5]]}
    quotes = []
    for e in own_evidence:
        q = _clean_text(e.quote, 200)
        key = q[:120].lower()
        if q and key not in used_quotes:
            used_quotes.add(key)
            quotes.append(q)
        if len(quotes) >= 4:
            break
    return ("list", quotes) if quotes else ("", None)


def _finish(card: dict, *, run_id: str, stage: str, bucket: str, category: str,
            finding: str, evidence_type: str, evidence: Any, interpretation: str,
            review_note: str, linked: list[ResearchQuestion], ev_by_q: dict,
            contradictions: list[Contradiction], titles: list[str], covered: bool,
            gap: str = "") -> Insight:
    own: list[Evidence] = []
    for q in linked:
        own += ev_by_q.get(q.id, [])
    own.sort(key=lambda e: (e.tier, -e.relevance))
    source_ids: list[str] = []
    for e in own:
        if e.source_id not in source_ids:
            source_ids.append(e.source_id)

    conf, reason = Confidence.READY, ""
    if not covered:
        conf = Confidence.REQUIRES_INPUT
        reason = gap or "The sources consulted did not cover this. Add what you know."
    else:
        for q in linked:
            c, r = assess_confidence(q, ev_by_q.get(q.id, []), contradictions)
            if c is Confidence.REQUIRES_INPUT:
                conf, reason = c, r
                break
    answered_all = bool(linked) and all(q.answer_status is AnswerStatus.ANSWERED for q in linked)
    return Insight(
        run_id=run_id, stage=stage, bucket=bucket, category=category,
        number=int(card.get("number") or 0), card_key=str(card.get("key") or ""),
        title=str(card.get("title") or "Finding"),
        summary=finding or ("Not covered by the sources consulted in this run."),
        evidence_type=evidence_type if evidence else "", evidence=evidence,
        interpretation=interpretation, review_note=review_note,
        confidence=conf, input_reason=reason, covered=covered,
        tag=VerificationTag.VERIFIED if answered_all and own else VerificationTag.INFERENCE,
        evidence_ids=[e.id for e in own], source_ids=source_ids,
        question_ids=[q.id for q in linked],
        used_web_fallback=any(q.used_web_fallback for q in linked),
        table_titles=titles,
    )


# -- deterministic fill --------------------------------------------------------
def deterministic(run_id: str, stage: str, bucket: str, report: StageReport,
                  questions: list[ResearchQuestion], evidence: list[Evidence],
                  contradictions: list[Contradiction]) -> list[Insight]:
    ev_by_q: dict[str, list[Evidence]] = {}
    for e in evidence:
        ev_by_q.setdefault(e.question_id, []).append(e)
    category = CATEGORY_BY_BUCKET.get(bucket, "Clinical")
    out: list[Insight] = []
    used_tables: set[str] = set()
    used_quotes: set[str] = set()
    used_findings: set[str] = set()
    for card in catalogue_for(bucket):
        linked = _questions_for(card, questions)
        # The finding is the first linked answer not already headlining another
        # card; failing that, the strongest unused quote.
        finding = ""
        for q in linked:
            text = _clean_text(q.answer_text, 500)
            if text and text[:120].lower() not in used_findings:
                finding = text
                break
        if not finding:
            for q in linked:
                for e in sorted(ev_by_q.get(q.id, []), key=lambda e: (e.tier, -e.relevance)):
                    text = _clean_text(e.quote, 320)
                    if text and text[:120].lower() not in used_findings:
                        finding = text
                        break
                if finding:
                    break
        if finding:
            used_findings.add(finding[:120].lower())
        own = [e for q in linked for e in ev_by_q.get(q.id, [])]
        etype, ev = _evidence_from_report(card, report, linked, own, used_tables, used_quotes)
        tables = [t.title for t in _tables_for(card, report)]
        covered = bool(finding)
        out.append(_finish(
            card, run_id=run_id, stage=stage, bucket=bucket, category=category,
            finding=finding, evidence_type=etype, evidence=ev, interpretation="",
            review_note="", linked=linked, ev_by_q=ev_by_q, contradictions=contradictions,
            titles=tables, covered=covered,
        ))
    return out


# -- model fill ----------------------------------------------------------------
async def generate(
    run_id: str, cfg: RunConfig, stage: str, bucket: str, report: StageReport,
    questions: list[ResearchQuestion], evidence: list[Evidence],
    contradictions: list[Contradiction], category: str | None = None,
) -> list[Insight]:
    """Every catalogue card for this agent, filled from the stage document.
    Falls back to the deterministic fill when no model is available or the
    call fails, so the set of cards is the same either way."""
    cards = catalogue_for(bucket)
    category = category or CATEGORY_BY_BUCKET.get(bucket, "Clinical")
    if not cards:
        return []
    if not llm.available or not questions:
        return deterministic(run_id, stage, bucket, report, questions, evidence, contradictions)

    fw = get_framework()["buckets"][bucket]
    meta = get_questions()["stage_meta"].get(stage, {})
    table_titles = [t.title for t in report.tables]
    prompt = (
        f"Indication: {cfg.indication}. Geography: {cfg.geography}. Objective: {cfg.objective}.\n"
        f"Agent: {fw['agent_name']} — {fw['name']}. Stage: {meta.get('name', stage)}.\n\n"
        "THE DOCUMENT FOR THIS STAGE:\n" + _document_text(report, questions) + "\n\n"
        "CARDS TO FILL (all of them, in this order):\n"
        + "\n".join(_card_spec_text(c) for c in cards) + "\n\n"
        'Return JSON: {"cards": [{"key": str, "covered": bool, "finding": str, '
        '"evidence": ..., "interpretation": str, "review_note": str, '
        '"questions": [int], "table_titles": [str], "gap": str}]}.\n'
        "- covered: false when the document does not contain what the card asks for; "
        "then finding, evidence and interpretation are empty and gap says in one sentence "
        "what a reviewer would need to supply.\n"
        "- finding: 1-2 sentences stating the finding with its figures, criteria, codes or "
        "agents as the document gives them.\n"
        "- evidence: in the card's format, from the document only.\n"
        "- interpretation: 1-2 sentences on what this means for building patient cohorts "
        "and lines of therapy from claims data.\n"
        "- review_note: one short sentence naming the single point most worth an expert's "
        "check (a figure's year, a threshold, a code's specificity); empty if nothing stands out.\n"
        "- questions: the Q numbers the card rests on. table_titles: exact TABLE titles used."
    )
    try:
        result = await llm.complete_json(_SYSTEM, prompt, max_tokens=5000)
    except LLMUnavailable:
        return deterministic(run_id, stage, bucket, report, questions, evidence, contradictions)
    except Exception:  # noqa: BLE001 - the deterministic fill covers the failure
        log.exception("insight generation failed for %s/%s", run_id, stage)
        return deterministic(run_id, stage, bucket, report, questions, evidence, contradictions)

    raw = (result or {}).get("cards") if isinstance(result, dict) else None
    by_key: dict[str, dict] = {}
    for item in (raw if isinstance(raw, list) else []):
        if isinstance(item, dict) and item.get("key"):
            by_key[str(item["key"]).strip()] = item
    if not by_key:
        log.warning("insight generation returned nothing usable for %s/%s; deterministic fill",
                    run_id, stage)
        return deterministic(run_id, stage, bucket, report, questions, evidence, contradictions)

    ev_by_q: dict[str, list[Evidence]] = {}
    for e in evidence:
        ev_by_q.setdefault(e.question_id, []).append(e)

    out: list[Insight] = []
    for card in cards:
        item = by_key.get(card["key"])
        if item is None:
            # The model skipped a slot; fill it deterministically so the set
            # of cards is always complete.
            out += [i for i in deterministic(run_id, stage, bucket, report, questions,
                                             evidence, contradictions)
                    if i.card_key == card["key"]]
            continue
        finding = _clean_text(item.get("finding"), 600)
        covered = bool(item.get("covered", True)) and bool(finding)
        q_idx = _clean_indices(item.get("questions"), len(questions))
        linked = [questions[i - 1] for i in q_idx] or _questions_for(card, questions)
        titles: list[str] = []
        for t in (item.get("table_titles") or []):
            m = _match_title(str(t), table_titles)
            if m and m not in titles:
                titles.append(m)
        if not titles:
            titles = [t.title for t in _tables_for(card, report)]
        etype = str(card.get("evidence_type") or "list")
        ev = _clean_evidence(etype, item.get("evidence")) if covered else None
        if covered and ev is None:
            own = [e for q in linked for e in ev_by_q.get(q.id, [])]
            etype, ev = _evidence_from_report(card, report, linked, own)
        out.append(_finish(
            card, run_id=run_id, stage=stage, bucket=bucket, category=category,
            finding=finding, evidence_type=etype, evidence=ev,
            interpretation=_clean_text(item.get("interpretation"), 400),
            review_note=_clean_text(item.get("review_note"), 200),
            linked=linked, ev_by_q=ev_by_q, contradictions=contradictions,
            titles=titles, covered=covered, gap=_clean_text(item.get("gap"), 240),
        ))
    log.info("stage %s: %d cards filled from the document", stage, len(out))
    return out


# -- phase cards -----------------------------------------------------------------
async def phase_cards(run_id: str, cfg: RunConfig, phase: str, reports: list[StageReport],
                      cards_so_far: list[Insight]) -> list[Insight]:
    """Cards owed by a whole phase, written from all of its stage documents."""
    out: list[Insight] = []
    for card in phase_catalogue(phase):
        bucket = str(card.get("bucket") or "C")
        stage = str(card.get("stage") or (reports[-1].stage if reports else "stage_2"))
        category = str(card.get("category") or "Synthesis")
        related = [i for i in cards_so_far if i.stage in {r.stage for r in reports}]
        points: list[str] = []
        finding = ""
        interpretation = ""
        if llm.available and reports:
            digest = "\n".join(
                f"[{i.number:02d}] {i.title}: {i.summary[:300]}" for i in related
            ) + "\n\nTAKEAWAYS:\n" + "\n".join(
                f"- {t[:240]}" for r in reports for t in r.takeaways[:5]
            )
            try:
                result = await llm.complete_json(
                    _SYSTEM,
                    f"Indication: {cfg.indication}. Objective: {cfg.objective}.\n"
                    f"Card: {card['title']}. Objective: {card.get('objective', '')}.\n\n"
                    "FINDINGS OF THE PHASE:\n" + digest + "\n\n"
                    'Return JSON: {"finding": str, "points": [str], "interpretation": str}. '
                    "finding: one sentence on what the phase established. points: 4-6 "
                    "insights, one sentence each, that downstream modelling must preserve "
                    "(population variables, diagnostic signals, treatment branches, label "
                    "changes). interpretation: one sentence on what to carry into the next "
                    "phase. Only from the findings above.",
                    max_tokens=1200,
                )
                finding = _clean_text((result or {}).get("finding"), 400)
                points = _clean_evidence("list", (result or {}).get("points")) or []
                interpretation = _clean_text((result or {}).get("interpretation"), 300)
            except Exception:  # noqa: BLE001 - fall back to takeaways
                log.exception("phase card failed for %s/%s", run_id, phase)
        if not points:
            points = [_clean_text(t, 200) for r in reports for t in r.takeaways[:3]][:6]
        if not finding:
            finding = (f"The {phase} phase established {len(related)} findings across "
                       f"{len(reports)} stage report(s); the points below should inform "
                       "downstream modelling.")
        sources: list[str] = []
        for i in related:
            for s in i.source_ids:
                if s not in sources:
                    sources.append(s)
        out.append(Insight(
            run_id=run_id, stage=stage, bucket=bucket, category=category,
            number=int(card.get("number") or 0), card_key=str(card.get("key") or ""),
            title=str(card.get("title") or "Key insights"),
            summary=finding, evidence_type="list" if points else "", evidence=points or None,
            interpretation=interpretation, confidence=Confidence.READY,
            tag=VerificationTag.INFERENCE,
            evidence_ids=[e for i in related for e in i.evidence_ids][:200],
            source_ids=sources, question_ids=[q for i in related for q in i.question_ids],
            table_titles=[], covered=bool(points),
        ))
    return out
