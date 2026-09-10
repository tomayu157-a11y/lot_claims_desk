"""Celestra — clinical desk research. FastAPI application and routes."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .events import bus
from .models import (
    PHASES,
    AgentStatus,
    Confidence,
    Contradiction,
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
from .services.scoring import assess_confidence
from .settings import (
    BASE_DIR,
    configure_tls,
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


def _llm_describe() -> str:
    """Active provider and model for the report header and settings page."""
    try:
        from .services.llm import llm

        return llm.describe() if llm.available else f"Deterministic ({llm.describe()})"
    except Exception:
        return "Deterministic (LLM layer unavailable)"


def web_search_status() -> dict[str, Any]:
    """Why open-web fallback is or is not contributing, for the sources panel,
    the Settings page and the banner. No network call; it reads what the
    connector recorded on its last real call."""
    settings = get_settings()
    try:
        from .connectors.firecrawl import breaker, firecrawl_status
    except Exception:
        return {"available": False, "reason": "web connector unavailable", "keyed": False,
                "error": "", "error_at": ""}
    if settings.firecrawl_enabled:
        error = firecrawl_status.get("error", "")
        return {
            "available": not error,
            "reason": ("Firecrawl configured; the last call failed" if error
                       else "Firecrawl configured"),
            "keyed": True, "error": error, "error_at": firecrawl_status.get("at", ""),
            "endpoint": settings.firecrawl_endpoint("search"),
            "version": settings.firecrawl_version,
        }
    if breaker.open:
        return {"available": False, "reason": breaker.reason, "keyed": False,
                "error": breaker.reason, "error_at": ""}
    return {
        "available": True,
        "reason": "using the keyless search path; configure FIRECRAWL_API_KEY for "
                  "reliable fallback",
        "keyed": False, "error": "", "error_at": "",
    }


def reference_datasets() -> dict[str, bool]:
    try:
        from .connectors.local_files import LocalFilesConnector

        return LocalFilesConnector().available_datasets()
    except Exception:
        return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    tls = configure_tls()
    log.info("TLS verification: %s", tls.get("detail"))
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
    "VERIFIED": "vtag-verified",
    "ORIGINAL": "vtag-original",
    "INFERENCE": "vtag-inference",
    "NOT VERIFIED": "vtag-not-verified",
    "GENERAL KNOWLEDGE": "vtag-general-knowledge",
}
_TAG_RE = re.compile(
    r"\[(VERIFIED|ORIGINAL|INFERENCE|NOT VERIFIED|GENERAL KNOWLEDGE|UPDATE[^\]]*)\]"
)
_SOURCE_RE = re.compile(r"\[Source:\s*([^\]]+)\]")


_TAG_TITLE = {
    "VERIFIED": "Verified: quoted directly from a source retrieved in this run",
    "ORIGINAL": "Original: an analytical construct of this workflow",
    "INFERENCE": "Inference: concluded from several pieces of evidence",
    "NOT VERIFIED": "Not verified: could not be confirmed in the cited source",
    "GENERAL KNOWLEDGE": "General knowledge: standard clinical information, not individually verified",
}


def tagify(value: Any) -> Markup:
    """Renders inline [VERIFIED] / [Source: x] markers.

    A tag becomes a small coloured dot with its meaning on hover, so a table
    of forty verified cells reads as a table and not as forty green pills.
    The legend on the page says what each colour means. A source marker
    becomes a quiet chip with the source name.
    """
    text = html.escape(str(value or ""))

    def tag_sub(m: re.Match) -> str:
        raw = m.group(1)
        cls = _TAG_CLASS.get(
            raw, "vtag-update" if raw.startswith("UPDATE") else "vtag-general-knowledge"
        )
        title = _TAG_TITLE.get(raw, raw.title())
        if raw.startswith("UPDATE"):
            title = f"Update: {raw[6:].strip(' —-:') or 'recent development'}"
        return (f'<span class="vdot {cls}" role="img" aria-label="{raw.title()}" '
                f'title="{html.escape(title)}"></span>')

    text = _TAG_RE.sub(tag_sub, text)
    text = _SOURCE_RE.sub(
        lambda m: f'<span class="vsrc" title="Source">{m.group(1).strip()}</span>', text
    )
    return Markup(text)


def pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "0%"


templates.env.filters["tagify"] = tagify
templates.env.filters["pct"] = pct


_PHASE_INDEX = {p["key"]: i for i, p in enumerate(PHASES)}


def run_steps(run: Run | None) -> list[dict[str, Any]]:
    """The one flow every run follows, as the sidebar and page headers show
    it: which step is done, which is current, which is still to come, and
    where each one lives. A single-agent run skips the gate and the phase it
    does not execute."""
    if run is None:
        return []
    rid = run.id
    urls = {
        "discovery": f"/runs/{rid}/progress",
        "review": f"/runs/{rid}/review",
        "mapping": f"/runs/{rid}/progress#mapping",
        "approval": f"/runs/{rid}/approval",
        "approved": f"/runs/{rid}/report",
    }
    phase = run.phase
    failed = phase == "failed"
    if failed:
        # Where it was when it broke: the live page still shows the agents.
        phase = "mapping" if run.resume_from_wave > 1 else "discovery"
        if run.config.mode is RunMode.SINGLE:
            phase = "mapping" if (run.config.selected_agent or "A") not in ("A", "C") else "discovery"
    current = _PHASE_INDEX.get(phase, 0)

    keys = [p["key"] for p in PHASES]
    if run.config.mode is RunMode.SINGLE:
        own = "discovery" if (run.config.selected_agent or "A") in ("A", "C") else "mapping"
        keys = [own, "approval", "approved"]

    out = []
    for spec in PHASES:
        if spec["key"] not in keys:
            continue
        idx = _PHASE_INDEX[spec["key"]]
        if idx < current:
            state = "done"
        elif idx == current:
            state = "failed" if failed else "current"
        else:
            state = "upcoming"
        out.append({
            **spec,
            "state": state,
            "url": urls[spec["key"]] if state != "upcoming" else "",
            "number": len(out) + 1,
        })
    return out


templates.env.globals["run_steps"] = run_steps


def base_ctx(request: Request, active: str = "") -> dict[str, Any]:
    s = get_settings()
    return {
        "request": request,
        "active": active,
        "credentials": s.credential_status(),
        "llm": s.llm_status(),
        "app_name": s.app_name,
        "messages": [],
        "source_names": _source_names(),
        "web_search": web_search_status(),
        "tiers": {
            int(k): v["label"] for k, v in get_source_registry()["tiers"].items()
        },
    }


def _agent_by_stage() -> dict[str, dict[str, str]]:
    fw = get_framework()["buckets"]
    return {
        stage: {"name": spec["agent_name"], "icon": spec.get("agent_icon", "dot")}
        for spec in fw.values()
        for stage in (spec.get("stages") or [])
    }


def _source_names() -> dict[str, str]:
    return {s["id"]: s["name"] for s in get_source_registry()["sources"]}


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
        request, "home.html", {**base_ctx(request, "home"), "runs": store.list_runs(10)}
    )


@app.get("/projects", response_class=HTMLResponse)
async def projects(request: Request):
    return templates.TemplateResponse(
        request, "projects.html", {**base_ctx(request, "projects"), "runs": store.list_runs(50)}
    )


# Options for the project form. Anything with enabled=False is shown greyed out
# so the roadmap is visible without pretending it works today.
THERAPY_AREAS = [
    {"value": "Oncology", "enabled": True},
    {"value": "Immunology", "enabled": False},
    {"value": "Neurology", "enabled": False},
    {"value": "Cardiovascular", "enabled": False},
]
POPULATIONS = [
    {"value": "All", "enabled": True},
]
GEOGRAPHIES = [
    {"value": "United States", "enabled": True},
    {"value": "Europe", "enabled": False},
]
OBJECTIVES = [
    {"value": "Build Claims Line of Therapy", "label": "Clinical & Treatment Landscape Research", "enabled": True},
    {"value": "Targeting", "label": "Targeting", "enabled": False},
    {"value": "Forecasting", "label": "Forecasting", "enabled": False},
]
UPCOMING_INDICATIONS = [
    "Multiple Myeloma", "Diffuse Large B-cell Lymphoma", "Acute Myeloid Leukemia",
    "Non-Small Cell Lung Cancer",
]


def _enabled_values(options: list[dict]) -> set[str]:
    return {o["value"] for o in options if o.get("enabled")}


@app.get("/projects/name", response_class=HTMLResponse)
async def name_project_dialog(request: Request):
    """Step one of a new project, as a dialog over whatever page the person
    is on. Its form is a GET to /projects/new, so no JavaScript is needed."""
    return templates.TemplateResponse(
        request, "partials/name_project_modal.html", base_ctx(request, "new_project"),
    )


@app.get("/projects/new", response_class=HTMLResponse)
async def new_project(request: Request, name: str = Query("")):
    """Step two: the research setup. Without a name yet, step one is shown
    first as a page."""
    name = name.strip()[:120]
    if not name:
        return templates.TemplateResponse(
            request, "name_project.html", base_ctx(request, "new_project"),
        )
    configured = get_questions()["indications"]
    indications = [
        {"key": k, "label": v["label"], "abbreviation": v["abbreviation"], "enabled": True}
        for k, v in configured.items()
    ] + [
        {"key": "", "label": name, "abbreviation": "", "enabled": False}
        for name in UPCOMING_INDICATIONS
    ]
    return templates.TemplateResponse(
        request, "new_project.html",
        {
            **base_ctx(request, "new"),
            "therapy_areas": THERAPY_AREAS,
            "indications": indications,
            "populations": POPULATIONS,
            "geographies": GEOGRAPHIES,
            "objectives": OBJECTIVES,
            "agents": agent_catalogue(),
            "project_name": name,
        },
    )


@app.post("/projects")
async def create_project(
    request: Request,
    indication: str = Form(...),
    therapy_area: str = Form("Oncology"),
    drug_brand: str = Form(""),
    population: str = Form("All"),
    geography: str = Form("United States"),
    objective: str = Form("Build Claims Line of Therapy"),
    target_population: str = Form(""),
    additional_context: str = Form(""),
    mode: str = Form("full"),
    selected_agent: str = Form(""),
    project_name: str = Form(""),
):
    # A disabled <option> cannot be submitted by a browser, but a crafted
    # request can send anything; the server holds the same line as the form.
    for label, value, options in (
        ("Therapy area", therapy_area, THERAPY_AREAS),
        ("Population", population, POPULATIONS),
        ("Geography", geography, GEOGRAPHIES),
        ("Objective", objective, OBJECTIVES),
    ):
        if value not in _enabled_values(options):
            return templates.TemplateResponse(
                request, "error.html",
                {**base_ctx(request), "message": f"{label} not available",
                 "detail": f"'{value}' is not available yet. Choose one of: "
                           + ", ".join(sorted(_enabled_values(options))) + "."},
                status_code=422,
            )

    key = _resolve_indication_key(indication)
    if key is None:
        return templates.TemplateResponse(
            request, "error.html",
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
        therapy_area=therapy_area,
        drug_brand=drug_brand.strip(),
        population=population,
        indication=get_questions()["indications"][key]["label"],
        indication_key=key,
        geography=geography,
        objective=objective,
        target_population=target_population.strip()
        or (f"{population} patients" if population != "All" else "All patients"),
        additional_context=additional_context.strip(),
        mode=run_mode,
        selected_agent=bucket,
        research_cutoff=get_settings().research_cutoff or orch.default_cutoff(),
    )
    run = Run(config=cfg, reference=orch.new_reference(), name=project_name.strip()[:120])
    run.agents = orch.build_agent_states(
        orch.all_buckets() if run_mode is RunMode.FULL else [bucket or "A"]
    )
    store.save_run(run)

    task = asyncio.create_task(_execute(run.id))
    _RUNNING[run.id] = task
    task.add_done_callback(lambda t, rid=run.id: _RUNNING.pop(rid, None))
    return RedirectResponse(f"/runs/{run.id}/progress", status_code=303)


def _resolve_indication_key(text: str) -> str | None:
    """Map free text to a configured indication.

    Exact matches first across the key, label, abbreviation and synonyms. Only
    then a containment check against the full label, so that a partial entry
    like "chronic lymphocytic" still resolves while a vague one like "leukemia"
    matches nothing rather than silently picking whichever came first.
    """
    text_l = " ".join(text.strip().lower().split())
    if not text_l:
        return None
    table: dict[str, list[str]] = {}
    for key, spec in get_questions()["indications"].items():
        table[key] = [
            key.lower(), spec["label"].lower(), spec["abbreviation"].lower(),
            *(s.lower() for s in spec.get("synonyms", [])),
        ]
    for key, candidates in table.items():
        if text_l in candidates:
            return key
    matches = [
        key for key, candidates in table.items()
        if any(len(text_l) >= 6 and text_l in c for c in candidates)
    ]
    return matches[0] if len(matches) == 1 else None


async def _execute(run_id: str) -> None:
    run = store.get_run(run_id)
    if run is None:
        return
    await orch.Orchestrator(run, registry()).execute()


def _step_url(run: Run) -> str:
    """Where a run's one flow currently is. Every entry point lands here."""
    return {
        RunStatus.AWAITING_REVIEW: f"/runs/{run.id}/review",
        RunStatus.COMPLETED: f"/runs/{run.id}/approval",
        RunStatus.APPROVED: f"/runs/{run.id}/report",
    }.get(run.status, f"/runs/{run.id}/progress")


