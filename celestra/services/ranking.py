"""Relevance ranking of discovered documents, before anything is fetched.

Discovery returns cheap metadata: a title and an abstract or snippet. Ranking
decides which of those deserve a full-text fetch and an extraction call, in
ONE model call for the whole candidate set rather than one per document.
Without a model, a term-overlap score does the same job less well.
"""
from __future__ import annotations

import logging
import re

from ..models import SourceRef
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.ranking")

_SYSTEM = (
    "You rank candidate documents by how likely each is to contain a direct, quotable "
    "answer to a clinical research question. You judge only from the title and abstract "
    "supplied. You never invent content a document might have."
)
_WORD = re.compile(r"[a-z0-9]+")


def _overlap_score(ref: SourceRef, terms) -> float:
    """Term overlap between a candidate's metadata and the question.

    Focus terms (what the question is about) decide the order; context terms
    (the disease name) only break ties, since every candidate mentions them.
    """
    blob = f"{ref.title} {ref.snippet}".lower()
    words = set(_WORD.findall(blob))
    if not words:
        return 0.0
    focus = getattr(terms, "focus", None)
    context = getattr(terms, "context", None)
    if focus is None:
        focus, context = set(terms or ()), set()
    focus_hits = len(words & focus)
    context_hits = len(words & (context or set()))
    if focus and not focus_hits:
        return round(0.02 * min(context_hits, 3), 3)
    base = focus_hits / (len(focus) ** 0.5) if focus else context_hits / max(len(context or ()), 1) ** 0.5
    # A primary-tier source with any overlap outranks a weak one with more.
    return round(base + 0.03 * min(context_hits, 3) + (0.3 if ref.tier <= 2 else 0.0), 3)


def rank_deterministic(refs: list[SourceRef], terms: set[str]) -> list[tuple[SourceRef, float]]:
    scored = [(r, _overlap_score(r, terms)) for r in refs]
    scored.sort(key=lambda x: (-x[1], x[0].tier))
    return scored


async def rank(
    refs: list[SourceRef], question: str, terms: set[str]
) -> list[tuple[SourceRef, float]]:
    """Every ref with a 0..1 relevance, best first. Never drops a ref: the
    caller decides the cut-off, so a ranking failure degrades to term overlap
    rather than to an empty candidate set."""
    if not refs:
        return []
    if not llm.available:
        return rank_deterministic(refs, terms)

    listing = "\n".join(
        f"[{i}] ({r.source_name}, tier {r.tier}) {r.title[:160]}\n"
        f"     {(r.snippet or '')[:400]}"
        for i, r in enumerate(refs)
    )
    try:
        result = await llm.complete_json(
            _SYSTEM,
            f"Question: {question}\n\nCandidates:\n{listing}\n\n"
            'Return JSON: [{"index": int, "relevance": 0..1, "reason": str}] with one '
            "entry per candidate. relevance is the probability the document contains a "
            "quotable sentence that directly answers the question. reason is at most "
            "twelve words.",
            max_tokens=200 + 60 * len(refs),
        )
    except LLMUnavailable as exc:
        log.info("ranking fell back to term overlap: %s", exc)
        return rank_deterministic(refs, terms)

    scores: dict[int, float] = {}
    for item in result or []:
        try:
            idx, rel = int(item.get("index")), float(item.get("relevance", 0.0))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= idx < len(refs):
            scores[idx] = max(0.0, min(rel, 1.0))
    if not scores:
        return rank_deterministic(refs, terms)

    # A candidate the model skipped keeps its overlap score, scaled down so it
    # sorts below anything the model actually rated.
    scored = [
        (r, scores.get(i, 0.5 * _overlap_score(r, terms) / 3.0))
        for i, r in enumerate(refs)
    ]
    scored.sort(key=lambda x: (-x[1], x[0].tier))
    return scored
