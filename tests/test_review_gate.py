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
        insights = store.get_insights(run_id)
        check("first-wave insights persisted", len(insights) == 10, f"{len(insights)} insights")
        check("no stage report for later stages yet",
              {s.stage for s in store.get_stage_reports(run_id)} == {"stage_1", "stage_2"})

        print("\n== review page ==")
        r = await c.get(f"/runs/{run_id}")
        check("run root redirects to review", r.status_code == 303
              and r.headers["location"].endswith("/review"), r.headers.get("location", ""))
        r = await c.get(f"/runs/{run_id}/review")
        check("review page renders", r.status_code == 200, f"HTTP {r.status_code}")
        check("  offers Continue", "Continue with remaining agents" in r.text)
        check("  shows the finished agents", "Clinical Landscape Agent" in r.text
              and "Treatment Evidence Agent" in r.text)
        check("  lists the waiting agents", "Diagnostic Footprint Agent" in r.text)
        check("  no internal vocabulary", not re.search(r"\bbucket", r.text, re.I))
        check("  every insight card has a table button",
              r.text.count("View Table") == len(insights), f"{r.text.count('View Table')}")

        print("\n== a decision made at the gate persists ==")
        r = await c.post(f"/runs/{run_id}/insights/{insights[0].id}/approve", json={})
        check("approve at the gate", r.status_code == 200
              and store.get_insight(run_id, insights[0].id).review_action.value == "approved")

        print("\n== table snapshot ==")
        r = await c.get(f"/runs/{run_id}/insights/{insights[0].id}/table")
        check("table fragment renders", r.status_code == 200, f"HTTP {r.status_code}")
        check("  contains a table", "<table" in r.text)
        check("  names the insight", insights[0].title in r.text)
        check("  is the document table", any(t in r.text for t in insights[0].table_titles),
              str(insights[0].table_titles))
        epi = next((i for i in insights if "pidemiology" in i.title), None)
        if epi:
            r = await c.get(f"/runs/{run_id}/insights/{epi.id}/table")
            check("epidemiology insight opens the epidemiology table",
                  "Epidemiology snapshot" in r.text, str(epi.table_titles))

        print("\n== continue ==")
        r = await c.post(f"/runs/{run_id}/continue")
        check("continue accepted", r.status_code == 303, f"HTTP {r.status_code}")
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

        r = await c.get(f"/runs/{run_id}")
        check("run root now redirects to overview", r.headers.get("location", "").endswith("/overview"))
        r = await c.post(f"/runs/{run_id}/continue")
        check("continue on a finished run is a no-op redirect", r.status_code == 303)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