@app.get("/runs/{run_id}", response_class=HTMLResponse)
@app.get("/runs/{run_id}/discovery", response_class=HTMLResponse)
async def run_root(run_id: str):
    """The run has one flow. Its root always sends you to the step it is on."""
    return RedirectResponse(_step_url(get_run_or_404(run_id)), status_code=303)


def _phase_groups(run: Run) -> list[dict]:
    """Agents grouped by phase, with the phase's own state derived from its
    agents so a header can say Complete, Running or Waiting without a second
    source of truth."""
    groups = []
    for spec in orch.phases_for([a.bucket for a in run.agents.values()]):
        agents = sorted(
            (a for a in run.agents.values() if orch.phase_of(a.bucket) == spec["key"]),
            key=lambda a: (a.wave, a.name),
        )
        statuses = {a.status for a in agents}
        if statuses and statuses <= {AgentStatus.COMPLETE}:
            state = "complete"
        elif AgentStatus.FAILED in statuses:
            state = "failed"
        elif statuses & {AgentStatus.RESEARCHING, AgentStatus.SYNTHESISING}:
            state = "running"
        elif run.phase == "review" and spec["key"] == "mapping":
            state = "gated"
        else:
            state = "queued"
        progress = (sum(1.0 if a.status is AgentStatus.COMPLETE else a.progress for a in agents)
                    / len(agents)) if agents else 0.0
        groups.append({**spec, "agents": agents, "state": state,
                       "progress": round(progress, 3),
                       "done": sum(1 for a in agents if a.status is AgentStatus.COMPLETE)})
    return groups


