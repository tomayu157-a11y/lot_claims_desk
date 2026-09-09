"""Turns retrieved documents into Evidence carrying verbatim quotes.

Integrity rule: a quote must appear in the retrieved text. When a model
proposes a quote, it is verified against the source before it is accepted; an
unverifiable quote is dropped rather than downgraded, because a fabricated
quote attached to a real URL is worse than no evidence at all.
"""
from __future__ import annotations

import logging
import re

from ..models import Evidence, EvidenceOrigin, SourceRef, VerificationTag
from ..settings import get_thresholds
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.extraction")

_SYSTEM = (
    "You extract evidence for clinical desk research. You select sentences that already "
    "exist in the supplied document text. You never paraphrase, never merge sentences, "
    "and never write a sentence that is not present verbatim in the text. If the document "
    "does not address the question, you return an empty list."
)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
_WORD = re.compile(r"[a-z0-9]+")


_TAGS = re.compile(r"<[^>]{1,200}>")
_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
    "&apos;": "'", "&nbsp;": " ", "&ndash;": "-", "&mdash;": "-",
}


def _normalise(text: str) -> str:
    """Collapse whitespace and strip inline markup.

    Europe PMC abstracts and some SPL sections embed HTML. Left in place it
    ends up inside a quote presented to the reader as verbatim source text.
    """
    text = text or ""
    if "<" in text:
        text = _TAGS.sub(" ", text)
    if "&" in text:
        for entity, char in _ENTITIES.items():
            text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def document_text(ref: SourceRef) -> str:
    """Every connector puts quotable text somewhere. Collect it in one place."""
    parts: list[str] = [ref.snippet or ""]
    raw = ref.raw or {}
    for key in (
        "abstract", "abstractText", "text", "content", "markdown", "body",
        "indications_and_usage", "adverse_reactions", "warnings_and_precautions",
        "dosage_and_administration", "clinical_studies", "description",
        "briefSummary", "detailedDescription", "eligibilityCriteria", "summary",
    ):
        val = raw.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, list):
            parts += [v for v in val if isinstance(v, str)]
    for val in raw.get("sections", []) if isinstance(raw.get("sections"), list) else []:
        if isinstance(val, dict):
            parts.append(str(val.get("text", "")))
        elif isinstance(val, str):
            parts.append(val)
    return _normalise(" ".join(p for p in parts if p))


def _sentences(text: str, min_len: int) -> list[str]:
    out = []
    for raw_sent in _SENT_SPLIT.split(text):
        s = _normalise(raw_sent)
        if min_len <= len(s) <= 600:
            out.append(s)
    return out


_OFF_TOPIC = re.compile(
    r"\b(in rats?|in mice|in dogs?|animal data|carcinogenes[ie]s|mutagenesis|"
    r"impairment of fertility|pregnancy category|nursing mothers)\b", re.I
)


def _score_sentence(sentence: str, terms: set[str]) -> float:
    words = set(_WORD.findall(sentence.lower()))
    if not words or not terms:
        return 0.0
    overlap = len(words & terms)
    if not overlap:
        # No topical connection to the question. Numbers alone are not evidence.
        return 0.0
    base = overlap / (len(terms) ** 0.5)
    # Quantities matter in this domain, but only as a tiebreaker between
    # sentences that are already on topic.
    numeric = len(re.findall(r"\d[\d,.]*\s*(?:%|per\s+100,?000)?", sentence))
    score = base + 0.15 * min(numeric, 3)
    if _OFF_TOPIC.search(sentence):
        # Preclinical and subpopulation boilerplate is rarely the answer to a
        # clinical desk-research question.
        score *= 0.25
    return score


def question_terms(question: str, aspects: list[str], synonyms: list[str]) -> set[str]:
    blob = " ".join([question, *aspects, *synonyms]).lower()
    stop = {
        "what", "which", "how", "are", "the", "and", "for", "with", "from", "that",
        "this", "does", "have", "has", "into", "used", "use", "including", "their",
        "there", "where", "when", "who", "why", "was", "were", "been", "being",
        "united", "states", "adult", "patients", "population",
    }
    return {w for w in _WORD.findall(blob) if len(w) > 3 and w not in stop}


