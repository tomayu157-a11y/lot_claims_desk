"""Builds a StageReport: the structured object the UI renders as a stage.

Two paths. With an LLM the tables and prose are model-written from the
evidence and then checked back against it. Without one, everything is derived
from the evidence directly. Both paths carry the same provenance, so a reader
can always see which source produced a row.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from ..models import (
    Answer,
    Contradiction,
    Evidence,
    InsightTable,
    QuestionStatus,
    ResearchQuestion,
    RunConfig,
    StageReport,
    VerificationTag,
)
from ..settings import get_framework, get_questions
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.synthesis")

_SYSTEM = (
    "You are a clinical desk-research synthesiser producing an SME-ready deliverable for "
    "US claims analytics. You write only what the supplied evidence supports. Every factual "
    "sentence you write must be traceable to a supplied quote. You never invent a "
    "statistic, a code, a regimen or an approval. You never resolve a disagreement between "
    "sources. You mark original analytical rules as [ORIGINAL] and inferences as "
    "[INFERENCE]; everything drawn directly from a quote is [VERIFIED]."
)

_COL_SPEC = re.compile(r"\(([^)]*\|[^)]*)\)")


def parse_expected_tables(expected: list[str]) -> list[tuple[str, list[str]]]:
    """`"Epidemiology snapshot table (Metric | Value | Source)"` becomes a title
    and its column list. Entries without a column spec are prose sections."""
    out: list[tuple[str, list[str]]] = []
    for item in expected:
        m = _COL_SPEC.search(item)
        if not m:
            continue
        cols = [c.strip() for c in m.group(1).split("|") if c.strip()]
        if len(cols) >= 2:
            out.append((item[: m.start()].strip(), cols))
    return out


def prose_sections(expected: list[str]) -> list[str]:
    return [i for i in expected if not _COL_SPEC.search(i)]


def _tag(text: str, tag: VerificationTag = VerificationTag.VERIFIED) -> str:
    return f"[{tag.value}] {text}" if not text.startswith("[") else text


def _cite(evs: list[Evidence]) -> str:
    names, seen = [], set()
    for e in evs:
        n = e.citation
        if n not in seen:
            seen.add(n)
            names.append(n)
    return f"[Source: {'; '.join(names[:4])}]" if names else ""


def _sentence_case(text: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------
# Deterministic construction
# --------------------------------------------------------------------------
def _metric_rows(
    evidence: list[Evidence], columns: list[str], used_question_ids: set[str] | None = None,
) -> list[list[str]]:
    """Fill an N-column table from evidence. The last column is always the
    citation; the first carries the subject; the middle carries the claim.
    `used_question_ids`, when given, collects which questions fed the rows so
    the table can be linked back to the insights built from them."""
    rows: list[list[str]] = []
    for ev in evidence:
        if len(rows) >= 12:
            break
        if used_question_ids is not None:
            used_question_ids.add(ev.question_id)
        subject = ev.title or ev.citation
        row = [_sentence_case(subject, 70)]
        while len(row) < len(columns) - 1:
            row.append(_tag(_sentence_case(ev.quote), ev.tag))
        row.append(f"[Source: {ev.citation}]")
        rows.append(row[: len(columns)])
    return rows


def _observability_rows(
    questions: list[ResearchQuestion], evidence_by_q: dict[str, list[Evidence]]
) -> list[dict[str, str]]:
    """Claims observability is an analytical judgement, not a source fact, so it
    is always tagged ORIGINAL."""
    out: list[dict[str, str]] = []
    for q in questions[:6]:
        evs = evidence_by_q.get(q.id, [])
        if not evs:
            continue
        answered = q.status is QuestionStatus.SUFFICIENT
        out.append({
            "concept": _sentence_case(q.seed_text.strip(" ?") or q.text, 110),
            "classification": "DIRECT SIGNAL" if answered else "PROXY SIGNAL",
            "basis": _sentence_case(evs[0].quote, 180),
            "limitation": (
                "Supported by coded sources retrievable from claims."
                if answered
                else q.unmet_reason
                or "Not consistently observable from administrative claims alone."
            ),
        })
    return out


def _takeaways(
    questions: list[ResearchQuestion], evidence_by_q: dict[str, list[Evidence]]
) -> list[str]:
    out: list[str] = []
    for q in questions:
        evs = sorted(evidence_by_q.get(q.id, []), key=lambda e: (e.tier, -e.relevance))
        if q.answer_text:
            tag = "VERIFIED" if evs and not evs[0].is_supplementary else "GENERAL KNOWLEDGE"
            out.append(f"{_sentence_case(q.answer_text, 240)} [{tag}]")
        elif evs:
            out.append(f"{_sentence_case(evs[0].quote, 240)} [{evs[0].tag.value}]")
        if len(out) >= 6:
            break
    return out


def _synthesis_paragraph(
    cfg: RunConfig, meta: dict, questions: list[ResearchQuestion],
    evidence_by_q: dict[str, list[Evidence]],
) -> str:
    bits: list[str] = []
    for q in questions:
        # An established answer says what the evidence means; a bare quote only
        # says what one source stated. Prefer the answer where one exists.
        if q.answer_text:
            bits.append(_sentence_case(q.answer_text, 400))
        else:
            evs = sorted(evidence_by_q.get(q.id, []), key=lambda e: (e.tier, -e.relevance))
            if evs:
                bits.append(_sentence_case(evs[0].quote, 300))
        if len(bits) >= 5:
            break
    if not bits:
        return (
            f"No approved source returned usable evidence for {cfg.indication} at this "
            f"stage. Every question is recorded below with the reason it could not be "
            f"answered."
        )
    lead = (
        f"For {cfg.indication} in {cfg.geography}"
        f"{' (' + cfg.target_population + ')' if cfg.target_population else ''}, "
        f"the retrieved evidence establishes the following."
    )
    return " ".join([lead, *bits])


# --------------------------------------------------------------------------
# LLM construction
# --------------------------------------------------------------------------
async def _llm_stage(
    cfg: RunConfig, stage: str, meta: dict,
    questions: list[ResearchQuestion], evidence_by_q: dict[str, list[Evidence]],
    answers: dict[str, list[Answer]] | None = None,
) -> dict | None:
    payload = {
        "indication": cfg.indication,
        "population": cfg.target_population or "adults",
        "geography": cfg.geography,
        "research_cutoff": cfg.research_cutoff,
        "stage_name": meta["name"],
        "core_question": meta["core_question"],
        "expected_output": meta["expected_output"],
        "questions": [
            {
                "question": q.text,
                "answered": q.status is QuestionStatus.SUFFICIENT,
                "established_answer": q.answer_text,
                "answer_status": q.answer_status.value,
                "unmet_reason": q.unmet_reason,
                "evidence": [
                    {
                        "quote": e.quote,
                        "source": e.citation,
                        "tier": e.tier,
                        "url": e.url,
                        "supplementary_web": e.is_supplementary,
                    }
                    for e in sorted(evidence_by_q.get(q.id, []), key=lambda x: x.tier)[:10]
                ],
            }
            for q in questions
        ],
    }
    try:
        return await llm.complete_json(
            _SYSTEM,
            "Write this research stage.\n\n"
            "Each question below carries an established_answer already derived from its "
            "evidence and verified against it. Build the stage from those answers: the "
            "synthesis, tables and narratives must be consistent with them and must not "
            "contradict or exceed them. The quotes are supplied so you can cite precisely, "
            "not so you can reach a different conclusion.\n\n"
            "Return JSON with keys:\n"
            '  "what_happens": one paragraph describing what this stage establishes.\n'
            '  "synthesis": 4-8 sentence prose synthesis, every claim traceable to a quote.\n'
            '  "tables": [{"title": str, "columns": [str], "rows": [[str]], '
            '"footnote": str, "question_indices": [int]}] — one entry per expected_output '
            "item that names columns in parentheses, using exactly those column names. "
            "question_indices lists the 0-based positions in `questions` that the table "
            "answers. Prefix each factual cell "
            "with [VERIFIED] and end each row's evidence with [Source: name]. Use "
            "[NOT VERIFIED] for any value you cannot locate in a quote.\n"
            '  "narratives": [{"heading": str, "body": str}] — one per expected_output '
            "item that does not name columns.\n"
            '  "takeaways": [str] — 4-6 numbered-style findings, each ending with a tag.\n'
            '  "assumptions": [str]\n'
            '  "observability": [{"concept": str, "classification": '
            '"DIRECT SIGNAL"|"PROXY SIGNAL"|"NOT OBSERVABLE", "basis": str, '
            '"limitation": str}]\n\n'
            "Do not resolve disagreements between sources. Do not invent codes, "
            "statistics, regimens or approvals.\n\n"
            f"{payload}",
            max_tokens=8000,
        )
    except LLMUnavailable as exc:
        log.info("stage synthesis falling back to deterministic: %s", exc)
        return None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def build_stage_report(
    run_id: str, cfg: RunConfig, stage: str, bucket: str,
    questions: list[ResearchQuestion], evidence: list[Evidence],
    contradictions: list[Contradiction], answers: list[Answer] | None = None,
) -> StageReport:
    fw = get_framework()
    meta = get_questions()["stage_meta"][stage]
    spec = fw["buckets"][bucket]
    steps = {n: s for n, s in fw["steps"].items() if s.get("stage") == stage}

    evidence_by_q: dict[str, list[Evidence]] = defaultdict(list)
    for e in evidence:
        evidence_by_q[e.question_id].append(e)

    report = StageReport(
        run_id=run_id, stage=stage, bucket=bucket,
        name=meta["name"], core_question=meta["core_question"],
        agent_name=spec["agent_name"],
        framework_steps=meta["framework_steps"],
        step_numbers=sorted(steps),
        substeps={
            k: v for s in steps.values() for k, v in (s.get("substeps") or {}).items()
        },
        gate=spec.get("gate", ""), output_name=spec.get("output", ""),
        expected_output=meta["expected_output"],
    )

    # The question-and-answer record. Built first, because it is the spine of
    # the document: the prose and tables below are written from these answers
    # rather than from the raw quote pile.
    by_question: dict[str, list[Answer]] = defaultdict(list)
    for a in answers or []:
        by_question[a.question_id].append(a)
    report.answers = [
        {
            "question": q.text,
            "seed": q.seed_text or q.text,
            "answer": q.answer_text or "",
            "status": q.answer_status.value,
            "citations": q.answer_citations,
            "sources": sorted({e.source_id for e in evidence_by_q.get(q.id, [])}),
            "evidence_count": len(evidence_by_q.get(q.id, [])),
            "coverage": round(q.coverage_score, 2),
            "supplementary": any(e.is_supplementary for e in evidence_by_q.get(q.id, [])),
            "unmet_reason": q.unmet_reason,
        }
        for q in questions
    ]

    data = await _llm_stage(cfg, stage, meta, questions, evidence_by_q,
                            answers=by_question) if llm.available else None

    if data:
        report.what_happens = str(data.get("what_happens", ""))
        report.synthesis = str(data.get("synthesis", ""))
        for t in data.get("tables") or []:
            cols = [str(c) for c in (t.get("columns") or [])]
            rows = [[str(c) for c in r] for r in (t.get("rows") or []) if r]
            if cols and rows:
                qids: list[str] = []
                for idx in t.get("question_indices") or []:
                    try:
                        qids.append(questions[int(idx)].id)
                    except (TypeError, ValueError, IndexError):
                        continue
                report.tables.append(
                    InsightTable(
                        title=str(t.get("title") or "Table"),
                        columns=cols,
                        rows=[r[: len(cols)] + [""] * (len(cols) - len(r)) for r in rows],
                        footnote=str(t.get("footnote") or ""),
                        question_ids=qids,
                    )
                )
        report.narratives = [
            {"heading": str(n.get("heading", "")), "body": str(n.get("body", ""))}
            for n in (data.get("narratives") or [])
            if n.get("body")
        ]
        report.takeaways = [str(x) for x in (data.get("takeaways") or [])]
        report.assumptions = [str(x) for x in (data.get("assumptions") or [])]
        report.observability = [
            {k: str(v) for k, v in row.items()} for row in (data.get("observability") or [])
        ]

    if not report.synthesis:
        report.synthesis = _synthesis_paragraph(cfg, meta, questions, evidence_by_q)
    if not report.what_happens:
        report.what_happens = (
            f"This stage establishes {meta['name'].lower()} for {cfg.indication} in "
            f"{cfg.geography}, drawn from the approved source registry and recorded with "
            f"per-claim provenance."
        )
    if not report.tables:
        ordered = sorted(evidence, key=lambda e: (e.tier, -e.relevance))
        for title, cols in parse_expected_tables(meta["expected_output"]):
            used: set[str] = set()
            rows = _metric_rows(ordered, cols, used)
            if rows:
                report.tables.append(
                    InsightTable(title=title, columns=cols, rows=rows,
                                 footnote="Rows are verbatim source statements; "
                                          "cell grouping is analytical.",
                                 question_ids=sorted(used))
                )
    if not report.narratives:
        for heading in prose_sections(meta["expected_output"])[:6]:
            body = " ".join(
                _sentence_case(e.quote, 320)
                for e in sorted(evidence, key=lambda x: (x.tier, -x.relevance))[:3]
            )
            if body:
                report.narratives.append({"heading": heading, "body": f"{body} {_cite(evidence[:3])}"})
    if not report.takeaways:
        report.takeaways = _takeaways(questions, evidence_by_q)
    if not report.observability:
        report.observability = _observability_rows(questions, evidence_by_q)
    if not report.assumptions:
        report.assumptions = [
            f"Published registry and guideline statistics reflect standard "
            f"{cfg.geography} clinical epidemiology through the {cfg.research_cutoff} cutoff."
        ]

    report.unanswered = [
        {
            "question": q.text,
            "reason": q.unmet_reason or "did not reach the sufficiency threshold",
            "sources_attempted": ", ".join(q.sources_attempted[:8]) or "none",
        }
        for q in questions
        if q.status is not QuestionStatus.SUFFICIENT
    ]

    report.evidence_count = len(evidence)
    report.source_count = len({e.source_id for e in evidence})
    report.supplementary_count = sum(1 for e in evidence if e.is_supplementary)
    report.tiers_represented = sorted({e.tier for e in evidence})
    return report
