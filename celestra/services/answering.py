"""Answering a research question from a batch of documents.

This is the step that changed the shape of the pipeline. Extraction alone
returns quotes and leaves the reader to infer the answer; here the model is
asked the question directly against a batch of documents and must either
answer it with verbatim support or say the batch does not contain the answer.

Two rules make the answer trustworthy:

  1. Every quote is verified character-for-character against the document it
     is attributed to. An answer whose quotes all fail verification is thrown
     away, however plausible its prose.
  2. An answer cites only documents that were actually supplied in the batch.
     A citation to anything else is dropped.
"""
from __future__ import annotations

import logging
import re

from ..models import Answer, AnswerStatus, Evidence, EvidenceOrigin, SourceRef, VerificationTag
from ..settings import get_thresholds
from .extraction import _build, _normalise, document_text, extract_deterministic
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.answering")

_SYSTEM = (
    "You answer a clinical desk-research question from the documents supplied, and only "
    "from those documents. You quote verbatim to support every claim. You never use prior "
    "knowledge, never generalise beyond the text, and never fill a gap with a plausible "
    "statement. If the documents do not answer the question, you say so; that is a correct "
    "and useful answer, and inventing one is not."
)


def _citation_of(ref: SourceRef) -> str:
    return ref.organization or ref.source_name


