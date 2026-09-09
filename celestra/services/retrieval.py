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
import functools
import logging
import re
from dataclasses import dataclass, field

from ..connectors.base import ConnectorResult, RetrievalContext
from ..models import (
    Answer,
    Evidence,
    QuestionStatus,
    ResearchQuestion,
    RunConfig,
    SourceRef,
)
from ..settings import get_source_registry, get_thresholds
from .answering import answer_batch, merge as merge_answers
from .extraction import build_terms
from .llm import LLMUnavailable, llm
from .scoring import Sufficiency, assess

log = logging.getLogger("celestra.retrieval")


@dataclass
class RetrievalOutcome:
    evidence: list[Evidence] = field(default_factory=list)
    answers: list[Answer] = field(default_factory=list)
    eliminated: int = 0
    web_sites: list[dict] = field(default_factory=list)
    sufficiency: Sufficiency | None = None
    attempted: list[str] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    used_web: bool = False
    rounds: int = 0


@functools.lru_cache(maxsize=1)
def _unusable_source_ids() -> frozenset[str]:
    """Sources that cannot answer today: missing licence, credential or file.

    Cached for the process because it reflects configuration, not run state.
    """
    try:
        from ..connectors.registry import connector_health

        return frozenset(row["id"] for row in connector_health() if not row["configured"])
    except Exception:  # noqa: BLE001 - never let health checking break retrieval
        return frozenset()


def sources_for(stage: str, indication_key: str) -> list[dict]:
    """Approved sources for this stage and indication, best first.

    Ordering matters because the per-question source budget is finite. Sorting
    by tier then id alone spent the whole budget alphabetically: a stage with
    fourteen registered sources would call eight blocked ones and never reach
    the working alternative further down the alphabet. Sources known to be
    unusable are therefore ranked last, so they are attempted only if budget
    remains and still appear in the attempted list with their blocking reason.
    """
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
    blocked = _unusable_source_ids()
    return sorted(out, key=lambda s: (s["id"] in blocked, s["tier"], s["id"]))


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


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).netloc or "").replace("www.", "")
    except ValueError:
        return ""


