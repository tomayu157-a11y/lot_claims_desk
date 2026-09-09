"""Applying a reviewer's instruction to a finding.

Modify is not a note. The reviewer's instruction goes to the model together
with the established answer and its evidence, and the model decides whether it
can apply the change from what is already held or needs to look further. When
it needs more, the open-web fallback runs and the revised answer is rebuilt
from what that returns.

The same rule as everywhere else holds: a revised answer must be supported by
verified quotes. An instruction cannot conjure a fact the sources do not
contain, and the model is told to say so rather than comply.
"""
from __future__ import annotations

import logging
import re

from ..models import (
    Answer,
    AnswerStatus,
    Evidence,
    Insight,
    ResearchQuestion,
    RunConfig,
)
from ..settings import get_thresholds
from .answering import answer_batch
from .extraction import build_terms
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.revision")

_TRIAGE_SYSTEM = (
    "You decide whether a reviewer's instruction about a research finding can be applied "
    "from the evidence already held, or whether more sources must be consulted first. You "
    "never invent a fact to satisfy an instruction."
)
_APPLY_SYSTEM = (
    "You revise a research finding according to a reviewer's instruction, using only the "
    "evidence supplied. Every claim in the revised answer must be supported by one of the "
    "quotes. If the instruction asks for something the evidence does not support, you say "
    "plainly that it is not supported and leave that part unchanged."
)


class RevisionResult:
    __slots__ = ("text", "status", "citations", "evidence", "answer",
                 "searched", "sites", "note")

    def __init__(self) -> None:
        self.text: str = ""
        self.status: AnswerStatus = AnswerStatus.NOT_FOUND
        self.citations: list[str] = []
        self.evidence: list[Evidence] = []
        self.answer: Answer | None = None
        self.searched: bool = False
        self.sites: list[dict] = []
        self.note: str = ""


async def _needs_more(instruction: str, question: str, answer: str,
                      quotes: list[str]) -> tuple[bool, str]:
    """Ask whether the held evidence can satisfy the instruction."""
    try:
        verdict = await llm.complete_json(
            _TRIAGE_SYSTEM,
            f"Question: {question}\n\nCurrent answer: {answer or '(none)'}\n\n"
            "Evidence held:\n" + "\n".join(f"- {q[:300]}" for q in quotes[:12]) + "\n\n"
            f"Reviewer instruction: {instruction}\n\n"
            'Return JSON: {"needs_more_sources": bool, "search_query": str, "reason": str}. '
            "needs_more_sources is true only when the instruction asks for information the "
            "evidence above does not contain. search_query is what to look for on the open "
            "web, empty when nothing is needed.",
            max_tokens=500,
        )
    except LLMUnavailable as exc:
        return False, str(exc)
    return bool((verdict or {}).get("needs_more_sources")), str(
        (verdict or {}).get("search_query") or ""
    )


async def revise(
    insight: Insight,
    question: ResearchQuestion,
    evidence: list[Evidence],
    instruction: str,
    cfg: RunConfig,
    registry: dict,
    synonyms: list[str] | None = None,
) -> RevisionResult:
    """Apply the instruction, searching the web when the held evidence cannot."""
    out = RevisionResult()
    out.evidence = list(evidence)
    instruction = (instruction or "").strip()
    if not instruction:
        return out

    if not llm.available:
        out.note = ("No model provider is configured, so the instruction was recorded "
                    "against the finding but not applied.")
        return out

    esc = get_thresholds()["escalation"]
    terms = build_terms(question.text, question.aspects, synonyms or [cfg.indication])
    quotes = [e.quote for e in evidence]

    # 1. Can this be applied from what is already held?
    needs_more, query = await _needs_more(instruction, question.text,
                                          question.answer_text, quotes)

    # 2. If not, fall back to the open web for the missing part.
    if needs_more:
        fire = registry.get("open_web")
        if fire is not None:
            out.searched = True
            search_query = query or f"{cfg.indication} {instruction}"
            try:
                refs = await fire.search(search_query, esc["open_web_max_results"])
                out.sites = [
                    {"url": r.url, "title": r.title or r.url, "scraped": False, "used": False}
                    for r in refs
                ]
                pages = []
                for i, ref in enumerate(refs[: esc["open_web_max_scrapes"]]):
                    page = await fire.scrape(ref.url)
                    if i < len(out.sites):
                        out.sites[i]["scraped"] = True
                    pages.append(page or ref)
                if pages:
                    answer, extra = await answer_batch(
                        f"{question.text}\n\nReviewer instruction: {instruction}",
                        question.aspects, pages, question.id, terms,
                    )
                    if extra:
                        used = {e.url for e in extra}
                        for site in out.sites:
                            site["used"] = site["url"] in used
                        out.evidence = out.evidence + [
                            e for e in extra
                            if e.quote[:120].lower()
                            not in {x.quote[:120].lower() for x in out.evidence}
                        ]
                    if answer is not None:
                        out.answer = answer
            except Exception as exc:  # noqa: BLE001 - a failed search is not fatal
                log.warning("revision search failed: %s", exc)
                out.note = f"Open-web search failed: {type(exc).__name__}."

    # 3. Rewrite the answer against the full evidence set.
    listing = "\n".join(
        f"- [{e.citation}{' · OPEN WEB' if e.is_supplementary else ''}] {e.quote[:400]}"
        for e in out.evidence[:20]
    )
    try:
        result = await llm.complete_json(
            _APPLY_SYSTEM,
            f"Question: {question.text}\n\nCurrent answer: {question.answer_text or '(none)'}\n\n"
            f"Evidence available:\n{listing}\n\n"
            f"Reviewer instruction: {instruction}\n\n"
            'Return JSON: {"answer": str, "status": "answered"|"partial", '
            '"applied": bool, "note": str}. answer is the revised finding, 2-5 sentences, '
            "every claim supported by a quote above. applied is false when the evidence "
            "cannot support the instruction; note then says what is missing, in one "
            "sentence.",
            max_tokens=1500,
        )
    except LLMUnavailable as exc:
        out.note = f"Revision could not be applied: {exc}"
        return out

    result = result or {}
    text = re.sub(r"\s+", " ", str(result.get("answer", ""))).strip()
    if text:
        out.text = text
        out.status = (
            AnswerStatus.ANSWERED
            if str(result.get("status", "")).lower() == "answered"
            else AnswerStatus.PARTIAL
        )
    if note := str(result.get("note") or "").strip():
        out.note = note
    if not result.get("applied", True) and not out.note:
        out.note = "The available evidence does not support this instruction."

    citations: list[str] = []
    for e in out.evidence:
        if e.citation not in citations:
            citations.append(e.citation)
    out.citations = citations
    return out