@app.get("/runs/{run_id}/progress", response_class=HTMLResponse)
async def progress(request: Request, run_id: str):
    """The agents, live while the run is running and as a record afterwards.
    This page never redirects away: a reviewer who wants to see what the
    agents did can always come back to it."""
    run = get_run_or_404(run_id)
    return templates.TemplateResponse(
        request, "progress.html",
        {**base_ctx(request, "progress"), "run": run, "phases": _phase_groups(run),
         "source_chips": source_chip_names(), "last_seq": 0,
         "next_url": _step_url(run)},
    )


@app.get("/runs/{run_id}/rename", response_class=HTMLResponse)
async def rename_form(request: Request, run_id: str):
    run = get_run_or_404(run_id)
    return templates.TemplateResponse(
        request, "partials/rename_modal.html", {**base_ctx(request), "run": run},
    )


@app.post("/runs/{run_id}/rename")
async def rename_run(request: Request, run_id: str):
    """Give the project the name the person uses for it. Nothing else about
    the run changes; the reference stays as its permanent id."""
    run = get_run_or_404(run_id)
    body = await _body(request)
    run.name = str(body.get("name", "")).strip()[:120]
    store.save_run(run)
    back = str(body.get("next") or request.headers.get("referer") or f"/runs/{run_id}")
    if not back.startswith("/"):
        back = f"/runs/{run_id}"
    return RedirectResponse(back, status_code=303)


_EXPORT_VERSION = 1


def _export_bundle(run: Run) -> dict[str, Any]:
    """Everything the store holds for one run, as plain JSON, so a project
    can move between a laptop and a hosted instance."""
    dump = lambda items: [i.model_dump(mode="json") for i in items]  # noqa: E731
    qa_metrics = store.get_qa(run.id)
    return {
        "celestra_export": _EXPORT_VERSION,
        "exported_at": utcnow().isoformat(),
        "run": run.model_dump(mode="json"),
        "questions": dump(store.get_questions(run.id)),
        "evidence": dump(store.get_evidence(run.id)),
        "answers": dump(store.get_answers(run.id)),
        "insights": dump(store.get_insights(run.id)),
        "contradictions": dump(store.get_contradictions(run.id)),
        "stage_reports": dump(store.get_stage_reports(run.id)),
        "qa": qa_metrics.model_dump(mode="json") if qa_metrics else None,
    }


@app.get("/runs/{run_id}/export")
async def export_run(run_id: str):
    run = get_run_or_404(run_id)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", run.display_name).strip("-")[:60] or "project"
    return JSONResponse(
        _export_bundle(run),
        headers={"Content-Disposition":
                 f'attachment; filename="celestra-{safe}-{run.reference or run.id}.json"'},
    )


