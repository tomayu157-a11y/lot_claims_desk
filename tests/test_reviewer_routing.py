"""Routing reviewer files: one call per agent maps questions to file
sections; unknown ids are ignored; a per-question ceiling applies; the
orchestrator records the result on each question and in the live feed."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import celestra.services.orchestrator as orch_mod
import celestra.store as store_mod
from celestra.models import (
    AgentState,
    Insight,
    ResearchQuestion,
    ReviewerFile,
    ReviewerFileSection,
    Run,
    RunConfig,
)
from celestra.services import reviewer_routing as rr
from celestra.services.llm import LLMUnavailable
from celestra.store import Store

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


class FakeLLM:
    def __init__(self, reply=None, error=None, available=True):
        self.reply, self.error, self.available = reply, error, available
        self.calls, self.prompts = 0, []

    async def complete_json(self, system, prompt, **kw):
        self.calls += 1
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.reply


POLICY = ReviewerFile(
    id="rf_policy0001", filename="Payer policy.pdf", kind="pdf", size_bytes=1000,
    markdown="A" * 8000 + "TAILMARKER" + "B" * 2000,
    sections=[
        ReviewerFileSection(id="s1", heading="Scope", text="Covers adults with CLL.", page=1),
        ReviewerFileSection(id="s2", heading="Step therapy", text="BTK inhibitors first.", page=4),
    ],
)
CARD = Insight(stage="stage_2", bucket="C", category="Treatment", title="First-line regimens",
               summary="S", reviewer_input="Use the payer policy.", reviewer_files=[POLICY])
QS = [ResearchQuestion(id="q_a", stage="stage_4", bucket="D", text="Which regimens are used first line?"),
      ResearchQuestion(id="q_b", stage="stage_4", bucket="D", text="Which ICD-10 codes identify CLL?")]


def big_file(file_id: str, n: int, size: int) -> ReviewerFile:
    return ReviewerFile(id=file_id, filename=f"{file_id}.txt", kind="txt", size_bytes=1, markdown="x",
                        sections=[ReviewerFileSection(id=f"s{i}", heading=f"H{i}", text="y" * size)
                                  for i in range(1, n + 1)])


async def route(reply=None, error=None, files=None):
    fake = FakeLLM(reply, error)
    original = rr.llm
    rr.llm = fake
    try:
        out = await rr.route_for_agent("Treatment Logic Agent", "Treatment logic", QS,
                                       files if files is not None else rr.attached_files([CARD]))
    finally:
        rr.llm = original
    return out, fake


async def unit_checks():
    print("\n== what the routing call sees ==")
    out, fake = await route({"routes": []})
    prompt = fake.prompts[0]
    check("questions listed", "- q_a: Which regimens are used first line?" in prompt)
    check("outline lists every section", "rf_policy0001.s1 | Scope |" in prompt
          and "rf_policy0001.s2 | Step therapy |" in prompt)
    check("card and its input named", "First-line regimens" in prompt and "Use the payer policy." in prompt)
    check("opening capped at 8,000 chars", "A" * 8000 in prompt and "TAILMARKER" not in prompt)
    check("empty routes are an empty result", out == {})

    print("\n== the reply is checked ==")
    out, _ = await route({"routes": [
        {"question_id": "q_a", "sections": ["rf_policy0001.s2", "rf_policy0001.s9",
                                            "rf_policy0001.s2", "rf_policy0001.s1"]},
        {"question_id": "q_zzz", "sections": ["rf_policy0001.s1"]},
        {"question_id": "q_b", "sections": []},
    ]})
    check("unknown ids and duplicates dropped, order kept",
          out == {"q_a": ["rf_policy0001.s2", "rf_policy0001.s1"]}, str(out))
    out, _ = await route(error=LLMUnavailable("down"))
    check("model unavailable -> None", out is None)
    out, _ = await route(error=RuntimeError("boom"))
    check("any failure -> None", out is None)
    out, _ = await route("nonsense")
    check("malformed reply -> None", out is None)
    out, fake = await route({"routes": []}, files=[])
    check("no files -> no call", out == {} and fake.calls == 0)

    print("\n== the per-question ceiling ==")
    files = rr.attached_files([Insight(stage="s", bucket="C", category="T", title="Big", summary="S",
                                       reviewer_files=[big_file("rf_big", 3, 10_000)])])
    kept, dropped = rr.apply_ceiling(["rf_big.s3", "rf_big.s1", "rf_big.s2"], files, 24_000)
    check("keeps in priority order until the next would pass", kept == ["rf_big.s3", "rf_big.s1"], str(kept))
    check("the rest recorded as dropped", dropped == ["rf_big.s2"], str(dropped))

    print("\n== documents grouped by file ==")
    two = rr.attached_files([
        Insight(stage="s", bucket="C", category="T", title="Card one", summary="S",
                reviewer_files=[big_file("rf_one", 2, 10)]),
        Insight(stage="s", bucket="A", category="C", title="Card two", summary="S",
                reviewer_files=[big_file("rf_two", 1, 10)]),
    ])
    docs = rr.documents_for(["rf_one.s1", "rf_two.s1", "rf_one.s2"], two)
    check("grouped by first appearance", [d["filename"] for d in docs] == ["rf_one.txt", "rf_two.txt"])
    check("sections in routed order", [s["heading"] for s in docs[0]["sections"]] == ["H1", "H2"])
    check("card named", docs[1]["card"] == "Card two")
    check("one entry per attached file, across cards",
          len(two) == 2 and len(rr.attached_files([CARD])) == 1)


def orchestrator_checks():
    print("\n== the orchestrator routes when an agent starts ==")
    tmp = Path(tempfile.mkdtemp(prefix="celestra_route_"))
    test_store = Store(tmp / "t.db")
    saved_stores = (store_mod.store, orch_mod.store)
    store_mod.store = orch_mod.store = test_store
    run = Run(config=RunConfig(indication="Chronic Lymphocytic Leukemia", indication_key="CLL"))
    test_store.save_run(run)
    test_store.save_insights(run.id, [CARD.model_copy(update={"run_id": run.id})])
    questions = [q.model_copy() for q in QS]
    o = orch_mod.Orchestrator(run, {})
    state = AgentState(bucket="D", key="treatment-logic", name="Treatment Logic Agent",
                       tagline="", icon="flow")
    original = rr.llm
    try:
        rr.llm = FakeLLM({"routes": [{"question_id": "q_a",
                                      "sections": ["rf_policy0001.s2", "rf_policy0001.s1"]}]})
        docs = asyncio.run(o._route_reviewer_files(state, "D", questions))
        check("only the routed question gets documents", set(docs) == {"q_a"}, str(docs))
        check("  in priority order", [s["heading"] for s in docs["q_a"][0]["sections"]]
              == ["Step therapy", "Scope"])
        stored_q = {q.id: q for q in test_store.get_questions(run.id)}
        check("routing saved on the question", stored_q["q_a"].reviewer_sections
              == ["rf_policy0001.s2", "rf_policy0001.s1"])
        check("unrouted question saved empty", stored_q["q_b"].reviewer_sections == [])
        check("live feed says what happened",
              state.message == "Reviewer files: 1 file routed to 1 of 2 questions", state.message)

        rr.llm = FakeLLM(error=LLMUnavailable("down"))
        docs = asyncio.run(o._route_reviewer_files(state, "D", [q.model_copy() for q in QS]))
        check("failed routing sends nothing", docs == {})
        check("  and says so", "could not be routed" in state.message, state.message)

        fake = FakeLLM({"routes": []}, available=False)
        rr.llm = fake
        docs = asyncio.run(o._route_reviewer_files(state, "D", [q.model_copy() for q in QS]))
        check("no model: nothing routed, no call", docs == {} and fake.calls == 0)
    finally:
        rr.llm = original
        store_mod.store, orch_mod.store = saved_stores


def main() -> int:
    asyncio.run(unit_checks())
    orchestrator_checks()
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