def _merge_evidence(
    current: list[Evidence], addition: list[Evidence], limits: dict
) -> list[Evidence]:
    """Add new evidence, keeping the quote set diverse across sources.

    Truncating a merged pile by score alone lets one verbose document take
    every slot, which reads as well-sourced while resting on a single source
    and fails the distinct-source threshold. Selection round-robins across
    sources under a per-source cap instead.
    """
    seen = {e.quote[:120].lower() for e in current}
    pool = list(current)
    for e in addition:
        key = e.quote[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        pool.append(e)

    per_source_cap = int(limits.get("max_evidence_items_per_source", 3))
    total_cap = int(limits["max_evidence_items_per_question"])

    by_source: dict[str, list[Evidence]] = {}
    for e in sorted(pool, key=lambda x: (x.tier, -x.relevance)):
        by_source.setdefault(e.source_id, []).append(e)

    out: list[Evidence] = []
    for depth in range(per_source_cap):
        for source_id in sorted(by_source, key=lambda s: by_source[s][0].tier):
            bucket = by_source[source_id]
            if depth < len(bucket) and len(out) < total_cap:
                out.append(bucket[depth])
        if len(out) >= total_cap:
            break
    out.sort(key=lambda e: (e.tier, -e.relevance))
    return out[:total_cap]


async def retrieve(
    question: ResearchQuestion,
    cfg: RunConfig,
    synonyms: list[str],
    registry: dict,
    on_source=None,
    context: dict | None = None,
) -> RetrievalOutcome:
    """Answer one question. `on_source` is an async callback
    (source_id, source_name, ok, count, reason) that streams live progress.

    Flow, in order:
      1. discover   every approved source returns cheap metadata (title, abstract)
      2. rank       one model call scores all candidates for this question
      3. hydrate    full text is fetched only for the top few
      4. extract    documents go to the model in batches, not one per call
      5. assess     against the sufficiency threshold
      6. widen      a further round keeps more candidates and rewrites the query
      7. fallback   open web, only after the approved sources are exhausted
    """
    from .hydration import hydrate
    from .ranking import rank

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
    terms = build_terms(question.text, question.aspects, synonyms, upstream_terms)
    query = question.text

    candidates: dict[str, SourceRef] = {}       # by ref.key, across rounds
    hydrated: dict[str, SourceRef] = {}         # cache so a doc is fetched once
    top_k = int(limits.get("rank_top_k", 8))
    hydrate_k = int(limits.get("hydrate_top_k", 5))
    batch_size = max(1, int(limits.get("extract_batch_size", 3)))
    min_relevance = float(limits.get("min_relevance", 0.35))

    for round_no in range(esc["max_refinement_rounds"] + 1):
        outcome.rounds = round_no
        ctx = RetrievalContext(
            indication=cfg.indication, indication_key=cfg.indication_key,
            synonyms=synonyms, geography=cfg.geography,
            population=cfg.target_population, stage=question.stage,
            question=query, aspects=question.aspects, cutoff=cfg.research_cutoff,
            extra=dict(context or {}),
        )

        # 1. discover
        results = await _gather(registry, source_ids, ctx, per_source)
        for r in results:
            if r.source_id not in outcome.attempted:
                outcome.attempted.append(r.source_id)
            if r.ok and r.count:
                if r.source_id not in outcome.answered:
                    outcome.answered.append(r.source_id)
                for ref in r.refs:
                    candidates.setdefault(ref.key, ref)
            elif not r.ok:
                outcome.failures[r.source_id] = r.reason
            if on_source:
                await on_source(r.source_id, names.get(r.source_id, r.source_id),
                                r.ok, r.count, r.reason)

        if not candidates:
            outcome.sufficiency = assess(question, [])
        else:
            # 2. rank, eliminate the irrelevant, keep the top slice
            ranked = await rank(list(candidates.values()), question.text, terms)
            relevant = [(ref, score) for ref, score in ranked if score >= min_relevance]
            outcome.eliminated = len(ranked) - len(relevant)
            if not relevant:
                # Nothing cleared the relevance floor. Keep the single best so
                # the round still reads something rather than reporting an
                # empty result that a wider query might have answered.
                relevant = ranked[:1]
            keep = [ref for ref, _ in relevant[:top_k]]

            # 3. hydrate the best few; the rest are read from their abstracts
            docs: list[SourceRef] = []
            for i, ref in enumerate(keep):
                if i < hydrate_k:
                    if ref.key not in hydrated:
                        hydrated[ref.key] = await hydrate(ref, registry)
                    docs.append(hydrated[ref.key])
                else:
                    docs.append(ref)

            # 4. answer the question from each batch, stopping as soon as the
            #    accumulated evidence clears the threshold. Later batches are
            #    not read when earlier ones already answered it.
            round_evidence: list[Evidence] = list(outcome.evidence)
            for batch_no, start in enumerate(range(0, len(docs), batch_size)):
                answer, evidence = await answer_batch(
                    question.text, question.aspects, docs[start:start + batch_size],
                    question.id, terms, batch_index=batch_no, round_index=round_no,
                )
                if answer is not None:
                    outcome.answers.append(answer)
                round_evidence = _merge_evidence(round_evidence, evidence, limits)
                outcome.evidence = round_evidence
                outcome.sufficiency = assess(question, round_evidence)
                if outcome.sufficiency.ok and outcome.answers:
                    break

            # 5. assess
            outcome.sufficiency = assess(question, outcome.evidence)

        if outcome.sufficiency.ok:
            return outcome

        # 6. widen: keep more candidates next round and rewrite the query
        if round_no < esc["max_refinement_rounds"]:
            top_k += int(limits.get("rank_top_k_step", 4))
            query = await refine_query(question, cfg, synonyms, outcome.sufficiency, round_no + 1)
            log.info("refining q=%s round=%s top_k=%s -> %s",
                     question.id, round_no + 1, top_k, query)

    if not esc["enable_open_web_fallback"]:
        return outcome

    # 7. open-web fallback, only once the approved sources are exhausted
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
        # Record every page the search returned, whether or not it was read,
        # so the evidence trail shows where the fallback actually looked.
        outcome.web_sites = [
            {"url": r.url, "title": r.title or r.url, "site": _domain(r.url),
             "scraped": False, "used": False}
            for r in web_refs
        ]
        scraped: list[SourceRef] = []
        for i, ref in enumerate(web_refs[: esc["open_web_max_scrapes"]]):
            page = await fire.scrape(ref.url)
            if i < len(outcome.web_sites):
                outcome.web_sites[i]["scraped"] = True
            scraped.append(page or ref)
        for batch_no, start in enumerate(range(0, len(scraped), batch_size)):
            answer, extra = await answer_batch(
                question.text, question.aspects, scraped[start:start + batch_size],
                question.id, terms, batch_index=batch_no, round_index=99,
            )
            if answer is not None:
                outcome.answers.append(answer)
            if extra and "open_web" not in outcome.answered:
                outcome.answered.append("open_web")
            outcome.evidence = _merge_evidence(outcome.evidence, extra, limits)
            if assess(question, outcome.evidence).ok and outcome.answers:
                break
        extra = [e for e in outcome.evidence if e.is_supplementary]
        # Mark which pages actually produced a quote.
        used_urls = {e.url for e in extra}
        for site in outcome.web_sites:
            site["used"] = site["url"] in used_urls
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

    text, status, citations = merge_answers(outcome.answers)
    question.answer_text = text
    question.answer_status = status
    question.answer_citations = citations
    question.web_sites = outcome.web_sites

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
