"""Answering one question against the source registry.

Order of attack, and it is deliberate:
  1. Approved sources mapped to this stage and indication, queried in parallel.
  2. If that misses sufficiency, refine the query and retry, up to the
     configured number of rounds.
  3. If it still misses, fall back to open-web search and scrape.

Getting an answer is the priority, so step 3 is a real escalation rather than a
formality. What it is not allowed to do is quietly pass itself off as an
approved source: open-web evidence is tier 5, carries EvidenceOrigin.OPEN_WEB,
and renders as SUPPLEMENTARY WEB EVIDENCE everywhere it appears.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from ..connectors.base import ConnectorResult, RetrievalContext
from ..models import Evidence, QuestionStatus, ResearchQuestion, RunConfig, SourceRef
from ..settings import get_source_registry, get_thresholds
from .extraction import extract, question_terms
from .llm import LLMUnavailable, llm
from .scoring import Sufficiency, assess

log = logging.getLogger("celestra.retrieval")


@dataclass
class RetrievalOutcome:
    evidence: list[Evidence] = field(default_factory=list)
    sufficiency: Sufficiency | None = None
    attempted: list[str] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    used_web: bool = False
    rounds: int = 0


def sources_for(stage: str, indication_key: str) -> list[dict]:
    """Approved sources registered for this stage and indication, best tier first."""
    out = []
    for src in get_source_registry()["sources"]:
        if not src.get("enabled", True) or src.get("fallback_only"):
            continue
        if stage not in (src.get("stages") or []):
            continue
        inds = src.get("indications") or []
        if inds and indication_key not in inds and "ANY" not in inds:
            continue
        out.append(src)
    return sorted(out, key=lambda s: (s["tier"], s["id"]))


def _broaden(question: str, aspects: list[str], synonyms: list[str], round_no: int) -> str:
    """Heuristic query refinement used when no model is configured. Round 1
    drops the parenthetical scoping, round 2 reduces to the key nouns."""
    text = re.sub(r"\s*\([^)]*\)\s*", " ", question).strip(" ?")
    if round_no == 1:
        return text
    nouns = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{4,}", text)][:6]
    base = " ".join(nouns) or text
    return f"{base} {synonyms[0] if synonyms else ''}".strip()


async def refine_query(
    question: ResearchQuestion, cfg: RunConfig, synonyms: list[str],
    suff: Sufficiency, round_no: int,
) -> str:
    if llm.available:
        try:
            result = await llm.complete_json(
                "You rewrite a failing literature/database query so it retrieves more "
                "relevant records. You keep the clinical meaning identical and only change "
                "specificity, phrasing and terminology.",
                f"Original question: {question.text}\n"
                f"Indication: {cfg.indication}\nSynonyms: {synonyms}\n"
                f"Why it failed: {suff.reason}\n"
                f"Aspects still uncovered: {question.aspects}\n\n"
                'Return JSON: {"query": str}. A shorter, more retrievable phrasing using '
                "standard clinical vocabulary. No parentheticals.",
                max_tokens=400,
            )
            if q := str((result or {}).get("query", "")).strip():
                return q
        except LLMUnavailable:
            pass
    return _broaden(question.text, question.aspects, synonyms, round_no)


async def _gather(
    registry: dict, source_ids: list[str], ctx: RetrievalContext, per_source: int
) -> list[ConnectorResult]:
    async def one(sid: str) -> ConnectorResult:
        conn = registry.get(sid)
        if conn is None:
            return ConnectorResult.failure(sid, "no connector registered")
        try:
            return await conn.discover(ctx, per_source)
        except Exception as exc:  # a connector bug must not kill the run
            log.exception("connector %s raised", sid)
            return ConnectorResult.failure(sid, f"connector error: {type(exc).__name__}")

    return list(await asyncio.gather(*(one(s) for s in source_ids)))


async def retrieve(
    question: ResearchQuestion,
    cfg: RunConfig,
    synonyms: list[str],
    registry: dict,
    on_source=None,
    context: dict | None = None,
) -> RetrievalOutcome:
    """`on_source` is an async callback (source_id, source_name, ok, count, reason)
    used to stream live progress to the UI."""
    th = get_thresholds()
    limits, esc = th["limits"], th["escalation"]
    outcome = RetrievalOutcome()

    approved = sources_for(question.stage, cfg.indication_key)[: limits["max_sources_per_question"]]
    source_ids = [s["id"] for s in approved]
    names = {s["id"]: s["name"] for s in approved}
    per_source = max(2, limits["max_evidence_items_per_question"] // max(len(source_ids), 1))

    upstream_terms: list[str] = []
    for value in (context or {}).values():
        if isinstance(value, list):
            upstream_terms += [str(v) for v in value[:20]]
    terms = question_terms(question.text, question.aspects, synonyms + upstream_terms)
    query = question.text
    refs: list[SourceRef] = []

    for round_no in range(esc["max_refinement_rounds"] + 1):
        outcome.rounds = round_no
        ctx = RetrievalContext(
            indication=cfg.indication, indication_key=cfg.indication_key,
            synonyms=synonyms, geography=cfg.geography,
            population=cfg.target_population, stage=question.stage,
            question=query, aspects=question.aspects, cutoff=cfg.research_cutoff,
            extra=dict(context or {}),
        )
        results = await _gather(registry, source_ids, ctx, per_source)
        for r in results:
            if r.source_id not in outcome.attempted:
                outcome.attempted.append(r.source_id)
            if r.ok and r.count:
                if r.source_id not in outcome.answered:
                    outcome.answered.append(r.source_id)
                refs.extend(r.refs)
            elif not r.ok:
                outcome.failures[r.source_id] = r.reason
            if on_source:
                await on_source(r.source_id, names.get(r.source_id, r.source_id),
                                r.ok, r.count, r.reason)

        outcome.evidence = await extract(refs, question.text, question.id, terms)
        outcome.sufficiency = assess(question, outcome.evidence)
        if outcome.sufficiency.ok:
            return outcome
        if round_no < esc["max_refinement_rounds"]:
            query = await refine_query(question, cfg, synonyms, outcome.sufficiency, round_no + 1)
            log.info("refining q=%s round=%s -> %s", question.id, round_no + 1, query)

    if not esc["enable_open_web_fallback"]:
        return outcome

    # ---- Open-web fallback ------------------------------------------------
    fire = registry.get("open_web")
    if fire is None:
        return outcome
    outcome.used_web = True
    if "open_web" not in outcome.attempted:
        outcome.attempted.append("open_web")
    try:
        plain = re.sub(r"\s*\([^)]*\)", "", question.text).strip(" ?")
        web_query = f"{cfg.indication} {plain}"
        web_refs = await fire.search(web_query, esc["open_web_max_results"])
        scraped: list[SourceRef] = []
        for ref in web_refs[: esc["open_web_max_scrapes"]]:
            page = await fire.scrape(ref.url)
            scraped.append(page or ref)
        extra = await extract(scraped, question.text, question.id, terms)
        if extra:
            outcome.answered.append("open_web")
        merged = {e.quote[:120].lower(): e for e in outcome.evidence}
        for e in extra:
            merged.setdefault(e.quote[:120].lower(), e)
        outcome.evidence = list(merged.values())[: limits["max_evidence_items_per_question"]]
        if on_source:
            await on_source("open_web", "Open Web (Supplementary)", bool(extra),
                            len(extra), "" if extra else "no usable page text")
    except Exception as exc:
        log.warning("web fallback failed for %s: %s", question.id, exc)
        outcome.failures["open_web"] = f"{type(exc).__name__}"
        if on_source:
            await on_source("open_web", "Open Web (Supplementary)", False, 0, str(exc)[:80])

    outcome.sufficiency = assess(question, outcome.evidence)
    return outcome


def apply_outcome(question: ResearchQuestion, outcome: RetrievalOutcome) -> None:
    """Write the retrieval result back onto the question, including an honest
    reason when it stayed unanswered."""
    suff = outcome.sufficiency
    question.refinement_rounds = outcome.rounds
    question.used_web_fallback = outcome.used_web
    question.sources_attempted = outcome.attempted
    question.sources_answered = outcome.answered
    question.coverage_score = suff.coverage if suff else 0.0

    if suff and suff.ok:
        question.status = QuestionStatus.SUFFICIENT
        question.unmet_reason = ""
        return

    if not outcome.evidence:
        question.status = QuestionStatus.UNANSWERED
        blockers = "; ".join(
            f"{sid}: {reason}" for sid, reason in list(outcome.failures.items())[:4]
        )
        question.unmet_reason = (
            f"no usable evidence after {outcome.rounds + 1} retrieval round(s)"
            + (f" — {blockers}" if blockers else "")
        )
    else:
        question.status = QuestionStatus.INSUFFICIENT
        question.unmet_reason = suff.reason if suff else "below sufficiency threshold"
