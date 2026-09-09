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


# Words that signal a question is asking for a quantity. Only then do numbers
# in a sentence earn credit; otherwise an incidence figure outranks a
# diagnostic definition for a question about diagnosis.
_QUANTITATIVE = {
    "incidence", "prevalence", "rate", "rates", "survival", "mortality", "cases",
    "deaths", "percent", "percentage", "proportion", "share", "median", "mean",
    "risk", "frequency", "dose", "dosing", "dosage", "duration", "cost", "costs",
    "estimate", "estimated", "number", "count", "statistics", "epidemiology",
}

_STOP = {
    "what", "which", "how", "are", "the", "and", "for", "with", "from", "that",
    "this", "does", "have", "has", "into", "used", "use", "including", "their",
    "there", "where", "when", "who", "why", "was", "were", "been", "being",
    "united", "states", "adult", "adults", "patients", "population", "research",
    "claims", "real", "world", "standard", "current", "relevant", "principal",
}


_FOCUS_SYNONYMS: dict[str, set[str]] = {
    "incidence": {"rate", "rates", "cases", "diagnosed"},
    "prevalence": {"living", "prevalent"},
    "mortality": {"death", "deaths", "died", "dying"},
    "survival": {"surviving", "survive", "survived"},
    "diagnosed": {"diagnosis", "diagnostic"},
    "diagnosis": {"diagnosed", "diagnostic"},
    "treatment": {"treated", "therapy", "regimen", "regimens"},
    "therapies": {"therapy", "treatment", "regimen", "regimens", "agents"},
    "approved": {"approval", "indicated", "indication"},
    "codes": {"code"},
}


class TermSet:
    """Two tiers of query terms.

    `focus` is what distinguishes this question from every other question about
    the same disease: "diagnosis", "workup", "immunophenotype". `context` is the
    disease name and upstream entities, which every relevant sentence shares and
    which therefore cannot rank one sentence above another. Mixing the two into
    one set was the defect that put an incidence figure at the top of every
    card in a stage.
    """

    __slots__ = ("focus", "context", "quantitative")

    def __init__(self, focus: set[str], context: set[str]) -> None:
        self.focus = focus
        self.context = context
        self.quantitative = bool(focus & _QUANTITATIVE)

    # Backwards compatibility for callers that treat the term set as a flat set.
    def __iter__(self):
        return iter(self.focus | self.context)

    def __len__(self) -> int:
        return len(self.focus | self.context)

    def __contains__(self, item: object) -> bool:
        return item in self.focus or item in self.context

    def __and__(self, other: set) -> set:
        return (self.focus | self.context) & other

    def __rand__(self, other: set) -> set:
        return self.__and__(other)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 3 and w not in _STOP}


def build_terms(
    question: str, aspects: list[str], synonyms: list[str], upstream: list[str] | None = None,
) -> TermSet:
    context = _words(" ".join([*synonyms, *(upstream or [])]))
    # A disease word inside the question text is context, not focus.
    focus = _words(" ".join([question, *aspects])) - context
    # Sources state quantities in their own words: "rate of new cases" for
    # incidence, "living with" for prevalence. Widen the focus accordingly.
    for word in list(focus):
        focus |= _FOCUS_SYNONYMS.get(word, set())
    return TermSet(focus - context, context)


def question_terms(question: str, aspects: list[str], synonyms: list[str]) -> TermSet:
    """Kept for callers of the old name; builds the two-tier set."""
    return build_terms(question, aspects, synonyms)


def _as_termset(terms) -> TermSet:
    if isinstance(terms, TermSet):
        return terms
    return TermSet(set(terms or ()), set())


