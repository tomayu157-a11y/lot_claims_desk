"""Celestra — clinical desk research. FastAPI application and routes."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .events import bus
from .models import (
    AgentStatus,
    Confidence,
    ContradictionSeverity,
    Insight,
    ReviewAction,
    Run,
    RunConfig,
    RunMode,
    RunStatus,
    utcnow,
)
from .services import orchestrator as orch
from .services import planner
from .settings import (
    BASE_DIR,
    ensure_dirs,
    get_framework,
    get_questions,
    get_source_registry,
    get_thresholds,
    get_settings,
)
from .store import store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("celestra")

_RUNNING: dict[str, asyncio.Task] = {}
_REGISTRY: dict[str, Any] = {}


def registry() -> dict[str, Any]:
    """Connector registry, built once. A broken connector module degrades the
    app to open-web fallback rather than preventing startup."""
    global _REGISTRY
    if not _REGISTRY:
        try:
            from .connectors.registry import build_registry

            _REGISTRY = build_registry()
            log.info("connector registry built: %d sources", len(_REGISTRY))
        except Exception:
            log.exception("connector registry failed to build")
            _REGISTRY = {}
    return _REGISTRY


def connector_health() -> list[dict]:
    try:
        from .connectors.registry import connector_health as ch

        return ch()
    except Exception:
        return []


def reference_datasets() -> dict[str, bool]:
    try:
        from .connectors.local_files import LocalFilesConnector

        return LocalFilesConnector().available_datasets()
    except Exception:
        return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    registry()
    yield
    for task in list(_RUNNING.values()):
        task.cancel()
    try:
        from .connectors.base import http

        await http.aclose()
    except Exception:
        pass


app = FastAPI(title="Celestra", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# --------------------------------------------------------------------------
# Template filters
# --------------------------------------------------------------------------
_TAG_CLASS = {
    "VERIFIED": "tag-verified",
    "ORIGINAL": "tag-original",
    "INFERENCE": "tag-inference",
    "NOT VERIFIED": "tag-unverified",
    "GENERAL KNOWLEDGE": "tag-general",
    "SUPPLEMENTARY WEB EVIDENCE": "tag-web",
}
_TAG_RE = re.compile(
    r"\[(VERIFIED|ORIGINAL|INFERENCE|NOT VERIFIED|GENERAL KNOWLEDGE|UPDATE[^\]]*)\]"
)
_SOURCE_RE = re.compile(r"\[Source:\s*([^\]]+)\]")


def tagify(value: Any) -> Markup:
    """Renders inline [VERIFIED] / [Source: x] markers as styled pills."""
    text = html.escape(str(value or ""))

    def tag_sub(m: re.Match) -> str:
        raw = m.group(1)
        cls = _TAG_CLASS.get(raw, "tag-update" if raw.startswith("UPDATE") else "tag-general")
        return f'<span class="vtag {cls}">{raw}</span>'

    text = _TAG_RE.sub(tag_sub, text)
    text = _SOURCE_RE.sub(
        lambda m: f'<span class="src-pill">{m.group(1).strip()}</span>', text
    )
    return Markup(text)


def pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "0%"


templates.env.filters["tagify"] = tagify
templates.env.filters["pct"] = pct


def base_ctx(request: Request, active: str = "") -> dict[str, Any]:
    s = get_settings()
    return {
        "request": request,
        "active": active,
        "credentials": s.credential_status(),
        "app_name": s.app_name,
    }


def get_run_or_404(run_id: str) -> Run:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def agent_catalogue() -> list[dict]:
    fw = get_framework()["buckets"]
    out = []
    for letter in orch.all_buckets():
        spec = fw[letter]
        out.append({
            "key": orch.agent_key(letter),
            "bucket": letter,
            "name": spec["agent_name"],
            "tagline": spec["agent_tagline"],
            "icon": spec.get("agent_icon", "dot"),
            "stages": [
                get_questions()["stage_meta"][s]["name"] for s in (spec.get("stages") or [])
            ],
            "depends_on": [
                fw[d]["agent_name"] for d in (spec.get("depends_on") or [])
            ],
        })
    return out


def source_chip_names() -> list[str]:
    seen, out = set(), []
    for src in get_source_registry()["sources"]:
        if src.get("enabled", True) and src["name"] not in seen:
            seen.add(src["name"])
            out.append(src["name"])
    return out


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "home.html", {**base_ctx(request, "home"), "runs": store.list_runs(10)}
    )


@app.get("/projects", response_class=HTMLResponse)
async def projects(request: Request):
    return templates.TemplateResponse(
        "projects.html", {**base_ctx(request, "projects"), "runs": store.list_runs(50)}
    )


@app.get("/projects/new", response_class=HTMLResponse)
async def new_project(request: Request):
    inds = [
        {"key": k, "label": v["label"], "synonyms": v.get("synonyms", [])}
        for k, v in get_questions()["indications"].items()
    ]
    return templates.TemplateResponse(
        "new_project.html",
        {
            **base_ctx(request, "new"),
            "indications": inds,
            "agents": agent_catalogue(),
            "objectives": [
                "Build Claims Line of Therapy",
                "Cohort definition",
                "Treatment pattern analysis",
                "Payer evidence dossier",
            ],
            "geographies": ["United States", "European Union", "United Kingdom", "Global"],
        },
    )


@app.post("/projects")
async def create_project(
    request: Request,
    background: BackgroundTasks,
    indication: str = Form(...),
    drug_brand: str = Form(""),
    geography: str = Form("United States"),
    objective: str = Form("Build Claims Line of Therapy"),
    target_population: str = Form(""),
    additional_context: str = Form(""),
    mode: str = Form("full"),
    selected_agent: str = Form(""),
):
    key = _resolve_indication_key(indication)
    if key is None:
        return templates.TemplateResponse(
            "error.html",
            {
                **base_ctx(request),
                "message": "Unsupported indication",
                "detail": (
                    f"'{indication}' has no configured question set. Add it to "
                    f"config/research_questions.yaml, then start the project again. "
                    f"Configured: "
                    + ", ".join(
                        v["label"] for v in get_questions()["indications"].values()
                    )
                ),
            },
            status_code=422,
        )

    run_mode = RunMode.SINGLE if mode == "single" else RunMode.FULL
    bucket = orch.bucket_for_agent_key(selected_agent) if run_mode is RunMode.SINGLE else None
    if run_mode is RunMode.SINGLE and bucket is None:
        bucket = "A"

    cfg = RunConfig(
        drug_brand=drug_brand.strip(),
        indication=get_questions()["indications"][key]["label"],
        indication_key=key,
        geography=geography,
        objective=objective,
        target_population=target_population.strip(),
        additional_context=additional_context.strip(),
        mode=run_mode,
        selected_agent=bucket,
        research_cutoff=get_settings().research_cutoff or orch.default_cutoff(),
    )
    run = Run(config=cfg, reference=orch.new_reference())
    run.agents = orch.build_agent_states(
        orch.all_buckets() if run_mode is RunMode.FULL else [bucket or "A"]
    )
    store.save_run(run)

    task = asyncio.create_task(_execute(run.id))
    _RUNNING[run.id] = task
    task.add_done_callback(lambda t, rid=run.id: _RUNNING.pop(rid, None))
    return RedirectResponse(f"/runs/{run.id}/discovery", status_code=303)


def _resolve_indication_key(text: str) -> str | None:
    text_l = text.strip().lower()
    for key, spec in get_questions()["indications"].items():
        candidates = [key.lower(), spec["label"].lower(), spec["abbreviation"].lower()]
        candidates += [s.lower() for s in spec.get("synonyms", [])]
        if text_l in candidates or any(text_l in c or c in text_l for c in candidates[:2]):
            return key
    return None


async def _execute(run_id: str) -> None:
    run = store.get_run(run_id)
    if run is None:
        return
    await orch.Orchestrator(run, registry()).execute()


@app.get("/runs/{run_id}/discovery", response_class=HTMLResponse)
async def discovery(request: Request, run_id: str):
    run = get_run_or_404(run_id)
    if run.status is RunStatus.COMPLETED:
        return RedirectResponse(f"/runs/{run_id}/overview", status_code=303)
    agents = sorted(run.agents.values(), key=lambda a: (a.wave, a.name))
    return templates.TemplateResponse(
        "discovery.html",
        {**base_ctx(request, "projects"), "run": run, "agents": agents,
         "source_chips": source_chip_names()},
    )


@app.get("/runs/{run_id}/events")
async def events(run_id: str, lastSeq: int = Query(0)):
    get_run_or_404(run_id)

    async def stream():
        queue = await bus.subscribe(run_id, lastSeq)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield event.to_sse()
                if event.type in ("stream_end",):
                    break
        finally:
            await bus.unsubscribe(run_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _counts(insights: list[Insight]) -> dict[str, int]:
    c = {k.value: 0 for k in Confidence}
    for i in insights:
        c[i.confidence.value] += 1
    c["insights"] = len(insights)
    c["sources"] = len({s for i in insights for s in i.source_ids})
    return c


def _categories(insights: list[Insight]) -> list[dict]:
    order: list[str] = []
    counts: dict[str, int] = {}
    for i in insights:
        counts[i.category] = counts.get(i.category, 0) + 1
        if i.category not in order:
            order.append(i.category)
    return [{"key": c.lower(), "label": c, "count": counts[c]} for c in order]


@app.get("/runs/{run_id}/overview", response_class=HTMLResponse)
async def overview(request: Request, run_id: str):
    run = get_run_or_404(run_id)
    insights = store.get_insights(run_id)
    stages = store.get_stage_reports(run_id)
    ranked = sorted(
        insights,
        key=lambda i: (
            {"high": 0, "medium": 1, "requires_input": 2, "rejected": 3}[i.confidence.value],
            -i.source_count,
        ),
    )
    takeaways = ranked[:2] + [i for i in insights if i.confidence is Confidence.REQUIRES_INPUT][:1]
    return templates.TemplateResponse(
        "overview.html",
        {
            **base_ctx(request, "projects"), "run": run,
            "counts": _counts(insights), "categories": _categories(insights),
            "takeaways": takeaways[:3], "stages": stages,
            "qa": store.get_qa(run_id),
        },
    )


@app.get("/runs/{run_id}/insights", response_class=HTMLResponse)
async def insights_page(request: Request, run_id: str):
    run = get_run_or_404(run_id)
    insights = store.get_insights(run_id)
    return templates.TemplateResponse(
        "insights.html",
        {
            **base_ctx(request, "projects"), "run": run, "insights": insights,
            "categories": _categories(insights), "agents": agent_catalogue(),
            "source_names": _source_names(),
        },
    )


def _source_names() -> dict[str, str]:
    return {s["id"]: s["name"] for s in get_source_registry()["sources"]}


@app.get("/runs/{run_id}/insights/{insight_id}/modal", response_class=HTMLResponse)
async def insight_modal(request: Request, run_id: str, insight_id: str):
    get_run_or_404(run_id)
    insight = store.get_insight(run_id, insight_id)
    if insight is None:
        raise HTTPException(404, "Insight not found")
    others = [i for i in store.get_insights(run_id) if i.id != insight_id]
    impacts = _impacts(insight, others)
    evidence = [e for e in store.get_evidence(run_id) if e.id in set(insight.evidence_ids)]
    return templates.TemplateResponse(
        "partials/insight_modal.html",
        {**base_ctx(request), "insight": insight, "impacts": impacts,
         "evidence": evidence, "run_id": run_id, "source_names": _source_names()},
    )


def _impacts(insight: Insight, others: list[Insight]) -> list[dict]:
    """Which other findings a change here would recalculate. Shared sources or
    a shared stage make two findings genuinely coupled."""
    fw = get_framework()["buckets"]
    out: list[dict] = []
    src = set(insight.source_ids)
    for other in others:
        overlap = src & set(other.source_ids)
        same_stage = other.stage == insight.stage
        if not overlap and not same_stage:
            continue
        reason = (
            f"shares {len(overlap)} source(s) with this finding"
            if overlap else "belongs to the same research stage"
        )
        out.append({
            "agent_name": fw[other.bucket]["agent_name"],
            "title": other.title,
            "description": f"{other.title} will be recalculated because it {reason}.",
        })
    return out[:6]


@app.get("/runs/{run_id}/insights/{insight_id}/evidence", response_class=HTMLResponse)
async def insight_evidence(request: Request, run_id: str, insight_id: str):
    get_run_or_404(run_id)
    insight = store.get_insight(run_id, insight_id)
    if insight is None:
        raise HTTPException(404, "Insight not found")
    evidence = [e for e in store.get_evidence(run_id) if e.id in set(insight.evidence_ids)]
    return templates.TemplateResponse(
        "partials/evidence_panel.html",
        {**base_ctx(request), "insight": insight, "evidence": evidence},
    )


@app.post("/runs/{run_id}/insights/{insight_id}/action", response_class=HTMLResponse)
async def insight_action(
    request: Request, run_id: str, insight_id: str,
    action: str = Form(...), user_input: str = Form(""),
):
    get_run_or_404(run_id)
    insight = store.get_insight(run_id, insight_id)
    if insight is None:
        raise HTTPException(404, "Insight not found")

    if action == "approve":
        insight.review_action = ReviewAction.APPROVED
    elif action in ("modify", "input", "proxy"):
        insight.review_action = ReviewAction.MODIFIED
        insight.user_input = user_input.strip()[:500]
        others = [i for i in store.get_insights(run_id) if i.id != insight_id]
        impacted = _impacts(insight, others)
        insight.impacted_insight_ids = [
            o.id for o in others if o.title in {x["title"] for x in impacted}
        ]
        if insight.user_input and insight.confidence is Confidence.REQUIRES_INPUT:
            # Human input is evidence of a kind: it lifts a blocked finding to
            # medium, never to high, because no new source was consulted.
            insight.confidence = Confidence.MEDIUM
    else:
        raise HTTPException(400, f"Unknown action '{action}'")

    store.save_insights(run_id, [insight])
    return templates.TemplateResponse(
        "partials/insight_card.html",
        {**base_ctx(request), "insight": insight, "run_id": run_id,
         "source_names": _source_names()},
    )


@app.get("/runs/{run_id}/contradictions", response_class=HTMLResponse)
async def contradictions_page(request: Request, run_id: str):
    run = get_run_or_404(run_id)
    items = store.get_contradictions(run_id)
    items.sort(key=lambda c: (c.severity is not ContradictionSeverity.ESCALATED, c.topic))
    return templates.TemplateResponse(
        "contradictions.html",
        {**base_ctx(request, "projects"), "run": run, "contradictions": items},
    )


@app.post("/runs/{run_id}/contradictions/{cid}/action")
async def contradiction_action(
    run_id: str, cid: str, action: str = Form(...), note: str = Form(""),
):
    get_run_or_404(run_id)
    item = store.get_contradiction(run_id, cid)
    if item is None:
        raise HTTPException(404, "Contradiction not found")
    mapping = {
        "prefer_a": ReviewAction.PREFER_A,
        "prefer_b": ReviewAction.PREFER_B,
        "acknowledge": ReviewAction.ACKNOWLEDGED,
    }
    if action not in mapping:
        raise HTTPException(400, f"Unknown action '{action}'")
    item.review_action = mapping[action]
    item.reviewer_note = note.strip()[:500]
    store.save_contradictions(run_id, [item])
    return JSONResponse({
        "id": item.id,
        "review_action": item.review_action.value,
        "note": item.reviewer_note,
    })


@app.get("/runs/{run_id}/report", response_class=HTMLResponse)
async def report(request: Request, run_id: str):
    run = get_run_or_404(run_id)
    stages = store.get_stage_reports(run_id)
    evidence = store.get_evidence(run_id)
    by_source: dict[tuple, dict] = {}
    names = _source_names()
    tiers = {s["id"]: s["tier"] for s in get_source_registry()["sources"]}
    for e in evidence:
        k = (e.source_id, e.url)
        row = by_source.setdefault(k, {
            "organization": e.organization or names.get(e.source_id, e.source_id),
            "title": e.title or "—",
            "published": e.published or "Not stated",
            "url": e.url,
            "tier": tiers.get(e.source_id, e.tier),
            "evidence_items": 0,
            "supplementary": e.is_supplementary,
        })
        row["evidence_items"] += 1
    sources = sorted(by_source.values(), key=lambda r: (r["tier"], -r["evidence_items"]))

    fw = get_framework()["buckets"]
    plan = [
        {
            "wave": i,
            "agents": [fw[b]["agent_name"] for b in wave],
            "stages": [
                get_questions()["stage_meta"][s]["name"]
                for b in wave for s in (fw[b].get("stages") or [])
            ],
            "outputs": [fw[b]["output"] for b in wave],
        }
        for i, wave in enumerate(
            orch.compute_waves([a.bucket for a in run.agents.values()]), start=1
        )
    ]
    return templates.TemplateResponse(
        "report.html",
        {
            **base_ctx(request, "projects"), "run": run, "stages": stages,
            "qa": store.get_qa(run_id), "contradictions": store.get_contradictions(run_id),
            "sources": sources, "plan": plan,
            "questions": store.get_questions(run_id),
        },
    )


@app.get("/runs/{run_id}/sources", response_class=HTMLResponse)
async def sources_panel(request: Request, run_id: str):
    run = get_run_or_404(run_id)
    evidence = store.get_evidence(run_id)
    questions = store.get_questions(run_id)
    names = _source_names()
    reg = {s["id"]: s for s in get_source_registry()["sources"]}

    used: dict[str, dict] = {}
    for e in evidence:
        row = used.setdefault(e.source_id, {
            "id": e.source_id, "name": names.get(e.source_id, e.source_id),
            "tier": e.tier, "origin": e.origin.label, "items": 0,
            "access_method": reg.get(e.source_id, {}).get("access_method", "api"),
        })
        row["items"] += 1

    attempted = {s for q in questions for s in q.sources_attempted}
    unavailable = [
        {
            "id": sid, "name": names.get(sid, sid),
            "tier": reg.get(sid, {}).get("tier", 5),
            "access_method": reg.get(sid, {}).get("access_method", "api"),
            "notes": reg.get(sid, {}).get("notes", ""),
        }
        for sid in sorted(attempted - set(used))
    ]
    return templates.TemplateResponse(
        "sources_panel.html",
        {**base_ctx(request, "projects"), "run": run,
         "used": sorted(used.values(), key=lambda r: (r["tier"], -r["items"])),
         "unavailable": unavailable, "health": connector_health()},
    )


@app.get("/runs/{run_id}/approval", response_class=HTMLResponse)
async def approval(request: Request, run_id: str):
    run = get_run_or_404(run_id)
    insights = store.get_insights(run_id)
    stages = store.get_stage_reports(run_id)
    fw = get_framework()["buckets"]
    counts = {
        "approved": sum(1 for i in insights if i.review_action is ReviewAction.APPROVED),
        "modified": sum(1 for i in insights if i.review_action is ReviewAction.MODIFIED),
        "user_inputs": sum(1 for i in insights if i.user_input),
        "assumptions": sum(len(s.assumptions) for s in stages),
        "pending": sum(1 for i in insights if i.review_action is ReviewAction.PENDING),
        "total": len(insights),
    }
    included = [
        {
            "name": fw[a.bucket]["agent_name"],
            "description": fw[a.bucket]["agent_tagline"],
            "icon": fw[a.bucket].get("agent_icon", "dot"),
            "output": fw[a.bucket]["output"],
        }
        for a in sorted(run.agents.values(), key=lambda x: x.wave)
        if a.status is AgentStatus.COMPLETE
    ]
    return templates.TemplateResponse(
        "approval.html",
        {**base_ctx(request, "projects"), "run": run, "counts": counts,
         "included": included, "contradictions": store.get_contradictions(run_id)},
    )


@app.post("/runs/{run_id}/approve")
async def approve(run_id: str):
    run = get_run_or_404(run_id)
    run.approved_at = utcnow()
    store.save_run(run)
    return RedirectResponse(f"/runs/{run_id}/report", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(
        "settings.html",
        {
            **base_ctx(request, "settings"),
            "thresholds": get_thresholds(),
            "datasets": reference_datasets(),
            "health": connector_health(),
            "model": get_settings().llm_model,
        },
    )


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge(request: Request):
    return templates.TemplateResponse(
        "projects.html",
        {**base_ctx(request, "knowledge"), "runs": store.list_runs(50),
         "heading": "Knowledge Library"},
    )


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "connectors": len(registry()),
        "credentials": get_settings().credential_status(),
        "active_runs": len(_RUNNING),
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/api", "/runs")) and "text/html" not in request.headers.get(
        "accept", ""
    ):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(
        "error.html",
        {**base_ctx(request), "message": exc.detail, "detail": f"HTTP {exc.status_code}"},
        status_code=exc.status_code,
    )
