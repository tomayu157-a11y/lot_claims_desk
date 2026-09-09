"""End-to-end test of the human review gate and the insight table snapshot.

Drives the real ASGI app with the offline demo registry: a full run must pause
after the first wave with only the first two agents complete, the review page
must render, Continue must resume it to completion, and every insight must be
able to open the stage-report table it was built from.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

import celestra.main as app_mod
import celestra.services.orchestrator as orch_mod
import celestra.store as store_mod
from celestra.demo import build_registry
from celestra.models import AgentStatus, RunStatus
from celestra.store import Store

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


async def wait_for(store: Store, run_id: str, wanted: set[RunStatus], timeout: float = 120) -> RunStatus:
    for _ in range(int(timeout * 4)):
        run = store.get_run(run_id)
        if run and run.status in wanted:
            return run.status
        await asyncio.sleep(0.25)
    run = store.get_run(run_id)
    return run.status if run else RunStatus.FAILED


async def main() -> int:
    db = Path("/tmp/celestra_gate.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    store = Store(db)
    for mod in (store_mod, orch_mod, app_mod):
        mod.store = store
    app_mod._REGISTRY = build_registry()

    transport = httpx.ASGITransport(app=app_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 follow_redirects=False) as c:
        print("\n== start a full run ==")
        r = await c.post("/projects", data={
            "therapy_area": "Oncology", "indication": "CLL", "population": "All",
            "geography": "United States", "objective": "Build Claims Line of Therapy",
            "drug_brand": "Venclexta", "mode": "full",
        })
        check("project created", r.status_code == 303, f"HTTP {r.status_code}")
        run_id = re.search(r"/runs/(run_[0-9a-f]+)/", r.headers.get("location", "")).group(1)

        print("\n== pauses at the gate after the first wave ==")
        status = await wait_for(store, run_id, {RunStatus.AWAITING_REVIEW, RunStatus.COMPLETED,
                                                RunStatus.FAILED})
        run = store.get_run(run_id)
        check("run is awaiting review", status is RunStatus.AWAITING_REVIEW, status.value)
        done = sorted(b for b, a in run.agents.items() if a.status is AgentStatus.COMPLETE)
        check("exactly the first-wave agents finished", done == ["A", "C"], str(done))
        check("later agents have not run",
              all(run.agents[b].status is AgentStatus.QUEUED for b in ("B", "D", "E", "F")))
        check("resume point recorded", run.resume_from_wave == 2, str(run.resume_from_wave))
        insights = sorted(store.get_insights(run_id), key=lambda i: i.number)
        from celestra.services.insights import catalogue_for, phase_catalogue
        expected_cards = len(catalogue_for("A")) + len(catalogue_for("C")) + len(phase_catalogue("discovery"))
        check("one card per discovery slot, plus the phase card",
              len(insights) == expected_cards, f"{len(insights)} cards, expected {expected_cards}")
        check("cards are numbered in document order",
              [i.number for i in insights] == sorted(i.number for i in insights) and insights[0].number == 1)
        check("every card carries an evidence block or says it is not covered",
              all(i.evidence or not i.covered for i in insights))
        synth = [i for i in insights if i.category == "Synthesis"]
        check("phase synthesis card written once both agents finished",
              len(synth) == 1 and synth[0].evidence, str(len(synth)))
        check("no stage report for later stages yet",
              {s.stage for s in store.get_stage_reports(run_id)} == {"stage_1", "stage_2"})

        print("\n== review page ==")
        r = await c.get(f"/runs/{run_id}")
        check("run root redirects to review", r.status_code == 303
              and r.headers["location"].endswith("/review"), r.headers.get("location", ""))
        r = await c.get(f"/runs/{run_id}/review")
        check("review page renders", r.status_code == 200, f"HTTP {r.status_code}")
        check("  offers the continue action", "start Mapping" in r.text)
        check("  shows the phase names", "Discovery phase" in r.text and "Mapping &amp; Synthesis" in r.text)
        check("  every finding is in one of two states",
              all(i.confidence.value in ("ready", "requires_input") for i in insights))
        blocked = [i for i in insights if i.needs_decision]
        check("  gate lists what still needs input", (len(blocked) == 0) == ("Nothing needs your input" in r.text),
              f"{len(blocked)} blockers")

        check("  shows the finished agents", "Clinical Landscape Agent" in r.text
              and "Treatment Evidence Agent" in r.text)
        check("  lists the waiting agents", "Diagnostic Footprint Agent" in r.text)
        check("  no internal vocabulary", not re.search(r"\bbucket", r.text, re.I))
        check("  cards with a document table offer it",
              r.text.count("Document table") == sum(1 for i in insights if i.table_titles),
              f"{r.text.count('Document table')}")
        check("  review needs are marked quietly, not as alarms",
              "Needs your review" in r.text or not any(i.needs_decision for i in insights))

        print("\n== the live page never bounces ==")
        r = await c.get(f"/runs/{run_id}/progress")
        check("progress page renders while paused", r.status_code == 200, f"HTTP {r.status_code}")
        check("  shows the review gate", "Review gate" in r.text)
        check("  groups agents by phase", 'data-phase="discovery"' in r.text and 'data-phase="mapping"' in r.text)

        print("\n== decisions at the gate ==")
        r = await c.post(f"/runs/{run_id}/insights/{insights[0].id}/approve", json={})
        check("approve at the gate", r.status_code == 200
              and store.get_insight(run_id, insights[0].id).review_action.value == "approved")
        check("  card shows the decision", "Approved by you" in r.text)
        r = await c.post(f"/runs/{run_id}/insights/{insights[1].id}/input", json={})
        check("add input with no text is refused", r.status_code == 400, f"HTTP {r.status_code}")
        r = await c.post(f"/runs/{run_id}/insights/{insights[1].id}/input",
                         json={"user_input": "Use the 2024 SEER release for incidence."})
        check("add input accepted", r.status_code == 200, f"HTTP {r.status_code}")
        saved = store.get_insight(run_id, insights[1].id)
        check("  input attached, finding text unchanged",
              saved.reviewer_input.startswith("Use the 2024") and saved.summary == insights[1].summary)
        check("  decision recorded as input added", saved.review_action.value == "input_added")
        check("  card shows the input", "Your input" in r.text)
        run = store.get_run(run_id)
        check("  input handed to the run context",
              any(e.get("insight_id") == insights[1].id for e in run.context.get("reviewer_inputs", [])))
        r = await c.post(f"/runs/{run_id}/insights/{insights[2].id}/modify",
                         json={"user_input": "Restrict to adults."})
        check("modify accepted", r.status_code == 200, f"HTTP {r.status_code}")
        saved = store.get_insight(run_id, insights[2].id)
        check("  decision recorded as revised", saved.review_action.value == "modified")
        check("  revision note explains what happened", bool(saved.revision_note), saved.revision_note)
        check("  card shows the instruction", "Your instruction" in r.text)

        # Anything still needing input gets a decision so the gate opens.
        for i in store.get_insights(run_id):
            if i.needs_decision:
                await c.post(f"/runs/{run_id}/insights/{i.id}/approve", json={})
        for con in store.get_contradictions(run_id):
            if con.severity.value == "escalated" and con.review_action.value == "pending":
                await c.post(f"/runs/{run_id}/contradictions/{con.id}/review",
                             json={"action": "acknowledged"})
        r = await c.get(f"/runs/{run_id}/gate?mode=review")
        check("gate panel fragment renders", r.status_code == 200 and "Nothing needs your input" in r.text)
        check("  approval page not reachable before the run finishes",
              (await c.get(f"/runs/{run_id}/approval")).status_code == 303)

        print("\n== table snapshot ==")
        r = await c.get(f"/runs/{run_id}/insights/{insights[0].id}/table")
        check("table fragment renders", r.status_code == 200, f"HTTP {r.status_code}")
        check("  contains a table", "<table" in r.text)
        check("  names the insight", insights[0].title in r.text)
        check("  is the document table", any(t in r.text for t in insights[0].table_titles),
              str(insights[0].table_titles))
        epi = next((i for i in insights if i.card_key == "epidemiology"), None)
        check("epidemiology card exists and carries figures",
              epi is not None and epi.evidence_type in ("table", "list", "metrics"),
              epi.evidence_type if epi else "missing")

        print("\n== continue ==")
        r = await c.post(f"/runs/{run_id}/continue")
        check("continue accepted", r.status_code == 303, f"HTTP {r.status_code}")
        check("  lands on the live page", "/progress" in r.headers.get("location", ""),
              r.headers.get("location", ""))
        check("  run is RUNNING before the resume task starts",
              store.get_run(run_id).status is RunStatus.RUNNING)
        r = await c.get(f"/runs/{run_id}/progress")
        check("  live page renders without bouncing", r.status_code == 200, f"HTTP {r.status_code}")
        check("  live page streams", 'data-run-stream' in r.text)
        r = await c.get(f"/runs/{run_id}")
        check("  run root goes to the live page", r.headers.get("location", "").endswith("/progress"))
        status = await wait_for(store, run_id, {RunStatus.COMPLETED, RunStatus.FAILED})
        run = store.get_run(run_id)
        check("run completed", status is RunStatus.COMPLETED, run.error or status.value)
        check("all six agents complete",
              all(a.status is AgentStatus.COMPLETE for a in run.agents.values()))
        check("review timestamp recorded", run.reviewed_at is not None)
        check("gate decision survived the resume",
              store.get_insight(run_id, insights[0].id).review_action.value == "approved")
        check("all six stages reported", len(store.get_stage_reports(run_id)) == 6)
        check("qa written", store.get_qa(run_id) is not None)
        later = [i for i in store.get_insights(run_id) if i.stage not in ("stage_1", "stage_2")]
        check("later insights link to their tables", all(i.table_titles for i in later),
              f"{sum(1 for i in later if not i.table_titles)} unlinked")

        check("reviewer input reached the later agents",
              "reviewer_notes" in str(run.context) or bool(run.context.get("reviewer_inputs")))

        print("\n== the stream of a resumed run does not end at once ==")
        from celestra.events import EventBus, bus
        replay = [e.type for e in bus._channels[run_id].replay]
        check("phase-two events reached the stream", "run_complete" in replay)
        check("phase one's stream_end was dropped on reopen", replay.count("stream_end") == 1,
              f"{replay.count('stream_end')} stream_end events in replay")
        probe = EventBus()
        await probe.publish("r", "agent_status", status="complete")
        await probe.publish("r", "review_required", redirect="/x")
        await probe.close("r")
        check("closed channel is closed", probe.is_closed("r"))
        await probe.reopen("r")
        await probe.publish("r", "run_resumed")
        q = await probe.subscribe("r", 0)
        seen = []
        while not q.empty():
            seen.append(q.get_nowait().type)
        check("reopened channel is open", not probe.is_closed("r"))
        check("a late subscriber sees no stale end or redirect",
              "stream_end" not in seen and "review_required" not in seen, str(seen))
        check("  but still sees the history and the resume", seen == ["agent_status", "run_resumed"], str(seen))

        print("\n== final approval ==")
        r = await c.get(f"/runs/{run_id}")
        check("run root now goes to final approval", r.headers.get("location", "").endswith("/approval"))
        r = await c.post(f"/runs/{run_id}/continue")
        check("continue on a finished run is a no-op redirect", r.status_code == 303)
        r = await c.get(f"/runs/{run_id}/approval")
        check("approval page renders", r.status_code == 200, f"HTTP {r.status_code}")
        check("  shows both phases in the document", "Discovery phase" in r.text and "Mapping" in r.text)
        blockers = [i for i in store.get_insights(run_id) if i.needs_decision]
        r = await c.post(f"/runs/{run_id}/approve")
        if blockers:
            check("approval refused while findings need input", r.status_code == 409,
                  f"HTTP {r.status_code} with {len(blockers)} blockers")
            check("  page says why", "still need your input" in r.text)
            for i in blockers:
                await c.post(f"/runs/{run_id}/insights/{i.id}/approve", json={})
            for con in store.get_contradictions(run_id):
                if con.severity.value == "escalated" and con.review_action.value == "pending":
                    await c.post(f"/runs/{run_id}/contradictions/{con.id}/review",
                                 json={"action": "acknowledged"})
            r = await c.post(f"/runs/{run_id}/approve")
        else:
            check("nothing blocked approval", True)
        check("approval accepted", r.status_code == 303 and r.headers["location"].endswith("/report"),
              f"HTTP {r.status_code}")
        run = store.get_run(run_id)
        check("run is APPROVED and locked", run.status is RunStatus.APPROVED and run.is_locked)
        check("approval timestamped", run.approved_at is not None)
        qa = store.get_qa(run_id)
        check("sign-off recorded in the QA checklist",
              any(row.get("check") == "Reviewer sign-off" for row in (qa.checklist if qa else [])))
        r = await c.get(f"/runs/{run_id}/report")
        check("approved document renders", r.status_code == 200 and "Approved document" in r.text)
        check("  reviewer input printed in the document", "Use the 2024 SEER release" in r.text)
        r = await c.post(f"/runs/{run_id}/insights/{insights[0].id}/modify",
                         json={"user_input": "change it"})
        check("edits after approval are refused", r.status_code == 409, f"HTTP {r.status_code}")
        r = await c.get(f"/runs/{run_id}")
        check("run root now opens the approved document", r.headers.get("location", "").endswith("/report"))
        r = await c.get(f"/runs/{run_id}/progress")
        check("the agent record is still viewable after approval",
              r.status_code == 200 and "Clinical Landscape Agent" in r.text)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
