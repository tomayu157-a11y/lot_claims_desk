"""Retrieval policy: a partial answer does not stop the search, inventory
questions are read wide and in full, and code lookups always carry the
indication's claims lexicon."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.connectors.base import ConnectorResult
from celestra.models import (
    Answer, AnswerStatus, Evidence, EvidenceOrigin, ResearchQuestion, RunConfig, SourceRef,
)
from celestra.services import handoff, retrieval
from celestra.services import hydration, ranking

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


TEXT = ("Blinatumomab is indicated for CD19-positive B-cell precursor acute lymphoblastic "
        "leukemia in adults and children. Inotuzumab ozogamicin is indicated for relapsed or "
        "refractory B-cell precursor acute lymphoblastic leukemia. " * 3)


class Src:
    def __init__(self, sid, tier, n_docs, origin=EvidenceOrigin.APPROVED_API):
        self.sid, self.tier, self.n, self.origin = sid, tier, n_docs, origin
        self.limits: list[int] = []

    async def discover(self, ctx, limit):
        self.limits.append(limit)
        refs = [SourceRef(source_id=self.sid, source_name=self.sid, tier=self.tier,
                          url=f"https://{self.sid}.example/{i}", title=f"Label {i} ALL",
                          organization=self.sid, snippet=TEXT, origin=self.origin,
                          raw={"text": TEXT, "abstract": TEXT})
                for i in range(min(limit, self.n))]
        return ConnectorResult(source_id=self.sid, refs=refs, ok=bool(refs), reason="")


class FakeLLM:
    available = True

    async def complete_json(self, *a, **k):
        return {"query": "acute lymphoblastic leukemia approved therapies"}


def run(question_text: str, api_status: AnswerStatus, targeted_status: AnswerStatus):
    api_ids = [s["id"] for s in retrieval.sources_for("stage_2", "ALL")]
    targeted_ids = [s["id"] for s in retrieval.targeted_sources_for("stage_2", "ALL")]
    registry = {sid: Src(sid, 1, 30) for sid in api_ids}
    for sid in targeted_ids:
        registry[sid] = Src(sid, 1, 3, EvidenceOrigin.TARGETED_SEARCH)
    registry["open_web"] = Src("open_web", 3, 0, EvidenceOrigin.OPEN_WEB)
    calls: list[str] = []

    async def fake_answer(qtext, aspects, refs, qid, terms, batch_index=0, round_index=0, notes=None, **kw):
        calls.append(refs[0].source_id)
        status = targeted_status if refs[0].origin is EvidenceOrigin.TARGETED_SEARCH else api_status
        ev = [Evidence(question_id=qid, source_id=r.source_id, source_name=r.source_name, tier=r.tier,
                       url=r.url, quote=TEXT[:160], origin=r.origin, relevance=0.8) for r in refs]
        ans = Answer(question_id=qid, status=status, text="Blinatumomab and inotuzumab are approved.",
                     evidence_ids=[e.id for e in ev], source_ids=[r.source_id for r in refs],
                     citations=[r.source_name for r in refs])
        return ans, ev

    async def fake_rank(refs, qtext, terms):
        return [(r, 0.9) for r in refs]

    async def fake_hydrate(ref, registry):
        return ref

    real = (retrieval.answer_batch, ranking.rank, hydration.hydrate, retrieval.llm)
    retrieval.answer_batch, ranking.rank, hydration.hydrate, retrieval.llm = (
        fake_answer, fake_rank, fake_hydrate, FakeLLM())
    try:
        q = ResearchQuestion(stage="stage_2", bucket="C", text=question_text, seed_text=question_text,
                             aspects=["approved agents", "label indications"])
        cfg = RunConfig(indication="Acute Lymphoblastic Leukemia", indication_key="ALL")
        outcome = asyncio.run(retrieval.retrieve(q, cfg, ["ALL"], registry))
    finally:
        retrieval.answer_batch, ranking.rank, hydration.hydrate, retrieval.llm = real
    return outcome, registry, calls, api_ids, targeted_ids


def main() -> int:
    print("\n== inventory questions ==")
    check("approved-therapy question is an inventory",
          retrieval.is_inventory_question("Which therapies are FDA-approved for ALL and what are their label indications?"))
    check("code question is an inventory",
          retrieval.is_inventory_question("What HCPCS J-codes and NDC identifiers map to ALL regimen components?"))
    check("a fact question is not", not retrieval.is_inventory_question("What is the incidence of ALL in the US?"))

    outcome, registry, calls, api_ids, _ = run(
        "Which therapies are FDA-approved for ALL and what are their label indications?",
        AnswerStatus.ANSWERED, AnswerStatus.ANSWERED)
    first = registry[api_ids[0]]
    check("inventory question asks each source for many candidates, not two",
          first.limits and first.limits[0] >= 20, str(first.limits[:1]))
    check("every batch is read even after an answer", len(calls) >= 4, f"{len(calls)} batches")
    check("answered from the API tier; no domain search needed",
          not outcome.used_targeted and outcome.answers)

    print("\n== a partial answer keeps going ==")
    outcome, registry, calls, api_ids, targeted_ids = run(
        "What is the incidence, prevalence, survival and mortality of ALL in the United States?",
        AnswerStatus.PARTIAL, AnswerStatus.ANSWERED)
    check("API tier gave only partial answers, so domain search was consulted",
          outcome.used_targeted, str(outcome.used_targeted))
    check("the partial answers were kept and merged with the full one",
          sum(1 for a in outcome.answers if a.status is AnswerStatus.PARTIAL) >= 1
          and any(a.status is AnswerStatus.ANSWERED for a in outcome.answers))
    check("stops once a full answer arrives; open web untouched", not outcome.used_web)

    outcome, registry, calls, api_ids, _ = run(
        "What is the incidence, prevalence, survival and mortality of ALL in the United States?",
        AnswerStatus.ANSWERED, AnswerStatus.ANSWERED)
    check("a full answer from the API tier stops there", not outcome.used_targeted and len(calls) == 1,
          f"{len(calls)} batches")

    print("\n== claims lexicon ==")
    ctx = handoff.with_lexicon("B", "ALL", handoff.for_agent("B", {}))
    check("code agent receives test names even when discovery extracted none",
          "bone marrow biopsy" in ctx.get("test_names", []) and "flow cytometry" in ctx.get("test_names", []))
    ctx = handoff.with_lexicon("D", "ALL", {"drugs": ["novel-agent"]})
    check("extracted entities are kept on top of the lexicon",
          "novel-agent" in ctx["drugs"] and "blinatumomab" in ctx["drugs"] and "tisagenlecleucel" in ctx["drugs"])
    check("discovery agents receive no lexicon", handoff.with_lexicon("A", "ALL", {}) == {})

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
