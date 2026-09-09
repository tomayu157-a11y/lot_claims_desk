"""The answer-first pipeline.

Two guarantees are non-negotiable and are pinned here:
  1. An answer whose quotes cannot be found in the supplied documents is
     discarded, however plausible its prose.
  2. Citations name only documents that were actually in the batch.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.models import AnswerStatus, EvidenceOrigin, SourceRef
from celestra.services import answering
from celestra.services.answering import answer_batch, merge
from celestra.services.extraction import build_terms

DOC_A = SourceRef(
    source_id="seer", source_name="NCI SEER", organization="NCI SEER", tier=1,
    url="https://seer.cancer.gov/x", title="CLL Stat Facts",
    snippet="The overall rate of new cases of chronic lymphocytic leukemia was 4.7 per "
            "100,000 men and women per year based on 2018-2022 cases, age-adjusted.",
)
DOC_B = SourceRef(
    source_id="nci", source_name="NCI", organization="National Cancer Institute (NCI)", tier=1,
    url="https://cancer.gov/x", title="CLL Treatment (PDQ)",
    snippet="Five-year relative survival for chronic lymphocytic leukemia is 88.5 percent "
            "across all stages in the United States.",
)
Q = "What is the incidence and survival of CLL in the United States?"
TERMS = build_terms(Q, ["incidence", "survival"], ["chronic lymphocytic leukemia"])

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


class FakeLLM:
    """Stands in for the provider so the contract is tested, not the model."""

    def __init__(self, payload):
        self.payload = payload
        self.available = True
        self.calls = 0

    async def complete_json(self, system, prompt, **kw):
        self.calls += 1
        return self.payload


async def run_with(payload):
    original = answering.llm
    answering.llm = FakeLLM(payload)
    try:
        return await answer_batch(Q, ["incidence", "survival"], [DOC_A, DOC_B], "q1", TERMS)
    finally:
        answering.llm = original


async def main() -> int:
    print("\n== a supported answer is kept ==")
    ans, ev = await run_with({
        "status": "answered",
        "answer": "CLL incidence is 4.7 per 100,000 per year and five-year survival is 88.5%.",
        "aspects_covered": ["incidence", "survival"],
        "support": [
            {"document": 0, "quote": "The overall rate of new cases of chronic lymphocytic "
                                     "leukemia was 4.7 per 100,000 men and women per year "
                                     "based on 2018-2022 cases, age-adjusted.", "relevance": 0.95},
            {"document": 1, "quote": "Five-year relative survival for chronic lymphocytic "
                                     "leukemia is 88.5 percent across all stages in the "
                                     "United States.", "relevance": 0.9},
        ],
    })
    check("answer returned", ans is not None)
    check("status is answered", ans and ans.status is AnswerStatus.ANSWERED)
    check("two quotes verified", len(ev) == 2, f"{len(ev)} evidence")
    check("cites both documents", ans and set(ans.source_ids) == {"seer", "nci"},
          str(ans.source_ids if ans else None))
    check("citations use the organisation name",
          ans and "NCI SEER" in ans.citations, str(ans.citations if ans else None))
    check("evidence is attributed to the right source",
          {e.source_id for e in ev} == {"seer", "nci"})

    print("\n== a fabricated answer is discarded ==")
    ans, ev = await run_with({
        "status": "answered",
        "answer": "CLL incidence is 12.4 per 100,000 and median survival is 3 years.",
        "support": [
            {"document": 0, "quote": "The incidence of CLL is 12.4 per 100,000 persons "
                                     "annually according to the registry.", "relevance": 0.9},
        ],
    })
    check("unverifiable quote is dropped", not ev, f"{len(ev)} evidence")
    check("answer with no support is discarded", ans is None)

    print("\n== a partial answer keeps only its verified half ==")
    ans, ev = await run_with({
        "status": "partial",
        "answer": "CLL incidence is 4.7 per 100,000 per year.",
        "support": [
            {"document": 0, "quote": "The overall rate of new cases of chronic lymphocytic "
                                     "leukemia was 4.7 per 100,000 men and women per year "
                                     "based on 2018-2022 cases, age-adjusted.", "relevance": 0.9},
            {"document": 1, "quote": "Median survival is three years.", "relevance": 0.8},
        ],
    })
    check("partial answer kept", ans is not None and ans.status is AnswerStatus.PARTIAL)
    check("only the verified quote survives", len(ev) == 1, f"{len(ev)} evidence")
    check("citation names only the cited document",
          ans and ans.source_ids == ["seer"], str(ans.source_ids if ans else None))

    print("\n== not_found is an acceptable answer ==")
    ans, ev = await run_with({"status": "not_found", "answer": "", "support": []})
    check("no answer manufactured", ans is None)

    print("\n== merge consolidates batches ==")
    a1, _ = await run_with({
        "status": "partial", "answer": "Incidence is 4.7 per 100,000 per year.",
        "support": [{"document": 0, "quote": DOC_A.snippet, "relevance": 0.9}],
    })
    a2, _ = await run_with({
        "status": "partial", "answer": "Five-year survival is 88.5 percent.",
        "support": [{"document": 1, "quote": DOC_B.snippet, "relevance": 0.9}],
    })
    text, status, cites = merge([a1, a2])
    check("both halves survive the merge",
          "4.7" in text and "88.5" in text, text[:90])
    check("citations from both batches", len(cites) == 2, str(cites))
    check("merged status is partial", status is AnswerStatus.PARTIAL)
    text2, _, _ = merge([a1, a1])
    check("a repeated sentence is not duplicated", text2.count("4.7") == 1, text2)

    print("\n== approved evidence outranks supplementary ==")
    web = DOC_A.model_copy(deep=True)
    web.origin = EvidenceOrigin.OPEN_WEB
    original = answering.llm
    answering.llm = FakeLLM({
        "status": "answered", "answer": "From the open web.",
        "support": [{"document": 0, "quote": DOC_A.snippet, "relevance": 0.8}],
    })
    try:
        ans, _ = await answer_batch(Q, [], [web], "q1", TERMS)
    finally:
        answering.llm = original
    check("web-only answer is marked supplementary", ans and ans.is_supplementary)

    print("\n== no model configured ==")
    original = answering.llm

    class NoLLM:
        available = False

    answering.llm = NoLLM()
    try:
        ans, ev = await answer_batch(Q, ["incidence"], [DOC_A, DOC_B], "q1", TERMS)
    finally:
        answering.llm = original
    check("no answer prose is invented without a model", ans is None)
    check("but quotes are still extracted", len(ev) >= 1, f"{len(ev)} evidence")

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
