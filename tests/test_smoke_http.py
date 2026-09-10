"""HTTP smoke test: boots the real app and exercises every route.

Uses the stub connector registry from the pipeline test so the run completes
deterministically and quickly, which lets the page assertions be about
rendering rather than about whichever sources happened to answer today.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from test_pipeline import build_stub_registry

import celestra.main as app_mod
import celestra.services.orchestrator as orch_mod
import celestra.store as store_mod
from celestra.models import RunStatus
from celestra.store import Store

FORBIDDEN = re.compile(r"\bbucket", re.I)


async def main() -> int:
    db = Path("/tmp/celestra_smoke.db")
    db.unlink(missing_ok=True)
    test_store = Store(db)
    for mod in (store_mod, orch_mod, app_mod):
        mod.store = test_store
    app_mod._REGISTRY = build_stub_registry()

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    transport = httpx.ASGITransport(app=app_mod.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as c:
        print("\n== static pages ==")
        for path in ("/", "/projects", "/projects/new", "/library", "/settings", "/healthz"):
            r = await c.get(path)
            check(f"GET {path}", r.status_code == 200, f"HTTP {r.status_code}")
            if r.headers.get("content-type", "").startswith("text/html"):
                check(f"  no internal vocabulary on {path}",
                      not FORBIDDEN.search(r.text))

        r = await c.get("/static/css/app.css")
        check("GET /static/css/app.css", r.status_code == 200)
        r = await c.get("/static/js/app.js")
        check("GET /static/js/app.js", r.status_code == 200)

        print("\n== validation ==")
        r = await c.post("/projects", data={"indication": "Metastatic NSCLC", "mode": "full"})
        check("unsupported indication is rejected", r.status_code == 422, f"HTTP {r.status_code}")
        check("  and explains why", "research_questions.yaml" in r.text)

        print("\n== create and run a project ==")
        r = await c.post("/projects", data={
            "indication": "Chronic Lymphocytic Leukemia",
            "drug_brand": "Venclexta (venetoclax)",
            "geography": "United States",
            "objective": "Build Claims Line of Therapy",
            "target_population": "Adult patients with CLL",
            "mode": "single",
            "selected_agent": "clinical-landscape-agent",
        })
        check("project created", r.status_code == 200, f"HTTP {r.status_code}")
        run_id = next(iter(test_store.list_runs(1)), None)
        check("run persisted", run_id is not None)
        run_id = run_id.id

        for _ in range(120):
            run = test_store.get_run(run_id)
            if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                break
            await asyncio.sleep(0.25)
        run = test_store.get_run(run_id)
        check("run finished", run.status is RunStatus.COMPLETED, run.error or run.status.value)

        print("\n== result pages ==")
        pages = {
            f"/runs/{run_id}/progress": "agents",
            f"/runs/{run_id}/overview": "Stage reports",
            f"/runs/{run_id}/insights": "Insights",
            f"/runs/{run_id}/contradictions": "",
            f"/runs/{run_id}/sources": "",
            f"/runs/{run_id}/report": "",
            f"/runs/{run_id}/approval": "",
            f"/runs/{run_id}/stages/stage_1": "",
        }
        html_by_path = {}
        for path in pages:
            r = await c.get(path)
            check(f"GET {path}", r.status_code == 200, f"HTTP {r.status_code}")
            html_by_path[path] = r.text
            check("  no internal vocabulary", not FORBIDDEN.search(r.text))

        report = html_by_path[f"/runs/{run_id}/report"]
        check("report shows the agent name",
              "Clinical Landscape Agent" in report)
        check("report shows the executive summary",
              "desk-research deliverable" in report)
        check("report shows the QA checklist", "PASS" in report or "NOT APPLICABLE" in report)
        check("report shows sources referenced", "seer.cancer.gov" in report)
        check("verification marks rendered as dots", 'class="vdot' in report)

        insights_html = html_by_path[f"/runs/{run_id}/insights"]
        check("insights name their sources",
              "NCI SEER" in insights_html or "National Cancer Institute" in insights_html)

        print("\n== interactions ==")
        insights = test_store.get_insights(run_id)
        check("insights exist", bool(insights))
        iid = insights[0].id
        r = await c.get(f"/runs/{run_id}/insights/{iid}/evidence")
        check("GET evidence panel", r.status_code == 200, f"HTTP {r.status_code}")
        check("  evidence panel quotes a source", "http" in r.text)

        r = await c.get(f"/runs/{run_id}/insights/{iid}/modify")
        check("GET modify modal", r.status_code == 200, f"HTTP {r.status_code}")

        r = await c.post(f"/runs/{run_id}/insights/{iid}/approve", json={})
        check("POST approve", r.status_code == 200, f"HTTP {r.status_code}")
        check("  returns a card fragment", "insight" in r.text.lower())
        check("  persisted",
              test_store.get_insight(run_id, iid).review_action.value == "approved")

        r = await c.post(f"/runs/{run_id}/insights/{iid}/modify",
                         json={"user_input": "Restrict to systemic therapy only."})
        check("POST modify", r.status_code == 200, f"HTTP {r.status_code}")
        saved = test_store.get_insight(run_id, iid)
        check("  user input persisted", saved.user_input.startswith("Restrict"))
        check("  impact recorded", isinstance(saved.impacted_insight_ids, list))

        cons = test_store.get_contradictions(run_id)
        if cons:
            cid = cons[0].id
            r = await c.post(f"/runs/{run_id}/contradictions/{cid}/review",
                             json={"action": "prefer_a", "note": "NCI is tier 1."})
            check("POST contradiction review", r.status_code == 200, f"HTTP {r.status_code}")
            reloaded = test_store.get_contradiction(run_id, cid)
            check("  decision persisted", reloaded.review_action.value == "prefer_a")
            check("  both claims still present",
                  bool(reloaded.source_a_claim and reloaded.source_b_claim))
        else:
            check("contradictions detected", False, "none found to review")

        for i in test_store.get_insights(run_id):
            if i.needs_decision:
                await c.post(f"/runs/{run_id}/insights/{i.id}/approve", json={})
        for con in test_store.get_contradictions(run_id):
            if con.severity.value == "escalated" and con.review_action.value == "pending":
                await c.post(f"/runs/{run_id}/contradictions/{con.id}/review",
                             json={"action": "acknowledged"})
        r = await c.post(f"/runs/{run_id}/approve")
        check("POST approve run", r.status_code == 200 and "Approved document" in r.text,
              f"HTTP {r.status_code}")
        run = test_store.get_run(run_id)
        check("  approval timestamped and locked", run.approved_at is not None and run.is_locked)

        print("\n== rename, export, import ==")
        r = await c.get(f"/runs/{run_id}/rename")
        check("rename dialog renders", r.status_code == 200 and "Rename project" in r.text)
        r = await c.post(f"/runs/{run_id}/rename", data={"name": "CLL pilot", "next": f"/runs/{run_id}/report"})
        check("rename persisted", test_store.get_run(run_id).display_name == "CLL pilot")
        check("  document carries the name", "CLL pilot" in r.text)
        r = await c.get(f"/runs/{run_id}/export")
        check("export downloads JSON", r.status_code == 200
              and r.headers.get("content-disposition", "").startswith("attachment"))
        bundle = r.json()
        check("  bundle holds every part of the run",
              all(k in bundle for k in ("run", "questions", "evidence", "insights", "stage_reports", "qa")))
        n_insights = len(test_store.get_insights(run_id))
        r = await c.post("/projects/import", files={"bundle": ("p.json", r.content, "application/json")})
        check("import accepted", r.status_code == 200, f"HTTP {r.status_code}")
        check("  re-import overwrites, not duplicates",
              len(test_store.get_insights(run_id)) == n_insights and test_store.get_run(run_id).name == "CLL pilot")
        r = await c.post("/projects/import", files={"bundle": ("x.json", b"{}", "application/json")})
        check("bad file is refused with an explanation", r.status_code == 422 and "could not be imported" in r.text)

        print("\n== evidence marks ==")
        r = await c.get(f"/runs/{run_id}/report")
        check("report uses dots, not pills, for verified marks",
              'class="vdot vtag-verified"' in r.text and 'class="vtag vtag-verified"' not in r.text)
        check("  and carries the legend", "How to read the evidence marks" in r.text)

        print("\n== errors ==")
        r = await c.get("/runs/run_doesnotexist/overview")
        check("unknown run returns 404", r.status_code == 404, f"HTTP {r.status_code}")

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