@app.post("/projects/import")
async def import_run(request: Request):
    """Load a project exported from another instance. An id already present
    is overwritten with the imported copy, so re-importing is safe."""
    from .models import (
        Answer, Contradiction, Evidence, QAMetrics, ResearchQuestion, StageReport,
    )

    form = await request.form()
    upload = form.get("bundle")
    raw = await upload.read() if hasattr(upload, "read") else b""
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or "run" not in data:
            raise ValueError("not a Celestra export")
        run = Run.model_validate(data["run"])
        if run.status in (RunStatus.PENDING, RunStatus.RUNNING):
            # A run that was mid-flight elsewhere cannot continue here.
            run.status = RunStatus.FAILED
            run.error = run.error or "imported while still running on the source instance"
        store.save_run(run)
        store.save_questions(run.id, [ResearchQuestion.model_validate(x) for x in data.get("questions", [])])
        store.save_evidence(run.id, [Evidence.model_validate(x) for x in data.get("evidence", [])])
        store.save_answers(run.id, [Answer.model_validate(x) for x in data.get("answers", [])])
        store.save_insights(run.id, [Insight.model_validate(x) for x in data.get("insights", [])])
        store.save_contradictions(run.id, [Contradiction.model_validate(x) for x in data.get("contradictions", [])])
        store.save_stage_reports(run.id, [StageReport.model_validate(x) for x in data.get("stage_reports", [])])
        if data.get("qa"):
            store.save_qa(run.id, QAMetrics.model_validate(data["qa"]))
    except Exception as exc:  # noqa: BLE001 - shown to the person
        return templates.TemplateResponse(
            request, "error.html",
            {**base_ctx(request), "message": "That file could not be imported",
             "detail": f"{type(exc).__name__}: {str(exc)[:300]}. Export a project from the "
                       "Projects page of the other instance and upload that file.",
             "entry_points": [{"label": "Projects", "url": "/projects"}]},
            status_code=422,
        )
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@app.get("/runs/{run_id}/stages/{stage}", response_class=HTMLResponse)
async def stage_page(request: Request, run_id: str, stage: str):
    run = get_run_or_404(run_id)
    report = next((s for s in store.get_stage_reports(run_id) if s.stage == stage), None)
    if report is None:
        raise HTTPException(404, f"No report for {stage} in this run")
    return templates.TemplateResponse(
        request, "stage_report.html",
        {**base_ctx(request, "projects"), "run": run, "stage_report": report,
         "report": report, "stage": report,
         "stage_contradictions": [
             c for c in store.get_contradictions(run_id) if c.stage == stage
         ],
         "agent_by_stage": _agent_by_stage()},
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
    c["needs_decision"] = sum(1 for i in insights if i.needs_decision)
    c["pending"] = sum(1 for i in insights if not i.review_action.is_decided)
    c["approved"] = sum(1 for i in insights if i.review_action is ReviewAction.APPROVED)
    c["modified"] = sum(1 for i in insights if i.review_action is ReviewAction.MODIFIED)
    c["input_added"] = sum(1 for i in insights if i.review_action is ReviewAction.INPUT_ADDED)
    c["decided"] = c["approved"] + c["modified"] + c["input_added"]
    c["user_inputs"] = sum(1 for i in insights if i.reviewer_input)
    return c


def _gate(run: Run, insights: list[Insight], contradictions: list[Contradiction],
          mode: str) -> dict[str, Any]:
    """What still blocks the next step, in words a reviewer can act on.

    The rule is the same at both gates: every finding that Requires Input
    needs a decision, and every escalated conflict needs one. Findings that
    are Ready may carry forward undecided; they are accepted as generated.
    """
    blockers: list[dict[str, str]] = []
    for i in insights:
        if i.needs_decision:
            blockers.append({
                "kind": "finding", "id": i.id, "title": i.title,
                "reason": i.input_reason or "This finding needs your input.",
                "anchor": f"#insight-{i.id}",
            })
    open_conflicts = [
        c for c in contradictions
        if c.severity is ContradictionSeverity.ESCALATED
        and c.review_action is ReviewAction.PENDING
    ]
    for c in open_conflicts:
        blockers.append({
            "kind": "conflict", "id": c.id, "title": c.topic,
            "reason": f"{c.source_a_name} and {c.source_b_name} disagree. Pick one, or "
                      "acknowledge that both are recorded.",
            "anchor": f"#conflict-{c.id}",
        })
    counts = _counts(insights)
    counts["conflicts"] = len(contradictions)
    counts["conflicts_open"] = sum(
        1 for c in contradictions if c.review_action is ReviewAction.PENDING
    )
    counts["conflicts_blocking"] = len(open_conflicts)
    if mode == "review":
        available = run.status is RunStatus.AWAITING_REVIEW
        action_url = f"/runs/{run.id}/continue"
        action_label = "Approve discovery and start Mapping & Synthesis"
        done_label = "Discovery approved"
        done = run.status not in (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.AWAITING_REVIEW) \
            or (run.status is RunStatus.RUNNING and run.resume_from_wave > 1)
    else:
        available = run.status is RunStatus.COMPLETED
        action_url = f"/runs/{run.id}/approve"
        action_label = "Approve the research document"
        done_label = "Document approved"
        done = run.status is RunStatus.APPROVED
    return {
        "mode": mode,
        "blockers": blockers,
        "counts": counts,
        "available": available,
        "can_proceed": available and not blockers,
        "done": done,
        "action_url": action_url,
        "action_label": action_label,
        "done_label": done_label,
        "refresh_url": f"/runs/{run.id}/gate?mode={mode}",
    }


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
        key=lambda i: (i.confidence is Confidence.REQUIRES_INPUT, -i.source_count),
    )
    takeaways = ranked[:2] + [i for i in insights if i.confidence is Confidence.REQUIRES_INPUT][:1]
    return templates.TemplateResponse(
        request, "overview.html",
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
    insights = sorted(store.get_insights(run_id), key=lambda i: (i.number or 999, i.stage))
    return templates.TemplateResponse(
        request, "insights.html",
        {
            **base_ctx(request, "insights"), "run": run, "insights": insights,
            "sections": _phase_sections(run, insights),
            "categories": _categories(insights), "agents": agent_catalogue(),
            "source_names": _source_names(), "counts": _counts(insights),
            "next_url": _step_url(run),
        },
    )


@app.get("/runs/{run_id}/insights/{insight_id}/modify", response_class=HTMLResponse)
@app.get("/runs/{run_id}/insights/{insight_id}/input", response_class=HTMLResponse)
async def insight_modal(request: Request, run_id: str, insight_id: str):
    """Two dialogs, one template. Modify hands an instruction to the model;
    Add Input attaches the reviewer's own knowledge. They do different things
    and the dialog says which."""
    run = get_run_or_404(run_id)
    insight = store.get_insight(run_id, insight_id)
    if insight is None:
        raise HTTPException(404, "Insight not found")
    mode = "input" if request.url.path.endswith("/input") else "modify"
    others = [i for i in store.get_insights(run_id) if i.id != insight_id]
    impacts = _impacts(insight, others) if mode == "modify" else []
    evidence = [e for e in store.get_evidence(run_id) if e.id in set(insight.evidence_ids)]
    fw = get_framework()["buckets"]
    downstream = [
        fw[a.bucket]["agent_name"] for a in sorted(run.agents.values(), key=lambda x: x.wave)
        if a.status is AgentStatus.QUEUED
    ]
    return templates.TemplateResponse(
        request, "partials/insight_modal.html",
        {**base_ctx(request), "insight": insight, "impacts": impacts, "mode": mode,
         "evidence": evidence, "run_id": run_id, "run": run,
         "downstream_agents": downstream, "web_available": web_search_status()["available"],
         "agent_by_stage": _agent_by_stage()},
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
    # Approved-source evidence first, open-web underneath it, so the reader
    # sees what a vetted source said before what the web added.
    evidence.sort(key=lambda e: (e.is_supplementary, e.tier, -e.relevance))

    # Pages the open-web fallback consulted for this insight's questions,
    # including ones that produced nothing, so the trail is auditable.
    wanted = set(insight.question_ids)
    sites: list[dict] = []
    seen: set[str] = set()
    for q in store.get_questions(run_id):
        if q.id not in wanted:
            continue
        for site in q.web_sites or []:
            url = str(site.get("url", ""))
            if url and url not in seen:
                seen.add(url)
                sites.append(site)

    return templates.TemplateResponse(
        request, "partials/evidence_panel.html",
        {**base_ctx(request), "insight": insight, "evidence": evidence,
         "web_sites": sites,
         "counts": {
             "approved": sum(1 for e in evidence if not e.is_supplementary),
             "web": sum(1 for e in evidence if e.is_supplementary),
         }},
    )


async def _body(request: Request) -> dict[str, Any]:
    """Accept either a JSON body or a form post, so the same handler serves the
    fetch-based UI and a no-JavaScript fallback."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            return dict(await request.json())
        except Exception:
            return {}
    try:
        return dict(await request.form())
    except Exception:
        return {}


async def _apply_insight_action(
    run_id: str, insight_id: str, action: str, user_input: str
) -> Insight:
    """The three decisions a reviewer can make on a finding.

    approve  accept it as generated
    modify   tell the model what to change; it re-checks the evidence, searches
             the web if needed, and rewrites the finding and the document
    input    attach your own knowledge; it is printed with the finding, goes
             into the document, and is handed to the agents still to run

    Any of the three settles a finding that Requires Input: a person looked.
    """
    run = get_run_or_404(run_id)
    if run.is_locked:
        raise HTTPException(409, "This document is approved and locked. Start a new "
                                 "project to research it again.")
    insight = store.get_insight(run_id, insight_id)
    if insight is None:
        raise HTTPException(404, "Insight not found")
    text = user_input.strip()[:500]

    if action == "approve":
        insight.review_action = ReviewAction.APPROVED
    elif action == "modify":
        if not text:
            raise HTTPException(400, "Say what should change. Modify sends your "
                                     "instruction to the model; it cannot act on nothing.")
        insight.review_action = ReviewAction.MODIFIED
        insight.user_input = text
        others = [i for i in store.get_insights(run_id) if i.id != insight_id]
        impacted = {x["title"] for x in _impacts(insight, others)}
        insight.impacted_insight_ids = [o.id for o in others if o.title in impacted]
        await _revise_insight(run_id, insight)
    elif action == "input":
        if not text:
            raise HTTPException(400, "Write the input you want attached to this finding.")
        insight.review_action = ReviewAction.INPUT_ADDED
        insight.reviewer_input = text
        _record_reviewer_input(run, insight)
    else:
        raise HTTPException(400, f"Unknown action '{action}'")

    # A reviewed finding no longer needs one. The reason it was flagged is
    # kept on the card so the decision stays explainable.
    insight.confidence = Confidence.READY
    insight.reviewed_at = utcnow()
    store.save_insights(run_id, [insight])
    return insight


def _record_reviewer_input(run: Run, insight: Insight) -> None:
    """Attach the reviewer's input to the run, the document and the agents
    that have not run yet. Nothing is rewritten by it."""
    entries = [
        e for e in (run.context.get("reviewer_inputs") or [])
        if isinstance(e, dict) and e.get("insight_id") != insight.id
    ]
    entries.append({
        "insight_id": insight.id, "stage": insight.stage, "title": insight.title,
        "input": insight.reviewer_input, "at": utcnow().isoformat(),
    })
    run.context["reviewer_inputs"] = entries
    store.save_run(run)

    question = next(
        (q for q in store.get_questions(run.id) if q.id in set(insight.question_ids)), None
    )
    if question is None:
        return
    for report in store.get_stage_reports(run.id):
        if report.stage != question.stage:
            continue
        changed = False
        for row in report.answers:
            if row.get("question") == question.text or row.get("seed") == question.seed_text:
                row["reviewer_input"] = insight.reviewer_input
                changed = True
        if changed:
            store.save_stage_reports(run.id, [report])


async def _revise_insight(run_id: str, insight: Insight) -> None:
    """Send the reviewer's instruction to the model and apply the result.

    The instruction is not filed as a note. The model decides whether the held
    evidence can satisfy it and searches the open web when it cannot, then
    rewrites the answer against everything found. The revision carries into
    the stage report so the final document reflects it.
    """
    from .services.revision import revise

    run = store.get_run(run_id)
    question = next(
        (q for q in store.get_questions(run_id) if q.id in set(insight.question_ids)), None
    )
    if run is None or question is None:
        return

    evidence = [e for e in store.get_evidence(run_id) if e.question_id == question.id]
    synonyms = list(
        get_questions()["indications"].get(run.config.indication_key, {}).get("synonyms") or []
    )
    try:
        result = await revise(insight, question, evidence, insight.user_input,
                              run.config, registry(), synonyms)
    except Exception:
        log.exception("revision failed for %s", insight.id)
        return

    if result.evidence and len(result.evidence) > len(evidence):
        store.save_evidence(run_id, [e for e in result.evidence if e not in evidence])
    if result.sites:
        question.web_sites = (question.web_sites or []) + result.sites

    if result.text:
        insight.summary = result.text[:400]
        question.answer_text = result.text
        question.answer_status = result.status
        if result.citations:
            question.answer_citations = result.citations
            insight.source_ids = list(dict.fromkeys(
                [e.source_id for e in result.evidence]
            ))
        insight.used_web_fallback = insight.used_web_fallback or bool(result.searched)
    where = (
        f"searched the web ({len(result.sites)} page(s))" if result.searched
        else "re-read the evidence it already held"
    )
    insight.revision_note = (
        (result.note or ("Finding rewritten." if result.text else "Finding left unchanged."))
        + f" Celestra {where}."
    )[:400]

    store.save_questions(run_id, [question])

    # Carry the revision into the stage document.
    for report in store.get_stage_reports(run_id):
        if report.stage != question.stage:
            continue
        for row in report.answers:
            if row.get("question") == question.text or row.get("seed") == question.seed_text:
                row["answer"] = question.answer_text
                row["status"] = question.answer_status.value
                row["citations"] = question.answer_citations
                row["revised"] = True
        store.save_stage_reports(run_id, [report])


@app.post("/runs/{run_id}/insights/{insight_id}/{action}", response_class=HTMLResponse)
async def insight_action(request: Request, run_id: str, insight_id: str, action: str):
    get_run_or_404(run_id)
    if action not in ("approve", "modify", "input"):
        raise HTTPException(404, "Unknown insight action")
    body = await _body(request)
    insight = await _apply_insight_action(
        run_id, insight_id, action, str(body.get("user_input", ""))
    )
    return templates.TemplateResponse(
        request, "partials/insight_card.html",
        {**base_ctx(request), "insight": insight, "run_id": run_id, "run": store.get_run(run_id),
         "agent_by_stage": _agent_by_stage()},
    )


@app.get("/runs/{run_id}/gate", response_class=HTMLResponse)
async def gate_panel(request: Request, run_id: str, mode: str = Query("review")):
    """The gate summary on its own, so the page can refresh it after every
    decision without reloading."""
    run = get_run_or_404(run_id)
    mode = "approval" if mode == "approval" else "review"
    gate = _gate(run, store.get_insights(run_id), store.get_contradictions(run_id), mode)
    return templates.TemplateResponse(
        request, "partials/gate_panel.html", {**base_ctx(request), "run": run, "gate": gate},
    )



# --------------------------------------------------------------------------
# Human review gate
# --------------------------------------------------------------------------
@app.get("/runs/{run_id}/review", response_class=HTMLResponse)
async def review_page(request: Request, run_id: str):
    """The gate between Discovery and Mapping & Synthesis. The later agents
    build on these findings, so a wrong one approved here propagates; that is
    why the gate sits here and why it blocks on anything that needs input."""
    run = get_run_or_404(run_id)
    insights = store.get_insights(run_id)
    contradictions = store.get_contradictions(run_id)
    contradictions.sort(key=lambda c: (c.severity is not ContradictionSeverity.ESCALATED, c.topic))
    # At the gate only the discovery findings exist. Afterwards this page is a
    # record of what was decided, and shows only what was decided here.
    discovery = {b for b in run.agents if orch.phase_of(b) == "discovery"}
    insights = [i for i in insights if i.bucket in discovery]
    contradictions = [
        c for c in contradictions
        if any(c.stage in (run.agents[b].stages or []) for b in discovery)
    ]
    insights.sort(key=lambda i: (i.number or 999, i.stage))
    fw = get_framework()["buckets"]
    phases = _phase_groups(run)
    return templates.TemplateResponse(
        request, "review.html",
        {
            **base_ctx(request, "review"), "run": run, "insights": insights,
            "contradictions": contradictions,
            "gate": _gate(run, insights, contradictions, "review"),
            "categories": _categories(insights), "agents": agent_catalogue(),
            "agent_by_stage": _agent_by_stage(), "source_names": _source_names(),
            "phases": phases,
            "completed_agents": [
                {"name": fw[a.bucket]["agent_name"], "icon": fw[a.bucket].get("agent_icon", "dot"),
                 "tagline": fw[a.bucket]["agent_tagline"]}
                for a in sorted(run.agents.values(), key=lambda x: x.wave)
                if a.bucket in discovery
            ],
            "remaining_agents": [
                {"name": fw[a.bucket]["agent_name"], "icon": fw[a.bucket].get("agent_icon", "dot"),
                 "tagline": fw[a.bucket]["agent_tagline"], "wave": a.wave}
                for a in sorted(run.agents.values(), key=lambda x: x.wave)
                if a.bucket not in discovery
            ],
        },
    )


@app.post("/runs/{run_id}/continue")
async def continue_run(run_id: str):
    """Approve the discovery findings and start Mapping & Synthesis.

    The status flips to RUNNING here, before the resume task is scheduled,
    so the live page this redirects to sees a running run and not a stale
    AWAITING_REVIEW that would bounce it straight back to the review page.
    """
    run = get_run_or_404(run_id)
    if run.status is not RunStatus.AWAITING_REVIEW:
        return RedirectResponse(_step_url(run), status_code=303)
    if run_id in _RUNNING:
        return RedirectResponse(f"/runs/{run_id}/progress", status_code=303)
    gate = _gate(run, store.get_insights(run_id), store.get_contradictions(run_id), "review")
    if gate["blockers"]:
        return RedirectResponse(f"/runs/{run_id}/review#gate", status_code=303)

    run.status = RunStatus.RUNNING
    run.reviewed_at = utcnow()
    store.save_run(run)

    async def _resume() -> None:
        current = store.get_run(run_id)
        if current is not None:
            await orch.Orchestrator(current, registry()).resume()

    task = asyncio.create_task(_resume())
    _RUNNING[run_id] = task
    task.add_done_callback(lambda t, rid=run_id: _RUNNING.pop(rid, None))
    return RedirectResponse(f"/runs/{run_id}/progress#mapping", status_code=303)


@app.get("/runs/{run_id}/insights/{insight_id}/table", response_class=HTMLResponse)
async def insight_table(request: Request, run_id: str, insight_id: str):
    """The stage-report table(s) this insight was built from, exactly as they
    appear in the final document."""
    get_run_or_404(run_id)
    insight = store.get_insight(run_id, insight_id)
    if insight is None:
        raise HTTPException(404, "Insight not found")
    report = next((s for s in store.get_stage_reports(run_id) if s.stage == insight.stage), None)
    tables = []
    if report is not None:
        wanted = set(insight.table_titles)
        tables = [t for t in report.tables if t.title in wanted] or (
            [t for t in report.tables if set(t.question_ids) & set(insight.question_ids)]
        )
    return templates.TemplateResponse(
        request, "partials/insight_table.html",
        {**base_ctx(request), "insight": insight, "tables": tables,
         "report": report, "run_id": run_id},
    )


@app.get("/runs/{run_id}/contradictions", response_class=HTMLResponse)
async def contradictions_page(request: Request, run_id: str):
    run = get_run_or_404(run_id)
    items = store.get_contradictions(run_id)
    items.sort(key=lambda c: (c.severity is not ContradictionSeverity.ESCALATED, c.topic))
    return templates.TemplateResponse(
        request, "contradictions.html",
        {**base_ctx(request, "contradictions"), "run": run, "contradictions": items,
         "next_url": _step_url(run)},
    )


@app.post("/runs/{run_id}/contradictions/{cid}/review", response_class=HTMLResponse)
async def contradiction_review(request: Request, run_id: str, cid: str):
    run = get_run_or_404(run_id)
    if run.is_locked:
        raise HTTPException(409, "This document is approved and locked.")
    item = store.get_contradiction(run_id, cid)
    if item is None:
        raise HTTPException(404, "Contradiction not found")
    body = await _body(request)
    mapping = {
        "prefer_a": ReviewAction.PREFER_A,
        "prefer_b": ReviewAction.PREFER_B,
        "acknowledged": ReviewAction.ACKNOWLEDGED,
        "acknowledge": ReviewAction.ACKNOWLEDGED,
    }
    action = str(body.get("action", ""))
    if action not in mapping:
        raise HTTPException(400, f"Unknown action '{action}'")
    # The decision is recorded against the disagreement. Neither claim is
    # rewritten or removed: the register keeps both sides as stated.
    item.review_action = mapping[action]
    item.reviewer_note = str(body.get("note", "")).strip()[:500]
    store.save_contradictions(run_id, [item])
    _settle_conflict_findings(run_id, item)
    return templates.TemplateResponse(
        request, "partials/contradiction_card.html",
        {**base_ctx(request), "contradiction": item, "run_id": run_id,
         "run": store.get_run(run_id)},
    )


def _settle_conflict_findings(run_id: str, decided: Contradiction) -> None:
    """A finding flagged only because of this conflict is Ready once the
    conflict is decided. Re-assess the undecided findings in its stage."""
    remaining = store.get_contradictions(run_id)
    evidence = store.get_evidence(run_id)
    questions = {q.id: q for q in store.get_questions(run_id)}
    changed: list[Insight] = []
    for insight in store.get_insights(run_id):
        if insight.stage != decided.stage or insight.review_action.is_decided:
            continue
        if insight.confidence is not Confidence.REQUIRES_INPUT:
            continue
        question = next((questions[q] for q in insight.question_ids if q in questions), None)
        if question is None:
            continue
        own = [e for e in evidence if e.id in set(insight.evidence_ids)]
        conf, reason = assess_confidence(question, own, remaining)
        if conf is not insight.confidence or reason != insight.input_reason:
            insight.confidence, insight.input_reason = conf, reason
            changed.append(insight)
    if changed:
        store.save_insights(run_id, changed)


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
    execution_plan = [
        {
            "wave": i,
            "mode": "Concurrent" if len(wave) > 1 else "Sequential",
            "agents": [fw[b]["agent_name"] for b in wave],
            "stages": [
                get_questions()["stage_meta"][st]["name"]
                for b in wave for st in (fw[b].get("stages") or [])
            ],
            "outputs": [fw[b]["output"] for b in wave],
            "gate": fw[wave[0]].get("gate", ""),
        }
        for i, wave in enumerate(
            orch.compute_waves([a.bucket for a in run.agents.values()]), start=1
        )
    ]
    qa_metrics = store.get_qa(run_id)
    cfg = run.config
    params = [
        {"label": "Therapy area", "value": cfg.therapy_area},
        {"label": "Indication", "value": cfg.indication},
        {"label": "Population", "value": cfg.population or cfg.target_population or "All"},
        {"label": "Geography", "value": cfg.geography},
        {"label": "Objective", "value": cfg.objective},
        {"label": "Research cutoff", "value": cfg.research_cutoff},
        {"label": "Research mode",
         "value": "All agents" if cfg.mode is RunMode.FULL else "Single agent"},
        {"label": "Run reference", "value": run.reference},
        {"label": "Document generated",
         "value": (run.finished_at or utcnow()).strftime("%Y-%m-%d %H:%M UTC")},
        {"label": "Stages completed", "value": ", ".join(s.name for s in stages) or "None"},
        {"label": "Evidence items", "value": str(len(evidence))},
        {"label": "Distinct sources", "value": str(len({e.source_id for e in evidence}))},
        {"label": "Synthesis engine",
         "value": _llm_describe()},
    ]
    if cfg.drug_brand:
        params.insert(1, {"label": "Drug / brand", "value": cfg.drug_brand})

    return templates.TemplateResponse(
        request, "report.html",
        {
            **base_ctx(request, "report"), "run": run, "stages": stages,
            "reviewer_inputs": list(run.context.get("reviewer_inputs") or []),
            "insights": store.get_insights(run_id),
            "qa": qa_metrics, "contradictions": store.get_contradictions(run_id),
            "sources": sources, "execution_plan": execution_plan, "params": params,
            "questions": store.get_questions(run_id),
            "report_title": (f"{run.display_name} — Clinical Foundation Research"
                             if run.name else f"{cfg.indication} — Clinical Foundation Research"),
            "executive_summary": qa_metrics.executive_summary if qa_metrics else "",
            "research_method": qa_metrics.research_method if qa_metrics else [],
            "document_limitations": qa_metrics.limitations if qa_metrics else [],
            "agent_by_stage": _agent_by_stage(),
        },
    )


@app.get("/runs/{run_id}/sources", response_class=HTMLResponse)
async def sources_panel(request: Request, run_id: str):
    """What this run actually queried, what each source returned, and what it
    could not reach. Three groups, because they need different actions from
    the reader: a source that answered, one that answered nothing, and one
    that was never usable because a credential or licence is missing."""
    run = get_run_or_404(run_id)
    evidence = store.get_evidence(run_id)
    questions = store.get_questions(run_id)
    names = _source_names()
    reg = {s["id"]: s for s in get_source_registry()["sources"]}
    health = {row["id"]: row for row in connector_health()}

    used: dict[str, dict] = {}
    for e in evidence:
        row = used.setdefault(e.source_id, {
            "id": e.source_id,
            "name": names.get(e.source_id, e.source_id),
            "organization": e.organization or names.get(e.source_id, e.source_id),
            "tier": e.tier,
            "origin": e.origin.label,
            "access_method": reg.get(e.source_id, {}).get("access_method", "api"),
            "evidence_items": 0,
            "url": e.url,
            "domain": reg.get(e.source_id, {}).get("domain") or "",
            "supplementary": e.is_supplementary,
        })
        row["evidence_items"] += 1
        # Prefer a real document link over whatever arrived first.
        if not row["url"] and e.url:
            row["url"] = e.url

    attempted = {sid for q in questions for sid in q.sources_attempted}
    failures: dict[str, str] = {}
    for q in questions:
        if q.unmet_reason and "—" in q.unmet_reason:
            for part in q.unmet_reason.split("—", 1)[1].split(";"):
                if ":" in part:
                    sid, _, reason = part.partition(":")
                    failures.setdefault(sid.strip(), reason.strip())

    empty: list[dict] = []
    blocked: list[dict] = []
    for sid in sorted(attempted - set(used)):
        source = reg.get(sid, {})
        row = {
            "id": sid,
            "name": names.get(sid, sid),
            "tier": int(source.get("tier", 5)),
            "access_method": source.get("access_method", "api"),
            "notes": source.get("notes", ""),
            "reason": failures.get(sid, "No matching documents were returned."),
        }
        blockers = health.get(sid, {}).get("blocking") or []
        if blockers:
            # These never had a chance, so they belong in their own group with
            # what is missing rather than reading as a source that came up empty.
            row["blocked_by"] = (
                "Licence" if source.get("access_method") == "licensed"
                else "Reference file" if source.get("access_method") == "local_file"
                else "Credentials"
            )
            row["reason"] = "; ".join(blockers)
            blocked.append(row)
        else:
            empty.append(row)

    return templates.TemplateResponse(
        request, "sources_panel.html",
        {**base_ctx(request, "sources"), "run": run,
         "used": sorted(used.values(), key=lambda r: (r["tier"], -r["evidence_items"])),
         "unavailable": empty + blocked,
         "health": connector_health(),
         "web_search": web_search_status()},
    )


def _phase_sections(run: Run, insights: list[Insight]) -> list[dict[str, Any]]:
    """Cards grouped by phase, the newest phase first. A reviewer coming from
    the gate has already seen the discovery cards; what they have not seen
    goes on top."""
    specs = orch.phases_for([a.bucket for a in run.agents.values()])
    sections = []
    for spec in reversed(specs):
        buckets = {b for b in run.agents if orch.phase_of(b) == spec["key"]}
        cards = sorted((i for i in insights if i.bucket in buckets),
                       key=lambda i: (i.number or 999, i.stage))
        if not cards:
            continue
        reviewed_at_gate = spec["key"] == "discovery" and run.reviewed_at is not None
        sections.append({
            **spec, "cards": cards,
            "label": ("Reviewed at the gate" if reviewed_at_gate else "New since your last review"),
            "is_new": not reviewed_at_gate,
            "needs": sum(1 for i in cards if i.needs_decision),
        })
    return sections


def _approval_ctx(request: Request, run: Run) -> dict[str, Any]:
    insights = store.get_insights(run.id)
    contradictions = store.get_contradictions(run.id)
    stages = store.get_stage_reports(run.id)
    fw = get_framework()["buckets"]
    gate = _gate(run, insights, contradictions, "approval")
    counts = dict(gate["counts"])
    counts["assumptions"] = sum(len(s.assumptions) for s in stages)
    counts["stages"] = len(stages)
    counts["evidence"] = sum(s.evidence_count for s in stages)
    # Findings that still need a decision come first, so the page reads as a
    # to-do list until it reads as a sign-off.
    ordered = sorted(insights, key=lambda i: (i.number or 999, i.stage))
    return {
        **base_ctx(request, "approval"), "run": run, "counts": counts, "gate": gate,
        "insights": ordered, "sections": _phase_sections(run, insights),
        "contradictions": contradictions,
        "categories": _categories(insights), "source_names": _source_names(),
        "agent_by_stage": _agent_by_stage(), "phases": _phase_groups(run),
        "reviewer_inputs": list(run.context.get("reviewer_inputs") or []),
        "included": [
            {
                "name": fw[a.bucket]["agent_name"],
                "description": fw[a.bucket]["agent_tagline"],
                "icon": fw[a.bucket].get("agent_icon", "dot"),
                "output": fw[a.bucket]["output"],
                "phase": orch.phase_spec(orch.phase_of(a.bucket))["name"],
            }
            for a in sorted(run.agents.values(), key=lambda x: x.wave)
            if a.status is AgentStatus.COMPLETE
        ],
    }


@app.get("/runs/{run_id}/approval", response_class=HTMLResponse)
async def approval(request: Request, run_id: str):
    """The last step. Everything that still needs input is listed as a
    blocker; when nothing does, the reviewer signs the document off."""
    run = get_run_or_404(run_id)
    if run.is_live or run.status is RunStatus.AWAITING_REVIEW:
        # Nothing to approve until every agent has finished.
        return RedirectResponse(_step_url(run), status_code=303)
    return templates.TemplateResponse(request, "approval.html", _approval_ctx(request, run))


@app.post("/runs/{run_id}/approve")
async def approve(request: Request, run_id: str):
    """Sign the document off. Refused, with the reasons on the page, while
    anything still needs input. On success the run is locked, the QA
    narrative is rebuilt with the reviewer's decisions in it, and the
    approved document opens."""
    run = get_run_or_404(run_id)
    if run.status is RunStatus.APPROVED:
        return RedirectResponse(f"/runs/{run_id}/report", status_code=303)
    if run.status is not RunStatus.COMPLETED:
        return RedirectResponse(_step_url(run), status_code=303)
    gate = _gate(run, store.get_insights(run_id), store.get_contradictions(run_id), "approval")
    if gate["blockers"]:
        ctx = _approval_ctx(request, run)
        ctx["messages"] = [{
            "level": "danger",
            "text": f"Not approved: {len(gate['blockers'])} item(s) still need your input. "
                    "They are listed below.",
        }]
        return templates.TemplateResponse(request, "approval.html", ctx, status_code=409)

    await _finalise_approval(run)
    return RedirectResponse(f"/runs/{run_id}/report", status_code=303)


async def _finalise_approval(run: Run) -> None:
    from .services import qa

    questions = store.get_questions(run.id)
    evidence = store.get_evidence(run.id)
    contradictions = store.get_contradictions(run.id)
    stages = store.get_stage_reports(run.id)
    insights = store.get_insights(run.id)
    try:
        metrics = qa.build_metrics(run.config, questions, evidence, contradictions, stages)
        metrics = qa.build_narrative(run.config, metrics, stages, questions)
        decided = [i for i in insights if i.review_action.is_decided]
        metrics.checklist.append({
            "check": "Reviewer sign-off",
            "status": "PASS",
            "detail": (
                f"{len(decided)} of {len(insights)} findings decided by the reviewer "
                f"({sum(1 for i in insights if i.review_action is ReviewAction.APPROVED)} approved, "
                f"{sum(1 for i in insights if i.review_action is ReviewAction.MODIFIED)} revised, "
                f"{sum(1 for i in insights if i.review_action is ReviewAction.INPUT_ADDED)} with "
                "reviewer input); the rest accepted as generated. "
                f"{sum(1 for c in contradictions if c.review_action.is_decided)} of "
                f"{len(contradictions)} source conflicts decided."
            ),
        })
        try:
            metrics = await qa.polish_readiness(metrics, run.config)
        except Exception:
            log.warning("readiness polish skipped on approval", exc_info=True)
        store.save_qa(run.id, metrics)
    except Exception:
        log.exception("qa rebuild failed on approval; approving with the existing narrative")

    run.status = RunStatus.APPROVED
    run.approved_at = utcnow()
    store.save_run(run)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(
        request, "settings.html",
        {
            **base_ctx(request, "settings"),
            "thresholds": get_thresholds(),
            "datasets": reference_datasets(),
            "health": connector_health(),
            "web_search": web_search_status(),
            "model": _llm_describe(),
            "provider": get_settings().provider,
            "provider_gaps": get_settings().provider_gaps(),
            "network": get_settings().network_status(),
        },
    )


@app.post("/settings/web-search-test", response_class=HTMLResponse)
async def web_search_test(request: Request):
    """One live Firecrawl call, with the cause and the fix in words when it
    fails. The same probe `run.py --check` runs, put where the person is."""
    from .connectors.firecrawl import FirecrawlConnector

    result = await FirecrawlConnector.probe()
    return templates.TemplateResponse(
        request, "partials/web_search_test.html",
        {**base_ctx(request, "settings"), "probe": result,
         "network": get_settings().network_status()},
    )


@app.get("/library", response_class=HTMLResponse)
async def knowledge(request: Request):
    return templates.TemplateResponse(
        request, "projects.html",
        {**base_ctx(request, "library"), "runs": store.list_runs(50),
         "heading": "Knowledge Library"},
    )


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "connectors": len(registry()),
        "credentials": get_settings().credential_status(),
        "web_search": web_search_status(),
        "active_runs": len(_RUNNING),
    }


# Common mistyped or guessed entry points. Landing on the app root is far more
# useful than a 404 for someone who has just started the server.
_ENTRY_ALIASES = {
    "/home": "/", "/index.html": "/", "/index": "/", "/app": "/",
    "/dashboard": "/", "/runs": "/projects", "/project": "/projects",
    "/new": "/projects/new", "/discovery": "/projects",
    "/knowledge": "/library", "/knowledge-library": "/library",
}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return RedirectResponse("/static/img/favicon.svg", status_code=308)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    path = request.url.path.rstrip("/") or "/"

    if exc.status_code == 404:
        if target := _ENTRY_ALIASES.get(path.lower()):
            return RedirectResponse(target, status_code=307)
        # A trailing-slash mismatch is a routing detail, not a dead end.
        if path != request.url.path and any(
            r.path == path for r in app.routes if hasattr(r, "path")
        ):
            return RedirectResponse(path, status_code=307)

    wants_json = "text/html" not in request.headers.get("accept", "")
    if wants_json and request.url.path.startswith(("/api/", "/healthz")):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return templates.TemplateResponse(
        request,
        "error.html",
        {
            **base_ctx(request),
            "message": (
                "That page does not exist" if exc.status_code == 404 else str(exc.detail)
            ),
            "detail": (
                f"No route matches {request.url.path}. Use the links below to get back "
                f"to a working page."
                if exc.status_code == 404
                else f"HTTP {exc.status_code}: {exc.detail}"
            ),
            "status_code": exc.status_code,
            "entry_points": [
                {"label": "Home", "url": "/"},
                {"label": "New project", "url": "/projects/new"},
                {"label": "Projects", "url": "/projects"},
                {"label": "Settings", "url": "/settings"},
            ],
        },
        status_code=exc.status_code,
    )