def _tag_for(ref: SourceRef) -> VerificationTag:
    return (
        VerificationTag.GENERAL_KNOWLEDGE
        if ref.origin is EvidenceOrigin.OPEN_WEB
        else VerificationTag.VERIFIED
    )


def _build(ref: SourceRef, question_id: str, quote: str, relevance: float) -> Evidence:
    return Evidence(
        question_id=question_id,
        source_id=ref.source_id,
        source_name=ref.source_name,
        organization=ref.organization or ref.source_name,
        tier=ref.tier,
        url=ref.url,
        title=ref.title,
        published=ref.published,
        quote=quote,
        origin=ref.origin,
        tag=_tag_for(ref),
        relevance=round(min(relevance, 1.0), 3),
        identifiers=ref.identifiers,
    )


def extract_deterministic(
    ref: SourceRef, question_id: str, terms: set[str], max_quotes: int = 2
) -> list[Evidence]:
    cfg = get_thresholds()["sufficiency"]
    text = document_text(ref)
    if not text:
        return []
    scored = [
        (_score_sentence(s, terms), s)
        for s in _sentences(text, cfg["min_quote_length"])
    ]
    scored = [(sc, s) for sc, s in scored if sc > 0.15]
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[:max_quotes]
    if not best:
        return []
    top = best[0][0] or 1.0
    return [_build(ref, question_id, s, sc / (top * 1.35)) for sc, s in best]


async def extract_with_llm(
    ref: SourceRef, question: str, question_id: str, terms: set[str], max_quotes: int = 2
) -> list[Evidence]:
    text = document_text(ref)
    if not text:
        return []
    excerpt = text[:12000]
    try:
        result = await llm.complete_json(
            _SYSTEM,
            f"Question: {question}\n\nDocument title: {ref.title}\n"
            f"Source: {ref.source_name}\n\nDocument text:\n{excerpt}\n\n"
            f"Return JSON: [{{\"quote\": str, \"relevance\": 0..1}}]. "
            f"At most {max_quotes} quotes, each copied character-for-character from the "
            f"document text above, each a complete sentence, each directly relevant to "
            f"the question. Empty list if the document does not address it.",
            max_tokens=1200,
        )
    except LLMUnavailable:
        return extract_deterministic(ref, question_id, terms, max_quotes)

    haystack = _normalise(text).lower()
    out: list[Evidence] = []
    for item in (result or [])[:max_quotes]:
        quote = _normalise(str(item.get("quote", "")))
        if len(quote) < get_thresholds()["sufficiency"]["min_quote_length"]:
            continue
        # Integrity gate: the quote must genuinely be in the document.
        if quote.lower() not in haystack:
            log.debug("dropped unverifiable quote from %s", ref.url)
            continue
        try:
            rel = float(item.get("relevance", 0.6))
        except (TypeError, ValueError):
            rel = 0.6
        out.append(_build(ref, question_id, quote, rel))
    return out or extract_deterministic(ref, question_id, terms, max_quotes)


async def extract(
    refs: list[SourceRef], question: str, question_id: str, terms: set[str]
) -> list[Evidence]:
    limits = get_thresholds()["limits"]
    per_source_cap = limits.get("max_evidence_items_per_source", 3)
    total_cap = limits["max_evidence_items_per_question"]

    collected: list[Evidence] = []
    seen: set[str] = set()
    for ref in refs:
        items = (
            await extract_with_llm(ref, question, question_id, terms)
            if llm.available
            else extract_deterministic(ref, question_id, terms)
        )
        for ev in items:
            fingerprint = ev.quote[:120].lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            collected.append(ev)

    # Round-robin across sources rather than taking the globally top-scoring
    # quotes. Taking the top N alone let one very long document win every slot,
    # which produced findings that cited five sources but quoted one.
    by_source: dict[str, list[Evidence]] = {}
    for ev in sorted(collected, key=lambda e: (e.tier, -e.relevance)):
        by_source.setdefault(ev.source_id, []).append(ev)

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