def _score_sentence(sentence: str, terms) -> float:
    ts = _as_termset(terms)
    words = set(_WORD.findall(sentence.lower()))
    if not words:
        return 0.0
    focus_hits = len(words & ts.focus)
    context_hits = len(words & ts.context)
    if ts.focus and not focus_hits:
        # On the disease but not on what was asked. Keep it as weak evidence
        # so recall survives when a source answers in different words, but cap
        # it below any sentence with a focus hit so it can never headline.
        return min(0.20, 0.07 * context_hits) if context_hits else 0.0
    base = focus_hits / (len(ts.focus) ** 0.5) if ts.focus else context_hits / max(len(ts.context), 1) ** 0.5
    score = max(base, 0.21) + 0.03 * min(context_hits, 3)
    if ts.quantitative:
        numeric = len(re.findall(r"\d[\d,.]*\s*(?:%|per\s+100,?000)?", sentence))
        score += 0.15 * min(numeric, 3)
    if _OFF_TOPIC.search(sentence):
        # Preclinical and subpopulation boilerplate is rarely the answer to a
        # clinical desk-research question.
        score *= 0.25
    return score


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


async def extract_batch(
    refs: list[SourceRef], question: str, question_id: str, terms: set[str],
    max_quotes: int = 3,
) -> list[Evidence]:
    """Extract from several documents in ONE model call.

    One call per document was the dominant cost of a run. Batching three
    documents per call cuts that by two thirds with no loss of the integrity
    gate: each returned quote is still verified character-for-character against
    the document it claims to come from, and an unverifiable quote is dropped.
    """
    docs = [(ref, document_text(ref)) for ref in refs]
    docs = [(ref, text) for ref, text in docs if text]
    if not docs:
        return []
    if not llm.available:
        out: list[Evidence] = []
        for ref, _ in docs:
            out += extract_deterministic(ref, question_id, terms, max_quotes)
        return out

    per_doc_budget = max(3000, 12000 // len(docs))
    listing = "\n\n".join(
        f"=== DOCUMENT {i} ===\nTitle: {ref.title}\nSource: {ref.source_name}\n"
        f"{text[:per_doc_budget]}"
        for i, (ref, text) in enumerate(docs)
    )
    try:
        result = await llm.complete_json(
            _SYSTEM,
            f"Question: {question}\n\n{listing}\n\n"
            'Return JSON: [{"document": int, "quote": str, "relevance": 0..1}]. '
            f"At most {max_quotes} quotes per document, each copied character-for-character "
            "from that document's text above, each a complete sentence, each directly "
            "relevant to the question. Omit a document entirely if it does not address "
            "the question.",
            max_tokens=600 + 500 * len(docs),
        )
    except LLMUnavailable:
        out = []
        for ref, _ in docs:
            out += extract_deterministic(ref, question_id, terms, max_quotes)
        return out

    min_len = get_thresholds()["sufficiency"]["min_quote_length"]
    haystacks = {i: _normalise(text).lower() for i, (_, text) in enumerate(docs)}
    counts: dict[int, int] = {}
    out = []
    for item in result or []:
        try:
            idx = int(item.get("document"))
        except (TypeError, ValueError, AttributeError):
            continue
        if idx not in haystacks or counts.get(idx, 0) >= max_quotes:
            continue
        quote = _normalise(str(item.get("quote", "")))
        if len(quote) < min_len or quote.lower() not in haystacks[idx]:
            continue
        try:
            rel = float(item.get("relevance", 0.6))
        except (TypeError, ValueError):
            rel = 0.6
        counts[idx] = counts.get(idx, 0) + 1
        out.append(_build(docs[idx][0], question_id, quote, rel))

    # A document the model returned nothing for still gets the deterministic
    # pass, so a terse model answer cannot silently discard a source.
    for i, (ref, _) in enumerate(docs):
        if i not in counts:
            out += extract_deterministic(ref, question_id, terms, max_quotes)
    return out


async def extract(
    refs: list[SourceRef], question: str, question_id: str, terms: set[str]
) -> list[Evidence]:
    limits = get_thresholds()["limits"]
    per_source_cap = limits.get("max_evidence_items_per_source", 3)
    total_cap = limits["max_evidence_items_per_question"]

    batch_size = max(1, int(limits.get("extract_batch_size", 3)))
    collected: list[Evidence] = []
    seen: set[str] = set()
    for start in range(0, len(refs), batch_size):
        # Offer at least as many candidates as the per-source cap can accept,
        # otherwise the cap is never the binding constraint and a source
        # contributes fewer quotes than the diversity rule allows.
        items = await extract_batch(
            refs[start:start + batch_size], question, question_id, terms,
            max_quotes=per_source_cap,
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