async def answer_batch(
    question_text: str,
    aspects: list[str],
    refs: list[SourceRef],
    question_id: str,
    terms,
    batch_index: int = 0,
    round_index: int = 0,
    notes: list[str] | None = None,
) -> tuple[Answer | None, list[Evidence]]:
    """Ask one batch of documents the question.

    `notes` are what a reviewer wrote at the review gate. They frame the
    answer (which subtype, which setting, which year matters) but are never a
    source: every claim still has to be quoted from a document.

    Returns the answer (None when the batch does not answer it) and the
    verified evidence that supports it. The evidence is returned even when the
    answer is partial, because it still counts toward the threshold.
    """
    docs = [(ref, document_text(ref)) for ref in refs]
    docs = [(ref, text) for ref, text in docs if text]
    if not docs:
        return None, []

    if not llm.available:
        # No model: fall back to quote selection, which is what the
        # deterministic engine can honestly do. No answer prose is invented.
        evidence: list[Evidence] = []
        for ref, _ in docs:
            evidence += extract_deterministic(ref, question_id, terms, max_quotes=3)
        return None, evidence

    per_doc_budget = max(3000, 14000 // len(docs))
    listing = "\n\n".join(
        f"=== DOCUMENT {i} ===\n"
        f"Source: {_citation_of(ref)} ({ref.source_name}, tier {ref.tier})\n"
        f"Title: {ref.title}\n{text[:per_doc_budget]}"
        for i, (ref, text) in enumerate(docs)
    )
    aspect_line = (
        "A complete answer covers: " + "; ".join(aspects) + ".\n" if aspects else ""
    )
    note_line = ""
    if notes:
        note_line = (
            "Reviewer context (human-supplied framing; use it to focus the answer, "
            "never cite it as a source):\n"
            + "\n".join(f"- {n[:400]}" for n in notes[:6]) + "\n"
        )
    try:
        result = await llm.complete_json(
            _SYSTEM,
            f"Question: {question_text}\n{aspect_line}{note_line}\n{listing}\n\n"
            'Return JSON: {"status": "answered"|"partial"|"not_found", "answer": str, '
            '"aspects_covered": [str], "support": [{"document": int, "quote": str, '
            '"relevance": 0..1}]}.\n'
            '- "answered" only when the documents fully answer the question; "partial" '
            "when they answer some of it; \"not_found\" when they do not address it.\n"
            "- answer is 2-5 sentences, every claim traceable to a quote below it. Empty "
            'string when status is "not_found".\n'
            "- support quotes are copied character-for-character from the document text "
            "above, each a complete sentence. At most three per document.",
            max_tokens=900 + 550 * len(docs),
        )
    except LLMUnavailable:
        evidence = []
        for ref, _ in docs:
            evidence += extract_deterministic(ref, question_id, terms, max_quotes=3)
        return None, evidence

    result = result or {}
    status_raw = str(result.get("status", "not_found")).strip().lower()
    status = {
        "answered": AnswerStatus.ANSWERED,
        "partial": AnswerStatus.PARTIAL,
    }.get(status_raw, AnswerStatus.NOT_FOUND)

    # ---- verify every quote against the document it is attributed to -------
    min_len = get_thresholds()["sufficiency"]["min_quote_length"]
    haystacks = {i: _normalise(text).lower() for i, (_, text) in enumerate(docs)}
    per_doc: dict[int, int] = {}
    evidence: list[Evidence] = []
    for item in result.get("support") or []:
        try:
            idx = int(item.get("document"))
        except (TypeError, ValueError, AttributeError):
            continue
        if idx not in haystacks or per_doc.get(idx, 0) >= 3:
            continue
        quote = _normalise(str(item.get("quote", "")))
        if len(quote) < min_len or quote.lower() not in haystacks[idx]:
            log.debug("dropped unverifiable quote for %s", question_id)
            continue
        try:
            rel = float(item.get("relevance", 0.7))
        except (TypeError, ValueError):
            rel = 0.7
        per_doc[idx] = per_doc.get(idx, 0) + 1
        evidence.append(_build(docs[idx][0], question_id, quote, rel))

    text = _normalise(str(result.get("answer", "")))
    if status is AnswerStatus.NOT_FOUND or not text:
        # Still return whatever verified quotes came back: a batch that could
        # not answer may still hold support for another aspect.
        return None, evidence

    if not evidence:
        # Prose with nothing verifiable under it. This is exactly the failure
        # mode the whole design exists to prevent.
        log.info("discarded unsupported answer for %s", question_id)
        return None, []

    cited = [docs[i][0] for i in sorted(per_doc)]
    origin = (
        EvidenceOrigin.OPEN_WEB
        if all(r.origin is EvidenceOrigin.OPEN_WEB for r in cited)
        else EvidenceOrigin.APPROVED_API
    )
    answer = Answer(
        question_id=question_id,
        status=status,
        text=text,
        aspects_covered=[str(a) for a in (result.get("aspects_covered") or [])][:8],
        evidence_ids=[e.id for e in evidence],
        source_ids=list(dict.fromkeys(r.source_id for r in cited)),
        citations=list(dict.fromkeys(_citation_of(r) for r in cited)),
        origin=origin,
        batch_index=batch_index,
        round_index=round_index,
    )
    return answer, evidence


def merge(answers: list[Answer]) -> tuple[str, AnswerStatus, list[str]]:
    """Consolidate every batch answer for one question into one statement.

    Batches are read independently, so two can answer different parts of the
    same question. Merging keeps both rather than letting the last one win.
    """
    usable = [a for a in answers if a.text.strip()]
    if not usable:
        return "", AnswerStatus.NOT_FOUND, []

    # Approved sources first, then the fullest answers.
    usable.sort(key=lambda a: (a.is_supplementary, a.status is not AnswerStatus.ANSWERED,
                               -len(a.text)))
    parts: list[str] = []
    seen: set[str] = set()
    for a in usable:
        for sentence in re.split(r"(?<=[.!?])\s+", a.text.strip()):
            key = re.sub(r"[^a-z0-9]+", "", sentence.lower())[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            parts.append(sentence.strip())

    citations: list[str] = []
    for a in usable:
        for c in a.citations:
            if c not in citations:
                citations.append(c)

    status = (
        AnswerStatus.ANSWERED
        if any(a.status is AnswerStatus.ANSWERED for a in usable)
        else AnswerStatus.PARTIAL
    )
    return " ".join(parts), status, citations


def tag_for(answer: Answer | None) -> VerificationTag:
    if answer is None:
        return VerificationTag.NOT_VERIFIED
    if answer.is_supplementary:
        return VerificationTag.GENERAL_KNOWLEDGE
    return VerificationTag.VERIFIED
