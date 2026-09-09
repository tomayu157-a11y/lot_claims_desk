"""Celestra — clinical desk research. FastAPI application and routes."""
from __future__ import annotations

import asyncio
import html
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
from .services.scoring import confidence_for
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


def _llm_describe() -> str:
    """Active provider and model for the report header and settings page."""
    try:
        from .services.llm import llm

        return llm.describe() if llm.available else f"Deterministic ({llm.describe()})"
    except Exception:
        return "Deterministic (LLM layer unavailable)"


def web_search_status() -> dict[str, Any]:
    """Why open-web fallback is or is not contributing, for the sources panel."""
    settings = get_settings()
    try:
        from .connectors.firecrawl import breaker
    except Exception:
        return {"available": False, "reason": "web connector unavailable", "keyed": False}
    if settings.firecrawl_enabled:
        return {"available": True, "reason": "Firecrawl configured", "keyed": True}
    if breaker.open:
        return {"available": False, "reason": breaker.reason, "keyed": False}
    return {
        "available": True,
        "reason": "using the keyless search path; configure FIRECRAWL_API_KEY for "
                  "reliable fallback",
        "keyed": False,
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


def tagify(value: Any) -> Markup:
    """Renders inline [VERIFIED] / [Source: x] markers as styled pills."""
    text = html.escape(str(value or ""))

    def tag_sub(m: re.Match) -> str:
        raw = m.group(1)
        cls = _TAG_CLASS.get(
            raw, "vtag-update" if raw.startswith("UPDATE") else "vtag-general-knowledge"
        )
        return f'<span class="vtag {cls}">{raw}</span>'

    text = _TAG_RE.sub(tag_sub, text)
    text = _SOURCE_RE.sub(
        lambda m: f'<span class="vtag vtag-source">{m.group(1).strip()}</span>', text
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
        "llm": s.llm_status(),
        "app_name": s.app_name,
        "messages": [],
        "source_names": _source_names(),
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
    {"value": "Hematology", "enabled": False},
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
    {"value": "Build Claims Line of Therapy", "label": "LOT claims", "enabled": True},
    {"value": "Targeting", "label": "Targeting", "enabled": False},
    {"value": "Forecasting", "label": "Forecasting", "enabled": False},
]
UPCOMING_INDICATIONS = [
    "Multiple Myeloma", "Diffuse Large B-cell Lymphoma", "Acute Myeloid Leukemia",
    "Non-Small Cell Lung Cancer",
]


def _enabled_values(options: list[dict]) -> set[str]:
    return {o["value"] for o in options if o.get("enabled")}


@app.get("/projects/new", response_class=HTMLResponse)
async def new_project(request: Request):
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


@app.get("/runs/{run_id}", response_class=HTMLResponse)
@app.get("/runs/{run_id}/discovery", response_class=HTMLResponse)
async def discovery(request: Request, run_id: str):
    """Live progress. A finished run has nothing left to watch, so it goes
    straight to the results."""
    run = get_run_or_404(run_id)
    if run.status is RunStatus.COMPLETED:
        return RedirectResponse(f"/runs/{run_id}/overview", status_code=303)
    if run.status is RunStatus.AWAITING_REVIEW:
        return RedirectResponse(f"/runs/{run_id}/review", status_code=303)
    agents = sorted(run.agents.values(), key=lambda a: (a.wave, a.name))
    return templates.TemplateResponse(
        request, "discovery.html",
        {**base_ctx(request, "projects"), "run": run, "agents": agents,
         "source_chips": source_chip_names(), "last_seq": 0},
    )


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
    insights = store.get_insights(run_id)
    return templates.TemplateResponse(
        request, "insights.html",
        {
            **base_ctx(request, "projects"), "run": run, "insights": insights,
            "categories": _categories(insights), "agents": agent_catalogue(),
            "source_names": _source_names(),
        },
    )


@app.get("/runs/{run_id}/insights/{insight_id}/modify", response_class=HTMLResponse)
@app.get("/runs/{run_id}/insights/{insight_id}/input", response_class=HTMLResponse)
async def insight_modal(request: Request, run_id: str, insight_id: str):
    get_run_or_404(run_id)
    insight = store.get_insight(run_id, insight_id)
    if insight is None:
        raise HTTPException(404, "Insight not found")
    others = [i for i in store.get_insights(run_id) if i.id != insight_id]
    impacts = _impacts(insight, others)
    evidence = [e for e in store.get_evidence(run_id) if e.id in set(insight.evidence_ids)]
    return templates.TemplateResponse(
        request, "partials/insight_modal.html",
        {**base_ctx(request), "insight": insight, "impacts": impacts,
         "evidence": evidence, "run_id": run_id, "run": store.get_run(run_id),
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
    insight = store.get_insight(run_id, insight_id)
    if insight is None:
        raise HTTPException(404, "Insight not found")

    if action == "approve":
        insight.review_action = ReviewAction.APPROVED
        store.save_insights(run_id, [insight])
        return insight

    if action not in ("modify", "input"):
        raise HTTPException(400, f"Unknown action '{action}'")

    insight.review_action = ReviewAction.MODIFIED
    insight.user_input = user_input.strip()[:500]
    others = [i for i in store.get_insights(run_id) if i.id != insight_id]
    impacted = {x["title"] for x in _impacts(insight, others)}
    insight.impacted_insight_ids = [o.id for o in others if o.title in impacted]

    if insight.user_input:
        await _revise_insight(run_id, insight)

    store.save_insights(run_id, [insight])
    return insight


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
        # A revision that consulted the open web is no longer purely
        # approved-source evidence, and the confidence must say so.
        insight.confidence = confidence_for(
            question, result.evidence, store.get_contradictions(run_id)
        )
    if result.note:
        insight.detail = (f"Reviewer instruction: {insight.user_input} — {result.note}")

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



# --------------------------------------------------------------------------
# Human review gate
# --------------------------------------------------------------------------
@app.get("/runs/{run_id}/review", response_class=HTMLResponse)
async def review_page(request: Request, run_id: str):
    """Findings and conflicts from the agents that have finished so far, with
    the decision to continue. Downstream agents build on these, so a wrong
    finding approved here propagates; that is why the gate sits here."""
    run = get_run_or_404(run_id)
    insights = store.get_insights(run_id)
    contradictions = store.get_contradictions(run_id)
    contradictions.sort(key=lambda c: (c.severity is not ContradictionSeverity.ESCALATED, c.topic))
    fw = get_framework()["buckets"]
    done = [a for a in run.agents.values() if a.status is AgentStatus.COMPLETE]
    remaining = [
        a for a in sorted(run.agents.values(), key=lambda x: x.wave)
        if a.status is not AgentStatus.COMPLETE
    ]
    counts = {
        "insights": len(insights),
        "pending": sum(1 for i in insights if i.review_action is ReviewAction.PENDING),
        "approved": sum(1 for i in insights if i.review_action is ReviewAction.APPROVED),
        "modified": sum(1 for i in insights if i.review_action is ReviewAction.MODIFIED),
        "conflicts": len(contradictions),
        "conflicts_open": sum(1 for c in contradictions
                              if c.review_action is ReviewAction.PENDING),
    }
    return templates.TemplateResponse(
        request, "review.html",
        {
            **base_ctx(request, "projects"), "run": run, "insights": insights,
            "contradictions": contradictions, "counts": counts,
            "categories": _categories(insights), "agents": agent_catalogue(),
            "agent_by_stage": _agent_by_stage(),
            "completed_agents": [
                {"name": fw[a.bucket]["agent_name"], "icon": fw[a.bucket].get("agent_icon", "dot"),
                 "tagline": fw[a.bucket]["agent_tagline"]} for a in done
            ],
            "remaining_agents": [
                {"name": fw[a.bucket]["agent_name"], "icon": fw[a.bucket].get("agent_icon", "dot"),
                 "tagline": fw[a.bucket]["agent_tagline"], "wave": a.wave} for a in remaining
            ],
            "can_continue": run.status is RunStatus.AWAITING_REVIEW,
        },
    )


@app.post("/runs/{run_id}/continue")
async def continue_run(run_id: str):
    run = get_run_or_404(run_id)
    if run.status is not RunStatus.AWAITING_REVIEW:
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    if run_id in _RUNNING:
        return RedirectResponse(f"/runs/{run_id}/discovery", status_code=303)

    async def _resume() -> None:
        current = store.get_run(run_id)
        if current is not None:
            await orch.Orchestrator(current, registry()).resume()

    task = asyncio.create_task(_resume())
    _RUNNING[run_id] = task
    task.add_done_callback(lambda t, rid=run_id: _RUNNING.pop(rid, None))
    return RedirectResponse(f"/runs/{run_id}/discovery", status_code=303)


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
        {**base_ctx(request, "projects"), "run": run, "contradictions": items},
    )


@app.post("/runs/{run_id}/contradictions/{cid}/review", response_class=HTMLResponse)
async def contradiction_review(request: Request, run_id: str, cid: str):
    get_run_or_404(run_id)
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
    return templates.TemplateResponse(
        request, "partials/contradiction_card.html",
        {**base_ctx(request), "contradiction": item, "run_id": run_id,
         "run": store.get_run(run_id)},
    )


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
            **base_ctx(request, "projects"), "run": run, "stages": stages,
            "qa": qa_metrics, "contradictions": store.get_contradictions(run_id),
            "sources": sources, "execution_plan": execution_plan, "params": params,
            "questions": store.get_questions(run_id),
            "report_title": f"{cfg.indication} — Clinical Foundation Research",
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
        {**base_ctx(request, "projects"), "run": run,
         "used": sorted(used.values(), key=lambda r: (r["tier"], -r["evidence_items"])),
         "unavailable": empty + blocked,
         "health": connector_health(),
         "web_search": web_search_status()},
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
        request, "approval.html",
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
        },
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
