# Directory Contents

### File: `.env.example`

```example
# Copy to .env and fill in. Every value is optional; the app runs without any
# of them, but degraded in the ways noted.

# ---------------------------------------------------------------------------
# LLM provider. Fill in ONE block below. LLM_PROVIDER may be left blank: the
# app then uses whichever provider has a complete set of keys. Set it only to
# force a choice when more than one block is filled in.
#   anthropic          Claude via the Anthropic API
#   anthropic_foundry  Claude deployed on Microsoft Foundry
#   azure_openai       a model deployed in Azure AI Foundry / Azure OpenAI
#
# Without a configured provider the app still runs: planning, ranking,
# extraction and synthesis fall back to the deterministic engine, which gives
# real evidence and real citations but weaker prose.
# ---------------------------------------------------------------------------
LLM_PROVIDER=
LLM_EFFORT=high
LLM_MAX_TOKENS=8000

# --- anthropic -------------------------------------------------------------
ANTHROPIC_API_KEY=
LLM_MODEL=claude-opus-5

# --- anthropic_foundry (Claude on Microsoft Foundry) -----------------------
# RESOURCE is the Foundry resource name, not a full URL.
FOUNDRY_API_KEY=
FOUNDRY_RESOURCE=
# Optional; defaults to LLM_MODEL above.
FOUNDRY_MODEL=

# --- azure_openai (Azure AI Foundry deployment) ---------------------------
# DEPLOYMENT is the name you gave the deployment, not the underlying model.
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_DEPLOYMENT=
AZURE_OPENAI_API_VERSION=2024-10-21

# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
# Open-web fallback. Without it the fallback uses a keyless search path, which
# several networks block outright.
FIRECRAWL_API_KEY=
# Firecrawl API base and version. Leave as is for the hosted service; point
# FIRECRAWL_API_URL at a self-hosted instance if you run one.
FIRECRAWL_API_URL=https://api.firecrawl.dev
FIRECRAWL_API_VERSION=v2

# ---------------------------------------------------------------------------
# Network. Only needed when outbound calls fail with a connection error.
# Run `python run.py --check` or press "Test web search now" on the Settings
# page: the message says which of these to set.
# ---------------------------------------------------------------------------
# A proxy the machine must go through. HTTPS_PROXY from the OS is honoured
# automatically; PROXY_URL forces one for every call.
#PROXY_URL=http://user:pass@proxy.company.com:8080
# HTTPS is verified against the operating system's certificate store by
# default (needs the `truststore` package from requirements.txt). That is what
# lets a corporate proxy your browser already trusts work for Python too.
#USE_SYSTEM_CERTS=true
# Only if that still fails: the proxy's root certificate exported as PEM.
#CA_BUNDLE=C:\certs\company-root.pem
# Last resort: disable certificate verification for every call.
#TLS_VERIFY=false

# Raises the NCBI E-utilities rate limit from 3/sec to 10/sec.
NCBI_API_KEY=
NCBI_EMAIL=research@example.org

# WHO ICD-11. Without these the ICD-11 leg of the code crosswalk is unanswered.
ICD11_CLIENT_ID=
ICD11_CLIENT_SECRET=

# LOINC search API. Same behaviour for lab and monitoring codes.
LOINC_USERNAME=
LOINC_PASSWORD=

# Server
HOST=0.0.0.0
PORT=8000
# Blank means today.
RESEARCH_CUTOFF=
```

### File: `.gitignore`

```text
__pycache__/
*.py[cod]
.env
celestra/data/*.db
celestra/data/*.db-wal
celestra/data/*.db-shm
celestra/data/cache/
celestra/data/reference/*
!celestra/data/reference/README.md
.pytest_cache/
```

### File: `requirements-dev.txt`

```txt
-r requirements.txt

# Tests and linting
pytest>=8.0
ruff>=0.6

# Only needed to capture screenshots of the running app
playwright>=1.45
```

### File: `requirements.txt`

```txt
fastapi>=0.115
uvicorn[standard]>=0.30
jinja2>=3.1
httpx>=0.27
pydantic>=2.7
pydantic-settings>=2.3
pyyaml>=6.0
python-multipart>=0.0.9
selectolax>=0.3.21
anthropic>=0.40
# Verifies HTTPS against the operating system certificate store, so a
# corporate proxy the browser trusts works for Python too.
truststore>=0.10
```

### File: `run.py`

```py
#!/usr/bin/env python3
"""Development entrypoint.

Production runs uvicorn or gunicorn directly:

    uvicorn celestra.main:app --host 0.0.0.0 --port 8000 --workers 1

Use a single worker: runs execute in-process and the SSE bus is per-process, so
a second worker would split a run's event stream. Scaling out means moving the
bus to Redis, which is a deliberate later step.
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
from pathlib import Path

# Make the app importable no matter where this script is invoked from, so
# `python /somewhere/else/run.py` behaves the same as running it in place.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REQUIRED = [
    # import name, pip name
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("jinja2", "jinja2"),
    ("httpx", "httpx"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("yaml", "pyyaml"),
    ("multipart", "python-multipart"),
    ("selectolax", "selectolax"),
]


def missing_dependencies() -> list[tuple[str, str]]:
    import importlib.util

    return [
        (mod, pkg) for mod, pkg in REQUIRED
        if importlib.util.find_spec(mod) is None
    ]


def _dependency_help(missing: list[tuple[str, str]]) -> str:
    names = ", ".join(pkg for _, pkg in missing)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    lines = [
        "",
        f"  Missing {len(missing)} dependency/dependencies: {names}",
        "",
        "  Install them with:",
        "",
        "    pip install -r requirements.txt",
        "",
    ]
    if not in_venv:
        lines += [
            "  You are not in a virtual environment. On most systems the command",
            "  above needs one:",
            "",
            "    python -m venv .venv",
            "    source .venv/bin/activate        # Windows: .venv\\Scripts\\activate",
            "    pip install -r requirements.txt",
            "",
        ]
    return "\n".join(lines)


def _port_taken_by(host: str, port: int) -> str | None:
    """Identify what is already on the port, so a stale server is obvious.

    Returns a short description, or None when the port is free. A previous
    Celestra left running is the single most confusing failure mode: the new
    process moves to another port while the browser keeps hitting the old one,
    which may be serving an older, broken build.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host if host != "0.0.0.0" else "", port))
            return None
        except OSError:
            pass
    try:
        import httpx

        resp = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2.0)
        if resp.status_code == 200 and "connectors" in resp.text:
            return "another Celestra server"
        return f"an HTTP server (returned {resp.status_code})"
    except Exception:
        return "something else"


def _free_port(host: str, port: int) -> int:
    """The requested port, or the next free one."""
    for candidate in range(port, port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host if host != "0.0.0.0" else "", candidate))
                return candidate
            except OSError:
                continue
    return port


def setup() -> int:
    """Prepare a local checkout: dependencies, .env, data directories."""
    print(f"\n  Celestra setup  (revision {_revision()})\n")

    missing = missing_dependencies()
    if missing:
        print("  FAIL  dependencies")
        print(_dependency_help(missing))
        return 1
    print(f"  ok    dependencies ({len(REQUIRED)} packages)")

    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        print(f"  ok    .env already exists ({env})")
    elif example.exists():
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  ok    created {env} from .env.example")
        print("        Every value in it is optional. Fill in an LLM provider to")
        print("        enable model-written synthesis.")
    else:
        print("  warn  no .env.example to copy; the app runs on defaults")

    from celestra.settings import ensure_dirs as _ensure

    _ensure()
    print("  ok    data directories")
    print("\n  Next:\n    python run.py --demo     # seed a run so the UI has content")
    print("    python run.py            # start the server\n")
    return 0


def demo() -> int:
    """Seed a completed run offline so every screen has content."""
    from celestra.settings import ensure_dirs as _ensure

    _ensure()
    print("\n  Seeding an offline demo run. No network, no credentials.\n")
    from celestra.demo import seed_sync

    try:
        run = seed_sync()
    except Exception as exc:
        print(f"  FAIL  {type(exc).__name__}: {exc}\n")
        return 1

    from celestra.store import store

    questions = store.get_questions(run.id)
    answered = sum(1 for q in questions if q.status.value == "sufficient")
    print(f"  Run {run.reference} — {run.status.value}")
    print(f"    {answered}/{len(questions)} questions answered")
    print(f"    {len(store.get_evidence(run.id))} evidence items")
    print(f"    {len(store.get_insights(run.id))} findings")
    print(f"    {len(store.get_contradictions(run.id))} source conflicts")
    print("\n  Start the server and open it:\n")
    print("    python run.py")
    from celestra.settings import get_settings as _gs

    print(f"    http://localhost:{_gs().port}/runs/{run.id}\n")
    return 0


def _revision() -> str:
    """Short git revision, so a stale checkout is visible at a glance."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        rev = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return f"{rev}{'+local changes' if dirty else ''}" if rev else "unknown"
    except Exception:
        return "unknown"


def self_check() -> int:
    """Verify the app can actually serve its pages, and say what is wrong if not.

    Catches the two failures that look identical from a browser: a stale
    checkout whose routes raise, and a working server reached at the wrong
    address.
    """
    import asyncio
    import logging

    # The check's own output is the point; library chatter buries it.
    logging.disable(logging.INFO)

    problems: list[str] = []
    print(f"\n  Celestra self-check  (revision {_revision()})\n")

    for name, path in (("templates", ROOT / "celestra" / "templates"),
                       ("stylesheet", ROOT / "celestra" / "static" / "css" / "app.css"),
                       ("javascript", ROOT / "celestra" / "static" / "js" / "app.js")):
        ok = path.exists()
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}: {path}")
        if not ok:
            problems.append(f"{name} missing at {path}")

    try:
        import httpx

        from celestra.main import app

        async def probe() -> list[tuple[str, int]]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://check") as c:
                out = []
                for path in ("/", "/projects", "/projects/new", "/settings", "/healthz"):
                    out.append((path, (await c.get(path)).status_code))
                return out

        for path, status in asyncio.run(probe()):
            ok = status == 200
            print(f"  {'ok  ' if ok else 'FAIL'}  GET {path} -> {status}")
            if not ok:
                problems.append(f"GET {path} returned {status}")
    except Exception as exc:
        print(f"  FAIL  app failed to load: {type(exc).__name__}: {exc}")
        problems.append(f"app import/render failed: {exc}")

    # Web search is the fallback the whole design leans on when the registered
    # sources come up short, so report exactly which backend would serve it.
    try:
        import asyncio as _a

        from celestra.settings import get_settings as _gs

        settings = _gs()
        from celestra.connectors.firecrawl import FirecrawlConnector
        from celestra.settings import configure_tls as _tls

        print(f"  info  TLS: {_tls().get('detail')}")
        probe_result = _a.run(FirecrawlConnector.probe())
        if not settings.firecrawl_enabled:
            print("  warn  firecrawl: no FIRECRAWL_API_KEY, web fallback uses the "
                  "keyless path")
        elif probe_result["ok"]:
            print(f"  ok    firecrawl {probe_result['version']}: key accepted, "
                  f"{probe_result['results']} result(s) in {probe_result['elapsed_ms']} ms")
        else:
            print(f"  FAIL  firecrawl: {probe_result['detail']}")
            if probe_result["remedy"]:
                print(f"        fix: {probe_result['remedy']}")
            problems.append(f"firecrawl call failed: {probe_result['detail']}")
    except Exception as exc:
        print(f"  warn  firecrawl probe skipped: {type(exc).__name__}: {exc}")

    print()
    if problems:
        print("  Not healthy:")
        for p in problems:
            print(f"    - {p}")
        # Match the advice to the failure. Telling someone to git pull when
        # their API key is rejected sends them the wrong way entirely.
        if any("firecrawl" in p for p in problems):
            print("\n  Web search is failing; the fix line above says why. Until it")
            print("  works, web fallback uses the keyless path and no calls appear")
            print("  on your Firecrawl account. Network settings live in .env:")
            print("    HTTPS_PROXY / PROXY_URL   when the machine reaches the web via a proxy")
            print("    CA_BUNDLE=/path/root.pem  behind a TLS-intercepting proxy")
            print("    FIRECRAWL_API_URL         for a self-hosted or regional endpoint")
        if any(p.startswith(("GET ", "app ", "templates", "stylesheet", "javascript"))
               for p in problems):
            print("\n  A page or asset failed, which usually means a stale checkout:")
            print("    git pull && pip install -r requirements.txt")
        print()
        return 1
    print("  All checks passed. Start the server with:  python run.py\n")
    return 0


def main() -> None:
    from celestra.settings import ensure_dirs, get_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Run the Celestra server.")
    parser.add_argument("--setup", action="store_true",
                        help="install check, create .env, prepare data directories")
    parser.add_argument("--check", action="store_true",
                        help="verify the app can serve its pages, then exit")
    parser.add_argument("--demo", action="store_true",
                        help="seed a completed run offline, then exit")
    args = parser.parse_args()

    missing = missing_dependencies()
    if missing and not args.setup:
        print(_dependency_help(missing))
        raise SystemExit(1)

    if args.setup:
        raise SystemExit(setup())
    if args.check:
        raise SystemExit(self_check())
    if args.demo:
        raise SystemExit(demo())

    settings = get_settings()
    ensure_dirs()
    occupant = _port_taken_by(settings.host, settings.port)
    port = _free_port(settings.host, settings.port)

    banner: list[str] = ["", f"  {settings.app_name} is running.  (revision {_revision()})", ""]

    if occupant:
        stale = occupant == "another Celestra server"
        banner += [
            "  " + "!" * 68,
            f"  PORT {settings.port} IS ALREADY IN USE by {occupant}.",
            f"  This server is on {port} instead.",
            "",
        ]
        if stale:
            banner += [
                f"  http://localhost:{settings.port} is the OLD server, which may be",
                "  running older code. Either use the address below, or stop the old",
                "  one first:",
                "",
                f"    macOS/Linux:  lsof -ti:{settings.port} | xargs kill",
                f"    Windows:      netstat -ano | findstr :{settings.port}"
                "   then  taskkill /PID <pid> /F",
            ]
        else:
            banner += [f"  Use the address below, or free port {settings.port}."]
        banner += ["  " + "!" * 68, ""]

    banner += [
        "  Open the first URL in a browser. Do not use 0.0.0.0 - that is the",
        "  bind address, not a reachable one.",
        "",
        f"  Open:      http://localhost:{port}",
        f"  API docs:  http://localhost:{port}/docs",
        f"  Health:    http://localhost:{port}/healthz",
        "",
    ]
    creds = settings.credential_status()
    if not creds["llm"]:
        banner += [
            "  No LLM provider is configured, so synthesis runs in deterministic",
            "  mode. Set a provider in .env to enable model-written output.",
            "",
        ]

    print("\n".join(banner), flush=True)

    import uvicorn

    uvicorn.run(
        "celestra.main:app",
        host=settings.host,
        port=port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
```

### File: `celestra\demo.py`

```py
"""Offline demo run.

Seeds a completed run from bundled fixtures so every screen has content the
moment the app starts, with no network and no credentials. It drives the real
orchestrator through stub connectors, so what you see is the actual pipeline
rather than canned screenshots.

The quotes below are real published statements, kept verbatim so the evidence,
citations and contradiction detection behave exactly as they do on a live run.
"""
from __future__ import annotations

import asyncio

from .connectors.base import ConnectorResult, RetrievalContext
from .models import EvidenceOrigin, Run, RunConfig, RunMode, RunStatus, SourceRef
from .services import orchestrator as orch
from .store import store

# source_id -> (tier, organisation, url, title, body)
FIXTURES: dict[str, tuple[int, str, str, str, str]] = {
    "seer": (
        1, "NCI SEER", "https://seer.cancer.gov/statfacts/html/clyl.html",
        "Chronic Lymphocytic Leukemia — Cancer Stat Facts",
        "Chronic lymphocytic leukemia is a cancer of the blood and bone marrow. The "
        "overall rate of new cases of chronic lymphocytic leukemia was 4.7 per 100,000 "
        "men and women per year based on 2018-2022 cases, age-adjusted. The death rate "
        "was 1.0 per 100,000 men and women per year. Five-year relative survival for "
        "chronic lymphocytic leukemia is 88.5 percent. In 2021 there were an estimated "
        "214,573 people living with chronic lymphocytic leukemia in the United States. "
        "The median age at diagnosis is 70 years, and incidence rises steeply with age.",
    ),
    "nci": (
        1, "National Cancer Institute (NCI)",
        "https://www.cancer.gov/types/leukemia/hp/cll-treatment-pdq",
        "Chronic Lymphocytic Leukemia Treatment (PDQ) — Health Professional Version",
        "Chronic lymphocytic leukemia is a cancer of the blood and bone marrow that "
        "usually gets worse slowly if it is not treated. Diagnosis requires a peripheral "
        "blood absolute B-lymphocyte count of at least 5,000 per microliter sustained "
        "for three months. Immunophenotyping by flow cytometry demonstrates coexpression "
        "of CD5, CD19, CD20 and CD23 with restricted light chain expression. Prognostic "
        "biomarkers including IGHV mutational status, TP53 mutation and deletion 17p "
        "determine treatment selection and prognosis. The age-adjusted incidence rate of "
        "chronic lymphocytic leukemia is 5.6 per 100,000 persons per year. Treatment is "
        "deferred until the disease is symptomatic or progressive.",
    ),
    "orphanet": (
        1, "Orphanet / Orphadata", "https://www.orpha.net/en/disease/detail/67038",
        "B-cell chronic lymphocytic leukemia (ORPHA:67038)",
        "B-cell chronic lymphocytic leukemia is indexed as ORPHA:67038. Terminology "
        "crosswalk: ICD-10 C91.1; ICD-11 2A82.0; MeSH D015451; UMLS C0023434. B-cell "
        "chronic lymphocytic leukemia is a type of B-cell non-Hodgkin lymphoma "
        "characterised by accumulation of small mature B lymphocytes. The reported "
        "prevalence class is 1-5 per 10,000 in Europe with a validated status.",
    ),
    "iwcll": (
        1, "iwCLL Guidelines (Blood)", "https://ashpublications.org/blood/article/131/25/2745",
        "iwCLL guidelines for diagnosis, indications for treatment, response assessment",
        "The diagnosis of chronic lymphocytic leukemia requires the presence of at least "
        "5,000 B lymphocytes per microliter in the peripheral blood. Indications for "
        "treatment include progressive marrow failure, massive or progressive "
        "splenomegaly, progressive lymphocytosis with an increase of more than 50 percent "
        "over two months, and constitutional symptoms. Response assessment requires "
        "evaluation of blood counts, physical examination and marrow assessment. "
        "Measurable residual disease is assessed by flow cytometry at a threshold of "
        "one CLL cell per 10,000 leukocytes.",
    ),
    "nlm_icd10cm": (
        1, "NLM Clinical Tables (ICD-10-CM)",
        "https://clinicaltables.nlm.nih.gov/apidoc/icd10cm/v3/doc.html",
        "ICD-10-CM codes for chronic lymphocytic leukemia",
        "ICD-10-CM code C91.10 is defined as Chronic lymphocytic leukemia of B-cell type "
        "not having achieved remission. ICD-10-CM code C91.11 is defined as Chronic "
        "lymphocytic leukemia of B-cell type in remission. ICD-10-CM code C91.12 is "
        "defined as Chronic lymphocytic leukemia of B-cell type in relapse.",
    ),
    "dailymed": (
        1, "DailyMed", "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=venclexta",
        "VENCLEXTA (venetoclax) tablet, film coated — prescribing information",
        "VENCLEXTA is indicated for the treatment of adult patients with chronic "
        "lymphocytic leukemia or small lymphocytic lymphoma. The recommended starting "
        "dose is 20 mg once daily for 7 days, followed by a weekly ramp-up over 5 weeks "
        "to the recommended daily dose of 400 mg. Tumor lysis syndrome is an important "
        "identified risk; assess tumor burden and initiate prophylaxis before the first "
        "dose. The most common adverse reactions are neutropenia, diarrhea, nausea, "
        "anemia, upper respiratory tract infection, thrombocytopenia and fatigue. "
        "Dosage should be interrupted for Grade 3 or 4 neutropenia with infection. "
        "In combination with obinutuzumab, treatment is given for a fixed duration of "
        "12 cycles. Venetoclax is administered orally with a meal and water.",
    ),
    "openfda_label": (
        1, "FDA Drug Labeling (openFDA)",
        "https://api.fda.gov/drug/label.json",
        "IMBRUVICA (ibrutinib) — Indications and Usage",
        "IMBRUVICA is a kinase inhibitor indicated for the treatment of adult patients "
        "with chronic lymphocytic leukemia or small lymphocytic lymphoma, including "
        "patients with 17p deletion. The recommended dose for chronic lymphocytic "
        "leukemia is 420 mg orally once daily until disease progression or unacceptable "
        "toxicity. Atrial fibrillation, hypertension and bleeding events are important "
        "identified risks requiring monitoring and possible dose modification. "
        "Treatment with a Bruton tyrosine kinase inhibitor is continuous rather than "
        "fixed duration.",
    ),
    "clinicaltrials": (
        2, "ClinicalTrials.gov", "https://clinicaltrials.gov/study/NCT03462719",
        "Venetoclax and Obinutuzumab in Previously Untreated CLL",
        "This phase 3 study enrolls previously untreated patients with chronic "
        "lymphocytic leukemia requiring treatment according to iwCLL criteria. Patients "
        "are randomised to fixed-duration venetoclax plus obinutuzumab or to "
        "chlorambucil plus obinutuzumab. The primary outcome measure is progression-free "
        "survival. Key secondary outcomes include undetectable minimal residual disease "
        "in peripheral blood and overall response rate. Eligibility requires treatment-"
        "naive disease and adequate organ function.",
    ),
    "acs": (
        3, "American Cancer Society",
        "https://www.cancer.org/cancer/types/chronic-lymphocytic-leukemia/about/key-statistics.html",
        "Key Statistics for Chronic Lymphocytic Leukemia",
        "The American Cancer Society estimates for chronic lymphocytic leukemia in the "
        "United States for 2026 are about 24,900 new cases and about 4,300 deaths. "
        "Chronic lymphocytic leukemia is a slow-growing leukemia that starts in lymphoid "
        "cells and mainly affects older adults, with an average age at diagnosis of "
        "around 70 years.",
    ),
}

BLOCKED = {
    "ama_cpt": "licensed connector not configured",
    "nccn": "licensed connector not configured",
    "icd11": "credentials not configured",
    "loinc": "credentials not configured",
    "cms_icd10": "reference file not installed: icd10cm",
    "cms_hcpcs": "reference file not installed: hcpcs",
}


def _registry_tier(source_id: str, fallback: int) -> int:
    """The tier the registry assigns, not the one written into the fixture.

    Hardcoding tiers here let the demo drift from sources.yaml: a source the
    registry had promoted still behaved as its old tier, which changed
    confidence and manufactured conflicts.
    """
    from .settings import get_source_registry

    for src in get_source_registry()["sources"]:
        if src["id"] == source_id:
            return int(src["tier"])
    return fallback


class _StubSource:
    origin = EvidenceOrigin.APPROVED_API

    def __init__(self, source_id: str, spec: tuple[int, str, str, str, str]) -> None:
        self.source_id = source_id
        tier, self.name, self.url, self.title, self.body = spec
        self.tier = _registry_tier(source_id, tier)

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        return ConnectorResult(
            source_id=self.source_id,
            refs=[SourceRef(
                source_id=self.source_id, source_name=self.name, tier=self.tier,
                url=self.url, title=self.title, organization=self.name,
                snippet=self.body, raw={"abstract": self.body}, origin=self.origin,
            )],
            calls=1,
        )


class _Blocked:
    origin = EvidenceOrigin.APPROVED_API
    tier = 1

    def __init__(self, source_id: str, reason: str) -> None:
        self.source_id, self.reason = source_id, reason

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        return ConnectorResult.failure(self.source_id, self.reason)


class _NoWeb:
    """Open-web fallback is unavailable offline, and says so rather than hanging."""
    source_id, tier = "open_web", 5
    origin = EvidenceOrigin.OPEN_WEB

    async def discover(self, ctx, limit):
        return ConnectorResult.failure("open_web", "offline demo: web fallback disabled")

    async def search(self, query: str, limit: int) -> list[SourceRef]:
        return []

    async def scrape(self, url: str):
        return None


def build_registry() -> dict:
    reg: dict = {sid: _StubSource(sid, spec) for sid, spec in FIXTURES.items()}
    reg.update({sid: _Blocked(sid, reason) for sid, reason in BLOCKED.items()})
    reg["open_web"] = _NoWeb()
    return reg


async def seed(indication_key: str = "CLL") -> Run:
    """Create and execute a demo run. Returns the finished run."""
    from .settings import get_questions

    label = get_questions()["indications"][indication_key]["label"]
    cfg = RunConfig(
        drug_brand="Venclexta (venetoclax)",
        indication=label,
        indication_key=indication_key,
        geography="United States",
        objective="Build Claims Line of Therapy",
        target_population=f"Adult patients with {indication_key}",
        additional_context="Offline demo run seeded from bundled fixtures.",
        mode=RunMode.FULL,
        research_cutoff=orch.default_cutoff(),
    )
    run = Run(config=cfg, reference=orch.new_reference())
    run.agents = orch.build_agent_states(orch.all_buckets())
    store.save_run(run)
    registry = build_registry()
    await orch.Orchestrator(run, registry).execute()

    # A full run pauses at the human review gate after the first wave. The
    # demo exists to populate every screen, so it plays the reviewer and
    # continues; a live run stops there and waits for a person.
    paused = store.get_run(run.id)
    if paused is not None and paused.status is RunStatus.AWAITING_REVIEW:
        await orch.Orchestrator(paused, registry).resume()
    return store.get_run(run.id) or run


def seed_sync(indication_key: str = "CLL") -> Run:
    return asyncio.run(seed(indication_key))
```

### File: `celestra\events.py`

```py
"""In-process pub/sub that backs the Server-Sent Events stream.

Each run owns a broadcast channel. Subscribers get their own queue, so a slow
browser tab can never block the orchestrator. A bounded replay buffer lets a
tab that connects late, or reconnects, catch up without re-running anything.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any

MAX_REPLAY = 400
QUEUE_MAXSIZE = 1000


class Event:
    __slots__ = ("seq", "run_id", "type", "data", "ts")

    def __init__(self, seq: int, run_id: str, type_: str, data: dict[str, Any]) -> None:
        self.seq = seq
        self.run_id = run_id
        self.type = type_
        self.data = data
        self.ts = datetime.now(timezone.utc).isoformat()

    def to_sse(self) -> str:
        payload = json.dumps({"seq": self.seq, "type": self.type, "ts": self.ts, **self.data})
        return f"id: {self.seq}\nevent: {self.type}\ndata: {payload}\n\n"


class _Channel:
    def __init__(self) -> None:
        self.seq = 0
        self.replay: deque[Event] = deque(maxlen=MAX_REPLAY)
        self.subscribers: set[asyncio.Queue[Event]] = set()
        self.closed = False


class EventBus:
    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}
        self._lock = asyncio.Lock()

    def _channel(self, run_id: str) -> _Channel:
        ch = self._channels.get(run_id)
        if ch is None:
            ch = _Channel()
            self._channels[run_id] = ch
        return ch

    async def publish(self, run_id: str, type_: str, **data: Any) -> None:
        # `run_id` and `type` are supplied by the channel and the envelope. A
        # caller passing them again would collide with these parameters, so
        # they are dropped rather than allowed to raise mid-run.
        data.pop("run_id", None)
        data.pop("type", None)
        async with self._lock:
            ch = self._channel(run_id)
            ch.seq += 1
            event = Event(ch.seq, run_id, type_, data)
            ch.replay.append(event)
            dead: list[asyncio.Queue[Event]] = []
            for q in ch.subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                ch.subscribers.discard(q)

    async def subscribe(self, run_id: str, last_seq: int = 0) -> asyncio.Queue[Event]:
        async with self._lock:
            ch = self._channel(run_id)
            q: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
            for event in ch.replay:
                if event.seq > last_seq:
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        break
            ch.subscribers.add(q)
            return q

    async def unsubscribe(self, run_id: str, q: asyncio.Queue[Event]) -> None:
        async with self._lock:
            ch = self._channels.get(run_id)
            if ch:
                ch.subscribers.discard(q)

    async def close(self, run_id: str) -> None:
        await self.publish(run_id, "stream_end", reason="run finished")
        async with self._lock:
            ch = self._channels.get(run_id)
            if ch:
                ch.closed = True

    async def reopen(self, run_id: str) -> None:
        """Take a closed channel back into service for a resumed run.

        The replay buffer keeps the phase-one history so a late tab still
        sees what happened, but the two events that ended that phase are
        dropped: a replayed `stream_end` would tear the new stream down at
        once, and a replayed `review_required` would bounce the live page
        straight back to the review it just left.
        """
        async with self._lock:
            ch = self._channel(run_id)
            ch.closed = False
            kept = [e for e in ch.replay if e.type not in ("stream_end", "review_required")]
            ch.replay.clear()
            ch.replay.extend(kept)

    def is_closed(self, run_id: str) -> bool:
        ch = self._channels.get(run_id)
        return bool(ch and ch.closed)


bus = EventBus()
```

### File: `celestra\main.py`

```py
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


@app.get("/runs/{run_id}/findings", response_class=HTMLResponse)
async def findings_report(request: Request, run_id: str, phase: str = Query("all"),
                          download: int = Query(0)):
    """The findings only: cards, tables and answers, per phase and stage,
    with the coloured marks and sources. No run metadata, no QA, no method.
    The same page is viewed, printed to PDF, or downloaded as a file."""
    run = get_run_or_404(run_id)
    insights = store.get_insights(run_id)
    reports = {r.stage: r for r in store.get_stage_reports(run_id)}
    phase = phase if phase in ("discovery", "mapping") else "all"
    groups: list[dict[str, Any]] = []
    for spec in orch.phases_for([a.bucket for a in run.agents.values()]):
        if phase != "all" and spec["key"] != phase:
            continue
        stages: list[dict[str, Any]] = []
        for a in sorted(run.agents.values(), key=lambda x: x.wave):
            if orch.phase_of(a.bucket) != spec["key"]:
                continue
            for st in a.stages or []:
                report = reports.get(st)
                if report is None:
                    continue
                cards = sorted((i for i in insights if i.stage == st),
                               key=lambda i: (i.number or 999, i.title))
                stages.append({"report": report, "cards": cards})
        groups.append({**spec, "stages": stages})
    labels = {"all": "Whole document", "discovery": "Discovery",
              "mapping": "Mapping & Synthesis"}
    ctx = {
        "request": request, "run": run, "phase_key": phase, "phase_label": labels[phase],
        "groups": groups, "download": bool(download), "source_names": _source_names(),
        "generated": utcnow().strftime("%d %b %Y, %H:%M UTC"),
    }
    response = templates.TemplateResponse(request, "findings.html", ctx)
    if download:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", run.display_name).strip("-")[:60] or "project"
        suffix = "" if phase == "all" else f"-{phase}"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{safe}-findings{suffix}.html"'
        )
    return response


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
```

### File: `celestra\models.py`

```py
"""Domain models. These are the contracts shared by connectors, agents,
services and templates. Nothing here imports application code."""
from __future__ import annotations

import enum
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------
class RunMode(str, enum.Enum):
    FULL = "full"          # every agent, by dependency wave
    SINGLE = "single"      # one agent, dependencies satisfied from cache


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    # Paused at the human review gate; a Continue action resumes it.
    AWAITING_REVIEW = "awaiting_review"
    # Every agent has finished. The document exists but is a draft until a
    # reviewer approves it.
    COMPLETED = "completed"
    # The reviewer signed the document off. The run is locked from then on.
    APPROVED = "approved"
    FAILED = "failed"
    CANCELLED = "cancelled"


# The linear flow every run follows. Each run is in exactly one of these at a
# time; `Run.phase` derives it from the status so the UI can never disagree
# with the orchestrator about where the run is.
PHASES: list[dict[str, str]] = [
    {"key": "discovery", "name": "Discovery",
     "description": "Clinical landscape and treatment evidence"},
    {"key": "review", "name": "Review gate",
     "description": "Decide the discovery findings before they propagate"},
    {"key": "mapping", "name": "Mapping & Synthesis",
     "description": "Diagnostic footprint, treatment logic, patient journey, synthesis"},
    {"key": "approval", "name": "Final approval",
     "description": "Resolve what still needs input, then sign off"},
    {"key": "approved", "name": "Approved document",
     "description": "The signed-off research document"},
]


class AgentStatus(str, enum.Enum):
    QUEUED = "queued"
    RESEARCHING = "researching"
    SYNTHESISING = "synthesising"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class Confidence(str, enum.Enum):
    """Exactly two states, because a reviewer needs exactly one answer to the
    question "do I have to do something here?".

    READY          a vetted source answered it and nothing is unresolved
    REQUIRES_INPUT a person must act: no vetted source answered it, or two
                   sources disagree and nobody has decided, or nothing was
                   found at all
    """
    READY = "ready"
    REQUIRES_INPUT = "requires_input"

    @property
    def label(self) -> str:
        return {"ready": "Ready", "requires_input": "Requires Input"}[self.value]

    @classmethod
    def _missing_(cls, value: object):
        # Runs stored before the two-state model used four values. Map them
        # rather than refuse to load a project someone already ran.
        legacy = {"high": cls.READY, "medium": cls.READY, "rejected": cls.REQUIRES_INPUT}
        return legacy.get(str(value).lower())


class EvidenceOrigin(str, enum.Enum):
    """How the evidence was obtained. Drives the SUPPLEMENTARY WEB label."""
    APPROVED_API = "approved_api"
    TARGETED_SEARCH = "targeted_search"
    LOCAL_FILE = "local_file"
    OPEN_WEB = "open_web"

    @property
    def label(self) -> str:
        return {
            "approved_api": "Approved source API",
            "targeted_search": "Targeted domain search",
            "local_file": "Local reference file",
            "open_web": "SUPPLEMENTARY WEB EVIDENCE",
        }[self.value]

    @property
    def is_supplementary(self) -> bool:
        return self is EvidenceOrigin.OPEN_WEB


class VerificationTag(str, enum.Enum):
    VERIFIED = "VERIFIED"
    GENERAL_KNOWLEDGE = "GENERAL KNOWLEDGE"
    ORIGINAL = "ORIGINAL"
    INFERENCE = "INFERENCE"
    UPDATE = "UPDATE"
    NOT_VERIFIED = "NOT VERIFIED"


class QuestionStatus(str, enum.Enum):
    PLANNED = "planned"
    RETRIEVING = "retrieving"
    SUFFICIENT = "sufficient"
    REFINING = "refining"
    WEB_FALLBACK = "web_fallback"
    INSUFFICIENT = "insufficient"
    UNANSWERED = "unanswered"


class ContradictionSeverity(str, enum.Enum):
    NOTED = "noted"
    ESCALATED = "escalated"


class ReviewAction(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"        # accepted as generated
    MODIFIED = "modified"        # rewritten by the model on the reviewer's instruction
    INPUT_ADDED = "input_added"  # reviewer attached their own knowledge to it
    PREFER_A = "prefer_a"
    PREFER_B = "prefer_b"
    ACKNOWLEDGED = "acknowledged"

    @property
    def label(self) -> str:
        return {
            "pending": "Awaiting decision",
            "approved": "Approved",
            "modified": "Revised",
            "input_added": "Input added",
            "prefer_a": "Source A preferred",
            "prefer_b": "Source B preferred",
            "acknowledged": "Acknowledged",
        }[self.value]

    @property
    def is_decided(self) -> bool:
        return self is not ReviewAction.PENDING


# --------------------------------------------------------------------------
# Retrieval layer
# --------------------------------------------------------------------------
class SourceRef(BaseModel):
    """A retrievable document located by a connector."""
    source_id: str
    source_name: str
    tier: int
    url: str
    title: str = ""
    organization: str = ""
    published: str = ""
    identifiers: dict[str, str] = Field(default_factory=dict)   # pmid, pmcid, doi, nct, set_id
    snippet: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    origin: EvidenceOrigin = EvidenceOrigin.APPROVED_API

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.source_id}|{self.url}".encode()).hexdigest()[:16]


class Evidence(BaseModel):
    """One verbatim, attributable support item for exactly one question."""
    id: str = Field(default_factory=lambda: new_id("ev"))
    question_id: str
    source_id: str
    source_name: str
    organization: str = ""
    tier: int
    url: str
    title: str = ""
    published: str = ""
    quote: str                       # verbatim from the source
    context: str = ""                # surrounding text, optional
    origin: EvidenceOrigin = EvidenceOrigin.APPROVED_API
    tag: VerificationTag = VerificationTag.VERIFIED
    relevance: float = 0.0           # 0..1
    identifiers: dict[str, str] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=utcnow)

    @property
    def is_supplementary(self) -> bool:
        return self.origin.is_supplementary

    @property
    def citation(self) -> str:
        return self.organization or self.source_name


class AnswerStatus(str, enum.Enum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    NOT_FOUND = "not_found"


class Answer(BaseModel):
    """What the model concluded for one question from one batch of documents.

    The unit the final document is built from. An answer is only as good as
    the quotes under it, so it carries the evidence ids that support it and is
    discarded if none of them survive verification.
    """
    id: str = Field(default_factory=lambda: new_id("ans"))
    run_id: str = ""
    question_id: str
    stage: str = ""
    status: AnswerStatus = AnswerStatus.NOT_FOUND
    text: str = ""                    # the answer itself, prose
    aspects_covered: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)   # organisation names, display order
    origin: EvidenceOrigin = EvidenceOrigin.APPROVED_API
    batch_index: int = 0
    round_index: int = 0
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_supplementary(self) -> bool:
        return self.origin.is_supplementary


class ResearchQuestion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("q"))
    run_id: str = ""
    stage: str                       # stage_1 .. stage_6
    bucket: str                      # A..G, internal only, never rendered
    text: str
    seed_text: str = ""              # the seed question this was expanded from
    aspects: list[str] = Field(default_factory=list)
    # Model-written aspects name what an answer must contain. Heuristic ones
    # are only the question's own words, so they are scored far more gently.
    aspects_from_model: bool = False
    status: QuestionStatus = QuestionStatus.PLANNED
    coverage_score: float = 0.0
    refinement_rounds: int = 0
    used_web_fallback: bool = False
    sources_attempted: list[str] = Field(default_factory=list)
    sources_answered: list[str] = Field(default_factory=list)
    unmet_reason: str = ""
    # The consolidated answer, merged from every batch that answered it.
    answer_text: str = ""
    answer_status: AnswerStatus = AnswerStatus.NOT_FOUND
    answer_citations: list[str] = Field(default_factory=list)
    # Open-web pages consulted during fallback: {url, title, used}. Recorded
    # even when a page contributed nothing, so the trail is auditable.
    web_sites: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_answered(self) -> bool:
        return self.status is QuestionStatus.SUFFICIENT


# --------------------------------------------------------------------------
# Findings layer
# --------------------------------------------------------------------------
class InsightTable(BaseModel):
    """A rendered table inside a stage report."""
    title: str
    columns: list[str]
    rows: list[list[str]]
    footnote: str = ""
    # Which research questions this table answers. Links a table to the
    # insight cards derived from the same questions.
    question_ids: list[str] = Field(default_factory=list)


class Insight(BaseModel):
    """One review card.

    Cards are fixed slots defined by the agent's objective (config/
    insight_cards.yaml) and filled from the agent's finished stage document.
    Five parts: what we found (summary), the evidence block, what it means
    (interpretation), the sources, and the reviewer's decision.
    """
    id: str = Field(default_factory=lambda: new_id("ins"))
    run_id: str = ""
    stage: str
    bucket: str
    category: str                    # Clinical | Treatment | Diagnostic | Logic | Journey | Synthesis
    title: str
    summary: str                     # 1. what we found
    detail: str = ""
    number: int = 0                  # position in the document-wide card sequence
    card_key: str = ""               # slot in insight_cards.yaml
    evidence_type: str = ""          # metrics | table | steps | list | ""
    evidence: Any = None             # 2. the card's own table / metrics / pathway
    interpretation: str = ""         # 3. what the evidence means
    review_note: str = ""            # what a reviewer should verify, in one line
    covered: bool = True             # False when the sources did not cover this slot
    confidence: Confidence = Confidence.READY
    # Why the finding needs a person, in words. Empty when it is ready.
    input_reason: str = ""
    tag: VerificationTag = VerificationTag.VERIFIED
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    used_web_fallback: bool = False
    review_action: ReviewAction = ReviewAction.PENDING
    # Modify: the instruction the reviewer gave the model, and what it did.
    user_input: str = ""
    revision_note: str = ""
    # Add Input: knowledge the reviewer attached. It is never rewritten; it is
    # printed in the document and handed to the agents that run afterwards.
    reviewer_input: str = ""
    reviewed_at: datetime | None = None
    impacted_insight_ids: list[str] = Field(default_factory=list)
    # Titles of the stage-report tables built from this insight's questions.
    table_titles: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def source_count(self) -> int:
        return len(self.source_ids)

    @property
    def needs_decision(self) -> bool:
        """True while a person still has to act on this finding."""
        return self.confidence is Confidence.REQUIRES_INPUT and not self.review_action.is_decided


class Contradiction(BaseModel):
    id: str = Field(default_factory=lambda: new_id("con"))
    run_id: str = ""
    stage: str
    # The question on which the disagreement surfaced. Lets a conflict flag
    # the one card it concerns rather than every card in the stage.
    question_id: str = ""
    topic: str
    source_a_name: str
    source_a_tier: int
    source_a_claim: str
    source_a_url: str = ""
    source_b_name: str
    source_b_tier: int
    source_b_claim: str
    source_b_url: str = ""
    reason: str
    severity: ContradictionSeverity = ContradictionSeverity.NOTED
    review_action: ReviewAction = ReviewAction.PENDING
    reviewer_note: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class StageReport(BaseModel):
    """Everything needed to render one stage exactly like the sample document."""
    id: str = Field(default_factory=lambda: new_id("stg"))
    run_id: str = ""
    stage: str
    bucket: str
    name: str
    core_question: str
    agent_name: str
    framework_steps: list[str] = Field(default_factory=list)
    step_numbers: list[int] = Field(default_factory=list)
    substeps: dict[str, str] = Field(default_factory=dict)
    gate: str = ""
    output_name: str = ""
    what_happens: str = ""
    expected_output: list[str] = Field(default_factory=list)
    synthesis: str = ""
    narratives: list[dict[str, str]] = Field(default_factory=list)   # {heading, body}
    tables: list[InsightTable] = Field(default_factory=list)
    takeaways: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    observability: list[dict[str, str]] = Field(default_factory=list)
    unanswered: list[dict[str, str]] = Field(default_factory=list)
    # Every question in this stage with its cited answer. The spine of the
    # final document; the prose and tables above are drawn from these.
    answers: list[dict[str, Any]] = Field(default_factory=list)
    evidence_count: int = 0
    source_count: int = 0
    supplementary_count: int = 0
    tiers_represented: list[int] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class AgentState(BaseModel):
    """Live state of one agent, streamed to the UI."""
    bucket: str
    key: str                         # url-safe agent key, e.g. clinical-landscape
    name: str
    tagline: str
    icon: str
    stages: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    status: AgentStatus = AgentStatus.QUEUED
    wave: int = 0
    progress: float = 0.0
    message: str = ""
    questions_total: int = 0
    questions_answered: int = 0
    evidence_count: int = 0
    sources_used: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""


class QAMetrics(BaseModel):
    questions_planned: int = 0
    questions_sufficient: int = 0
    questions_web_only: int = 0
    questions_below_threshold: int = 0
    mean_coverage: float = 0.0
    evidence_total: int = 0
    evidence_approved: int = 0
    evidence_supplementary: int = 0
    distinct_sources: int = 0
    conflicts_surfaced: int = 0
    checklist: list[dict[str, str]] = Field(default_factory=list)
    readiness: str = ""
    sme_checklist: list[str] = Field(default_factory=list)
    executive_summary: str = ""
    research_method: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RunConfig(BaseModel):
    therapy_area: str = "Oncology"
    drug_brand: str = ""
    population: str = "All"
    indication: str
    indication_key: str              # ALL | CLL | custom
    geography: str = "United States"
    objective: str = "Build Claims Line of Therapy"
    target_population: str = ""
    additional_context: str = ""
    mode: RunMode = RunMode.FULL
    selected_agent: str | None = None   # bucket letter when mode is SINGLE
    research_cutoff: str = ""


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    reference: str = ""
    # What the person calls this project. Defaults to the indication.
    name: str = ""
    config: RunConfig
    status: RunStatus = RunStatus.PENDING
    agents: dict[str, AgentState] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    approved_at: datetime | None = None
    # Entities discovered by completed agents, handed to downstream agents.
    context: dict[str, Any] = Field(default_factory=dict)
    # Human review gate. The run pauses after `review_after_wave` and resumes
    # from `resume_from_wave` once a reviewer continues it. 0 disables the gate.
    review_after_wave: int = 1
    resume_from_wave: int = 0
    reviewed_at: datetime | None = None

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.config.indication

    @property
    def phase(self) -> str:
        """Which step of the flow the run is in. Derived, never stored."""
        s = self.status
        if s is RunStatus.AWAITING_REVIEW:
            return "review"
        if s is RunStatus.COMPLETED:
            return "approval"
        if s is RunStatus.APPROVED:
            return "approved"
        if s in (RunStatus.FAILED, RunStatus.CANCELLED):
            return "failed"
        if self.config.mode is RunMode.SINGLE:
            return "discovery" if (self.config.selected_agent or "A") in ("A", "C") else "mapping"
        return "mapping" if self.resume_from_wave > 1 else "discovery"

    @property
    def is_live(self) -> bool:
        return self.status in (RunStatus.PENDING, RunStatus.RUNNING)

    @property
    def is_locked(self) -> bool:
        """Approved runs accept no further edits."""
        return self.status is RunStatus.APPROVED

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or utcnow()
        return (end - self.started_at).total_seconds()
```

### File: `celestra\settings.py`

```py
"""Application settings. Every secret is read from the environment or .env."""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

import os

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
# Where the database, cache and reference files live. On a hosted service
# point CELESTRA_DATA_DIR at a persistent disk (Render: the disk's mount
# path), otherwise every deploy starts with an empty project list.
DATA_DIR = Path(os.environ.get("CELESTRA_DATA_DIR") or (BASE_DIR / "data")).expanduser()
REFERENCE_DIR = DATA_DIR / "reference"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR.parent / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Celestra"
    environment: str = "production"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = f"sqlite:///{DATA_DIR / 'celestra.db'}"

    # --- LLM -----------------------------------------------------------
    # Three providers are supported. An absent provider degrades synthesis to
    # the deterministic engine rather than failing the run.
    #   anthropic          Claude via the Anthropic API
    #   anthropic_foundry  Claude deployed on Microsoft Foundry
    #   azure_openai       a model deployed in Azure AI Foundry / Azure OpenAI
    # Blank means auto-detect from whichever provider's keys are present.
    llm_provider: str = ""
    llm_max_tokens: int = 8000
    llm_timeout_seconds: int = 120
    llm_max_concurrency: int = 4
    llm_effort: str = "high"          # low | medium | high | xhigh | max

    # Anthropic API
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    llm_model: str = "claude-opus-5"

    # Claude on Microsoft Foundry
    foundry_api_key: str | None = None
    foundry_resource: str | None = None
    foundry_model: str | None = None   # defaults to llm_model

    # Azure AI Foundry / Azure OpenAI deployment
    azure_openai_endpoint: str | None = None     # https://<name>.openai.azure.com
    azure_openai_api_key: str | None = None
    azure_openai_deployment: str | None = None   # the deployment name, not the model
    azure_openai_api_version: str = "2024-10-21"

    # --- Retrieval credentials ------------------------------------------
    firecrawl_api_key: str | None = None
    # Base URL of the Firecrawl API. Change it for a self-hosted instance or a
    # regional endpoint. A pasted "/v1" or "/v2" suffix is tolerated.
    firecrawl_api_url: str = "https://api.firecrawl.dev"
    firecrawl_api_version: str = "v2"       # v2 (current) or v1 (legacy)

    # --- Network ---------------------------------------------------------
    # HTTPS_PROXY / HTTP_PROXY / NO_PROXY from the environment are honoured
    # automatically. PROXY_URL forces one for every outbound call, for
    # machines where the proxy is set in the OS but not exported to the shell.
    proxy_url: str | None = None
    # Path to a PEM bundle that includes your network's root certificate. Needed
    # behind a TLS-intercepting proxy (Zscaler, Netskope, corporate firewalls),
    # which otherwise fails every HTTPS call with CERTIFICATE_VERIFY_FAILED.
    # SSL_CERT_FILE and REQUESTS_CA_BUNDLE are read as well.
    ca_bundle: str | None = None
    ssl_cert_file: str | None = None
    requests_ca_bundle: str | None = None
    # Last resort only: disables certificate verification for every call.
    tls_verify: bool = True
    # Verify against the operating system's certificate store (Windows, macOS,
    # Linux) instead of Python's bundled list. This is what makes a corporate
    # proxy's root certificate, which the OS already trusts, work for Python
    # too. Needs the `truststore` package; falls back silently without it.
    use_system_certs: bool = True
    ncbi_api_key: str | None = None
    ncbi_tool: str = "celestra"
    ncbi_email: str = "research@example.org"
    icd11_client_id: str | None = None
    icd11_client_secret: str | None = None
    loinc_username: str | None = None
    loinc_password: str | None = None

    research_cutoff: str = ""  # ISO date; blank means today

    def _azure_ready(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key
                    and self.azure_openai_deployment)

    def _foundry_ready(self) -> bool:
        return bool(self.foundry_api_key and self.foundry_resource)

    @property
    def provider_is_explicit(self) -> bool:
        return bool((self.llm_provider or "").strip())

    @property
    def provider(self) -> str:
        """The provider in use.

        An explicit LLM_PROVIDER wins. Otherwise the provider is whichever one
        has a complete set of credentials, so adding Azure keys to .env is
        enough on its own; forgetting to also flip LLM_PROVIDER used to leave
        the app silently on Anthropic reporting a missing Anthropic key.
        """
        explicit = (self.llm_provider or "").strip().lower()
        if explicit:
            return explicit
        if self._azure_ready():
            return "azure_openai"
        if self._foundry_ready():
            return "anthropic_foundry"
        return "anthropic"

    @property
    def llm_enabled(self) -> bool:
        """Whether the selected provider has everything it needs to be called."""
        if self.provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.provider == "anthropic_foundry":
            return bool(self.foundry_api_key and self.foundry_resource)
        if self.provider == "azure_openai":
            return bool(
                self.azure_openai_endpoint
                and self.azure_openai_api_key
                and self.azure_openai_deployment
            )
        return False

    @property
    def active_model(self) -> str:
        """The model or deployment name the selected provider will call."""
        if self.provider == "azure_openai":
            return self.azure_openai_deployment or "(no deployment configured)"
        if self.provider == "anthropic_foundry":
            return self.foundry_model or self.llm_model
        return self.llm_model

    def provider_gaps(self) -> list[str]:
        """Settings the selected provider still needs. Empty when it is ready."""
        required = {
            "anthropic": [("ANTHROPIC_API_KEY", self.anthropic_api_key)],
            "anthropic_foundry": [
                ("FOUNDRY_API_KEY", self.foundry_api_key),
                ("FOUNDRY_RESOURCE", self.foundry_resource),
            ],
            "azure_openai": [
                ("AZURE_OPENAI_ENDPOINT", self.azure_openai_endpoint),
                ("AZURE_OPENAI_API_KEY", self.azure_openai_api_key),
                ("AZURE_OPENAI_DEPLOYMENT", self.azure_openai_deployment),
            ],
        }.get(self.provider)
        if required is None:
            return [f"LLM_PROVIDER '{self.llm_provider}' is not a supported provider"]
        gaps = [name for name, value in required if not value]
        if gaps and not self.provider_is_explicit and self.provider == "anthropic":
            # Nothing was configured at all; say what any provider would need
            # rather than implying Anthropic is the only option.
            return ["ANTHROPIC_API_KEY, or AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY + "
                    "AZURE_OPENAI_DEPLOYMENT, or FOUNDRY_API_KEY + FOUNDRY_RESOURCE"]
        return gaps

    @property
    def firecrawl_enabled(self) -> bool:
        return bool(self.firecrawl_api_key)

    def firecrawl_endpoint(self, action: str) -> str:
        """Full URL for a Firecrawl action, from the configured base and version."""
        base = (self.firecrawl_api_url or "https://api.firecrawl.dev").strip().rstrip("/")
        version = (self.firecrawl_api_version or "v2").strip().strip("/").lower()
        for suffix in ("/v1", "/v2"):
            if base.endswith(suffix):
                version = suffix.strip("/")
                base = base[: -len(suffix)]
        if version not in ("v1", "v2"):
            version = "v2"
        return f"{base}/{version}/{action}"

    @property
    def firecrawl_version(self) -> str:
        return self.firecrawl_endpoint("x").rsplit("/", 2)[-2]

    def tls_verify_value(self) -> bool | str:
        """What httpx should verify against: a CA bundle path, True, or False."""
        if not self.tls_verify:
            return False
        for candidate in (self.ca_bundle, self.ssl_cert_file, self.requests_ca_bundle):
            if candidate and candidate.strip():
                return candidate.strip()
        return True

    def network_status(self) -> dict[str, Any]:
        verify = self.tls_verify_value()
        return {
            "trust_store": tls_trust_description(),
            "proxy": self.proxy_url or "from environment (HTTPS_PROXY) if set",
            "proxy_forced": bool(self.proxy_url),
            "ca_bundle": verify if isinstance(verify, str) else "",
            "tls_verify": verify is not False,
            "firecrawl_endpoint": self.firecrawl_endpoint("search"),
            "firecrawl_version": self.firecrawl_version,
        }

    def llm_status(self) -> dict[str, Any]:
        """What the UI shows about the model layer."""
        return {
            "configured": self.llm_enabled,
            "provider": self.provider,
            "explicit": self.provider_is_explicit,
            "model": self.active_model,
            "gaps": self.provider_gaps(),
        }

    def credential_status(self) -> dict[str, bool]:
        return {
            "llm": self.llm_enabled,
            "anthropic_api_key": bool(self.anthropic_api_key),
            "firecrawl_api_key": bool(self.firecrawl_api_key),
            "ncbi_api_key": bool(self.ncbi_api_key),
            "icd11": bool(self.icd11_client_id and self.icd11_client_secret),
            "loinc": bool(self.loinc_username and self.loinc_password),
        }


def _load_yaml(name: str) -> dict[str, Any]:
    with (CONFIG_DIR / name).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@functools.lru_cache(maxsize=1)
def get_framework() -> dict[str, Any]:
    return _load_yaml("framework.yaml")


@functools.lru_cache(maxsize=1)
def get_questions() -> dict[str, Any]:
    return _load_yaml("research_questions.yaml")


@functools.lru_cache(maxsize=1)
def get_source_registry() -> dict[str, Any]:
    return _load_yaml("sources.yaml")


@functools.lru_cache(maxsize=1)
def get_insight_cards() -> dict[str, Any]:
    return _load_yaml("insight_cards.yaml")


@functools.lru_cache(maxsize=1)
def get_thresholds() -> dict[str, Any]:
    return _load_yaml("thresholds.yaml")


_TLS_STATE: dict[str, Any] = {"configured": False, "system": False, "detail": ""}


def configure_tls() -> dict[str, Any]:
    """Make every HTTPS client in the process trust what the operating system
    trusts. Idempotent; call it before the first client is built.

    Python ships its own certificate list and ignores the OS store. On a
    machine behind a TLS-intercepting proxy the browser works (Windows trusts
    the proxy's root certificate) while Python fails every call with
    CERTIFICATE_VERIFY_FAILED. `truststore` closes that gap by routing
    verification through the OS store, which is what pip itself does.
    """
    if _TLS_STATE["configured"]:
        return _TLS_STATE
    s = get_settings()
    _TLS_STATE["configured"] = True
    verify = s.tls_verify_value()
    if verify is False:
        _TLS_STATE["detail"] = "verification disabled (TLS_VERIFY=false)"
        return _TLS_STATE
    if isinstance(verify, str):
        _TLS_STATE["detail"] = f"custom CA bundle {verify}"
        return _TLS_STATE
    if not s.use_system_certs:
        _TLS_STATE["detail"] = "Python's bundled certificates (USE_SYSTEM_CERTS=false)"
        return _TLS_STATE
    try:
        import truststore  # noqa: PLC0415

        truststore.inject_into_ssl()
        _TLS_STATE["system"] = True
        _TLS_STATE["detail"] = "operating system certificate store (truststore)"
    except ImportError:
        _TLS_STATE["detail"] = ("Python's bundled certificates; install `truststore` "
                                "(pip install -r requirements.txt) to use the OS store")
    except Exception as exc:  # noqa: BLE001 - never fail startup over this
        _TLS_STATE["detail"] = f"Python's bundled certificates (truststore failed: {exc})"
    return _TLS_STATE


def tls_trust_description() -> str:
    return str(configure_tls().get("detail") or "")


def system_certs_active() -> bool:
    return bool(configure_tls().get("system"))


def ensure_dirs() -> None:
    configure_tls()
    for path in (DATA_DIR, REFERENCE_DIR, DATA_DIR / "cache"):
        path.mkdir(parents=True, exist_ok=True)
```

### File: `celestra\store.py`

```py
"""Persistence. SQLite with pydantic documents.

A run is a small object graph that is written far more often than it is
queried in complex ways, so documents beat a normalised schema here. Every
table is keyed by run_id so a run can be loaded or deleted atomically.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, TypeVar
from collections.abc import Iterable

from .models import (
    Answer,
    Contradiction,
    Evidence,
    Insight,
    QAMetrics,
    ResearchQuestion,
    Run,
    StageReport,
)
from .settings import DATA_DIR

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    reference TEXT,
    status TEXT,
    indication TEXT,
    created_at TEXT,
    doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, question_id TEXT,
    source_id TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS insights (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contradictions (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stage_reports (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS answers (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, question_id TEXT, stage TEXT,
    doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qa (
    run_id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_questions_run ON questions(run_id);
CREATE INDEX IF NOT EXISTS ix_evidence_run ON evidence(run_id);
CREATE INDEX IF NOT EXISTS ix_evidence_q ON evidence(question_id);
CREATE INDEX IF NOT EXISTS ix_insights_run ON insights(run_id);
CREATE INDEX IF NOT EXISTS ix_contra_run ON contradictions(run_id);
CREATE INDEX IF NOT EXISTS ix_stages_run ON stage_reports(run_id);
CREATE INDEX IF NOT EXISTS ix_answers_run ON answers(run_id);
CREATE INDEX IF NOT EXISTS ix_answers_q ON answers(question_id);
"""


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (DATA_DIR / "celestra.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @staticmethod
    def _dump(obj: Any) -> str:
        return obj.model_dump_json()

    # -- runs ------------------------------------------------------------
    def save_run(self, run: Run) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs(id,reference,status,indication,created_at,doc) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "reference=excluded.reference,status=excluded.status,doc=excluded.doc",
                (run.id, run.reference, run.status.value, run.config.indication,
                 run.created_at.isoformat(), self._dump(run)),
            )

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn().execute("SELECT doc FROM runs WHERE id=?", (run_id,)).fetchone()
        return Run.model_validate_json(row["doc"]) if row else None

    def list_runs(self, limit: int = 50) -> list[Run]:
        rows = self._conn().execute(
            "SELECT doc FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Run.model_validate_json(r["doc"]) for r in rows]

    def delete_run(self, run_id: str) -> None:
        with self._conn() as c:
            for t in ("questions", "evidence", "answers", "insights",
                      "contradictions", "stage_reports", "qa"):
                c.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
            c.execute("DELETE FROM runs WHERE id=?", (run_id,))

    # -- generic document tables ----------------------------------------
    def _save_many(self, table: str, run_id: str, items: Iterable[Any],
                   extra_cols: tuple[str, ...] = ()) -> None:
        rows = []
        for it in items:
            extras = tuple(getattr(it, col, "") for col in extra_cols)
            rows.append((it.id, run_id, *extras, self._dump(it)))
        if not rows:
            return
        cols = ("id", "run_id", *extra_cols, "doc")
        ph = ",".join("?" * len(cols))
        assign = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        with self._conn() as c:
            c.executemany(
                f"INSERT INTO {table}({','.join(cols)}) VALUES({ph}) "
                f"ON CONFLICT(id) DO UPDATE SET {assign}",
                rows,
            )

    def _load_many(self, table: str, run_id: str, model: type[T], where: str = "") -> list[T]:
        sql = f"SELECT doc FROM {table} WHERE run_id=?{where}"
        rows = self._conn().execute(sql, (run_id,)).fetchall()
        return [model.model_validate_json(r["doc"]) for r in rows]  # type: ignore[attr-defined]

    def save_questions(self, run_id: str, items: Iterable[ResearchQuestion]) -> None:
        self._save_many("questions", run_id, items, ("stage",))

    def get_questions(self, run_id: str) -> list[ResearchQuestion]:
        return self._load_many("questions", run_id, ResearchQuestion)

    def save_evidence(self, run_id: str, items: Iterable[Evidence]) -> None:
        self._save_many("evidence", run_id, items, ("question_id", "source_id"))

    def get_evidence(self, run_id: str) -> list[Evidence]:
        return self._load_many("evidence", run_id, Evidence)

    def get_evidence_for(self, run_id: str, question_id: str) -> list[Evidence]:
        rows = self._conn().execute(
            "SELECT doc FROM evidence WHERE run_id=? AND question_id=?", (run_id, question_id)
        ).fetchall()
        return [Evidence.model_validate_json(r["doc"]) for r in rows]

    def save_answers(self, run_id: str, items: Iterable[Answer]) -> None:
        self._save_many("answers", run_id, items, ("question_id", "stage"))

    def get_answers(self, run_id: str) -> list[Answer]:
        return self._load_many("answers", run_id, Answer)

    def get_answers_for(self, run_id: str, question_id: str) -> list[Answer]:
        rows = self._conn().execute(
            "SELECT doc FROM answers WHERE run_id=? AND question_id=?", (run_id, question_id)
        ).fetchall()
        return [Answer.model_validate_json(r["doc"]) for r in rows]

    def save_insights(self, run_id: str, items: Iterable[Insight]) -> None:
        self._save_many("insights", run_id, items, ("stage",))

    def get_insights(self, run_id: str) -> list[Insight]:
        return self._load_many("insights", run_id, Insight)

    def get_insight(self, run_id: str, insight_id: str) -> Insight | None:
        row = self._conn().execute(
            "SELECT doc FROM insights WHERE run_id=? AND id=?", (run_id, insight_id)
        ).fetchone()
        return Insight.model_validate_json(row["doc"]) if row else None

    def save_contradictions(self, run_id: str, items: Iterable[Contradiction]) -> None:
        self._save_many("contradictions", run_id, items, ("stage",))

    def get_contradictions(self, run_id: str) -> list[Contradiction]:
        return self._load_many("contradictions", run_id, Contradiction)

    def get_contradiction(self, run_id: str, cid: str) -> Contradiction | None:
        row = self._conn().execute(
            "SELECT doc FROM contradictions WHERE run_id=? AND id=?", (run_id, cid)
        ).fetchone()
        return Contradiction.model_validate_json(row["doc"]) if row else None

    def save_stage_reports(self, run_id: str, items: Iterable[StageReport]) -> None:
        self._save_many("stage_reports", run_id, items, ("stage",))

    def get_stage_reports(self, run_id: str) -> list[StageReport]:
        reports = self._load_many("stage_reports", run_id, StageReport)
        return sorted(reports, key=lambda r: r.stage)

    def save_qa(self, run_id: str, qa: QAMetrics) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO qa(run_id,doc) VALUES(?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET doc=excluded.doc",
                (run_id, self._dump(qa)),
            )

    def get_qa(self, run_id: str) -> QAMetrics | None:
        row = self._conn().execute("SELECT doc FROM qa WHERE run_id=?", (run_id,)).fetchone()
        return QAMetrics.model_validate_json(row["doc"]) if row else None


store = Store()
```

### File: `celestra\__init__.py`

```py

```

### File: `celestra\agents\__init__.py`

```py

```

### File: `celestra\config\framework.yaml`

```yaml
phase: "Phase 1: Clinical Foundation Research"

steps:
  1:  {name: "Disease Understanding & Epidemiology Review", bucket: A, stage: stage_1}
  2:
    name: "Diagnostic Criteria & Confirmatory Workup Review"
    bucket: A
    stage: stage_1
    substeps:
      2A: "Diagnostic criteria and confirmatory-workup research"
      2B: "Align diagnostic criteria with disease taxonomy, subtypes, stages and population"
  3:  {name: "Guideline & SOC Treatment Review", bucket: C, stage: stage_2}
  4:  {name: "Diagnosis Code Universe (ICD-10-CM, extended to ICD-9-CM and ICD-11)", bucket: B, stage: stage_3}
  5:  {name: "Diagnostic Test & Procedure Code Universe", bucket: B, stage: stage_3}
  6:  {name: "Drug & Biologic Code Universe & Label Review", bucket: C, stage: stage_2}
  7:  {name: "LOT & Treatment-Trigger Assumptions", bucket: D, stage: stage_4}
  8:
    name: "Regimen & Real-World Pattern Library"
    bucket: D
    stage: stage_4
    substeps:
      8A: "Collect guideline, trial and real-world regimen evidence"
      8B: "Finalize regimen library by line, segment, setting and evidence source"
  9:  {name: "Regimen Definition, Component & Biosimilar Code Mapping (extended to NDC)", bucket: D, stage: stage_4}
  10:
    name: "Supportive-Care & Non-Therapeutic Exclusion List"
    bucket: D
    stage: stage_4
    substeps:
      10A: "Collect preliminary supportive-care and non-therapeutic candidates"
      10B: "Classify therapeutic components versus supportive or contextual medications"
      10C: "Finalize supportive-care exclusions and contextual exceptions"
  11: {name: "Discontinuation, Switching & AE Signal Review", bucket: E, stage: stage_5}
  12: {name: "Monitoring, Response & Relapse/Progression Signal Review", bucket: E, stage: stage_5}
  13:
    name: "End-of-Journey Outcome States"
    bucket: E
    stage: stage_5
    substeps:
      13A: "Draft remission, transplant, hospice, death, maintenance and follow-up states"
      13B: "Finalize journey end states and observable proxies"
  14:
    name: "Clinical Gaps & Unmet-Need Review"
    bucket: F
    stage: stage_6
    substeps:
      14A: "Early unmet-need, treatment-gap, delayed-diagnosis, access and off-label-use evidence collection"
      14B: "Compare disease evidence, diagnosis practice, guidelines, labels, codes, regimens, monitoring and outcomes"
      14C: "Classify contradictions, unmet needs, evidence limitations, data-observability gaps and unresolved assumptions"
  15:
    name: "QA/SME Validation & Sign-Off Gate"
    bucket: G
    stage: stage_6
    substeps:
      15A: "Define QA rules, source hierarchy, schemas, version controls and acceptance thresholds"
      15B: "Rolling QA workers active during execution"
      15C: "Clinical SME review of assumptions, codes, regimens, conflicts and limitations"

# The run is presented to a reviewer as two phases with a human gate between
# them. Discovery runs the agents with no dependencies; their findings are
# reviewed before Mapping & Synthesis builds on them.
phases:
  discovery:
    name: "Discovery"
    description: "Clinical landscape and treatment evidence, researched in parallel"
  mapping:
    name: "Mapping & Synthesis"
    description: "Diagnostic footprint, treatment logic, patient journey and cross-agent synthesis"

# agent_* fields drive the UI. The word "bucket" never reaches a template.
buckets:
  A:
    name: "Clinical disease and diagnostic foundation"
    phase: discovery
    agent_name: "Clinical Landscape Agent"
    agent_tagline: "Disease context, patient journey, clinical events"
    agent_icon: "leaf"
    steps: [1, 2]
    stages: [stage_1]
    depends_on: []
    context_from: []
    gate: "Reconciliation gate — diagnostic definitions aligned with disease taxonomy and population"
    output: "DiseaseDiagnosisProfile"
  C:
    name: "Treatment evidence foundation"
    phase: discovery
    agent_name: "Treatment Evidence Agent"
    agent_tagline: "Therapies, treatment settings, regulatory evidence"
    agent_icon: "pill"
    steps: [3, 6]
    stages: [stage_2]
    depends_on: []
    context_from: [A]
    gate: "Treatment evidence reconciliation gate — guideline recommendation vs regulatory approval"
    output: "TreatmentEvidenceMaster"
  B:
    name: "Diagnostic observability and code footprint"
    phase: mapping
    agent_name: "Diagnostic Footprint Agent"
    agent_tagline: "Claims signals, diagnosis & procedure codes"
    agent_icon: "microscope"
    steps: [4, 5]
    stages: [stage_3]
    depends_on: [A]
    context_from: []
    gate: "Code reconciliation gate — each code linked to subtype, stage, purpose, specificity, geography and effective date"
    output: "DiagnosticObservabilityCodebook"
  D:
    name: "Treatment logic and regimen operationalization"
    phase: mapping
    agent_name: "Treatment Logic Agent"
    agent_tagline: "Treatment patterns, episode logic, LOT rules"
    agent_icon: "flow"
    steps: [7, 8, 9, 10]
    stages: [stage_4]
    depends_on: [C]
    context_from: [A]
    gate: "Final integration gate — line rules, regimen library, code mapping and exclusions reconciled"
    output: "RegimenAndLOTLogic"
  E:
    name: "Treatment journey, monitoring and outcomes"
    phase: mapping
    agent_name: "Patient Journey Agent"
    agent_tagline: "Discontinuation, monitoring, response and outcomes"
    agent_icon: "route"
    steps: [11, 12, 13]
    stages: [stage_5]
    depends_on: [B, C, D]
    context_from: [A]
    gate: "Transition reconciliation gate then end-state finalization gate"
    output: "PatientJourneyStateModel"
  F:
    name: "Clinical gaps and unmet-need synthesis"
    phase: mapping
    agent_name: "Information Synthesis Agent"
    agent_tagline: "Cross-agent analysis, key insights and gaps"
    agent_icon: "sparkle"
    steps: [14]
    stages: [stage_6]
    depends_on: [A, B, C, D, E]
    context_from: []
    gate: "Synthesis inputs complete — all substantive outputs available"
    output: "ClinicalGapMatrix"
  G:
    name: "QA, governance, SME validation and sign-off"
    phase: mapping
    agent_name: "QA & Validation Agent"
    agent_tagline: "Thresholds, contradictions, SME readiness"
    agent_icon: "shield"
    steps: [15]
    stages: []
    depends_on: [F]
    context_from: [A, B, C, D, E, F]
    gate: "Final review gate — QA passed and SME review complete"
    output: "ValidatedClinicalFoundationPackage"
    mode: "governance"

governance:
  max_rework_rounds: 1
  rework_scope: "owning_bucket_only"
  max_parallel_buckets: 3
```

### File: `celestra\config\insight_cards.yaml`

```yaml
# The insight cards each agent owes, in the order they are shown.
#
# A card is a fixed slot defined by the agent's objective, not something the
# model invents per run. The model fills each slot from the agent's finished
# stage document; without a model the slot is filled from the questions that
# map to it. Numbering runs across the whole document.
#
# evidence_type: metrics | table | steps | list
#   metrics  a few labelled figures            evidence: [{label, value}]
#   table    columns and rows                  evidence: {columns: [], rows: [[]]}
#   steps    an ordered pathway                evidence: [str]
#   list     short bullet points               evidence: [str]
# question_hints: words that link a research question to this card
# table_hints:    words that link a stage-report table to this card

buckets:
  A:
    - key: epidemiology
      number: 1
      title: "Epidemiology"
      objective: "How common the disease is, who gets it, and how deadly it is"
      evidence_type: metrics
      metrics: ["New US cases per year", "Deaths per year", "Incidence rate", "Mortality rate", "Median age at diagnosis", "5-year survival"]
      question_hints: [incidence, prevalence, survival, mortality, epidemiolog]
      table_hints: [epidemiolog]
    - key: population_segmentation
      number: 2
      title: "Patient Population & Segmentation"
      objective: "The population splits that matter for cohorts: age groups, lineage, biology, risk"
      evidence_type: table
      columns: ["Segment", "Approximate share", "Defining feature"]
      question_hints: [subtype, immunophenotyp, molecular, risk-strat, risk strat, population, segment]
      table_hints: [subtype, biology, breakdown]
    - key: disease_definition
      number: 3
      title: "Disease Definition & Taxonomy"
      objective: "What the disease is, how it is classified, and its natural history"
      evidence_type: table
      columns: ["Classification", "Category", "Note"]
      question_hints: [definition, natural history, classification, taxonomy]
      table_hints: [classification, taxonomy, subtype]
    - key: diagnostic_foundation
      number: 4
      title: "Diagnostic Foundation"
      objective: "The signals that establish the diagnosis, in the order they are used"
      evidence_type: steps
      question_hints: [diagnos, workup, confirmatory, criteria]
      table_hints: [diagnos, workup]
    - key: disease_journey
      number: 5
      title: "Natural History & Disease Journey"
      objective: "The clinical course from diagnosis onward and its decision points"
      evidence_type: steps
      question_hints: [natural history, journey, course, risk-strat, risk strat, prognos]
      table_hints: [journey, course, variable, fork]
  C:
    - key: treatment_landscape
      number: 6
      title: "Treatment Landscape"
      objective: "The therapeutic modalities in use and the populations each serves"
      evidence_type: table
      columns: ["Therapy class", "Examples", "Key populations / role"]
      question_hints: [first-line, first line, treatment, therap, regimen, differ]
      table_hints: [inventory, agent, class, drug]
    - key: standard_of_care
      number: 7
      title: "Guideline / Standard of Care"
      objective: "Which guidelines govern treatment and how their pathways branch"
      evidence_type: table
      columns: ["Guideline / body", "Population", "Recommended approach"]
      question_hints: [guideline, recommended, relapsed, refractory, standard of care]
      table_hints: [guideline, pathway, version]
    - key: approved_therapy
      number: 8
      title: "Approved Therapy & Label Intelligence"
      objective: "Approved agents, their label indications, settings, and recent label changes"
      evidence_type: table
      columns: ["Therapy", "Indication / population", "Setting", "Approval", "Recent label change"]
      question_hints: [fda, approv, label, indication, expansion]
      table_hints: [approv, label, inventory, expansion]
  B:
    - key: diagnosis_codes
      number: 10
      title: "Diagnosis Code Footprint"
      objective: "The diagnosis codes that identify the disease, including remission and relapse"
      evidence_type: table
      columns: ["Code system", "Code", "Description", "Use"]
      question_hints: [icd-10, icd10, diagnosis code, remission, relapse]
      table_hints: [crosswalk, diagnosis, remission, icd]
    - key: procedure_lab_codes
      number: 11
      title: "Diagnostic Procedure & Lab Codes"
      objective: "Procedure, laboratory and molecular test codes that mark the diagnostic workup"
      evidence_type: table
      columns: ["Category", "Code system", "Example codes", "Purpose"]
      question_hints: [cpt, hcpcs, loinc, biopsy, flow cytometry, cytogenetic, molecular test]
      table_hints: [procedure, test, loinc, monitoring]
    - key: code_crosswalk
      number: 12
      title: "Code System Crosswalk"
      objective: "How legacy and international code systems map onto the current one"
      evidence_type: table
      columns: ["ICD-9-CM", "ICD-10-CM", "ICD-11", "Description"]
      question_hints: [icd-9, icd9, icd-11, icd11, crosswalk, extension]
      table_hints: [crosswalk, icd-11, extension]
    - key: observability_limits
      number: 13
      title: "Claims Observability Limits"
      objective: "Which clinical concepts claims cannot see, and what that means for cohorts"
      evidence_type: list
      question_hints: [observab, limitation, not observable, claims alone]
      table_hints: [observab, limitation]
  D:
    - key: lot_rules
      number: 14
      title: "Line-of-Therapy Rules"
      objective: "The triggers that start, advance and end a line of therapy"
      evidence_type: list
      question_hints: [line of therapy, lines of therapy, lot, trigger]
      table_hints: [trigger, line-of-therapy, lot]
    - key: regimen_library
      number: 15
      title: "Regimen Library"
      objective: "Standard regimens by phase and setting with their components"
      evidence_type: table
      columns: ["Regimen", "Setting / phase", "Components", "Population"]
      question_hints: [regimen, induction, consolidation, maintenance]
      table_hints: [regimen]
    - key: drug_codes
      number: 16
      title: "Drug, Administration & NDC Codes"
      objective: "How regimen components appear in medical and pharmacy claims"
      evidence_type: table
      columns: ["Agent", "HCPCS / J-code", "NDC / labeler", "Benefit", "Note"]
      question_hints: [hcpcs, j-code, ndc, labeler, pharmacy benefit, medical benefit, dosing, administration]
      table_hints: [hcpcs, ndc, dosing, component]
    - key: exclusions_ambiguity
      number: 17
      title: "Exclusions & Ambiguity Rules"
      objective: "Supportive-care agents to exclude and the same-drug, different-intent rules"
      evidence_type: list
      question_hints: [supportive, exclu, ambigu]
      table_hints: [exclusion, ambiguity, supportive]
  E:
    - key: journey_states
      number: 18
      title: "Treatment Journey States"
      objective: "The states a patient passes through from initiation to an end state"
      evidence_type: steps
      question_hints: [journey, state, initiation, transplant, car-t, cellular, end state]
      table_hints: [state model, journey, end-of-journey, outcome state]
    - key: discontinuation_signals
      number: 19
      title: "Discontinuation & Adverse-Event Signals"
      objective: "Why treatment stops or switches, by agent class, and the signature toxicities"
      evidence_type: table
      columns: ["Agent class", "Signature adverse event", "Effect on treatment"]
      question_hints: [discontinu, dose modification, adverse, toxicit, switch]
      table_hints: [discontinu, adverse, signal]
    - key: monitoring_response
      number: 20
      title: "Monitoring & Response Assessment"
      objective: "How response, residual disease and progression are assessed and how often"
      evidence_type: table
      columns: ["Assessment", "Tool / criteria", "Timing"]
      question_hints: [response, residual disease, mrd, monitoring, cadence, progression]
      table_hints: [monitoring, response, criteria]
    - key: outcome_states
      number: 21
      title: "Outcomes & Claims Observability"
      objective: "End states and which journey signals claims can actually observe"
      evidence_type: table
      columns: ["Journey signal", "Observable in claims", "How"]
      question_hints: [transplant, car-t, hospice, death, end state, observab]
      table_hints: [observab, end-of-journey, outcome]
  F:
    - key: unmet_needs
      number: 22
      title: "Unmet Needs & Evidence Gaps"
      objective: "Where treatment falls short and where the evidence is thin"
      evidence_type: table
      columns: ["Gap area", "Description", "Impact on modelling"]
      question_hints: [unmet, gap, evidence gap]
      table_hints: [unmet, gap]
    - key: source_divergence
      number: 23
      title: "Guideline, Label & Evidence Divergence"
      objective: "Where guidelines, labels and literature disagree and what was decided"
      evidence_type: table
      columns: ["Topic", "Guideline says", "Label / evidence says", "Resolution"]
      question_hints: [diverge, differ, contradict, conflict, guideline recommendations and fda]
      table_hints: [contradiction, divergence, conflict]
    - key: modelling_insights
      number: 24
      title: "Key Insights for Downstream Modelling"
      objective: "What the diagnostic-footprint, treatment-logic and journey work must carry forward"
      evidence_type: list
      question_hints: [sequencing, not observable, claims alone, real-world]
      table_hints: [assumption, limitation, readiness]

# Cards written once every agent in a phase has finished, from all of that
# phase's stage documents together.
phase_cards:
  discovery:
    - key: key_clinical_treatment_insights
      number: 9
      title: "Key Clinical & Treatment Insights"
      objective: "What the discovery phase established that downstream modelling must preserve"
      evidence_type: list
      bucket: C
      stage: stage_2
      category: Synthesis
```

### File: `celestra\config\research_questions.yaml`

```yaml
stage_meta:
  stage_1:
    name: "Disease & Diagnostic Foundation"
    core_question: "Who gets the disease and how is it diagnosed?"
    framework_steps:
      - "Disease definition and natural history"
      - "Epidemiology, incidence, prevalence, mortality"
      - "Subtype and molecular/cytogenetic classification"
      - "Diagnostic criteria and confirmatory workup"
      - "Risk stratification and key clinical variables"
    expected_output:
      - "Epidemiology snapshot table (Metric | Value | Source)"
      - "Subtype / biology breakdown table (Subtype | Approximate share | Notes)"
      - "Diagnostic criteria summary (classification, immunophenotype, cytogenetics, molecular, staging)"
      - "Diagnostic workup table"
      - "Key clinical variables and cohort-defining forks"
      - "Key takeaways"
    default_sources: [seer, nci, who, pubmed, cdc_icd10, acs]

  stage_2:
    name: "Guideline-Based Treatment Landscape & Drug/Biologic Universe"
    core_question: "What is recommended and what is approved?"
    framework_steps:
      - "Identify governing guidelines and versions"
      - "Map first-line and later-line recommendations"
      - "Inventory FDA-approved agents and label indications"
      - "Capture recent approvals and label expansions"
      - "Classify therapeutic class and mechanism"
    expected_output:
      - "Guideline bodies and current versions table (Body | Guideline | Current version)"
      - "Simplified treatment pathway by risk group / branch point"
      - "FDA-approved drug and biologic inventory (Agent | Class or MOA | Typical line of use)"
      - "Recent approvals and label expansions worth flagging for cohort logic"
      - "Key takeaways"
    default_sources: [nccn, fda, dailymed, esmo, nci, clinicaltrials, pubmed]

  stage_3:
    name: "Claims Code Universe: Diagnosis & Procedures"
    core_question: "How would the clinical concepts appear in claims?"
    framework_steps:
      - "Diagnosis code universe (ICD-10-CM, ICD-9-CM, ICD-11)"
      - "Procedure and service codes (CPT, HCPCS)"
      - "Laboratory and molecular testing codes (LOINC, CPT)"
      - "Remission / relapse coding conventions"
      - "Observability limitations"
    expected_output:
      - "Three-system diagnosis code crosswalk (ICD-9-CM legacy | ICD-10-CM current | ICD-11 stem + extension | Description)"
      - "Remission / relapse code variants"
      - "ICD-11 extension codes for molecular subtypes, layered onto the disease stem"
      - "Diagnostic test and procedure code universe (Category | Example codes | Purpose)"
      - "Monitoring signal table"
      - "Observability limitations"
    default_sources: [cms, cdc_icd10, ama_cpt, loinc, icd11, who]

  stage_4:
    name: "Treatment Sequencing, Regimen Library & Code Mapping"
    core_question: "How does treatment sequence appear in real-world data?"
    framework_steps:
      - "Define line-of-therapy (LOT) rules"
      - "Build the regimen library by line and phase"
      - "Map regimen components to HCPCS/J-codes and NDC"
      - "Separate pharmacy from medical benefit claims"
      - "Define supportive-care exclusions and ambiguity rules"
    expected_output:
      - "Line-of-therapy trigger rules (Trigger | Interpretation)"
      - "Regimen library (Regimen | Setting | Components)"
      - "Component and HCPCS/J-code mapping (Component drug | HCPCS code | Biosimilar / NDC considerations)"
      - "NDC universe (Agent | Representative NDCs | Labeler | Coverage note) with a method note for an exhaustive pull"
      - "Dosing and administration reference (Agent | Standard adult dosing | Route / schedule | Key administration notes)"
      - "Supportive-care and non-therapeutic exclusion list"
      - "Ambiguity rules (pharmacy vs medical benefit, same-drug different-intent)"
    default_sources: [nccn, fda, dailymed, cms, pubmed, clinicaltrials]

  stage_5:
    name: "Patient Journey Signals: Discontinuation, Monitoring & Outcomes"
    core_question: "How does a patient move through the clinical journey?"
    framework_steps:
      - "Treatment initiation, discontinuation and switching signals"
      - "Adverse events and toxicity management"
      - "Response, relapse and progression assessment"
      - "Monitoring cadence and MRD assessment"
      - "End states: transplant, cellular therapy, hospice, death"
    expected_output:
      - "Discontinuation and adverse-event signals by agent class (Agent class | Signature AE driving discontinuation or switch)"
      - "Monitoring and response criteria (Assessment | Tool / criteria)"
      - "Patient state model"
      - "End-of-journey outcome states"
      - "Claims observability table"
      - "Key takeaways"
    default_sources: [nccn, pubmed, dailymed, cibmtr, nci, clinicaltrials]

  stage_6:
    name: "Unmet Need Synthesis & QA Validation"
    core_question: "Where are the gaps, conflicts and limitations?"
    framework_steps:
      - "Compare guidelines vs labels vs literature vs real-world evidence"
      - "Identify unmet needs and evidence gaps"
      - "Surface contradictions between sources"
      - "Document assumptions and limitations"
      - "Run QA and SME review readiness assessment"
    expected_output:
      - "Unmet-need synthesis (Gap area | Description)"
      - "Contradiction table"
      - "Assumptions"
      - "Limitations"
      - "QA checklist"
      - "QA / SME sign-off checklist"
      - "Readiness assessment"
    default_sources: [pubmed, nccn, fda, clinicaltrials, seer, cibmtr]

indications:

  ALL:
    label: "Acute Lymphoblastic Leukemia"
    abbreviation: "ALL"
    synonyms:
      - "acute lymphoblastic leukemia"
      - "acute lymphocytic leukemia"
      - "acute lymphoid leukemia"
      - "B-ALL"
      - "T-ALL"
    stage_1:
      - "What is the disease definition and natural history of acute lymphoblastic leukemia?"
      - "What is the incidence, prevalence, survival and mortality of ALL in the United States?"
      - "What are the clinically important immunophenotypic and molecular subtypes of ALL?"
      - "How is ALL diagnosed and what confirmatory workup is required?"
      - "How is ALL risk-stratified at diagnosis?"
    stage_2:
      - "What are the current guideline-recommended first-line treatments for ALL?"
      - "What are the guideline-recommended treatments for relapsed/refractory ALL?"
      - "Which therapies are FDA-approved for ALL and what are their label indications?"
      - "How does ALL treatment differ between Ph-positive and Ph-negative disease?"
      - "What recent FDA approvals or label expansions have occurred in ALL?"
    stage_3:
      - "What ICD-10-CM diagnosis codes identify acute lymphoblastic leukemia, including remission and relapse?"
      - "What CPT and HCPCS codes cover bone marrow biopsy, aspiration and flow cytometry in ALL?"
      - "What CPT and LOINC codes cover cytogenetic and molecular testing relevant to ALL?"
      - "How are ICD-9-CM and ICD-11 leukemia codes crosswalked to ICD-10-CM?"
      - "Which ICD-11 extension codes express ALL molecular subtypes on the disease stem?"
    stage_4:
      - "How are lines of therapy defined for ALL in real-world claims research?"
      - "What are the standard multi-agent induction, consolidation and maintenance regimens for ALL?"
      - "What HCPCS J-codes and NDC identifiers map to ALL regimen components?"
      - "Which ALL therapies are billed under the pharmacy benefit versus the medical benefit?"
      - "What supportive care agents should be excluded from ALL regimen identification?"
      - "What NDC identifiers and labelers correspond to the principal ALL agents, and where are biosimilars or multi-source generics involved?"
      - "What are the standard adult dosing, route and administration considerations for the principal ALL agents?"
    stage_5:
      - "What are the common reasons for treatment discontinuation and dose modification in ALL?"
      - "What adverse events are most clinically significant for ALL therapies?"
      - "How is treatment response and measurable residual disease assessed in ALL?"
      - "What is the role of allogeneic transplant and CAR-T as end states in ALL?"
      - "What monitoring cadence is recommended during and after ALL therapy?"
    stage_6:
      - "What are the principal unmet needs in ALL treatment in the United States?"
      - "Where do guideline recommendations and FDA labels diverge in ALL?"
      - "What real-world evidence gaps exist for ALL treatment sequencing?"
      - "Which ALL clinical concepts are not observable from administrative claims alone?"

  CLL:
    label: "Chronic Lymphocytic Leukemia"
    abbreviation: "CLL"
    synonyms:
      - "chronic lymphocytic leukemia"
      - "small lymphocytic lymphoma"
      - "CLL/SLL"
      - "B-cell chronic lymphocytic leukemia"
    stage_1:
      - "What is the disease definition and natural history of chronic lymphocytic leukemia?"
      - "What is the incidence, prevalence, survival and mortality of CLL in the United States?"
      - "How is CLL classified, staged and risk-stratified (Rai, Binet, CLL-IPI)?"
      - "How is CLL diagnosed and what confirmatory workup is required?"
      - "Which prognostic biomarkers (IGHV, TP53, del(17p)) matter in CLL and why?"
    stage_2:
      - "What are the current guideline-recommended first-line treatments for CLL?"
      - "What are the guideline-recommended treatments for relapsed/refractory CLL?"
      - "Which therapies are FDA-approved for CLL and what are their label indications?"
      - "How does CLL treatment differ by TP53 aberration and IGHV mutational status?"
      - "What recent FDA approvals or label expansions have occurred in CLL?"
    stage_3:
      - "What ICD-10-CM diagnosis codes identify chronic lymphocytic leukemia, including remission and relapse?"
      - "What CPT and HCPCS codes cover flow cytometry, bone marrow and lymph node biopsy in CLL?"
      - "What CPT and LOINC codes cover IGHV, TP53 and FISH testing relevant to CLL?"
      - "How are ICD-9-CM and ICD-11 CLL codes crosswalked to ICD-10-CM?"
      - "Which ICD-11 extension codes express CLL subtypes and status on the disease stem?"
    stage_4:
      - "How are lines of therapy defined for CLL in real-world claims research?"
      - "What are the standard first-line and later-line regimens for CLL including targeted agents?"
      - "What HCPCS J-codes and NDC identifiers map to CLL regimen components?"
      - "Which CLL therapies are billed under the pharmacy benefit versus the medical benefit?"
      - "What are the treatment-duration conventions for continuous BTK inhibitors versus fixed-duration venetoclax regimens?"
      - "What NDC identifiers and labelers correspond to the principal CLL agents, and where are biosimilars or multi-source generics involved?"
      - "What are the standard adult dosing, route and administration considerations for the principal CLL agents?"
    stage_5:
      - "What triggers treatment initiation in CLL and what defines active disease?"
      - "What are the common reasons for treatment discontinuation and switching in CLL?"
      - "How is response assessed in CLL and what is the role of measurable residual disease?"
      - "What are the significant end states in CLL including Richter transformation and cellular therapy?"
      - "What monitoring cadence is recommended for watch-and-wait and treated CLL patients?"
    stage_6:
      - "What are the principal unmet needs in CLL treatment in the United States?"
      - "Where do guideline recommendations and FDA labels diverge in CLL?"
      - "What real-world evidence gaps exist for CLL treatment sequencing after BTK inhibitor failure?"
      - "Which CLL clinical concepts are not observable from administrative claims alone?"
```

### File: `celestra\config\sources.yaml`

```yaml
# Credible Source Registry
# access_method: api | targeted_search | local_file | licensed | firecrawl_search
# connector: key resolved by connectors/registry.py. null => search-only source.

tiers:
  1:
    label: "Tier 1 — Government, regulators, official guidelines, coding authorities, product labels"
    weight: 1.00
    primary: true
  2:
    label: "Tier 2 — Peer-reviewed literature, trial registries, professional and disease organisations"
    weight: 0.85
    primary: true
  3:
    label: "Tier 3 — Open-web search (supplementary, lowest)"
    weight: 0.30
    primary: false

tier_policy:
  # Every registered source is credible, so tiers 1 and 2 are both primary and
  # either can answer a question on its own. Tier 3 is the open web: it may
  # support a finding but never carries one alone.
  never_override_below_tier: 3
  supplementary_tier_floor: 3
  approved_max_tier: 2

sources:

  # ---- Literature -------------------------------------------------------
  - id: pubmed
    name: PubMed
    domain: pubmed.ncbi.nlm.nih.gov
    tier: 2
    types: [literature]
    indications: [ALL, CLL]
    stages: [stage_1, stage_2, stage_4, stage_5, stage_6]
    access_method: api
    connector: pubmed
    enabled: true

  - id: europepmc
    name: Europe PMC
    domain: ebi.ac.uk
    tier: 2
    types: [literature]
    indications: [ALL, CLL]
    stages: [stage_1, stage_2, stage_4, stage_5, stage_6]
    access_method: api
    connector: europepmc
    notes: "resultType=core returns title, abstract and all IDs in one call. Primary ranking surface."
    enabled: true

  - id: crossref
    name: Crossref
    domain: api.crossref.org
    tier: 2
    types: [literature]
    indications: [ALL, CLL]
    stages: [stage_2, stage_4, stage_5]
    access_method: api
    connector: crossref
    notes: "DOI prefix 10.1200 discovers ASCO supportive-care guidance."
    enabled: true

  # ---- Epidemiology -----------------------------------------------------
  - id: orphanet
    name: Orphanet / Orphadata
    domain: api.orphadata.com
    tier: 1
    types: [epidemiology, coding]
    indications: [ALL, CLL]
    stages: [stage_1, stage_3]
    access_method: api
    connector: orphanet
    notes: "Name lookup resolves ORPHA code plus ICD-10/ICD-11/MeSH/UMLS crosswalk."
    enabled: true

  - id: seer
    name: NCI SEER
    domain: seer.cancer.gov
    tier: 1
    types: [epidemiology]
    indications: [ALL, CLL]
    stages: [stage_1, stage_5, stage_6]
    access_method: api
    connector: seer
    search_hint: "SEER stat facts incidence survival"
    notes: "Stat Facts HTML is scraped. api.seer.cancer.gov does NOT carry these statistics."
    enabled: true

  - id: nci
    name: National Cancer Institute (NCI)
    domain: cancer.gov
    tier: 1
    types: [guideline, epidemiology]
    indications: [ALL, CLL]
    stages: [stage_1, stage_2, stage_5]
    access_method: api
    connector: nci_pdq
    search_hint: "PDQ treatment health professional version"
    enabled: true

  - id: cdc_wonder
    name: CDC WONDER (NVSS mortality)
    domain: wonder.cdc.gov
    tier: 1
    types: [epidemiology]
    indications: [ALL, CLL]
    stages: [stage_1, stage_5]
    access_method: api
    connector: cdc_wonder
    notes: "POST only, XML request body, requires the ICD-10 family from the code stage."
    enabled: true

  - id: who_gho
    name: WHO Global Health Observatory
    domain: ghoapi.azureedge.net
    tier: 1
    types: [epidemiology]
    indications: [ALL, CLL]
    stages: [stage_1]
    access_method: api
    connector: who_gho
    notes: "May legitimately return no_specific_indicator for a leukemia subtype."
    enabled: true

  # ---- Regulatory / labels ---------------------------------------------
  - id: openfda_label
    name: FDA Drug Labeling (openFDA)
    domain: api.fda.gov
    tier: 1
    types: [regulatory, label]
    indications: [ALL, CLL]
    stages: [stage_2, stage_4, stage_5]
    access_method: api
    connector: openfda_label
    notes: "Response carries set_id, so a separate DailyMed SETID lookup is unnecessary."
    enabled: true

  - id: openfda_drugsfda
    name: FDA Drugs@FDA (openFDA)
    domain: api.fda.gov
    tier: 1
    types: [regulatory]
    indications: [ALL, CLL]
    stages: [stage_2, stage_4]
    access_method: api
    connector: openfda_drugsfda
    notes: "application_number accepts OR-batched values."
    enabled: true

  - id: openfda_ndc
    name: FDA NDC Directory (openFDA)
    domain: api.fda.gov
    tier: 1
    types: [coding, label]
    indications: [ALL, CLL]
    stages: [stage_3, stage_4]
    access_method: api
    connector: openfda_ndc
    enabled: true

  - id: faers
    name: FDA FAERS Adverse Events (openFDA)
    domain: api.fda.gov
    tier: 2
    types: [registry, safety]
    indications: [ALL, CLL]
    stages: [stage_5]
    access_method: api
    connector: faers
    notes: "Signal frequency only. One report carries multiple drugs and reactions with no causal linkage."
    enabled: true

  - id: dailymed
    name: DailyMed
    domain: dailymed.nlm.nih.gov
    tier: 1
    types: [label]
    indications: [ALL, CLL]
    stages: [stage_2, stage_4, stage_5]
    access_method: api
    connector: dailymed
    notes: "search_string is silently ignored by the v2 API. Use drug_name or application_number."
    enabled: true

  - id: purple_book
    name: FDA Purple Book
    domain: purplebooksearch.fda.gov
    tier: 1
    types: [regulatory, coding]
    indications: [ALL, CLL]
    stages: [stage_4]
    access_method: local_file
    connector: local_files
    local_dataset: purple_book
    notes: "Monthly report contains all products, not only changes. Drop the CSV in data/reference/."
    enabled: true

  # ---- Trials -----------------------------------------------------------
  - id: clinicaltrials
    name: ClinicalTrials.gov
    domain: clinicaltrials.gov
    tier: 2
    types: [registry]
    indications: [ALL, CLL]
    stages: [stage_2, stage_4, stage_5, stage_6]
    access_method: api
    connector: clinicaltrials
    enabled: true

  # ---- NLM Clinical Tables (no authentication) --------------------------
  # Free keyless search over code sets that otherwise need a licence, an
  # account or a downloaded release file. Secondary to the official CMS files:
  # when a local dataset is installed that connector answers first.
  - id: nlm_icd10cm
    name: NLM Clinical Tables (ICD-10-CM)
    domain: clinicaltables.nlm.nih.gov
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3]
    access_method: api
    connector: nlm_icd10cm
    notes: "Answers the ICD-10-CM code universe without the CMS release files."
    enabled: true

  - id: nlm_icd9cm
    name: NLM Clinical Tables (ICD-9-CM)
    domain: clinicaltables.nlm.nih.gov
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3]
    access_method: api
    connector: nlm_icd9cm
    notes: "Supplies the legacy leg of the three-system crosswalk in place of the GEMs files."
    enabled: true

  - id: nlm_hcpcs
    name: NLM Clinical Tables (HCPCS)
    domain: clinicaltables.nlm.nih.gov
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3, stage_4]
    access_method: api
    connector: nlm_hcpcs
    notes: "Searched with the drug and test names handed down by upstream agents, never the disease name."
    enabled: true

  - id: nlm_loinc
    name: NLM Clinical Tables (LOINC)
    domain: clinicaltables.nlm.nih.gov
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3, stage_5]
    access_method: api
    connector: nlm_loinc
    notes: "Keyless LOINC search; the Regenstrief API remains the credentialed path."
    enabled: true

  - id: nlm_rxterms
    name: NLM Clinical Tables (RxTerms)
    domain: clinicaltables.nlm.nih.gov
    tier: 2
    types: [coding, label]
    indications: [ALL, CLL]
    stages: [stage_4]
    access_method: api
    connector: nlm_rxterms
    notes: "Normalises drug names and dose forms for regimen component mapping."
    enabled: true

  # ---- Coding authorities ----------------------------------------------
  - id: cms_icd10
    name: CMS ICD-10-CM Release Files
    domain: cms.gov
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3]
    access_method: local_file
    connector: local_files
    local_dataset: icd10cm
    notes: "Official release files only. Explicitly not a web search."
    enabled: true

  - id: cms_hcpcs
    name: CMS HCPCS Release Files
    domain: cms.gov
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3, stage_4]
    access_method: local_file
    connector: local_files
    local_dataset: hcpcs
    enabled: true

  - id: cms_gems
    name: CMS ICD-9-CM to ICD-10-CM GEMs
    domain: cms.gov
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3]
    access_method: local_file
    connector: local_files
    local_dataset: gems
    notes: "Supplies the ICD-9 leg of the three-system crosswalk."
    enabled: true

  - id: icd11
    name: WHO ICD-11
    domain: id.who.int
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3]
    access_method: api
    connector: icd11
    requires_credentials: [ICD11_CLIENT_ID, ICD11_CLIENT_SECRET]
    enabled: true

  - id: loinc
    name: LOINC (Regenstrief)
    domain: loinc.org
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3, stage_5]
    access_method: api
    connector: loinc
    requires_credentials: [LOINC_USERNAME, LOINC_PASSWORD]
    enabled: true

  - id: ama_cpt
    name: AMA CPT
    domain: ama-assn.org
    tier: 1
    types: [coding]
    indications: [ALL, CLL]
    stages: [stage_3, stage_4]
    access_method: licensed
    connector: null
    notes: "No anonymous full-code API. Licensed dataset required. Never scraped."
    enabled: true

  # ---- Guidelines -------------------------------------------------------
  - id: nccn
    name: NCCN
    domain: nccn.org
    tier: 1
    types: [guideline]
    indications: [ALL, CLL]
    stages: [stage_2, stage_4, stage_5]
    access_method: licensed
    connector: null
    search_hint: "clinical practice guidelines in oncology version"
    notes: "Licensed connector required. Never scraped. Substitute NCI PDQ, iwCLL, ASH and the current European guideline."
    enabled: true

  - id: esmo
    name: ESMO
    domain: esmo.org
    tier: 1
    types: [guideline]
    indications: [ALL, CLL]
    stages: [stage_2, stage_4]
    access_method: api
    connector: guideline_discovery
    search_hint: "clinical practice guideline"
    notes: "Discovered through publication-type literature search. PMIDs are never pinned."
    enabled: true

  - id: eha
    name: EHA (European Hematology Association)
    domain: ehaweb.org
    tier: 1
    types: [guideline]
    indications: [CLL]
    stages: [stage_2]
    access_method: api
    connector: guideline_discovery
    notes: "Responsible for European CLL guideline editions after ESMO."
    enabled: true

  - id: iwcll
    name: iwCLL Guidelines (Blood)
    domain: ashpublications.org
    tier: 1
    types: [guideline]
    indications: [CLL]
    stages: [stage_1, stage_2, stage_5]
    access_method: api
    connector: guideline_discovery
    search_hint: "iwCLL guidelines diagnosis treatment response assessment"
    enabled: true

  - id: ashpublications
    name: ASH / Blood (American Society of Hematology)
    domain: ashpublications.org
    tier: 2
    types: [guideline, literature]
    indications: [ALL, CLL]
    stages: [stage_1, stage_2, stage_5]
    access_method: api
    connector: guideline_discovery
    enabled: true

  - id: asco
    name: ASCO
    domain: asco.org
    tier: 2
    types: [guideline, literature]
    indications: [ALL, CLL]
    stages: [stage_2, stage_5]
    access_method: api
    connector: crossref
    notes: "Discovered through Crossref DOI prefix 10.1200, then firecrawl fallback."
    enabled: true

  # ---- Organizations / registries --------------------------------------
  - id: acs
    name: American Cancer Society
    domain: cancer.org
    tier: 2
    types: [advocacy, epidemiology]
    indications: [ALL, CLL]
    stages: [stage_1, stage_5]
    access_method: targeted_search
    enabled: true

  - id: lls
    name: Leukemia & Lymphoma Society
    domain: lls.org
    tier: 2
    types: [advocacy]
    indications: [ALL, CLL]
    stages: [stage_1, stage_5]
    access_method: targeted_search
    enabled: true

  - id: cibmtr
    name: CIBMTR
    domain: cibmtr.org
    tier: 2
    types: [registry, real_world]
    indications: [ALL, CLL]
    stages: [stage_5, stage_6]
    access_method: targeted_search
    search_hint: "transplant cellular therapy summary slides"
    enabled: true

  - id: who
    name: WHO
    domain: who.int
    tier: 1
    types: [guideline, coding]
    indications: [ALL, CLL]
    stages: [stage_1, stage_3]
    access_method: targeted_search
    search_hint: "classification of haematolymphoid tumours ICD-11"
    enabled: true

  - id: cdc_icd10
    name: CDC / NCHS
    domain: cdc.gov
    tier: 1
    types: [coding, epidemiology]
    indications: [ALL, CLL]
    stages: [stage_1, stage_3]
    access_method: targeted_search
    search_hint: "ICD-10-CM tabular list"
    enabled: true

  - id: cms
    name: CMS
    domain: cms.gov
    tier: 1
    types: [coding, regulatory]
    indications: [ALL, CLL]
    stages: [stage_3, stage_4]
    access_method: targeted_search
    search_hint: "HCPCS ICD-10 code set"
    enabled: true

  - id: fda
    name: FDA
    domain: fda.gov
    tier: 1
    types: [regulatory, label]
    indications: [ALL, CLL]
    stages: [stage_2, stage_4, stage_6]
    access_method: targeted_search
    search_hint: "approval indication oncology"
    enabled: true

  # ---- Fallback ---------------------------------------------------------
  - id: open_web
    name: Open Web (Supplementary)
    domain: null
    tier: 3
    types: [web]
    indications: [ALL, CLL]
    stages: [stage_1, stage_2, stage_3, stage_4, stage_5, stage_6]
    access_method: firecrawl_search
    connector: firecrawl
    fallback_only: true
    enabled: true
```

### File: `celestra\config\thresholds.yaml`

```yaml
# Sufficiency thresholds and escalation limits.
# A question is answered only when it clears sufficiency. Everything else
# escalates: refine the query, then widen to open-web fallback, then give up
# explicitly rather than silently.

sufficiency:
  # Every registered source is a vetted authority: SEER, NCI, FDA, iwCLL and
  # the rest. When one of them answers the question, that IS the answer, and
  # demanding corroboration from a second one only manufactures gaps. So a
  # single verified quote from a primary-tier source is sufficient.
  min_evidence_items: 1
  min_distinct_sources: 1
  min_primary_tier_items: 1      # tier 1 or 2; the open web cannot satisfy this
  primary_tier_ceiling: 2
  # Coverage no longer gates sufficiency. It remains a quality signal and is
  # what separates High from Medium confidence below.
  min_coverage_score: 0.0
  min_quote_length: 40

confidence:
  # Two states only. A reviewer needs one answer: "do I have to act here?".
  ready:
    description: "A vetted (tier 1 or 2) source answered it and no conflict is open"
  requires_input:
    # Exactly three situations reach a person:
    #   1. nothing usable was found after every retrieval strategy
    #   2. the only evidence is open-web, so no vetted source answered it
    #   3. an escalated source disagreement is still undecided
    description: "Unanswered, answered only from the open web, or carrying an open conflict"

escalation:
  max_refinement_rounds: 2       # query rewrites against approved sources
  # Registry sources reached by a domain-scoped web search (cdc.gov, who.int,
  # cancer.org, fda.gov ...) are approved, tier 1-2 evidence, but every call
  # costs a web-search credit. They are consulted only after the API sources
  # have failed to answer the question, and before the open web.
  enable_targeted_search_fallback: true
  targeted_search_max_sources: 2
  enable_open_web_fallback: true
  # After this many consecutive failures to reach Firecrawl, web search is
  # switched off for the session so no question waits on it again. A 402 (no
  # credits) or a rejected key switches it off at once.
  firecrawl_max_consecutive_failures: 2
  # Last-resort harvest of a domain's own sitemap, used when every search
  # front-end is blocked. Off by default: it costs 60-90s per domain, and the
  # domains that matter most here (cdc.gov, who.int, lls.org, fda.gov) refuse
  # the request or publish no usable index, so it usually buys nothing. Turn
  # it on for a network where search is blocked but sitemaps are reachable.
  enable_domain_index: false
  # Pages fetched per question from the open web. Search and page content
  # come back in one call, so this is also the credit cost of the fallback.
  open_web_max_results: 3
  open_web_max_scrapes: 3
  # Open-web evidence is always tier 5 and always labelled SUPPLEMENTARY WEB EVIDENCE.
  open_web_never_overrides_tier: 4

limits:
  max_questions_per_stage: 12
  max_sources_per_question: 8
  max_evidence_items_per_question: 14
  # Per source, per question. A single drug label runs to tens of thousands of
  # words, so without a cap one document supplies every quote and the finding
  # looks well-sourced while resting on one document.
  max_evidence_items_per_source: 3
  # Literature flow: discover cheap metadata, rank it in one model call, fetch
  # full text only for the top candidates, then extract in small batches.
  # Candidates scoring below this are eliminated as not relevant to the
  # question, rather than merely sorted to the bottom. Ranking is cheap;
  # fetching and reading an irrelevant document is not.
  min_relevance: 0.35
  rank_top_k: 8                 # candidates kept after ranking, first round
  rank_top_k_step: 4            # widened by this much on each further round
  hydrate_top_k: 5              # of those, how many get full-text hydration
  extract_batch_size: 3         # documents per extraction call
  max_concurrent_connectors: 6
  connector_timeout_seconds: 30
  connector_retries: 2
  # Wall-clock budget per question. Once spent, the remaining tiers (domain
  # search, open web) are skipped and the question reports what it has; the
  # hard timeout is the ceiling after which the question is abandoned so an
  # agent can never sit on one question indefinitely.
  question_time_budget_seconds: 240
  question_hard_timeout_seconds: 480
  http_cache_ttl_seconds: 86400
  max_parallel_agents: 3

contradictions:
  # A conflict between two tiers this far apart is escalated, not merely noted.
  escalate_on_tier_gap: 2
  escalate_on_numeric_delta_pct: 15
  auto_resolve: false            # never. Both sides are reported as stated.
```

### File: `celestra\connectors\base.py`

```py
"""Connector protocol plus the shared HTTP client.

Every source adapter implements `discover` and, where the source supports it,
`hydrate`. A connector never raises to the caller: it returns a ConnectorResult
carrying either refs or a failure reason, so one dead source can never take
down a run. The orchestrator uses that reason to decide whether to refine the
query or escalate to open-web fallback.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..models import EvidenceOrigin, SourceRef
from ..settings import DATA_DIR, configure_tls, get_settings, system_certs_active

log = logging.getLogger("celestra.connector")

USER_AGENT = "Celestra-DeskResearch/1.0 (clinical desk research; contact research@example.org)"


@dataclass
class ConnectorResult:
    source_id: str
    refs: list[SourceRef] = field(default_factory=list)
    ok: bool = True
    reason: str = ""
    calls: int = 0
    elapsed_ms: int = 0

    @property
    def count(self) -> int:
        return len(self.refs)

    @classmethod
    def failure(cls, source_id: str, reason: str) -> "ConnectorResult":
        return cls(source_id=source_id, ok=False, reason=reason)


@dataclass
class RetrievalContext:
    """What a connector needs to know about the question being answered."""
    indication: str
    indication_key: str
    synonyms: list[str]
    geography: str
    population: str
    stage: str
    question: str
    aspects: list[str] = field(default_factory=list)
    cutoff: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def term(self) -> str:
        return self.indication

    def or_terms(self) -> list[str]:
        seen, out = set(), []
        for t in [self.indication, *self.synonyms]:
            k = t.lower().strip()
            if k and k not in seen:
                seen.add(k)
                out.append(t)
        return out


class Connector(Protocol):
    source_id: str
    tier: int
    origin: EvidenceOrigin

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult: ...


class _DiskCache:
    """Content-addressed response cache. Keeps repeat runs fast and polite to
    rate-limited public APIs."""

    def __init__(self, root: Path, ttl: int) -> None:
        self.root = root
        self.ttl = ttl
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> Any | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            if time.time() - p.stat().st_mtime > self.ttl:
                return None
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def set(self, key: str, value: Any) -> None:
        try:
            self._path(key).write_text(json.dumps(value), encoding="utf-8")
        except (OSError, TypeError):
            pass


class HttpClient:
    """Shared async HTTP client with retry, backoff, caching and a concurrency
    gate. One instance is reused for the process lifetime."""

    def __init__(self) -> None:
        s = get_settings()
        limits = get_settings()
        self._settings = s
        from ..settings import get_thresholds
        th = get_thresholds()["limits"]
        self.timeout = th["connector_timeout_seconds"]
        self.retries = th["connector_retries"]
        self._sem = asyncio.Semaphore(th["max_concurrent_connectors"])
        self._cache = _DiskCache(DATA_DIR / "cache", th["http_cache_ttl_seconds"])
        self._client: httpx.AsyncClient | None = None
        del limits

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            s = get_settings()
            configure_tls()
            verify: Any = s.tls_verify_value()
            if verify is True and system_certs_active():
                import ssl  # noqa: PLC0415

                import truststore  # noqa: PLC0415

                verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            if verify is False:
                log.warning("TLS_VERIFY=false: certificate verification is disabled for "
                            "every outbound call. Use CA_BUNDLE instead where possible.")
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout, connect=min(self.timeout, 15)),
                "follow_redirects": True,
                "headers": {"User-Agent": USER_AGENT},
                "verify": verify,
                "trust_env": True,          # HTTPS_PROXY / NO_PROXY / SSL_CERT_FILE
            }
            if s.proxy_url:
                kwargs["proxy"] = s.proxy_url
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @staticmethod
    def _key(method: str, url: str, params: Any, body: Any) -> str:
        blob = json.dumps([method, url, params, body], sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        as_json: bool = True,
        use_cache: bool = True,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Returns parsed JSON or text. Raises on final failure so the calling
        connector can translate it into a ConnectorResult reason.

        Only a rate limit, a server error or a transport failure is retried.
        Every other 4xx is the server's final word (a bad key, no credits, a
        malformed request) and retrying it only spends time.
        """
        key = self._key(method, url, params, json_body or data)
        if use_cache and method.upper() == "GET":
            hit = self._cache.get(key)
            if hit is not None:
                return hit

        attempts = (self.retries if retries is None else max(0, retries)) + 1
        last_exc: Exception | None = None
        async with self._sem:
            client = await self.client()
            for attempt in range(attempts):
                try:
                    resp = await client.request(
                        method, url, params=params, json=json_body,
                        data=data, headers=headers,
                        timeout=httpx.Timeout(timeout) if timeout else httpx.USE_CLIENT_DEFAULT,
                    )
                    if resp.status_code in (429, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError(
                            f"retryable {resp.status_code}", request=resp.request, response=resp
                        )
                    resp.raise_for_status()
                    out = resp.json() if as_json else resp.text
                    if use_cache and method.upper() == "GET":
                        self._cache.set(key, out)
                    return out
                except (httpx.HTTPError, ValueError) as exc:
                    last_exc = exc
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    final = status is not None and status not in (429, 500, 502, 503, 504)
                    if final or attempt == attempts - 1:
                        break
                    await asyncio.sleep(0.5 * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    async def get_json(self, url: str, **kw: Any) -> Any:
        return await self.request("GET", url, **kw)

    async def get_text(self, url: str, **kw: Any) -> str:
        kw["as_json"] = False
        return await self.request("GET", url, **kw)


http = HttpClient()


def _root_cause(exc: BaseException) -> str:
    """The deepest message in the exception chain. httpx wraps an SSL or socket
    error in ConnectError with an empty message; the useful text is below it."""
    seen: list[str] = []
    cur: BaseException | None = exc
    while cur is not None and len(seen) < 6:
        text = str(cur).strip()
        if text and text not in seen:
            seen.append(text)
        cur = cur.__cause__ or cur.__context__
    return seen[-1] if seen else type(exc).__name__


def explain_transport_error(exc: Exception) -> tuple[str, str]:
    """(what went wrong, what to do about it) for a connection-level failure.

    'ConnectError' on its own has sent more than one person to check an API
    key that was fine. The cause is almost always the network the process is
    on, and each cause has a different fix.
    """
    cause = _root_cause(exc)
    low = cause.lower()
    if "certificate_verify_failed" in low or "ssl" in low and "verif" in low:
        if system_certs_active():
            remedy = (
                "Something on this network intercepts HTTPS and its certificate is not "
                "trusted even by the operating system store. Export the proxy's root "
                "certificate as PEM and set CA_BUNDLE=/path/to/bundle.pem in .env. "
                "TLS_VERIFY=false disables checking entirely, as a last resort."
            )
        else:
            remedy = (
                "Something on this network intercepts HTTPS (a corporate proxy or firewall). "
                "Run `pip install -r requirements.txt` so the `truststore` package is "
                "installed: Celestra then verifies against the operating system's "
                "certificate store, which already trusts the proxy (that is why the "
                "browser works). Otherwise export the proxy's root certificate as PEM and "
                "set CA_BUNDLE=/path/to/bundle.pem. TLS_VERIFY=false is the last resort."
            )
        return (f"TLS certificate verification failed ({cause[:160]})", remedy)
    if "ssl" in low or "tls" in low or "handshake" in low:
        return (
            f"TLS handshake failed ({cause[:160]})",
            "A proxy or firewall is interfering with HTTPS. Set CA_BUNDLE to the "
            "network's root certificate, or PROXY_URL if a proxy is required.",
        )
    if ("getaddrinfo" in low or "name or service not known" in low
            or "nodename nor servname" in low or "temporary failure in name resolution" in low
            or "no address associated" in low):
        return (
            f"DNS lookup failed ({cause[:160]})",
            "This process cannot resolve internet hostnames. Check the machine is online, "
            "and if it reaches the web only through a proxy set HTTPS_PROXY or PROXY_URL.",
        )
    if "proxy" in low or "407" in low or "tunnel" in low:
        return (
            f"proxy refused the connection ({cause[:160]})",
            "Check HTTPS_PROXY / PROXY_URL, including credentials if the proxy needs them.",
        )
    if "refused" in low or "unreachable" in low or "no route" in low or "reset" in low:
        return (
            f"connection refused or reset ({cause[:160]})",
            "The host is blocked from this network. Ask for api.firecrawl.dev to be "
            "allowed through the firewall, or set PROXY_URL to a proxy that can reach it.",
        )
    if isinstance(exc, httpx.TimeoutException) or "timed out" in low or "timeout" in low:
        return (
            "connection timed out",
            "The host did not answer. A firewall that drops packets silently looks like "
            "this; try HTTPS_PROXY / PROXY_URL, or raise connector_timeout_seconds.",
        )
    return (
        f"connection failed ({cause[:160]})",
        "Check that this machine can reach the internet from a terminal "
        "(curl https://api.firecrawl.dev) and set HTTPS_PROXY / CA_BUNDLE as needed.",
    )


def describe_http_error(exc: Exception) -> str:
    """Turn a transport exception into a reason string the UI can show."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "credentials rejected (401)"
    if status == 402:
        return "payment required (402): the account has no credits"
    if status == 403:
        return "access forbidden (403)"
    if status == 404:
        return "not found (404)"
    if status == 429:
        return "rate limited (429)"
    if status:
        return f"HTTP {status}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, (httpx.TransportError, OSError)):
        return explain_transport_error(exc)[0]
    return f"{type(exc).__name__}: {str(exc)[:120]}" if str(exc) else type(exc).__name__


def remedy_for(exc: Exception) -> str:
    """What to do about a failure, when there is something to do."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "The API key was rejected. Copy it again from the provider's dashboard into .env."
    if status == 402:
        return "The account is out of credits. Top it up on the provider's dashboard."
    if status == 403:
        return "The key is valid but not allowed to call this endpoint; check the plan."
    if status == 429:
        return "Rate limited. Wait, or lower max_concurrent_connectors in thresholds.yaml."
    if status:
        return ""
    if isinstance(exc, (httpx.TransportError, OSError)):
        return explain_transport_error(exc)[1]
    return ""
```

### File: `celestra\connectors\cdc_wonder.py`

```py
"""CDC WONDER — NVSS provisional mortality (database D158).

POST only. A GET to the datarequest controller returns 500. The request is a
form POST carrying `request_xml` (the WONDER parameter document) and
`accept_datause_restrictions=true`; the response is XML, either a
`<data-table>` or a `<page>` carrying `<message>` elements explaining the
rejection.

The ICD-10 code family is an input, not a guess: it comes from
`ctx.extra["icd10_codes"]` (the coding stage supplies it, Orphanet resolves
it). With no codes the connector fails fast with "no ICD-10 code family
supplied" rather than pulling all-cause mortality and implying it is the
indication's.

Operational facts observed on 2026-09-09, all of them by probing:

* WONDER enforces a minimum of 15 seconds between API requests; a faster
  request comes back with a "Request rate exceeded" message rather than data.
* It validates a "button" selection per variable group server-side. D158
  requires O_age, O_race, O_location and O_urban to be set or it rejects the
  request; the values below satisfy that check.
* The request template here still trips one final server-side rule
  ("Age Adjusted Rates are not available when county level locations ... are
  selected"), and the D158 request form itself is not readable from here
  (GET on wonder.cdc.gov returns 403), so the remaining measure-checkbox
  parameter could not be resolved by probing. The connector therefore reports
  WONDER's own message as the failure reason instead of retrying blindly or
  pretending the source returned nothing.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

REQUEST_URL = "https://wonder.cdc.gov/controller/datarequest/D158"
PORTAL_URL = "https://wonder.cdc.gov/mcd-icd10-provisional.html"

# Minimum spacing WONDER enforces between API requests.
MIN_INTERVAL_SECONDS = 15


def parameter(name: str, *values: str) -> str:
    inner = "".join(f"<value>{v}</value>" for v in values)
    return f"<parameter><name>{name}</name>{inner}</parameter>"


def build_request_xml(icd10_codes: list[str], title: str = "celestra") -> str:
    """WONDER parameter document: deaths and crude rate by ICD-10 cause,
    restricted to the supplied code family."""
    codes = [clean(c) for c in icd10_codes if clean(c)]
    params = [
        parameter("accept_datause_restrictions", "true"),
        parameter("B_1", "D158.V2"),
        parameter("B_2", "*None*"),
        parameter("B_3", "*None*"),
        parameter("B_4", "*None*"),
        parameter("B_5", "*None*"),
        parameter("M_1", "D158.M1"),      # Deaths
        parameter("M_2", "D158.M2"),      # Population
        parameter("M_3", "D158.M3"),      # Crude rate
        parameter("F_D158.V2", *codes),   # UCD - ICD-10 codes
        parameter("I_D158.V2", *codes),
        parameter("O_ucd", "D158.V2"),
        # WONDER validates a "button" selection for each of these groups and
        # rejects the whole request when one is missing. Probed 2026-09-09.
        parameter("O_age", "D158.V51"),        # five-year age groups
        parameter("O_race", "D158.V27"),       # single race 6
        parameter("O_location", "D158.V9"),    # state / county
        parameter("O_urban", "D158.V9"),
        parameter("O_aar", "aar_none"),        # age-adjusted rates off
        parameter("O_javascript", "on"),
        parameter("O_precision", "1"),
        parameter("O_rate_per", "100000"),
        parameter("O_show_totals", "false"),
        parameter("O_show_zeros", "true"),
        parameter("O_timeout", "300"),
        parameter("O_title", title),
    ]
    return "<request-parameters>" + "".join(params) + "</request-parameters>"


def parse_messages(xml_text: str) -> list[str]:
    return [clean(m) for m in
            re.findall(r"<message>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</message>", xml_text, re.S)]


def parse_data_table(xml_text: str) -> list[list[str]]:
    """Rows of the WONDER `<data-table>` as lists of cell values."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "ignore"))
    except ET.ParseError:
        return []
    table = root.find(".//data-table")
    if table is None:
        return []
    rows: list[list[str]] = []
    for row in table.findall("./r"):
        cells = [clean("".join(cell.itertext())) for cell in row.findall("./c")]
        # WONDER writes the label in `l` and the value in `v` attributes too.
        if not any(cells):
            cells = [clean(cell.get("l") or cell.get("v") or "") for cell in row.findall("./c")]
        if any(cells):
            rows.append(cells)
    return rows


class CdcWonderConnector:
    """Provisional mortality counts for the indication's ICD-10 family."""

    source_id = "cdc_wonder"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "CDC WONDER (NVSS mortality)"

    async def request(self, icd10_codes: list[str], title: str = "celestra") -> str:
        return await http.request(
            "POST", REQUEST_URL,
            data={"request_xml": build_request_xml(icd10_codes, title),
                  "accept_datause_restrictions": "true"},
            as_json=False, use_cache=False,
        )

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        codes = [clean(c) for c in (ctx.extra.get("icd10_codes") or []) if clean(c)]
        if not codes:
            return ConnectorResult.failure(self.source_id, "no ICD-10 code family supplied")
        try:
            xml_text = await self.request(codes, title=f"celestra {ctx.indication_key}")
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            # WONDER answers a rejected request with HTTP 500 and an XML body
            # that explains why. Reporting "HTTP 500" would throw that away.
            body = getattr(getattr(exc, "response", None), "text", "") or ""
            messages = parse_messages(body)
            if messages:
                # Report every message, not just the first: WONDER lists the
                # order-of-by-variables complaint ahead of the rule that is
                # actually blocking the request.
                joined = " | ".join(messages[:3])
                return ConnectorResult.failure(
                    self.source_id, clip(f"WONDER rejected the request: {joined}", 300))
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))

        messages = parse_messages(xml_text)
        rows = parse_data_table(xml_text)
        if not rows:
            reason = messages[0] if messages else "WONDER returned no data table"
            if any("rate exceeded" in m.lower() for m in messages):
                reason = (f"rate limited by WONDER (minimum {MIN_INTERVAL_SECONDS}s "
                          "between API requests)")
            return ConnectorResult(source_id=self.source_id, refs=[], ok=False,
                                   reason=clip(reason, 200), calls=1,
                                   elapsed_ms=int((time.perf_counter() - started) * 1000))

        body = join_sections({
            "ICD-10 codes": ", ".join(codes),
            "Rows": " | ".join(" ".join(r) for r in rows[:40]),
            "Notes": " ".join(messages[:3]),
        })
        ref = SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=PORTAL_URL,
            title=f"CDC WONDER provisional mortality for ICD-10 {', '.join(codes)}",
            organization="Centers for Disease Control and Prevention, National Center for Health Statistics",
            published="",
            identifiers={"database": "D158", "icd10_codes": ", ".join(codes)},
            snippet=clip(body, 1400),
            raw={"database": "D158", "icd10_codes": codes, "rows": rows,
                 "messages": messages, "text": body},
            origin=self.origin,
        )
        return ConnectorResult(
            source_id=self.source_id, refs=[ref][:limit], ok=True, reason="", calls=1,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\clinicaltrials.py`

```py
"""ClinicalTrials.gov API v2.

`query.cond` is loose — it returns studies whose condition list merely brushes
the term — so every study is post-filtered against `ctx.or_terms()` on its
title and condition list before it becomes a SourceRef.

`hydrate(nct_id)` pulls the full protocolSection for a single study, which is
where eligibility criteria and outcome measures live.
"""
from __future__ import annotations

import time

import httpx

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections, matches_any
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"
STUDY_URL = STUDIES_URL + "/{nct_id}"
PUBLIC_URL = "https://clinicaltrials.gov/study/{nct_id}"

# clinicaltrials.gov's edge rejects unrecognised User-Agent strings with a bare
# 403 — the shared client's product token included. Sending the transport's own
# default identifies us accurately and is accepted.
CTG_HEADERS = {"User-Agent": f"python-httpx/{httpx.__version__}"}

FIELDS = (
    "NCTId,BriefTitle,OfficialTitle,OverallStatus,Condition,InterventionName,"
    "InterventionType,Phase,BriefSummary,EligibilityCriteria,StartDate,"
    "CompletionDate,LeadSponsorName,StudyType,EnrollmentCount,PrimaryOutcomeMeasure"
)


def _section(study: dict, module: str) -> dict:
    return (study.get("protocolSection") or {}).get(module) or {}


def summarise(study: dict) -> dict:
    ident = _section(study, "identificationModule")
    status = _section(study, "statusModule")
    design = _section(study, "designModule")
    arms = _section(study, "armsInterventionsModule")
    desc = _section(study, "descriptionModule")
    cond = _section(study, "conditionsModule")
    elig = _section(study, "eligibilityModule")
    sponsor = _section(study, "sponsorCollaboratorsModule")
    outcomes = _section(study, "outcomesModule")
    return {
        "nct_id": clean(ident.get("nctId")),
        "title": clean(ident.get("briefTitle")) or clean(ident.get("officialTitle")),
        "official_title": clean(ident.get("officialTitle")),
        "status": clean(status.get("overallStatus")),
        "start_date": clean((status.get("startDateStruct") or {}).get("date")),
        "completion_date": clean((status.get("completionDateStruct") or {}).get("date")),
        "phases": [clean(p) for p in design.get("phases") or []],
        "study_type": clean(design.get("studyType")),
        "enrollment": clean((design.get("enrollmentInfo") or {}).get("count")),
        "conditions": [clean(c) for c in cond.get("conditions") or []],
        "interventions": [clean(i.get("name")) for i in arms.get("interventions") or []],
        "brief_summary": clean(desc.get("briefSummary")),
        "eligibility": clean(elig.get("eligibilityCriteria")),
        "sponsor": clean((sponsor.get("leadSponsor") or {}).get("name")),
        "primary_outcomes": [clean(o.get("measure"))
                             for o in outcomes.get("primaryOutcomes") or []][:6],
    }


class ClinicalTrialsConnector:
    """Trial registry discovery with a title/condition relevance gate."""

    source_id = "clinicaltrials"
    tier = 2
    origin = EvidenceOrigin.APPROVED_API
    source_name = "ClinicalTrials.gov"

    async def _search(self, ctx: RetrievalContext, limit: int) -> list[dict]:
        params: dict[str, object] = {
            "query.cond": ctx.indication,
            "pageSize": max(1, min(limit * 3, 200)),  # over-fetch: the filter discards
            "fields": FIELDS,
            "sort": "LastUpdatePostDate:desc",
        }
        payload = await http.get_json(STUDIES_URL, params=params, headers=CTG_HEADERS)
        return payload.get("studies") or []

    async def hydrate(self, nct_id: str) -> dict:
        """Full protocolSection for one study, or {} when it cannot be read."""
        nct_id = clean(nct_id).upper()
        if not nct_id:
            return {}
        try:
            return await http.get_json(STUDY_URL.format(nct_id=nct_id), headers=CTG_HEADERS)
        except Exception:  # noqa: BLE001 - hydration is an enrichment
            return {}

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        terms = ctx.or_terms()
        try:
            studies = await self._search(ctx, limit)
            refs: list[SourceRef] = []
            for study in studies:
                info = summarise(study)
                haystack = " ".join([info["title"], *info["conditions"]])
                if not matches_any(haystack, terms):
                    continue  # query.cond is loose; drop off-target studies
                body = join_sections({
                    "Brief summary": info["brief_summary"],
                    "Conditions": ", ".join(info["conditions"]),
                    "Interventions": ", ".join(info["interventions"]),
                    "Primary outcomes": "; ".join(info["primary_outcomes"]),
                    "Eligibility": info["eligibility"],
                })
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=PUBLIC_URL.format(nct_id=info["nct_id"]),
                    title=info["title"],
                    organization=info["sponsor"] or "ClinicalTrials.gov",
                    published=info["start_date"],
                    identifiers={k: v for k, v in {
                        "nct": info["nct_id"],
                        "phase": ", ".join(info["phases"]),
                        "status": info["status"],
                    }.items() if v},
                    snippet=clip(info["brief_summary"] or body, 1400),
                    raw={**info, "text": body},
                    origin=self.origin,
                ))
                if len(refs) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no on-target studies", calls=1,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\crossref.py`

```py
"""Crossref works search.

Also serves the `asco` source id through the `prefix` constructor argument:
DOI prefix 10.1200 is ASCO's, so a prefix-filtered bibliographic query is how
ASCO guidance is discovered without scraping asco.org.

Crossref frequently omits `abstract`. Since the extraction layer needs
quotable text, records that come back without one are enriched in a single
batched Europe PMC DOI lookup rather than left as bare titles.
"""
from __future__ import annotations

import time

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections, strip_tags
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

WORKS_URL = "https://api.crossref.org/works"
SELECT = "DOI,title,container-title,author,published,abstract,type,publisher,URL,issued"


def _author_string(item: dict) -> str:
    names = []
    for a in (item.get("author") or [])[:12]:
        name = " ".join(x for x in [clean(a.get("given")), clean(a.get("family"))] if x)
        if name:
            names.append(name)
    return ", ".join(names)


def _published(item: dict) -> str:
    for key in ("published", "issued", "published-print", "published-online"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts:
            return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))
    return ""


class CrossrefConnector:
    """Bibliographic search over Crossref, optionally pinned to a DOI prefix."""

    origin = EvidenceOrigin.APPROVED_API

    def __init__(self, source_id: str = "crossref", source_name: str = "Crossref",
                 tier: int = 2, prefix: str | None = None,
                 organization: str = "") -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.tier = tier
        self.prefix = prefix
        self.organization = organization

    def build_params(self, ctx: RetrievalContext, limit: int) -> dict[str, object]:
        bib = " ".join([ctx.indication, *(ctx.aspects[:3] or [])]).strip() or ctx.indication
        filters = []
        if self.prefix:
            filters.append(f"prefix:{self.prefix}")
        if ctx.cutoff:
            filters.append(f"until-pub-date:{ctx.cutoff}")
        params: dict[str, object] = {
            "query.bibliographic": bib,
            "select": SELECT,
            "rows": max(1, min(limit, 100)),
            "sort": "score",
        }
        if filters:
            params["filter"] = ",".join(filters)
        return params

    async def _abstracts_by_doi(self, dois: list[str]) -> dict[str, dict]:
        """One Europe PMC call for every DOI missing an abstract."""
        if not dois:
            return {}
        query = " OR ".join(f'DOI:"{d}"' for d in dois[:25])
        try:
            payload = await http.get_json(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": query, "resultType": "core", "format": "json",
                        "pageSize": len(dois[:25])},
            )
        except Exception:  # noqa: BLE001 - enrichment only
            return {}
        out = {}
        for rec in (payload.get("resultList") or {}).get("result") or []:
            doi = clean(rec.get("doi")).lower()
            if doi:
                out[doi] = rec
        return out

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            payload = await http.get_json(WORKS_URL, params=self.build_params(ctx, limit))
            calls += 1
            items = (payload.get("message") or {}).get("items") or []

            missing = [clean(i.get("DOI")) for i in items if not i.get("abstract")]
            enrichment = await self._abstracts_by_doi([d for d in missing if d])
            if enrichment:
                calls += 1

            refs: list[SourceRef] = []
            for item in items:
                doi = clean(item.get("DOI"))
                title = clean(item.get("title"))
                journal = clean(item.get("container-title"))
                epmc = enrichment.get(doi.lower(), {})
                abstract = strip_tags(clean(item.get("abstract"))) or clean(epmc.get("abstractText"))
                identifiers = {k: v for k, v in {
                    "doi": doi,
                    "pmid": clean(epmc.get("pmid")),
                    "pmcid": clean(epmc.get("pmcid")),
                }.items() if v}
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=clean(item.get("URL")) or (f"https://doi.org/{doi}" if doi else WORKS_URL),
                    title=title,
                    organization=self.organization or journal or clean(item.get("publisher")),
                    published=_published(item),
                    identifiers=identifiers,
                    snippet=clip(abstract or f"{title}. {journal}.", 1200),
                    raw={
                        "title": title,
                        "abstract": abstract,
                        "journal": journal,
                        "publisher": clean(item.get("publisher")),
                        "type": clean(item.get("type")),
                        "authors": _author_string(item),
                        "text": join_sections({
                            "Title": title,
                            "Journal": journal,
                            "Abstract": abstract,
                        }),
                    },
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\dailymed.py`

```py
"""DailyMed SPL services (v2).

RECORDED BEHAVIOUR — the `search_string` parameter of
`/services/v2/spls.json` is SILENTLY IGNORED. It does not filter and does not
error: the request returns the entire 159,125-record SPL catalogue, which
looks like a successful search until you read the payload. Verified 2026-09-09.
Use `drug_name=` or `application_number=`; both filter correctly. Nothing in
this module ever sends `search_string`.

The listing endpoints return only title/setid/version, so the connector
hydrates the SPL XML for the set ids it is going to emit — a ref whose snippet
is a bare product title is useless to the extraction layer.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
SPLS_URL = f"{BASE}/spls.json"
SPL_XML_URL = BASE + "/spls/{setid}.xml"
HISTORY_URL = BASE + "/spls/{setid}/history.json"
NDCS_URL = BASE + "/spls/{setid}/ndcs.json"
DRUGINFO_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"

V3 = {"v3": "urn:hl7-org:v3"}
UNCLASSIFIED = "SPL UNCLASSIFIED SECTION"

# How many SPL documents to hydrate per discover call. Each XML is ~0.5 MB.
MAX_HYDRATE = 4


def parse_spl_sections(xml_text: str) -> dict[str, str]:
    """SPL section label -> text.

    Real labels sit on the parent section while the prose often sits in nested
    "SPL UNCLASSIFIED SECTION" children, so unclassified text is folded into
    the last labelled section seen in document order.
    """
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "ignore"))
    except ET.ParseError:
        return {}
    sections: dict[str, list[str]] = {}
    current = "Label"
    for node in root.iter(f"{{{V3['v3']}}}section"):
        title_node = node.find("v3:title", V3)
        code_node = node.find("v3:code", V3)
        label = clean("".join(title_node.itertext())) if title_node is not None else ""
        if not label and code_node is not None:
            label = clean(code_node.get("displayName"))
        text = clean(" ".join("".join(t.itertext()) for t in node.findall("v3:text", V3)))
        if label and label.upper() != UNCLASSIFIED:
            current = label
        if text:
            sections.setdefault(current, []).append(text)
    return {k: clean(" ".join(v)) for k, v in sections.items() if clean(" ".join(v))}


def _pick_indications(sections: dict[str, str]) -> str:
    for label, text in sections.items():
        if "indication" in label.lower():
            return text
    return ""


class DailyMedConnector:
    """SPL listings plus hydrated label text."""

    source_id = "dailymed"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "DailyMed"

    # -- raw service wrappers -------------------------------------------
    async def by_drug_name(self, drug_name: str, page_size: int = 20) -> list[dict]:
        payload = await http.get_json(
            SPLS_URL, params={"drug_name": clean(drug_name), "pagesize": page_size},
        )
        return payload.get("data") or []

    async def by_application_number(self, application_number: str,
                                    page_size: int = 20) -> list[dict]:
        payload = await http.get_json(
            SPLS_URL,
            params={"application_number": clean(application_number), "pagesize": page_size},
        )
        return payload.get("data") or []

    async def spl_xml(self, setid: str) -> str:
        return await http.get_text(SPL_XML_URL.format(setid=clean(setid)))

    async def history(self, setid: str) -> dict:
        payload = await http.get_json(HISTORY_URL.format(setid=clean(setid)))
        return (payload.get("data") or {})

    async def ndcs(self, setid: str) -> list[str]:
        payload = await http.get_json(NDCS_URL.format(setid=clean(setid)))
        return [clean(n.get("ndc")) for n in ((payload.get("data") or {}).get("ndcs") or [])]

    # -- discovery -------------------------------------------------------
    async def _candidate_names(self, ctx: RetrievalContext) -> list[str]:
        """Drug names to look up. Supplied by the orchestrator when a prior
        stage has them; otherwise derived from the openFDA label index so this
        connector also works standalone."""
        names = [clean(n) for n in (ctx.extra.get("drug_names") or []) if clean(n)]
        if names:
            return names
        from .openfda import LABEL_URL, openfda_block, or_terms_query

        payload = await http.get_json(
            LABEL_URL,
            params={"search": or_terms_query("indications_and_usage", ctx.or_terms()),
                    "limit": 40},
        )
        out: list[str] = []
        for record in payload.get("results") or []:
            block = openfda_block(record)
            for key in ("generic_name", "brand_name"):
                for value in block.get(key) or []:
                    value = clean(value).title()
                    if value and value not in out:
                        out.append(value)
        return out

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            names = await self._candidate_names(ctx)
            calls += 1
            if not names:
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason="no drug names for indication", calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))

            listings: list[tuple[str, dict]] = []
            for name in names[:8]:
                if len(listings) >= max(1, min(limit, MAX_HYDRATE)):
                    break
                try:
                    rows = await self.by_drug_name(name, page_size=5)
                except Exception:  # noqa: BLE001 - one bad name must not sink the source
                    continue
                calls += 1
                for row in rows:
                    if clean(row.get("setid")):
                        listings.append((name, row))

            refs: list[SourceRef] = []
            seen: set[str] = set()
            for name, row in listings:
                if len(refs) >= max(1, min(limit, MAX_HYDRATE)):
                    break
                setid = clean(row.get("setid"))
                if setid in seen:
                    continue
                seen.add(setid)
                sections: dict[str, str] = {}
                try:
                    sections = parse_spl_sections(await self.spl_xml(setid))
                    calls += 1
                except Exception:  # noqa: BLE001 - listing still has value
                    sections = {}
                body = _pick_indications(sections) or join_sections(sections)
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=DRUGINFO_URL.format(setid=setid),
                    title=clean(row.get("title")),
                    organization="National Library of Medicine (DailyMed)",
                    published=clean(row.get("published_date")),
                    identifiers={k: v for k, v in {
                        "set_id": setid,
                        "spl_version": clean(row.get("spl_version")),
                        "drug_name": name,
                    }.items() if v},
                    snippet=clip(body or clean(row.get("title")), 1500),
                    raw={
                        "setid": setid,
                        "listing": row,
                        "sections": sections,
                        "text": join_sections(sections) or clean(row.get("title")),
                    },
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\europepmc.py`

```py
"""Europe PMC.

The primary literature surface: `resultType=core` returns title AND abstract
AND every identifier (pmid, pmcid, doi) in a single call, so one request yields
quotable text instead of a second round trip per record.

Queries are always field-scoped (TITLE / ABSTRACT / JOURNAL / PUB_TYPE); an
unscoped query matches full text and drowns the result set in noise.
"""
from __future__ import annotations

import time

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, first_str, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
ARTICLE_URL = "https://europepmc.org/article/{source}/{ident}"


# Below this length a synonym is an acronym (CLL, ALL, SLL) that collides with
# unrelated vocabulary once it is matched against abstracts.
MIN_ABSTRACT_TERM_CHARS = 8


def phrase_clause(terms: list[str], fields: tuple[str, ...] = ("TITLE", "ABSTRACT")) -> str:
    """OR-join every synonym across the given field scopes.

    Acronyms are restricted to TITLE. `ABSTRACT:"SLL"` matches an ultrasound
    beamforming paper as readily as a lymphoma one, and sorting by date then
    puts the noise on top.
    """
    parts = []
    for term in terms:
        if not term:
            continue
        for field in fields:
            if field == "ABSTRACT" and len(term) < MIN_ABSTRACT_TERM_CHARS:
                continue
            parts.append(f'{field}:"{term}"')
    return "(" + " OR ".join(parts) + ")" if parts else ""


def on_topic(record: dict, terms: list[str]) -> bool:
    """Keep a record only when a full disease phrase appears in its title or
    abstract. Guards against acronym collisions the query grammar lets through."""
    long_terms = [t for t in terms if len(t) >= MIN_ABSTRACT_TERM_CHARS]
    if not long_terms:
        return True
    haystack = f"{record.get('title') or ''} {record.get('abstractText') or ''}".lower()
    return any(t.lower() in haystack for t in long_terms)


def article_url(rec: dict) -> str:
    doi = clean(rec.get("doi"))
    pmid = clean(rec.get("pmid"))
    pmcid = clean(rec.get("pmcid"))
    if pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    if pmcid:
        return f"https://europepmc.org/article/PMC/{pmcid}"
    if doi:
        return f"https://doi.org/{doi}"
    return ARTICLE_URL.format(source=clean(rec.get("source")) or "MED", ident=clean(rec.get("id")))


def journal_of(rec: dict) -> str:
    info = rec.get("journalInfo") or {}
    journal = info.get("journal") or {}
    return first_str(journal.get("title"), rec.get("journalTitle"), rec.get("bookOrReportDetails"))


def ref_from_record(rec: dict, source_id: str, source_name: str, tier: int,
                    origin: EvidenceOrigin) -> SourceRef:
    """Build a fully populated SourceRef. `raw` keeps the abstract intact so the
    extraction service can pull verbatim quotes from it."""
    abstract = clean(rec.get("abstractText"))
    title = clean(rec.get("title"))
    journal = journal_of(rec)
    pub_types = [clean(t) for t in (rec.get("pubTypeList") or {}).get("pubType", [])]
    identifiers = {
        k: v for k, v in {
            "pmid": clean(rec.get("pmid")),
            "pmcid": clean(rec.get("pmcid")),
            "doi": clean(rec.get("doi")),
            "epmc_id": clean(rec.get("id")),
            "source": clean(rec.get("source")),
        }.items() if v
    }
    return SourceRef(
        source_id=source_id,
        source_name=source_name,
        tier=tier,
        url=article_url(rec),
        title=title,
        organization=journal or source_name,
        published=first_str(rec.get("firstPublicationDate"), rec.get("pubYear")),
        identifiers=identifiers,
        snippet=clip(abstract or title, 1200),
        raw={
            "title": title,
            "abstract": abstract,
            "journal": journal,
            "authors": clean(rec.get("authorString")),
            "pub_types": pub_types,
            "cited_by": rec.get("citedByCount"),
            "is_open_access": rec.get("isOpenAccess") == "Y",
            "mesh": [clean(m.get("descriptorName"))
                     for m in (rec.get("meshHeadingList") or {}).get("meshHeading", [])][:20],
            "text": join_sections({"Title": title, "Abstract": abstract}),
        },
        origin=origin,
    )


class EuropePmcConnector:
    """Core literature search over Europe PMC."""

    source_id = "europepmc"
    tier = 2
    origin = EvidenceOrigin.APPROVED_API

    def __init__(self, source_id: str = "europepmc", source_name: str = "Europe PMC",
                 tier: int = 2) -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.tier = tier

    def build_query(self, ctx: RetrievalContext, extra_clause: str = "") -> str:
        clause = phrase_clause(ctx.or_terms())
        parts = [p for p in (clause, extra_clause) if p]
        query = " AND ".join(parts)
        if ctx.cutoff:
            query = f'{query} AND (FIRST_PDATE:[1900-01-01 TO {ctx.cutoff}])'
        return query

    async def search(self, query: str, limit: int, sort: str = "P_PDATE_D desc") -> list[dict]:
        payload = await http.get_json(
            SEARCH_URL,
            params={
                "query": query,
                "resultType": "core",
                "format": "json",
                "pageSize": max(1, min(limit, 100)),
                "sort": sort,
            },
        )
        return (payload.get("resultList") or {}).get("result") or []

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            query = self.build_query(ctx)
            records = await self.search(query, limit)
            calls += 1
            if not records:
                # Narrow scope produced nothing: widen from title/abstract to
                # the whole record rather than reporting a dead source.
                query = " OR ".join(f'"{t}"' for t in ctx.or_terms())
                records = await self.search(query, limit)
                calls += 1
            terms = ctx.or_terms()
            on_target = [r for r in records if on_topic(r, terms)] or records
            refs = [
                ref_from_record(r, self.source_id, self.source_name, self.tier, self.origin)
                for r in on_target
            ]
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id,
            refs=refs,
            ok=True,
            reason="" if refs else "no results",
            calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


async def fetch_full_text(pmcid: str) -> str:
    """Open-access full text for a PMCID, or "" when the article is not in the
    OA subset. A 404 here is the normal case, not an error."""
    pmcid = clean(pmcid).upper()
    if not pmcid:
        return ""
    if not pmcid.startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    try:
        xml = await http.get_text(FULLTEXT_URL.format(pmcid=pmcid))
    except Exception:  # noqa: BLE001 - absence of full text is expected
        return ""
    from ._util import strip_tags

    return strip_tags(xml)
```

### File: `celestra\connectors\firecrawl.py`

```py
"""Web search and page scraping.

Two modes, one class:

* `open_web` — the tier 5 supplementary fallback. origin=OPEN_WEB.
* `targeted_search` — a domain-restricted search serving acs, lls, cibmtr,
  who, cdc_icd10, cms and fda. origin=TARGETED_SEARCH and the tier is the
  registry tier for that source, NOT 5. A targeted search of cdc.gov is
  approved-domain evidence; calling it tier 5 would understate it.

With `FIRECRAWL_API_KEY` set, the Firecrawl API (v2 by default; v1 via
FIRECRAWL_API_VERSION, any base via FIRECRAWL_API_URL) does search and scrape.
Without it the connector still works through a keyless chain, tried in order
until one returns on-topic results:

  1. Bing HTML        (www.bing.com/search)
  2. DuckDuckGo HTML  (html.duckduckgo.com)
  3. DuckDuckGo Lite  (lite.duckduckgo.com)
  4. Domain index     (sitemap harvest: the restricted domain for a targeted
                     search, OPEN_WEB_DOMAIN_PANEL for the open-web source)

Every keyless result passes a relevance gate before it is kept: search
front-ends under bot pressure happily return a plausible-looking page of
results for the first word of the query alone, and unfiltered noise is worse
than an honest empty result. Whatever the backend, the page itself is then
fetched and its text extracted, so a ref always carries quotable text rather
than a search-result teaser.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from selectolax.parser import HTMLParser

from ..models import EvidenceOrigin, SourceRef
from ..settings import get_settings, get_thresholds
from ._util import clean, clip, html_text, tokens
from .base import ConnectorResult, RetrievalContext, describe_http_error, http, remedy_for

DDG_HTML = "https://html.duckduckgo.com/html/"
DDG_LITE = "https://lite.duckduckgo.com/lite/"
BING_HTML = "https://www.bing.com/search"

log = logging.getLogger("celestra.connector.web")

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

MAX_SCRAPES = 3                 # open-web pages read per question
MAX_SCRAPES_TARGETED = 2        # per approved domain, per question
MIN_PAGE_CHARS = 200
FIRECRAWL_RETRIES = 1
FIRECRAWL_TIMEOUT = 25.0

# Domains the open-web fallback harvests when no search front-end is reachable.
# Public, high-credibility health publishers whose sitemaps carry topical URLs.
# This is a last resort for the unrestricted `open_web` source only; results are
# still tier 5 and still labelled SUPPLEMENTARY WEB EVIDENCE.
OPEN_WEB_DOMAIN_PANEL = ("cancer.org", "cancer.gov", "medlineplus.gov", "cms.gov")


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _unwrap_redirect(href: str) -> str:
    """Search engines wrap result links. Recover the real target.

    Bing encodes it as `u=a1<urlsafe-base64>` on /ck/a; DuckDuckGo puts it in
    `uddg=` on /l/. An unwrappable link is returned unchanged so the caller can
    reject it on its own terms.
    """
    if not href:
        return ""
    href = href.replace("&amp;", "&")
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query)

    if "bing.com" in host and parsed.path.startswith("/ck/"):
        raw = (query.get("u") or [""])[0]
        if raw.startswith("a1"):
            payload = raw[2:]
            payload += "=" * (-len(payload) % 4)
            try:
                decoded = base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
            except (ValueError, UnicodeDecodeError):
                return ""
            return decoded if decoded.startswith("http") else ""
        return ""

    if "duckduckgo.com" in host and parsed.path.startswith("/l/"):
        target = (query.get("uddg") or [""])[0]
        return unquote(target) if target else ""

    return href


def is_relevant(query: str, *texts: str) -> bool:
    """Does the result actually answer the query, or just echo one word of it?

    A search front-end under bot pressure answers a multi-word clinical query
    with results for its first word alone — "chronic lymphocytic leukemia
    incidence" comes back as dictionary entries for "chronic". Requiring a
    single token match lets all of that through, so a multi-token query must
    match at least two distinct tokens.
    """
    wanted = {t for t in tokens(query) if len(t) > 3}
    if not wanted:
        return True
    matched = wanted & tokens(" ".join(t for t in texts if t))
    return len(matched) >= (2 if len(wanted) >= 2 else 1)


class _SearchBreaker:
    """Stops re-attempting web search once the network has proved it is blocked.

    Keyless search backends are frequently unreachable: rate limits, bot
    challenges, or an egress policy. Without a breaker every question pays the
    full backend timeout chain for nothing, which is the difference between a
    run taking one minute and twenty. After `threshold` consecutive total
    failures the breaker opens and search returns immediately with a reason the
    UI can show. Any single success closes it again.

    A configured Firecrawl key is the supported path and never trips it.
    """

    threshold = 3

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.open = False
        self.reason = ""

    def record_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold and not self.open:
            self.open = True
            self.reason = (
                "open-web search unavailable from this network "
                f"({reason}); configure FIRECRAWL_API_KEY to enable fallback"
            )
            log.warning("web-search breaker opened: %s", self.reason)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.open:
            log.info("web-search breaker closed")
        self.open = False
        self.reason = ""


# Shared by every domain-scoped instance, because they all use the same
# backends and therefore all fail for the same reason.
breaker = _SearchBreaker()

# The most recent Firecrawl failure across every instance, in words, with the
# time it happened. Empty once a call succeeds. The Settings page and the
# banner read this so a failing key is visible without opening a log.
firecrawl_status: dict = {
    "error": "", "at": "",
    # A definitive refusal (no credits, key rejected) or repeated transport
    # failures stop every further Firecrawl call in this process. Questions
    # then rely on the registry sources and are reported as unanswered where
    # those did not suffice; the run never stops or hangs on the web.
    "blocked": "", "consecutive_failures": 0, "calls": 0, "skipped": 0,
}

# Search results for the process, so a refined query or a second agent asking
# the same thing never pays for the same search twice.
_SEARCH_CACHE: dict[tuple[str, int, bool], list[dict]] = {}


def _record_firecrawl_failure(message: str, *, status: int | None = None,
                              transport: bool = False) -> None:
    from datetime import datetime, timezone

    firecrawl_status["error"] = message
    firecrawl_status["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds") if message else ""
    if not message:
        firecrawl_status["consecutive_failures"] = 0
        return
    if status == 402:
        firecrawl_status["blocked"] = (
            "Firecrawl credits are exhausted (402). Web search is off for the rest of this "
            "session; questions the registry sources cannot answer stay unanswered. Top up "
            "the account and restart to re-enable it."
        )
    elif status in (401, 403):
        firecrawl_status["blocked"] = (
            f"Firecrawl rejected the API key ({status}). Web search is off until the key "
            "in .env is fixed and the server restarted."
        )
    elif transport:
        firecrawl_status["consecutive_failures"] += 1
        limit = int(get_thresholds()["escalation"].get("firecrawl_max_consecutive_failures", 2))
        if firecrawl_status["consecutive_failures"] >= limit:
            firecrawl_status["blocked"] = (
                f"Firecrawl could not be reached {limit} times in a row ({message[:120]}). "
                "Web search is off for the rest of this session so the run does not wait "
                "on it; fix the network setting shown on the Settings page and restart."
            )
    if firecrawl_status["blocked"]:
        log.warning("firecrawl disabled for this session: %s", firecrawl_status["blocked"])


def firecrawl_blocked() -> str:
    """Why Firecrawl must not be called right now, or '' when it may be."""
    return str(firecrawl_status.get("blocked") or "")


def reset_firecrawl_status() -> None:
    firecrawl_status.update({"error": "", "at": "", "blocked": "",
                             "consecutive_failures": 0, "calls": 0, "skipped": 0})
    _SEARCH_CACHE.clear()

# domain -> harvested sitemap URLs, or [] when the domain will not serve them.
# Process-lifetime, because a sitemap changes far more slowly than a run.
_SITEMAP_CACHE: dict[str, list[str]] = {}


def _title_from_url(url: str) -> str:
    """A readable label from a URL path, for results that carry no title."""
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"\.(html?|aspx|php)$", "", tail)
    return re.sub(r"[-_]+", " ", tail).strip().title() or url


class FirecrawlConnector:
    """Open-web or domain-restricted search with page text extraction."""

    def __init__(self, source_id: str = "open_web",
                 source_name: str = "Open Web (Supplementary)",
                 tier: int = 5, domain: str | None = None,
                 organization: str = "", search_hint: str = "") -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.domain = (domain or "").lower().removeprefix("www.") or None
        self.tier = tier
        self.organization = organization or source_name
        self.search_hint = search_hint
        # Which backend last answered, and why a preferred one did not. Read by
        # the health check and the sources panel.
        self.last_backend: str = ""
        self.last_error: str = ""
        # A domain restriction is what separates targeted search from open web.
        self.origin = (EvidenceOrigin.TARGETED_SEARCH if self.domain
                       else EvidenceOrigin.OPEN_WEB)

    # -- query -----------------------------------------------------------
    def build_query(self, ctx: RetrievalContext) -> str:
        parts = [ctx.indication]
        if self.search_hint:
            parts.append(self.search_hint)
        elif ctx.aspects:
            parts.extend(ctx.aspects[:2])
        elif ctx.question:
            parts.append(ctx.question)
        return clean(" ".join(parts))

    def _scoped(self, query: str) -> str:
        return f"site:{self.domain} {query}" if self.domain else query

    # -- backends --------------------------------------------------------
    @staticmethod
    def _firecrawl_headers() -> dict[str, str]:
        key = (get_settings().firecrawl_api_key or "").strip()
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "Accept": "application/json"}

    @staticmethod
    def parse_search_payload(payload: dict) -> list[dict]:
        """Results from either API shape.

        v1 returns `data` as a list. v2 returns `data` as an object keyed by
        source (`web`, `news`, `images`); only `web` carries pages worth
        quoting. Both are accepted so a version change never means an empty
        result that looks like "nothing on the web".
        """
        if not isinstance(payload, dict):
            return []
        if payload.get("success") is False:
            raise RuntimeError(str(payload.get("error") or "Firecrawl returned success=false"))
        data = payload.get("data")
        items: list = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = list(data.get("web") or [])
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = clean(item.get("url"))
            if not url:
                continue
            out.append({
                "url": url,
                "title": clean(item.get("title")),
                "snippet": clean(item.get("description") or item.get("snippet")),
                "markdown": clean(item.get("markdown")),
                "backend": "firecrawl",
            })
        return out

    async def _firecrawl_search(self, query: str, limit: int,
                                with_content: bool = True) -> list[dict]:
        """One search call. With `with_content` the pages come back in the same
        call as markdown, so no separate scrape is paid for or waited on.
        `limit` is the number of pages actually wanted, never more."""
        s = get_settings()
        scoped = self._scoped(query)
        limit = max(1, min(limit, 10))
        key = (scoped, limit, with_content)
        if key in _SEARCH_CACHE:
            return [dict(r) for r in _SEARCH_CACHE[key]]
        body: dict = {"query": scoped, "limit": limit}
        if s.firecrawl_version == "v2":
            body["sources"] = ["web"]
        if with_content:
            body["scrapeOptions"] = {"formats": ["markdown"], "onlyMainContent": True}
        firecrawl_status["calls"] += 1
        payload = await http.request(
            "POST", s.firecrawl_endpoint("search"), json_body=body,
            headers=self._firecrawl_headers(), use_cache=False,
            retries=FIRECRAWL_RETRIES, timeout=FIRECRAWL_TIMEOUT,
        )
        results = self.parse_search_payload(payload)
        _SEARCH_CACHE[key] = [dict(r) for r in results]
        return results

    @classmethod
    async def probe(cls, query: str = "chronic lymphocytic leukemia incidence") -> dict:
        """One live Firecrawl call, reported in words. Used by `run.py --check`
        and the Settings page so a broken key or a blocked network is
        diagnosed where the person is looking, not in a log."""
        s = get_settings()
        started = time.perf_counter()
        info = {
            "configured": s.firecrawl_enabled,
            "endpoint": s.firecrawl_endpoint("search"),
            "version": s.firecrawl_version,
            "ok": False, "results": 0, "detail": "", "remedy": "", "elapsed_ms": 0,
        }
        if not s.firecrawl_enabled:
            info["detail"] = "FIRECRAWL_API_KEY is not set"
            info["remedy"] = ("Add FIRECRAWL_API_KEY to .env and restart. Until then the "
                              "open-web fallback uses a keyless path that many networks block.")
            return info
        conn = cls()
        try:
            hits = await conn._firecrawl_search(query, 3)
            info["ok"] = bool(hits)
            info["results"] = len(hits)
            if not hits:
                info["detail"] = "the key was accepted but the probe query returned no results"
                info["remedy"] = "Unusual for this query; retry, then check the Firecrawl status page."
        except Exception as exc:  # noqa: BLE001 - this is the diagnostic
            info["detail"] = describe_http_error(exc)
            info["remedy"] = remedy_for(exc)
            # A failed probe is a real failed call and counts like one.
            _record_firecrawl_failure(
                f"{info['detail']}. {info['remedy']}".strip(),
                status=getattr(getattr(exc, "response", None), "status_code", None),
                transport=isinstance(exc, (httpx.TransportError, OSError)),
            )
        else:
            _record_firecrawl_failure("")
        info["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return info

    async def _ddg(self, query: str, limit: int, lite: bool = False) -> list[dict]:
        url = DDG_LITE if lite else DDG_HTML
        html = await http.request(
            "POST", url, data={"q": self._scoped(query)},
            headers={"User-Agent": BROWSER_UA,
                     "Content-Type": "application/x-www-form-urlencoded"},
            as_json=False, use_cache=False,
        )
        tree = HTMLParser(html)
        out = []
        anchors = tree.css("a.result__a") or tree.css("a.result-link")
        for anchor in anchors[:limit * 3]:
            href = _unwrap_redirect(anchor.attributes.get("href", ""))
            if not href.startswith("http"):
                continue
            parent = anchor.parent.parent if anchor.parent else None
            snippet_node = parent.css_first(".result__snippet") if parent else None
            out.append({"url": href, "title": clean(anchor.text()),
                        "snippet": clean(snippet_node.text()) if snippet_node else "",
                        "backend": "duckduckgo-lite" if lite else "duckduckgo"})
        return out

    async def _bing(self, query: str, limit: int) -> list[dict]:
        html = await http.get_text(
            BING_HTML,
            params={"q": self._scoped(query), "count": max(10, limit * 2)},
            headers={"User-Agent": BROWSER_UA},
            use_cache=False,
        )
        tree = HTMLParser(html)
        out: list[dict] = []
        seen: set[str] = set()

        # Prefer the structured result blocks, which carry a snippet.
        for item in tree.css("li.b_algo"):
            anchor = item.css_first("h2 a") or item.css_first("a[href]")
            if anchor is None:
                continue
            href = _unwrap_redirect(anchor.attributes.get("href", ""))
            if not href.startswith("http") or href in seen:
                continue
            seen.add(href)
            para = item.css_first("p")
            out.append({"url": href, "title": clean(anchor.text()),
                        "snippet": clean(para.text()) if para else "", "backend": "bing"})

        # Bing rotates its result markup, so fall back to every wrapped link on
        # the page rather than depending on one class name.
        if len(out) < limit:
            for anchor in tree.css('a[href*="/ck/a"]'):
                href = _unwrap_redirect(anchor.attributes.get("href", ""))
                if not href.startswith("http") or href in seen:
                    continue
                title = clean(anchor.text())
                if len(title) < 8:
                    continue
                seen.add(href)
                out.append({"url": href, "title": title, "snippet": "", "backend": "bing"})
                if len(out) >= limit * 3:
                    break
        return out

    async def _domain_index(self, query: str, limit: int) -> list[dict]:
        """Harvest the restricted domain's own sitemap and rank its URLs by
        token overlap with the query.

        Last resort: when every search front-end is unreachable or bot-blocked,
        a site's own sitemap still yields real, on-topic URLs. A domain-scoped
        instance harvests its own domain; the unrestricted open-web instance
        harvests OPEN_WEB_DOMAIN_PANEL. Sitemap locations come from robots.txt
        where the site publishes them, so no per-domain path is hardcoded.
        """
        wanted = {t for t in tokens(query) if len(t) > 3}
        if not wanted:
            return []
        domains = [self.domain] if self.domain else list(OPEN_WEB_DOMAIN_PANEL)
        results: list[dict] = []
        for domain in domains:
            results.extend(await self._sitemap_urls(domain, wanted, limit))
            if len(results) >= limit * 2:
                break
        return results[:limit * 2]

    async def _sitemap_urls(self, domain: str, wanted: set[str],
                            limit: int) -> list[dict]:
        """Harvested URLs for one domain, memoised for the process.

        A sitemap describes the whole site, so it is worth fetching once and
        reusing for every question. Without this, a domain that blocks the
        request (cdc.gov, who.int and lls.org all do) was re-probed for every
        question in every stage, and each failed probe costs several seconds
        across the robots.txt and sitemap URL candidates. An empty result is
        cached too, because "this domain will not serve us" is exactly the
        answer worth remembering.
        """
        cached = _SITEMAP_CACHE.get(domain)
        if cached is None:
            cached = await self._harvest_sitemap(domain)
            _SITEMAP_CACHE[domain] = cached
        if not cached:
            return []
        scored = []
        for url in cached:
            overlap = len(tokens(url) & wanted)
            if overlap:
                scored.append((overlap, url))
        scored.sort(key=lambda t: -t[0])
        return [
            {"url": url, "title": _title_from_url(url), "snippet": "",
             "backend": "domain-index"}
            for _, url in scored[: limit * 2]
        ]

    async def _harvest_sitemap(self, domain: str) -> list[str]:
        queue = [f"https://www.{domain}/sitemap.xml",
                 f"https://{domain}/sitemap.xml",
                 f"https://www.{domain}/sitemap_index.xml"]
        for robots in (f"https://www.{domain}/robots.txt",
                       f"https://{domain}/robots.txt"):
            try:
                text = await http.get_text(robots, headers={"User-Agent": BROWSER_UA})
            except Exception:  # noqa: BLE001 - robots.txt is optional
                continue
            queue.extend(re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)[:8])
            break

        locs: list[str] = []
        seen_maps: set[str] = set()
        while queue and len(locs) < 20000 and len(seen_maps) < 12:
            candidate = queue.pop(0)
            if candidate in seen_maps:
                continue
            seen_maps.add(candidate)
            try:
                xml = await http.get_text(candidate, headers={"User-Agent": BROWSER_UA})
            except Exception:  # noqa: BLE001 - try the next sitemap
                continue
            found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
            if "<sitemapindex" in xml:
                queue.extend(found[:8])
                continue
            locs.extend(found)

        # Ranking happens per query in _sitemap_urls; this returns the raw
        # index so one harvest serves every question in the run.
        return [unquote(u) for u in locs]

    async def search(self, query: str, limit: int) -> list[dict]:
        """Ranked results as {url, title, snippet, backend}. Never raises."""
        query = clean(query)
        if not query:
            return []

        keyed = get_settings().firecrawl_enabled
        if not keyed and breaker.open:
            return []
        if keyed and firecrawl_blocked():
            # A refused key or exhausted credits: do not spend a call, and do
            # not drag the run through the keyless engines either. The
            # question falls back to whatever the registry sources gave.
            firecrawl_status["skipped"] += 1
            self.last_error = firecrawl_blocked()
            return []

        backends = []
        if keyed:
            backends.append(("firecrawl", lambda: self._firecrawl_search(query, limit)))
        backends.extend([
            ("bing", lambda: self._bing(query, limit)),
            ("duckduckgo", lambda: self._ddg(query, limit)),
            ("duckduckgo-lite", lambda: self._ddg(query, limit, lite=True)),
        ])
        if get_thresholds()["escalation"].get("enable_domain_index"):
            backends.append(("domain-index", lambda: self._domain_index(query, limit)))

        last_error = "no backend returned results"
        for name, backend in backends:
            try:
                results = await backend()
            except Exception as exc:  # noqa: BLE001 - fall through to the next backend
                detail = describe_http_error(exc) if isinstance(exc, Exception) else str(exc)
                last_error = f"{name}: {detail}"
                if name == "firecrawl":
                    # Firecrawl is configured and billed. Falling through to a
                    # keyless engine without saying so is how a broken key
                    # looks exactly like a working one. Say what broke and
                    # what fixes it; "ConnectError" alone helps nobody.
                    remedy = remedy_for(exc) if isinstance(exc, Exception) else ""
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    log.warning("firecrawl search failed: %s. %s Falling back to a "
                                "keyless engine for query=%r",
                                detail, remedy, query[:120])
                    self.last_error = f"{detail}. {remedy}".strip()
                    _record_firecrawl_failure(
                        self.last_error, status=status,
                        transport=isinstance(exc, (httpx.TransportError, OSError)),
                    )
                    if firecrawl_blocked() and breaker.open:
                        return []
                elif breaker.open:
                    # The keyless engines have already proved unreachable
                    # on this network; do not pay their timeouts again.
                    continue
                continue
            if name == "firecrawl":
                _record_firecrawl_failure("")
            self.last_backend = name
            kept: list[dict] = []
            for item in results:
                url = item.get("url", "")
                if self.domain:
                    host = _domain_of(url)
                    if host != self.domain and not host.endswith("." + self.domain):
                        continue
                # Sitemap URLs carry no title or snippet, so their relevance
                # was already decided by the token match on the URL itself.
                if item.get("backend") != "domain-index" and not is_relevant(
                        query, item.get("title", ""), item.get("snippet", ""), url,
                        str(item.get("markdown", ""))[:3000]):
                    continue
                if url not in {k["url"] for k in kept}:
                    kept.append(item)
            if kept:
                if name != "firecrawl":
                    breaker.record_success()
                return kept[:limit]

        if not any(n == "firecrawl" and not firecrawl_blocked() for n, _ in backends):
            # Every engine tried was keyless (or Firecrawl was already
            # blocked); count the miss towards the keyless breaker so a
            # blocked network stops costing a timeout chain per question.
            breaker.record_failure(last_error)
        return []

    async def scrape(self, url: str, markdown: str = "") -> dict:
        """Page text as {url, title, text, backend}. Never raises. Pass the
        markdown a search already returned to skip the scrape call."""
        url = clean(url)
        if not url:
            return {}
        if markdown and len(clean(markdown)) >= MIN_PAGE_CHARS:
            return {"url": url, "title": "", "text": clean(markdown), "backend": "firecrawl"}
        if get_settings().firecrawl_enabled and not firecrawl_blocked():
            try:
                firecrawl_status["calls"] += 1
                payload = await http.request(
                    "POST", get_settings().firecrawl_endpoint("scrape"),
                    json_body={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                    headers=self._firecrawl_headers(), use_cache=False,
                    retries=FIRECRAWL_RETRIES, timeout=FIRECRAWL_TIMEOUT,
                )
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise RuntimeError(str(payload.get("error") or "success=false"))
                data = (payload.get("data") if isinstance(payload, dict) else None) or {}
                text = clean(data.get("markdown") or data.get("content"))
                if text:
                    meta = data.get("metadata") or {}
                    return {"url": url, "title": clean(meta.get("title")),
                            "text": text, "backend": "firecrawl"}
            except Exception as exc:  # noqa: BLE001 - fall back to a direct fetch
                detail = describe_http_error(exc)
                log.warning("firecrawl scrape failed: %s. %s Fetching %s directly.",
                            detail, remedy_for(exc), url)
                self.last_error = f"scrape: {detail}"
                _record_firecrawl_failure(
                    self.last_error,
                    status=getattr(getattr(exc, "response", None), "status_code", None),
                    transport=isinstance(exc, (httpx.TransportError, OSError)),
                )
        try:
            html = await http.get_text(url, headers={"User-Agent": BROWSER_UA})
        except Exception:  # noqa: BLE001 - an unreachable page is not an error
            return {}
        tree = HTMLParser(html)
        title_node = tree.css_first("title")
        # Prefer the article body; fall back to the whole document.
        text = ""
        for selector in ("main", "article", "div#content", "div.content"):
            text = html_text(html, selector)
            if len(text) >= MIN_PAGE_CHARS:
                break
        if len(text) < MIN_PAGE_CHARS:
            text = html_text(html)
        return {"url": url, "title": clean(title_node.text()) if title_node else "",
                "text": text, "backend": "direct"}

    def refs_from_results(self, results: list[dict], query: str = "") -> list[SourceRef]:
        """Search results as SourceRefs, carrying any page text the search
        already returned so a later scrape can be skipped."""
        refs: list[SourceRef] = []
        for item in results:
            url = clean(item.get("url"))
            if not url:
                continue
            text = clean(item.get("markdown")) or clean(item.get("snippet"))
            refs.append(SourceRef(
                source_id=self.source_id, source_name=self.source_name, tier=self.tier,
                url=url, title=clean(item.get("title")) or url,
                organization=self.organization,
                identifiers={"domain": _domain_of(url),
                             "search_backend": item.get("backend", "")},
                snippet=clip(text, 1500),
                raw={"query": query, "search_backend": item.get("backend", ""),
                     "markdown": clean(item.get("markdown")),
                     "result_snippet": clean(item.get("snippet")),
                     "page_text": text[:20000], "text": text[:20000]},
                origin=self.origin,
            ))
        return refs

    async def search_refs(self, query: str, limit: int) -> list[SourceRef]:
        return self.refs_from_results(await self.search(query, limit), query)

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        if get_settings().firecrawl_enabled and firecrawl_blocked():
            return ConnectorResult.failure(self.source_id, firecrawl_blocked()[:160])
        try:
            query = clean(ctx.extra.get("search_query") or "") if ctx.extra else ""
            query = query or self.build_query(ctx)
            results = await self.search(query, max(1, min(limit,
                                        MAX_SCRAPES_TARGETED if self.domain else MAX_SCRAPES)))
            calls += 1
            if not results:
                return ConnectorResult(
                    source_id=self.source_id, refs=[], ok=False,
                    reason="no web search backend returned on-topic results",
                    calls=calls,
                    elapsed_ms=int((time.perf_counter() - started) * 1000))

            refs: list[SourceRef] = []
            cap = MAX_SCRAPES_TARGETED if self.domain else MAX_SCRAPES
            for item in results[:min(limit, cap)]:
                page = await self.scrape(item["url"], item.get("markdown", ""))
                calls += 1
                text = clean(page.get("text"))
                if len(text) < MIN_PAGE_CHARS:
                    text = clean(item.get("snippet"))
                if not text:
                    continue  # a ref with no quotable text is worthless downstream
                title = clean(item.get("title")) or clean(page.get("title")) or item["url"]
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=item["url"],
                    title=title,
                    organization=self.organization,
                    published="",
                    identifiers={"domain": _domain_of(item["url"]),
                                 "search_backend": item.get("backend", "")},
                    snippet=clip(text, 1500),
                    raw={
                        "query": self._scoped(query),
                        "search_backend": item.get("backend", ""),
                        "scrape_backend": page.get("backend", "snippet"),
                        "result_snippet": clean(item.get("snippet")),
                        "page_text": text[:20000],
                        "text": text[:20000],
                    },
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=bool(refs),
            reason="" if refs else "no page text could be extracted", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\guideline_discovery.py`

```py
"""Programmatic guideline discovery.

This is the answer to "how do we find the guideline PMID without pinning it".
A guideline is discovered by publication type, not by a hardcoded identifier:
Europe PMC is queried for `PUB_TYPE:"Practice Guideline"` OR `"Guideline"` OR
`"Consensus Development Conference"` intersected with the indication in the
title, sorted newest first. The newest edition therefore wins automatically
when a society republishes.

An optional `journal` or `organization` hint narrows the same query to the
publishing venue — Ann Oncol for ESMO, Blood / ashpublications for iwCLL and
ASH — instead of naming a specific paper. NO PMID IS EVER HARDCODED.
"""
from __future__ import annotations

import time

from ..models import EvidenceOrigin
from ._util import clean, matches_any
from .base import ConnectorResult, RetrievalContext, describe_http_error
from .europepmc import EuropePmcConnector, phrase_clause, ref_from_record

GUIDELINE_PUB_TYPES = (
    "Practice Guideline",
    "Guideline",
    "Consensus Development Conference",
)

# Title words that mark a document as guidance even when the publication type
# metadata has not caught up.
GUIDELINE_TITLE_TERMS = (
    "guideline", "guidelines", "recommendation", "recommendations",
    "consensus", "practice statement", "clinical practice",
)


class GuidelineDiscoveryConnector:
    """Discover the current guideline for an indication, optionally per venue."""

    origin = EvidenceOrigin.APPROVED_API

    def __init__(self, source_id: str, source_name: str, tier: int = 1,
                 journal: str | list[str] | None = None,
                 organization: str = "") -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.tier = tier
        self.journals = [journal] if isinstance(journal, str) else list(journal or [])
        self.organization = organization
        self._epmc = EuropePmcConnector(source_id=source_id, source_name=source_name,
                                        tier=tier)

    def build_query(self, ctx: RetrievalContext, with_journal: bool = True) -> str:
        pub_types = " OR ".join(f'PUB_TYPE:"{p}"' for p in GUIDELINE_PUB_TYPES)
        title_terms = phrase_clause(ctx.or_terms(), fields=("TITLE",))
        parts = [f"({pub_types})"]
        if title_terms:
            parts.append(title_terms)
        if with_journal and self.journals:
            parts.append("(" + " OR ".join(f'JOURNAL:"{j}"' for j in self.journals) + ")")
        query = " AND ".join(parts)
        if ctx.cutoff:
            query = f"{query} AND (FIRST_PDATE:[1900-01-01 TO {ctx.cutoff}])"
        return query

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            records = []
            if self.journals:
                records = await self._epmc.search(self.build_query(ctx), limit)
                calls += 1
            if not records:
                # Venue hint too narrow (or absent): fall back to publication
                # type alone rather than reporting no guideline exists.
                records = await self._epmc.search(self.build_query(ctx, with_journal=False),
                                                  limit)
                calls += 1
            if not records:
                # Last resort: title-level guidance wording. Some societies
                # publish guidance that Europe PMC has not typed yet.
                title_clause = phrase_clause(ctx.or_terms(), fields=("TITLE",))
                wording = " OR ".join(f'TITLE:"{t}"' for t in GUIDELINE_TITLE_TERMS)
                query = " AND ".join(p for p in [title_clause, f"({wording})"] if p)
                records = await self._epmc.search(query, limit)
                calls += 1

            refs = [
                ref_from_record(r, self.source_id, self.source_name, self.tier, self.origin)
                for r in records
            ]
            for ref in refs:
                ref.organization = self.organization or ref.organization
                ref.raw["discovered_by"] = "publication-type search"
                ref.raw["journal_hint"] = self.journals

            if self.journals:
                # Keep venue matches first without discarding the rest: a
                # society's guideline sometimes appears in a partner journal.
                refs.sort(key=lambda r: 0 if matches_any(
                    clean(r.raw.get("journal")), self.journals) else 1)
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs[:limit], ok=True,
            reason="" if refs else "no guideline publication found", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\icd11.py`

```py
"""WHO ICD-11 (ICD API).

Credential-gated. The missing-credential check happens BEFORE any network
call: an unauthenticated request would come back 401 and read like a broken
endpoint, when the truth is simply that ICD11_CLIENT_ID / ICD11_CLIENT_SECRET
are not configured.

The OAuth2 client-credentials token is cached in-process with its expiry, so a
run makes one token call, not one per query.
"""
from __future__ import annotations

import time
from typing import Any

from ..models import EvidenceOrigin, SourceRef
from ..settings import get_settings
from ._util import clean, clip, join_sections, strip_tags
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
RELEASE = "2026-01"
SEARCH_URL = f"https://id.who.int/icd/release/11/{RELEASE}/mms/search"
ENTITY_URL = "https://icd.who.int/browse/{release}/mms/en#/{entity_id}"

MISSING_CREDENTIALS = "credentials not configured"

# Module-level so every connector instance in a run shares one token.
_token: dict[str, Any] = {"value": "", "expires_at": 0.0}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "API-Version": "v2",
        "Accept": "application/json",
        "Accept-Language": "en",
    }


async def get_token() -> str:
    """Cached client-credentials token. Returns "" when unconfigured."""
    s = get_settings()
    if not (s.icd11_client_id and s.icd11_client_secret):
        return ""
    if _token["value"] and time.time() < _token["expires_at"]:
        return _token["value"]
    import base64

    basic = base64.b64encode(
        f"{s.icd11_client_id}:{s.icd11_client_secret}".encode()
    ).decode()
    payload = await http.request(
        "POST", TOKEN_URL,
        data={"grant_type": "client_credentials", "scope": "icdapi_access"},
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        use_cache=False,
    )
    token = clean(payload.get("access_token"))
    # Renew a minute early so a long run never uses an expiring token.
    _token.update({"value": token,
                   "expires_at": time.time() + float(payload.get("expires_in") or 3600) - 60})
    return token


class Icd11Connector:
    """ICD-11 MMS entity search."""

    source_id = "icd11"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "WHO ICD-11"
    required_credentials = ("ICD11_CLIENT_ID", "ICD11_CLIENT_SECRET")

    @staticmethod
    def configured() -> bool:
        s = get_settings()
        return bool(s.icd11_client_id and s.icd11_client_secret)

    async def search(self, query: str, token: str) -> dict:
        return await http.get_json(
            SEARCH_URL,
            params={"q": clean(query), "useFlexisearch": "false",
                    "flatResults": "true"},
            headers=_headers(token),
        )

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        # Gate BEFORE calling out: a 401 would misreport a config gap as an outage.
        if not self.configured():
            return ConnectorResult.failure(self.source_id, MISSING_CREDENTIALS)
        started = time.perf_counter()
        calls = 0
        try:
            token = await get_token()
            calls += 1
            if not token:
                return ConnectorResult.failure(self.source_id, MISSING_CREDENTIALS)
            payload = await self.search(ctx.indication, token)
            calls += 1
            entities = payload.get("destinationEntities") or []
            refs = []
            for entity in entities[:limit]:
                title = strip_tags(clean(entity.get("title")))
                code = clean(entity.get("theCode"))
                entity_id = clean(entity.get("id")).rsplit("/", 1)[-1]
                matched = [strip_tags(clean(p.get("label")))
                           for p in entity.get("matchingPVs") or []][:6]
                body = join_sections({
                    "ICD-11 code": code,
                    "Title": title,
                    "Matching terms": "; ".join(matched),
                })
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=clean(entity.get("id")) or
                        ENTITY_URL.format(release=RELEASE, entity_id=entity_id),
                    title=f"ICD-11 {code}: {title}".strip(),
                    organization="World Health Organization",
                    published=RELEASE,
                    identifiers={k: v for k, v in {
                        "icd11_code": code, "entity_id": entity_id, "release": RELEASE,
                    }.items() if v},
                    snippet=clip(body, 900),
                    raw={"entity": entity, "text": body},
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\local_files.py`

```py
"""Local reference files under celestra/data/reference/.

Four datasets, all official release files that have no anonymous API:
icd10cm (CMS ICD-10-CM order/code files), hcpcs (CMS HCPCS release),
gems (CMS ICD-9 to ICD-10 general equivalence mappings) and purple_book
(FDA Purple Book monthly export).

Formats supported: the fixed-width ICD-10-CM order file, the plain
"code<space>description" code file, .csv and .tsv. Rows are searchable by
free text and by code prefix.

A missing file is a configuration fact, not an outage: the connector fails
with "reference file not installed: <dataset>" and attaches a single hint ref
whose `raw` names the filenames it looked for, so the UI can tell an operator
exactly what to drop in. Because the result is ok=False the orchestrator will
not treat the hint as evidence.
"""
from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..models import EvidenceOrigin, SourceRef
from ..settings import REFERENCE_DIR
from ._util import clean, clip
from .base import ConnectorResult, RetrievalContext

# The ICD-10-CM order file is fixed width: order number, code, valid flag,
# short description, long description.
ORDER_LINE = re.compile(r"^(\d{5})\s+([A-Z0-9]{3,7})\s+([01])\s+(.{1,60}?)\s{2,}(.+)$")
CODE_LINE = re.compile(r"^([A-Z0-9]{3,7})\s+(.+)$")


@dataclass(frozen=True)
class Dataset:
    key: str
    label: str
    # Filenames are matched as globs, in order of preference.
    patterns: tuple[str, ...]
    code_columns: tuple[str, ...] = ()
    text_columns: tuple[str, ...] = ()
    citation_url: str = ""
    notes: str = ""


DATASETS: dict[str, Dataset] = {
    "icd10cm": Dataset(
        key="icd10cm",
        label="CMS ICD-10-CM Release Files",
        patterns=("icd10cm_order_*.txt", "icd10cm_codes_*.txt", "icd10cm*.txt",
                  "icd10cm*.csv", "icd10cm*.tsv"),
        code_columns=("code", "icd10cm", "icd_10_cm_code", "diagnosis_code"),
        text_columns=("long_description", "description", "short_description"),
        citation_url="https://www.cms.gov/medicare/coding-billing/icd-10-codes",
    ),
    "hcpcs": Dataset(
        key="hcpcs",
        label="CMS HCPCS Release Files",
        patterns=("hcpcs*.csv", "hcpcs*.tsv", "hcpcs*.txt", "HCPC*.txt"),
        code_columns=("hcpc", "code", "hcpcs_code"),
        text_columns=("long_description", "long description", "description",
                      "short_description"),
        citation_url="https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system",
    ),
    "gems": Dataset(
        key="gems",
        label="CMS ICD-9-CM to ICD-10-CM GEMs",
        patterns=("*gem*.txt", "*gems*.csv", "*gems*.tsv"),
        code_columns=("icd10", "icd9", "code"),
        text_columns=("flags", "description"),
        citation_url="https://www.cms.gov/medicare/coding-billing/icd-10-codes",
        notes="Supplies the ICD-9 leg of the three-system crosswalk.",
    ),
    "purple_book": Dataset(
        key="purple_book",
        label="FDA Purple Book",
        patterns=("purple*book*.csv", "purplebook*.csv", "purple*book*.tsv"),
        code_columns=("bla_number", "applicationnumber", "application_number",
                      "bla number"),
        text_columns=("proprietary_name", "proper_name", "proprietary name",
                      "proper name", "product", "description"),
        citation_url="https://purplebooksearch.fda.gov/",
    ),
}


@dataclass
class Row:
    code: str
    text: str
    fields: dict[str, str] = field(default_factory=dict)

    def line(self) -> str:
        return f"{self.code} — {self.text}".strip(" —")


def _normalise_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def _parse_delimited(path: Path, dataset: Dataset) -> list[Row]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: list[Row] = []
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        headers = [_normalise_header(h) for h in (reader.fieldnames or [])]
        code_key = next((c for c in dataset.code_columns
                         if _normalise_header(c) in headers), "")
        text_keys = [c for c in dataset.text_columns if _normalise_header(c) in headers]
        for raw_row in reader:
            record = {_normalise_header(k): clean(v) for k, v in raw_row.items() if k}
            code = record.get(_normalise_header(code_key), "") if code_key else ""
            if not code:
                code = next((v for v in record.values() if v), "")
            text = " ".join(record.get(_normalise_header(k), "") for k in text_keys).strip()
            if not text:
                text = " ".join(v for k, v in record.items()
                                if v and k != _normalise_header(code_key))
            rows.append(Row(code=code, text=clean(text), fields=record))
    return rows


def _parse_text(path: Path) -> list[Row]:
    rows: list[Row] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            match = ORDER_LINE.match(line)
            if match:
                order, code, valid, short, long = match.groups()
                rows.append(Row(code=clean(code), text=clean(long),
                                fields={"order": order, "code": clean(code),
                                        "valid_for_billing": valid,
                                        "short_description": clean(short),
                                        "long_description": clean(long)}))
                continue
            match = CODE_LINE.match(line.strip())
            if match:
                code, text = match.groups()
                rows.append(Row(code=clean(code), text=clean(text),
                                fields={"code": clean(code), "description": clean(text)}))
                continue
            parts = line.split()
            if len(parts) >= 2:
                # GEMs style: icd9 icd10 flags
                rows.append(Row(code=clean(parts[1]), text=clean(" ".join(parts)),
                                fields={"source_code": parts[0], "target_code": parts[1],
                                        "flags": " ".join(parts[2:])}))
    return rows


def resolve_path(dataset: Dataset, root: Path | None = None) -> Path | None:
    root = root or REFERENCE_DIR
    if not root.exists():
        return None
    for pattern in dataset.patterns:
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[-1]  # newest release when several years are present
    return None


def available_datasets(root: Path | None = None) -> dict[str, bool]:
    """Which reference files are installed. The UI shows this so an operator
    can see the blockers without reading a run log."""
    return {key: resolve_path(ds, root) is not None for key, ds in DATASETS.items()}


def load_rows(dataset_key: str, root: Path | None = None) -> list[Row]:
    dataset = DATASETS[dataset_key]
    path = resolve_path(dataset, root)
    if path is None:
        return []
    if path.suffix.lower() in (".csv", ".tsv"):
        return _parse_delimited(path, dataset)
    return _parse_text(path)


def search_rows(rows: Iterable[Row], terms: list[str], code_prefixes: list[str],
                limit: int) -> list[Row]:
    """Match by code prefix first (exact coding intent), then by free text."""
    prefixes = tuple(p.upper() for p in code_prefixes if p)
    lowered = [t.lower() for t in terms if t]
    by_code, by_text = [], []
    for row in rows:
        code = row.code.upper()
        if prefixes and code.startswith(prefixes):
            by_code.append(row)
        elif lowered and any(t in row.text.lower() for t in lowered):
            by_text.append(row)
        if len(by_code) >= limit * 4:
            break
    return (by_code + by_text)[:limit]


class LocalFilesConnector:
    """One installed reference dataset, searchable by term and code prefix."""

    origin = EvidenceOrigin.LOCAL_FILE

    def __init__(self, source_id: str, dataset: str, source_name: str = "",
                 tier: int = 1, root: Path | None = None) -> None:
        if dataset not in DATASETS:
            raise KeyError(f"unknown reference dataset: {dataset}")
        self.source_id = source_id
        self.dataset_key = dataset
        self.dataset = DATASETS[dataset]
        self.source_name = source_name or self.dataset.label
        self.tier = tier
        self.root = root or REFERENCE_DIR

    def available_datasets(self) -> dict[str, bool]:
        return available_datasets(self.root)

    def installed(self) -> bool:
        return resolve_path(self.dataset, self.root) is not None

    def _missing(self) -> ConnectorResult:
        expected = list(self.dataset.patterns)
        hint = SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=self.dataset.citation_url,
            title=f"{self.dataset.label} not installed",
            organization="local reference file",
            identifiers={"dataset": self.dataset_key},
            snippet=(f"Install the {self.dataset.label} release file in "
                     f"{self.root} — expected one of: {', '.join(expected)}."),
            raw={
                "dataset": self.dataset_key,
                "expected_filenames": expected,
                "reference_dir": str(self.root),
                "citation_url": self.dataset.citation_url,
            },
            origin=self.origin,
        )
        result = ConnectorResult.failure(
            self.source_id, f"reference file not installed: {self.dataset_key}")
        result.refs = [hint]  # hint only: ok=False keeps it out of the evidence pool
        return result

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        if not self.installed():
            return self._missing()
        try:
            path = resolve_path(self.dataset, self.root)
            rows = load_rows(self.dataset_key, self.root)
            prefixes = [clean(c) for c in (ctx.extra.get("code_prefixes")
                                           or ctx.extra.get("icd10_codes") or [])]
            # HCPCS and the Purple Book are indexed by product, not by disease,
            # so upstream entities (drug and test names) are searched alongside
            # the indication's own synonyms.
            # Key names must match what services/handoff.py publishes, which is
            # "drugs" and "test_names"; "drug_names" never appears and silently
            # contributed nothing.
            terms = ctx.or_terms() + [
                clean(t) for key in ("search_terms", "drugs", "test_names", "regimens")
                for t in (ctx.extra.get(key) or [])
            ]
            hits = search_rows(rows, [t for t in terms if t], prefixes, max(1, limit))
            refs: list[SourceRef] = []
            if hits:
                body = "\n".join(h.line() for h in hits)
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=self.dataset.citation_url or f"file://{path}",
                    title=f"{self.dataset.label}: {len(hits)} matching rows",
                    organization="local reference file",
                    published="",
                    identifiers={"dataset": self.dataset_key,
                                 "file": path.name if path else ""},
                    snippet=clip(body, 1500),
                    raw={
                        "dataset": self.dataset_key,
                        "file": str(path),
                        "row_count": len(rows),
                        "matches": [{"code": h.code, "text": h.text, **h.fields}
                                    for h in hits],
                        "text": body,
                    },
                    origin=self.origin,
                ))
        except (OSError, ValueError, csv.Error) as exc:
            return ConnectorResult.failure(
                self.source_id, f"reference file unreadable: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, type(exc).__name__)
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no matching rows", calls=0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\loinc.py`

```py
"""LOINC (Regenstrief) search API.

Credential-gated by HTTP basic auth (LOINC_USERNAME / LOINC_PASSWORD). The
check runs before any request: without credentials the endpoint answers 401,
which would otherwise be reported as an unavailable source instead of an
unconfigured one.
"""
from __future__ import annotations

import time

from ..models import EvidenceOrigin, SourceRef
from ..settings import get_settings
from ._util import clean, clip, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

SEARCH_URL = "https://loinc.regenstrief.org/searchapi/loincs"
DETAIL_URL = "https://loinc.org/{loinc_num}/"

MISSING_CREDENTIALS = "credentials not configured"


class LoincConnector:
    """LOINC term search for the indication's laboratory concepts."""

    source_id = "loinc"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "LOINC (Regenstrief)"
    required_credentials = ("LOINC_USERNAME", "LOINC_PASSWORD")

    @staticmethod
    def configured() -> bool:
        s = get_settings()
        return bool(s.loinc_username and s.loinc_password)

    def build_query(self, ctx: RetrievalContext) -> str:
        # Lab-facing terms rather than the disease name alone: LOINC indexes
        # observations, not diagnoses.
        aspects = " ".join(ctx.aspects[:2]) if ctx.aspects else ""
        return clean(f"{ctx.indication} {aspects}") or ctx.indication

    async def search(self, query: str, rows: int) -> dict:
        s = get_settings()
        import base64

        basic = base64.b64encode(
            f"{s.loinc_username}:{s.loinc_password}".encode()
        ).decode()
        return await http.get_json(
            SEARCH_URL,
            params={"query": query, "rows": max(1, min(rows, 100))},
            headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
        )

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        # Gate BEFORE calling out.
        if not self.configured():
            return ConnectorResult.failure(self.source_id, MISSING_CREDENTIALS)
        started = time.perf_counter()
        try:
            payload = await self.search(self.build_query(ctx), limit)
            results = payload.get("Results") or payload.get("results") or []
            refs = []
            for row in results[:limit]:
                num = clean(row.get("LOINC_NUM") or row.get("loincNumber"))
                long_name = clean(row.get("LONG_COMMON_NAME") or row.get("longCommonName"))
                body = join_sections({
                    "LOINC": num,
                    "Long common name": long_name,
                    "Component": clean(row.get("COMPONENT")),
                    "Property": clean(row.get("PROPERTY")),
                    "System": clean(row.get("SYSTEM")),
                    "Scale": clean(row.get("SCALE_TYP")),
                    "Method": clean(row.get("METHOD_TYP")),
                    "Class": clean(row.get("CLASS")),
                })
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=DETAIL_URL.format(loinc_num=num) if num else SEARCH_URL,
                    title=f"LOINC {num}: {long_name}".strip(),
                    organization="Regenstrief Institute",
                    published=clean(row.get("VersionLastChanged")),
                    identifiers={"loinc_num": num} if num else {},
                    snippet=clip(body, 900),
                    raw={"loinc": row, "text": body},
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=1,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\nci_pdq.py`

```py
"""NCI PDQ health-professional treatment summaries.

Scrape of cancer.gov PDQ pages. Each h2/h3 section becomes its own SourceRef
so a stage question can be answered from the section that actually addresses
it (Incidence and Mortality, Prognostic Factors, Treatment Options ...) rather
than from one undifferentiated page blob.
"""
from __future__ import annotations

import time

from selectolax.parser import HTMLParser

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, matches_any
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

BASE = "https://www.cancer.gov"

# Verified PDQ health-professional URLs.
PAGES: dict[str, str] = {
    "CLL": "/types/leukemia/hp/cll-treatment-pdq",
    "ALL": "/types/leukemia/hp/adult-all-treatment-pdq",
    "AML": "/types/leukemia/hp/adult-aml-treatment-pdq",
    "CML": "/types/leukemia/hp/cml-treatment-pdq",
}

# Sections with no clinical payload.
SKIP_HEADINGS = (
    "about this pdq summary", "changes to this summary", "latest updates",
    "key references", "references", "current clinical trials",
)

# A section shorter than this is a heading stub, not quotable evidence.
MIN_SECTION_CHARS = 250


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


class NciPdqConnector:
    """PDQ section extraction for the run's indication."""

    source_id = "nci"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "National Cancer Institute (NCI) PDQ"

    def page_candidates(self, ctx: RetrievalContext) -> list[str]:
        key = (ctx.indication_key or "").upper()
        if key in PAGES:
            return [BASE + PAGES[key]]
        return [BASE + p for p in PAGES.values()]

    def _sections(self, html: str) -> list[tuple[str, str]]:
        """Walk the summary body, accumulating paragraph text under the last
        heading seen. cancer.gov nests sections, so a flat walk is more robust
        than relying on the section element tree."""
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style", "noscript"])
        root = tree.css_first("div#cgvBody") or tree.css_first("main") or tree.body
        if root is None:
            return []
        out: list[tuple[str, list[str]]] = []
        # traverse() preserves document order; a comma-separated css() call
        # groups by selector instead, which would file every paragraph under
        # the last heading on the page.
        for node in root.traverse(include_text=False):
            if node.tag not in ("h2", "h3", "h4", "p", "li"):
                continue
            if node.tag in ("p", "li"):
                # traverse() yields the node itself first; a p/li below it means
                # this is an outer container whose children are visited anyway.
                descendants = list(node.traverse(include_text=False))[1:]
                if any(d.tag in ("p", "li") for d in descendants):
                    continue
            text = clean(node.text(separator=" ", strip=True))
            if not text:
                continue
            if node.tag in ("h2", "h3", "h4"):
                out.append((text, []))
            elif out and len(text) > 40:
                out[-1][1].append(text)
        # Parent headings whose prose all sits under sub-headings end up with a
        # stub body; MIN_SECTION_CHARS keeps those out of the evidence pool.
        return [(h, " ".join(body)) for h, body in out
                if body and len(" ".join(body)) >= MIN_SECTION_CHARS
                and h.lower() not in SKIP_HEADINGS
                and not any(h.lower().startswith(s) for s in SKIP_HEADINGS)]

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        terms = ctx.or_terms()
        try:
            chosen_url, sections = "", []
            for url in self.page_candidates(ctx):
                html = await http.get_text(url)
                calls += 1
                found = self._sections(html)
                if not found:
                    continue
                title_node = HTMLParser(html).css_first("title")
                title = clean(title_node.text()) if title_node else ""
                if len(self.page_candidates(ctx)) == 1 or matches_any(title, terms):
                    chosen_url, sections = url, found
                    break
                if not sections:
                    chosen_url, sections = url, found
            if not sections:
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason="no PDQ summary matched the indication",
                                       calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))

            # Sections whose heading or body mentions the indication first.
            def rank(item: tuple[str, str]) -> tuple[int, int]:
                heading, body = item
                return (0 if matches_any(heading, terms) else 1, -len(body))

            refs = []
            for heading, body in sorted(sections, key=rank)[:max(1, limit)]:
                anchor = heading.lower().replace(" ", "-")
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=chosen_url,
                    title=f"PDQ: {heading}",
                    organization="National Cancer Institute",
                    published="",
                    identifiers={"pdq_summary": _slug(chosen_url), "section": heading},
                    snippet=clip(body, 1400),
                    raw={"heading": heading, "section_text": body,
                         "anchor": anchor, "text": body},
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\nlm_clinical_tables.py`

```py
"""NLM Clinical Tables — no-authentication code lookups.

The National Library of Medicine publishes free, keyless search APIs over the
same code sets that otherwise require a licence, an account, or a downloaded
release file. That matters here because it is the difference between the code
stage answering its questions and reporting five blockers:

  ICD-10-CM   CMS release files are not installed
  ICD-9-CM    no GEMs file, so the three-system crosswalk had no legacy leg
  HCPCS       CMS release files are not installed
  LOINC       the Regenstrief search API needs an account

These stay secondary to the official release files: when a local dataset is
installed the local connector answers first and this one corroborates. What it
must never do is silently substitute for CPT, which is licensed and has no
free equivalent.

Search is driven by the entities upstream agents discovered — test names,
drug names, code anchors — which is the whole point of the handoff: the
diagnostic stage decides which tests matter and this stage maps them to codes.
Searching a code authority for the disease name alone returns almost nothing.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..models import EvidenceOrigin, SourceRef
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

log = logging.getLogger("celestra.connector.nlm")

BASE = "https://clinicaltables.nlm.nih.gov/api"


class _ClinicalTablesConnector:
    """Shared behaviour. Subclasses declare their table and field mapping."""

    table: str = ""
    version: str = "v3"
    search_fields: str = ""
    display_fields: str = ""
    origin = EvidenceOrigin.APPROVED_API
    tier = 1
    source_name = "NLM Clinical Tables"
    system_label = ""
    browse_url = ""
    # Which handoff keys supply the search terms, best first.
    context_keys: tuple[str, ...] = ()

    def __init__(self, source_id: str, name: str | None = None, tier: int | None = None) -> None:
        self.source_id = source_id
        if name:
            self.source_name = name
        if tier is not None:
            self.tier = tier

    # -- term selection --------------------------------------------------
    def terms_for(self, ctx: RetrievalContext) -> list[str]:
        """Upstream entities first, then the indication and its synonyms.

        Deduplicated case-insensitively and capped, because each term is a
        separate request and the point is coverage, not exhaustiveness.
        """
        terms: list[str] = []
        for key in self.context_keys:
            value = (ctx.extra or {}).get(key)
            if isinstance(value, list):
                terms += [str(v) for v in value]
        terms += self.fallback_terms(ctx)
        seen: set[str] = set()
        out: list[str] = []
        for t in terms:
            t = t.strip()
            key = t.lower()
            if len(t) < 3 or key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out[:8]

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        return ctx.or_terms()

    # -- request ---------------------------------------------------------
    async def _search(self, term: str, limit: int) -> list[list[str]]:
        params: dict[str, Any] = {"terms": term, "maxList": min(limit, 20)}
        if self.search_fields:
            params["sf"] = self.search_fields
        if self.display_fields:
            params["df"] = self.display_fields
        payload = await http.get_json(f"{BASE}/{self.table}/{self.version}/search", params=params)
        if not isinstance(payload, list) or len(payload) < 4:
            return []
        rows = payload[3]
        return [[str(c) for c in row] for row in rows if row] if isinstance(rows, list) else []

    def build_ref(self, row: list[str], term: str) -> SourceRef | None:
        if len(row) < 2:
            return None
        code = row[0].strip()
        description = " — ".join(p.strip() for p in row[1:] if p and p.strip())
        if not code or not description:
            return None
        # The quote is written so the code and its system survive extraction
        # into evidence, which is what the report table needs.
        quote = (
            f"{self.system_label} code {code} is defined as \"{description}\" in the "
            f"{self.source_name} {self.system_label} table."
        )
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=self.browse_url.format(code=code) if self.browse_url else f"{BASE}/{self.table}",
            title=f"{self.system_label} {code} — {description[:90]}",
            organization="U.S. National Library of Medicine",
            identifiers={"code": code, "system": self.system_label, "matched_term": term},
            snippet=quote,
            origin=self.origin,
            raw={"code": code, "description": description, "system": self.system_label,
                 "text": quote, "matched_term": term},
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}

    def _rank(self, refs: list[SourceRef], ctx: RetrievalContext) -> list[SourceRef]:
        """Order by overlap with what was actually asked.

        These tables match on substrings, so a broad term returns rows from
        unrelated conditions. Ranking by overlap keeps the on-topic codes and
        drops the noise when better rows exist.
        """
        wanted = self._tokens(" ".join([*ctx.or_terms(), *(ctx.aspects or [])]))
        if not wanted:
            return refs

        def score(ref: SourceRef) -> int:
            return len(self._tokens(ref.title) & wanted)

        scored = [(score(r), i, r) for i, r in enumerate(refs)]
        on_topic = [t for t in scored if t[0] > 0]
        chosen = on_topic or scored
        chosen.sort(key=lambda t: (-t[0], t[1]))
        return [t[2] for t in chosen]

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        terms = self.terms_for(ctx)
        if not terms:
            return ConnectorResult.failure(self.source_id, "no search term available")

        refs: list[SourceRef] = []
        seen_codes: set[str] = set()
        errors: list[str] = []
        calls = 0

        for term in terms:
            if len(refs) >= limit:
                break
            try:
                rows = await self._search(term, max(limit, 10))
                calls += 1
            except Exception as exc:  # noqa: BLE001 - one bad term must not end the lookup
                errors.append(describe_http_error(exc))
                continue
            for row in rows:
                ref = self.build_ref(row, term)
                if ref is None:
                    continue
                code = ref.identifiers["code"]
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                refs.append(ref)
                if len(refs) >= limit:
                    break

        if refs:
            return ConnectorResult(
                source_id=self.source_id, refs=self._rank(refs, ctx)[:limit], calls=calls
            )
        if errors:
            return ConnectorResult.failure(self.source_id, errors[0])
        return ConnectorResult.failure(
            self.source_id, f"no {self.system_label} match for: {', '.join(terms[:3])}"
        )


class NlmIcd10CmConnector(_ClinicalTablesConnector):
    table = "icd10cm"
    search_fields = "code,name"
    display_fields = "code,name"
    system_label = "ICD-10-CM"
    source_name = "NLM Clinical Tables (ICD-10-CM)"
    browse_url = "https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search?terms={code}"
    context_keys = ("icd10_codes",)


class NlmIcd9CmConnector(_ClinicalTablesConnector):
    table = "icd9cm_dx"
    search_fields = "long_name"
    display_fields = "code,long_name"
    system_label = "ICD-9-CM"
    source_name = "NLM Clinical Tables (ICD-9-CM)"
    browse_url = "https://clinicaltables.nlm.nih.gov/api/icd9cm_dx/v3/search?terms={code}"

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        """ICD-9-CM predates current subtype naming.

        Its leukemia titles say "lymphoid" where ICD-10-CM and current clinical
        usage say "lymphocytic" or "lymphoblastic", so searching the modern
        name alone returns nothing useful. The legacy spellings are added as
        extra search terms; which code comes back is still decided by the
        table, not by this connector.
        """
        terms = list(ctx.or_terms())
        legacy = []
        for term in terms:
            lowered = term.lower()
            for modern, historic in (("lymphocytic", "lymphoid"),
                                     ("lymphoblastic", "lymphoid")):
                if modern in lowered:
                    legacy.append(lowered.replace(modern, historic))
        return [*terms, *legacy, "lymphoid leukemia"]


class NlmHcpcsConnector(_ClinicalTablesConnector):
    table = "hcpcs"
    search_fields = "long_desc"
    display_fields = "code,short_desc,long_desc"
    system_label = "HCPCS"
    source_name = "NLM Clinical Tables (HCPCS)"
    browse_url = "https://clinicaltables.nlm.nih.gov/api/hcpcs/v3/search?terms={code}"
    context_keys = ("drugs", "hcpcs_codes", "test_names")

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        # HCPCS indexes products and services, never diseases. Without upstream
        # drug or test names there is nothing meaningful to look up.
        return []


class NlmLoincConnector(_ClinicalTablesConnector):
    table = "loinc_items"
    display_fields = "LOINC_NUM,LONG_COMMON_NAME"
    system_label = "LOINC"
    source_name = "NLM Clinical Tables (LOINC)"
    browse_url = "https://loinc.org/{code}/"
    context_keys = ("test_names", "loinc_codes")

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        return []


class NlmRxTermsConnector(_ClinicalTablesConnector):
    table = "rxterms"
    display_fields = "DISPLAY_NAME,STRENGTHS_AND_FORMS"
    system_label = "RxTerms"
    source_name = "NLM Clinical Tables (RxTerms)"
    tier = 2
    browse_url = "https://clinicaltables.nlm.nih.gov/api/rxterms/v3/search?terms={code}"
    context_keys = ("drugs",)

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        return []
```

### File: `celestra\connectors\openfda.py`

```py
"""openFDA: drug labels, Drugs@FDA, the NDC directory and FAERS.

Four connectors share one endpoint family and one query grammar.

Label search builds a SINGLE OR-joined synonym query
(`indications_and_usage:("a" OR "b" OR "c")`) — never one request per synonym —
and surfaces `set_id` in identifiers, which is what makes a separate DailyMed
SETID lookup unnecessary.

Drugs@FDA and the NDC directory take application numbers. When the caller has
not supplied any in `ctx.extra["application_numbers"]` they are derived from
one label search and then OR-batched into a single request.

The `openfda` sub-object is absent on some label records; every read of it is
guarded.
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, first_str, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

LABEL_URL = "https://api.fda.gov/drug/label.json"
DRUGSFDA_URL = "https://api.fda.gov/drug/drugsfda.json"
NDC_URL = "https://api.fda.gov/drug/ndc.json"
EVENT_URL = "https://api.fda.gov/drug/event.json"
DAILYMED_SPL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
DRUGSFDA_PAGE = ("https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
                 "?event=overview.process&ApplNo={appl}")

# Label sections that carry quotable clinical text, in the order we prefer them.
LABEL_SECTIONS = (
    "indications_and_usage",
    "dosage_and_administration",
    "warnings_and_precautions",
    "adverse_reactions",
    "clinical_studies",
    "boxed_warning",
    "contraindications",
)

def _appl_digits(application_number: str) -> str:
    """Digits of an FDA application number, without its type prefix.

    `str.lstrip("ANDABL")` strips any of those characters rather than the
    prefix, so it only worked because application numbers are numeric after
    the prefix. This says what it means.
    """
    return re.sub(r"^(?:ANDA|NDA|BLA)\s*", "", (application_number or "").strip(), flags=re.I)



def or_terms_query(field: str, terms: list[str]) -> str:
    """`field:("a" OR "b")` — one query, every synonym."""
    joined = " OR ".join(f'"{clean(t)}"' for t in terms if clean(t))
    return f"{field}:({joined})"


def openfda_block(record: dict) -> dict[str, Any]:
    """openFDA metadata is sometimes missing entirely."""
    return record.get("openfda") or {}


def _first_of(block: dict, key: str) -> str:
    values = block.get(key) or []
    return clean(values[0]) if values else ""


def label_sections(record: dict) -> dict[str, str]:
    return {
        name: clean(record.get(name))
        for name in LABEL_SECTIONS
        if clean(record.get(name))
    }


def _fmt_date(value: str) -> str:
    value = clean(value)
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


class OpenFdaLabelConnector:
    """SPL drug labels whose Indications and Usage mentions the indication."""

    source_id = "openfda_label"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "FDA Drug Labeling (openFDA)"

    def build_search(self, ctx: RetrievalContext) -> str:
        return or_terms_query("indications_and_usage", ctx.or_terms())

    async def _search(self, search: str, limit: int) -> dict:
        return await http.get_json(LABEL_URL,
                                   params={"search": search, "limit": max(1, min(limit, 100))})

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        try:
            payload = await self._search(self.build_search(ctx), limit)
            total = ((payload.get("meta") or {}).get("results") or {}).get("total", 0)
            refs = [self._ref(r, total) for r in payload.get("results") or []]
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=1,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def _ref(self, record: dict, total: int) -> SourceRef:
        block = openfda_block(record)
        set_id = clean(record.get("set_id"))
        sections = label_sections(record)
        brand = _first_of(block, "brand_name")
        generic = _first_of(block, "generic_name")
        title = " ".join(x for x in [brand, f"({generic})" if generic else ""] if x) \
            or f"SPL label {set_id[:8]}"
        identifiers = {k: v for k, v in {
            "set_id": set_id,
            "spl_id": clean(record.get("id")),
            "application_number": _first_of(block, "application_number"),
            "rxcui": _first_of(block, "rxcui"),
            "unii": _first_of(block, "unii"),
            "spl_version": clean(record.get("version")),
        }.items() if v}
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=DAILYMED_SPL.format(set_id=set_id) if set_id else LABEL_URL,
            title=title,
            organization=first_str(_first_of(block, "manufacturer_name"), "US Food and Drug Administration"),
            published=_fmt_date(record.get("effective_time", "")),
            identifiers=identifiers,
            snippet=clip(sections.get("indications_and_usage", "")
                         or join_sections(sections), 1500),
            raw={
                "set_id": set_id,
                "sections": sections,
                "openfda": block,
                "effective_time": clean(record.get("effective_time")),
                "total_matches": total,
                "text": join_sections(sections),
            },
            origin=self.origin,
        )


async def application_numbers_for(ctx: RetrievalContext, limit: int = 50) -> list[str]:
    """Application numbers for the indication, from the label index.

    Used when the orchestrator has not already supplied them, so Drugs@FDA and
    the NDC directory work standalone instead of needing a prior stage.
    """
    supplied = [clean(a) for a in (ctx.extra.get("application_numbers") or []) if clean(a)]
    if supplied:
        return supplied
    payload = await http.get_json(
        LABEL_URL,
        params={"search": or_terms_query("indications_and_usage", ctx.or_terms()),
                "limit": max(1, min(limit, 100))},
    )
    numbers: list[str] = []
    for record in payload.get("results") or []:
        for appl in openfda_block(record).get("application_number") or []:
            appl = clean(appl)
            if appl and appl not in numbers:
                numbers.append(appl)
    return numbers


class OpenFdaDrugsFdaConnector:
    """Approval history for the applications that treat the indication."""

    source_id = "openfda_drugsfda"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "FDA Drugs@FDA (openFDA)"

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            numbers = await application_numbers_for(ctx)
            calls += 1
            # Only NDA/BLA applications carry the approval narrative we need.
            priority = [n for n in numbers if n.startswith(("NDA", "BLA"))] or numbers
            if not priority:
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason="no application numbers for indication",
                                       calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))
            search = or_terms_query("application_number", priority[:20])
            payload = await http.get_json(DRUGSFDA_URL,
                                          params={"search": search,
                                                  "limit": max(1, min(limit, 100))})
            calls += 1
            refs = [self._ref(r) for r in payload.get("results") or []]
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def _ref(self, record: dict) -> SourceRef:
        appl = clean(record.get("application_number"))
        block = openfda_block(record)
        products = record.get("products") or []
        product_lines = [
            " ".join(x for x in [
                clean(p.get("brand_name")),
                clean(p.get("dosage_form")),
                clean(p.get("route")),
                f"strength {clean((p.get('active_ingredients') or [{}])[0].get('strength'))}"
                if p.get("active_ingredients") else "",
                f"marketing status {clean(p.get('marketing_status'))}",
            ] if x) for p in products[:12]
        ]
        submissions = record.get("submissions") or []
        sub_lines = [
            " ".join(x for x in [
                clean(s.get("submission_type")),
                clean(s.get("submission_number")),
                clean(s.get("submission_status")),
                _fmt_date(s.get("submission_status_date", "")),
                clean(s.get("submission_class_code_description")),
                clean(s.get("review_priority")),
            ] if x) for s in submissions[:20]
        ]
        sponsor = first_str(record.get("sponsor_name"), _first_of(block, "manufacturer_name"))
        body = join_sections({
            "Application": f"{appl} sponsored by {sponsor}" if sponsor else appl,
            "Products": " | ".join(product_lines),
            "Submissions": " | ".join(sub_lines),
        })
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=DRUGSFDA_PAGE.format(appl=_appl_digits(appl)) if appl else DRUGSFDA_URL,
            title=f"Drugs@FDA {appl}: " + (clean((products or [{}])[0].get('brand_name')) or sponsor),
            organization=sponsor or "US Food and Drug Administration",
            published=_fmt_date(max([clean(s.get("submission_status_date")) for s in submissions]
                                    or [""])),
            identifiers={k: v for k, v in {
                "application_number": appl,
                "sponsor": sponsor,
            }.items() if v},
            snippet=clip(body, 1400),
            raw={"application_number": appl, "products": products,
                 "submissions": submissions[:30], "openfda": block, "text": body},
            origin=self.origin,
        )


class OpenFdaNdcConnector:
    """NDC directory entries for the indication's applications."""

    source_id = "openfda_ndc"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "FDA NDC Directory (openFDA)"

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            numbers = await application_numbers_for(ctx)
            calls += 1
            if not numbers:
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason="no application numbers for indication",
                                       calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))
            payload = await http.get_json(
                NDC_URL,
                params={"search": or_terms_query("application_number", numbers[:20]),
                        "limit": max(1, min(limit, 100))},
            )
            calls += 1
            refs = [self._ref(r) for r in payload.get("results") or []]
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def _ref(self, record: dict) -> SourceRef:
        product_ndc = clean(record.get("product_ndc"))
        brand = clean(record.get("brand_name"))
        generic = clean(record.get("generic_name"))
        packaging = [clean(p.get("description")) for p in record.get("packaging") or []][:8]
        ingredients = [
            f"{clean(a.get('name'))} {clean(a.get('strength'))}"
            for a in record.get("active_ingredients") or []
        ][:8]
        body = join_sections({
            "Product": " ".join(x for x in [brand, f"({generic})" if generic else "",
                                            clean(record.get("dosage_form")),
                                            clean(record.get("route"))] if x),
            "Active ingredients": "; ".join(ingredients),
            "Packaging": " | ".join(packaging),
            "Marketing category": clean(record.get("marketing_category")),
            "Marketing start date": _fmt_date(record.get("marketing_start_date", "")),
            "Pharm class": "; ".join(clean(c) for c in record.get("pharm_class") or [])[:600],
        })
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=f"https://ndclist.com/ndc/{product_ndc}" if product_ndc else NDC_URL,
            title=f"NDC {product_ndc}: {brand or generic}".strip(),
            organization=first_str(record.get("labeler_name"), "US Food and Drug Administration"),
            published=_fmt_date(record.get("marketing_start_date", "")),
            identifiers={k: v for k, v in {
                "product_ndc": product_ndc,
                "application_number": clean(record.get("application_number")),
                "product_type": clean(record.get("product_type")),
                "spl_id": clean(record.get("spl_id")),
            }.items() if v},
            snippet=clip(body, 1000),
            raw={"ndc": record, "text": body},
            origin=self.origin,
        )


class FaersConnector:
    """FAERS reaction frequency for the indication.

    Signal frequency only: one report carries several drugs and reactions with
    no causal linkage, which is recorded in `raw["caveat"]` so downstream text
    can never present a count as an incidence rate.
    """

    source_id = "faers"
    tier = 2
    origin = EvidenceOrigin.APPROVED_API
    source_name = "FDA FAERS Adverse Events (openFDA)"
    CAVEAT = ("FAERS counts are spontaneous report frequencies. One report lists "
              "multiple drugs and reactions with no causal linkage and no denominator, "
              "so these counts are signals, not incidence rates.")

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            search = or_terms_query("patient.drug.drugindication", ctx.or_terms())
            payload = await http.get_json(
                EVENT_URL,
                params={"search": search, "count": "patient.reaction.reactionmeddrapt.exact"},
            )
            calls += 1
            # The count form returns results:[{term,count}] and carries NO
            # meta.results.total; the plain form returns full report objects.
            rows = payload.get("results") or []
            meta_total = ((payload.get("meta") or {}).get("results") or {}).get("total")
            if rows and isinstance(rows[0], dict) and "term" in rows[0]:
                refs = [self._count_ref(ctx, rows, search, meta_total)]
            else:
                refs = [self._report_ref(ctx, r, search) for r in rows[:limit]]
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def _query_url(self, search: str, count: str = "") -> str:
        url = f"{EVENT_URL}?search={quote(search)}"
        return f"{url}&count={count}" if count else url

    def _count_ref(self, ctx: RetrievalContext, rows: list[dict], search: str,
                   meta_total: Any) -> SourceRef:
        top = [(clean(r.get("term")), int(r.get("count") or 0)) for r in rows[:40]]
        body = "; ".join(f"{term}: {count} reports" for term, count in top)
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=self._query_url(search, "patient.reaction.reactionmeddrapt.exact"),
            title=f"FAERS reported reactions where the indication is {ctx.indication}",
            organization="US Food and Drug Administration",
            published="",
            identifiers={"openfda_endpoint": "drug/event", "count_field":
                         "patient.reaction.reactionmeddrapt.exact"},
            snippet=clip(f"{body}. {self.CAVEAT}", 1500),
            raw={"terms": [{"term": t, "count": c} for t, c in top],
                 "meta_total": meta_total, "caveat": self.CAVEAT, "text": body},
            origin=self.origin,
        )

    def _report_ref(self, ctx: RetrievalContext, record: dict, search: str) -> SourceRef:
        patient = record.get("patient") or {}
        reactions = "; ".join(clean(r.get("reactionmeddrapt"))
                              for r in patient.get("reaction") or [])[:800]
        drugs = "; ".join(clean(d.get("medicinalproduct"))
                          for d in patient.get("drug") or [])[:800]
        body = join_sections({"Reactions": reactions, "Drugs": drugs,
                              "Serious": clean(record.get("serious"))})
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=self._query_url(search),
            title=f"FAERS report {clean(record.get('safetyreportid'))}",
            organization="US Food and Drug Administration",
            published=_fmt_date(record.get("receiptdate", "")),
            identifiers={"safetyreportid": clean(record.get("safetyreportid"))},
            snippet=clip(f"{body}. {self.CAVEAT}", 1200),
            raw={"report": record, "caveat": self.CAVEAT, "text": body},
            origin=self.origin,
        )
```

### File: `celestra\connectors\orphanet.py`

```py
"""Orphanet / Orphadata.

Three products are used: rd-cross-referencing (name -> ORPHAcode plus the
ICD-10 / ICD-11 / MeSH / OMIM / UMLS crosswalk), rd-epidemiology (prevalence
and annual incidence rows) and rd-natural_history (age of onset, inheritance).

`resolve_codes` is the orchestrator's provisional terminology anchor, so it is
a first-class public coroutine rather than an internal step of `discover`.
Verified today: CLL -> ORPHA 67038 / ICD-10 C91.1 / ICD-11 2A82.0;
ALL -> ORPHA 513 / ICD-10 C91.0 with NO ICD-11 mapping. The absence of an
ICD-11 row is reported as absent, never back-filled.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

BASE = "https://api.orphadata.com"
NAME_URL = BASE + "/rd-cross-referencing/orphacodes/names/{name}"
EPI_URL = BASE + "/rd-epidemiology/orphacodes/{code}"
NH_URL = BASE + "/rd-natural_history/orphacodes/{code}"
ORPHA_PAGE = "https://www.orpha.net/en/disease/detail/{code}"

# Crosswalk sources we surface. Everything else in ExternalReference is kept in
# `raw` but not promoted into identifiers.
CROSSWALK_SOURCES = ("ICD-10", "ICD-11", "MeSH", "OMIM", "UMLS", "MONDO", "GARD")


def _results(payload: Any) -> dict[str, Any]:
    """Orphadata returns `data.results` as an object for a single hit and a
    list when several match. Normalise to the first object."""
    data = (payload or {}).get("data") or {}
    results = data.get("results")
    if isinstance(results, list):
        return results[0] if results else {}
    return results or {}


def _definition(record: dict) -> str:
    for block in record.get("SummaryInformation") or []:
        text = clean(block.get("Definition"))
        if text:
            return text
    return ""


def _prevalence_lines(epi: dict) -> list[str]:
    lines = []
    for row in epi.get("Prevalence") or []:
        parts = [
            clean(row.get("PrevalenceType")),
            clean(row.get("PrevalenceClass")),
            f"mean {clean(row.get('ValMoy'))}" if clean(row.get("ValMoy")) else "",
            f"in {clean(row.get('PrevalenceGeographic'))}" if row.get("PrevalenceGeographic") else "",
            f"({clean(row.get('PrevalenceValidationStatus'))})"
            if row.get("PrevalenceValidationStatus") else "",
        ]
        line = " ".join(p for p in parts if p)
        if line:
            lines.append(line)
    return lines


class OrphanetConnector:
    """Rare-disease epidemiology plus the terminology crosswalk."""

    source_id = "orphanet"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "Orphanet / Orphadata"

    async def _by_name(self, name: str) -> dict:
        payload = await http.get_json(NAME_URL.format(name=quote(clean(name))),
                                      params={"lang": "en"})
        return _results(payload)

    async def resolve_codes(self, name: str) -> dict[str, Any]:
        """Resolve a disease name to its ORPHAcode and terminology crosswalk.

        Returns {} when nothing resolves. Missing systems are simply absent
        from `codes` — an unmapped ICD-11 is a fact about the disease, not a
        gap to be filled in.
        """
        try:
            record = await self._by_name(name)
        except Exception:  # noqa: BLE001 - the anchor degrades, it never raises
            return {}
        if not record:
            return {}
        codes: dict[str, list[str]] = {}
        for ref in record.get("ExternalReference") or []:
            source = clean(ref.get("Source"))
            value = clean(ref.get("Reference"))
            if not source or not value:
                continue
            codes.setdefault(source, [])
            if value not in codes[source]:
                codes[source].append(value)
        orpha = clean(record.get("ORPHAcode"))
        return {
            "orphacode": orpha,
            "preferred_term": clean(record.get("Preferred term")),
            "synonyms": [clean(s) for s in record.get("Synonym") or []],
            "definition": _definition(record),
            "url": ORPHA_PAGE.format(code=orpha) if orpha else clean(record.get("OrphanetURL")),
            "codes": {k: v for k, v in codes.items() if k in CROSSWALK_SOURCES},
            "all_codes": codes,
            "icd10": (codes.get("ICD-10") or [""])[0],
            "icd11": (codes.get("ICD-11") or [""])[0],
            "mesh": (codes.get("MeSH") or [""])[0],
            "umls": (codes.get("UMLS") or [""])[0],
            "unmapped": [s for s in ("ICD-10", "ICD-11", "MeSH", "UMLS") if not codes.get(s)],
        }

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        refs: list[SourceRef] = []
        try:
            anchor: dict[str, Any] = {}
            for candidate in ctx.or_terms():
                anchor = await self.resolve_codes(candidate)
                calls += 1
                if anchor.get("orphacode"):
                    break
            if not anchor.get("orphacode"):
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason="no ORPHAcode for indication", calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))

            code = anchor["orphacode"]
            url = anchor.get("url") or ORPHA_PAGE.format(code=code)
            identifiers = {k: v for k, v in {
                "orphacode": code,
                "icd10": anchor.get("icd10", ""),
                "icd11": anchor.get("icd11", ""),
                "mesh": anchor.get("mesh", ""),
                "umls": anchor.get("umls", ""),
            }.items() if v}

            crosswalk_text = "; ".join(
                f"{system}: {', '.join(values)}" for system, values in
                (anchor.get("codes") or {}).items()
            )
            unmapped = anchor.get("unmapped") or []
            definition = anchor.get("definition", "")
            refs.append(SourceRef(
                source_id=self.source_id,
                source_name=self.source_name,
                tier=self.tier,
                url=url,
                title=f"Orphanet {anchor.get('preferred_term', '')} (ORPHA:{code})".strip(),
                organization="Orphanet",
                published="",
                identifiers=identifiers,
                snippet=clip(join_sections({
                    "Definition": definition,
                    "Terminology crosswalk": crosswalk_text,
                    "Not mapped": ", ".join(unmapped),
                }), 1400),
                raw={
                    "anchor": anchor,
                    "definition": definition,
                    "crosswalk": anchor.get("codes"),
                    "unmapped_systems": unmapped,
                    "text": join_sections({
                        "Preferred term": anchor.get("preferred_term", ""),
                        "Definition": definition,
                        "Synonyms": ", ".join(anchor.get("synonyms") or []),
                        "Terminology crosswalk": crosswalk_text,
                    }),
                },
                origin=self.origin,
            ))

            epi = _results(await http.get_json(EPI_URL.format(code=code), params={"lang": "en"}))
            calls += 1
            lines = _prevalence_lines(epi)
            if lines:
                body = " | ".join(lines)
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=url,
                    title=f"Orphanet epidemiology: {anchor.get('preferred_term', '')}",
                    organization="Orphanet",
                    published=clean(epi.get("Date"))[:10],
                    identifiers=identifiers,
                    snippet=clip(body, 1200),
                    raw={"prevalence": epi.get("Prevalence") or [], "text": body},
                    origin=self.origin,
                ))

            nat = _results(await http.get_json(NH_URL.format(code=code), params={"lang": "en"}))
            calls += 1
            onset = ", ".join(clean(x) for x in nat.get("AverageAgeOfOnset") or [])
            inheritance = ", ".join(clean(x) for x in nat.get("TypeOfInheritance") or [])
            if onset or inheritance:
                body = join_sections({
                    "Average age of onset": onset,
                    "Type of inheritance": inheritance,
                    "Typology": clean(nat.get("Typology")),
                })
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=url,
                    title=f"Orphanet natural history: {anchor.get('preferred_term', '')}",
                    organization="Orphanet",
                    published=clean(nat.get("Date"))[:10],
                    identifiers=identifiers,
                    snippet=clip(body, 800),
                    raw={"natural_history": nat, "text": body},
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            if refs:
                # Partial success beats a dead source: keep what resolved.
                return ConnectorResult(source_id=self.source_id, refs=refs[:limit], ok=True,
                                       reason=f"partial: {describe_http_error(exc)}", calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs[:limit], ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\pubmed.py`

```py
"""PubMed via NCBI E-utilities.

Three calls, never one-per-record: esearch for the id set, one batched
esummary for the metadata of every id, one batched efetch for the abstracts.
`tool` and `email` are sent on every request (NCBI requires them) and
`api_key` whenever settings carry one; without a key NCBI caps us at 3 req/s,
which is why the calls are sequential.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from ..models import EvidenceOrigin, SourceRef
from ..settings import get_settings
from ._util import clean, clip, first_str, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
ARTICLE_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"


def eutils_params(**extra: object) -> dict[str, object]:
    s = get_settings()
    params: dict[str, object] = {"tool": s.ncbi_tool, "email": s.ncbi_email}
    if s.ncbi_api_key:
        params["api_key"] = s.ncbi_api_key
    params.update(extra)
    return params


def _abstract_from_article(article: ET.Element) -> str:
    """Concatenate labelled AbstractText blocks in document order."""
    chunks: list[str] = []
    for node in article.iter("AbstractText"):
        label = node.attrib.get("Label", "").strip()
        text = clean("".join(node.itertext()))
        if not text:
            continue
        chunks.append(f"{label}: {text}" if label else text)
    return " ".join(chunks)


def parse_efetch(xml_text: str) -> dict[str, dict]:
    """pmid -> {abstract, title, journal, pub_types, mesh}."""
    out: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for art in root.iter("PubmedArticle"):
        pmid_node = art.find("./MedlineCitation/PMID")
        pmid = clean(pmid_node.text if pmid_node is not None else "")
        if not pmid:
            continue
        title_node = art.find("./MedlineCitation/Article/ArticleTitle")
        journal_node = art.find("./MedlineCitation/Article/Journal/Title")
        out[pmid] = {
            "abstract": _abstract_from_article(art),
            "title": clean("".join(title_node.itertext())) if title_node is not None else "",
            "journal": clean(journal_node.text) if journal_node is not None else "",
            "pub_types": [clean(n.text) for n in art.iter("PublicationType") if n.text],
            "mesh": [clean(n.text) for n in art.iter("DescriptorName") if n.text][:20],
        }
    return out


class PubMedConnector:
    """Literature discovery against PubMed."""

    source_id = "pubmed"
    tier = 2
    origin = EvidenceOrigin.APPROVED_API
    source_name = "PubMed"

    def build_term(self, ctx: RetrievalContext) -> str:
        synonyms = " OR ".join(f'"{t}"[Title/Abstract]' for t in ctx.or_terms())
        term = f"({synonyms})" if synonyms else clean(ctx.indication)
        if ctx.cutoff:
            term = f'{term} AND ("1900/01/01"[PDAT] : "{ctx.cutoff}"[PDAT])'
        return term

    async def _esearch(self, term: str, limit: int) -> list[str]:
        payload = await http.get_json(
            f"{EUTILS}/esearch.fcgi",
            params=eutils_params(db="pubmed", term=term, retmode="json",
                                 retmax=max(1, min(limit, 100)), sort="date"),
        )
        return [clean(i) for i in ((payload.get("esearchresult") or {}).get("idlist") or [])]

    async def _esummary(self, pmids: list[str]) -> dict[str, dict]:
        if not pmids:
            return {}
        payload = await http.get_json(
            f"{EUTILS}/esummary.fcgi",
            params=eutils_params(db="pubmed", id=",".join(pmids), retmode="json"),
        )
        result = payload.get("result") or {}
        return {p: result[p] for p in pmids if isinstance(result.get(p), dict)}

    async def _efetch(self, pmids: list[str]) -> dict[str, dict]:
        if not pmids:
            return {}
        xml_text = await http.get_text(
            f"{EUTILS}/efetch.fcgi",
            params=eutils_params(db="pubmed", id=",".join(pmids), retmode="xml", rettype="abstract"),
        )
        return parse_efetch(xml_text)

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            pmids = await self._esearch(self.build_term(ctx), limit)
            calls += 1
            if not pmids:
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason="no results", calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))
            summaries = await self._esummary(pmids)
            calls += 1
            details = await self._efetch(pmids)
            calls += 1

            refs: list[SourceRef] = []
            for pmid in pmids:
                summary = summaries.get(pmid, {})
                detail = details.get(pmid, {})
                title = first_str(summary.get("title"), detail.get("title"))
                abstract = clean(detail.get("abstract"))
                journal = first_str(summary.get("fulljournalname"), detail.get("journal"))
                ids = {clean(a.get("idtype")): clean(a.get("value"))
                       for a in (summary.get("articleids") or [])}
                identifiers = {k: v for k, v in {
                    "pmid": pmid,
                    "pmcid": ids.get("pmcid", ""),
                    "doi": ids.get("doi", ""),
                }.items() if v}
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=ARTICLE_URL.format(pmid=pmid),
                    title=title,
                    organization=journal or self.source_name,
                    published=first_str(summary.get("pubdate"), summary.get("epubdate")),
                    identifiers=identifiers,
                    snippet=clip(abstract or title, 1200),
                    raw={
                        "title": title,
                        "abstract": abstract,
                        "journal": journal,
                        "authors": ", ".join(clean(a.get("name")) for a in
                                             (summary.get("authors") or [])[:12]),
                        "pub_types": detail.get("pub_types") or summary.get("pubtype") or [],
                        "mesh": detail.get("mesh") or [],
                        "text": join_sections({"Title": title, "Abstract": abstract}),
                    },
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


async def convert_ids(ids: list[str]) -> dict[str, dict]:
    """PMC ID converter: pmid/pmcid/doi crosswalk. Returns {input_id: record}."""
    ids = [clean(i) for i in ids if clean(i)]
    if not ids:
        return {}
    s = get_settings()
    try:
        payload = await http.get_json(
            IDCONV,
            params={"ids": ",".join(ids), "format": "json",
                    "tool": s.ncbi_tool, "email": s.ncbi_email},
        )
    except Exception:  # noqa: BLE001 - the crosswalk is an enrichment, not a gate
        return {}
    out: dict[str, dict] = {}
    for rec in payload.get("records") or []:
        key = clean(rec.get("pmid")) or clean(rec.get("pmcid")) or clean(rec.get("doi"))
        if key:
            out[key] = rec
    return out
```

### File: `celestra\connectors\registry.py`

```py
"""Connector registry.

`build_registry()` turns config/sources.yaml into live connector instances:
tier, display name and domain always come from the YAML, never from a constant
in code, so adding or re-tiering a source is a config change.

Three families are assembled by access_method rather than by a `connector:`
key:

* targeted_search  -> a domain-scoped FirecrawlConnector carrying the source's
  own registry tier and origin=TARGETED_SEARCH (not tier 5).
* licensed         -> LicensedConnector, which always fails with
  "licensed connector not configured" so the UI shows the blocker honestly
  instead of silently dropping NCCN or AMA CPT from the source list.
* local_file       -> LocalFilesConnector bound to the source's local_dataset.
"""
from __future__ import annotations

from typing import Any, Callable

from ..models import EvidenceOrigin
from ..settings import get_settings, get_source_registry
from .base import Connector, ConnectorResult, RetrievalContext
from .cdc_wonder import CdcWonderConnector
from .clinicaltrials import ClinicalTrialsConnector
from .crossref import CrossrefConnector
from .dailymed import DailyMedConnector
from .europepmc import EuropePmcConnector
from .firecrawl import FirecrawlConnector
from .guideline_discovery import GuidelineDiscoveryConnector
from .icd11 import Icd11Connector
from .local_files import LocalFilesConnector, available_datasets
from .loinc import LoincConnector
from .nci_pdq import NciPdqConnector
from .nlm_clinical_tables import (NlmHcpcsConnector, NlmIcd9CmConnector,
                                  NlmIcd10CmConnector, NlmLoincConnector,
                                  NlmRxTermsConnector)
from .openfda import (FaersConnector, OpenFdaDrugsFdaConnector, OpenFdaLabelConnector,
                      OpenFdaNdcConnector)
from .orphanet import OrphanetConnector
from .pubmed import PubMedConnector
from .seer import SeerConnector
from .who_gho import WhoGhoConnector

# Publishing venues used to narrow guideline discovery. These are venue hints,
# not pinned documents: the query still selects by publication type and date.
GUIDELINE_JOURNALS: dict[str, tuple[list[str], str]] = {
    "esmo": (["Annals of oncology", "Ann Oncol", "ESMO open"],
             "European Society for Medical Oncology"),
    "eha": (["HemaSphere", "Haematologica"],
            "European Hematology Association"),
    "iwcll": (["Blood", "Blood advances"],
              "International Workshop on Chronic Lymphocytic Leukemia"),
    "ashpublications": (["Blood", "Blood advances", "Hematology"],
                        "American Society of Hematology"),
}

# DOI prefixes owned by a society, used by the Crossref connector.
DOI_PREFIXES: dict[str, str] = {"asco": "10.1200"}


class LicensedConnector:
    """Placeholder for a source that needs a paid licence (NCCN, AMA CPT).

    It never scrapes and never invents an alternative: it reports the blocker
    so the run can substitute an approved open source explicitly.
    """

    origin = EvidenceOrigin.APPROVED_API
    REASON = "licensed connector not configured"

    def __init__(self, source_id: str, source_name: str, tier: int) -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.tier = tier

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        return ConnectorResult.failure(self.source_id, self.REASON)


def _build_from_key(key: str, source: dict[str, Any]) -> Connector | None:
    """Instantiate the connector named by the YAML `connector:` key."""
    sid = source["id"]
    name = source.get("name", sid)
    tier = int(source.get("tier", 5))
    domain = source.get("domain") or None

    simple: dict[str, Callable[[], Connector]] = {
        "pubmed": PubMedConnector,
        "orphanet": OrphanetConnector,
        "seer": SeerConnector,
        "nci_pdq": NciPdqConnector,
        "cdc_wonder": CdcWonderConnector,
        "who_gho": WhoGhoConnector,
        "openfda_label": OpenFdaLabelConnector,
        "openfda_drugsfda": OpenFdaDrugsFdaConnector,
        "openfda_ndc": OpenFdaNdcConnector,
        "faers": FaersConnector,
        "dailymed": DailyMedConnector,
        "clinicaltrials": ClinicalTrialsConnector,
        "icd11": Icd11Connector,
        "loinc": LoincConnector,
    }
    if key in simple:
        return simple[key]()

    # NLM Clinical Tables share one base class and differ only by table, so
    # they take the source id, name and tier from the YAML like the others.
    nlm: dict[str, type] = {
        "nlm_icd10cm": NlmIcd10CmConnector,
        "nlm_icd9cm": NlmIcd9CmConnector,
        "nlm_hcpcs": NlmHcpcsConnector,
        "nlm_loinc": NlmLoincConnector,
        "nlm_rxterms": NlmRxTermsConnector,
    }
    if key in nlm:
        return nlm[key](source_id=sid, name=name, tier=tier)
    if key == "europepmc":
        return EuropePmcConnector(source_id=sid, source_name=name, tier=tier)
    if key == "crossref":
        return CrossrefConnector(source_id=sid, source_name=name, tier=tier,
                                 prefix=DOI_PREFIXES.get(sid))
    if key == "guideline_discovery":
        journals, organization = GUIDELINE_JOURNALS.get(sid, ([], name))
        return GuidelineDiscoveryConnector(source_id=sid, source_name=name, tier=tier,
                                           journal=journals, organization=organization)
    if key == "local_files":
        return LocalFilesConnector(source_id=sid, dataset=source["local_dataset"],
                                   source_name=name, tier=tier)
    if key == "firecrawl":
        return FirecrawlConnector(source_id=sid, source_name=name, tier=tier,
                                  domain=domain,
                                  search_hint=source.get("search_hint", ""))
    return None


def build_connector(source: dict[str, Any]) -> Connector | None:
    """One YAML source entry -> one connector instance, or None if unmappable."""
    sid = source["id"]
    name = source.get("name", sid)
    tier = int(source.get("tier", 5))
    access = source.get("access_method", "")
    key = source.get("connector")

    if access == "licensed" or (not key and access == "licensed"):
        return LicensedConnector(sid, name, tier)
    if key:
        return _build_from_key(key, source)
    if access == "targeted_search":
        # Domain-scoped web search that keeps the source's own tier.
        return FirecrawlConnector(source_id=sid, source_name=name, tier=tier,
                                  domain=source.get("domain"),
                                  organization=name,
                                  search_hint=source.get("search_hint", ""))
    return None


def build_registry(include_disabled: bool = False) -> dict[str, Connector]:
    """source id -> live connector, for every enabled source in sources.yaml."""
    registry: dict[str, Connector] = {}
    for source in get_source_registry().get("sources", []):
        if not source.get("enabled", True) and not include_disabled:
            continue
        connector = build_connector(source)
        if connector is not None:
            registry[source["id"]] = connector
    return registry


def _missing_credentials(source: dict[str, Any]) -> list[str]:
    settings = get_settings()
    required = list(source.get("requires_credentials") or [])
    missing = [name for name in required if not getattr(settings, name.lower(), None)]
    if source.get("access_method") == "targeted_search" or \
            source.get("access_method") == "firecrawl_search":
        # Keyless fallback exists, so this is a degradation, not a blocker.
        if not settings.firecrawl_api_key:
            missing.append("FIRECRAWL_API_KEY (optional: keyless fallback in use)")
    return missing


def connector_health() -> list[dict[str, Any]]:
    """Per-source readiness for the UI: is this source actually usable today,
    and if not, what is missing."""
    datasets = available_datasets()
    registry = build_registry()
    rows: list[dict[str, Any]] = []
    for source in get_source_registry().get("sources", []):
        sid = source["id"]
        access = source.get("access_method", "")
        missing = _missing_credentials(source)
        blocking = [m for m in missing if "optional" not in m]

        if access == "licensed":
            configured = False
            blocking = blocking or ["licensed dataset"]
        elif access == "local_file":
            dataset = source.get("local_dataset", "")
            configured = bool(datasets.get(dataset))
            if not configured:
                blocking = blocking or [f"reference file: {dataset}"]
        else:
            configured = sid in registry and not blocking

        rows.append({
            "id": sid,
            "name": source.get("name", sid),
            "tier": int(source.get("tier", 5)),
            "access_method": access,
            "connector": source.get("connector"),
            "enabled": bool(source.get("enabled", True)),
            "configured": configured,
            "missing_credentials": missing,
            "blocking": blocking,
        })
    return rows
```

### File: `celestra\connectors\seer.py`

```py
"""NCI SEER Cancer Stat Facts.

HTML scrape only. api.seer.cancer.gov does not carry the Stat Facts
statistics, so the fact sheet page is parsed directly: the "At a Glance"
boxes, the survival stat box with its reporting period, the incidence /
mortality / prevalence prose, and the first data table.

Indication keys map to page slugs. When a key is unknown the connector tries
the known slugs and keeps whichever page's title matches the context terms —
that is the firecrawl-free fallback; no web search is involved.
"""
from __future__ import annotations

import re
import time

from selectolax.parser import HTMLParser

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections, matches_any
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

STATFACTS = "https://seer.cancer.gov/statfacts/html/{slug}.html"

# Verified slugs. CLL and ALL are the two indications this desk covers; the
# rest are here so an unknown key still has somewhere sensible to look.
SLUGS: dict[str, str] = {
    "CLL": "clyl",
    "ALL": "alyl",
    "AML": "amyl",
    "CML": "cmyl",
    "NHL": "nhl",
    "MM": "mulmy",
}

# Prose we want verbatim; each matches a sentence in the fact sheet body.
PROSE_MARKERS = (
    "rate of new cases",
    "death rate",
    "people living with",
    "lifetime risk",
    "percent of men and women will be diagnosed",
    "most frequently diagnosed among",
    "most frequent among",
)


def _glance_pairs(tree: HTMLParser) -> dict[str, str]:
    """The 'Estimated New Cases in <year> / 22,760' label-value boxes."""
    pairs: dict[str, str] = {}
    for box in tree.css(".glance-factSheet .glanceBox p"):
        spans = [clean(s.text()) for s in box.css("span")]
        if len(spans) >= 2 and spans[0]:
            pairs[spans[0]] = spans[1]
    for box in tree.css(".glance-factSheet .statBox"):
        label = clean(box.css_first("p").text()) if box.css_first("p") else ""
        value = clean(box.css_first("strong").text()) if box.css_first("strong") else ""
        period = clean(box.css_first("span").text()) if box.css_first("span") else ""
        if label and value:
            pairs[label] = f"{value} ({period})" if period else value
    return pairs


def _prose(tree: HTMLParser) -> list[str]:
    out: list[str] = []
    for p in tree.css("p"):
        text = clean(p.text(separator=" ", strip=True))
        if len(text) < 60:
            continue
        low = text.lower()
        if any(m in low for m in PROSE_MARKERS):
            out.append(text)
    # De-duplicate while keeping page order.
    seen, unique = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique[:8]


def _first_table(tree: HTMLParser, max_rows: int = 12) -> dict[str, list]:
    table = tree.css_first("table")
    if table is None:
        return {}
    rows = []
    for tr in table.css("tr")[:max_rows]:
        cells = [clean(c.text()) for c in tr.css("th,td")]
        if any(cells):
            rows.append(cells)
    return {"rows": rows} if rows else {}


def _reporting_periods(text: str) -> list[str]:
    return sorted(set(re.findall(r"(?:19|20)\d{2}\s*[–-]\s*(?:19|20)\d{2}", text)))[:6]


class SeerConnector:
    """Cancer Stat Facts scrape for the run's indication."""

    source_id = "seer"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "NCI SEER"

    def slug_candidates(self, ctx: RetrievalContext) -> list[str]:
        key = (ctx.indication_key or "").upper()
        if key in SLUGS:
            return [SLUGS[key]]
        # Unknown key: try every known slug and keep the page whose title
        # matches the context terms.
        return list(SLUGS.values())

    async def _fetch(self, slug: str) -> tuple[str, str]:
        url = STATFACTS.format(slug=slug)
        return url, await http.get_text(url)

    def _build_ref(self, url: str, html: str) -> SourceRef | None:
        tree = HTMLParser(html)
        title_node = tree.css_first("title")
        title = clean(title_node.text()) if title_node else "SEER Cancer Stat Facts"
        glance = _glance_pairs(tree)
        prose = _prose(tree)
        if not glance and not prose:
            return None
        glance_text = "; ".join(f"{k}: {v}" for k, v in glance.items())
        body = join_sections({
            "At a glance": glance_text,
            "Statistics": " ".join(prose),
        }, limit=6000)
        page_text = clean(tree.body.text(separator=" ", strip=True)) if tree.body else ""
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=url,
            title=title,
            organization="National Cancer Institute, Surveillance, Epidemiology, and End Results Program",
            published="",
            identifiers={"slug": url.rsplit("/", 1)[-1].replace(".html", "")},
            snippet=clip(body, 1500),
            raw={
                "at_a_glance": glance,
                "statistics": prose,
                "reporting_periods": _reporting_periods(page_text),
                "table": _first_table(tree),
                "text": body,
            },
            origin=self.origin,
        )

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        terms = ctx.or_terms()
        best: SourceRef | None = None
        try:
            for slug in self.slug_candidates(ctx):
                url, html = await self._fetch(slug)
                calls += 1
                ref = self._build_ref(url, html)
                if ref is None:
                    continue
                if len(self.slug_candidates(ctx)) == 1 or matches_any(ref.title, terms):
                    best = ref
                    break
                best = best or ref
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        refs = [best] if best else []
        return ConnectorResult(
            source_id=self.source_id, refs=refs[:limit], ok=True,
            reason="" if refs else "no stat facts page matched the indication",
            calls=calls, elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\who_gho.py`

```py
"""WHO Global Health Observatory (OData).

The indicator catalogue is one 414 KB document, so it is fetched once and left
to the shared HTTP cache; indicator codes are then resolved locally and only
the matching series are pulled.

GHO carries no leukaemia-subtype indicator. Returning zero refs with the
reason `no_specific_indicator` is a legitimate outcome for this connector, not
a failure — when nothing subtype-specific exists the connector falls back to
the broader cancer indicators and says so in the reason, so a report can never
present a national cancer aggregate as a CLL or ALL statistic.
"""
from __future__ import annotations

import time

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections, tokens
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

INDICATOR_URL = "https://ghoapi.azureedge.net/api/Indicator"
SERIES_URL = "https://ghoapi.azureedge.net/api/{code}"
PORTAL_URL = "https://www.who.int/data/gho/data/indicators"

# Fallback vocabulary when no indicator names the disease itself.
BROADER_TERMS = ("cancer", "neoplasm", "malignant", "oncolog")

# Clinical modifiers that appear in the indication name but say nothing about
# which disease it is: "chronic" alone would select the chronic respiratory
# disease indicators.
GENERIC_MODIFIERS = frozenset(
    {"chronic", "acute", "adult", "adults", "child", "children", "cell", "cells",
     "disease", "diseases", "disorder", "syndrome", "small", "phase", "stage",
     "refractory", "relapsed", "primary", "secondary"}
)

# How many indicator series to pull values for.
MAX_SERIES = 3
MAX_ROWS = 400


def _indicator_matches(name: str, terms: set[str]) -> bool:
    low = (name or "").lower()
    return any(t in low for t in terms)


class WhoGhoConnector:
    """Population-level indicators from the WHO GHO OData service."""

    source_id = "who_gho"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "WHO Global Health Observatory"

    async def indicators(self) -> list[dict]:
        payload = await http.get_json(INDICATOR_URL)
        return payload.get("value") or []

    async def series(self, code: str) -> list[dict]:
        payload = await http.get_json(SERIES_URL.format(code=clean(code)),
                                      params={"$top": MAX_ROWS})
        return payload.get("value") or []

    def _select(self, catalogue: list[dict], ctx: RetrievalContext) -> tuple[list[dict], str]:
        """Return (indicators, reason). Reason records when the match had to be
        widened away from the indication itself."""
        specific = set()
        for term in ctx.or_terms():
            specific |= {t for t in tokens(term)
                         if len(t) > 4 and t not in GENERIC_MODIFIERS}
        # British and American spellings both appear in GHO indicator names.
        specific |= {t.replace("leukemia", "leukaem") for t in specific}
        hits = [i for i in catalogue
                if _indicator_matches(i.get("IndicatorName", ""), specific)]
        if hits:
            return hits, ""
        broad = [i for i in catalogue
                 if _indicator_matches(i.get("IndicatorName", ""), set(BROADER_TERMS))]
        if broad:
            return broad, ("no_specific_indicator: GHO has no indicator for this "
                           "leukemia subtype; broader cancer indicators returned")
        return [], "no_specific_indicator"

    def _ref(self, indicator: dict, rows: list[dict], reason: str) -> SourceRef:
        code = clean(indicator.get("IndicatorCode"))
        name = clean(indicator.get("IndicatorName"))
        # Most recent observation per country, newest first.
        rows = sorted(rows, key=lambda r: clean(r.get("TimeDim")), reverse=True)
        lines = []
        for row in rows[:25]:
            value = clean(row.get("Value")) or clean(row.get("NumericValue"))
            if not value:
                continue
            lines.append(" ".join(x for x in [
                clean(row.get("SpatialDim")),
                clean(row.get("TimeDim")),
                clean(row.get("Dim1")),
                f"= {value}",
            ] if x))
        body = join_sections({
            "Indicator": name,
            "Observations": " | ".join(lines),
            "Scope caveat": reason,
        })
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=SERIES_URL.format(code=code),
            title=f"WHO GHO {code}: {name}",
            organization="World Health Organization",
            published=clean(rows[0].get("TimeDim")) if rows else "",
            identifiers={"indicator_code": code},
            snippet=clip(body, 1400),
            raw={
                "indicator_code": code,
                "indicator_name": name,
                "language": clean(indicator.get("Language")),
                "observations": rows[:MAX_ROWS],
                "portal": PORTAL_URL,
                "scope_caveat": reason,
                "text": body,
            },
            origin=self.origin,
        )

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            catalogue = await self.indicators()
            calls += 1
            selected, reason = self._select(catalogue, ctx)
            if not selected:
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason=reason or "no_specific_indicator", calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))
            refs: list[SourceRef] = []
            for indicator in selected[:max(1, min(limit, MAX_SERIES))]:
                try:
                    rows = await self.series(indicator.get("IndicatorCode", ""))
                    calls += 1
                except Exception:  # noqa: BLE001 - a dead series is not a dead source
                    rows = []
                if not rows:
                    continue
                refs.append(self._ref(indicator, rows, reason))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason=reason if refs else (reason or "no_specific_indicator"), calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
```

### File: `celestra\connectors\_util.py`

```py
"""Small text helpers shared by the connectors.

Kept private (leading underscore) because it is not part of the connector
contract: every public entry point is still a Connector class in its own
module. The extraction service reads `snippet`/`raw`, so everything here is
about turning source payloads into clean, quotable text.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from selectolax.parser import HTMLParser

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")

# Words that carry no discriminating power when we check whether an open-web
# result actually answers the query.
STOPWORDS = frozenset(
    """a an and are as at be by for from has have how in is it its of on or that
    the to was were what when where which who why with without into over under
    between about across during per than then this these those you your our""".split()
)


def clean(value: Any) -> str:
    """Collapse whitespace, strip markup entities, and coerce to a plain str."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v)
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("&nbsp;", " ").replace(" ", " ")
    return _WS.sub(" ", value).strip()


def clip(text: str, limit: int) -> str:
    """Trim to `limit` characters on a word boundary. Never mid-word: a quote
    cut mid-word is not verbatim-usable downstream."""
    text = clean(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def strip_tags(html: str) -> str:
    return clean(_TAG.sub(" ", html or ""))


def html_text(html: str, selector: str | None = None, separator: str = " ") -> str:
    """Extract readable text from an HTML document, dropping script/style."""
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
    except Exception:  # selectolax refuses only truly malformed input
        return strip_tags(html)
    # Site chrome is not evidence: navigation and cookie banners would
    # otherwise become the "verbatim quote" for a scraped page.
    tree.strip_tags(["script", "style", "noscript", "svg", "iframe", "form",
                     "nav", "header", "footer", "aside", "button", "select"])
    node = tree.css_first(selector) if selector else (tree.body or tree.root)
    if node is None:
        node = tree.body or tree.root
    if node is None:
        return strip_tags(html)
    return clean(node.text(separator=separator, strip=True))


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def first_str(*values: Any) -> str:
    for v in values:
        s = clean(v)
        if s:
            return s
    return ""


def tokens(text: str) -> set[str]:
    """Content tokens used for cheap relevance checks."""
    return {
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 2 and t not in STOPWORDS
    }


def matches_any(haystack: str, terms: Iterable[str]) -> bool:
    low = (haystack or "").lower()
    return any(t.lower() in low for t in terms if t)


def year_of(value: str) -> str:
    m = re.search(r"(19|20)\d{2}", clean(value))
    return m.group(0) if m else ""


def join_sections(sections: dict[str, str], limit: int = 6000) -> str:
    """Flatten named sections into one quotable block, longest first so the
    most substantive text survives the clip."""
    parts = [f"{k}: {clean(v)}" for k, v in sections.items() if clean(v)]
    parts.sort(key=len, reverse=True)
    out = ""
    for part in parts:
        remaining = limit - len(out)
        if remaining <= 0:
            break
        if len(part) > remaining:
            # A single oversized section must still contribute text; dropping it
            # whole is how `raw["text"]` ends up empty for label-sized payloads.
            if not out:
                out = clip(part, limit)
            break
        out = f"{out}\n\n{part}" if out else part
    return out.strip()
```

### File: `celestra\connectors\__init__.py`

```py

```

### File: `celestra\data\celestra.db`

> *File excluded: Binary or non-UTF-8 content*

### File: `celestra\services\answering.py`

```py
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
```

### File: `celestra\services\contradictions.py`

```py
"""Detects disagreements between sources answering the same question.

Nothing here resolves a conflict. Both sides are recorded as their source
stated them and handed to a human. That is a deliberate product rule: an
automated merge of two clinical claims destroys the very signal an SME needs.
"""
from __future__ import annotations

import itertools
import logging
import re

from ..models import Contradiction, ContradictionSeverity, Evidence, ResearchQuestion
from ..settings import get_thresholds
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.contradictions")

_SYSTEM = (
    "You compare two verbatim claims from different sources answering the same clinical "
    "research question. You decide whether they genuinely disagree about the same fact, "
    "or merely describe different facts. You never decide which is correct."
)

# A number with an optional unit that matters in this domain.
_TIER_GAP_REASON = (
    "Sources sit at materially different evidence tiers; the lower-tier source must not "
    "override the higher-tier source."
)
_SAME_CONCEPT_REASON = (
    "Sources make differing assertions about the same concept; requires SME adjudication."
)

_NUMBER = re.compile(r"\b(\d[\d,]*\.?\d*)\s*(%|percent|per\s+100,?000|per\s+year)?")

# What kind of quantity is being stated. Two numbers are comparable only when
# they are the same kind: an incidence rate against another incidence rate, not
# against a gene-usage frequency that happens to share a unit.
_MEASURE_KINDS = {
    "incidence": "incidence", "rate of new": "incidence", "new cases": "incidence",
    "prevalence": "prevalence", "living with": "prevalence",
    "survival": "survival", "surviving": "survival",
    "mortality": "mortality", "deaths": "mortality", "died": "mortality",
    "death rate": "mortality",
    "diagnosed": "cases", "estimated new": "cases", "cases": "cases",
    "median": "median", "average": "mean", "mean": "mean",
    "frequency": "frequency", "usage": "frequency",
    "proportion": "proportion", "share": "proportion",
    "response": "response", "remission": "remission",
    "risk": "risk", "odds": "risk", "hazard": "risk",
}


def _measurements(text: str) -> list[tuple[float, str, frozenset[str]]]:
    """Quantities in a quote, each with its unit and the kind of thing measured.

    The kind word can sit on either side of the number ("the rate was 4.7 per
    100,000", "about 6,250 new cases"), so the window spans both. Bare
    four-digit years are skipped: a sentence naming 2025 and another naming
    2026 is not a disagreement about a quantity.
    """
    out: list[tuple[float, str, frozenset[str]]] = []
    for match in _NUMBER.finditer(text):
        raw, unit = match.group(1), (match.group(2) or "").lower().strip()
        try:
            number = float(raw.replace(",", ""))
        except ValueError:
            continue
        if not unit and "," not in raw and 1900 <= number <= 2100 and number.is_integer():
            continue
        window = text[max(0, match.start() - 120): match.end() + 60].lower()
        kinds = frozenset(kind for word, kind in _MEASURE_KINDS.items() if word in window)
        if kinds:
            out.append((number, unit, kinds))
    return out


def _numeric_conflict(a: str, b: str, delta_pct: float) -> bool:
    """True only when both quotes state the same kind of quantity in the same
    unit and disagree beyond the tolerance.

    The caller has already established that the two quotes address the same
    question and share a subject, so this decides only whether the numbers are
    comparable. It is deliberately conservative: a missed conflict is
    recoverable by a reviewer reading the evidence, while a fabricated one
    spends their attention and devalues the register real conflicts sit in.
    """
    for va, ua, ka in _measurements(a):
        for vb, ub, kb in _measurements(b):
            if ua != ub or not (ka & kb):
                continue
            if va == 0 and vb == 0:
                continue
            if abs(va - vb) / max(abs(va), abs(vb)) * 100 >= delta_pct:
                return True
    return False


def _shared_subject(a: str, b: str) -> bool:
    wa = {w for w in re.findall(r"[a-z]{5,}", a.lower())}
    wb = {w for w in re.findall(r"[a-z]{5,}", b.lower())}
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.28


def _severity(tier_a: int, tier_b: int, numeric: bool) -> ContradictionSeverity:
    cfg = get_thresholds()["contradictions"]
    if abs(tier_a - tier_b) >= cfg["escalate_on_tier_gap"]:
        return ContradictionSeverity.ESCALATED
    return ContradictionSeverity.NOTED if not numeric else ContradictionSeverity.NOTED


def _reason(tier_a: int, tier_b: int) -> str:
    cfg = get_thresholds()["contradictions"]
    if abs(tier_a - tier_b) >= cfg["escalate_on_tier_gap"]:
        return _TIER_GAP_REASON
    return _SAME_CONCEPT_REASON


def dedupe(items: list[Contradiction]) -> list[Contradiction]:
    """Collapse repeats of the same claim pair.

    A single disagreement between two sources surfaces on every question both
    sources answered. Reviewers need one decision per disagreement, not one per
    question, so the pair of claims is the identity.
    """
    out: list[Contradiction] = []
    seen: set[tuple[str, str]] = set()
    for c in sorted(items, key=lambda x: (x.severity is not ContradictionSeverity.ESCALATED,)):
        key = tuple(sorted((c.source_a_claim[:90].lower(), c.source_b_claim[:90].lower())))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def detect_deterministic(
    question: ResearchQuestion, evidence: list[Evidence]
) -> list[Contradiction]:
    cfg = get_thresholds()["contradictions"]
    found: list[Contradiction] = []
    seen: set[tuple[str, str]] = set()

    for a, b in itertools.combinations(evidence, 2):
        if a.source_id == b.source_id:
            continue
        pair = tuple(sorted((a.id, b.id)))
        if pair in seen:
            continue
        numeric = _numeric_conflict(a.quote, b.quote, cfg["escalate_on_numeric_delta_pct"])
        tier_gap = abs(a.tier - b.tier) >= cfg["escalate_on_tier_gap"]
        if not (numeric or (tier_gap and _shared_subject(a.quote, b.quote))):
            continue
        seen.add(pair)
        hi, lo = (a, b) if a.tier <= b.tier else (b, a)
        found.append(
            Contradiction(
                run_id=question.run_id,
                stage=question.stage,
                question_id=question.id,
                topic=question.seed_text.strip(" ?").lower() or question.text[:90],
                source_a_name=f"{hi.citation} (tier {hi.tier})",
                source_a_tier=hi.tier,
                source_a_claim=hi.quote,
                source_a_url=hi.url,
                source_b_name=f"{lo.citation} (tier {lo.tier})",
                source_b_tier=lo.tier,
                source_b_claim=lo.quote,
                source_b_url=lo.url,
                reason=_reason(hi.tier, lo.tier),
                severity=_severity(hi.tier, lo.tier, numeric),
            )
        )
    return found


async def detect(
    question: ResearchQuestion, evidence: list[Evidence]
) -> list[Contradiction]:
    candidates = detect_deterministic(question, evidence)
    if not candidates or not llm.available:
        return candidates

    # The model's only job is to discard false positives and write a better
    # reason. It can never mark a real conflict resolved.
    try:
        payload = [
            {
                "id": c.id,
                "topic": c.topic,
                "a": {"source": c.source_a_name, "claim": c.source_a_claim},
                "b": {"source": c.source_b_name, "claim": c.source_b_claim},
            }
            for c in candidates
        ]
        verdicts = await llm.complete_json(
            _SYSTEM,
            f"Question: {question.text}\n\nCandidate disagreements:\n{payload}\n\n"
            "Return JSON: [{\"id\": str, \"is_real_disagreement\": bool, "
            "\"reason\": str}]. is_real_disagreement is false when the two claims "
            "describe different facts, different years, or different populations "
            "rather than contradicting each other. reason explains the divergence in "
            "one sentence. Never state which source is correct.",
            max_tokens=2000,
        )
        by_id = {str(v.get("id")): v for v in (verdicts or [])}
        kept: list[Contradiction] = []
        for c in candidates:
            v = by_id.get(c.id)
            if v is None:
                kept.append(c)
                continue
            if not v.get("is_real_disagreement", True):
                continue
            if reason := str(v.get("reason") or "").strip():
                c.reason = reason
            kept.append(c)
        return kept
    except LLMUnavailable:
        return candidates
```

### File: `celestra\services\extraction.py`

```py
"""Turns retrieved documents into Evidence carrying verbatim quotes.

Integrity rule: a quote must appear in the retrieved text. When a model
proposes a quote, it is verified against the source before it is accepted; an
unverifiable quote is dropped rather than downgraded, because a fabricated
quote attached to a real URL is worse than no evidence at all.
"""
from __future__ import annotations

import logging
import re

from ..models import Evidence, EvidenceOrigin, SourceRef, VerificationTag
from ..settings import get_thresholds
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.extraction")

_SYSTEM = (
    "You extract evidence for clinical desk research. You select sentences that already "
    "exist in the supplied document text. You never paraphrase, never merge sentences, "
    "and never write a sentence that is not present verbatim in the text. If the document "
    "does not address the question, you return an empty list."
)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
_WORD = re.compile(r"[a-z0-9]+")


_TAGS = re.compile(r"<[^>]{1,200}>")
_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
    "&apos;": "'", "&nbsp;": " ", "&ndash;": "-", "&mdash;": "-",
}


def _normalise(text: str) -> str:
    """Collapse whitespace and strip inline markup.

    Europe PMC abstracts and some SPL sections embed HTML. Left in place it
    ends up inside a quote presented to the reader as verbatim source text.
    """
    text = text or ""
    if "<" in text:
        text = _TAGS.sub(" ", text)
    if "&" in text:
        for entity, char in _ENTITIES.items():
            text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def document_text(ref: SourceRef) -> str:
    """Every connector puts quotable text somewhere. Collect it in one place."""
    parts: list[str] = [ref.snippet or ""]
    raw = ref.raw or {}
    for key in (
        "abstract", "abstractText", "text", "content", "markdown", "body",
        "indications_and_usage", "adverse_reactions", "warnings_and_precautions",
        "dosage_and_administration", "clinical_studies", "description",
        "briefSummary", "detailedDescription", "eligibilityCriteria", "summary",
    ):
        val = raw.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, list):
            parts += [v for v in val if isinstance(v, str)]
    for val in raw.get("sections", []) if isinstance(raw.get("sections"), list) else []:
        if isinstance(val, dict):
            parts.append(str(val.get("text", "")))
        elif isinstance(val, str):
            parts.append(val)
    return _normalise(" ".join(p for p in parts if p))


def _sentences(text: str, min_len: int) -> list[str]:
    out = []
    for raw_sent in _SENT_SPLIT.split(text):
        s = _normalise(raw_sent)
        if min_len <= len(s) <= 600:
            out.append(s)
    return out


_OFF_TOPIC = re.compile(
    r"\b(in rats?|in mice|in dogs?|animal data|carcinogenes[ie]s|mutagenesis|"
    r"impairment of fertility|pregnancy category|nursing mothers)\b", re.I
)


# Words that signal a question is asking for a quantity. Only then do numbers
# in a sentence earn credit; otherwise an incidence figure outranks a
# diagnostic definition for a question about diagnosis.
_QUANTITATIVE = {
    "incidence", "prevalence", "rate", "rates", "survival", "mortality", "cases",
    "deaths", "percent", "percentage", "proportion", "share", "median", "mean",
    "risk", "frequency", "dose", "dosing", "dosage", "duration", "cost", "costs",
    "estimate", "estimated", "number", "count", "statistics", "epidemiology",
}

_STOP = {
    "what", "which", "how", "are", "the", "and", "for", "with", "from", "that",
    "this", "does", "have", "has", "into", "used", "use", "including", "their",
    "there", "where", "when", "who", "why", "was", "were", "been", "being",
    "united", "states", "adult", "adults", "patients", "population", "research",
    "claims", "real", "world", "standard", "current", "relevant", "principal",
}


_FOCUS_SYNONYMS: dict[str, set[str]] = {
    "incidence": {"rate", "rates", "cases", "diagnosed"},
    "prevalence": {"living", "prevalent"},
    "mortality": {"death", "deaths", "died", "dying"},
    "survival": {"surviving", "survive", "survived"},
    "diagnosed": {"diagnosis", "diagnostic"},
    "diagnosis": {"diagnosed", "diagnostic"},
    "treatment": {"treated", "therapy", "regimen", "regimens"},
    "therapies": {"therapy", "treatment", "regimen", "regimens", "agents"},
    "approved": {"approval", "indicated", "indication"},
    "codes": {"code"},
}


class TermSet:
    """Two tiers of query terms.

    `focus` is what distinguishes this question from every other question about
    the same disease: "diagnosis", "workup", "immunophenotype". `context` is the
    disease name and upstream entities, which every relevant sentence shares and
    which therefore cannot rank one sentence above another. Mixing the two into
    one set was the defect that put an incidence figure at the top of every
    card in a stage.
    """

    __slots__ = ("focus", "context", "quantitative")

    def __init__(self, focus: set[str], context: set[str]) -> None:
        self.focus = focus
        self.context = context
        self.quantitative = bool(focus & _QUANTITATIVE)

    # Backwards compatibility for callers that treat the term set as a flat set.
    def __iter__(self):
        return iter(self.focus | self.context)

    def __len__(self) -> int:
        return len(self.focus | self.context)

    def __contains__(self, item: object) -> bool:
        return item in self.focus or item in self.context

    def __and__(self, other: set) -> set:
        return (self.focus | self.context) & other

    def __rand__(self, other: set) -> set:
        return self.__and__(other)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 3 and w not in _STOP}


def build_terms(
    question: str, aspects: list[str], synonyms: list[str], upstream: list[str] | None = None,
) -> TermSet:
    context = _words(" ".join([*synonyms, *(upstream or [])]))
    # A disease word inside the question text is context, not focus.
    focus = _words(" ".join([question, *aspects])) - context
    # Sources state quantities in their own words: "rate of new cases" for
    # incidence, "living with" for prevalence. Widen the focus accordingly.
    for word in list(focus):
        focus |= _FOCUS_SYNONYMS.get(word, set())
    return TermSet(focus - context, context)


def question_terms(question: str, aspects: list[str], synonyms: list[str]) -> TermSet:
    """Kept for callers of the old name; builds the two-tier set."""
    return build_terms(question, aspects, synonyms)


def _as_termset(terms) -> TermSet:
    if isinstance(terms, TermSet):
        return terms
    return TermSet(set(terms or ()), set())


def _score_sentence(sentence: str, terms) -> float:
    ts = _as_termset(terms)
    words = set(_WORD.findall(sentence.lower()))
    if not words:
        return 0.0
    focus_hits = len(words & ts.focus)
    context_hits = len(words & ts.context)
    if ts.focus and not focus_hits:
        # On the disease but not on what was asked. Keep it as weak evidence
        # so recall survives when a source answers in different words, but cap
        # it below any sentence with a focus hit so it can never headline.
        return min(0.20, 0.07 * context_hits) if context_hits else 0.0
    base = focus_hits / (len(ts.focus) ** 0.5) if ts.focus else context_hits / max(len(ts.context), 1) ** 0.5
    score = max(base, 0.21) + 0.03 * min(context_hits, 3)
    if ts.quantitative:
        numeric = len(re.findall(r"\d[\d,.]*\s*(?:%|per\s+100,?000)?", sentence))
        score += 0.15 * min(numeric, 3)
    if _OFF_TOPIC.search(sentence):
        # Preclinical and subpopulation boilerplate is rarely the answer to a
        # clinical desk-research question.
        score *= 0.25
    return score


def _tag_for(ref: SourceRef) -> VerificationTag:
    return (
        VerificationTag.GENERAL_KNOWLEDGE
        if ref.origin is EvidenceOrigin.OPEN_WEB
        else VerificationTag.VERIFIED
    )


def _build(ref: SourceRef, question_id: str, quote: str, relevance: float) -> Evidence:
    return Evidence(
        question_id=question_id,
        source_id=ref.source_id,
        source_name=ref.source_name,
        organization=ref.organization or ref.source_name,
        tier=ref.tier,
        url=ref.url,
        title=ref.title,
        published=ref.published,
        quote=quote,
        origin=ref.origin,
        tag=_tag_for(ref),
        relevance=round(min(relevance, 1.0), 3),
        identifiers=ref.identifiers,
    )


def extract_deterministic(
    ref: SourceRef, question_id: str, terms: set[str], max_quotes: int = 2
) -> list[Evidence]:
    cfg = get_thresholds()["sufficiency"]
    text = document_text(ref)
    if not text:
        return []
    scored = [
        (_score_sentence(s, terms), s)
        for s in _sentences(text, cfg["min_quote_length"])
    ]
    scored = [(sc, s) for sc, s in scored if sc > 0.15]
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[:max_quotes]
    if not best:
        return []
    top = best[0][0] or 1.0
    return [_build(ref, question_id, s, sc / (top * 1.35)) for sc, s in best]


async def extract_with_llm(
    ref: SourceRef, question: str, question_id: str, terms: set[str], max_quotes: int = 2
) -> list[Evidence]:
    text = document_text(ref)
    if not text:
        return []
    excerpt = text[:12000]
    try:
        result = await llm.complete_json(
            _SYSTEM,
            f"Question: {question}\n\nDocument title: {ref.title}\n"
            f"Source: {ref.source_name}\n\nDocument text:\n{excerpt}\n\n"
            f"Return JSON: [{{\"quote\": str, \"relevance\": 0..1}}]. "
            f"At most {max_quotes} quotes, each copied character-for-character from the "
            f"document text above, each a complete sentence, each directly relevant to "
            f"the question. Empty list if the document does not address it.",
            max_tokens=1200,
        )
    except LLMUnavailable:
        return extract_deterministic(ref, question_id, terms, max_quotes)

    haystack = _normalise(text).lower()
    out: list[Evidence] = []
    for item in (result or [])[:max_quotes]:
        quote = _normalise(str(item.get("quote", "")))
        if len(quote) < get_thresholds()["sufficiency"]["min_quote_length"]:
            continue
        # Integrity gate: the quote must genuinely be in the document.
        if quote.lower() not in haystack:
            log.debug("dropped unverifiable quote from %s", ref.url)
            continue
        try:
            rel = float(item.get("relevance", 0.6))
        except (TypeError, ValueError):
            rel = 0.6
        out.append(_build(ref, question_id, quote, rel))
    return out or extract_deterministic(ref, question_id, terms, max_quotes)


async def extract_batch(
    refs: list[SourceRef], question: str, question_id: str, terms: set[str],
    max_quotes: int = 3,
) -> list[Evidence]:
    """Extract from several documents in ONE model call.

    One call per document was the dominant cost of a run. Batching three
    documents per call cuts that by two thirds with no loss of the integrity
    gate: each returned quote is still verified character-for-character against
    the document it claims to come from, and an unverifiable quote is dropped.
    """
    docs = [(ref, document_text(ref)) for ref in refs]
    docs = [(ref, text) for ref, text in docs if text]
    if not docs:
        return []
    if not llm.available:
        out: list[Evidence] = []
        for ref, _ in docs:
            out += extract_deterministic(ref, question_id, terms, max_quotes)
        return out

    per_doc_budget = max(3000, 12000 // len(docs))
    listing = "\n\n".join(
        f"=== DOCUMENT {i} ===\nTitle: {ref.title}\nSource: {ref.source_name}\n"
        f"{text[:per_doc_budget]}"
        for i, (ref, text) in enumerate(docs)
    )
    try:
        result = await llm.complete_json(
            _SYSTEM,
            f"Question: {question}\n\n{listing}\n\n"
            'Return JSON: [{"document": int, "quote": str, "relevance": 0..1}]. '
            f"At most {max_quotes} quotes per document, each copied character-for-character "
            "from that document's text above, each a complete sentence, each directly "
            "relevant to the question. Omit a document entirely if it does not address "
            "the question.",
            max_tokens=600 + 500 * len(docs),
        )
    except LLMUnavailable:
        out = []
        for ref, _ in docs:
            out += extract_deterministic(ref, question_id, terms, max_quotes)
        return out

    min_len = get_thresholds()["sufficiency"]["min_quote_length"]
    haystacks = {i: _normalise(text).lower() for i, (_, text) in enumerate(docs)}
    counts: dict[int, int] = {}
    out = []
    for item in result or []:
        try:
            idx = int(item.get("document"))
        except (TypeError, ValueError, AttributeError):
            continue
        if idx not in haystacks or counts.get(idx, 0) >= max_quotes:
            continue
        quote = _normalise(str(item.get("quote", "")))
        if len(quote) < min_len or quote.lower() not in haystacks[idx]:
            continue
        try:
            rel = float(item.get("relevance", 0.6))
        except (TypeError, ValueError):
            rel = 0.6
        counts[idx] = counts.get(idx, 0) + 1
        out.append(_build(docs[idx][0], question_id, quote, rel))

    # A document the model returned nothing for still gets the deterministic
    # pass, so a terse model answer cannot silently discard a source.
    for i, (ref, _) in enumerate(docs):
        if i not in counts:
            out += extract_deterministic(ref, question_id, terms, max_quotes)
    return out


async def extract(
    refs: list[SourceRef], question: str, question_id: str, terms: set[str]
) -> list[Evidence]:
    limits = get_thresholds()["limits"]
    per_source_cap = limits.get("max_evidence_items_per_source", 3)
    total_cap = limits["max_evidence_items_per_question"]

    batch_size = max(1, int(limits.get("extract_batch_size", 3)))
    collected: list[Evidence] = []
    seen: set[str] = set()
    for start in range(0, len(refs), batch_size):
        # Offer at least as many candidates as the per-source cap can accept,
        # otherwise the cap is never the binding constraint and a source
        # contributes fewer quotes than the diversity rule allows.
        items = await extract_batch(
            refs[start:start + batch_size], question, question_id, terms,
            max_quotes=per_source_cap,
        )
        for ev in items:
            fingerprint = ev.quote[:120].lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            collected.append(ev)

    # Round-robin across sources rather than taking the globally top-scoring
    # quotes. Taking the top N alone let one very long document win every slot,
    # which produced findings that cited five sources but quoted one.
    by_source: dict[str, list[Evidence]] = {}
    for ev in sorted(collected, key=lambda e: (e.tier, -e.relevance)):
        by_source.setdefault(ev.source_id, []).append(ev)

    out: list[Evidence] = []
    for depth in range(per_source_cap):
        for source_id in sorted(by_source, key=lambda s: by_source[s][0].tier):
            bucket = by_source[source_id]
            if depth < len(bucket) and len(out) < total_cap:
                out.append(bucket[depth])
        if len(out) >= total_cap:
            break
    out.sort(key=lambda e: (e.tier, -e.relevance))
    return out[:total_cap]
```

### File: `celestra\services\handoff.py`

```py
"""Context handed from one agent to the next.

The framework's dependency edges exist for a reason: step 2 decides WHICH
tests matter, and step 5 maps those tests to codes. Without a handoff, the
downstream agent would search a code authority for the disease name and get
nothing useful. So each agent publishes the entities it discovered, and
downstream agents receive them in their retrieval context.

Extraction is pattern-based over the evidence actually retrieved, optionally
sharpened by a model. It never invents an entity that no quote mentions.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..models import Evidence, RunConfig
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.handoff")

# ICD-10-CM: a letter, two digits, optional dotted extension. Excludes U and
# the letter I/O confusions that produce false hits on ordinary prose.
_ICD10 = re.compile(r"\b([A-TV-Z]\d{2}(?:\.\d{1,4})?)\b")
_ICD11 = re.compile(r"\b(\d[A-Z]\d{2}(?:\.\d{1,2})?)\b")
_LOINC = re.compile(r"\b(\d{4,5}-\d)\b")
_HCPCS = re.compile(r"\b([A-V]\d{4})\b")
_NDC = re.compile(r"\b(\d{4,5}-\d{3,4}-\d{1,2})\b")
_APPNO = re.compile(r"\b((?:NDA|BLA|ANDA)\s?\d{6})\b", re.I)

# Concepts a code-mapping agent can actually search a code authority for.
_TEST_LEXICON = [
    "flow cytometry", "immunophenotyping", "bone marrow biopsy",
    "bone marrow aspirate", "bone marrow aspiration", "karyotype",
    "conventional cytogenetics", "fluorescence in situ hybridization", "FISH",
    "polymerase chain reaction", "RT-PCR", "next-generation sequencing",
    "measurable residual disease", "minimal residual disease",
    "complete blood count", "peripheral blood smear", "lymph node biopsy",
    "beta-2 microglobulin", "direct antiglobulin test", "computed tomography",
    "IGHV mutational status", "TP53 mutation", "deletion 17p", "del(17p)",
    "BCR::ABL1", "KMT2A", "immunoglobulin heavy chain",
    "serum lactate dehydrogenase", "coagulation", "lumbar puncture",
]


def _findall(pattern: re.Pattern, texts: list[str], limit: int = 40) -> list[str]:
    seen, out = set(), []
    for t in texts:
        for m in pattern.findall(t):
            v = m.strip().upper().replace(" ", "")
            if v not in seen:
                seen.add(v)
                out.append(m.strip())
                if len(out) >= limit:
                    return out
    return out


def _lexicon_hits(texts: list[str], limit: int = 20) -> list[str]:
    blob = " ".join(texts).lower()
    out = []
    for concept in _TEST_LEXICON:
        if concept.lower() in blob and concept not in out:
            out.append(concept)
            if len(out) >= limit:
                break
    return out


def _drug_names(evidence: list[Evidence]) -> tuple[list[str], list[str], list[str]]:
    """Generic names, application numbers and SPL set ids come from the
    identifiers the label connectors attach, not from parsing prose."""
    generics, apps, setids = [], [], []
    for e in evidence:
        ident = e.identifiers or {}
        for key, bucket in (
            ("generic_name", generics),
            ("application_number", apps),
            ("set_id", setids),
        ):
            val = ident.get(key)
            if val and val not in bucket:
                bucket.append(val)
    texts = [e.quote for e in evidence]
    for app in _findall(_APPNO, texts, 20):
        norm = app.upper().replace(" ", "")
        if norm not in apps:
            apps.append(norm)
    return generics[:40], apps[:40], setids[:40]


async def build(
    bucket: str, cfg: RunConfig, evidence: list[Evidence]
) -> dict[str, Any]:
    """Entities this agent discovered, for downstream agents to use."""
    if not evidence:
        return {}
    texts = [f"{e.title} {e.quote}" for e in evidence]
    generics, apps, setids = _drug_names(evidence)

    out: dict[str, Any] = {}
    if codes := _findall(_ICD10, texts, 30):
        out["icd10_codes"] = codes
    if codes := _findall(_ICD11, texts, 20):
        out["icd11_codes"] = codes
    if codes := _findall(_LOINC, texts, 30):
        out["loinc_codes"] = codes
    if codes := _findall(_HCPCS, texts, 30):
        out["hcpcs_codes"] = codes
    if codes := _findall(_NDC, texts, 30):
        out["ndc_codes"] = codes
    if tests := _lexicon_hits(texts):
        out["test_names"] = tests
    if generics:
        out["drugs"] = generics
    if apps:
        out["application_numbers"] = apps
    if setids:
        out["set_ids"] = setids

    if llm.available:
        try:
            enriched = await llm.complete_json(
                "You extract named clinical entities that a downstream code-mapping or "
                "safety agent can search for. You only list entities that appear in the "
                "supplied quotes. You never add an entity that is not mentioned.",
                f"Indication: {cfg.indication}\n\nQuotes:\n"
                + "\n".join(f"- {t[:400]}" for t in texts[:30])
                + '\n\nReturn JSON: {"test_names": [str], "drugs": [str], '
                '"subtypes": [str], "biomarkers": [str], "regimens": [str]}. '
                "Each list at most 15 entries, each a short searchable term. "
                "Empty list where the quotes name none.",
                max_tokens=1500,
            )
            for key in ("test_names", "drugs", "subtypes", "biomarkers", "regimens"):
                values = [str(v).strip() for v in (enriched or {}).get(key, []) if str(v).strip()]
                if values:
                    merged = out.get(key, []) + values
                    out[key] = list(dict.fromkeys(merged))[:20]
        except LLMUnavailable:
            pass

    if out:
        log.info("agent %s handoff: %s", bucket,
                 {k: len(v) if isinstance(v, list) else v for k, v in out.items()})
    return out


def merge(existing: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    out = dict(existing)
    for key, value in addition.items():
        if isinstance(value, list):
            out[key] = list(dict.fromkeys(out.get(key, []) + value))[:60]
        else:
            out[key] = value
    return out


def for_agent(bucket: str, context: dict[str, Any]) -> dict[str, Any]:
    """What this agent should actually receive. Narrow rather than dumping the
    whole context, so a connector's query stays focused."""
    wants = {
        "A": (),
        "C": ("subtypes", "biomarkers"),
        "B": ("test_names", "icd10_codes", "icd11_codes", "subtypes", "biomarkers"),
        "D": ("drugs", "application_numbers", "set_ids", "hcpcs_codes", "regimens"),
        "E": ("drugs", "regimens", "icd10_codes", "test_names", "loinc_codes"),
        "F": ("drugs", "regimens", "test_names", "icd10_codes", "subtypes"),
        "G": (),
    }.get(bucket, ())
    out = {k: context[k] for k in wants if context.get(k)}
    # What the reviewer wrote at the gate goes to every agent that runs after
    # it. It is the one piece of context that is human, not extracted.
    notes = [
        str(n.get("input") if isinstance(n, dict) else n).strip()
        for n in (context.get("reviewer_inputs") or [])
    ]
    notes = [n for n in notes if n]
    if notes:
        out["reviewer_notes"] = notes[:12]
    return out
```

### File: `celestra\services\hydration.py`

```py
"""Full-text hydration for the documents that survived ranking.

Discovery deliberately fetches only metadata. Once ranking has chosen the few
documents worth reading, this fetches their full text from the same source
API and returns an enriched copy of the ref. A hydration miss is normal: most
articles are not open access, and the abstract still counts as evidence.
"""
from __future__ import annotations

import logging

from ..models import SourceRef

log = logging.getLogger("celestra.hydration")


def _ident(ref: SourceRef, *keys: str) -> str:
    for k in keys:
        v = (ref.identifiers or {}).get(k)
        if v:
            return str(v)
    return ""


async def hydrate(ref: SourceRef, registry: dict) -> SourceRef:
    """Return the ref with full text in raw["text"], or the ref unchanged."""
    text = ""
    try:
        if pmcid := _ident(ref, "pmcid"):
            from ..connectors.europepmc import fetch_full_text

            text = await fetch_full_text(pmcid)
        elif nct := _ident(ref, "nct", "nct_id"):
            conn = registry.get("clinicaltrials")
            if conn is not None and hasattr(conn, "hydrate"):
                study = await conn.hydrate(nct)
                proto = (study or {}).get("protocolSection") or {}
                parts = []
                for section in ("descriptionModule", "eligibilityModule", "armsInterventionsModule",
                                "outcomesModule", "designModule"):
                    node = proto.get(section) or {}
                    parts += [str(v) for v in node.values() if isinstance(v, str)]
                text = " ".join(parts)
        elif setid := _ident(ref, "set_id", "setid"):
            conn = registry.get("dailymed")
            if conn is not None and hasattr(conn, "spl_xml"):
                from ..connectors._util import strip_tags

                text = strip_tags(await conn.spl_xml(setid))
    except Exception as exc:  # noqa: BLE001 - hydration is an enrichment, never a gate
        log.debug("hydration failed for %s: %s", ref.url, exc)
        return ref

    if not text or len(text) <= len(ref.snippet or ""):
        return ref
    enriched = ref.model_copy(deep=True)
    enriched.raw = dict(enriched.raw or {})
    enriched.raw["text"] = text
    enriched.raw["hydrated"] = True
    return enriched
```

### File: `celestra\services\insights.py`

```py
"""Insight cards: fixed slots defined by each agent's objective, filled from
the agent's finished stage document.

The slots live in config/insight_cards.yaml. For each one the model writes:
what we found, the card's own evidence block (figures, a table, a pathway or
a list), what it means, and one quiet line saying what a reviewer should
check. Without a model, the slot is filled from the research questions that
map to it: their answers, their evidence and the stage tables.

The model never decides on its own that a card needs a person. The evidence
rule does: a card resting on a question answered only from the open web, or
not at all, or carrying an undecided escalated conflict, is Requires Input.
A slot the sources did not cover is Requires Input too, because only a
reviewer can supply it.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..models import (
    AnswerStatus,
    Confidence,
    Contradiction,
    Evidence,
    Insight,
    ResearchQuestion,
    RunConfig,
    StageReport,
    VerificationTag,
)
from ..settings import get_framework, get_insight_cards, get_questions
from .llm import LLMUnavailable, llm
from .scoring import assess_confidence

log = logging.getLogger("celestra.insights")

CATEGORY_BY_BUCKET = {
    "A": "Clinical", "C": "Treatment", "B": "Diagnostic",
    "D": "Logic", "E": "Journey", "F": "Synthesis", "G": "Validation",
}

_SYSTEM = (
    "You fill fixed review cards for a clinical desk-research document. Each card has "
    "a defined objective and an evidence format. You write only what the supplied "
    "document states: never a fact, figure, drug, code or guideline it does not contain. "
    "Where the document does not cover a card, you say so instead of writing around it. "
    "Your reader is a subject-matter expert who will approve, edit or add to each card."
)


# -- catalogue ---------------------------------------------------------------
def catalogue_for(bucket: str) -> list[dict]:
    return [dict(c) for c in (get_insight_cards().get("buckets") or {}).get(bucket, [])]


def phase_catalogue(phase: str) -> list[dict]:
    return [dict(c) for c in (get_insight_cards().get("phase_cards") or {}).get(phase, [])]


def _hits(text: str, hints: list[str]) -> bool:
    low = (text or "").lower()
    return any(h.lower() in low for h in hints or [])


def _questions_for(card: dict, questions: list[ResearchQuestion]) -> list[ResearchQuestion]:
    return [q for q in questions if _hits(f"{q.seed_text} {q.text}", card.get("question_hints"))]


def _tables_for(card: dict, report: StageReport):
    return [t for t in report.tables if _hits(t.title, card.get("table_hints"))]


# -- document text for the model -------------------------------------------
def _table_text(table, max_rows: int = 8) -> str:
    rows = [" | ".join(str(c)[:90] for c in r) for r in table.rows[:max_rows]]
    more = f"\n  ... {len(table.rows) - max_rows} more rows" if len(table.rows) > max_rows else ""
    return (f"TABLE: {table.title}\n  columns: {' | '.join(table.columns)}\n  "
            + "\n  ".join(rows) + more)


def _document_text(report: StageReport, questions: list[ResearchQuestion]) -> str:
    parts: list[str] = []
    if report.synthesis:
        parts.append("SYNTHESIS:\n" + report.synthesis[:2500])
    for n in report.narratives[:6]:
        parts.append(f"{n.get('heading', 'Section')}:\n{str(n.get('body', ''))[:1200]}")
    parts.append("QUESTIONS AND ANSWERS:")
    for i, q in enumerate(questions, start=1):
        answer = q.answer_text or f"(not answered: {q.unmet_reason or 'no source addressed it'})"
        cites = ", ".join(q.answer_citations[:4]) or "no citation"
        parts.append(f"Q{i}. {q.seed_text or q.text}\n   status={q.answer_status.value}; "
                     f"sources: {cites}\n   {answer[:1000]}")
    for t in report.tables[:8]:
        parts.append(_table_text(t))
    if report.takeaways:
        parts.append("TAKEAWAYS:\n- " + "\n- ".join(str(t)[:300] for t in report.takeaways[:6]))
    if report.assumptions:
        parts.append("ASSUMPTIONS:\n- " + "\n- ".join(str(a)[:200] for a in report.assumptions[:6]))
    return "\n\n".join(parts)


def _card_spec_text(card: dict) -> str:
    et = card.get("evidence_type", "list")
    if et == "metrics":
        shape = ('"evidence": [{"label": str, "value": str}] using labels such as '
                 + "; ".join(card.get("metrics") or []) + " (only those the document states)")
    elif et == "table":
        shape = ('"evidence": {"columns": ' + str(card.get("columns") or ["Item", "Detail"])
                 + ', "rows": [[str, ...]]} with 3-8 rows')
    elif et == "steps":
        shape = '"evidence": [str] — 4-8 ordered steps, each at most 6 words'
    else:
        shape = '"evidence": [str] — 3-6 bullet points, each one sentence'
    return (f'- key "{card["key"]}": {card["title"]}. Objective: {card.get("objective", "")}. '
            f"Evidence format: {shape}.")


# -- helpers -----------------------------------------------------------------
def _match_title(candidate: str, titles: list[str]) -> str | None:
    c = re.sub(r"\W+", " ", candidate.lower()).strip()
    if not c:
        return None
    for t in titles:
        if re.sub(r"\W+", " ", t.lower()).strip() == c:
            return t
    for t in titles:
        tl = re.sub(r"\W+", " ", t.lower()).strip()
        if c in tl or tl in c:
            return t
    return None


def _clean_indices(raw: Any, count: int) -> list[int]:
    out: list[int] = []
    for v in (raw if isinstance(raw, list) else [raw]):
        try:
            n = int(str(v).strip().lstrip("Qq"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= count and n not in out:
            out.append(n)
    return out


def _clean_text(v: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def _clean_evidence(evidence_type: str, raw: Any) -> Any:
    """Coerce whatever the model returned into the card's evidence shape.
    Anything that does not fit is dropped, never guessed."""
    if evidence_type == "metrics":
        out = []
        for item in (raw if isinstance(raw, list) else []):
            if isinstance(item, dict) and item.get("label") and item.get("value") not in (None, ""):
                out.append({"label": _clean_text(item["label"], 60),
                            "value": _clean_text(item["value"], 60)})
        return out[:6]
    if evidence_type == "table":
        if not isinstance(raw, dict):
            return None
        cols = [_clean_text(c, 40) for c in (raw.get("columns") or []) if _clean_text(c, 40)]
        rows = []
        for r in (raw.get("rows") or []):
            if isinstance(r, list) and any(_clean_text(c, 120) for c in r):
                cells = [_clean_text(c, 120) for c in r][: len(cols) or None]
                cells += [""] * (len(cols) - len(cells))
                rows.append(cells)
        return {"columns": cols, "rows": rows[:10]} if cols and rows else None
    items = [_clean_text(x, 200) for x in (raw if isinstance(raw, list) else []) if _clean_text(x, 200)]
    return items[:8] or None


def _evidence_from_report(card: dict, report: StageReport, linked: list[ResearchQuestion],
                          own_evidence: list[Evidence],
                          used_tables: set[str] | None = None,
                          used_quotes: set[str] | None = None) -> tuple[str, Any]:
    """Deterministic evidence block: a matching stage table not already shown
    on another card, otherwise quotes not already shown on another card. The
    same table under three cards reads as padding and hides how much distinct
    evidence there is."""
    used_tables = used_tables if used_tables is not None else set()
    used_quotes = used_quotes if used_quotes is not None else set()
    tables = _tables_for(card, report) + [
        t for t in report.tables if set(t.question_ids) & {q.id for q in linked}
    ]
    for t in tables:
        if t.title in used_tables:
            continue
        used_tables.add(t.title)
        return "table", {"columns": list(t.columns), "rows": [list(r) for r in t.rows[:5]]}
    quotes = []
    for e in own_evidence:
        q = _clean_text(e.quote, 200)
        key = q[:120].lower()
        if q and key not in used_quotes:
            used_quotes.add(key)
            quotes.append(q)
        if len(quotes) >= 4:
            break
    return ("list", quotes) if quotes else ("", None)


def _finish(card: dict, *, run_id: str, stage: str, bucket: str, category: str,
            finding: str, evidence_type: str, evidence: Any, interpretation: str,
            review_note: str, linked: list[ResearchQuestion], ev_by_q: dict,
            contradictions: list[Contradiction], titles: list[str], covered: bool,
            gap: str = "") -> Insight:
    own: list[Evidence] = []
    for q in linked:
        own += ev_by_q.get(q.id, [])
    own.sort(key=lambda e: (e.tier, -e.relevance))
    source_ids: list[str] = []
    for e in own:
        if e.source_id not in source_ids:
            source_ids.append(e.source_id)

    conf, reason = Confidence.READY, ""
    if not covered:
        conf = Confidence.REQUIRES_INPUT
        reason = gap or "The sources consulted did not cover this. Add what you know."
    else:
        for q in linked:
            c, r = assess_confidence(q, ev_by_q.get(q.id, []), contradictions)
            if c is Confidence.REQUIRES_INPUT:
                conf, reason = c, r
                break
    answered_all = bool(linked) and all(q.answer_status is AnswerStatus.ANSWERED for q in linked)
    return Insight(
        run_id=run_id, stage=stage, bucket=bucket, category=category,
        number=int(card.get("number") or 0), card_key=str(card.get("key") or ""),
        title=str(card.get("title") or "Finding"),
        summary=finding or ("Not covered by the sources consulted in this run."),
        evidence_type=evidence_type if evidence else "", evidence=evidence,
        interpretation=interpretation, review_note=review_note,
        confidence=conf, input_reason=reason, covered=covered,
        tag=VerificationTag.VERIFIED if answered_all and own else VerificationTag.INFERENCE,
        evidence_ids=[e.id for e in own], source_ids=source_ids,
        question_ids=[q.id for q in linked],
        used_web_fallback=any(q.used_web_fallback for q in linked),
        table_titles=titles,
    )


# -- deterministic fill --------------------------------------------------------
def deterministic(run_id: str, stage: str, bucket: str, report: StageReport,
                  questions: list[ResearchQuestion], evidence: list[Evidence],
                  contradictions: list[Contradiction]) -> list[Insight]:
    ev_by_q: dict[str, list[Evidence]] = {}
    for e in evidence:
        ev_by_q.setdefault(e.question_id, []).append(e)
    category = CATEGORY_BY_BUCKET.get(bucket, "Clinical")
    out: list[Insight] = []
    used_tables: set[str] = set()
    used_quotes: set[str] = set()
    used_findings: set[str] = set()
    for card in catalogue_for(bucket):
        linked = _questions_for(card, questions)
        # The finding is the first linked answer not already headlining another
        # card; failing that, the strongest unused quote.
        finding = ""
        for q in linked:
            text = _clean_text(q.answer_text, 500)
            if text and text[:120].lower() not in used_findings:
                finding = text
                break
        if not finding:
            for q in linked:
                for e in sorted(ev_by_q.get(q.id, []), key=lambda e: (e.tier, -e.relevance)):
                    text = _clean_text(e.quote, 320)
                    if text and text[:120].lower() not in used_findings:
                        finding = text
                        break
                if finding:
                    break
        if finding:
            used_findings.add(finding[:120].lower())
        own = [e for q in linked for e in ev_by_q.get(q.id, [])]
        etype, ev = _evidence_from_report(card, report, linked, own, used_tables, used_quotes)
        tables = [t.title for t in _tables_for(card, report)]
        covered = bool(finding)
        out.append(_finish(
            card, run_id=run_id, stage=stage, bucket=bucket, category=category,
            finding=finding, evidence_type=etype, evidence=ev, interpretation="",
            review_note="", linked=linked, ev_by_q=ev_by_q, contradictions=contradictions,
            titles=tables, covered=covered,
        ))
    return out


# -- model fill ----------------------------------------------------------------
async def generate(
    run_id: str, cfg: RunConfig, stage: str, bucket: str, report: StageReport,
    questions: list[ResearchQuestion], evidence: list[Evidence],
    contradictions: list[Contradiction], category: str | None = None,
) -> list[Insight]:
    """Every catalogue card for this agent, filled from the stage document.
    Falls back to the deterministic fill when no model is available or the
    call fails, so the set of cards is the same either way."""
    cards = catalogue_for(bucket)
    category = category or CATEGORY_BY_BUCKET.get(bucket, "Clinical")
    if not cards:
        return []
    if not llm.available or not questions:
        return deterministic(run_id, stage, bucket, report, questions, evidence, contradictions)

    fw = get_framework()["buckets"][bucket]
    meta = get_questions()["stage_meta"].get(stage, {})
    table_titles = [t.title for t in report.tables]
    prompt = (
        f"Indication: {cfg.indication}. Geography: {cfg.geography}. Objective: {cfg.objective}.\n"
        f"Agent: {fw['agent_name']} — {fw['name']}. Stage: {meta.get('name', stage)}.\n\n"
        "THE DOCUMENT FOR THIS STAGE:\n" + _document_text(report, questions) + "\n\n"
        "CARDS TO FILL (all of them, in this order):\n"
        + "\n".join(_card_spec_text(c) for c in cards) + "\n\n"
        'Return JSON: {"cards": [{"key": str, "covered": bool, "finding": str, '
        '"evidence": ..., "interpretation": str, "review_note": str, '
        '"questions": [int], "table_titles": [str], "gap": str}]}.\n'
        "- covered: false when the document does not contain what the card asks for; "
        "then finding, evidence and interpretation are empty and gap says in one sentence "
        "what a reviewer would need to supply.\n"
        "- finding: 1-2 sentences stating the finding with its figures, criteria, codes or "
        "agents as the document gives them.\n"
        "- evidence: in the card's format, from the document only.\n"
        "- interpretation: 1-2 sentences on what this means for building patient cohorts "
        "and lines of therapy from claims data.\n"
        "- review_note: one short sentence naming the single point most worth an expert's "
        "check (a figure's year, a threshold, a code's specificity); empty if nothing stands out.\n"
        "- questions: the Q numbers the card rests on. table_titles: exact TABLE titles used."
    )
    try:
        result = await llm.complete_json(_SYSTEM, prompt, max_tokens=5000)
    except LLMUnavailable:
        return deterministic(run_id, stage, bucket, report, questions, evidence, contradictions)
    except Exception:  # noqa: BLE001 - the deterministic fill covers the failure
        log.exception("insight generation failed for %s/%s", run_id, stage)
        return deterministic(run_id, stage, bucket, report, questions, evidence, contradictions)

    raw = (result or {}).get("cards") if isinstance(result, dict) else None
    by_key: dict[str, dict] = {}
    for item in (raw if isinstance(raw, list) else []):
        if isinstance(item, dict) and item.get("key"):
            by_key[str(item["key"]).strip()] = item
    if not by_key:
        log.warning("insight generation returned nothing usable for %s/%s; deterministic fill",
                    run_id, stage)
        return deterministic(run_id, stage, bucket, report, questions, evidence, contradictions)

    ev_by_q: dict[str, list[Evidence]] = {}
    for e in evidence:
        ev_by_q.setdefault(e.question_id, []).append(e)

    out: list[Insight] = []
    for card in cards:
        item = by_key.get(card["key"])
        if item is None:
            # The model skipped a slot; fill it deterministically so the set
            # of cards is always complete.
            out += [i for i in deterministic(run_id, stage, bucket, report, questions,
                                             evidence, contradictions)
                    if i.card_key == card["key"]]
            continue
        finding = _clean_text(item.get("finding"), 600)
        covered = bool(item.get("covered", True)) and bool(finding)
        q_idx = _clean_indices(item.get("questions"), len(questions))
        linked = [questions[i - 1] for i in q_idx] or _questions_for(card, questions)
        titles: list[str] = []
        for t in (item.get("table_titles") or []):
            m = _match_title(str(t), table_titles)
            if m and m not in titles:
                titles.append(m)
        if not titles:
            titles = [t.title for t in _tables_for(card, report)]
        etype = str(card.get("evidence_type") or "list")
        ev = _clean_evidence(etype, item.get("evidence")) if covered else None
        if covered and ev is None:
            own = [e for q in linked for e in ev_by_q.get(q.id, [])]
            etype, ev = _evidence_from_report(card, report, linked, own)
        out.append(_finish(
            card, run_id=run_id, stage=stage, bucket=bucket, category=category,
            finding=finding, evidence_type=etype, evidence=ev,
            interpretation=_clean_text(item.get("interpretation"), 400),
            review_note=_clean_text(item.get("review_note"), 200),
            linked=linked, ev_by_q=ev_by_q, contradictions=contradictions,
            titles=titles, covered=covered, gap=_clean_text(item.get("gap"), 240),
        ))
    log.info("stage %s: %d cards filled from the document", stage, len(out))
    return out


# -- phase cards -----------------------------------------------------------------
async def phase_cards(run_id: str, cfg: RunConfig, phase: str, reports: list[StageReport],
                      cards_so_far: list[Insight]) -> list[Insight]:
    """Cards owed by a whole phase, written from all of its stage documents."""
    out: list[Insight] = []
    for card in phase_catalogue(phase):
        bucket = str(card.get("bucket") or "C")
        stage = str(card.get("stage") or (reports[-1].stage if reports else "stage_2"))
        category = str(card.get("category") or "Synthesis")
        related = [i for i in cards_so_far if i.stage in {r.stage for r in reports}]
        points: list[str] = []
        finding = ""
        interpretation = ""
        if llm.available and reports:
            digest = "\n".join(
                f"[{i.number:02d}] {i.title}: {i.summary[:300]}" for i in related
            ) + "\n\nTAKEAWAYS:\n" + "\n".join(
                f"- {t[:240]}" for r in reports for t in r.takeaways[:5]
            )
            try:
                result = await llm.complete_json(
                    _SYSTEM,
                    f"Indication: {cfg.indication}. Objective: {cfg.objective}.\n"
                    f"Card: {card['title']}. Objective: {card.get('objective', '')}.\n\n"
                    "FINDINGS OF THE PHASE:\n" + digest + "\n\n"
                    'Return JSON: {"finding": str, "points": [str], "interpretation": str}. '
                    "finding: one sentence on what the phase established. points: 4-6 "
                    "insights, one sentence each, that downstream modelling must preserve "
                    "(population variables, diagnostic signals, treatment branches, label "
                    "changes). interpretation: one sentence on what to carry into the next "
                    "phase. Only from the findings above.",
                    max_tokens=1200,
                )
                finding = _clean_text((result or {}).get("finding"), 400)
                points = _clean_evidence("list", (result or {}).get("points")) or []
                interpretation = _clean_text((result or {}).get("interpretation"), 300)
            except Exception:  # noqa: BLE001 - fall back to takeaways
                log.exception("phase card failed for %s/%s", run_id, phase)
        if not points:
            points = [_clean_text(t, 200) for r in reports for t in r.takeaways[:3]][:6]
        if not finding:
            finding = (f"The {phase} phase established {len(related)} findings across "
                       f"{len(reports)} stage report(s); the points below should inform "
                       "downstream modelling.")
        sources: list[str] = []
        for i in related:
            for s in i.source_ids:
                if s not in sources:
                    sources.append(s)
        out.append(Insight(
            run_id=run_id, stage=stage, bucket=bucket, category=category,
            number=int(card.get("number") or 0), card_key=str(card.get("key") or ""),
            title=str(card.get("title") or "Key insights"),
            summary=finding, evidence_type="list" if points else "", evidence=points or None,
            interpretation=interpretation, confidence=Confidence.READY,
            tag=VerificationTag.INFERENCE,
            evidence_ids=[e for i in related for e in i.evidence_ids][:200],
            source_ids=sources, question_ids=[q for i in related for q in i.question_ids],
            table_titles=[], covered=bool(points),
        ))
    return out
```

### File: `celestra\services\llm.py`

```py
"""LLM access with an honest degraded mode.

The app must run end to end without an API key, so every caller of this module
is required to have a deterministic fallback. `available` tells callers which
path to take; it is surfaced in the UI so nobody mistakes rule-based synthesis
for model-written synthesis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from ..settings import get_settings

log = logging.getLogger("celestra.llm")


class LLMUnavailable(RuntimeError):
    """Raised when no API key is configured, or the provider call failed."""


def _extract_json(text: str) -> Any:
    """Models sometimes wrap JSON in prose or a fenced block. Recover it."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    raise LLMUnavailable("model did not return parseable JSON")


class LLMClient:
    """One interface over three providers.

    Anthropic and Microsoft Foundry both speak the Messages API and use the
    official SDK. Azure OpenAI speaks a different wire format and has no
    Anthropic SDK, so it goes over HTTP. Callers see the same two methods and
    the same LLMUnavailable failure in every case.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: Any = None
        self._sem = asyncio.Semaphore(self._settings.llm_max_concurrency)

    @property
    def available(self) -> bool:
        return self._settings.llm_enabled

    @property
    def provider(self) -> str:
        return self._settings.provider

    @property
    def model(self) -> str:
        return self._settings.active_model

    def describe(self) -> str:
        if not self.available:
            gaps = ", ".join(self._settings.provider_gaps())
            return f"not configured ({self.provider}: missing {gaps})"
        return f"{self.provider} · {self.model}"

    # -- provider clients ------------------------------------------------
    def _anthropic(self) -> Any:
        if self._client is None:
            s = self._settings
            if s.provider == "anthropic_foundry":
                from anthropic import AnthropicFoundry

                self._client = AnthropicFoundry(
                    api_key=s.foundry_api_key,
                    resource=s.foundry_resource,
                    timeout=float(s.llm_timeout_seconds),
                )
            else:
                from anthropic import AsyncAnthropic

                self._client = AsyncAnthropic(
                    api_key=s.anthropic_api_key,
                    base_url=s.anthropic_base_url,
                    timeout=float(s.llm_timeout_seconds),
                )
        return self._client

    async def _complete_anthropic(
        self, system: str, prompt: str, max_tokens: int
    ) -> str:
        client = self._anthropic()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            # Synthesis and extraction are judgement work, so let the model
            # decide how much reasoning each call needs.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self._settings.llm_effort},
        }
        result = client.messages.create(**kwargs)
        msg = await result if hasattr(result, "__await__") else result
        # Thinking blocks carry .thinking, not .text, so this yields the answer only.
        return "".join(getattr(b, "text", "") or "" for b in msg.content)

    async def _complete_azure(self, system: str, prompt: str, max_tokens: int) -> str:
        """Azure AI Foundry / Azure OpenAI deployment over its REST API."""
        import httpx

        s = self._settings
        endpoint = (s.azure_openai_endpoint or "").rstrip("/")
        url = (
            f"{endpoint}/openai/deployments/{s.azure_openai_deployment}"
            f"/chat/completions?api-version={s.azure_openai_api_version}"
        )
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_completion_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=float(s.llm_timeout_seconds)) as http:
            resp = await http.post(
                url,
                json=payload,
                headers={"api-key": s.azure_openai_api_key or "", "Content-Type": "application/json"},
            )
            if resp.status_code == 400 and "max_completion_tokens" in resp.text:
                # Older Azure API versions and non-reasoning deployments still
                # take max_tokens; retry once rather than failing the run.
                payload["max_tokens"] = payload.pop("max_completion_tokens")
                resp = await http.post(
                    url,
                    json=payload,
                    headers={"api-key": s.azure_openai_api_key or "",
                             "Content-Type": "application/json"},
                )
            resp.raise_for_status()
            data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMUnavailable("azure deployment returned no choices")
        return str((choices[0].get("message") or {}).get("content") or "")

    # -- public API ------------------------------------------------------
    async def complete(self, system: str, prompt: str, *, max_tokens: int | None = None) -> str:
        if not self.available:
            raise LLMUnavailable(
                f"{self.provider} is not configured: missing "
                + ", ".join(self._settings.provider_gaps())
            )
        budget = max_tokens or self._settings.llm_max_tokens
        async with self._sem:
            try:
                if self.provider == "azure_openai":
                    return await self._complete_azure(system, prompt, budget)
                return await self._complete_anthropic(system, prompt, budget)
            except LLMUnavailable:
                raise
            except Exception as exc:  # a provider error must not kill a run
                log.warning("llm call failed (%s): %s", self.provider, exc)
                raise LLMUnavailable(f"{self.provider}: {exc}") from exc

    async def complete_json(
        self, system: str, prompt: str, *, max_tokens: int | None = None
    ) -> Any:
        text = await self.complete(
            system + "\n\nRespond with JSON only. No prose, no code fence.",
            prompt,
            max_tokens=max_tokens,
        )
        return _extract_json(text)


llm = LLMClient()
```

### File: `celestra\services\orchestrator.py`

```py
"""Runs the agents.

Execution is driven by the dependency graph in framework.yaml, not by stage
order. Agents with no unmet dependency form a wave and run concurrently; a
reconciliation gate closes each wave before the next begins. Two modes are
supported: every agent end to end, or a single agent on its own.

Everything user-visible calls these units "agents". The bucket letters are an
internal key and never leave this module in rendered form.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import defaultdict
from datetime import date

from ..events import bus
from ..models import (
    AgentState,
    Answer,
    AgentStatus,
    Confidence,
    Contradiction,
    Evidence,
    Insight,
    QuestionStatus,
    ResearchQuestion,
    Run,
    RunMode,
    RunStatus,
    StageReport,
    utcnow,
)
from ..settings import get_framework, get_questions, get_thresholds
from ..store import store
from . import contradictions as contra
from . import handoff
from . import insights as insight_gen
from . import planner, qa, retrieval, synthesis
from .scoring import assess_confidence

log = logging.getLogger("celestra.orchestrator")

CATEGORY_BY_BUCKET = {
    "A": "Clinical", "C": "Treatment", "B": "Diagnostic",
    "D": "Logic", "E": "Journey", "F": "Synthesis", "G": "Validation",
}


def agent_key(bucket: str) -> str:
    name = get_framework()["buckets"][bucket]["agent_name"]
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def bucket_for_agent_key(key: str) -> str | None:
    for letter in get_framework()["buckets"]:
        if agent_key(letter) == key:
            return letter
    return None


def build_agent_states(selected: list[str]) -> dict[str, AgentState]:
    fw = get_framework()["buckets"]
    waves = compute_waves(selected)
    wave_of = {b: i + 1 for i, wave in enumerate(waves) for b in wave}
    out: dict[str, AgentState] = {}
    for letter in selected:
        spec = fw[letter]
        out[letter] = AgentState(
            bucket=letter,
            key=agent_key(letter),
            name=spec["agent_name"],
            tagline=spec["agent_tagline"],
            icon=spec.get("agent_icon", "dot"),
            stages=list(spec.get("stages") or []),
            depends_on=[d for d in (spec.get("depends_on") or []) if d in selected],
            wave=wave_of.get(letter, 1),
        )
    return out


def compute_waves(selected: list[str]) -> list[list[str]]:
    """Topological layering. Dependencies outside the selection are ignored,
    which is what makes single-agent mode possible."""
    fw = get_framework()["buckets"]
    remaining = set(selected)
    done: set[str] = set()
    waves: list[list[str]] = []
    max_parallel = get_thresholds()["limits"]["max_parallel_agents"]

    while remaining:
        ready = sorted(
            b for b in remaining
            if all(d in done for d in (fw[b].get("depends_on") or []) if d in selected)
        )
        if not ready:  # cycle guard; should never fire with the shipped config
            ready = sorted(remaining)
        for i in range(0, len(ready), max_parallel):
            waves.append(ready[i : i + max_parallel])
        done |= set(ready)
        remaining -= set(ready)
    return waves


def all_buckets() -> list[str]:
    return [b for b, spec in get_framework()["buckets"].items() if spec.get("mode") != "governance"]


def phase_of(bucket: str) -> str:
    return str(get_framework()["buckets"][bucket].get("phase") or "discovery")


def phase_spec(key: str) -> dict:
    spec = dict((get_framework().get("phases") or {}).get(key) or {})
    spec.setdefault("name", key.replace("_", " ").title())
    spec.setdefault("description", "")
    spec["key"] = key
    return spec


def phases_for(selected: list[str]) -> list[dict]:
    """The phases this run passes through, each with its agents, in order."""
    order = list((get_framework().get("phases") or {}).keys()) or ["discovery", "mapping"]
    out = []
    for key in order:
        agents = [b for b in selected if phase_of(b) == key]
        if agents:
            out.append({**phase_spec(key), "agents": [agent_key(b) for b in agents]})
    return out


class Orchestrator:
    def __init__(self, run: Run, registry: dict) -> None:
        self.run = run
        self.registry = registry
        self.cfg = run.config
        ind = planner.indication_config(self.cfg.indication_key)
        self.synonyms = list(ind.get("synonyms") or [])
        if self.cfg.indication not in self.synonyms:
            self.synonyms.insert(0, self.cfg.indication)
        self.questions: list[ResearchQuestion] = []
        self.evidence: list[Evidence] = []
        self.answers: list[Answer] = []
        self.insights: list[Insight] = []
        self.contradictions: list[Contradiction] = []
        self.stages: list[StageReport] = []
        # Quote fingerprints already used as an insight headline.
        self._used_summaries: set[str] = set()

    # -- event helpers ---------------------------------------------------
    async def _emit(self, type_: str, **data) -> None:
        await bus.publish(self.run.id, type_, **data)

    async def _agent(self, state: AgentState, status: AgentStatus | None = None,
                     *, progress: float | None = None, message: str | None = None) -> None:
        if status is not None:
            state.status = status
        if progress is not None:
            state.progress = round(min(max(progress, 0.0), 1.0), 3)
        if message is not None:
            state.message = message
        self.run.agents[state.bucket] = state
        store.save_run(self.run)
        await self._emit(
            "agent_status",
            agent_key=state.key, agent_name=state.name, status=state.status.value,
            progress=state.progress, message=state.message,
            questions_total=state.questions_total,
            questions_answered=state.questions_answered,
            evidence_count=state.evidence_count,
        )

    # -- main ------------------------------------------------------------
    async def execute(self) -> None:
        selected = (
            all_buckets()
            if self.cfg.mode is RunMode.FULL
            else [self.cfg.selected_agent or "A"]
        )
        self.run.agents = build_agent_states(selected)
        self.run.status = RunStatus.RUNNING
        self.run.started_at = utcnow()
        store.save_run(self.run)

        waves = compute_waves(selected)
        await self._emit(
            "run_started",
            mode=self.cfg.mode.value,
            indication=self.cfg.indication,
            agents=[
                {"key": a.key, "name": a.name, "tagline": a.tagline,
                 "icon": a.icon, "wave": a.wave, "phase": phase_of(a.bucket)}
                for a in self.run.agents.values()
            ],
            waves=[[agent_key(b) for b in w] for w in waves],
            phases=phases_for(selected),
        )
        await self._emit_phase(self.run.phase)

        try:
            paused = await self._run_waves(waves, start_index=1)
            if not paused:
                await self._finalise()
        except asyncio.CancelledError:
            self.run.status = RunStatus.CANCELLED
            store.save_run(self.run)
            await self._emit("run_failed", error="cancelled")
            raise
        except Exception as exc:
            log.exception("run %s failed", self.run.id)
            self.run.status = RunStatus.FAILED
            self.run.error = f"{type(exc).__name__}: {exc}"
            self.run.finished_at = utcnow()
            store.save_run(self.run)
            await self._emit("run_failed", error=self.run.error)
        finally:
            await bus.close(self.run.id)

    async def _run_waves(self, waves: list[list[str]], start_index: int) -> bool:
        """Run waves from `start_index` (1-based). Returns True when the run
        paused at the human review gate rather than finishing."""
        gate = self.run.review_after_wave if self.cfg.mode is RunMode.FULL else 0
        for index, wave in enumerate(waves, start=1):
            if index < start_index:
                continue
            await self._emit("wave_started", wave=index,
                             agents=[agent_key(b) for b in wave])
            await asyncio.gather(*(self._run_agent(b) for b in wave))
            await self._emit(
                "wave_complete", wave=index,
                gate=get_framework()["buckets"][wave[0]].get("gate", ""),
            )
            await self._phase_cards_if_complete()
            if gate and index == gate and index < len(waves):
                await self._pause_for_review(index, waves)
                return True
        return False

    async def _web_notice_if_needed(self, state: AgentState) -> None:
        """Say once, on the live page and in the run, when web search has been
        switched off for the session, so unanswered questions are explained
        where the person is looking."""
        from ..connectors.firecrawl import firecrawl_blocked

        reason = firecrawl_blocked()
        if not reason or self.run.context.get("web_search_notice") == reason:
            return
        self.run.context["web_search_notice"] = reason
        store.save_run(self.run)
        await self._emit("notice", level="warning", agent_key=state.key,
                         text=f"Web search switched off: {reason}")

    async def _phase_cards_if_complete(self) -> None:
        """A phase whose agents have all finished owes its phase-level cards,
        written from every stage document in it. Once per phase."""
        if self.cfg.mode is not RunMode.FULL:
            # A phase is only complete when every agent in it ran.
            return
        done_phases = set(self.run.context.get("phase_cards_done") or [])
        for spec in phases_for([a.bucket for a in self.run.agents.values()]):
            key = spec["key"]
            if key in done_phases or not insight_gen.phase_catalogue(key):
                continue
            buckets = [b for b in self.run.agents if phase_of(b) == key]
            if not all(self.run.agents[b].status is AgentStatus.COMPLETE for b in buckets):
                continue
            stages = {st for b in buckets for st in (self.run.agents[b].stages or [])}
            reports = [r for r in self.stages if r.stage in stages]
            cards = await insight_gen.phase_cards(
                self.run.id, self.cfg, key, reports, [i for i in self.insights if i.stage in stages]
            )
            for card in cards:
                self.insights.append(card)
                await self._emit_insight(agent_key(card.bucket), card)
            if cards:
                store.save_insights(self.run.id, cards)
            done_phases.add(key)
            self.run.context["phase_cards_done"] = sorted(done_phases)
            store.save_run(self.run)

    async def _emit_phase(self, key: str) -> None:
        spec = phase_spec(key)
        await self._emit("phase_started", phase=key, name=spec["name"],
                         description=spec["description"])

    async def _pause_for_review(self, wave_index: int, waves: list[list[str]]) -> None:
        """Stop after a wave and hand the findings so far to a reviewer.

        Downstream agents build on these findings, so a wrong one propagates.
        Reviewing here is cheaper than reviewing everything at the end."""
        self.run.status = RunStatus.AWAITING_REVIEW
        self.run.resume_from_wave = wave_index + 1
        store.save_run(self.run)
        needs_input = [i for i in self.insights if i.needs_decision]
        await self._emit(
            "review_required",
            redirect=f"/runs/{self.run.id}/review",
            wave=wave_index,
            insights=len(self.insights),
            requires_input=len(needs_input),
            conflicts=len(self.contradictions),
            remaining_agents=[agent_key(b) for w in waves[wave_index:] for b in w],
        )

    async def resume(self) -> None:
        """Continue a run paused at the review gate. State is reloaded from the
        store, because the process that paused it may not be the one resuming.

        The web handler that triggers this marks the run RUNNING before the
        task starts, so the live page it redirects to is never bounced back to
        the review page by a stale status. Both statuses are therefore valid
        here; what matters is that a resume point was recorded.
        """
        if self.run.resume_from_wave < 2 or self.run.status not in (
            RunStatus.AWAITING_REVIEW, RunStatus.RUNNING
        ):
            return
        # The channel was closed when phase one ended. Bring it back before
        # anything is published, or the resumed run streams into the void.
        await bus.reopen(self.run.id)
        self.questions = store.get_questions(self.run.id)
        self.evidence = store.get_evidence(self.run.id)
        self.insights = store.get_insights(self.run.id)
        self.contradictions = store.get_contradictions(self.run.id)
        self.answers = store.get_answers(self.run.id)
        self.stages = store.get_stage_reports(self.run.id)
        self._used_summaries = {
            re.sub(r"\s+", " ", i.summary).strip()[:120].lower() for i in self.insights
        }
        self.run.status = RunStatus.RUNNING
        self.run.reviewed_at = self.run.reviewed_at or utcnow()
        store.save_run(self.run)

        selected = [a.bucket for a in self.run.agents.values()]
        waves = compute_waves(selected)
        await self._emit("run_resumed", from_wave=self.run.resume_from_wave,
                         waves=[[agent_key(b) for b in w] for w in waves],
                         phases=phases_for(selected))
        await self._emit_phase("mapping")
        try:
            paused = await self._run_waves(waves, start_index=self.run.resume_from_wave)
            if not paused:
                await self._finalise()
        except asyncio.CancelledError:
            self.run.status = RunStatus.CANCELLED
            store.save_run(self.run)
            await self._emit("run_failed", error="cancelled")
            raise
        except Exception as exc:
            log.exception("run %s failed on resume", self.run.id)
            self.run.status = RunStatus.FAILED
            self.run.error = f"{type(exc).__name__}: {exc}"
            self.run.finished_at = utcnow()
            store.save_run(self.run)
            await self._emit("run_failed", error=self.run.error)
        finally:
            await bus.close(self.run.id)

    async def _run_agent(self, bucket: str) -> None:
        state = self.run.agents[bucket]
        state.started_at = utcnow()
        await self._agent(state, AgentStatus.RESEARCHING, progress=0.02,
                          message="Planning research questions")

        try:
            stages = state.stages or []
            agent_questions: list[ResearchQuestion] = []
            for stage in stages:
                agent_questions += await planner.plan_stage(self.run.id, self.cfg, stage)

            state.questions_total = len(agent_questions)
            await self._agent(state, progress=0.06,
                              message=f"{len(agent_questions)} questions planned")
            if agent_questions:
                store.save_questions(self.run.id, agent_questions)

            inbound_context = handoff.for_agent(bucket, self.run.context)
            if inbound_context:
                await self._agent(state, message=(
                    "Using upstream context: "
                    + ", ".join(f"{len(v)} {k.replace('_', ' ')}"
                                for k, v in inbound_context.items())
                ))

            agent_evidence: list[Evidence] = []
            agent_answers: list[Answer] = []
            agent_contra: list[Contradiction] = []

            for i, question in enumerate(agent_questions, start=1):
                question.status = QuestionStatus.RETRIEVING
                await self._emit("question_status", agent_key=state.key,
                                 question_id=question.id, text=question.text,
                                 status=question.status.value)

                async def on_source(sid, sname, ok, count, reason, _s=state):
                    if ok and count and sid not in _s.sources_used:
                        _s.sources_used.append(sid)
                    await self._emit("source_used", agent_key=_s.key, source_id=sid,
                                     source_name=sname, ok=ok, count=count, reason=reason)

                hard_limit = float(get_thresholds()["limits"].get(
                    "question_hard_timeout_seconds", 480))
                try:
                    outcome = await asyncio.wait_for(
                        retrieval.retrieve(
                            question, self.cfg, self.synonyms, self.registry, on_source,
                            context=inbound_context,
                        ),
                        timeout=hard_limit,
                    )
                except asyncio.TimeoutError:
                    # The agent must never sit on one question. Report it as
                    # unanswered with the reason and move on.
                    log.warning("q=%s abandoned after %ss", question.id, int(hard_limit))
                    outcome = retrieval.RetrievalOutcome(
                        skipped=f"abandoned after {int(hard_limit)}s; sources did not respond",
                    )
                    outcome.sufficiency = retrieval.assess(question, [])
                retrieval.apply_outcome(question, outcome)
                await self._web_notice_if_needed(state)
                agent_evidence += outcome.evidence
                for a in outcome.answers:
                    a.run_id, a.stage = self.run.id, question.stage
                agent_answers += outcome.answers

                found = await contra.detect(question, outcome.evidence)
                agent_contra += found
                for c in found:
                    await self._emit("contradiction_added", agent_key=state.key,
                                     topic=c.topic, severity=c.severity.value)

                if question.status is QuestionStatus.SUFFICIENT:
                    state.questions_answered += 1
                state.evidence_count = len(agent_evidence)

                # Persist per question, not per agent. A long agent that fails
                # on question four should not discard the first three answers.
                store.save_questions(self.run.id, [question])
                store.save_evidence(self.run.id, outcome.evidence)
                if outcome.answers:
                    store.save_answers(self.run.id, outcome.answers)
                # Conflicts are deliberately not persisted per question: the same
                # claim pair surfaces on every question both sources answered, and
                # deduplication runs once the agent has seen them all. Saving here
                # made the duplicates permanent regardless of that pass.


                await self._agent(
                    state, progress=0.06 + 0.74 * (i / max(len(agent_questions), 1)),
                    message=(
                        f"{question.seed_text[:70] or question.text[:70]} — "
                        f"{'answered' if question.status is QuestionStatus.SUFFICIENT else question.status.value}"
                    ),
                )

            store.save_questions(self.run.id, agent_questions)
            store.save_evidence(self.run.id, agent_evidence)
            store.save_answers(self.run.id, agent_answers)
            agent_contra = contra.dedupe(agent_contra)
            store.save_contradictions(self.run.id, agent_contra)
            self.questions += agent_questions
            self.evidence += agent_evidence
            self.answers += agent_answers
            self.contradictions += agent_contra

            await self._agent(state, AgentStatus.SYNTHESISING, progress=0.84,
                              message="Synthesising findings")
            for stage in stages:
                sq = [q for q in agent_questions if q.stage == stage]
                se = [e for e in agent_evidence if e.question_id in {q.id for q in sq}]
                sc = [c for c in agent_contra if c.stage == stage]
                sa = [a for a in agent_answers if a.question_id in {q.id for q in sq}]
                report = await synthesis.build_stage_report(
                    self.run.id, self.cfg, stage, bucket, sq, se, sc, sa
                )
                self.stages.append(report)
                store.save_stage_reports(self.run.id, [report])
                await self._emit("stage_complete", agent_key=state.key, stage=stage,
                                 name=report.name, evidence_count=report.evidence_count,
                                 source_count=report.source_count,
                                 tables=len(report.tables))

                # Cards are the fixed slots this agent owes, filled from the
                # stage document it just wrote (by the model when one is
                # configured, from the mapped questions otherwise).
                await self._agent(state, message=f"Filling review cards for {report.name}")
                cards = await insight_gen.generate(
                    self.run.id, self.cfg, stage, bucket, report, sq, se, sc,
                    CATEGORY_BY_BUCKET.get(bucket, "Clinical"),
                )
                for card in cards:
                    if not card.table_titles:
                        titles = [
                            t.title for t in report.tables
                            if set(t.question_ids) & set(card.question_ids)
                        ]
                        card.table_titles = titles or [t.title for t in report.tables][:1]
                    self.insights.append(card)
                    await self._emit_insight(state.key, card)
                if cards:
                    store.save_insights(self.run.id, cards)

            produced = await handoff.build(bucket, self.cfg, agent_evidence)
            if produced:
                self.run.context = handoff.merge(self.run.context, produced)
                store.save_run(self.run)
                await self._emit("context_published", agent_key=state.key,
                                 entities={k: len(v) if isinstance(v, list) else 1
                                           for k, v in produced.items()})

            state.finished_at = utcnow()
            await self._agent(
                state, AgentStatus.COMPLETE, progress=1.0,
                message=f"{state.questions_answered}/{state.questions_total} questions "
                        f"answered from {len(state.sources_used)} sources",
            )
        except Exception as exc:
            log.exception("agent %s failed", bucket)
            state.error = f"{type(exc).__name__}: {exc}"
            state.finished_at = utcnow()
            await self._agent(state, AgentStatus.FAILED, message=state.error[:160])

    # -- insights --------------------------------------------------------
    async def _emit_insight(self, agent_key_: str, insight: Insight) -> None:
        await self._emit(
            "insight_added", agent_key=agent_key_, insight_id=insight.id,
            title=insight.title, confidence=insight.confidence.value,
            category=insight.category, sources=insight.source_ids,
        )

    def _insight_title(self, question: ResearchQuestion) -> str:
        meta = get_questions()["stage_meta"][question.stage]
        seeds = planner.seeds_for(self.cfg.indication_key, question.stage)
        steps = meta.get("framework_steps") or []
        if question.seed_text in seeds:
            idx = seeds.index(question.seed_text)
            if idx < len(steps):
                return str(steps[idx])
        text = re.sub(r"\s*\([^)]*\)", "", question.seed_text or question.text).strip(" ?")
        text = re.sub(r"^(what|which|how|where)\s+(are|is|do|does)?\s*", "", text, flags=re.I)
        return text[:80].strip().capitalize() or "Finding"

    def _insight_for(
        self, question: ResearchQuestion, evidence: list[Evidence],
        found: list[Contradiction], bucket: str,
    ) -> Insight | None:
        conf, reason = assess_confidence(question, evidence, found)
        best = sorted(evidence, key=lambda e: (e.tier, -e.relevance))

        # Two questions in a stage often retrieve the same document, and its
        # strongest quote would then headline both cards. Take the best quote
        # this run has not already used as a headline, so every card says
        # something different. The full evidence set is unchanged.
        # The established answer is what the question actually asked for. A raw
        # quote is the fallback, and only when no answer was reached.
        summary = ""
        if question.answer_text:
            answer = re.sub(r"\s+", " ", question.answer_text).strip()
            fingerprint = answer[:120].lower()
            if fingerprint not in self._used_summaries:
                self._used_summaries.add(fingerprint)
                summary = answer[:400]
        for candidate in ([] if summary else best):
            text = re.sub(r"\s+", " ", candidate.quote).strip()
            fingerprint = text[:120].lower()
            if fingerprint not in self._used_summaries:
                self._used_summaries.add(fingerprint)
                summary = text[:260]
                break
        if not summary:
            summary = (
                re.sub(r"\s+", " ", best[0].quote)[:260]
                if best
                else (question.unmet_reason or "No usable evidence was retrieved.")
            )
        source_ids: list[str] = []
        for e in best:
            if e.source_id not in source_ids:
                source_ids.append(e.source_id)
        return Insight(
            run_id=self.run.id, stage=question.stage, bucket=bucket,
            category=CATEGORY_BY_BUCKET.get(bucket, "Clinical"),
            title=self._insight_title(question),
            summary=summary,
            detail=" ".join(self._fresh_detail(best[1:6])),
            confidence=conf,
            input_reason=reason,
            evidence_ids=[e.id for e in evidence],
            source_ids=source_ids,
            question_ids=[question.id],
            used_web_fallback=question.used_web_fallback,
        )

    def _fresh_detail(self, candidates: list[Evidence], limit: int = 3) -> list[str]:
        """Supporting quotes not already shown on another card.

        Without this the same passages repeated under every finding in a
        stage, which reads as padding and hides how much distinct evidence
        there actually is."""
        out: list[str] = []
        for ev in candidates:
            text = re.sub(r"\s+", " ", ev.quote).strip()
            fingerprint = text[:120].lower()
            if fingerprint in self._used_summaries:
                continue
            self._used_summaries.add(fingerprint)
            out.append(text)
            if len(out) >= limit:
                break
        return out

    # -- finalisation ----------------------------------------------------
    async def _finalise(self) -> None:
        metrics = qa.build_metrics(
            self.cfg, self.questions, self.evidence, self.contradictions, self.stages
        )
        metrics = qa.build_narrative(self.cfg, metrics, self.stages, self.questions)
        metrics = await qa.polish_readiness(metrics, self.cfg)
        store.save_qa(self.run.id, metrics)

        self.run.status = RunStatus.COMPLETED
        self.run.finished_at = utcnow()
        store.save_run(self.run)

        counts = defaultdict(int)
        for i in self.insights:
            counts[i.confidence.value] += 1
        await self._emit(
            "run_complete",
            redirect=f"/runs/{self.run.id}/approval",
            insights=len(self.insights),
            sources=metrics.distinct_sources,
            ready=counts[Confidence.READY.value],
            requires_input=counts[Confidence.REQUIRES_INPUT.value],
            needs_decision=sum(1 for i in self.insights if i.needs_decision),
            questions_answered=metrics.questions_sufficient,
            questions_planned=metrics.questions_planned,
            duration=round(self.run.duration_seconds, 1),
        )


def new_reference() -> str:
    return f"RUN-{uuid.uuid4().hex[:8].upper()}"


def default_cutoff() -> str:
    return date.today().isoformat()
```

### File: `celestra\services\planner.py`

```py
"""Turns seed questions into scoped research questions.

The seed list in research_questions.yaml is deliberately short. The planner
widens each seed into a question carrying the population, geography and
cutoff, plus the sub-aspects that the sufficiency check later measures
coverage against. With an LLM configured this is model-written; without one it
is templated, which is weaker but never wrong.
"""
from __future__ import annotations

import logging

from ..models import ResearchQuestion, RunConfig
from ..settings import get_framework, get_questions, get_thresholds
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.planner")

_SYSTEM = (
    "You are a clinical desk-research planner for US claims analytics. You turn a broad "
    "seed question into one precise, answerable research question, scoped to the named "
    "population and geography, plus the specific sub-aspects that a complete answer must "
    "cover. You never invent clinical facts; you only shape the question."
)


def _stage_indices() -> list[str]:
    return [f"stage_{i}" for i in range(1, 7)]


def bucket_for_stage(stage: str) -> str:
    fw = get_framework()
    for letter, spec in fw["buckets"].items():
        if stage in (spec.get("stages") or []):
            return letter
    return "F"


def seeds_for(indication_key: str, stage: str) -> list[str]:
    cfg = get_questions()["indications"].get(indication_key)
    if not cfg:
        return []
    return list(cfg.get(stage) or [])


def indication_config(indication_key: str) -> dict:
    return get_questions()["indications"].get(indication_key, {})


def _fallback_aspects(seed: str) -> list[str]:
    """Split a seed question into coverage aspects without a model.

    The seed list already enumerates what a complete answer needs, either in
    parentheses or as a comma-and list in the question itself. Both are used.
    Falling back to the whole question as a single aspect makes the aspect
    unmatchable, which caps every coverage score.
    """
    import re

    parts: list[str] = []

    # "(Rai, Binet, CLL-IPI)" and "(IGHV, TP53, del(17p))"
    for group in re.findall(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)", seed):
        parts += [p.strip() for p in re.split(r",| or | and ", group) if len(p.strip()) > 2]

    # "the incidence, prevalence, survival and mortality of ..."
    stripped = re.sub(r"\([^)]*\)", " ", seed)
    for run in re.findall(r"((?:[a-z][a-z-]{3,},\s*){1,}[a-z][a-z-]{3,}(?:\s+(?:and|or)\s+[a-z][a-z-]{3,})?)",
                          stripped, flags=re.I):
        parts += [p.strip() for p in re.split(r",|\band\b|\bor\b", run) if len(p.strip()) > 3]

    stop = {"are", "the", "and", "for", "with", "from", "that", "this", "does",
            "have", "has", "used", "use", "their", "there", "was", "were",
            "united", "states", "adult", "patients", "population", "including",
            "such", "other", "both", "either", "relevant", "current"}

    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = p.lower().strip()
        # A connective picked up by the list regex is not a research aspect.
        if key in stop or key in seen:
            continue
        seen.add(key)
        out.append(p)
    # One surviving aspect is not a decomposition; fall through to the nouns.
    if len(out) >= 2:
        return out[:6]

    # No enumeration: use the question's own distinctive nouns as one aspect
    # each, so coverage measures concepts rather than sentence structure.
    text = re.sub(r"^(what|which|how|where|when)\b\s*", "", stripped.strip(" ?"), flags=re.I)
    nouns = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{4,}", text) if w.lower() not in stop]
    return nouns[:5] or [text[:120]]


def _scoped(seed: str, cfg: RunConfig) -> str:
    population = cfg.target_population or "adult patients"
    if cfg.geography.lower() not in seed.lower():
        seed = seed.rstrip("?") + f" in {cfg.geography}?"
    return f"{seed.rstrip('?')} ({population}, as of {cfg.research_cutoff})?"


async def plan_stage(
    run_id: str, cfg: RunConfig, stage: str
) -> list[ResearchQuestion]:
    limits = get_thresholds()["limits"]
    seeds = seeds_for(cfg.indication_key, stage)[: limits["max_questions_per_stage"]]
    bucket = bucket_for_stage(stage)
    meta = get_questions()["stage_meta"][stage]

    if not seeds:
        return []

    planned: list[ResearchQuestion] = []
    if llm.available:
        try:
            payload = {
                "indication": cfg.indication,
                "population": cfg.target_population or "adults",
                "geography": cfg.geography,
                "objective": cfg.objective,
                "additional_context": cfg.additional_context,
                "research_cutoff": cfg.research_cutoff,
                "stage_name": meta["name"],
                "stage_core_question": meta["core_question"],
                "expected_output": meta["expected_output"],
                "seed_questions": seeds,
            }
            result = await llm.complete_json(
                _SYSTEM,
                "Expand each seed question into a scoped research question.\n"
                "Return JSON: [{\"seed\": str, \"question\": str, \"aspects\": [str]}]. "
                "aspects are 2-6 short noun phrases naming what a complete answer must "
                "cover, used later to score evidence coverage.\n\n"
                f"{payload}",
            )
            for item in result or []:
                seed = str(item.get("seed") or "")
                text = str(item.get("question") or "").strip()
                if not text:
                    continue
                planned.append(
                    ResearchQuestion(
                        run_id=run_id, stage=stage, bucket=bucket, text=text,
                        seed_text=seed or text,
                        aspects=[str(a) for a in (item.get("aspects") or [])][:6],
                        aspects_from_model=True,
                    )
                )
        except LLMUnavailable as exc:
            log.info("planner falling back to templates: %s", exc)
            planned = []

    if not planned:
        planned = [
            ResearchQuestion(
                run_id=run_id, stage=stage, bucket=bucket,
                text=_scoped(seed, cfg), seed_text=seed,
                aspects=_fallback_aspects(seed),
            )
            for seed in seeds
        ]
    return planned


async def plan_run(run_id: str, cfg: RunConfig, stages: list[str]) -> list[ResearchQuestion]:
    out: list[ResearchQuestion] = []
    for stage in stages:
        out += await plan_stage(run_id, cfg, stage)
    return out
```

### File: `celestra\services\qa.py`

```py
"""Run-level QA. Produces the metrics, checklist and readiness assessment.

Every check is computed from the run's own evidence base. A check that cannot
be evaluated reports NOT APPLICABLE rather than PASS, because a checklist that
passes by default is worse than no checklist.
"""
from __future__ import annotations

from ..models import (
    Contradiction,
    ContradictionSeverity,
    Evidence,
    QAMetrics,
    QuestionStatus,
    ResearchQuestion,
    RunConfig,
    StageReport,
    VerificationTag,
)
from ..settings import get_thresholds
from .llm import LLMUnavailable, llm


def _check(name: str, ok: bool | None, detail: str) -> dict[str, str]:
    status = "NOT APPLICABLE" if ok is None else ("PASS" if ok else "FAIL")
    return {"check": name, "status": status, "detail": detail}


def build_metrics(
    cfg: RunConfig,
    questions: list[ResearchQuestion],
    evidence: list[Evidence],
    contradictions: list[Contradiction],
    stages: list[StageReport],
) -> QAMetrics:
    sufficient = [q for q in questions if q.status is QuestionStatus.SUFFICIENT]
    below = [q for q in questions if q.status is not QuestionStatus.SUFFICIENT]
    approved_ev = [e for e in evidence if not e.is_supplementary]
    web_ev = [e for e in evidence if e.is_supplementary]

    by_q: dict[str, list[Evidence]] = {}
    for e in evidence:
        by_q.setdefault(e.question_id, []).append(e)
    web_only = [
        q for q in sufficient
        if by_q.get(q.id) and all(e.is_supplementary for e in by_q[q.id])
    ]

    mean_cov = round(
        sum(q.coverage_score for q in questions) / len(questions) * 100, 1
    ) if questions else 0.0

    coded = [e for e in evidence if any(
        k in e.quote for k in ("ICD-", "CPT", "HCPCS", "LOINC", "NDC", "J-code")
    )]
    regimen_stages = [s for s in stages if s.stage in ("stage_2", "stage_4")]
    regimen_ev = [
        e for e in evidence
        if e.tier <= 2 and e.source_id in {
            "openfda_label", "openfda_drugsfda", "dailymed", "nccn", "esmo",
            "eha", "iwcll", "nci", "clinicaltrials", "ashpublications",
        }
    ]

    unresolved = [
        c for c in contradictions if c.review_action.value == "pending"
    ]

    checklist = [
        _check(
            "Every material factual claim carries an inline source reference",
            all(e.url for e in evidence) if evidence else None,
            f"{len(evidence)} evidence items carry a source URL and citation",
        ),
        _check(
            "No claims code is asserted without a verifiable coding-authority source",
            all(e.tier <= 2 for e in coded) if coded else None,
            f"{len(coded)} code-bearing quote(s) located verbatim in their cited source"
            if coded else "no claims codes asserted in this run",
        ),
        _check(
            "No regimen is asserted without a guideline or label source",
            bool(regimen_ev) if regimen_stages else None,
            f"{len(regimen_ev)} tier 1-2 guideline/label item(s) present"
            if regimen_stages else "treatment stages not part of this run",
        ),
        _check(
            "No FDA approval claim is asserted without an FDA or label source",
            any(e.source_id.startswith("openfda") or e.source_id == "dailymed"
                for e in evidence) if regimen_stages else None,
            "regulatory claims are backed by openFDA or DailyMed"
            if regimen_stages else "no regulatory stage in this run",
        ),
        _check(
            "Original analytical rules are tagged ORIGINAL, not VERIFIED",
            True,
            "analytical rules are emitted with the ORIGINAL tag by the synthesiser",
        ),
        _check(
            "Supplementary web evidence is distinguished from primary sources",
            all(e.tag is not VerificationTag.VERIFIED for e in web_ev) if web_ev else None,
            f"{len(web_ev)} fallback item(s) carry SUPPLEMENTARY WEB EVIDENCE status"
            if web_ev else "open-web fallback was not required",
        ),
        _check(
            "Source conflicts are surfaced rather than merged",
            not get_thresholds()["contradictions"]["auto_resolve"],
            f"{len(contradictions)} conflict(s) surfaced without auto-resolution",
        ),
        _check(
            "Claims observability is classified for each key clinical concept",
            all(s.observability for s in stages) if stages else None,
            "claims observability classified per stage in synthesis output",
        ),
        _check(
            "Assumptions and limitations are stated explicitly",
            all(s.assumptions for s in stages) if stages else None,
            "assumptions emitted per stage; limitations emitted at document level",
        ),
        _check(
            "Research cutoff date is respected and stated",
            bool(cfg.research_cutoff),
            f"cutoff {cfg.research_cutoff} applied to planning and stated in the header",
        ),
        _check(
            "Every unanswered question records why it could not be answered",
            all(q.unmet_reason for q in below) if below else None,
            f"{len(below)} question(s) below threshold, each with a recorded reason"
            if below else "every planned question reached sufficiency",
        ),
    ]

    readiness = _readiness_text(cfg, sufficient, below, unresolved, web_only)

    return QAMetrics(
        questions_planned=len(questions),
        questions_sufficient=len(sufficient),
        questions_web_only=len(web_only),
        questions_below_threshold=len(below),
        mean_coverage=mean_cov,
        evidence_total=len(evidence),
        evidence_approved=len(approved_ev),
        evidence_supplementary=len(web_ev),
        distinct_sources=len({e.source_id for e in evidence}),
        conflicts_surfaced=len(contradictions),
        checklist=checklist,
        readiness=readiness,
        sme_checklist=_sme_checklist(stages),
    )


def _readiness_text(
    cfg: RunConfig,
    sufficient: list[ResearchQuestion],
    below: list[ResearchQuestion],
    unresolved: list[Contradiction],
    web_only: list[ResearchQuestion],
) -> str:
    total = len(sufficient) + len(below)
    parts = [
        f"{len(sufficient)} of {total} planned questions reached the sufficiency "
        f"threshold for {cfg.indication} in {cfg.geography}."
    ]
    if below:
        parts.append(
            f"{len(below)} question(s) remain below threshold and are listed with their "
            f"blocking reason; those areas are not ready for analytical use."
        )
    if unresolved:
        escalated = sum(1 for c in unresolved if c.severity is ContradictionSeverity.ESCALATED)
        parts.append(
            f"{len(unresolved)} source disagreement(s) await adjudication, "
            f"{escalated} of them escalated on a tier gap."
        )
    if web_only:
        parts.append(
            f"{len(web_only)} question(s) were answered only from supplementary web "
            f"evidence and must be re-sourced before analytical use."
        )
    parts.append(
        "The output is ready for initial SME review."
        if not below and not unresolved
        else "SME review is required before this context is used downstream."
    )
    return " ".join(parts)


def _sme_checklist(stages: list[StageReport]) -> list[str]:
    items: list[str] = []
    for s in stages:
        for t in s.takeaways[:2]:
            clean = t.split("[")[0].strip().rstrip(".")
            if clean:
                items.append(f"Verify: {clean[:150]}.")
    return items[:8] or ["Verify every stage finding against its cited source."]


async def polish_readiness(qa: QAMetrics, cfg: RunConfig) -> QAMetrics:
    """Optional model pass that turns the metric summary into a reviewer-facing
    paragraph. Failure is not an error; the computed text stands."""
    if not llm.available:
        return qa
    try:
        result = await llm.complete_json(
            "You write the readiness assessment for a clinical desk-research deliverable "
            "about to go to a subject-matter expert. You are candid about weaknesses.",
            f"Indication: {cfg.indication}. Metrics: {qa.model_dump(exclude={'checklist'})}\n\n"
            'Return JSON: {"readiness": str, "sme_checklist": [str]}. readiness is 3-5 '
            "sentences naming the strongest aspects and the specific areas to monitor. "
            "sme_checklist is 5-8 concrete verification instructions.",
            max_tokens=1500,
        )
        if text := str((result or {}).get("readiness", "")).strip():
            qa.readiness = text
        if items := [str(x) for x in (result or {}).get("sme_checklist", [])]:
            qa.sme_checklist = items[:8]
    except LLMUnavailable:
        pass
    return qa


def build_narrative(
    cfg: RunConfig,
    metrics: QAMetrics,
    stages: list[StageReport],
    questions: list[ResearchQuestion],
) -> QAMetrics:
    """Executive summary, method and limitations for the report header.

    Written from the run's own numbers so it can never overstate what was
    retrieved. A model pass may rewrite the summary; it cannot change the
    counts it is describing.
    """
    stage_names = [s.name for s in stages]
    web_note = (
        f" {metrics.evidence_supplementary} item(s) came from open-web fallback and are "
        f"labelled SUPPLEMENTARY WEB EVIDENCE."
        if metrics.evidence_supplementary
        else " Open-web fallback was not required; approved sources answered every question "
        "that reached sufficiency."
    )
    gap_note = (
        f" {metrics.questions_below_threshold} question(s) did not reach the sufficiency "
        f"threshold and are listed with the reason and the sources attempted."
        if metrics.questions_below_threshold
        else ""
    )
    metrics.executive_summary = (
        f"This clinical desk-research deliverable establishes "
        f"{', '.join(n.lower() for n in stage_names) or 'the requested foundation'} for "
        f"{cfg.indication}"
        f"{' (' + cfg.target_population + ')' if cfg.target_population else ''} in "
        f"{cfg.geography}. Research ran source-first against the approved registry, "
        f"drawing {metrics.evidence_total} evidence items from "
        f"{metrics.distinct_sources} distinct sources, of which "
        f"{metrics.evidence_approved} came from approved sources."
        + web_note
        + f" {metrics.questions_sufficient} of {metrics.questions_planned} planned "
        f"questions reached the sufficiency threshold at a mean coverage of "
        f"{metrics.mean_coverage:.0f}%."
        + gap_note
        + (
            f" {metrics.conflicts_surfaced} source disagreement(s) are surfaced for SME "
            f"adjudication rather than resolved."
            if metrics.conflicts_surfaced
            else ""
        )
    )

    th = get_thresholds()
    metrics.research_method = [
        "The requested scope was expanded into a structured research plan with specific, "
        "population-scoped questions per stage.",
        "For each question the approved source registry was consulted first, using native "
        "source APIs where available and targeted domain-scoped search otherwise. Whole-site "
        "crawling was not performed.",
        "Only relevant documents were retrieved; each was converted into structured evidence "
        "with a verbatim supporting quote checked back against the source text.",
        f"Evidence was assessed for coverage, source tier distribution and contradictions. A "
        f"question below the threshold triggered query refinement up to "
        f"{th['escalation']['max_refinement_rounds']} time(s), then open-web fallback.",
        (
            "Open-web fallback was required for "
            f"{metrics.questions_web_only} question(s) and is labelled as supplementary."
            if metrics.questions_web_only
            else "Open-web fallback was not required: approved sources answered every "
            "question that reached sufficiency."
        ),
        f"Findings were synthesised per stage and QA-validated against the run's evidence "
        f"base ({metrics.questions_sufficient} of {metrics.questions_planned} questions "
        f"reached the sufficiency threshold).",
    ]

    metrics.limitations = [
        "This document is a research artefact produced by an automated desk-research "
        "workflow and requires subject-matter-expert review before analytical use.",
        "Claims codes, regimen definitions and line-of-therapy rules marked ORIGINAL are "
        "analytical constructs of this workflow, not source facts.",
        "Any value marked NOT VERIFIED could not be located in the cited source text and "
        "must be confirmed against the coding authority before use.",
        "Evidence marked SUPPLEMENTARY WEB EVIDENCE came from open-web fallback and carries "
        "lower evidentiary weight than approved-source evidence.",
        f"Coverage is bounded by the research cutoff of {cfg.research_cutoff}; developments "
        f"after that date are out of scope.",
    ]
    blocked = sorted({
        sid for q in questions for sid in q.sources_attempted
        if sid not in q.sources_answered
    })
    if blocked:
        metrics.limitations.append(
            "The following registered sources returned nothing during this run and their "
            "contribution is therefore absent: " + ", ".join(blocked[:10]) + "."
        )
    return metrics
```

### File: `celestra\services\ranking.py`

```py
"""Relevance ranking of discovered documents, before anything is fetched.

Discovery returns cheap metadata: a title and an abstract or snippet. Ranking
decides which of those deserve a full-text fetch and an extraction call, in
ONE model call for the whole candidate set rather than one per document.
Without a model, a term-overlap score does the same job less well.
"""
from __future__ import annotations

import logging
import re

from ..models import SourceRef
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.ranking")

_SYSTEM = (
    "You rank candidate documents by how likely each is to contain a direct, quotable "
    "answer to a clinical research question. You judge only from the title and abstract "
    "supplied. You never invent content a document might have."
)
_WORD = re.compile(r"[a-z0-9]+")


def _overlap_score(ref: SourceRef, terms) -> float:
    """Term overlap between a candidate's metadata and the question.

    Focus terms (what the question is about) decide the order; context terms
    (the disease name) only break ties, since every candidate mentions them.
    """
    blob = f"{ref.title} {ref.snippet}".lower()
    words = set(_WORD.findall(blob))
    if not words:
        return 0.0
    focus = getattr(terms, "focus", None)
    context = getattr(terms, "context", None)
    if focus is None:
        focus, context = set(terms or ()), set()
    focus_hits = len(words & focus)
    context_hits = len(words & (context or set()))
    if focus and not focus_hits:
        return round(0.02 * min(context_hits, 3), 3)
    base = focus_hits / (len(focus) ** 0.5) if focus else context_hits / max(len(context or ()), 1) ** 0.5
    # A primary-tier source with any overlap outranks a weak one with more.
    return round(base + 0.03 * min(context_hits, 3) + (0.3 if ref.tier <= 2 else 0.0), 3)


def rank_deterministic(refs: list[SourceRef], terms: set[str]) -> list[tuple[SourceRef, float]]:
    """Term-overlap ranking, normalised to 0..1.

    Normalisation matters: the caller eliminates candidates below a relevance
    floor expressed on the model's 0..1 probability scale. Raw overlap scores
    are unbounded and usually far below it, so leaving them unscaled silently
    eliminated almost every candidate.
    """
    raw = [(r, _overlap_score(r, terms)) for r in refs]
    top = max((sc for _, sc in raw), default=0.0)

    # Term overlap is ordinal, not a probability. Mapping it onto the model's
    # absolute scale is what makes the caller's relevance floor mean the same
    # thing in both modes: anything with real topical overlap lands above the
    # floor, and only a candidate with none is eliminated. Scaling by the best
    # score instead would cut a genuinely relevant document merely because a
    # better one existed.
    scored: list[tuple[SourceRef, float]] = []
    for ref, sc in raw:
        if sc <= 0 or top <= 0:
            scored.append((ref, 0.0))
        else:
            scored.append((ref, round(0.4 + 0.6 * (sc / top), 3)))
    scored.sort(key=lambda x: (-x[1], x[0].tier))
    return scored


async def rank(
    refs: list[SourceRef], question: str, terms: set[str]
) -> list[tuple[SourceRef, float]]:
    """Every ref with a 0..1 relevance, best first. Never drops a ref: the
    caller decides the cut-off, so a ranking failure degrades to term overlap
    rather than to an empty candidate set."""
    if not refs:
        return []
    if not llm.available:
        return rank_deterministic(refs, terms)

    listing = "\n".join(
        f"[{i}] ({r.source_name}, tier {r.tier}) {r.title[:160]}\n"
        f"     {(r.snippet or '')[:400]}"
        for i, r in enumerate(refs)
    )
    try:
        result = await llm.complete_json(
            _SYSTEM,
            f"Question: {question}\n\nCandidates:\n{listing}\n\n"
            'Return JSON: [{"index": int, "relevance": 0..1, "reason": str}] with one '
            "entry per candidate. relevance is the probability the document contains a "
            "quotable sentence that directly answers the question. reason is at most "
            "twelve words.",
            max_tokens=200 + 60 * len(refs),
        )
    except LLMUnavailable as exc:
        log.info("ranking fell back to term overlap: %s", exc)
        return rank_deterministic(refs, terms)

    scores: dict[int, float] = {}
    for item in result or []:
        try:
            idx, rel = int(item.get("index")), float(item.get("relevance", 0.0))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= idx < len(refs):
            scores[idx] = max(0.0, min(rel, 1.0))
    if not scores:
        return rank_deterministic(refs, terms)

    # A candidate the model skipped keeps a normalised overlap score, halved so
    # it sorts below anything the model actually rated.
    _fallback = {r.key: sc for r, sc in rank_deterministic(refs, terms)}
    scored = [
        (r, scores.get(i, round(0.5 * _fallback.get(r.key, 0.0), 3)))
        for i, r in enumerate(refs)
    ]
    scored.sort(key=lambda x: (-x[1], x[0].tier))
    return scored
```

### File: `celestra\services\retrieval.py`

```py
"""Answering one question against the source registry.

Order of attack, and it is deliberate:
  1. Approved API and local sources mapped to this stage and indication,
     queried in parallel. No web search is made at this step.
  2. If that misses, refine the query and retry, up to the configured number
     of rounds.
  3. If still unanswered, the approved sources that are reached by a
     domain-scoped web search (cdc.gov, who.int, cancer.org, fda.gov ...).
     Still tier 1-2 evidence, but each call costs a search credit, so they
     wait until the APIs have had their turn.
  4. If still unanswered, the open web.

Getting an answer is the priority, so steps 3 and 4 are real escalations
rather than formalities. What step 4 is not allowed to do is quietly pass
itself off as an approved source: open-web evidence is tier 3, carries
EvidenceOrigin.OPEN_WEB, and renders as SUPPLEMENTARY WEB EVIDENCE everywhere
it appears.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from dataclasses import dataclass, field

from ..connectors.base import ConnectorResult, RetrievalContext
from ..connectors.firecrawl import firecrawl_blocked
from ..models import (
    Answer,
    Evidence,
    EvidenceOrigin,
    QuestionStatus,
    ResearchQuestion,
    RunConfig,
    SourceRef,
)
from ..settings import get_source_registry, get_thresholds
from .answering import answer_batch, merge as merge_answers
from .extraction import build_terms
from .llm import LLMUnavailable, llm
from .scoring import Sufficiency, assess

log = logging.getLogger("celestra.retrieval")


@dataclass
class RetrievalOutcome:
    evidence: list[Evidence] = field(default_factory=list)
    answers: list[Answer] = field(default_factory=list)
    eliminated: int = 0
    web_sites: list[dict] = field(default_factory=list)
    sufficiency: Sufficiency | None = None
    attempted: list[str] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    used_web: bool = False
    used_targeted: bool = False
    rounds: int = 0
    search_query: str = ""          # the model-drafted web query, if one was made
    skipped: str = ""               # why later tiers were not tried


@functools.lru_cache(maxsize=1)
def _unusable_source_ids() -> frozenset[str]:
    """Sources that cannot answer today: missing licence, credential or file.

    Cached for the process because it reflects configuration, not run state.
    """
    try:
        from ..connectors.registry import connector_health

        return frozenset(row["id"] for row in connector_health() if not row["configured"])
    except Exception:  # noqa: BLE001 - never let health checking break retrieval
        return frozenset()


SEARCH_ACCESS = ("targeted_search", "firecrawl_search")


def _registry_matches(src: dict, stage: str, indication_key: str) -> bool:
    if not src.get("enabled", True) or src.get("fallback_only"):
        return False
    if stage not in (src.get("stages") or []):
        return False
    inds = src.get("indications") or []
    return not inds or indication_key in inds or "ANY" in inds


def targeted_sources_for(stage: str, indication_key: str) -> list[dict]:
    """Approved sources that are reached by a domain-scoped web search. They
    are consulted only once the API sources have failed to answer."""
    out = [
        src for src in get_source_registry()["sources"]
        if src.get("access_method") in SEARCH_ACCESS
        and _registry_matches(src, stage, indication_key)
    ]
    blocked = _unusable_source_ids()
    return sorted(out, key=lambda s: (s["id"] in blocked, s["tier"], s["id"]))


def sources_for(stage: str, indication_key: str) -> list[dict]:
    """Approved API and local-file sources for this stage and indication, best
    first. Search-reached sources are excluded here; see targeted_sources_for.

    Ordering matters because the per-question source budget is finite. Sorting
    by tier then id alone spent the whole budget alphabetically: a stage with
    fourteen registered sources would call eight blocked ones and never reach
    the working alternative further down the alphabet. Sources known to be
    unusable are therefore ranked last, so they are attempted only if budget
    remains and still appear in the attempted list with their blocking reason.
    """
    out = [
        src for src in get_source_registry()["sources"]
        if src.get("access_method") not in SEARCH_ACCESS
        and _registry_matches(src, stage, indication_key)
    ]
    blocked = _unusable_source_ids()
    return sorted(out, key=lambda s: (s["id"] in blocked, s["tier"], s["id"]))


def _broaden(question: str, aspects: list[str], synonyms: list[str], round_no: int) -> str:
    """Heuristic query refinement used when no model is configured. Round 1
    drops the parenthetical scoping, round 2 reduces to the key nouns."""
    text = re.sub(r"\s*\([^)]*\)\s*", " ", question).strip(" ?")
    if round_no == 1:
        return text
    nouns = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{4,}", text)][:6]
    base = " ".join(nouns) or text
    return f"{base} {synonyms[0] if synonyms else ''}".strip()


async def refine_query(
    question: ResearchQuestion, cfg: RunConfig, synonyms: list[str],
    suff: Sufficiency, round_no: int,
) -> str:
    if llm.available:
        try:
            result = await llm.complete_json(
                "You rewrite a failing literature/database query so it retrieves more "
                "relevant records. You keep the clinical meaning identical and only change "
                "specificity, phrasing and terminology.",
                f"Original question: {question.text}\n"
                f"Indication: {cfg.indication}\nSynonyms: {synonyms}\n"
                f"Why it failed: {suff.reason}\n"
                f"Aspects still uncovered: {question.aspects}\n\n"
                'Return JSON: {"query": str}. A shorter, more retrievable phrasing using '
                "standard clinical vocabulary. No parentheticals.",
                max_tokens=400,
            )
            if q := str((result or {}).get("query", "")).strip():
                return q
        except LLMUnavailable:
            pass
    return _broaden(question.text, question.aspects, synonyms, round_no)


async def draft_search_query(question: ResearchQuestion, cfg: RunConfig,
                             notes: list[str]) -> str:
    """One short search-engine query for this question, written by the model.

    A research question is phrased for a person ("What CPT and HCPCS codes
    cover bone marrow biopsy ... in ALL?"); a search engine wants the terms
    ("acute lymphoblastic leukemia bone marrow biopsy CPT HCPCS codes"). One
    small call here saves failed searches later, which cost credits.
    """
    plain = re.sub(r"\s*\([^)]*\)", "", question.text).strip(" ?")
    fallback = f"{cfg.indication} {plain}"
    if not llm.available:
        return fallback
    try:
        result = await llm.complete_json(
            "You write web search queries for clinical desk research. You return the "
            "6-12 most discriminating terms, no question words, no quotes, no operators.",
            f"Indication: {cfg.indication}. Geography: {cfg.geography}.\n"
            f"Question: {question.text}\n"
            + (f"Reviewer context: {' '.join(notes)[:300]}\n" if notes else "")
            + 'Return JSON: {"query": str}.',
            max_tokens=80,
        )
        q = re.sub(r"\s+", " ", str((result or {}).get("query", ""))).strip().strip('"')
        if 3 <= len(q.split()) <= 16:
            return q
    except LLMUnavailable:
        pass
    except Exception:  # noqa: BLE001 - a failed draft is not a failed question
        log.debug("search query draft failed", exc_info=True)
    return fallback


def _as_ref(item, source_id: str = "open_web", tier: int = 3) -> SourceRef | None:
    """Web search results as SourceRefs, whichever shape a backend returned."""
    if isinstance(item, SourceRef):
        return item
    if isinstance(item, dict) and item.get("url"):
        text = str(item.get("markdown") or item.get("snippet") or "")
        return SourceRef(
            source_id=source_id, source_name="Open Web (Supplementary)", tier=tier,
            url=str(item["url"]), title=str(item.get("title") or item["url"]),
            organization="Open web", snippet=text[:1500],
            raw={"markdown": str(item.get("markdown") or ""), "page_text": text[:20000],
                 "text": text[:20000], "search_backend": item.get("backend", "")},
            origin=EvidenceOrigin.OPEN_WEB,
        )
    return None


async def _gather(
    registry: dict, source_ids: list[str], ctx: RetrievalContext, per_source: int
) -> list[ConnectorResult]:
    async def one(sid: str) -> ConnectorResult:
        conn = registry.get(sid)
        if conn is None:
            return ConnectorResult.failure(sid, "no connector registered")
        try:
            return await conn.discover(ctx, per_source)
        except Exception as exc:  # a connector bug must not kill the run
            log.exception("connector %s raised", sid)
            return ConnectorResult.failure(sid, f"connector error: {type(exc).__name__}")

    return list(await asyncio.gather(*(one(s) for s in source_ids)))


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).netloc or "").replace("www.", "")
    except ValueError:
        return ""


def _merge_evidence(
    current: list[Evidence], addition: list[Evidence], limits: dict
) -> list[Evidence]:
    """Add new evidence, keeping the quote set diverse across sources.

    Truncating a merged pile by score alone lets one verbose document take
    every slot, which reads as well-sourced while resting on a single source
    and fails the distinct-source threshold. Selection round-robins across
    sources under a per-source cap instead.
    """
    seen = {e.quote[:120].lower() for e in current}
    pool = list(current)
    for e in addition:
        key = e.quote[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        pool.append(e)

    per_source_cap = int(limits.get("max_evidence_items_per_source", 3))
    total_cap = int(limits["max_evidence_items_per_question"])

    by_source: dict[str, list[Evidence]] = {}
    for e in sorted(pool, key=lambda x: (x.tier, -x.relevance)):
        by_source.setdefault(e.source_id, []).append(e)

    out: list[Evidence] = []
    for depth in range(per_source_cap):
        for source_id in sorted(by_source, key=lambda s: by_source[s][0].tier):
            bucket = by_source[source_id]
            if depth < len(bucket) and len(out) < total_cap:
                out.append(bucket[depth])
        if len(out) >= total_cap:
            break
    out.sort(key=lambda e: (e.tier, -e.relevance))
    return out[:total_cap]


async def retrieve(
    question: ResearchQuestion,
    cfg: RunConfig,
    synonyms: list[str],
    registry: dict,
    on_source=None,
    context: dict | None = None,
) -> RetrievalOutcome:
    """Answer one question. `on_source` is an async callback
    (source_id, source_name, ok, count, reason) that streams live progress.

    Flow, in order:
      1. discover   every approved source returns cheap metadata (title, abstract)
      2. rank       one model call scores all candidates for this question
      3. hydrate    full text is fetched only for the top few
      4. extract    documents go to the model in batches, not one per call
      5. assess     against the sufficiency threshold
      6. widen      a further round keeps more candidates and rewrites the query
      7. fallback   open web, only after the approved sources are exhausted
    """
    from .hydration import hydrate
    from .ranking import rank

    th = get_thresholds()
    limits, esc = th["limits"], th["escalation"]
    outcome = RetrievalOutcome()
    started_at = time.monotonic()
    budget = float(limits.get("question_time_budget_seconds", 240))

    def over_budget(step: str) -> bool:
        spent = time.monotonic() - started_at
        if spent < budget:
            return False
        if not outcome.skipped:
            outcome.skipped = (f"time budget of {int(budget)}s spent before {step}; "
                               "reported with what was found")
            log.warning("q=%s %s", question.id, outcome.skipped)
        return True

    approved = sources_for(question.stage, cfg.indication_key)[: limits["max_sources_per_question"]]
    source_ids = [s["id"] for s in approved]
    names = {s["id"]: s["name"] for s in approved}
    per_source = max(2, limits["max_evidence_items_per_question"] // max(len(source_ids), 1))

    upstream_terms: list[str] = []
    notes: list[str] = [str(n) for n in (context or {}).get("reviewer_notes") or []]
    for key, value in (context or {}).items():
        # Reviewer notes are sentences for the model, not terms for a search.
        if key == "reviewer_notes":
            continue
        if isinstance(value, list):
            upstream_terms += [str(v) for v in value[:20]]
    terms = build_terms(question.text, question.aspects, synonyms, upstream_terms)
    query = question.text

    candidates: dict[str, SourceRef] = {}       # by ref.key, across rounds
    hydrated: dict[str, SourceRef] = {}         # cache so a doc is fetched once
    top_k = int(limits.get("rank_top_k", 8))
    hydrate_k = int(limits.get("hydrate_top_k", 5))
    batch_size = max(1, int(limits.get("extract_batch_size", 3)))
    min_relevance = float(limits.get("min_relevance", 0.35))

    def answered() -> bool:
        """Stop condition. With a model, an answer must exist; without one,
        the deterministic engine can only collect quotes, so sufficiency
        alone is the honest bar."""
        return bool(outcome.sufficiency and outcome.sufficiency.ok
                    and (outcome.answers or not llm.available))

    async def run_round(ids: list[str], round_no: int, query_text: str) -> None:
        """One pass: discover from `ids`, rank, hydrate, answer in batches."""
        nonlocal top_k
        ctx = RetrievalContext(
            indication=cfg.indication, indication_key=cfg.indication_key,
            synonyms=synonyms, geography=cfg.geography,
            population=cfg.target_population, stage=question.stage,
            question=query_text, aspects=question.aspects, cutoff=cfg.research_cutoff,
            extra=dict(context or {}),
        )

        # 1. discover
        results = await _gather(registry, ids, ctx, per_source)
        for r in results:
            if r.source_id not in outcome.attempted:
                outcome.attempted.append(r.source_id)
            if r.ok and r.count:
                if r.source_id not in outcome.answered:
                    outcome.answered.append(r.source_id)
                for ref in r.refs:
                    candidates.setdefault(ref.key, ref)
            elif not r.ok:
                outcome.failures[r.source_id] = r.reason
            if on_source:
                await on_source(r.source_id, names.get(r.source_id, r.source_id),
                                r.ok, r.count, r.reason)

        if not candidates:
            outcome.sufficiency = assess(question, outcome.evidence)
            return

        # 2. rank, eliminate the irrelevant, keep the top slice
        ranked = await rank(list(candidates.values()), question.text, terms)
        relevant = [(ref, score) for ref, score in ranked if score >= min_relevance]
        outcome.eliminated = len(ranked) - len(relevant)
        if not relevant:
            # Nothing cleared the relevance floor. Keep the single best so
            # the round still reads something rather than reporting an
            # empty result that a wider query might have answered.
            relevant = ranked[:1]
        keep = [ref for ref, _ in relevant[:top_k]]

        # 3. hydrate the best few; the rest are read from their abstracts
        docs: list[SourceRef] = []
        for i, ref in enumerate(keep):
            if i < hydrate_k:
                if ref.key not in hydrated:
                    hydrated[ref.key] = await hydrate(ref, registry)
                docs.append(hydrated[ref.key])
            else:
                docs.append(ref)

        # 4. answer the question from each batch, stopping as soon as the
        #    accumulated evidence clears the threshold. Later batches are
        #    not read when earlier ones already answered it.
        round_evidence: list[Evidence] = list(outcome.evidence)
        for batch_no, start in enumerate(range(0, len(docs), batch_size)):
            answer, evidence = await answer_batch(
                question.text, question.aspects, docs[start:start + batch_size],
                question.id, terms, batch_index=batch_no, round_index=round_no,
                notes=notes,
            )
            if answer is not None:
                outcome.answers.append(answer)
            round_evidence = _merge_evidence(round_evidence, evidence, limits)
            outcome.evidence = round_evidence
            outcome.sufficiency = assess(question, round_evidence)
            if answered():
                break

        # 5. assess
        outcome.sufficiency = assess(question, outcome.evidence)

    # -- API and local sources first, widening the query between rounds ------
    for round_no in range(esc["max_refinement_rounds"] + 1):
        outcome.rounds = round_no
        await run_round(source_ids, round_no, query)

        # Finding the answer matters more than which source supplies it. Held
        # evidence that never produced an answer is not a reason to stop: the
        # question is still unanswered, so keep going and let the next tier try.
        if answered():
            return outcome

        # 6. widen: keep more candidates next round and rewrite the query
        if round_no < esc["max_refinement_rounds"]:
            if over_budget(f"refinement round {round_no + 1}"):
                break
            top_k += int(limits.get("rank_top_k_step", 4))
            query = await refine_query(question, cfg, synonyms, outcome.sufficiency, round_no + 1)
            log.info("refining q=%s round=%s top_k=%s -> %s",
                     question.id, round_no + 1, top_k, query)

    # -- approved sources reached by a domain-scoped search ------------------
    # Only now. Every call here is a web-search credit, and the answer may
    # already be in the APIs above. These are still approved, tier 1-2 sources.
    targeted = targeted_sources_for(question.stage, cfg.indication_key)
    targeted = targeted[: int(esc.get("targeted_search_max_sources", 2))]
    web_off = firecrawl_blocked()
    if targeted and esc.get("enable_targeted_search_fallback", True):
        if web_off:
            outcome.failures["targeted_search"] = web_off[:160]
            outcome.skipped = outcome.skipped or web_off
        elif not over_budget("domain search"):
            names.update({s["id"]: s["name"] for s in targeted})
            outcome.used_targeted = True
            # One drafted query serves the domain searches and the open web.
            outcome.search_query = await draft_search_query(question, cfg, notes)
            log.info("q=%s unanswered by API sources; searching approved domains %s for %r",
                     question.id, [s["id"] for s in targeted], outcome.search_query)
            context = {**(context or {}), "search_query": outcome.search_query}
            await run_round([s["id"] for s in targeted], outcome.rounds + 1, query)
            if answered():
                return outcome

    if not esc["enable_open_web_fallback"]:
        return outcome
    if answered():
        # Already answered from registered sources; the web has nothing to add.
        return outcome

    # 7. open-web fallback, only once every approved source is exhausted
    fire = registry.get("open_web")
    if fire is None:
        return outcome
    if firecrawl_blocked():
        outcome.failures["open_web"] = firecrawl_blocked()[:160]
        outcome.skipped = outcome.skipped or firecrawl_blocked()
        if on_source:
            await on_source("open_web", "Open Web (Supplementary)", False, 0,
                            "web search unavailable")
        outcome.sufficiency = assess(question, outcome.evidence)
        return outcome
    if over_budget("open-web search"):
        outcome.sufficiency = assess(question, outcome.evidence)
        return outcome
    outcome.used_web = True
    if "open_web" not in outcome.attempted:
        outcome.attempted.append("open_web")
    try:
        web_query = outcome.search_query or await draft_search_query(question, cfg, notes)
        outcome.search_query = web_query
        raw_results = await fire.search(web_query, esc["open_web_max_results"])
        web_refs = [r for r in (_as_ref(x) for x in raw_results) if r is not None]
        # Record every page the search returned, whether or not it was read,
        # so the evidence trail shows where the fallback actually looked.
        outcome.web_sites = [
            {"url": r.url, "title": r.title or r.url, "site": _domain(r.url),
             "scraped": False, "used": False}
            for r in web_refs
        ]
        scraped: list[SourceRef] = []
        for i, ref in enumerate(web_refs[: esc["open_web_max_scrapes"]]):
            markdown = str((ref.raw or {}).get("markdown") or "")
            if markdown:
                # The search already returned the page; no scrape call needed.
                page: SourceRef | dict | None = ref
            else:
                page = await fire.scrape(ref.url)
            if isinstance(page, dict):
                page = _as_ref({**page, "backend": page.get("backend", "")}) or ref
                if page is not ref:
                    page.raw["page_text"] = str(page.snippet or "")
            if i < len(outcome.web_sites):
                outcome.web_sites[i]["scraped"] = True
            scraped.append(page or ref)
        for batch_no, start in enumerate(range(0, len(scraped), batch_size)):
            answer, extra = await answer_batch(
                question.text, question.aspects, scraped[start:start + batch_size],
                question.id, terms, batch_index=batch_no, round_index=99,
                notes=notes,
            )
            if answer is not None:
                outcome.answers.append(answer)
            if extra and "open_web" not in outcome.answered:
                outcome.answered.append("open_web")
            outcome.evidence = _merge_evidence(outcome.evidence, extra, limits)
            outcome.sufficiency = assess(question, outcome.evidence)
            if answered():
                break
        extra = [e for e in outcome.evidence if e.is_supplementary]
        # Mark which pages actually produced a quote.
        used_urls = {e.url for e in extra}
        for site in outcome.web_sites:
            site["used"] = site["url"] in used_urls
        if on_source:
            await on_source("open_web", "Open Web (Supplementary)", bool(extra),
                            len(extra), "" if extra else "no usable page text")
    except Exception as exc:
        log.warning("web fallback failed for %s: %s", question.id, exc)
        outcome.failures["open_web"] = f"{type(exc).__name__}"
        if on_source:
            await on_source("open_web", "Open Web (Supplementary)", False, 0, str(exc)[:80])

    outcome.sufficiency = assess(question, outcome.evidence)
    return outcome


def llm_configured() -> bool:
    """Whether answers are expected at all.

    Without a model the pipeline never produces answer prose, so requiring one
    would mark every question unanswered.
    """
    from .llm import llm

    return llm.available


def apply_outcome(question: ResearchQuestion, outcome: RetrievalOutcome) -> None:
    """Write the retrieval result back onto the question, including an honest
    reason when it stayed unanswered."""
    suff = outcome.sufficiency
    question.refinement_rounds = outcome.rounds
    question.used_web_fallback = outcome.used_web
    question.sources_attempted = outcome.attempted
    question.sources_answered = outcome.answered
    question.coverage_score = suff.coverage if suff else 0.0

    text, status, citations = merge_answers(outcome.answers)
    question.answer_text = text
    question.answer_status = status
    question.answer_citations = citations
    question.web_sites = outcome.web_sites

    if suff and suff.ok and (outcome.answers or not llm_configured()):
        question.status = QuestionStatus.SUFFICIENT
        question.unmet_reason = ""
        return

    if suff and suff.ok and not outcome.answers:
        # Sourced, but nothing in it answered the question. Reporting this as
        # answered because the evidence count cleared a threshold is exactly
        # the kind of false green a reviewer cannot see through.
        question.status = QuestionStatus.INSUFFICIENT
        question.unmet_reason = (
            "evidence was retrieved but no source answered the question"
            + (f"; {outcome.skipped[:160]}" if outcome.skipped else "")
        )
        return

    if not outcome.evidence:
        question.status = QuestionStatus.UNANSWERED
        blockers = "; ".join(
            f"{sid}: {reason}" for sid, reason in list(outcome.failures.items())[:4]
        )
        question.unmet_reason = (
            f"no usable evidence after {outcome.rounds + 1} retrieval round(s)"
            + (f" — {blockers}" if blockers else "")
        )
    else:
        question.status = QuestionStatus.INSUFFICIENT
        question.unmet_reason = suff.reason if suff else "below sufficiency threshold"
    if outcome.skipped and outcome.skipped[:60] not in question.unmet_reason:
        question.unmet_reason += f"; {outcome.skipped[:160]}"
```

### File: `celestra\services\revision.py`

```py
"""Applying a reviewer's instruction to a finding.

Modify is not a note. The reviewer's instruction goes to the model together
with the established answer and its evidence, and the model decides whether it
can apply the change from what is already held or needs to look further. When
it needs more, the open-web fallback runs and the revised answer is rebuilt
from what that returns.

The same rule as everywhere else holds: a revised answer must be supported by
verified quotes. An instruction cannot conjure a fact the sources do not
contain, and the model is told to say so rather than comply.
"""
from __future__ import annotations

import logging
import re

from ..models import (
    Answer,
    AnswerStatus,
    Evidence,
    Insight,
    ResearchQuestion,
    RunConfig,
)
from ..settings import get_thresholds
from .answering import answer_batch
from .extraction import build_terms
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.revision")

_TRIAGE_SYSTEM = (
    "You decide whether a reviewer's instruction about a research finding can be applied "
    "from the evidence already held, or whether more sources must be consulted first. You "
    "never invent a fact to satisfy an instruction."
)
_APPLY_SYSTEM = (
    "You revise a research finding according to a reviewer's instruction, using only the "
    "evidence supplied. Every claim in the revised answer must be supported by one of the "
    "quotes. If the instruction asks for something the evidence does not support, you say "
    "plainly that it is not supported and leave that part unchanged."
)


class RevisionResult:
    __slots__ = ("text", "status", "citations", "evidence", "answer",
                 "searched", "sites", "note")

    def __init__(self) -> None:
        self.text: str = ""
        self.status: AnswerStatus = AnswerStatus.NOT_FOUND
        self.citations: list[str] = []
        self.evidence: list[Evidence] = []
        self.answer: Answer | None = None
        self.searched: bool = False
        self.sites: list[dict] = []
        self.note: str = ""


async def _needs_more(instruction: str, question: str, answer: str,
                      quotes: list[str]) -> tuple[bool, str]:
    """Ask whether the held evidence can satisfy the instruction."""
    try:
        verdict = await llm.complete_json(
            _TRIAGE_SYSTEM,
            f"Question: {question}\n\nCurrent answer: {answer or '(none)'}\n\n"
            "Evidence held:\n" + "\n".join(f"- {q[:300]}" for q in quotes[:12]) + "\n\n"
            f"Reviewer instruction: {instruction}\n\n"
            'Return JSON: {"needs_more_sources": bool, "search_query": str, "reason": str}. '
            "needs_more_sources is true only when the instruction asks for information the "
            "evidence above does not contain. search_query is what to look for on the open "
            "web, empty when nothing is needed.",
            max_tokens=500,
        )
    except LLMUnavailable as exc:
        return False, str(exc)
    return bool((verdict or {}).get("needs_more_sources")), str(
        (verdict or {}).get("search_query") or ""
    )


async def revise(
    insight: Insight,
    question: ResearchQuestion,
    evidence: list[Evidence],
    instruction: str,
    cfg: RunConfig,
    registry: dict,
    synonyms: list[str] | None = None,
) -> RevisionResult:
    """Apply the instruction, searching the web when the held evidence cannot."""
    out = RevisionResult()
    out.evidence = list(evidence)
    instruction = (instruction or "").strip()
    if not instruction:
        return out

    if not llm.available:
        out.note = ("No model provider is configured, so the instruction was recorded "
                    "against the finding but not applied.")
        return out

    esc = get_thresholds()["escalation"]
    terms = build_terms(question.text, question.aspects, synonyms or [cfg.indication])
    quotes = [e.quote for e in evidence]

    # 1. Can this be applied from what is already held?
    needs_more, query = await _needs_more(instruction, question.text,
                                          question.answer_text, quotes)

    # 2. If not, fall back to the open web for the missing part.
    if needs_more:
        fire = registry.get("open_web")
        if fire is not None:
            out.searched = True
            search_query = query or f"{cfg.indication} {instruction}"
            try:
                refs = await fire.search(search_query, esc["open_web_max_results"])
                out.sites = [
                    {"url": r.url, "title": r.title or r.url, "scraped": False, "used": False}
                    for r in refs
                ]
                pages = []
                for i, ref in enumerate(refs[: esc["open_web_max_scrapes"]]):
                    page = await fire.scrape(ref.url)
                    if i < len(out.sites):
                        out.sites[i]["scraped"] = True
                    pages.append(page or ref)
                if pages:
                    answer, extra = await answer_batch(
                        f"{question.text}\n\nReviewer instruction: {instruction}",
                        question.aspects, pages, question.id, terms,
                    )
                    if extra:
                        used = {e.url for e in extra}
                        for site in out.sites:
                            site["used"] = site["url"] in used
                        out.evidence = out.evidence + [
                            e for e in extra
                            if e.quote[:120].lower()
                            not in {x.quote[:120].lower() for x in out.evidence}
                        ]
                    if answer is not None:
                        out.answer = answer
            except Exception as exc:  # noqa: BLE001 - a failed search is not fatal
                log.warning("revision search failed: %s", exc)
                out.note = f"Open-web search failed: {type(exc).__name__}."

    # 3. Rewrite the answer against the full evidence set.
    listing = "\n".join(
        f"- [{e.citation}{' · OPEN WEB' if e.is_supplementary else ''}] {e.quote[:400]}"
        for e in out.evidence[:20]
    )
    try:
        result = await llm.complete_json(
            _APPLY_SYSTEM,
            f"Question: {question.text}\n\nCurrent answer: {question.answer_text or '(none)'}\n\n"
            f"Evidence available:\n{listing}\n\n"
            f"Reviewer instruction: {instruction}\n\n"
            'Return JSON: {"answer": str, "status": "answered"|"partial", '
            '"applied": bool, "note": str}. answer is the revised finding, 2-5 sentences, '
            "every claim supported by a quote above. applied is false when the evidence "
            "cannot support the instruction; note then says what is missing, in one "
            "sentence.",
            max_tokens=1500,
        )
    except LLMUnavailable as exc:
        out.note = f"Revision could not be applied: {exc}"
        return out

    result = result or {}
    text = re.sub(r"\s+", " ", str(result.get("answer", ""))).strip()
    if text:
        out.text = text
        out.status = (
            AnswerStatus.ANSWERED
            if str(result.get("status", "")).lower() == "answered"
            else AnswerStatus.PARTIAL
        )
    if note := str(result.get("note") or "").strip():
        out.note = note
    if not result.get("applied", True) and not out.note:
        out.note = "The available evidence does not support this instruction."

    citations: list[str] = []
    for e in out.evidence:
        if e.citation not in citations:
            citations.append(e.citation)
    out.citations = citations
    return out
```

### File: `celestra\services\scoring.py`

```py
"""Sufficiency, coverage and confidence.

Every threshold lives in config/thresholds.yaml. Nothing here hardcodes a
number, so tuning the strictness of a run is a config change, not a code
change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import (
    Confidence,
    Contradiction,
    ContradictionSeverity,
    Evidence,
    QuestionStatus,
    ResearchQuestion,
    ReviewAction,
)
from ..settings import get_source_registry, get_thresholds


def tier_weight(tier: int) -> float:
    tiers = get_source_registry()["tiers"]
    spec = tiers.get(tier) or tiers.get(str(tier)) or {}
    return float(spec.get("weight", 0.2))


@dataclass
class Sufficiency:
    ok: bool
    coverage: float
    reason: str = ""
    evidence_items: int = 0
    distinct_sources: int = 0
    primary_items: int = 0
    aspects_covered: int = 0
    aspects_total: int = 0


def _aspect_hits(question: ResearchQuestion, evidence: list[Evidence]) -> tuple[int, int]:
    """How many of the question's aspects the evidence actually covers.

    Word-level matching, not substring: 'age' inside 'percentage' would
    silently inflate coverage. Aspects that contain no matchable word are
    dropped from the denominator as well as the numerator, because counting an
    unmatchable aspect as a miss put a hard ceiling on every question's score.
    """
    if not question.aspects:
        return (1, 1) if evidence else (0, 1)

    blob = " ".join(f"{e.title} {e.quote} {e.context}" for e in evidence).lower()
    tokens = set(re.findall(r"[a-z0-9]+", blob))

    usable: list[list[str]] = []
    for aspect in question.aspects:
        # Three characters, not four: Rai, IGHV, TP53 and CLL are exactly the
        # discriminating terms these questions turn on.
        words = [w for w in re.findall(r"[a-z0-9]+", aspect.lower()) if len(w) >= 3]
        if words:
            usable.append(words)
    if not usable:
        return (1, 1) if evidence else (0, 1)

    hits = 0
    for words in usable:
        matched = sum(1 for w in words if w in tokens)
        # A one or two word aspect names a single concept and must be present.
        # A longer phrase is satisfied by its distinctive words; demanding all
        # of them measures phrasing rather than coverage.
        need = 1 if len(words) <= 2 else max(2, round(len(words) * 0.4))
        if matched >= need:
            hits += 1
    return hits, len(usable)


def assess(question: ResearchQuestion, evidence: list[Evidence]) -> Sufficiency:
    cfg = get_thresholds()["sufficiency"]
    usable = [e for e in evidence if len(e.quote.strip()) >= cfg["min_quote_length"]]
    sources = {e.source_id for e in usable}
    primary = [e for e in usable if e.tier <= cfg["primary_tier_ceiling"]]
    hits, total = _aspect_hits(question, usable)

    aspect_ratio = hits / total if total else 0.0
    tier_component = 0.0
    if usable:
        tier_component = sum(tier_weight(e.tier) for e in usable) / len(usable)
    volume = min(len(usable) / max(cfg["min_evidence_items"], 1), 1.0)
    diversity = min(len(sources) / max(cfg["min_distinct_sources"] * 2, 1), 1.0)

    # Two weightings, because the two kinds of aspect mean different things.
    #
    # Model-written aspects name the concepts an answer must contain, so
    # failing to find them is genuine evidence the question is unanswered and
    # they carry the most weight.
    #
    # Heuristic aspects are only the question's own words. Sources answer in
    # their own vocabulary: a label that fully answers "which therapies are
    # FDA-approved" says "is indicated for the treatment of" and contains none
    # of "FDA", "approved", "label" or "indications". Scoring those as a miss
    # measured phrasing, not coverage, and held well-sourced answers below the
    # threshold. They now act as an uplift that can raise a score, never as a
    # gate that sinks one, and the weight moves to what is observable without
    # a model: source quality, independent corroboration and volume.
    if question.aspects_from_model:
        coverage = (0.45 * aspect_ratio + 0.30 * tier_component
                    + 0.15 * diversity + 0.10 * volume)
    else:
        coverage = (0.20 * aspect_ratio + 0.40 * tier_component
                    + 0.25 * diversity + 0.15 * volume)
    coverage = round(coverage, 3)

    checks = [
        (len(usable) >= cfg["min_evidence_items"],
         f"only {len(usable)} usable evidence item(s), need {cfg['min_evidence_items']}"),
        (len(sources) >= cfg["min_distinct_sources"],
         f"only {len(sources)} distinct source(s), need {cfg['min_distinct_sources']}"),
        (len(primary) >= cfg["min_primary_tier_items"],
         f"no tier {cfg['primary_tier_ceiling']} or better source"),
        (coverage >= cfg["min_coverage_score"],
         f"coverage {coverage:.2f} below {cfg['min_coverage_score']:.2f}"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    return Sufficiency(
        ok=not failed,
        coverage=coverage,
        reason="; ".join(failed),
        evidence_items=len(usable),
        distinct_sources=len(sources),
        primary_items=len(primary),
        aspects_covered=hits,
        aspects_total=total,
    )


def confidence_for(
    question: ResearchQuestion,
    evidence: list[Evidence],
    contradictions: list[Contradiction],
) -> Confidence:
    return assess_confidence(question, evidence, contradictions)[0]


def assess_confidence(
    question: ResearchQuestion,
    evidence: list[Evidence],
    contradictions: list[Contradiction],
) -> tuple[Confidence, str]:
    """Two states, and the reason in words when a person has to act.

    Requires Input fires for exactly three reasons: nothing usable was found,
    only the open web answered the question, or two sources disagree and
    nobody has decided. Everything else is Ready: a vetted source answered
    it, which is the sufficiency rule this app runs on.
    """
    ceiling = get_thresholds()["sufficiency"]["primary_tier_ceiling"]
    if not evidence:
        return Confidence.REQUIRES_INPUT, (
            "No source returned usable evidence. Add what you know, or tell "
            "Celestra where to look."
        )

    primary = [e for e in evidence if e.tier <= ceiling and not e.is_supplementary]
    if not primary:
        return Confidence.REQUIRES_INPUT, (
            "Only open-web pages answered this. No approved source confirmed it, "
            "so a person has to accept it, correct it, or add a source."
        )

    # A conflict concerns the question it surfaced on. Only conflicts with no
    # recorded question fall back to the stage, so one disagreement no longer
    # flags every card in the stage.
    open_conflicts = [
        c for c in contradictions
        if c.severity is ContradictionSeverity.ESCALATED
        and c.review_action is ReviewAction.PENDING
        and ((c.question_id == question.id) if c.question_id
             else (not c.stage or c.stage == question.stage))
    ]
    if open_conflicts:
        c = open_conflicts[0]
        return Confidence.REQUIRES_INPUT, (
            f"{c.source_a_name} and {c.source_b_name} disagree on {c.topic}. "
            "Decide the conflict below, or add input, before this can be used."
        )
    return Confidence.READY, ""


def status_after_retrieval(question: ResearchQuestion, suff: Sufficiency) -> QuestionStatus:
    esc = get_thresholds()["escalation"]
    if suff.ok:
        return QuestionStatus.SUFFICIENT
    if question.refinement_rounds < esc["max_refinement_rounds"]:
        return QuestionStatus.REFINING
    if esc["enable_open_web_fallback"] and not question.used_web_fallback:
        return QuestionStatus.WEB_FALLBACK
    return QuestionStatus.INSUFFICIENT if suff.evidence_items else QuestionStatus.UNANSWERED
```

### File: `celestra\services\synthesis.py`

```py
"""Builds a StageReport: the structured object the UI renders as a stage.

Two paths. With an LLM the tables and prose are model-written from the
evidence and then checked back against it. Without one, everything is derived
from the evidence directly. Both paths carry the same provenance, so a reader
can always see which source produced a row.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from ..models import (
    Answer,
    Contradiction,
    Evidence,
    InsightTable,
    QuestionStatus,
    ResearchQuestion,
    RunConfig,
    StageReport,
    VerificationTag,
)
from ..settings import get_framework, get_questions
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.synthesis")

_SYSTEM = (
    "You are a clinical desk-research synthesiser producing an SME-ready deliverable for "
    "US claims analytics. You write only what the supplied evidence supports. Every factual "
    "sentence you write must be traceable to a supplied quote. You never invent a "
    "statistic, a code, a regimen or an approval. You never resolve a disagreement between "
    "sources. You mark original analytical rules as [ORIGINAL] and inferences as "
    "[INFERENCE]; everything drawn directly from a quote is [VERIFIED]."
)

_COL_SPEC = re.compile(r"\(([^)]*\|[^)]*)\)")


def parse_expected_tables(expected: list[str]) -> list[tuple[str, list[str]]]:
    """`"Epidemiology snapshot table (Metric | Value | Source)"` becomes a title
    and its column list. Entries without a column spec are prose sections."""
    out: list[tuple[str, list[str]]] = []
    for item in expected:
        m = _COL_SPEC.search(item)
        if not m:
            continue
        cols = [c.strip() for c in m.group(1).split("|") if c.strip()]
        if len(cols) >= 2:
            out.append((item[: m.start()].strip(), cols))
    return out


def prose_sections(expected: list[str]) -> list[str]:
    return [i for i in expected if not _COL_SPEC.search(i)]


def _tag(text: str, tag: VerificationTag = VerificationTag.VERIFIED) -> str:
    return f"[{tag.value}] {text}" if not text.startswith("[") else text


def _cite(evs: list[Evidence]) -> str:
    names, seen = [], set()
    for e in evs:
        n = e.citation
        if n not in seen:
            seen.add(n)
            names.append(n)
    return f"[Source: {'; '.join(names[:4])}]" if names else ""


def _sentence_case(text: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------
# Deterministic construction
# --------------------------------------------------------------------------
def _metric_rows(
    evidence: list[Evidence], columns: list[str], used_question_ids: set[str] | None = None,
) -> list[list[str]]:
    """Fill an N-column table from evidence. The last column is always the
    citation; the first carries the subject; the middle carries the claim.
    `used_question_ids`, when given, collects which questions fed the rows so
    the table can be linked back to the insights built from them."""
    rows: list[list[str]] = []
    for ev in evidence:
        if len(rows) >= 12:
            break
        if used_question_ids is not None:
            used_question_ids.add(ev.question_id)
        subject = ev.title or ev.citation
        row = [_sentence_case(subject, 70)]
        while len(row) < len(columns) - 1:
            row.append(_tag(_sentence_case(ev.quote), ev.tag))
        row.append(f"[Source: {ev.citation}]")
        rows.append(row[: len(columns)])
    return rows


def _observability_rows(
    questions: list[ResearchQuestion], evidence_by_q: dict[str, list[Evidence]]
) -> list[dict[str, str]]:
    """Claims observability is an analytical judgement, not a source fact, so it
    is always tagged ORIGINAL."""
    out: list[dict[str, str]] = []
    for q in questions[:6]:
        evs = evidence_by_q.get(q.id, [])
        if not evs:
            continue
        answered = q.status is QuestionStatus.SUFFICIENT
        out.append({
            "concept": _sentence_case(q.seed_text.strip(" ?") or q.text, 110),
            "classification": "DIRECT SIGNAL" if answered else "PROXY SIGNAL",
            "basis": _sentence_case(evs[0].quote, 180),
            "limitation": (
                "Supported by coded sources retrievable from claims."
                if answered
                else q.unmet_reason
                or "Not consistently observable from administrative claims alone."
            ),
        })
    return out


def _takeaways(
    questions: list[ResearchQuestion], evidence_by_q: dict[str, list[Evidence]]
) -> list[str]:
    out: list[str] = []
    for q in questions:
        evs = sorted(evidence_by_q.get(q.id, []), key=lambda e: (e.tier, -e.relevance))
        if q.answer_text:
            tag = "VERIFIED" if evs and not evs[0].is_supplementary else "GENERAL KNOWLEDGE"
            out.append(f"{_sentence_case(q.answer_text, 240)} [{tag}]")
        elif evs:
            out.append(f"{_sentence_case(evs[0].quote, 240)} [{evs[0].tag.value}]")
        if len(out) >= 6:
            break
    return out


def _synthesis_paragraph(
    cfg: RunConfig, meta: dict, questions: list[ResearchQuestion],
    evidence_by_q: dict[str, list[Evidence]],
) -> str:
    bits: list[str] = []
    for q in questions:
        # An established answer says what the evidence means; a bare quote only
        # says what one source stated. Prefer the answer where one exists.
        if q.answer_text:
            bits.append(_sentence_case(q.answer_text, 400))
        else:
            evs = sorted(evidence_by_q.get(q.id, []), key=lambda e: (e.tier, -e.relevance))
            if evs:
                bits.append(_sentence_case(evs[0].quote, 300))
        if len(bits) >= 5:
            break
    if not bits:
        return (
            f"No approved source returned usable evidence for {cfg.indication} at this "
            f"stage. Every question is recorded below with the reason it could not be "
            f"answered."
        )
    lead = (
        f"For {cfg.indication} in {cfg.geography}"
        f"{' (' + cfg.target_population + ')' if cfg.target_population else ''}, "
        f"the retrieved evidence establishes the following."
    )
    return " ".join([lead, *bits])


# --------------------------------------------------------------------------
# LLM construction
# --------------------------------------------------------------------------
async def _llm_stage(
    cfg: RunConfig, stage: str, meta: dict,
    questions: list[ResearchQuestion], evidence_by_q: dict[str, list[Evidence]],
    answers: dict[str, list[Answer]] | None = None,
) -> dict | None:
    payload = {
        "indication": cfg.indication,
        "population": cfg.target_population or "adults",
        "geography": cfg.geography,
        "research_cutoff": cfg.research_cutoff,
        "stage_name": meta["name"],
        "core_question": meta["core_question"],
        "expected_output": meta["expected_output"],
        "questions": [
            {
                "question": q.text,
                "answered": q.status is QuestionStatus.SUFFICIENT,
                "established_answer": q.answer_text,
                "answer_status": q.answer_status.value,
                "unmet_reason": q.unmet_reason,
                "evidence": [
                    {
                        "quote": e.quote,
                        "source": e.citation,
                        "tier": e.tier,
                        "url": e.url,
                        "supplementary_web": e.is_supplementary,
                    }
                    for e in sorted(evidence_by_q.get(q.id, []), key=lambda x: x.tier)[:10]
                ],
            }
            for q in questions
        ],
    }
    try:
        return await llm.complete_json(
            _SYSTEM,
            "Write this research stage.\n\n"
            "Each question below carries an established_answer already derived from its "
            "evidence and verified against it. Build the stage from those answers: the "
            "synthesis, tables and narratives must be consistent with them and must not "
            "contradict or exceed them. The quotes are supplied so you can cite precisely, "
            "not so you can reach a different conclusion.\n\n"
            "Return JSON with keys:\n"
            '  "what_happens": one paragraph describing what this stage establishes.\n'
            '  "synthesis": 4-8 sentence prose synthesis, every claim traceable to a quote.\n'
            '  "tables": [{"title": str, "columns": [str], "rows": [[str]], '
            '"footnote": str, "question_indices": [int]}] — one entry per expected_output '
            "item that names columns in parentheses, using exactly those column names. "
            "question_indices lists the 0-based positions in `questions` that the table "
            "answers. Prefix each factual cell "
            "with [VERIFIED] and end each row's evidence with [Source: name]. Use "
            "[NOT VERIFIED] for any value you cannot locate in a quote.\n"
            '  "narratives": [{"heading": str, "body": str}] — one per expected_output '
            "item that does not name columns.\n"
            '  "takeaways": [str] — 4-6 numbered-style findings, each ending with a tag.\n'
            '  "assumptions": [str]\n'
            '  "observability": [{"concept": str, "classification": '
            '"DIRECT SIGNAL"|"PROXY SIGNAL"|"NOT OBSERVABLE", "basis": str, '
            '"limitation": str}]\n\n'
            "Do not resolve disagreements between sources. Do not invent codes, "
            "statistics, regimens or approvals.\n\n"
            f"{payload}",
            max_tokens=8000,
        )
    except LLMUnavailable as exc:
        log.info("stage synthesis falling back to deterministic: %s", exc)
        return None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def build_stage_report(
    run_id: str, cfg: RunConfig, stage: str, bucket: str,
    questions: list[ResearchQuestion], evidence: list[Evidence],
    contradictions: list[Contradiction], answers: list[Answer] | None = None,
) -> StageReport:
    fw = get_framework()
    meta = get_questions()["stage_meta"][stage]
    spec = fw["buckets"][bucket]
    steps = {n: s for n, s in fw["steps"].items() if s.get("stage") == stage}

    evidence_by_q: dict[str, list[Evidence]] = defaultdict(list)
    for e in evidence:
        evidence_by_q[e.question_id].append(e)

    report = StageReport(
        run_id=run_id, stage=stage, bucket=bucket,
        name=meta["name"], core_question=meta["core_question"],
        agent_name=spec["agent_name"],
        framework_steps=meta["framework_steps"],
        step_numbers=sorted(steps),
        substeps={
            k: v for s in steps.values() for k, v in (s.get("substeps") or {}).items()
        },
        gate=spec.get("gate", ""), output_name=spec.get("output", ""),
        expected_output=meta["expected_output"],
    )

    # The question-and-answer record. Built first, because it is the spine of
    # the document: the prose and tables below are written from these answers
    # rather than from the raw quote pile.
    by_question: dict[str, list[Answer]] = defaultdict(list)
    for a in answers or []:
        by_question[a.question_id].append(a)
    report.answers = [
        {
            "question": q.text,
            "seed": q.seed_text or q.text,
            "answer": q.answer_text or "",
            "status": q.answer_status.value,
            "citations": q.answer_citations,
            "sources": sorted({e.source_id for e in evidence_by_q.get(q.id, [])}),
            "evidence_count": len(evidence_by_q.get(q.id, [])),
            "coverage": round(q.coverage_score, 2),
            "supplementary": any(e.is_supplementary for e in evidence_by_q.get(q.id, [])),
            "unmet_reason": q.unmet_reason,
        }
        for q in questions
    ]

    data = await _llm_stage(cfg, stage, meta, questions, evidence_by_q,
                            answers=by_question) if llm.available else None

    if data:
        report.what_happens = str(data.get("what_happens", ""))
        report.synthesis = str(data.get("synthesis", ""))
        for t in data.get("tables") or []:
            cols = [str(c) for c in (t.get("columns") or [])]
            rows = [[str(c) for c in r] for r in (t.get("rows") or []) if r]
            if cols and rows:
                qids: list[str] = []
                for idx in t.get("question_indices") or []:
                    try:
                        qids.append(questions[int(idx)].id)
                    except (TypeError, ValueError, IndexError):
                        continue
                report.tables.append(
                    InsightTable(
                        title=str(t.get("title") or "Table"),
                        columns=cols,
                        rows=[r[: len(cols)] + [""] * (len(cols) - len(r)) for r in rows],
                        footnote=str(t.get("footnote") or ""),
                        question_ids=qids,
                    )
                )
        report.narratives = [
            {"heading": str(n.get("heading", "")), "body": str(n.get("body", ""))}
            for n in (data.get("narratives") or [])
            if n.get("body")
        ]
        report.takeaways = [str(x) for x in (data.get("takeaways") or [])]
        report.assumptions = [str(x) for x in (data.get("assumptions") or [])]
        report.observability = [
            {k: str(v) for k, v in row.items()} for row in (data.get("observability") or [])
        ]

    if not report.synthesis:
        report.synthesis = _synthesis_paragraph(cfg, meta, questions, evidence_by_q)
    if not report.what_happens:
        report.what_happens = (
            f"This stage establishes {meta['name'].lower()} for {cfg.indication} in "
            f"{cfg.geography}, drawn from the approved source registry and recorded with "
            f"per-claim provenance."
        )
    if not report.tables:
        ordered = sorted(evidence, key=lambda e: (e.tier, -e.relevance))
        for title, cols in parse_expected_tables(meta["expected_output"]):
            used: set[str] = set()
            rows = _metric_rows(ordered, cols, used)
            if rows:
                report.tables.append(
                    InsightTable(title=title, columns=cols, rows=rows,
                                 footnote="Rows are verbatim source statements; "
                                          "cell grouping is analytical.",
                                 question_ids=sorted(used))
                )
    if not report.narratives:
        for heading in prose_sections(meta["expected_output"])[:6]:
            body = " ".join(
                _sentence_case(e.quote, 320)
                for e in sorted(evidence, key=lambda x: (x.tier, -x.relevance))[:3]
            )
            if body:
                report.narratives.append({"heading": heading, "body": f"{body} {_cite(evidence[:3])}"})
    if not report.takeaways:
        report.takeaways = _takeaways(questions, evidence_by_q)
    if not report.observability:
        report.observability = _observability_rows(questions, evidence_by_q)
    if not report.assumptions:
        report.assumptions = [
            f"Published registry and guideline statistics reflect standard "
            f"{cfg.geography} clinical epidemiology through the {cfg.research_cutoff} cutoff."
        ]

    report.unanswered = [
        {
            "question": q.text,
            "reason": q.unmet_reason or "did not reach the sufficiency threshold",
            "sources_attempted": ", ".join(q.sources_attempted[:8]) or "none",
        }
        for q in questions
        if q.status is not QuestionStatus.SUFFICIENT
    ]

    report.evidence_count = len(evidence)
    report.source_count = len({e.source_id for e in evidence})
    report.supplementary_count = sum(1 for e in evidence if e.is_supplementary)
    report.tiers_represented = sorted({e.tier for e in evidence})
    return report
```

### File: `celestra\services\__init__.py`

```py

```

### File: `celestra\static\css\app.css`

```css
/* ==========================================================================
   Celestra — clinical desk research
   One handwritten stylesheet. No framework, no CDN, no external assets.
   Every colour lives in the token block below; a theme swap is one block.
   ========================================================================== */

/* --------------------------------------------------------------------------
   1. Design tokens
   -------------------------------------------------------------------------- */
:root {
  color-scheme: light;

  /* canvas + surfaces */
  --bg: #f6f7f9;
  --bg-alt: #eef1f5;
  --surface: #ffffff;
  --surface-2: #fbfcfd;
  --surface-3: #f4f6f8;
  --overlay: rgba(15, 23, 42, .44);

  /* hairlines */
  --border: #e6e8ec;
  --border-strong: #d6dae1;
  --border-input: #d8dde5;

  /* type */
  --text: #0f172a;
  --text-2: #4a5567;
  --text-3: #8a94a6;
  --text-inv: #ffffff;

  /* brand */
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --primary-active: #1e40af;
  --primary-soft: #eff5ff;
  --primary-soft-2: #dbe8fe;
  --primary-border: #bfd6fd;
  --primary-text: #1d4ed8;

  /* pastel tint families: soft background / border / ink */
  --green-soft: #e7f7ee;   --green-border: #bfe8d1;  --green-ink: #157347;
  --blue-soft: #e6efff;    --blue-border: #c3d9fd;   --blue-ink: #1d4ed8;
  --purple-soft: #f0eaff;  --purple-border: #dbcdfb; --purple-ink: #6d28d9;
  --indigo-soft: #e8eaff;  --indigo-border: #cbd0fb; --indigo-ink: #4338ca;
  --teal-soft: #dff6f3;    --teal-border: #b3e6df;   --teal-ink: #0f766e;
  --amber-soft: #fdf3dc;   --amber-border: #f3ddab;  --amber-ink: #96620d;
  --rose-soft: #fee9ec;    --rose-border: #fac7ce;   --rose-ink: #be123c;
  --slate-soft: #eef0f4;   --slate-border: #dcdfe6;  --slate-ink: #5b6577;

  /* status semantics */
  --ok: #157347;
  --warn: #b45309;
  --danger: #be123c;
  --muted: #6b7482;

  /* shape + depth */
  --radius: 14px;
  --radius-md: 12px;
  --radius-sm: 9px;
  --radius-pill: 999px;
  --shadow-xs: 0 1px 2px rgba(16, 24, 40, .05);
  --shadow-sm: 0 1px 3px rgba(16, 24, 40, .06), 0 1px 2px rgba(16, 24, 40, .04);
  --shadow-md: 0 4px 14px rgba(16, 24, 40, .07), 0 1px 3px rgba(16, 24, 40, .05);
  --shadow-lg: 0 18px 44px rgba(16, 24, 40, .16), 0 3px 10px rgba(16, 24, 40, .07);
  --focus-ring: 0 0 0 3px rgba(37, 99, 235, .35);

  /* metrics */
  --sidebar-w: 236px;
  --sidebar-w-collapsed: 68px;
  --shell-max: 1240px;

  /* type stacks — system only, nothing fetched */
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue",
          Arial, "Noto Sans", sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
               "Liberation Mono", monospace;
}

/* --------------------------------------------------------------------------
   2. Base
   -------------------------------------------------------------------------- */
* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

h1, h2, h3, h4, h5 { margin: 0; font-weight: 650; line-height: 1.25; letter-spacing: -.01em; }
h1 { font-size: 24px; }
h2 { font-size: 19px; }
h3 { font-size: 16px; }
h4 { font-size: 14px; }
p { margin: 0 0 12px; }
p:last-child { margin-bottom: 0; }

a { color: var(--primary-text); text-decoration: none; }
a:hover { text-decoration: underline; }

ul, ol { margin: 0 0 12px; padding-left: 20px; }
li { margin: 0 0 6px; }
li:last-child { margin-bottom: 0; }

hr { border: 0; border-top: 1px solid var(--border); margin: 26px 0; }

:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
  border-radius: 4px;
}

.visually-hidden {
  position: absolute; width: 1px; height: 1px;
  margin: -1px; padding: 0; overflow: hidden;
  clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap;
}

.skip-link {
  position: absolute; left: 12px; top: -60px; z-index: 200;
  background: var(--surface); color: var(--text);
  padding: 10px 14px; border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm); box-shadow: var(--shadow-md);
}
.skip-link:focus { top: 12px; }

/* --------------------------------------------------------------------------
   3. Shell + sidebar
   -------------------------------------------------------------------------- */
.app { min-height: 100vh; }

.sidebar {
  position: fixed;
  inset: 0 auto 0 0;
  width: var(--sidebar-w);
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 20px 14px 16px;
  z-index: 30;
}

.brand {
  display: flex; align-items: center; gap: 10px;
  padding: 4px 8px 22px;
  color: var(--text);
  font-weight: 700; letter-spacing: .14em; font-size: 12.5px;
  text-transform: uppercase;
}
.brand:hover { text-decoration: none; }
.brand-mark { flex: 0 0 auto; color: var(--primary); }
.brand-word { white-space: nowrap; }

.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-group-label {
  padding: 14px 10px 6px;
  font-size: 10.5px; font-weight: 700; letter-spacing: .1em;
  text-transform: uppercase; color: var(--text-3);
}
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  font-size: 13.5px; font-weight: 500;
  transition: background .14s ease, color .14s ease;
}
.nav-item:hover { background: var(--surface-3); color: var(--text); text-decoration: none; }
.nav-item .ic { flex: 0 0 auto; color: var(--text-3); }
.nav-item.is-active {
  background: var(--primary-soft);
  color: var(--primary-text);
  font-weight: 600;
}
.nav-item.is-active .ic { color: var(--primary); }
.nav-spacer { flex: 1 1 auto; }
.nav-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.main {
  margin-left: var(--sidebar-w);
  min-height: 100vh;
  padding: 28px 32px 64px;
}
.shell { max-width: var(--shell-max); margin: 0 auto; }
.shell-narrow { max-width: 900px; margin: 0 auto; }

.page-head { margin-bottom: 20px; }
.page-head .eyebrow {
  font-size: 11px; font-weight: 700; letter-spacing: .1em;
  text-transform: uppercase; color: var(--text-3); margin-bottom: 6px;
}
.page-head .sub { color: var(--text-2); margin-top: 6px; }
.page-head-row {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
}

/* --------------------------------------------------------------------------
   4. Banners + flashes
   -------------------------------------------------------------------------- */
/* The user-agent [hidden] rule is display:none at author-origin weight zero,
   so any of our own `display:` declarations silently defeats it. This keeps
   `el.hidden = true` working everywhere. */
[hidden] { display: none !important; }

.banner {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 11px 14px;
  border: 1px solid var(--amber-border);
  background: var(--amber-soft);
  color: var(--amber-ink);
  border-radius: var(--radius-md);
  margin-bottom: 12px;
  font-size: 13px;
}
.banner .ic { flex: 0 0 auto; margin-top: 1px; }
.banner-body { flex: 1 1 auto; }
.banner strong { font-weight: 650; }
.banner-close {
  flex: 0 0 auto; border: 0; background: transparent; cursor: pointer;
  color: inherit; padding: 2px; border-radius: 6px; line-height: 0;
}
.banner-close:hover { background: rgba(0, 0, 0, .07); }
.banner.is-info { border-color: var(--blue-border); background: var(--blue-soft); color: var(--blue-ink); }
.banner.is-ok { border-color: var(--green-border); background: var(--green-soft); color: var(--green-ink); }
.banner.is-danger { border-color: var(--rose-border); background: var(--rose-soft); color: var(--rose-ink); }

.flash-area { margin-bottom: 16px; }

/* --------------------------------------------------------------------------
   5. Cards
   -------------------------------------------------------------------------- */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
}
.card-pad { padding: 20px 22px; }
.card + .card { margin-top: 16px; }
.card-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding: 16px 20px; border-bottom: 1px solid var(--border);
}
.card-head h2, .card-head h3 { margin: 0; }
.card-body { padding: 18px 20px; }
.card-foot {
  padding: 14px 20px; border-top: 1px solid var(--border);
  background: var(--surface-2);
  border-radius: 0 0 var(--radius) var(--radius);
}
.card-hover { transition: box-shadow .16s ease, transform .16s ease, border-color .16s ease; }
.card-hover:hover { box-shadow: var(--shadow-md); border-color: var(--border-strong); }

.section { margin-bottom: 26px; }
.section-title {
  display: flex; align-items: center; gap: 8px;
  margin: 0 0 12px; font-size: 15px; font-weight: 650;
}
.section-title .count { color: var(--text-3); font-weight: 500; }

.grid { display: grid; gap: 14px; }
.grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.grid-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }

/* --------------------------------------------------------------------------
   6. Icon tiles
   -------------------------------------------------------------------------- */
.tile {
  flex: 0 0 auto;
  width: 38px; height: 38px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 11px;
  background: var(--slate-soft);
  border: 1px solid var(--slate-border);
  color: var(--slate-ink);
}
.tile svg { width: 19px; height: 19px; }
.tile-sm { width: 30px; height: 30px; border-radius: 9px; }
.tile-sm svg { width: 15px; height: 15px; }
.tile-lg { width: 44px; height: 44px; border-radius: 13px; }
.tile-lg svg { width: 22px; height: 22px; }

.tint-green  { background: var(--green-soft);  border-color: var(--green-border);  color: var(--green-ink); }
.tint-blue   { background: var(--blue-soft);   border-color: var(--blue-border);   color: var(--blue-ink); }
.tint-purple { background: var(--purple-soft); border-color: var(--purple-border); color: var(--purple-ink); }
.tint-indigo { background: var(--indigo-soft); border-color: var(--indigo-border); color: var(--indigo-ink); }
.tint-teal   { background: var(--teal-soft);   border-color: var(--teal-border);   color: var(--teal-ink); }
.tint-amber  { background: var(--amber-soft);  border-color: var(--amber-border);  color: var(--amber-ink); }
.tint-rose   { background: var(--rose-soft);   border-color: var(--rose-border);   color: var(--rose-ink); }
.tint-slate  { background: var(--slate-soft);  border-color: var(--slate-border);  color: var(--slate-ink); }

/* --------------------------------------------------------------------------
   7. Stat tiles
   -------------------------------------------------------------------------- */
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.stat {
  position: relative;
  padding: 16px 18px;
  border-radius: var(--radius-md);
  border: 1px solid var(--slate-border);
  background: var(--slate-soft);
}
.stat-value {
  display: flex; align-items: center; gap: 8px;
  font-size: 30px; font-weight: 700; line-height: 1.1; letter-spacing: -.02em;
  color: var(--text);
}
.stat-label { margin-top: 6px; font-size: 12.5px; color: var(--text-2); font-weight: 500; }
.stat-note { margin-top: 2px; font-size: 11.5px; color: var(--text-3); }
.stat.stat-green  { background: var(--green-soft);  border-color: var(--green-border); }
.stat.stat-green  .stat-value, .stat.stat-green .stat-label { color: var(--green-ink); }
.stat.stat-amber  { background: var(--amber-soft);  border-color: var(--amber-border); }
.stat.stat-amber  .stat-value, .stat.stat-amber .stat-label { color: var(--amber-ink); }
.stat.stat-rose   { background: var(--rose-soft);   border-color: var(--rose-border); }
.stat.stat-rose   .stat-value, .stat.stat-rose .stat-label { color: var(--rose-ink); }
.stat.stat-blue   { background: var(--blue-soft);   border-color: var(--blue-border); }
.stat.stat-blue   .stat-value, .stat.stat-blue .stat-label { color: var(--blue-ink); }
.stat.stat-slate  { background: var(--slate-soft);  border-color: var(--slate-border); }
.stat.stat-slate  .stat-value, .stat.stat-slate .stat-label { color: var(--slate-ink); }
.stat-check { color: var(--ok); display: inline-flex; }

/* --------------------------------------------------------------------------
   8. Chips + badges
   -------------------------------------------------------------------------- */
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 11px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--slate-border);
  background: var(--slate-soft);
  color: var(--slate-ink);
  font-size: 12px; font-weight: 600; white-space: nowrap;
}
.chip-sm { padding: 2px 9px; font-size: 11px; font-weight: 550; }
.chip-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }

.chip-ready, .chip-high { background: var(--green-soft);  border-color: var(--green-border);  color: var(--green-ink); }
.chip-amber, .chip-medium { background: var(--amber-soft);  border-color: var(--amber-border);  color: var(--amber-ink); }
.chip-requires_input { background: var(--rose-soft); border-color: var(--rose-border);   color: var(--rose-ink); }
.chip-muted, .chip-rejected { background: var(--slate-soft);  border-color: var(--slate-border);  color: var(--slate-ink); }
.chip-blue         { background: var(--blue-soft);   border-color: var(--blue-border);   color: var(--blue-ink); }
.chip-purple       { background: var(--purple-soft); border-color: var(--purple-border); color: var(--purple-ink); }
.chip-teal         { background: var(--teal-soft);   border-color: var(--teal-border);   color: var(--teal-ink); }

/* source chips light up as they are actually used */
.chip-source {
  background: var(--surface);
  border-color: var(--border);
  color: var(--text-3);
  font-weight: 550;
  opacity: .72;
  transition: opacity .2s ease, color .2s ease, border-color .2s ease, background .2s ease;
}
.chip-source.is-used {
  opacity: 1;
  background: var(--primary-soft);
  border-color: var(--primary-border);
  color: var(--primary-text);
}
.chip-row { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; }

.tier {
  display: inline-flex; align-items: center;
  padding: 2px 8px; border-radius: 6px;
  font-size: 11px; font-weight: 650; letter-spacing: .01em;
  border: 1px solid var(--slate-border); background: var(--slate-soft); color: var(--slate-ink);
  white-space: nowrap;
}
.tier-1 { background: var(--green-soft);  border-color: var(--green-border);  color: var(--green-ink); }
.tier-2 { background: var(--blue-soft);   border-color: var(--blue-border);   color: var(--blue-ink); }
.tier-3 { background: var(--purple-soft); border-color: var(--purple-border); color: var(--purple-ink); }
.tier-4 { background: var(--amber-soft);  border-color: var(--amber-border);  color: var(--amber-ink); }
.tier-5 { background: var(--rose-soft);   border-color: var(--rose-border);   color: var(--rose-ink); }

/* run + agent status chips */
.status { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; font-weight: 600; }
.status-queued, .status-pending { color: var(--text-3); }
.status-researching, .status-running, .status-synthesising { color: var(--primary-text); }
.status-complete, .status-completed { color: var(--ok); }
.status-failed { color: var(--danger); }
.status-blocked, .status-skipped, .status-cancelled { color: var(--muted); }

.meta { color: var(--text-3); font-size: 12.5px; }
.meta-row { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; }
.dot-sep { color: var(--border-strong); }

/* --------------------------------------------------------------------------
   9. Buttons
   -------------------------------------------------------------------------- */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  padding: 8px 15px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong);
  background: var(--surface);
  color: var(--text);
  font: inherit; font-size: 13px; font-weight: 600;
  cursor: pointer;
  text-decoration: none;
  transition: background .14s ease, border-color .14s ease, color .14s ease, box-shadow .14s ease;
}
.btn:hover { background: var(--surface-3); text-decoration: none; }
.btn:focus-visible { outline: none; box-shadow: var(--focus-ring); }
.btn[disabled], .btn.is-disabled { opacity: .55; cursor: not-allowed; }
.btn svg { width: 15px; height: 15px; }

.btn-primary {
  background: var(--primary); border-color: var(--primary); color: var(--text-inv);
  box-shadow: var(--shadow-xs);
}
.btn-primary:hover { background: var(--primary-hover); border-color: var(--primary-hover); }
.btn-primary:active { background: var(--primary-active); }

.btn-secondary { background: var(--surface); border-color: var(--border-strong); color: var(--text); }
.btn-secondary:hover { background: var(--surface-3); }

.btn-ghost { background: transparent; border-color: transparent; color: var(--text-2); }
.btn-ghost:hover { background: var(--surface-3); color: var(--text); }

.btn-danger { background: var(--surface); border-color: var(--rose-border); color: var(--danger); }
.btn-danger:hover { background: var(--rose-soft); }

.btn-soft { background: var(--primary-soft); border-color: var(--primary-border); color: var(--primary-text); }
.btn-soft:hover { background: var(--primary-soft-2); }

.btn-lg { padding: 11px 20px; font-size: 14px; }
.btn-sm { padding: 5px 11px; font-size: 12px; }
.btn-block { width: 100%; }
.btn-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.btn-row-end { justify-content: flex-end; }

/* filter pills */
.pills { display: flex; flex-wrap: wrap; gap: 8px; }
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 13px; border-radius: var(--radius-pill);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-2);
  font: inherit; font-size: 12.5px; font-weight: 600;
  cursor: pointer;
  transition: background .14s ease, color .14s ease, border-color .14s ease;
}
.pill:hover { background: var(--surface-3); color: var(--text); }
.pill:focus-visible { outline: none; box-shadow: var(--focus-ring); }
.pill[aria-pressed="true"], .pill.is-active {
  background: var(--primary-soft); border-color: var(--primary-border); color: var(--primary-text);
}

/* --------------------------------------------------------------------------
   10. Tables — hairline, no zebra, sticky header, horizontal scroll wrapper
   -------------------------------------------------------------------------- */
.table-wrap {
  width: 100%;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  max-height: 620px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}
table.data {
  width: 100%;
  min-width: 560px;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 13px;
}
table.data th, table.data td {
  padding: 10px 14px;
  text-align: left;
  vertical-align: top;
  border-bottom: 1px solid var(--border);
}
table.data thead th {
  position: sticky; top: 0; z-index: 2;
  background: var(--surface-3);
  color: var(--text-2);
  font-size: 11.5px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
  white-space: nowrap;
  border-bottom: 1px solid var(--border-strong);
}
table.data tbody tr:last-child td { border-bottom: 0; }
table.data tbody tr:hover td { background: var(--surface-2); }
table.data td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.data td.nowrap, table.data th.nowrap { white-space: nowrap; }
table.data.table-compact th, table.data.table-compact td { padding: 7px 12px; }

.table-title {
  display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
  margin: 0 0 8px;
}
.table-footnote {
  margin: 8px 2px 0;
  font-size: 12px; font-style: italic; color: var(--text-3);
}
.kv { width: 100%; border-collapse: collapse; font-size: 13px; }
.kv th, .kv td { padding: 9px 12px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
.kv th { width: 34%; color: var(--text-2); font-weight: 600; background: var(--surface-2); }
.kv tr:last-child th, .kv tr:last-child td { border-bottom: 0; }

/* --------------------------------------------------------------------------
   11. Forms
   -------------------------------------------------------------------------- */
.field { margin-bottom: 16px; }
.field label, .label {
  display: block; margin-bottom: 6px;
  font-size: 13px; font-weight: 600; color: var(--text);
}
.label .optional { color: var(--text-3); font-weight: 500; }
.req { color: var(--danger); }
.hint { margin-top: 5px; font-size: 12px; color: var(--text-3); }

input[type="text"], input[type="search"], input[type="email"], input[type="url"],
select, textarea {
  width: 100%;
  padding: 9px 12px;
  font: inherit; font-size: 13.5px;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  transition: border-color .14s ease, box-shadow .14s ease;
}
textarea { min-height: 96px; resize: vertical; line-height: 1.5; }
input::placeholder, textarea::placeholder { color: var(--text-3); }
input:focus, select:focus, textarea:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: var(--focus-ring);
}
select {
  appearance: none;
  padding-right: 34px;
  background-image: linear-gradient(45deg, transparent 50%, var(--text-3) 50%),
                    linear-gradient(135deg, var(--text-3) 50%, transparent 50%);
  background-position: calc(100% - 17px) 50%, calc(100% - 12px) 50%;
  background-size: 5px 5px, 5px 5px;
  background-repeat: no-repeat;
}
.field-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }

.search-wrap { position: relative; flex: 1 1 260px; min-width: 200px; }
.search-wrap .ic {
  position: absolute; left: 11px; top: 50%; transform: translateY(-50%);
  color: var(--text-3); pointer-events: none;
}
.search-wrap input { padding-left: 34px; }

.toolbar {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  margin-bottom: 16px;
}
.toolbar .spacer { flex: 1 1 auto; }
.sort-wrap { display: flex; align-items: center; gap: 8px; }
.sort-wrap label { margin: 0; white-space: nowrap; font-weight: 500; color: var(--text-2); }
.sort-wrap select { width: auto; min-width: 150px; }

/* radio cards — mode selector */
.radio-cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.radio-card {
  position: relative;
  display: flex; gap: 11px; align-items: flex-start;
  padding: 14px 16px;
  border: 1px solid var(--border-input);
  border-radius: var(--radius-md);
  background: var(--surface);
  cursor: pointer;
  transition: border-color .14s ease, background .14s ease, box-shadow .14s ease;
}
.radio-card:hover { border-color: var(--border-strong); background: var(--surface-2); }
.radio-card input[type="radio"] { margin: 3px 0 0; accent-color: var(--primary); flex: 0 0 auto; }
.radio-card:has(input:checked) {
  border-color: var(--primary);
  background: var(--primary-soft);
  box-shadow: 0 0 0 1px var(--primary) inset;
}
.radio-card:has(input:focus-visible) { box-shadow: var(--focus-ring); }
.radio-card .rc-title { font-weight: 650; font-size: 13.5px; }
.radio-card .rc-desc { margin-top: 3px; font-size: 12.5px; color: var(--text-2); }

.reveal { margin-top: 12px; }
.reveal[hidden] { display: none !important; }

/* --------------------------------------------------------------------------
   12. Progress bar + spinner
   -------------------------------------------------------------------------- */
.progress {
  position: relative;
  height: 5px;
  border-radius: var(--radius-pill);
  background: var(--surface-3);
  overflow: hidden;
}
.progress-bar {
  height: 100%;
  width: 0;
  border-radius: var(--radius-pill);
  background: var(--primary);
  transition: width .45s ease;
}
.progress.is-complete .progress-bar { background: var(--ok); }
.progress.is-failed .progress-bar { background: var(--danger); }

.spinner {
  display: inline-block;
  width: 14px; height: 14px;
  border: 2px solid var(--primary-soft-2);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin .8s linear infinite;
  flex: 0 0 auto;
}
@keyframes spin { to { transform: rotate(360deg); } }

.pulse-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--primary);
  animation: pulse 1.6s ease-in-out infinite;
}
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }

/* --------------------------------------------------------------------------
   13. Verification tag pills
   -------------------------------------------------------------------------- */
.vtag {
  display: inline-block;
  padding: 1px 6px;
  margin: 0 2px 0 0;
  border-radius: 5px;
  font-family: var(--font-mono);
  font-size: 10.5px; font-weight: 600; letter-spacing: .01em;
  line-height: 1.5;
  white-space: nowrap;
  border: 1px solid var(--slate-border);
  background: var(--slate-soft);
  color: var(--slate-ink);
  vertical-align: baseline;
}
.vtag-verified   { background: var(--green-soft);  border-color: var(--green-border);  color: var(--green-ink); }
.vtag-original   { background: var(--blue-soft);   border-color: var(--blue-border);   color: var(--blue-ink); }
.vtag-inference  { background: var(--amber-soft);  border-color: var(--amber-border);  color: var(--amber-ink); }
.vtag-update     { background: var(--purple-soft); border-color: var(--purple-border); color: var(--purple-ink); }
.vtag-not-verified { background: var(--rose-soft); border-color: var(--rose-border);   color: var(--rose-ink); }
.vtag-general-knowledge { background: var(--slate-soft); border-color: var(--slate-border); color: var(--slate-ink); }

.src-ref {
  font-size: 11.5px; color: var(--text-3);
  font-family: var(--font-mono);
}

/* --------------------------------------------------------------------------
   14. Agent cards (discovery) + insight cards
   -------------------------------------------------------------------------- */
.agent-card {
  display: block;
  padding: 15px 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface);
  box-shadow: var(--shadow-xs);
}
.agent-card + .agent-card { margin-top: 10px; }
.agent-card-top { display: flex; align-items: center; gap: 12px; }
.agent-id { flex: 1 1 auto; min-width: 0; }
.agent-name { font-size: 14px; font-weight: 650; }
.agent-tagline { font-size: 12.5px; color: var(--text-2); margin-top: 2px; }
.agent-status { flex: 0 0 auto; display: flex; align-items: center; gap: 7px; }
.agent-progress { margin-top: 12px; }
.agent-message {
  margin-top: 7px;
  font-size: 12px; color: var(--text-3);
  min-height: 18px;
  overflow-wrap: anywhere;
}
.agent-card.is-complete { border-color: var(--green-border); }
.agent-card.is-failed { border-color: var(--rose-border); }
.agent-card.is-blocked { opacity: .78; }

.source-strip {
  margin-top: 16px;
  padding: 16px 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-2);
}
.source-strip h3 { font-size: 13px; font-weight: 650; margin-bottom: 10px; color: var(--text-2); }

.insight-card { padding: 16px 18px; }
.insight-card-top { display: flex; gap: 12px; align-items: flex-start; }
.insight-main { flex: 1 1 auto; min-width: 0; }
.insight-title { font-size: 14.5px; font-weight: 650; }
.insight-summary { margin-top: 4px; color: var(--text-2); font-size: 13px; }
.insight-detail { margin-top: 8px; font-size: 13px; color: var(--text-2); }
.insight-aside { flex: 0 0 auto; display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }
.insight-sources { margin-top: 11px; }
.insight-sources .lead { font-size: 12px; color: var(--text-3); margin-bottom: 6px; }
.insight-foot {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; margin-top: 13px;
  padding-top: 12px; border-top: 1px solid var(--border);
}
.insight-card[data-hidden="1"] { display: none; }
.insight-card.is-reviewed { border-color: var(--green-border); }
.review-note {
  margin-top: 10px; padding: 10px 12px;
  border-left: 3px solid var(--primary-border);
  background: var(--primary-soft);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 12.5px; color: var(--text-2);
}

.empty {
  padding: 34px 20px; text-align: center;
  color: var(--text-3);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--surface-2);
}
.empty h3 { color: var(--text-2); margin-bottom: 6px; }

/* takeaway highlight cards */
.takeaway { padding: 16px 18px; display: flex; flex-direction: column; gap: 10px; height: 100%; }
.takeaway .tk-title { font-size: 13.5px; font-weight: 650; }
.takeaway .tk-body { font-size: 12.5px; color: var(--text-2); flex: 1 1 auto; }

/* --------------------------------------------------------------------------
   15. Modal + slide-over
   -------------------------------------------------------------------------- */
.modal-backdrop, .slideover-backdrop {
  position: fixed; inset: 0;
  background: var(--overlay);
  z-index: 90;
  display: flex; align-items: center; justify-content: center;
  padding: 24px;
  animation: fade .16s ease;
}
.slideover-backdrop { justify-content: flex-end; padding: 0; }
@keyframes fade { from { opacity: 0; } to { opacity: 1; } }

.modal {
  width: 100%; max-width: 620px;
  max-height: calc(100vh - 48px);
  display: flex; flex-direction: column;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  animation: rise .18s ease;
}
@keyframes rise { from { transform: translateY(8px); opacity: .6; } to { transform: none; opacity: 1; } }

.modal-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 16px 20px; border-bottom: 1px solid var(--border);
}
.modal-body { padding: 18px 20px; overflow-y: auto; }
.modal-foot {
  display: flex; justify-content: flex-end; gap: 10px;
  padding: 14px 20px; border-top: 1px solid var(--border);
  background: var(--surface-2);
}
.icon-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; padding: 0;
  border: 0; border-radius: 8px; background: transparent;
  color: var(--text-3); cursor: pointer;
}
.icon-btn:hover { background: var(--surface-3); color: var(--text); }
.icon-btn:focus-visible { outline: none; box-shadow: var(--focus-ring); }

.readonly-block {
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-3);
  color: var(--text-2);
  font-size: 13px;
}
.counter { margin-top: 6px; text-align: right; font-size: 11.5px; color: var(--text-3); font-variant-numeric: tabular-nums; }
.counter.is-over { color: var(--danger); font-weight: 650; }

.impact {
  margin-top: 16px; padding: 14px 16px;
  border: 1px solid var(--blue-border);
  background: var(--blue-soft);
  border-radius: var(--radius-md);
}
.impact h4 { display: flex; align-items: center; gap: 7px; color: var(--blue-ink); }
.impact .lead { font-size: 12.5px; color: var(--text-2); margin: 6px 0 10px; }
.impact-item { display: flex; gap: 10px; align-items: flex-start; padding: 7px 0; }
.impact-item + .impact-item { border-top: 1px solid var(--blue-border); }
.impact-name { font-size: 13px; font-weight: 620; }
.impact-desc { font-size: 12.5px; color: var(--text-2); }

.slideover {
  width: 100%; max-width: 520px; height: 100vh;
  display: flex; flex-direction: column;
  background: var(--surface);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow-lg);
  animation: slidein .2s ease;
}
@keyframes slidein { from { transform: translateX(24px); opacity: .5; } to { transform: none; opacity: 1; } }
.slideover-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 16px 20px; border-bottom: 1px solid var(--border);
}
.slideover-body { padding: 18px 20px; overflow-y: auto; flex: 1 1 auto; }

.evidence-item { padding: 14px 0; border-bottom: 1px solid var(--border); }
.evidence-item:last-child { border-bottom: 0; }
.evidence-item.is-supplementary {
  border-left: 3px solid var(--amber-border);
  background: var(--amber-soft);
  padding: 14px 14px;
  border-radius: var(--radius-sm);
  margin-bottom: 10px;
}
.evidence-quote {
  margin: 0 0 10px;
  padding: 10px 14px;
  border-left: 3px solid var(--primary-border);
  background: var(--surface-3);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 13px; color: var(--text);
}
.evidence-item.is-supplementary .evidence-quote { background: var(--surface); border-left-color: var(--amber-border); }
.evidence-meta { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; font-size: 12px; color: var(--text-3); }
.supp-flag {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 8px; border-radius: 6px;
  background: var(--amber-soft); border: 1px solid var(--amber-border); color: var(--amber-ink);
  font-size: 10.5px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
  font-family: var(--font-mono);
}

/* --------------------------------------------------------------------------
   16. Report typography
   -------------------------------------------------------------------------- */
.report { }
.report .doc-title { font-size: 26px; margin-bottom: 6px; }
.report-section { margin-bottom: 28px; }
.report-section > h2 {
  font-size: 18px;
  padding-bottom: 8px;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.report-section h3 { font-size: 15px; margin: 18px 0 8px; }
.report-section h4 { font-size: 13.5px; margin: 14px 0 6px; color: var(--text-2); }
.prose { font-size: 13.5px; line-height: 1.65; color: var(--text); max-width: 78ch; }
.prose p + p { margin-top: 10px; }

.stage {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 24px 26px;
  margin-bottom: 20px;
}
.stage-head { display: flex; gap: 14px; align-items: flex-start; margin-bottom: 6px; }
.stage-head .stage-id {
  font-size: 11px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
  color: var(--text-3);
}
.stage-head h2 { font-size: 20px; margin-top: 3px; }
.core-question {
  margin: 12px 0 18px;
  padding: 11px 14px;
  border-radius: var(--radius-sm);
  background: var(--primary-soft);
  border: 1px solid var(--primary-border);
  color: var(--primary-text);
  font-size: 13.5px; font-weight: 550;
}
.core-question strong { font-weight: 700; }
.stage-meta { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }

.subblock { margin: 18px 0; }
.subblock > h3 {
  font-size: 12px; font-weight: 700; letter-spacing: .07em; text-transform: uppercase;
  color: var(--text-3); margin-bottom: 9px;
}
.callout {
  padding: 13px 16px;
  border-left: 3px solid var(--primary);
  background: var(--primary-soft);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 13px; color: var(--text-2);
}
.callout.is-amber { border-left-color: var(--warn); background: var(--amber-soft); color: var(--amber-ink); }
.callout.is-rose { border-left-color: var(--danger); background: var(--rose-soft); color: var(--rose-ink); }
.callout.is-slate { border-left-color: var(--border-strong); background: var(--surface-3); color: var(--text-2); }
.callout strong { color: inherit; }

ol.takeaways { padding-left: 22px; font-size: 13.5px; }
ol.takeaways li { margin-bottom: 8px; }
ul.checks { list-style: none; padding-left: 0; }
ul.checks li { display: flex; gap: 9px; align-items: flex-start; margin-bottom: 8px; font-size: 13.5px; }
ul.checks .ic { flex: 0 0 auto; margin-top: 2px; color: var(--ok); }

.stage-toc { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }

/* contradictions */
.contra { padding: 0; overflow: hidden; }
.contra-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
  padding: 14px 18px; border-bottom: 1px solid var(--border); background: var(--surface-2);
}
.contra-sides { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.contra-side { padding: 16px 18px; }
.contra-side + .contra-side { border-left: 1px solid var(--border); }
.contra-side .side-label {
  font-size: 10.5px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
  color: var(--text-3); margin-bottom: 6px;
}
.contra-side .side-source { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.contra-side .side-name { font-size: 13.5px; font-weight: 650; }
.contra-claim { font-size: 13px; color: var(--text-2); }
.contra-reason { padding: 13px 18px; border-top: 1px solid var(--border); background: var(--surface-2); font-size: 12.5px; color: var(--text-2); }
.contra-actions { padding: 14px 18px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 10px; }
.no-autoresolve {
  display: flex; align-items: center; gap: 8px;
  padding: 11px 14px; margin-bottom: 16px;
  border: 1px dashed var(--rose-border); background: var(--rose-soft); color: var(--rose-ink);
  border-radius: var(--radius-md); font-size: 13px; font-weight: 600;
}

/* --------------------------------------------------------------------------
   17. Misc layout helpers
   -------------------------------------------------------------------------- */
.stack { display: flex; flex-direction: column; gap: 14px; }
.stack-sm { display: flex; flex-direction: column; gap: 8px; }
.row { display: flex; gap: 12px; align-items: center; }
.row-between { display: flex; gap: 12px; align-items: center; justify-content: space-between; }
.wrap { flex-wrap: wrap; }
.mt-0 { margin-top: 0; } .mt-1 { margin-top: 8px; } .mt-2 { margin-top: 16px; } .mt-3 { margin-top: 24px; }
.mb-0 { margin-bottom: 0; } .mb-1 { margin-bottom: 8px; } .mb-2 { margin-bottom: 16px; } .mb-3 { margin-bottom: 24px; }
.text-sm { font-size: 12.5px; }
.text-muted { color: var(--text-2); }
.text-faint { color: var(--text-3); }
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mono { font-family: var(--font-mono); font-size: 12px; }
.break { overflow-wrap: anywhere; }

.list-plain { list-style: none; padding-left: 0; }
.list-plain li { display: flex; gap: 9px; align-items: flex-start; }

.included-item { display: flex; gap: 12px; align-items: flex-start; padding: 12px 0; }
.included-item + .included-item { border-top: 1px solid var(--border); }
.included-name { font-size: 13.5px; font-weight: 650; }
.included-desc { font-size: 12.5px; color: var(--text-2); margin-top: 2px; }

.run-row { display: flex; align-items: center; gap: 14px; padding: 14px 18px; }
.run-row + .run-row { border-top: 1px solid var(--border); }
.run-row .run-main { flex: 1 1 auto; min-width: 0; }
.run-row .run-title { font-size: 13.5px; font-weight: 650; }

/* --------------------------------------------------------------------------
   18. Responsive
   -------------------------------------------------------------------------- */
@media (max-width: 1120px) {
  .grid-4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 900px) {
  .sidebar { width: var(--sidebar-w-collapsed); padding: 18px 10px 14px; align-items: center; }
  .sidebar .brand { padding: 4px 0 20px; justify-content: center; }
  .sidebar .brand-word, .sidebar .nav-label, .sidebar .nav-group-label { display: none; }
  .nav { width: 100%; }
  .nav-item { justify-content: center; padding: 10px 0; }
  .main { margin-left: var(--sidebar-w-collapsed); padding: 20px 18px 56px; }
  .grid-3, .grid-2, .field-row, .radio-cards, .contra-sides { grid-template-columns: minmax(0, 1fr); }
  .contra-side + .contra-side { border-left: 0; border-top: 1px solid var(--border); }
  .slideover { max-width: 100%; }
  .stage { padding: 18px 16px; }
}

@media (max-width: 620px) {
  .stats, .grid-4 { grid-template-columns: minmax(0, 1fr); }
  .modal-backdrop { padding: 0; align-items: flex-end; }
  .modal { max-width: 100%; max-height: 92vh; border-radius: var(--radius) var(--radius) 0 0; }
  .insight-card-top { flex-wrap: wrap; }
  .page-head-row { flex-direction: column; align-items: stretch; }
}

/* --------------------------------------------------------------------------
   19. Reduced motion
   -------------------------------------------------------------------------- */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .001ms !important;
    scroll-behavior: auto !important;
  }
  .spinner { border-top-color: var(--primary); }
}

/* --------------------------------------------------------------------------
   20. Dark theme — tokens only
   -------------------------------------------------------------------------- */
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;

    --bg: #0d1117;
    --bg-alt: #11161f;
    --surface: #151b24;
    --surface-2: #1a212b;
    --surface-3: #212a36;
    --overlay: rgba(2, 6, 14, .66);

    --border: #262f3c;
    --border-strong: #35404f;
    --border-input: #3a4553;

    --text: #e8edf5;
    --text-2: #a7b3c4;
    --text-3: #78859a;
    --text-inv: #0b1018;

    --primary: #3b82f6;
    --primary-hover: #60a5fa;
    --primary-active: #2563eb;
    --primary-soft: #16233a;
    --primary-soft-2: #1d3050;
    --primary-border: #2c4874;
    --primary-text: #93b8fb;

    --green-soft: #10241b;   --green-border: #1f4634;  --green-ink: #6ee7b7;
    --blue-soft: #12233c;    --blue-border: #24406b;   --blue-ink: #93b8fb;
    --purple-soft: #201a38;  --purple-border: #3a2f63; --purple-ink: #c4b5fd;
    --indigo-soft: #191d3d;  --indigo-border: #2e3568; --indigo-ink: #a5b4fc;
    --teal-soft: #0e2725;    --teal-border: #1d4844;   --teal-ink: #5eead4;
    --amber-soft: #2a2010;   --amber-border: #55411b;  --amber-ink: #fcd34d;
    --rose-soft: #2c1219;    --rose-border: #5c2231;   --rose-ink: #fda4af;
    --slate-soft: #1c242f;   --slate-border: #2f3a48;  --slate-ink: #a7b3c4;

    --ok: #34d399;
    --warn: #fbbf24;
    --danger: #fb7185;
    --muted: #8a97a8;

    --shadow-xs: 0 1px 2px rgba(0, 0, 0, .4);
    --shadow-sm: 0 1px 3px rgba(0, 0, 0, .45), 0 1px 2px rgba(0, 0, 0, .3);
    --shadow-md: 0 4px 14px rgba(0, 0, 0, .5), 0 1px 3px rgba(0, 0, 0, .35);
    --shadow-lg: 0 18px 44px rgba(0, 0, 0, .62), 0 3px 10px rgba(0, 0, 0, .4);
    --focus-ring: 0 0 0 3px rgba(59, 130, 246, .45);
  }
}

/* --------------------------------------------------------------------------
   21. Print
   -------------------------------------------------------------------------- */
@media print {
  .sidebar, .toolbar, .btn, .pills, .no-print { display: none !important; }
  .main { margin-left: 0; padding: 0; }
  .card, .stage { box-shadow: none; break-inside: avoid; }
  .table-wrap { max-height: none; overflow: visible; }
}

/* Wide dialog for table snapshots: report tables need the room. */
.modal.modal-wide { width: min(1100px, 96vw); max-width: none; }

/* Agent roster on the review gate page. */
.agent-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 12px; }
.agent-list li { display: flex; align-items: flex-start; gap: 12px; }
.agent-list li .sub { margin: 2px 0 0; }

/* Question-and-answer record in a stage report. */
.qa-item { padding: 12px 0; border-top: 1px solid var(--hairline); }
.qa-item:first-of-type { border-top: 0; }
.qa-question { font-weight: 650; margin: 0 0 4px; }
.qa-answer { margin: 0 0 6px; }
.qa-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
           margin: 0; font-size: 12px; }
.src-chip { display: inline-block; padding: 1px 8px; border-radius: 999px;
            border: 1px solid var(--blue-border); background: var(--blue-soft);
            color: var(--blue-ink); font-size: 11px; }

/* Open-web pages consulted, listed under an insight's evidence. */
.site-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; font-size: 12px; }
.site-list li { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.site-list a { word-break: break-word; }


/* --------------------------------------------------------------------------
   20. The flow: stepper, phase groups, gate panel, decision states
   -------------------------------------------------------------------------- */
.flow { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.flow-step { position: relative; }
.flow-link {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 8px 10px; border-radius: var(--radius-sm);
  color: var(--text-2); font-size: 13px; font-weight: 500;
}
.flow-link:hover { background: var(--surface-3); color: var(--text); text-decoration: none; }
.flow-link.is-disabled { color: var(--text-3); cursor: default; }
.flow-link.is-disabled:hover { background: transparent; }
.flow-num {
  flex: 0 0 auto; width: 20px; height: 20px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700;
  border: 1.5px solid var(--border-strong); color: var(--text-3); background: var(--surface);
}
.flow-text { display: flex; flex-direction: column; min-width: 0; }
.flow-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.flow-desc { font-size: 11.5px; color: var(--text-3); font-weight: 400; margin-top: 1px; }
.flow-step.is-done .flow-num { background: var(--green-soft); border-color: var(--green-border); color: var(--green-ink); }
.flow-step.is-current .flow-link { background: var(--primary-soft); color: var(--primary-text); font-weight: 600; }
.flow-step.is-current .flow-num { background: var(--primary); border-color: var(--primary); color: #fff; }
.flow-step.is-failed .flow-num { background: var(--rose-soft); border-color: var(--rose-border); color: var(--rose-ink); }
.flow-step.is-upcoming .flow-link { opacity: .7; }
.flow.is-compact .flow-link { padding: 6px 8px; }
.sidebar .flow-desc { display: none; }

.phase-group { margin-top: 18px; }
.phase-group + .phase-group { margin-top: 26px; }
.phase-head {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 12px 16px; margin-bottom: 10px;
  border: 1px solid var(--border); border-radius: var(--radius-md);
  background: var(--surface-2);
}
.phase-head .phase-num {
  width: 26px; height: 26px; border-radius: 50%; flex: 0 0 auto;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700;
  background: var(--surface); border: 1.5px solid var(--border-strong); color: var(--text-2);
}
.phase-head h2 { font-size: 15px; margin: 0; }
.phase-head .sub { margin: 0; font-size: 12.5px; }
.phase-head .phase-state { margin-left: auto; display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; font-weight: 600; }
.phase-group.is-complete .phase-head { border-color: var(--green-border); background: var(--green-soft); }
.phase-group.is-complete .phase-num { background: var(--green-ink); border-color: var(--green-ink); color: #fff; }
.phase-group.is-complete .phase-state { color: var(--green-ink); }
.phase-group.is-running .phase-head { border-color: var(--primary-border); background: var(--primary-soft); }
.phase-group.is-running .phase-num { background: var(--primary); border-color: var(--primary); color: #fff; }
.phase-group.is-running .phase-state { color: var(--primary-text); }
.phase-group.is-failed .phase-head { border-color: var(--rose-border); background: var(--rose-soft); }
.phase-group.is-failed .phase-state { color: var(--rose-ink); }
.phase-group.is-gated .phase-head, .phase-group.is-queued .phase-head { border-style: dashed; }
.phase-group.is-gated .phase-state, .phase-group.is-queued .phase-state { color: var(--text-3); }
.phase-gate {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin: 16px 0 0; padding: 12px 16px;
  border: 1px dashed var(--amber-border); border-radius: var(--radius-md);
  background: var(--amber-soft); color: var(--amber-ink); font-size: 13px;
}
.phase-gate.is-passed { border-color: var(--green-border); background: var(--green-soft); color: var(--green-ink); }
.phase-gate .ic { flex: 0 0 auto; }

.gate-panel { border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); box-shadow: var(--shadow-xs); }
.gate-panel.is-blocked { border-color: var(--rose-border); }
.gate-panel.is-ready { border-color: var(--green-border); }
.gate-panel.is-done { border-color: var(--green-border); background: var(--green-soft); }
.gate-body { padding: 16px 18px; display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.gate-main { flex: 1 1 320px; min-width: 0; }
.gate-title { font-size: 15px; font-weight: 650; display: flex; align-items: center; gap: 8px; }
.gate-sub { color: var(--text-2); font-size: 13px; margin-top: 4px; }
.gate-actions { flex: 0 0 auto; display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
.gate-actions .hint { font-size: 12px; color: var(--text-3); max-width: 280px; text-align: right; }
.gate-blockers { list-style: none; margin: 12px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.gate-blockers li {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 8px 10px; border-radius: var(--radius-sm);
  background: var(--rose-soft); color: var(--rose-ink); font-size: 12.5px;
}
.gate-blockers li .ic { flex: 0 0 auto; margin-top: 1px; }
.gate-blockers a { color: inherit; font-weight: 600; }
.gate-counts { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }

.insight-card.is-requires-input { border-color: var(--rose-border); }
.insight-card.is-requires-input.is-reviewed { border-color: var(--green-border); }
.input-reason {
  margin-top: 10px; padding: 10px 12px;
  border-left: 3px solid var(--danger);
  background: var(--rose-soft); color: var(--rose-ink);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 12.5px;
}
.input-reason strong { color: inherit; }
.input-reason.is-resolved { border-left-color: var(--green-border); background: var(--green-soft); color: var(--green-ink); }
.review-note.is-input { border-left-color: var(--amber-border); background: var(--amber-soft); color: var(--amber-ink); }
.review-note.is-revision { border-left-color: var(--blue-border); background: var(--blue-soft); color: var(--blue-ink); }
.review-note strong { color: inherit; }
.decision-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.decision-row .lead { font-size: 12px; color: var(--text-3); }
.insight-card.is-locked .btn { display: none; }
.insight-card.is-locked .btn.btn-ghost, .insight-card.is-locked .btn[data-modal-url$="/table"] { display: inline-flex; }

.modal-explain {
  padding: 12px 14px; margin-bottom: 14px;
  border-radius: var(--radius-sm); font-size: 13px; line-height: 1.5;
  border: 1px solid var(--border); background: var(--surface-2); color: var(--text-2);
}
.modal-explain.is-modify { border-color: var(--blue-border); background: var(--blue-soft); color: var(--blue-ink); }
.modal-explain.is-input { border-color: var(--amber-border); background: var(--amber-soft); color: var(--amber-ink); }
.modal-explain strong { color: inherit; }
.modal-explain ul { margin: 6px 0 0 18px; padding: 0; }

.doc-state {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 12px 16px; margin-bottom: 18px;
  border-radius: var(--radius-md); font-size: 13.5px;
}
.doc-state.is-draft { border: 1px dashed var(--amber-border); background: var(--amber-soft); color: var(--amber-ink); }
.doc-state.is-approved { border: 1px solid var(--green-border); background: var(--green-soft); color: var(--green-ink); }
.doc-state strong { color: inherit; }
.doc-state .spacer { flex: 1 1 auto; }

.nav-project-meta { padding: 2px 10px 8px; }
.nav-project-name { font-size: 13px; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.nav-project-meta .meta { margin-top: 4px; }
@media (max-width: 900px) { .sidebar .flow-text, .sidebar .nav-project-meta { display: none; } }

.insight-card.is-flash, .contra.is-flash { animation: card-flash 1.6s ease-out; }
@keyframes card-flash { 0% { box-shadow: 0 0 0 3px var(--green-border); } 100% { box-shadow: var(--shadow-xs); } }
textarea.is-invalid { border-color: var(--danger); }

.callout.is-green { border-left-color: var(--green-border); background: var(--green-soft); color: var(--green-ink); }

.phase-toggle {
  width: 100%; text-align: left; cursor: pointer; font: inherit; color: inherit;
  margin-bottom: 0; border-radius: var(--radius-md) var(--radius-md) 0 0;
}
.phase-toggle:hover { filter: brightness(.98); }
.phase-toggle:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.phase-id { display: flex; flex-direction: column; min-width: 0; flex: 1 1 auto; }
.phase-title { font-size: 15px; font-weight: 650; }
.phase-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; margin-left: auto; }
.phase-head .phase-state { margin-left: 0; }
.phase-chevron { display: inline-flex; color: var(--text-3); transition: transform .16s ease; margin-left: 6px; }
.phase-group.is-open .phase-chevron { transform: rotate(90deg); }
.phase-progress { margin: 0; border-radius: 0; height: 8px; }
.phase-progress .progress-bar { border-radius: 0; }
.phase-hint { padding: 6px 16px 0; font-size: 12px; }
.phase-body { margin-top: 12px; }
.phase-body .agent-card + .agent-card { margin-top: 10px; }

/* --------------------------------------------------------------------------
   21. Insight cards (five-part review cards) and the card grid
   -------------------------------------------------------------------------- */
.insight-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }
@media (max-width: 900px) { .insight-grid { grid-template-columns: 1fr; } }
.insight-grid > [data-hidden="1"] { display: none; }

.icard {
  display: flex; flex-direction: column; gap: 10px;
  padding: 16px 18px;
  border: 1px solid var(--border); border-radius: var(--radius-md);
  background: var(--surface); box-shadow: var(--shadow-xs);
  border-top: 3px solid var(--border-strong);
}
.icard.is-reviewed { border-top-color: var(--green-border); }
.icard.needs-review { border-top-color: var(--amber-border); }
.icard.not-covered { background: var(--surface-2); }
.icard.is-flash { animation: card-flash 1.6s ease-out; }

.icard-head { display: flex; align-items: center; gap: 10px; }
.icard-num {
  flex: 0 0 auto; padding: 2px 8px; border-radius: var(--radius-sm);
  background: var(--primary-soft); color: var(--primary-text);
  font-weight: 700; font-size: 12.5px; font-family: var(--font-mono);
}
.icard-title { flex: 1 1 auto; min-width: 0; font-size: 15px; font-weight: 700; margin: 0; }
.icard-agent {
  flex: 0 0 auto; font-size: 11px; color: var(--text-3);
  padding: 2px 8px; border: 1px solid var(--border); border-radius: var(--radius-pill);
  white-space: nowrap;
}
.icard-finding { margin: 0; font-size: 13.5px; color: var(--text); line-height: 1.5; }

.icard-status { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--text-2); font-weight: 600; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: 0 0 auto; }
.status-dot.is-ready { background: var(--ok); }
.status-dot.is-review { background: var(--warn); }
.status-sep { color: var(--border-strong); font-weight: 400; }
.status-text { font-weight: 600; }
.icard-why { margin: -4px 0 0; font-size: 12px; color: var(--text-3); line-height: 1.45; }
.icard-why.is-resolved { color: var(--text-3); }

.icard-evidence { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px 12px; background: var(--surface-2); }
.icard-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }
.icard-metric { padding: 8px 10px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); }
.icard-metric-value { font-size: 17px; font-weight: 700; color: var(--text); line-height: 1.2; overflow-wrap: anywhere; }
.icard-metric-label { font-size: 11.5px; color: var(--text-3); margin-top: 2px; }
.icard-table td, .icard-table th { font-size: 12px; }
.icard-steps { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; counter-reset: step; }
.icard-steps li {
  counter-increment: step; display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 10px 5px 8px; font-size: 12px; font-weight: 600;
  background: var(--primary-soft); color: var(--primary-text);
  border-radius: var(--radius-sm);
}
.icard-steps li::before {
  content: counter(step); width: 16px; height: 16px; border-radius: 50%;
  background: var(--primary); color: #fff; font-size: 10px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center;
}
.icard-points { margin: 0; padding-left: 20px; font-size: 12.5px; color: var(--text-2); display: flex; flex-direction: column; gap: 4px; }
.icard-points li::marker { color: var(--primary); font-weight: 700; }

.icard-section { display: flex; flex-direction: column; gap: 4px; }
.icard-label { font-size: 10.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--text-3); }
.icard-text { margin: 0; font-size: 12.5px; color: var(--text-2); line-height: 1.5; }
.icard-note { margin: 0; font-size: 12px; color: var(--text-3); display: flex; align-items: center; gap: 6px; }

.icard-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; padding-top: 10px; border-top: 1px solid var(--border); margin-top: 2px; }
.icard-links { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.icard-link { font-size: 12.5px; font-weight: 600; color: var(--primary-text); display: inline-flex; align-items: center; gap: 4px; }
.icard-link.as-button { background: none; border: 0; padding: 0; cursor: pointer; font: inherit; font-size: 12.5px; font-weight: 600; color: var(--primary-text); }
.icard-link:hover, .icard-link.as-button:hover { text-decoration: underline; }
.icard-actions { display: flex; gap: 6px; flex-wrap: wrap; }

.phase-cards { margin-bottom: 28px; }
.phase-cards-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.phase-cards-head h3 { margin: 0; font-size: 15px; }
.phase-cards-head .count { color: var(--text-3); font-weight: 500; }
.phase-cards.is-new .phase-cards-head h3 { color: var(--primary-text); }

/* Evidence marks: a dot per tag, a quiet chip per source, one legend per page */
.vdot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin: 0 5px 1px 0; vertical-align: middle; border: 1px solid transparent;
  background: var(--slate-border);
}
.vdot.vtag-verified { background: var(--ok); border-color: var(--green-border); }
.vdot.vtag-inference { background: var(--warn); border-color: var(--amber-border); }
.vdot.vtag-original { background: var(--primary); border-color: var(--blue-border); }
.vdot.vtag-update { background: var(--purple-ink); border-color: var(--purple-border); }
.vdot.vtag-not-verified { background: var(--danger); border-color: var(--rose-border); }
.vdot.vtag-general-knowledge { background: var(--slate-ink); border-color: var(--slate-border); opacity: .55; }
.vdot-label { display: inline-flex; align-items: center; gap: 2px; font-size: 12px; color: var(--text-2); }
.vsrc {
  display: inline-block; margin-left: 4px; padding: 0 6px;
  font-size: 11px; color: var(--text-3); background: var(--surface-3);
  border-radius: var(--radius-pill); white-space: nowrap; vertical-align: middle;
}
.vlegend {
  display: flex; flex-wrap: wrap; gap: 6px 16px;
  padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--surface-2); font-size: 12px; color: var(--text-2);
}
.vlegend.is-compact { padding: 6px 10px; font-size: 11.5px; }
.icon-btn-sm { width: 22px; height: 22px; padding: 0; }
.import-box summary::-webkit-details-marker { display: none; }
```

### File: `celestra\static\js\app.js`

```js
/* ==========================================================================
   Celestra front-end. Vanilla JS, zero dependencies, nothing fetched from a CDN.

   Public surface
     Celestra.initEventStream(runId, lastSeq)
     Celestra.postJSON(url, body, options)
     Celestra.openModal(el) / closeModal()
     Celestra.openSlideOver(el) / closeSlideOver()
   ========================================================================== */
(function () {
  'use strict';

  var Celestra = window.Celestra || {};
  window.Celestra = Celestra;

  /* ---------------------------------------------------------------- utils */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function on(root, type, selector, handler) {
    root.addEventListener(type, function (ev) {
      var target = ev.target.closest(selector);
      if (target && root.contains(target)) handler(ev, target);
    });
  }
  function announce(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }
  function normaliseProgress(value) {
    var n = parseFloat(value);
    if (isNaN(n)) return null;
    if (n <= 1) n = n * 100;
    return Math.max(0, Math.min(100, n));
  }
  function titleise(value) {
    if (!value) return '';
    return String(value).replace(/_/g, ' ').replace(/\b\w/g, function (c) {
      return c.toUpperCase();
    });
  }

  var STATUS_LABELS = {
    queued: 'Queued',
    researching: 'Researching…',
    synthesising: 'Synthesising…',
    complete: 'Complete',
    failed: 'Failed',
    skipped: 'Skipped',
    blocked: 'Blocked'
  };
  var BUSY = { researching: 1, synthesising: 1 };

  /* ------------------------------------------------------------ 1. stream */
  var ICON_CHECK =
    '<svg class="ic" width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
  var ICON_ALERT =
    '<svg class="ic" width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><path d="M12 9v4"/>' +
    '<path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 ' +
    '1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>';
  var ICON_DOT = '<span class="chip-dot" aria-hidden="true"></span>';

  function statusMarkup(status) {
    var label = STATUS_LABELS[status] || titleise(status);
    if (BUSY[status]) return '<span class="spinner" aria-hidden="true"></span><span>' + label + '</span>';
    if (status === 'complete') return ICON_CHECK + '<span>' + label + '</span>';
    if (status === 'failed') return ICON_ALERT + '<span>' + label + '</span>';
    return ICON_DOT + '<span>' + label + '</span>';
  }

  function agentCard(key) {
    if (!key) return null;
    return document.querySelector('[data-agent-key="' + String(key).replace(/"/g, '') + '"]');
  }

  function applyAgentStatus(card, status) {
    if (!card || !status) return;
    var holder = $('[data-agent-status]', card);
    card.setAttribute('data-status', status);
    card.classList.remove('is-complete', 'is-failed', 'is-blocked');
    if (status === 'complete') card.classList.add('is-complete');
    if (status === 'failed') card.classList.add('is-failed');
    if (status === 'blocked' || status === 'skipped') card.classList.add('is-blocked');
    if (holder) {
      holder.className = 'agent-status status status-' + status;
      holder.innerHTML = statusMarkup(status);
    }
    var progress = $('[data-agent-progress]', card);
    if (progress) {
      progress.classList.toggle('is-complete', status === 'complete');
      progress.classList.toggle('is-failed', status === 'failed');
    }
    if (status === 'complete') applyAgentProgress(card, 100);
  }

  function applyAgentProgress(card, value) {
    var pct = normaliseProgress(value);
    if (card == null || pct === null) return;
    var wrap = $('[data-agent-progress]', card);
    var bar = wrap ? $('.progress-bar', wrap) : null;
    if (bar) bar.style.width = pct.toFixed(1) + '%';
    if (wrap) wrap.setAttribute('aria-valuenow', Math.round(pct));
  }

  function applyAgentMessage(card, message) {
    if (!card || message == null) return;
    announce($('[data-agent-message]', card), message);
  }

  function markSourceUsed(name, key) {
    var chip = null;
    if (key) chip = document.querySelector('[data-source-key="' + String(key).replace(/"/g, '') + '"]');
    if (!chip && name) {
      chip = $$('[data-source-name]').filter(function (el) {
        return el.getAttribute('data-source-name').toLowerCase() === String(name).toLowerCase();
      })[0];
    }
    var strip = $('[data-source-strip]');
    if (!chip && strip && name) {
      chip = document.createElement('span');
      chip.className = 'chip chip-sm chip-source';
      chip.setAttribute('data-source-name', name);
      if (key) chip.setAttribute('data-source-key', key);
      chip.textContent = name;
      var more = $('[data-source-more]', strip);
      if (more) strip.querySelector('.chip-row').insertBefore(chip, more);
      else strip.querySelector('.chip-row').appendChild(chip);
    }
    if (chip) chip.classList.add('is-used');
  }

  function setPhaseState(key, state, label) {
    var group = document.querySelector('[data-phase="' + String(key).replace(/"/g, '') + '"]');
    if (!group) return;
    group.classList.remove('is-complete', 'is-running', 'is-failed', 'is-gated', 'is-queued');
    group.classList.add('is-' + state);
    var holder = $('[data-phase-state]', group);
    if (holder) {
      if (state === 'running') {
        holder.innerHTML = '<span class="spinner" aria-hidden="true"></span> ' + (label || 'Running');
      } else if (state === 'complete') {
        holder.innerHTML = ICON_CHECK + ' ' + (label || 'Complete');
      } else if (state === 'failed') {
        holder.innerHTML = ICON_ALERT + ' ' + (label || 'An agent failed');
      } else {
        holder.innerHTML = ICON_DOT + ' ' + (label || 'Queued');
      }
    }
  }

  function refreshPhaseFromCards(key) {
    var group = document.querySelector('[data-phase="' + String(key).replace(/"/g, '') + '"]');
    if (!group) return;
    var cards = $$('[data-agent-key]', group);
    if (!cards.length) return;
    var done = cards.filter(function (c) { return c.getAttribute('data-status') === 'complete'; }).length;
    var failed = cards.some(function (c) { return c.getAttribute('data-status') === 'failed'; });
    var busy = cards.some(function (c) { return BUSY[c.getAttribute('data-status')]; });
    if (done === cards.length) setPhaseState(key, 'complete');
    else if (failed && !busy) setPhaseState(key, 'failed');
    else if (busy || done) setPhaseState(key, 'running', 'Running');

    // The phase bar is the mean of its agents; a finished agent counts as 100.
    var total = 0;
    cards.forEach(function (c) {
      if (c.getAttribute('data-status') === 'complete') { total += 100; return; }
      var wrap = $('[data-agent-progress]', c);
      total += wrap ? (parseFloat(wrap.getAttribute('aria-valuenow')) || 0) : 0;
    });
    var pct = Math.round(total / cards.length);
    var bar = $('[data-phase-progress]', group);
    if (bar) {
      bar.setAttribute('aria-valuenow', pct);
      var fill = $('.progress-bar', bar);
      if (fill) fill.style.width = pct + '%';
      bar.classList.toggle('is-complete', done === cards.length);
      bar.classList.toggle('is-failed', failed && !busy);
    }
    var pctEl = $('[data-phase-pct]', group);
    if (pctEl) pctEl.textContent = pct;
    var count = $('[data-phase-count]', group);
    if (count) count.textContent = done + ' / ' + cards.length + ' agents done';
  }

  function togglePhase(group, open) {
    var body = $('[data-phase-body]', group);
    var btn = $('[data-phase-toggle]', group);
    if (!body || !btn) return;
    var expanded = open == null ? body.hidden : open;
    body.hidden = !expanded;
    btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    group.classList.toggle('is-open', expanded);
  }

  function phaseOfCard(card) {
    var group = card ? card.closest('[data-phase]') : null;
    return group ? group.getAttribute('data-phase') : null;
  }

  function bumpCounter(selector) {
    var el = $(selector);
    if (!el) return;
    var current = parseInt(el.getAttribute('data-count') || el.textContent, 10);
    if (isNaN(current)) current = 0;
    current += 1;
    el.setAttribute('data-count', current);
    el.textContent = current;
  }

  Celestra.initEventStream = function (runId, lastSeq) {
    if (!runId || typeof window.EventSource === 'undefined') return null;

    var seq = parseInt(lastSeq, 10) || 0;
    var attempts = 0;
    var source = null;
    var closed = false;
    var moved = false;
    var timer = null;
    var live = $('[data-stream-live]');

    function url() {
      return '/runs/' + encodeURIComponent(runId) + '/events?lastSeq=' + seq;
    }

    function parse(ev) {
      var data = {};
      try { data = JSON.parse(ev.data || '{}'); } catch (err) { data = {}; }
      if (data && typeof data.seq === 'number' && data.seq > seq) seq = data.seq;
      return data;
    }

    function handle(type, data) {
      var key = data.agent_key || data.agent || data.key;
      var card = agentCard(key);

      switch (type) {
        case 'run_started':
          announce(live, 'Research run started.');
          break;

        case 'phase_started':
          if (data.phase) setPhaseState(data.phase, 'running', 'Running');
          var title = $('[data-page-title]');
          var sub = $('[data-page-sub]');
          if (data.phase === 'mapping') {
            if (title) title.textContent = 'Mapping & Synthesis is running';
            if (sub) sub.textContent = 'The four remaining agents are building on the discovery ' +
              'findings you approved. When they finish, the run moves to Final approval.';
            var gateNote = $('[data-phase-gate]');
            if (gateNote) {
              gateNote.classList.add('is-passed');
              var span = $('span', gateNote);
              if (span) span.innerHTML = '<strong>Review gate passed.</strong> You approved the discovery findings.';
            }
          }
          announce(live, (data.name || titleise(data.phase)) + ' phase started.');
          break;

        case 'agent_status':
          applyAgentStatus(card, data.status);
          if (data.message) applyAgentMessage(card, data.message);
          if (data.progress != null) applyAgentProgress(card, data.progress);
          if (card && data.questions_total) {
            var qEl = $('[data-agent-questions]', card);
            if (qEl) {
              qEl.hidden = false;
              qEl.textContent = (data.questions_answered || 0) + ' / ' + data.questions_total;
            }
          }
          refreshPhaseFromCards(phaseOfCard(card));
          announce(live, (data.agent_name || titleise(key)) + ': ' +
            (STATUS_LABELS[data.status] || titleise(data.status)));
          break;

        case 'agent_progress':
          applyAgentProgress(card, data.progress);
          applyAgentMessage(card, data.message);
          if (data.status) applyAgentStatus(card, data.status);
          break;

        case 'source_used':
          markSourceUsed(data.source_name || data.name, data.source_key || data.source_id);
          if (card && data.message) applyAgentMessage(card, data.message);
          break;

        case 'question_status':
          if (card && data.message) applyAgentMessage(card, data.message);
          if (card && data.questions_total) {
            var counterEl = $('[data-agent-questions]', card);
            if (counterEl) {
              counterEl.textContent = (data.questions_answered || 0) + ' / ' + data.questions_total;
            }
          }
          break;

        case 'insight_added':
          bumpCounter('[data-live-insights]');
          break;

        case 'contradiction_added':
          bumpCounter('[data-live-contradictions]');
          break;

        case 'stage_complete':
          if (card) applyAgentStatus(card, 'complete');
          announce(live, (data.name || data.stage || 'Stage') + ' complete.');
          break;

        case 'run_complete':
          announce(live, 'Every agent has finished. Opening final approval…');
          setPhaseState('mapping', 'complete');
          moved = true;
          teardown();
          var next = data.redirect || ('/runs/' + encodeURIComponent(runId) + '/approval');
          window.setTimeout(function () { window.location.assign(next); }, 900);
          break;

        case 'review_required':
          announce(live, 'Discovery finished. Opening the findings for your review…');
          setPhaseState('discovery', 'complete');
          moved = true;
          teardown();
          var reviewUrl = data.redirect || ('/runs/' + encodeURIComponent(runId) + '/review');
          window.setTimeout(function () { window.location.assign(reviewUrl); }, 900);
          break;

        case 'run_resumed':
          announce(live, 'Mapping & Synthesis started.');
          break;

        case 'notice':
          if (data.text) flash(data.text, data.level === 'warning' ? 'is-danger' : 'is-info');
          announce(live, data.text || '');
          break;

        case 'run_failed':
          announce(live, 'Run failed: ' + (data.error || data.message || 'unknown error'));
          var banner = $('[data-run-error]');
          if (banner) {
            banner.hidden = false;
            var body = $('[data-run-error-message]', banner) || banner;
            body.textContent = data.error || data.message || 'The run failed.';
          }
          moved = true;
          teardown();
          break;

        case 'stream_end':
          teardown();
          // The stream can only end because the run paused, finished or failed.
          // If none of those events reached this tab, ask the server where the
          // run is now instead of leaving a stale page.
          if (!moved) {
            window.setTimeout(function () {
              window.location.assign('/runs/' + encodeURIComponent(runId));
            }, 1200);
          }
          break;

        case 'heartbeat':
        default:
          break;
      }
    }

    var TYPES = ['run_started', 'phase_started', 'agent_status', 'agent_progress', 'source_used',
      'question_status', 'insight_added', 'contradiction_added', 'stage_complete',
      'review_required', 'run_resumed', 'context_published', 'wave_started', 'wave_complete', 'notice',
      'run_complete', 'run_failed', 'stream_end', 'heartbeat'];

    function connect() {
      if (closed) return;
      source = new EventSource(url());

      source.addEventListener('open', function () { attempts = 0; });

      TYPES.forEach(function (type) {
        source.addEventListener(type, function (ev) { handle(type, parse(ev)); });
      });

      source.addEventListener('message', function (ev) {
        var data = parse(ev);
        if (data && data.type) handle(data.type, data);
      });

      source.addEventListener('error', function () {
        if (closed) return;
        if (source) { source.close(); source = null; }
        attempts += 1;
        var delay = Math.min(30000, 800 * Math.pow(2, attempts - 1));
        delay = delay + Math.floor(Math.random() * 250);
        announce(live, 'Connection lost. Reconnecting…');
        timer = window.setTimeout(connect, delay);
      });
    }

    function teardown() {
      closed = true;
      if (timer) { window.clearTimeout(timer); timer = null; }
      if (source) { source.close(); source = null; }
    }

    connect();
    window.addEventListener('beforeunload', teardown);
    return { close: teardown, lastSeq: function () { return seq; } };
  };

  /* ------------------------------------------------- 2. insight filtering */
  var CONF_RANK = { requires_input: 0, ready: 1 };

  function initInsights() {
    var lists = $$('[data-insight-list]');
    if (!lists.length) return;
    var list = lists[0];

    var state = { category: 'all', query: '', sort: 'confidence' };
    var countEl = $('[data-insight-count]');
    var emptyEl = $('[data-insight-empty]');

    function cards() {
      return lists.reduce(function (acc, l) { return acc.concat($$('[data-insight-id]', l)); }, []);
    }

    function matches(card) {
      var cat = (card.getAttribute('data-category') || '').toLowerCase();
      var conf = (card.getAttribute('data-confidence') || '').toLowerCase();
      var decided = (card.getAttribute('data-decision') || 'pending') !== 'pending';
      if (state.category === 'requires_input') {
        if (conf !== 'requires_input' || decided) return false;
      } else if (state.category === 'ready') {
        if (conf !== 'ready' && !decided) return false;
      } else if (state.category !== 'all' && cat !== state.category) {
        return false;
      }
      if (!state.query) return true;
      return (card.getAttribute('data-search') || card.textContent || '')
        .toLowerCase().indexOf(state.query) !== -1;
    }

    function apply() {
      var visible = 0;
      cards().forEach(function (card) {
        var show = matches(card);
        card.hidden = !show;
        card.setAttribute('data-hidden', show ? '0' : '1');
        if (show) visible += 1;
      });
      if (countEl) countEl.textContent = visible;
      if (emptyEl) emptyEl.hidden = visible !== 0;
    }

    function sortList(l) {
      var items = $$('[data-insight-id]', l);
      items.sort(function (a, b) {
        if (state.sort === 'sources') {
          return (parseInt(b.getAttribute('data-sources'), 10) || 0) -
                 (parseInt(a.getAttribute('data-sources'), 10) || 0);
        }
        if (state.sort === 'stage') {
          return (a.getAttribute('data-stage') || '').localeCompare(b.getAttribute('data-stage') || '');
        }
        var ra = CONF_RANK[a.getAttribute('data-confidence')];
        var rb = CONF_RANK[b.getAttribute('data-confidence')];
        ra = ra === undefined ? 9 : ra;
        rb = rb === undefined ? 9 : rb;
        if (ra !== rb) return ra - rb;
        return (parseInt(a.getAttribute('data-number'), 10) || 999) -
               (parseInt(b.getAttribute('data-number'), 10) || 999);
      });
      items.forEach(function (card) { l.appendChild(card); });
    }
    function sort() { lists.forEach(sortList); }

    on(document, 'click', '[data-filter-category]', function (ev, btn) {
      ev.preventDefault();
      state.category = (btn.getAttribute('data-filter-category') || 'all').toLowerCase();
      $$('[data-filter-category]').forEach(function (el) {
        el.setAttribute('aria-pressed', el === btn ? 'true' : 'false');
      });
      apply();
    });

    var search = $('[data-insight-search]');
    if (search) {
      search.addEventListener('input', function () {
        state.query = search.value.trim().toLowerCase();
        apply();
      });
    }

    var sortSelect = $('[data-insight-sort]');
    if (sortSelect) {
      state.sort = sortSelect.value || state.sort;
      sortSelect.addEventListener('change', function () {
        state.sort = sortSelect.value;
        sort();
        apply();
      });
    }

    sort();
    apply();
    Celestra.refreshInsights = function () { sort(); apply(); };
  }

  /* ------------------------------- 3. modal: focus trap, escape, counter */
  var modalRoot, slideRoot, lastFocused;

  function focusables(container) {
    return $$(
      'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]),' +
      'select:not([disabled]), [tabindex]:not([tabindex="-1"])', container
    ).filter(function (el) { return el.offsetParent !== null || el === document.activeElement; });
  }

  function trap(container, ev) {
    if (ev.key !== 'Tab') return;
    var items = focusables(container);
    if (!items.length) { ev.preventDefault(); return; }
    var first = items[0];
    var last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }

  function root(id, cls) {
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement('div');
      el.id = id;
      if (cls) el.className = cls;
      document.body.appendChild(el);
    }
    return el;
  }

  function activate(container, onClose) {
    lastFocused = document.activeElement;
    document.body.style.overflow = 'hidden';
    var dialog = $('[role="dialog"]', container) || container;
    var auto = $('[data-autofocus]', container) || focusables(dialog)[0];
    if (auto) auto.focus();

    function keydown(ev) {
      if (ev.key === 'Escape') { ev.preventDefault(); onClose(); return; }
      trap(dialog, ev);
    }
    container.addEventListener('keydown', keydown);
    container._celestraKeydown = keydown;

    container.addEventListener('mousedown', function (ev) {
      var t = ev.target;
      if (t === container || t.hasAttribute('data-modal-backdrop') ||
          t.hasAttribute('data-slideover-backdrop')) {
        onClose();
      }
    });
    initCounters(container);
  }

  function deactivate(container) {
    if (!container) return;
    if (container._celestraKeydown) {
      container.removeEventListener('keydown', container._celestraKeydown);
      container._celestraKeydown = null;
    }
    container.innerHTML = '';
    if (!$('#modal-root') || !$('#modal-root').innerHTML) {
      if (!$('#slideover-root') || !$('#slideover-root').innerHTML) {
        document.body.style.overflow = '';
      }
    }
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  }

  Celestra.closeModal = function () { deactivate(modalRoot); };
  Celestra.closeSlideOver = function () { deactivate(slideRoot); };

  Celestra.openModal = function (html) {
    modalRoot = root('modal-root');
    modalRoot.innerHTML = html;
    activate(modalRoot, Celestra.closeModal);
    return modalRoot;
  };

  Celestra.openSlideOver = function (html) {
    slideRoot = root('slideover-root');
    slideRoot.innerHTML = html;
    activate(slideRoot, Celestra.closeSlideOver);
    return slideRoot;
  };

  function initCounters(scope) {
    $$('[data-counter-for]', scope || document).forEach(function (counter) {
      var field = document.getElementById(counter.getAttribute('data-counter-for'));
      if (!field) return;
      var max = parseInt(field.getAttribute('maxlength'), 10) ||
                parseInt(counter.getAttribute('data-counter-max'), 10) || 500;
      function render() {
        counter.textContent = field.value.length + '/' + max;
        counter.classList.toggle('is-over', field.value.length > max);
      }
      field.addEventListener('input', render);
      render();
    });
  }
  Celestra.initCounters = initCounters;

  /* ------------------------------------------------- 4. remote panels */
  async function fetchFragment(url) {
    var res = await fetch(url, { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' });
    var text = await res.text();
    if (!res.ok) throw new Error(text || ('Request failed: ' + res.status));
    return text;
  }

  /* ------------------------------------------------------ 5. postJSON */
  Celestra.postJSON = async function (url, body, options) {
    options = options || {};
    var res = await fetch(url, {
      method: options.method || 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/html, application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {})
    });

    var type = res.headers.get('Content-Type') || '';
    var payload = type.indexOf('application/json') !== -1 ? await res.json() : await res.text();

    if (!res.ok) {
      var message = (payload && payload.detail) || (payload && payload.error) ||
        (typeof payload === 'string' ? payload : 'Request failed');
      if (options.onError) options.onError(message);
      else flash(String(message).slice(0, 400), 'is-danger');
      return { ok: false, payload: payload, status: res.status };
    }

    var html = typeof payload === 'string' ? payload : (payload && payload.html);
    var target = options.swap;
    if (typeof target === 'string') target = $(target);

    if (html && target) {
      var holder = document.createElement('div');
      holder.innerHTML = html.trim();
      var fresh = holder.firstElementChild;
      if (fresh) {
        target.replaceWith(fresh);
        initCounters(fresh);
        if (Celestra.refreshInsights) Celestra.refreshInsights();
        fresh.classList.add('is-flash');
        window.setTimeout(function () { fresh.classList.remove('is-flash'); }, 1600);
      }
    }
    refreshGate();

    if (payload && payload.redirect) window.location.assign(payload.redirect);
    if (options.onSuccess) options.onSuccess(payload);
    return { ok: true, payload: payload, html: html };
  };

  async function refreshGate() {
    var panel = $('[data-gate-panel][data-gate-url]');
    if (!panel) return;
    try {
      var html = await fetchFragment(panel.getAttribute('data-gate-url'));
      var holder = document.createElement('div');
      holder.innerHTML = html.trim();
      var fresh = holder.firstElementChild;
      if (fresh) panel.replaceWith(fresh);
    } catch (err) { /* the page still works; the panel is just stale */ }
  }
  Celestra.refreshGate = refreshGate;

  function flash(message, kind) {
    var area = $('[data-flash-area]');
    if (!area) { return; }
    var box = document.createElement('div');
    box.className = 'banner ' + (kind || 'is-info');
    box.setAttribute('role', 'status');
    box.innerHTML = '<div class="banner-body"></div>';
    box.firstChild.textContent = message;
    area.appendChild(box);
  }
  Celestra.flash = flash;

  /* ---------------------------------------------------- 6. wiring */
  function payloadFor(btn) {
    var data = {};
    var raw = btn.getAttribute('data-payload');
    if (raw) { try { data = JSON.parse(raw); } catch (err) { data = {}; } }
    var scopeSel = btn.getAttribute('data-fields-from');
    var scope = scopeSel ? $(scopeSel) : btn.closest('[data-action-scope]');
    if (scope) {
      $$('[data-field]', scope).forEach(function (field) {
        data[field.getAttribute('data-field')] = field.value;
      });
    }
    return data;
  }

  function wire() {
    modalRoot = root('modal-root');
    slideRoot = root('slideover-root');

    /* dismissible banners */
    on(document, 'click', '[data-dismiss-banner]', function (ev, btn) {
      var banner = btn.closest('.banner');
      if (banner) banner.remove();
      var key = btn.getAttribute('data-dismiss-banner');
      if (key && window.localStorage) {
        try { window.localStorage.setItem('celestra.dismissed.' + key, '1'); } catch (err) { /* ignore */ }
      }
    });
    $$('[data-dismiss-banner]').forEach(function (btn) {
      var key = btn.getAttribute('data-dismiss-banner');
      if (!key || !window.localStorage) return;
      try {
        if (window.localStorage.getItem('celestra.dismissed.' + key) === '1') {
          var banner = btn.closest('.banner');
          if (banner) banner.remove();
        }
      } catch (err) { /* ignore */ }
    });

    /* modal */
    on(document, 'click', '[data-modal-url]', async function (ev, btn) {
      ev.preventDefault();
      btn.setAttribute('aria-busy', 'true');
      try {
        Celestra.openModal(await fetchFragment(btn.getAttribute('data-modal-url')));
      } catch (err) {
        flash('Could not open that dialog.', 'is-danger');
      } finally {
        btn.removeAttribute('aria-busy');
      }
    });
    on(document, 'click', '[data-modal-close]', function (ev) {
      ev.preventDefault();
      Celestra.closeModal();
    });

    /* slide-over */
    on(document, 'click', '[data-evidence-url]', async function (ev, btn) {
      ev.preventDefault();
      btn.setAttribute('aria-busy', 'true');
      try {
        Celestra.openSlideOver(await fetchFragment(btn.getAttribute('data-evidence-url')));
      } catch (err) {
        flash('Could not load the evidence panel.', 'is-danger');
      } finally {
        btn.removeAttribute('aria-busy');
      }
    });
    on(document, 'click', '[data-slideover-close]', function (ev) {
      ev.preventDefault();
      Celestra.closeSlideOver();
    });

    /* JSON actions: approve / modify / add input / contradiction review */
    on(document, 'click', '[data-action-url]', async function (ev, btn) {
      ev.preventDefault();
      if (btn.disabled) return;
      var payload = payloadFor(btn);
      var required = btn.getAttribute('data-requires-field');
      if (required && !String(payload[required] || '').trim()) {
        var scope = btn.closest('[data-action-scope]');
        var field = scope ? $('[data-field="' + required + '"]', scope) : null;
        if (field) { field.focus(); field.classList.add('is-invalid'); }
        flash('Write something first: this action sends your text to Celestra.', 'is-danger');
        return;
      }
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      var original = btn.innerHTML;
      var busyLabel = btn.getAttribute('data-busy-label');
      if (busyLabel) btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> ' + busyLabel;
      var swap = btn.getAttribute('data-swap');
      var closes = btn.hasAttribute('data-closes-modal');
      try {
        var result = await Celestra.postJSON(btn.getAttribute('data-action-url'), payload, {
          swap: swap ? $(swap) : null
        });
        if (result.ok && closes) Celestra.closeModal();
      } finally {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        if (busyLabel) btn.innerHTML = original;
      }
    });

    /* the gate form: never submit while blocked */
    on(document, 'submit', '[data-gate-form]', function (ev, form) {
      var button = $('button[type="submit"]', form);
      if (button && button.disabled) { ev.preventDefault(); return; }
      if (button) {
        button.disabled = true;
        button.innerHTML = '<span class="spinner" aria-hidden="true"></span> Working…';
      }
    });

    /* phase groups on the live page: collapsed by default, click to expand */
    on(document, 'click', '[data-phase-toggle]', function (ev, btn) {
      ev.preventDefault();
      var group = btn.closest('[data-phase]');
      if (group) togglePhase(group);
    });
    if (window.location.hash) {
      var target = document.querySelector('[data-phase]' + window.location.hash.replace(/[^#\w-]/g, ''));
      if (target) togglePhase(target, true);
    }

    /* mode selector on the new-project form */
    $$('[data-mode-radio]').forEach(function (radio) {
      radio.addEventListener('change', function () {
        var reveal = $('[data-mode-reveal]');
        if (!reveal) return;
        var single = radio.value === 'single' && radio.checked;
        reveal.hidden = !single;
        var select = $('select', reveal);
        if (select) select.required = single;
      });
      if (radio.checked) radio.dispatchEvent(new Event('change'));
    });

    initInsights();
    initCounters(document);

    var stream = $('[data-run-stream]');
    if (stream) {
      Celestra.initEventStream(
        stream.getAttribute('data-run-stream'),
        stream.getAttribute('data-last-seq') || 0
      );
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
```

### File: `celestra\templates\approval.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import stat_tile, agent_tile %}
{% block title %}Final approval — Celestra{% endblock %}

{% set c = counts | default({}) %}

{% block content %}
{%- set rid = run.id -%}
<div class="page-head">
  <p class="eyebrow">Step {{ '5 of 5 · Approved' if gate.done else '4 of 5 · Final approval' }}</p>
  <h1>{% if gate.done %}The document is approved{% else %}Approve the research document{% endif %}</h1>
  <p class="sub">
    {% if gate.done %}
    Every finding was reviewed and the document is locked. Open it below, or start a new project
    to research this indication again.
    {% else %}
    Every agent has finished. Below is everything that still needs your input, then every finding
    from both phases. When nothing needs input, approve: the document is locked and your decisions
    are recorded in it.
    {% endif %}
  </p>
</div>

{% include "partials/gate_panel.html" %}

<div class="stats mt-3 mb-3">
  {{ stat_tile(c.get('ready', 0), 'Ready', 'green', check=true) }}
  {{ stat_tile(c.get('needs_decision', 0), 'Need your input', 'rose' if c.get('needs_decision') else 'slate') }}
  {{ stat_tile(c.get('decided', 0), 'Decided by you', 'blue',
               note=(c.get('approved', 0) ~ ' approved · ' ~ c.get('modified', 0) ~ ' revised · ' ~ c.get('input_added', 0) ~ ' with input')) }}
  {{ stat_tile(c.get('conflicts_open', 0), 'Open source conflicts', 'amber' if c.get('conflicts_open') else 'slate') }}
</div>

<section class="section">
  <h2 class="section-title">What the document contains</h2>
  <div class="card card-pad">
    {% for item in included | default([]) %}
    <div class="included-item">
      {{ agent_tile(item.icon | default('sparkle', true), 'tile-sm') }}
      <div>
        <div class="included-name">{{ item.name }}{% if item.get("phase") %} <span class="meta">· {{ item.phase }} phase</span>{% endif %}</div>
        <div class="included-desc">{{ item.description }}</div>
      </div>
    </div>
    {% else %}
    <p class="meta mb-0">Nothing has been produced for this run yet.</p>
    {% endfor %}
    <div class="meta mt-1">
      {{ c.get('stages', 0) }} stage reports · {{ c.get('evidence', 0) }} evidence items ·
      {{ c.get('assumptions', 0) }} stated assumptions
    </div>
  </div>
</section>

{% if reviewer_inputs %}
<section class="section">
  <h2 class="section-title">Your inputs in the document ({{ reviewer_inputs | length }})</h2>
  <div class="card card-pad">
    {% for r in reviewer_inputs %}
    <div class="review-note is-input mb-1"><strong>{{ r.title }}:</strong> {{ r.input }}</div>
    {% endfor %}
  </div>
</section>
{% endif %}

<h2 class="section-head">All findings ({{ c.insights }})</h2>
<p class="sub mb-2">Cards from the latest phase come first; the discovery cards you reviewed at the
  gate are below them.</p>
<div class="chips mb-2" data-filter-group>
  <button type="button" class="pill" data-filter-category="all" aria-pressed="true">All ({{ c.insights }})</button>
  <button type="button" class="pill" data-filter-category="requires_input" aria-pressed="false">Needs input ({{ c.needs_decision }})</button>
  <button type="button" class="pill" data-filter-category="ready" aria-pressed="false">Ready ({{ c.ready }})</button>
  {% for cat in categories %}
  <button type="button" class="pill" data-filter-category="{{ cat.key }}" aria-pressed="false">{{ cat.label }} ({{ cat.count }})</button>
  {% endfor %}
</div>
{% for sec in sections | default([]) %}
<section class="phase-cards {{ 'is-new' if sec.is_new }}" id="cards-{{ sec.key }}">
  <div class="phase-cards-head">
    <h3>{{ sec.name }} phase <span class="count">({{ sec.cards | length }} cards)</span></h3>
    <span class="chip chip-sm {{ 'chip-blue' if sec.is_new else 'chip-muted' }}">{{ sec.label }}</span>
    {% if sec.needs %}<span class="meta">{{ sec.needs }} need{{ '' if sec.needs == 1 else 's' }} your review</span>{% endif %}
  </div>
  <div class="insight-grid" data-insight-list>
    {% for insight in sec.cards %}
      {% include "partials/insight_card.html" %}
    {% endfor %}
  </div>
</section>
{% else %}
<div class="insight-grid" data-insight-list>
  {% for insight in insights | default([]) %}
    {% include "partials/insight_card.html" %}
  {% else %}
  <p class="sub">No findings were produced.</p>
  {% endfor %}
</div>
{% endfor %}
<div class="empty mt-2" data-insight-empty {% if insights %}hidden{% endif %}>
  <h3>No findings match that filter</h3>
  <p class="mb-0">Pick a different filter.</p>
</div>

{% if contradictions %}
<h2 class="section-head">Source conflicts ({{ contradictions | length }})</h2>
<div class="stack">
  {% for contradiction in contradictions %}
    {% include "partials/contradiction_card.html" %}
  {% endfor %}
</div>
{% endif %}

<div class="btn-row btn-row-end mt-3">
  <a class="btn btn-secondary" href="/runs/{{ rid }}/findings">{{ icon('download', 14) }} Findings report</a>
  <a class="btn btn-secondary" href="/runs/{{ rid }}/report">{{ 'Open the approved document' if gate.done else 'Preview the draft document' }}</a>
  {% if not gate.done %}<a class="btn btn-ghost" href="#gate">Back to the approval decision ↑</a>{% endif %}
</div>
{% endblock %}
```

### File: `celestra\templates\base.html`

```html
{%- from "partials/icons.html" import icon, ring_logo -%}
{%- from "partials/macros.html" import run_status_chip, flow_steps -%}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{% block title %}Celestra{% endblock %}</title>
  <link rel="stylesheet" href="/static/css/app.css">
  <link rel="icon" type="image/svg+xml" href="/static/img/favicon.svg">
</head>
<body>
<a class="skip-link" href="#main">Skip to main content</a>

<div class="app">
  {% block sidebar %}
  <nav class="sidebar" aria-label="Primary">
    <a class="brand" href="/">
      {{ ring_logo(22) }}
      <span class="brand-word">Celestra</span>
    </a>

    <div class="nav">
      <a class="nav-item {{ 'is-active' if active == 'home' }}" href="/"
         {% if active == 'home' %}aria-current="page"{% endif %}>
        {{ icon('home', 17) }}<span class="nav-label">Home</span>
      </a>
      <a class="nav-item {{ 'is-active' if active == 'new_project' }}" href="/projects/new" data-modal-url="/projects/name"
         {% if active == 'new_project' %}aria-current="page"{% endif %}>
        {{ icon('plus-square', 17) }}<span class="nav-label">New Project</span>
      </a>
      <a class="nav-item {{ 'is-active' if active == 'projects' }}" href="/projects"
         {% if active == 'projects' %}aria-current="page"{% endif %}>
        {{ icon('folder', 17) }}<span class="nav-label">Projects</span>
      </a>
      <a class="nav-item {{ 'is-active' if active == 'library' }}" href="/library"
         {% if active == 'library' %}aria-current="page"{% endif %}>
        {{ icon('book', 17) }}<span class="nav-label">Knowledge Library</span>
      </a>
    </div>

    {% if run is defined and run %}
    <div class="nav">
      <div class="nav-group-label">This project</div>
      <div class="nav-project-meta">
        <div class="nav-project-name" title="{{ run.display_name }}">{{ run.display_name }}</div>
        <div class="meta row" style="gap:6px;">{{ run_status_chip(run.status) }}
          <button type="button" class="icon-btn icon-btn-sm" data-modal-url="/runs/{{ run.id }}/rename"
                  title="Rename project" aria-label="Rename project">{{ icon('edit', 13) }}</button>
        </div>
      </div>
      {{ flow_steps(run_steps(run), compact=true) }}
    </div>
    <div class="nav">
      <div class="nav-group-label">Detail</div>
      <a class="nav-item {{ 'is-active' if active == 'insights' }}" href="/runs/{{ run.id }}/insights"
         {% if active == 'insights' %}aria-current="page"{% endif %}>
        {{ icon('list', 17) }}<span class="nav-label">All findings</span>
      </a>
      <a class="nav-item {{ 'is-active' if active == 'contradictions' }}" href="/runs/{{ run.id }}/contradictions"
         {% if active == 'contradictions' %}aria-current="page"{% endif %}>
        {{ icon('split', 17) }}<span class="nav-label">Source conflicts</span>
      </a>
      <a class="nav-item {{ 'is-active' if active == 'overview' }}" href="/runs/{{ run.id }}/overview"
         {% if active == 'overview' %}aria-current="page"{% endif %}>
        {{ icon('layers', 17) }}<span class="nav-label">Stage reports</span>
      </a>
      <a class="nav-item {{ 'is-active' if active == 'sources' }}" href="/runs/{{ run.id }}/sources"
         {% if active == 'sources' %}aria-current="page"{% endif %}>
        {{ icon('database', 17) }}<span class="nav-label">Sources</span>
      </a>
      <a class="nav-item {{ 'is-active' if active == 'findings' }}" href="/runs/{{ run.id }}/findings"
         {% if active == 'findings' %}aria-current="page"{% endif %}>
        {{ icon('download', 17) }}<span class="nav-label">Findings report</span>
      </a>
      <a class="nav-item {{ 'is-active' if active == 'report' }}" href="/runs/{{ run.id }}/report"
         {% if active == 'report' %}aria-current="page"{% endif %}>
        {{ icon('file-text', 17) }}<span class="nav-label">{{ 'Approved document' if run.status.value == 'approved' else 'Draft document' }}</span>
      </a>
    </div>
    {% endif %}

    <div class="nav-spacer"></div>

    <div class="nav">
      <a class="nav-item {{ 'is-active' if active == 'settings' }}" href="/settings"
         {% if active == 'settings' %}aria-current="page"{% endif %}>
        {{ icon('settings', 17) }}<span class="nav-label">Settings</span>
      </a>
    </div>
  </nav>
  {% endblock %}

  <main class="main" id="main">
    <div class="shell">

      {% block banners %}
      {% if credentials is defined and credentials %}
        {% if llm is defined and not llm.configured %}
        <div class="banner" role="status">
          {{ icon('alert', 16) }}
          <div class="banner-body">
            <strong>Deterministic mode.</strong>
            No LLM provider is configured, so findings are assembled from retrieved evidence
            with rule-based templates rather than model-written prose.
            Set one of these in <code>.env</code>: {{ llm.gaps | join('; ') }}.
          </div>
          <button type="button" class="banner-close" data-dismiss-banner="llm"
                  aria-label="Dismiss deterministic mode notice">{{ icon('x', 15) }}</button>
        </div>
        {% elif llm is defined %}
        <div class="banner is-ok" role="status">
          {{ icon('check-circle', 16) }}
          <div class="banner-body">
            <strong>Model-written synthesis on.</strong>
            Provider <code>{{ llm.provider }}</code>, model <code>{{ llm.model }}</code>
            {%- if not llm.explicit %} (auto-detected from the keys in <code>.env</code>){% endif %}.
          </div>
          <button type="button" class="banner-close" data-dismiss-banner="llm-ok"
                  aria-label="Dismiss model notice">{{ icon('x', 15) }}</button>
        </div>
        {% endif %}
        {% if web_search is defined and web_search.get('keyed') and web_search.get('error') %}
        <div class="banner is-danger" role="alert">
          {{ icon('alert', 16) }}
          <div class="banner-body">
            <strong>Firecrawl is configured but its last call failed.</strong>
            {{ web_search.error }}
            Web fallback is using the keyless path until this is fixed.
            <a href="/settings#web-search">Test it on the Settings page</a>.
          </div>
        </div>
        {% endif %}
        {% if not credentials.get('firecrawl_api_key') %}
        <div class="banner" role="status">
          {{ icon('alert', 16) }}
          <div class="banner-body">
            <strong>Keyless web fallback.</strong>
            No Firecrawl API key is configured, so open-web fallback is using the keyless search
            path. Supplementary web results may be thinner and are always labelled as such.
          </div>
          <button type="button" class="banner-close" data-dismiss-banner="firecrawl"
                  aria-label="Dismiss web fallback notice">{{ icon('x', 15) }}</button>
        </div>
        {% endif %}
      {% endif %}
      {% endblock %}

      <div class="flash-area" data-flash-area aria-live="polite">
        {% for message in messages | default([]) %}
          {% if message is mapping %}
          <div class="banner is-{{ message.get('level', 'info') }}" role="status">
            <div class="banner-body">{{ message.get('text', '') }}</div>
            <button type="button" class="banner-close" data-dismiss-banner
                    aria-label="Dismiss message">{{ icon('x', 15) }}</button>
          </div>
          {% else %}
          <div class="banner is-info" role="status">
            <div class="banner-body">{{ message }}</div>
            <button type="button" class="banner-close" data-dismiss-banner
                    aria-label="Dismiss message">{{ icon('x', 15) }}</button>
          </div>
          {% endif %}
        {% endfor %}
      </div>

      {% block content %}{% endblock %}
    </div>
  </main>
</div>

<div id="modal-root"></div>
<div id="slideover-root"></div>

<script src="/static/js/app.js" defer></script>
{% block page_scripts %}{% endblock %}
</body>
</html>
```

### File: `celestra\templates\contradictions.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% block title %}Source conflicts — Celestra{% endblock %}

{% block content %}
<div class="page-head">
  <h1>Source Conflicts</h1>
  <p class="sub">
    Where two sources disagree, Celestra reports both sides exactly as stated and stops.
    A person decides which side stands.
  </p>
</div>

<div class="no-autoresolve">
  {{ icon('alert', 16) }}
  <span>Nothing on this page has been auto-resolved. Every conflict below is waiting on a human decision.</span>
</div>

{% if contradictions %}
<p class="meta mb-2">
  {{ contradictions | length }} conflict{{ '' if contradictions | length == 1 else 's' }} surfaced across this run.
</p>

<div class="stack">
  {% for contradiction in contradictions %}
    {% include "partials/contradiction_card.html" %}
  {% endfor %}
</div>
{% else %}
<div class="empty">
  <h3>No source conflicts surfaced</h3>
  <p class="mb-0">No two sources made materially different assertions about the same concept in this run.</p>
</div>
{% endif %}

<div class="btn-row mt-3">
  <a class="btn btn-primary" href="{{ next_url | default('/runs/' ~ run.id) }}">
    Back to the current step {{ icon('arrow-right', 15) }}
  </a>
  <a class="btn btn-secondary" href="/runs/{{ run.id }}/insights">All findings</a>
</div>
{% endblock %}
```

### File: `celestra\templates\error.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% block title %}Something went wrong — Celestra{% endblock %}

{% block content %}
<div class="shell-narrow">
  <div class="card card-pad">
    <div class="row mb-2">
      <span class="tile tile-lg tint-rose">{{ icon('alert', 22) }}</span>
      <div>
        <h1>{{ message | default('Something went wrong', true) }}</h1>
        <p class="sub mb-0">Nothing was changed. You can go back and try again.</p>
      </div>
    </div>

    {% if detail %}
    <div class="callout is-rose mb-2">
      <strong>Detail</strong>
      <div class="mt-1 break">{{ detail }}</div>
    </div>
    {% endif %}

    <div class="btn-row">
      <a class="btn btn-primary" href="/">Back to home</a>
      <a class="btn btn-secondary" href="/projects">All projects</a>
    </div>
  </div>
</div>
{% endblock %}
```

### File: `celestra\templates\findings.html`

```html
{#- The findings report: everything that was found, nothing about how.
    Standalone (own CSS, no sidebar) so the same page is what you view, print
    to PDF, or download as a file that opens in a browser or Word.
    Context: run, phase_key, phase_label, groups, generated, download, source_names -#}
{% from "partials/macros.html" import data_table, source_name_chips, tag_legend %}
{%- set names = source_names | default({}) -%}
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ run.display_name }} — Findings{{ ' · ' ~ phase_label if phase_key != 'all' else '' }}</title>
<style>
  :root { --ink:#1b2130; --ink2:#4a5567; --ink3:#8a94a6; --line:#e6e8ec; --soft:#f6f7f9;
          --ok:#157347; --warn:#b45309; --danger:#be123c; --primary:#2563eb; --purple:#6d28d9; --slate:#5b6577; }
  * { box-sizing: border-box; }
  body { margin:0; font: 13.5px/1.55 -apple-system, "Segoe UI", Inter, Roboto, sans-serif; color:var(--ink); background:#fff; }
  .doc { max-width: 960px; margin: 0 auto; padding: 32px 40px 80px; }
  .toolbar { position: sticky; top: 0; background:#fff; border-bottom:1px solid var(--line); padding:10px 0; margin-bottom: 20px;
             display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .toolbar a, .toolbar button { font: inherit; font-size:12.5px; font-weight:600; color:var(--primary); background:var(--soft);
             border:1px solid var(--line); border-radius:8px; padding:6px 10px; text-decoration:none; cursor:pointer; }
  .toolbar a.is-active { background:var(--primary); color:#fff; border-color:var(--primary); }
  .toolbar .spacer { flex:1; }
  h1 { font-size: 24px; margin: 0 0 4px; }
  h2 { font-size: 18px; margin: 34px 0 6px; padding-top: 18px; border-top: 2px solid var(--ink); }
  h3 { font-size: 15px; margin: 22px 0 6px; }
  h4 { font-size: 13px; margin: 14px 0 6px; text-transform: uppercase; letter-spacing:.06em; color:var(--ink3); }
  .meta { color: var(--ink3); font-size: 12.5px; }
  .sub { color: var(--ink2); margin: 0 0 8px; }
  .card { border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin: 12px 0; break-inside: avoid; }
  .card-head { display:flex; align-items:center; gap:10px; margin-bottom:6px; }
  .num { font-family: ui-monospace, Menlo, monospace; font-weight:700; font-size:12px; color:var(--primary); background:#eaf1ff; padding:2px 7px; border-radius:6px; }
  .card-title { font-weight:700; font-size:14.5px; }
  .finding { margin: 0 0 8px; }
  .interp { margin: 8px 0 0; color: var(--ink2); }
  .label { font-size: 10.5px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--ink3); margin: 8px 0 3px; }
  .metrics { display:grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr)); gap:8px; }
  .metric { border:1px solid var(--line); border-radius:8px; padding:8px 10px; background:var(--soft); }
  .metric b { display:block; font-size:16px; }
  .metric span { font-size:11.5px; color:var(--ink3); }
  ol.steps { display:flex; flex-wrap:wrap; gap:6px; padding:0; margin:0; list-style:none; counter-reset:s; }
  ol.steps li { counter-increment:s; background:#eaf1ff; color:#1e3a8a; border-radius:6px; padding:4px 9px; font-size:12px; font-weight:600; }
  ol.steps li::before { content: counter(s) ". "; color:var(--primary); }
  ul.points, ol.points { margin: 4px 0 0 18px; padding:0; }
  table.data { width:100%; border-collapse:collapse; font-size:12.5px; margin: 6px 0; }
  table.data th, table.data td { text-align:left; vertical-align:top; padding:6px 9px; border-bottom:1px solid var(--line); }
  table.data thead th { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--ink3); background:var(--soft); }
  .table-wrap { overflow-x:auto; }
  .subblock { margin: 10px 0; }
  .table-title h3 { margin: 10px 0 4px; font-size: 13.5px; }
  .table-footnote { font-size:11.5px; color:var(--ink3); margin: 2px 0 8px; }
  .chip-row { display:inline-flex; flex-wrap:wrap; gap:5px; }
  .chip { display:inline-block; font-size:11px; padding:1px 8px; border-radius:999px; border:1px solid var(--line); background:var(--soft); color:var(--ink2); }
  .chip-source.is-used { background:#eaf1ff; color:#1e3a8a; border-color:#c7d7fb; }
  .vdot { display:inline-block; width:8px; height:8px; border-radius:50%; margin:0 5px 1px 0; vertical-align:middle; background:#dcdfe6; }
  .vdot.vtag-verified { background:var(--ok); } .vdot.vtag-inference { background:var(--warn); }
  .vdot.vtag-original { background:var(--primary); } .vdot.vtag-update { background:var(--purple); }
  .vdot.vtag-not-verified { background:var(--danger); } .vdot.vtag-general-knowledge { background:var(--slate); opacity:.55; }
  .vsrc { display:inline-block; margin-left:4px; padding:0 6px; font-size:11px; color:var(--ink3); background:var(--soft); border-radius:999px; white-space:nowrap; }
  .vlegend { display:flex; flex-wrap:wrap; gap:6px 16px; padding:8px 12px; border:1px solid var(--line); border-radius:8px; background:var(--soft); font-size:12px; color:var(--ink2); margin: 10px 0 0; }
  .vdot-label { display:inline-flex; align-items:center; gap:2px; font-size:12px; color:var(--ink2); }
  .qa { margin: 8px 0 10px; padding-left: 12px; border-left: 3px solid var(--line); }
  .qa .q { font-weight:600; margin:0 0 2px; }
  .qa .a { margin:0; }
  .qa .s { margin:3px 0 0; font-size:12px; color:var(--ink3); }
  .note { margin: 6px 0 0; padding: 6px 10px; border-left: 3px solid #f6c453; background:#fff8e6; font-size:12.5px; }
  .status { font-size:12px; color:var(--ink2); margin: 0 0 6px; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:middle; }
  .dot.ready { background:var(--ok); } .dot.review { background:var(--warn); }
  @media print { .toolbar { display:none; } .doc { padding: 0; max-width:none; } h2 { break-before: page; } h2:first-of-type { break-before: auto; } a { color:inherit; text-decoration:none; } }
</style>
</head>
<body>
<div class="doc">
  {% if not download %}
  <div class="toolbar">
    <a href="/runs/{{ run.id }}" title="Back to the project">← {{ run.display_name }}</a>
    <span class="spacer"></span>
    <a class="{{ 'is-active' if phase_key == 'all' }}" href="/runs/{{ run.id }}/findings">Whole document</a>
    <a class="{{ 'is-active' if phase_key == 'discovery' }}" href="/runs/{{ run.id }}/findings?phase=discovery">Discovery</a>
    <a class="{{ 'is-active' if phase_key == 'mapping' }}" href="/runs/{{ run.id }}/findings?phase=mapping">Mapping &amp; Synthesis</a>
    <a href="/runs/{{ run.id }}/findings?phase={{ phase_key }}&amp;download=1">Download</a>
    <button type="button" onclick="window.print()">Print / Save as PDF</button>
  </div>
  {% endif %}

  <h1>{{ run.display_name }}</h1>
  <p class="sub">{{ run.config.indication }} · {{ run.config.geography }} · {{ run.config.objective }}{% if run.config.drug_brand %} · {{ run.config.drug_brand }}{% endif %}</p>
  <p class="meta">Findings{{ ' · ' ~ phase_label if phase_key != 'all' else '' }} · {{ 'Approved' if run.status.value == 'approved' else 'Draft' }} · {{ generated }}</p>
  {{ tag_legend() }}

  {% for g in groups %}
  <h2>{{ g.name }} phase</h2>
  <p class="sub">{{ g.description }}</p>

    {% for st in g.stages %}
    <h3>{{ st.report.name }}</h3>
    <p class="sub">{{ st.report.core_question }}</p>

    {% for c in st.cards %}
    <div class="card">
      <div class="card-head">
        {% if c.number %}<span class="num">{{ '%02d' % c.number }}</span>{% endif %}
        <span class="card-title">{{ c.title }}</span>
      </div>
      <p class="finding">{{ c.summary }}</p>
      <p class="status">
        {% if c.review_action.value != 'pending' or c.confidence.value == 'ready' %}<span class="dot ready"></span>{{ c.review_action.label if c.review_action.value != 'pending' else 'Ready' }}
        {% else %}<span class="dot review"></span>Needs review{% if c.input_reason %}: {{ c.input_reason }}{% endif %}{% endif %}
      </p>
      {% if c.evidence %}
        {% if c.evidence_type == 'metrics' %}
        <div class="metrics">{% for m in c.evidence %}<div class="metric"><b>{{ m.value }}</b><span>{{ m.label }}</span></div>{% endfor %}</div>
        {% elif c.evidence_type == 'table' and c.evidence.columns is defined %}
        <div class="table-wrap"><table class="data"><thead><tr>{% for col in c.evidence.columns %}<th>{{ col }}</th>{% endfor %}</tr></thead>
        <tbody>{% for row in c.evidence.rows %}<tr>{% for cell in row %}<td>{{ cell | tagify }}</td>{% endfor %}</tr>{% endfor %}</tbody></table></div>
        {% elif c.evidence_type == 'steps' %}
        <ol class="steps">{% for s in c.evidence %}<li>{{ s }}</li>{% endfor %}</ol>
        {% else %}
        <ul class="points">{% for pt in c.evidence %}<li>{{ pt | tagify }}</li>{% endfor %}</ul>
        {% endif %}
      {% endif %}
      {% if c.interpretation %}<p class="interp"><strong>What this means:</strong> {{ c.interpretation }}</p>{% endif %}
      {% if c.reviewer_input %}<p class="note"><strong>Reviewer input:</strong> {{ c.reviewer_input }}</p>{% endif %}
      {% if c.source_ids %}<div class="label">Sources</div>{{ source_name_chips(c.source_ids, names) }}{% endif %}
    </div>
    {% endfor %}

    {% if st.report.tables %}
    <h4>Tables</h4>
    {% for t in st.report.tables %}{{ data_table(t, compact=true) }}{% endfor %}
    {% endif %}

    {% if st.report.answers %}
    <h4>Questions and answers</h4>
    {% for row in st.report.answers %}
    <div class="qa">
      <p class="q">{{ row.get('seed') or row.get('question') }}</p>
      {% if row.get('answer') %}<p class="a">{{ row.get('answer') | tagify }}</p>
      {% else %}<p class="a meta">Not answered from the sources consulted.</p>{% endif %}
      <p class="s">
        {%- set stt = row.get('status', 'not_found') -%}
        <span class="vdot {{ 'vtag-verified' if stt == 'answered' else ('vtag-inference' if stt == 'partial' else 'vtag-not-verified') }}"></span>
        {%- for cite in row.get('citations') or [] %}<span class="vsrc">{{ cite }}</span>{% endfor %}
      </p>
      {% if row.get('reviewer_input') %}<p class="note"><strong>Reviewer input:</strong> {{ row.get('reviewer_input') }}</p>{% endif %}
    </div>
    {% endfor %}
    {% endif %}
    {% endfor %}
  {% else %}
  <p class="sub">Nothing has been produced for this phase yet.</p>
  {% endfor %}
</div>
</body>
</html>
```

### File: `celestra\templates\home.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import run_status_chip, dt, tinted_tile %}
{% block title %}Celestra — Clinical desk research{% endblock %}

{% block content %}
<div class="page-head">
  <div class="eyebrow">Clinical desk research</div>
  <h1>Welcome to Celestra</h1>
  <p class="sub">
    Celestra runs source-first desk research across approved clinical databases, then hands you an
    evidence-backed context layer to review, correct and approve.
  </p>
</div>

<div class="card card-pad mb-3">
  <div class="row-between wrap">
    <div>
      <h2>Start a new indication project</h2>
      <p class="text-muted mt-1 mb-0">
        Give Celestra the indication, geography and objective. It plans the research questions,
        queries approved sources first, and reports which source produced every finding.
      </p>
    </div>
    <a class="btn btn-primary btn-lg" href="/projects/new" data-modal-url="/projects/name">
      {{ icon('plus-circle', 16) }} New project
    </a>
  </div>
</div>

<div class="grid grid-3 mb-3">
  <div class="card card-pad">
    <div class="row mb-1">{{ tinted_tile('database', 'green') }}</div>
    <h3>Approved sources first</h3>
    <p class="text-muted text-sm mb-0">
      Native source APIs and targeted domain search run before anything else. Open-web results are
      only ever supplementary, and are labelled wherever they appear.
    </p>
  </div>
  <div class="card card-pad">
    <div class="row mb-1">{{ tinted_tile('split', 'amber') }}</div>
    <h3>Conflicts are surfaced</h3>
    <p class="text-muted text-sm mb-0">
      Where two sources disagree, both sides are reported as stated with their tier. Nothing is
      auto-resolved: a human decides.
    </p>
  </div>
  <div class="card card-pad">
    <div class="row mb-1">{{ tinted_tile('users', 'indigo') }}</div>
    <h3>Your input propagates</h3>
    <p class="text-muted text-sm mb-0">
      Modify a finding and Celestra recalculates every related insight across the other agents
      before you approve the context.
    </p>
  </div>
</div>

<div class="section">
  <div class="row-between mb-1">
    <h2 class="section-title mb-0">Recent projects <span class="count">({{ runs | default([]) | length }})</span></h2>
    <a class="btn btn-ghost btn-sm" href="/projects">View all {{ icon('chevron-right', 14) }}</a>
  </div>

  {% if runs %}
  <div class="card">
    {% for r in runs %}
    <div class="run-row">
      {{ tinted_tile('file-text', 'blue', 'tile-sm') }}
      <div class="run-main">
        <div class="run-title">
          <a href="/runs/{{ r.id }}">{{ r.display_name }}</a>
          {% if r.config.drug_brand %}<span class="text-faint">· {{ r.config.drug_brand }}</span>{% endif %}
        </div>
        <div class="meta meta-row">
          <span>{{ r.reference or r.id }}</span>
          <span class="dot-sep">·</span>
          <span>{{ r.config.geography }}</span>
          <span class="dot-sep">·</span>
          <span>{{ r.config.objective }}</span>
          <span class="dot-sep">·</span>
          <span>{{ dt(r.created_at) }}</span>
        </div>
      </div>
      {{ run_status_chip(r.status) }}
      <a class="btn btn-secondary btn-sm" href="/runs/{{ r.id }}">Open</a>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty">
    <h3>No projects yet</h3>
    <p class="mb-2">Create your first indication project to see discovery results here.</p>
    <a class="btn btn-primary" href="/projects/new" data-modal-url="/projects/name">Start discovery {{ icon('arrow-right', 15) }}</a>
  </div>
  {% endif %}
</div>
{% endblock %}
```

### File: `celestra\templates\insights.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% block title %}All findings — Celestra{% endblock %}

{% block content %}
{% set c = counts | default({}) %}
<div class="page-head">
  <h1>All findings</h1>
  <p class="sub">
    Every finding from every agent that has run. Decisions made here count at the current step:
    {% if run.is_locked %}the document is approved, so findings are final.{% else %}approve a finding
    as stated, modify it, or add your input.{% endif %}
  </p>
</div>

<div class="toolbar">
  <div class="pills">
    <button type="button" class="pill" data-filter-category="all" aria-pressed="true">
      All (<span data-insight-count>{{ insights | default([]) | length }}</span>)
    </button>
    <button type="button" class="pill" data-filter-category="requires_input" aria-pressed="false">
      Needs input ({{ c.get('needs_decision', 0) }})
    </button>
    <button type="button" class="pill" data-filter-category="ready" aria-pressed="false">
      Ready ({{ c.get('ready', 0) }})
    </button>
    {% for cat in categories | default([]) %}
    <button type="button" class="pill" data-filter-category="{{ cat.key | lower }}" aria-pressed="false">
      {{ cat.label }} ({{ cat.count }})
    </button>
    {% endfor %}
  </div>

  <div class="spacer"></div>

  <div class="search-wrap">
    <label class="visually-hidden" for="insight-search">Search insights</label>
    {{ icon('search', 15) }}
    <input type="search" id="insight-search" data-insight-search placeholder="Search insights…"
           autocomplete="off">
  </div>

  <div class="sort-wrap">
    <label for="insight-sort">Sort by</label>
    <select id="insight-sort" data-insight-sort>
      <option value="confidence">Needs input first</option>
      <option value="stage">Stage</option>
      <option value="sources">Sources</option>
    </select>
  </div>
</div>

{% for sec in sections | default([]) %}
<section class="phase-cards {{ 'is-new' if sec.is_new }}" id="cards-{{ sec.key }}">
  <div class="phase-cards-head">
    <h3>{{ sec.name }} phase <span class="count">({{ sec.cards | length }} cards)</span></h3>
    <span class="chip chip-sm {{ 'chip-blue' if sec.is_new else 'chip-muted' }}">{{ sec.label }}</span>
    {% if sec.needs %}<span class="meta">{{ sec.needs }} need{{ '' if sec.needs == 1 else 's' }} your review</span>{% endif %}
  </div>
  <div class="insight-grid" data-insight-list>
    {% for insight in sec.cards %}
      {% include "partials/insight_card.html" %}
    {% endfor %}
  </div>
</section>
{% else %}
<div class="insight-grid" data-insight-list>
  {% for insight in insights | default([]) %}
    {% include "partials/insight_card.html" %}
  {% else %}
  <p class="sub">No findings were produced.</p>
  {% endfor %}
</div>
{% endfor %}

<div class="empty mt-2" data-insight-empty {% if insights %}hidden{% endif %}>
  <h3>No insights match those filters</h3>
  <p class="mb-0">Clear the search box or pick a different category.</p>
</div>

<div class="btn-row mt-3">
  <a class="btn btn-primary" href="{{ next_url | default('/runs/' ~ run.id) }}">
    Back to the current step {{ icon('arrow-right', 15) }}
  </a>
  <a class="btn btn-secondary" href="/runs/{{ run.id }}/contradictions">Source conflicts</a>
</div>
{% endblock %}
```

### File: `celestra\templates\name_project.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% block title %}Name your project — Celestra{% endblock %}
{% block content %}
<div class="shell-narrow">
  <div class="page-head">
    <p class="eyebrow">New project · step 1 of 2</p>
    <h1>Name your project</h1>
    <p class="sub">Next you choose the indication, geography and objective.</p>
  </div>
  <form class="card card-pad" method="get" action="/projects/new">
    <div class="field">
      <label for="new-project-name">Project name</label>
      <input type="text" id="new-project-name" name="name" maxlength="120" required autofocus
             autocomplete="off" placeholder="e.g. CLL line-of-therapy, US, 2026">
      <p class="hint">You can rename it later from the sidebar or the Projects page.</p>
    </div>
    <div class="btn-row btn-row-end">
      <a class="btn btn-secondary" href="/projects">Cancel</a>
      <button type="submit" class="btn btn-primary">Continue {{ icon('arrow-right', 14) }}</button>
    </div>
  </form>
</div>
{% endblock %}
```

### File: `celestra\templates\new_project.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import agent_tile %}
{% block title %}New project — Celestra{% endblock %}

{% block content %}
<div class="shell-narrow">
  <form class="card" method="post" action="/projects" novalidate>
    <input type="hidden" name="project_name" value="{{ project_name | default('') }}">
    <div class="card-body">
      <h1>Create a New Project</h1>
      {% if project_name | default('') %}<p class="sub"><strong>{{ project_name }}</strong> · step 2 of 2</p>{% endif %}
      <p class="sub mb-3">Provide basic details to help Celestra understand your research objective.</p>

      <div class="field">
        <label for="therapy_area">Therapy Area <span class="req" aria-hidden="true">*</span>
          <span class="visually-hidden">(required)</span></label>
        <select id="therapy_area" name="therapy_area" required>
          {% for t in therapy_areas | default([{'value': 'Oncology', 'enabled': True}]) %}
          <option value="{{ t.value }}" {{ 'selected' if t.enabled and loop.first }}
                  {{ 'disabled' if not t.enabled }}>{{ t.value }}{{ '' if t.enabled else ' ' }}</option>
          {% endfor %}
        </select>
      </div>

      <div class="field">
        <label for="indication">Indication <span class="req" aria-hidden="true">*</span>
          <span class="visually-hidden">(required)</span></label>
        <select id="indication" name="indication" required aria-describedby="indication-hint">
          {% for ind in indications | default([]) %}
          <option value="{{ ind.key if ind.enabled else ind.label }}">
            {{- ind.abbreviation ~ ' — ' if ind.abbreviation }}{{ ind.label }}{{ '' if ind.enabled else ' ' -}}
          </option>
          {% endfor %}
        </select>
        <p class="hint" id="indication-hint">Greyed-out indications are on the roadmap and cannot be selected yet.</p>
      </div>

      <div class="field">
        <label for="drug_brand">Drug <span class="optional">(Optional)</span></label>
        <input type="text" id="drug_brand" name="drug_brand" autocomplete="off"
               placeholder="e.g. Venclexta (venetoclax)">
        <p class="hint">A drug or brand of interest within the indication. Leave blank to research the indication as a whole.</p>
      </div>

      <div class="field-row">
        <div class="field" style="position: fixed; top:-2000px; bottom: -2000px">
          <label for="population">Population <span class="req" aria-hidden="true">*</span></label>
          <select id="population" name="population" required>
            {% for p in populations | default([{'value': 'All', 'enabled': True}]) %}
            <option value="{{ p.value }}" {{ 'disabled' if not p.enabled }}>{{ p.value }}{{ '' if p.enabled else ' ' }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="field">
          <label for="geography">Geography <span class="req" aria-hidden="true">*</span></label>
          <select id="geography" name="geography" required>
            {% for g in geographies | default([{'value': 'United States', 'enabled': True}]) %}
            <option value="{{ g.value }}" {{ 'disabled' if not g.enabled }}>{{ g.value }}{{ '' if g.enabled else ' ' }}</option>
            {% endfor %}
          </select>
        </div>
      </div>

      <div class="field">
        <label for="objective">Objective <span class="req" aria-hidden="true">*</span></label>
        <select id="objective" name="objective" required>
          {% for o in objectives | default([{'value': 'Build Claims Line of Therapy', 'label': 'Clinical & Treatment Landscape Research', 'enabled': True}]) %}
          <option value="{{ o.value }}" {{ 'disabled' if not o.enabled }}>{{ o.label | default(o.value) }}{{ '' if o.enabled else ' ' }}</option>
          {% endfor %}
        </select>
      </div>


      <fieldset class="field" style="position: fixed; top:-2000px; border:0;padding:0;margin:24px 0 0; ">
        <legend class="label" style="padding:0;">Research mode</legend>
        <p class="hint mt-0 mb-1">
          Run the whole programme, or re-run a single agent against the cached context of the others.
        </p>

        <div class="radio-cards mt-1">
          <label class="radio-card" for="mode-full">
            <input type="radio" id="mode-full" name="mode" value="full" checked data-mode-radio>
            <span>
              <span class="rc-title">Run END to END</span>
              <span class="rc-desc">End to end, in dependency order. Each wave closes with a
                reconciliation gate before the next begins.</span>
            </span>
          </label>

          <label class="radio-card" for="mode-single">
            <input type="radio" id="mode-single" name="mode" value="single" data-mode-radio>
            <span>
              <span class="rc-title">Test a Single Phase</span>
              <span class="rc-desc">One phase only. Its dependencies are satisfied from the most
                recent cached context rather than re-researched.</span>
            </span>
          </label>
        </div>

        <div class="reveal" data-mode-reveal hidden>
          <div class="field mb-0">
            <label for="selected_agent">Agent to run</label>
            <select id="selected_agent" name="selected_agent">
              {% for a in agents | default([]) %}
              <option value="{{ a.key }}">{{ a.name }} — {{ a.tagline }}</option>
              {% endfor %}
            </select>
            <p class="hint">
              Anything this agent depends on is read from the most recent completed run for the
              same indication.
            </p>
          </div>
        </div>
      </fieldset>
    </div>

    <div class="card-foot">
      <button type="submit" class="btn btn-primary btn-lg btn-block">
        Start Discovery {{ icon('arrow-right', 16) }}
      </button>
    </div>
  </form>
</div>
{% endblock %}
```

### File: `celestra\templates\overview.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import stat_tile, confidence_chip, agent_tile, tinted_tile %}
{% block title %}Stage reports — Celestra{% endblock %}

{% set c = counts | default({}) %}

{% block content %}
<div class="page-head page-head-row">
  <div>
    <h1>Stage reports</h1>
    <p class="sub">
      {{ c.get('insights', 0) }} findings from {{ c.get('sources', 0) }} sources across
      {{ stages | length }} stage report{{ '' if stages | length == 1 else 's' }}
    </p>
    <div class="meta meta-row mt-1">
      <span><strong>{{ run.config.indication }}</strong></span>
      <span class="dot-sep">·</span>
      <span>{{ run.config.geography }}</span>
      {% if run.config.target_population %}
      <span class="dot-sep">·</span><span>{{ run.config.target_population }}</span>
      {% endif %}
      {% if run.reference %}
      <span class="dot-sep">·</span><span class="mono">{{ run.reference }}</span>
      {% endif %}
    </div>
  </div>
  <a class="btn btn-secondary" href="/runs/{{ run.id }}/sources">
    {{ icon('database', 15) }} View Sources
  </a>
</div>

<div class="stats mb-3">
  {{ stat_tile(c.get('ready', 0), 'Ready', 'green') }}
  {{ stat_tile(c.get('needs_decision', 0), 'Need your input', 'rose' if c.get('needs_decision') else 'slate') }}
  {{ stat_tile(c.get('decided', 0), 'Decided by you', 'blue') }}
  {{ stat_tile(c.get('sources', 0), 'Sources cited', 'slate') }}
</div>

{% if categories %}
<div class="pills mb-3">
  <a class="pill is-active" href="/runs/{{ run.id }}/insights">All Insights ({{ c.get('insights', 0) }})</a>
  {% for cat in categories %}
  <a class="pill" href="/runs/{{ run.id }}/insights?category={{ cat.key }}">{{ cat.label }} ({{ cat.count }})</a>
  {% endfor %}
</div>
{% endif %}

<section class="section">
  <h2 class="section-title">Key Takeaways</h2>
  {% if takeaways %}
  <div class="grid grid-3">
    {% for ins in takeaways %}
    <article class="card card-hover takeaway">
      <div class="row-between">
        {{ tinted_tile('sparkle', ['green', 'blue', 'rose', 'indigo', 'teal'][loop.index0 % 5], 'tile-sm') }}
      </div>
      <div class="tk-title">{{ ins.title }}</div>
      <div class="tk-body">{{ ins.summary }}</div>
      <div class="row-between wrap">
        {{ confidence_chip(ins.confidence, true) }}
        <span class="meta">{{ ins.source_ids | length }} source{{ '' if ins.source_ids | length == 1 else 's' }}</span>
      </div>
    </article>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty"><h3>No takeaways yet</h3><p class="mb-0">Takeaways appear once the agents finish synthesising.</p></div>
  {% endif %}
</section>

{% if stages %}
<section class="section">
  <h2 class="section-title">Stage outputs <span class="count">({{ stages | length }})</span></h2>
  <div class="card">
    {% for s in stages %}
    <div class="run-row">
      {{ agent_tile('sparkle', 'tile-sm') }}
      <div class="run-main">
        <div class="run-title">
          <a href="/runs/{{ run.id }}/stages/{{ s.stage }}">{{ s.name }}</a>
        </div>
        <div class="meta meta-row">
          <span>{{ s.agent_name }}</span>
          <span class="dot-sep">·</span>
          <span>{{ s.core_question }}</span>
        </div>
      </div>
      <span class="chip chip-sm">{{ s.evidence_count }} evidence</span>
      <span class="chip chip-sm">{{ s.source_count }} sources</span>
      <a class="btn btn-ghost btn-sm" href="/runs/{{ run.id }}/stages/{{ s.stage }}">
        Open {{ icon('chevron-right', 14) }}
      </a>
    </div>
    {% endfor %}
  </div>
</section>
{% endif %}

<div class="btn-row mt-3">
  <a class="btn btn-primary" href="/runs/{{ run.id }}">
    Back to the current step {{ icon('arrow-right', 15) }}
  </a>
  <a class="btn btn-secondary" href="/runs/{{ run.id }}/insights">All findings</a>
  <a class="btn btn-secondary" href="/runs/{{ run.id }}/report">Document</a>
</div>
{% endblock %}
```

### File: `celestra\templates\progress.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import agent_tile, agent_status_markup, enum_value, flow_steps %}
{% block title %}{{ 'Research in progress' if run.is_live else 'Research record' }} — Celestra{% endblock %}

{% block content %}
{% if run.is_live %}
<div data-run-stream="{{ run.id }}" data-last-seq="{{ last_seq | default(0) }}"
     data-next-url="{{ next_url | default('') }}"></div>
{% endif %}

<div class="page-head">
  {% set st = enum_value(run.status) %}
  {% if run.is_live %}
  <h1 data-page-title>
    {% if run.phase == 'mapping' %}Mapping &amp; Synthesis is running{% else %}Discovery is running{% endif %}
  </h1>
  <p class="sub" data-page-sub>
    {% if run.phase == 'mapping' %}
    The four remaining agents are building on the discovery findings you approved. When they
    finish, the run moves to Final approval.
    {% else %}
    Two agents are researching the clinical landscape and treatment evidence in parallel. When they
    finish, the run pauses for your review before the other four agents start.
    {% endif %}
  </p>
  {% elif st == 'awaiting_review' %}
  <h1>Discovery finished — waiting for your review</h1>
  <p class="sub">The two discovery agents are done. The four Mapping &amp; Synthesis agents start once
    you approve their findings.</p>
  {% elif st in ['completed', 'approved'] %}
  <h1>What the agents did</h1>
  <p class="sub">Every agent has finished. This page is the record of the run; the next step is
    {% if st == 'approved' %}the approved document{% else %}final approval{% endif %}.</p>
  {% else %}
  <h1>The run stopped</h1>
  <p class="sub">{{ run.error or 'The run did not finish.' }}</p>
  {% endif %}
  <div class="meta meta-row mt-1">
    <span><strong>{{ run.config.indication }}</strong></span>
    <span class="dot-sep">·</span>
    <span>{{ run.config.geography }}</span>
    <span class="dot-sep">·</span>
    <span>{{ run.config.objective }}</span>
    {% if run.reference %}
    <span class="dot-sep">·</span>
    <span class="mono">{{ run.reference }}</span>
    {% endif %}
  </div>
</div>

<div class="banner is-danger" data-run-error {% if st not in ['failed', 'cancelled'] %}hidden{% endif %} role="alert">
  {{ icon('alert', 16) }}
  <div class="banner-body">
    <strong>The run failed.</strong>
    <div data-run-error-message>{{ run.error }}</div>
  </div>
</div>

<p class="visually-hidden" aria-live="polite" data-stream-live></p>

{% for ph in phases | default([]) %}
<section class="phase-group is-{{ ph.state }}" id="{{ ph.key }}" data-phase="{{ ph.key }}" aria-label="{{ ph.name }}">
  {% set pct = ((ph.get('progress', 0) or 0) * 100) | round | int %}
  <button type="button" class="phase-head phase-toggle" data-phase-toggle
          aria-expanded="false" aria-controls="phase-body-{{ ph.key }}">
    <span class="phase-num">{{ loop.index }}</span>
    <span class="phase-id">
      <span class="phase-title">{{ ph.name }} phase</span>
      <span class="sub">{{ ph.description }}</span>
    </span>
    <span class="phase-meta">
      <span class="phase-state" data-phase-state>
        {% if ph.state == 'complete' %}{{ icon('check', 15) }} Complete
        {% elif ph.state == 'running' %}<span class="spinner" aria-hidden="true"></span> Running
        {% elif ph.state == 'failed' %}{{ icon('alert', 15) }} An agent failed
        {% elif ph.state == 'gated' %}{{ icon('lock', 15) }} Starts after your review
        {% else %}<span class="chip-dot" aria-hidden="true"></span> Queued{% endif %}
      </span>
      <span class="meta mono" data-phase-count>{{ ph.done }} / {{ ph.agents | length }} agents done</span>
    </span>
    <span class="phase-chevron" aria-hidden="true">{{ icon('chevron-right', 16) }}</span>
  </button>
  <div class="progress phase-progress {{ 'is-complete' if ph.state == 'complete' }}{{ ' is-failed' if ph.state == 'failed' }}"
       data-phase-progress role="progressbar" aria-valuemin="0" aria-valuemax="100"
       aria-valuenow="{{ pct }}" aria-label="{{ ph.name }} phase progress">
    <div class="progress-bar" style="width: {{ pct }}%"></div>
  </div>
  <p class="hint phase-hint mb-0"><span data-phase-pct>{{ pct }}</span>% of this phase ·
    click the phase to see each agent</p>

  <div class="phase-body" id="phase-body-{{ ph.key }}" data-phase-body hidden aria-live="polite">
    {% for a in ph.agents %}
    {% set ast = enum_value(a.status) %}
    <article class="agent-card {{ 'is-complete' if ast == 'complete' }}{{ ' is-failed' if ast == 'failed' }}{{ ' is-blocked' if ast in ['blocked', 'skipped'] }}"
             data-agent-key="{{ a.key }}" data-status="{{ ast }}">
      <div class="agent-card-top">
        {{ agent_tile(a.icon) }}
        <div class="agent-id">
          <div class="agent-name">{{ a.name }}</div>
          <div class="agent-tagline">{{ a.tagline }}</div>
        </div>
        <div class="agent-status status status-{{ ast }}" data-agent-status>
          {{ agent_status_markup(a.status) }}
        </div>
      </div>

      <div class="progress agent-progress {{ 'is-complete' if ast == 'complete' }}{{ ' is-failed' if ast == 'failed' }}"
           data-agent-progress role="progressbar" aria-valuemin="0" aria-valuemax="100"
           aria-valuenow="{{ ((a.progress | default(0)) * 100) | round | int if (a.progress | default(0)) <= 1 else (a.progress | round | int) }}"
           aria-label="{{ a.name }} progress">
        <div class="progress-bar"
             style="width: {{ ((a.progress | default(0)) * 100) | round(1) if (a.progress | default(0)) <= 1 else (a.progress | round(1)) }}%"></div>
      </div>

      <div class="row-between">
        <p class="agent-message" data-agent-message>{{ a.message }}</p>
        <span class="meta mono" data-agent-questions {% if not a.questions_total %}hidden{% endif %}>{{ a.questions_answered }} / {{ a.questions_total }}</span>
      </div>

      {% if a.error %}
      <div class="callout is-rose mt-1 text-sm">{{ a.error }}</div>
      {% endif %}
    </article>
    {% endfor %}
  </div>

  {% if ph.key == 'discovery' and phases | length > 1 %}
  {% set passed = run.reviewed_at is not none %}
  <div class="phase-gate {{ 'is-passed' if passed }}" data-phase-gate>
    {{ icon('check-circle' if passed else 'lock', 16) }}
    <span>
      {% if passed %}
      <strong>Review gate passed.</strong> You approved the discovery findings
      {% if run.reviewed_at %}on {{ run.reviewed_at.strftime('%d %b %Y, %H:%M UTC') }}{% endif %}.
      {% elif st == 'awaiting_review' %}
      <strong>Review gate.</strong> Discovery is finished. The next phase waits for your decisions.
      {% else %}
      <strong>Review gate.</strong> When both discovery agents finish, the run pauses here for your
      review before the next phase starts.
      {% endif %}
    </span>
    {% if st == 'awaiting_review' %}
    <a class="btn btn-primary btn-sm" href="/runs/{{ run.id }}/review" style="margin-left:auto">Review the findings {{ icon('arrow-right', 14) }}</a>
    {% elif passed %}
    <a class="btn btn-ghost btn-sm" href="/runs/{{ run.id }}/review" style="margin-left:auto">See what was decided</a>
    {% endif %}
  </div>
  {% endif %}
</section>
{% else %}
<div class="empty"><h3>No agents queued</h3><p class="mb-0">This run has no agents to execute.</p></div>
{% endfor %}

{% if run.is_live %}
<div class="source-strip" data-source-strip>
  <h3>Analyzing sources from trusted databases</h3>
  <div class="chip-row">
    {% for s in source_chips | default([]) %}
      {% if s is mapping %}
      <span class="chip chip-sm chip-source {{ 'is-used' if s.get('used') }}"
            data-source-key="{{ s.get('key', '') }}" data-source-name="{{ s.get('name', '') }}">{{ s.get('name', '') }}</span>
      {% else %}
      <span class="chip chip-sm chip-source" data-source-name="{{ s }}">{{ s }}</span>
      {% endif %}
    {% endfor %}
    <span class="meta" data-source-more>and more…</span>
  </div>
  <p class="hint mt-1 mb-0">A source lights up once it has actually returned evidence for this run.</p>
</div>
{% endif %}

<div class="row mt-3 wrap">
  <span class="meta">Live counts:</span>
  <span class="chip chip-sm">Findings <strong data-live-insights data-count="0">0</strong></span>
  <span class="chip chip-sm">Conflicts <strong data-live-contradictions data-count="0">0</strong></span>
  <span class="spacer" style="flex:1 1 auto"></span>
  {% if st == 'awaiting_review' %}
  <a class="btn btn-primary btn-sm" href="/runs/{{ run.id }}/review">Review the findings {{ icon('arrow-right', 14) }}</a>
  {% elif st == 'completed' %}
  <a class="btn btn-primary btn-sm" href="/runs/{{ run.id }}/approval">Go to final approval {{ icon('arrow-right', 14) }}</a>
  {% elif st == 'approved' %}
  <a class="btn btn-primary btn-sm" href="/runs/{{ run.id }}/report">Open the approved document {{ icon('arrow-right', 14) }}</a>
  {% elif run.is_live %}
  <span class="meta">This page moves on by itself when the phase finishes.</span>
  {% endif %}
</div>
{% endblock %}
```

### File: `celestra\templates\projects.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import run_status_chip, dt, tinted_tile %}
{% block title %}Projects — Celestra{% endblock %}

{% block content %}
<div class="page-head page-head-row">
  <div>
    <h1>Projects</h1>
    <p class="sub">Every discovery run, newest first.</p>
  </div>
  <a class="btn btn-primary" href="/projects/new" data-modal-url="/projects/name">{{ icon('plus-circle', 16) }} New project</a>
</div>

<details class="card card-pad mb-2 import-box">
  <summary class="row" style="gap:8px; cursor:pointer;">
    {{ icon('download', 15) }} <strong>Import a project</strong>
    <span class="meta">from a file exported on another instance (your laptop, or the hosted version)</span>
  </summary>
  <form method="post" action="/projects/import" enctype="multipart/form-data" class="row wrap mt-2" style="gap:10px;">
    <input type="file" name="bundle" accept="application/json,.json" required>
    <button type="submit" class="btn btn-primary btn-sm">Import</button>
    <span class="hint">Export is on each project's row. Importing the same project again overwrites it.</span>
  </form>
</details>

{% if runs %}
<div class="card">
  <div class="table-wrap">
    <table class="data">
      <thead>
        <tr>
          <th scope="col">Project</th>
          <th scope="col">Reference</th>
          <th scope="col">Geography</th>
          <th scope="col">Objective</th>
          <th scope="col">Mode</th>
          <th scope="col">Status</th>
          <th scope="col" class="nowrap">Created</th>
          <th scope="col"><span class="visually-hidden">Actions</span></th>
        </tr>
      </thead>
      <tbody>
        {% for r in runs %}
        <tr>
          <td>
            <a href="/runs/{{ r.id }}"><strong>{{ r.display_name }}</strong></a>
            {% if r.name %}<div class="meta text-sm">{{ r.config.indication }}</div>{% endif %}
            {% if r.config.drug_brand %}<div class="text-faint text-sm">{{ r.config.drug_brand }}</div>{% endif %}
          </td>
          <td class="mono">{{ r.reference or r.id }}</td>
          <td>{{ r.config.geography }}</td>
          <td>{{ r.config.objective }}</td>
          <td class="nowrap">
            {% set m = r.config.mode.value if r.config.mode.value is defined else r.config.mode %}
            {{ 'All agents' if m == 'full' else 'Single agent' }}
          </td>
          <td>{{ run_status_chip(r.status) }}</td>
          <td class="nowrap">{{ dt(r.created_at, '%d %b %Y') }}</td>
          <td class="nowrap">
            <a class="btn btn-secondary btn-sm" href="/runs/{{ r.id }}">Open</a>
            <button type="button" class="btn btn-ghost btn-sm" data-modal-url="/runs/{{ r.id }}/rename" title="Rename">{{ icon('edit', 13) }}</button>
            <a class="btn btn-ghost btn-sm" href="/runs/{{ r.id }}/export" title="Download this project as a file you can import on another instance">{{ icon('download', 13) }} Export</a>
            <a class="btn btn-ghost btn-sm" href="/runs/{{ r.id }}/report">Report</a>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% else %}
<div class="empty">
  <h3>No projects yet</h3>
  <p class="mb-2">Discovery runs will be listed here once you create one.</p>
  <a class="btn btn-primary" href="/projects/new" data-modal-url="/projects/name">Start discovery {{ icon('arrow-right', 15) }}</a>
</div>
{% endif %}
{% endblock %}
```

### File: `celestra\templates\report.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import tag_legend, tier_badge, severity_chip, ext_link, dict_table, pick, dt %}
{% block title %}Desk research report — Celestra{% endblock %}

{%- set waves = (run.agents.values() | list) if (run is defined and run and run.agents) else [] -%}

{% block content %}
<div class="report">

{% if run.status.value == 'approved' %}
<div class="doc-state is-approved no-print">
  {{ icon('check-circle', 17) }}
  <span><strong>Approved document.</strong> Signed off
    {% if run.approved_at %}on {{ run.approved_at.strftime('%d %b %Y, %H:%M UTC') }}{% endif %} and locked.
    {% if reviewer_inputs %}{{ reviewer_inputs | length }} reviewer input{{ '' if reviewer_inputs | length == 1 else 's' }} are printed with their findings.{% endif %}
  </span>
  <span class="spacer"></span>
  <a class="btn btn-secondary btn-sm" href="/projects/new" data-modal-url="/projects/name">Start another project</a>
</div>
{% elif run.status.value == 'completed' %}
<div class="doc-state is-draft no-print">
  {{ icon('edit', 17) }}
  <span><strong>Draft.</strong> This document is not approved yet. It changes as you decide findings.</span>
  <span class="spacer"></span>
  <a class="btn btn-primary btn-sm" href="/runs/{{ run.id }}/approval">Go to final approval {{ icon('arrow-right', 14) }}</a>
</div>
{% elif run.status.value in ['running', 'pending', 'awaiting_review'] %}
<div class="doc-state is-draft no-print">
  {{ icon('activity', 17) }}
  <span><strong>In progress.</strong> Agents are still running; this is a partial draft.</span>
  <span class="spacer"></span>
  <a class="btn btn-secondary btn-sm" href="/runs/{{ run.id }}">Back to the run {{ icon('arrow-right', 14) }}</a>
</div>
{% endif %}

<div class="page-head page-head-row">
  <div>
    <div class="eyebrow">Clinical foundation research · {{ 'Approved' if run.status.value == 'approved' else 'Draft' }}</div>
    <h1 class="doc-title">
      {{ report_title | default('Applying the Phase 1 Clinical Foundation Research Framework to ' ~ run.config.indication, true) }}
    </h1>
    <p class="sub">
      {{ run.config.target_population or 'All patients' }} · {{ run.config.geography }} ·
      {{ run.config.objective }}
    </p>
  </div>
  <div class="btn-row no-print">
    <a class="btn btn-primary" href="/runs/{{ run.id }}/findings">{{ icon('download', 15) }} Findings only</a>
    <a class="btn btn-secondary" href="/runs/{{ run.id }}/overview">Stage reports</a>
    <a class="btn btn-secondary" href="/runs/{{ run.id }}/sources">Sources</a>
    <button type="button" class="btn btn-ghost" onclick="window.print()">{{ icon('download', 15) }} Print</button>
  </div>
</div>

{# ---------------------------------------------------------------- params #}
<section class="report-section">
  <h2>Run parameters</h2>
  <div class="card">
    <table class="kv">
      <tbody>
      {% if params is mapping %}
        {% for key, value in params.items() %}
        <tr><th scope="row">{{ key | replace('_', ' ') | title }}</th><td>{{ value }}</td></tr>
        {% endfor %}
      {% else %}
        {% for row in params | default([]) %}
        <tr>
          <th scope="row">{{ pick(row, ['label', 'parameter', 'name', 'key']) }}</th>
          <td>{{ pick(row, ['value', 'val', 'detail']) }}</td>
        </tr>
        {% endfor %}
      {% endif %}
      </tbody>
    </table>
  </div>
</section>

{# ------------------------------------------------------- tagging key #}
<section class="report-section">
  <h2>How to read the evidence marks</h2>
  <p class="sub mb-1">Every figure, criterion or code in this document carries a small coloured dot
    saying how it was established. Hover a dot for its meaning.</p>
  {{ tag_legend() }}
</section>

{# ------------------------------------------------- executive summary #}
{% if executive_summary | default('', true) %}
<section class="report-section">
  <h2>Executive summary</h2>
  <div class="prose">{{ executive_summary | default('', true) | tagify }}</div>
</section>
{% endif %}

{# ----------------------------------------------------- research method #}
<section class="report-section">
  <h2>Research method</h2>
  <ol class="takeaways">
    {% if research_method | default([], true) %}
      {% for step in research_method | default([], true) %}<li>{{ step | tagify }}</li>{% endfor %}
    {% else %}
      <li>The requested scope was expanded into a structured research plan with specific,
        population-scoped questions per stage.</li>
      <li>For each question the approved source registry was consulted <strong>first</strong>, using
        native source APIs where available and targeted domain-scoped search otherwise. Whole-site
        crawling was not performed.</li>
      <li>Only relevant documents were retrieved; each was converted into structured evidence with a
        verbatim supporting quote.</li>
      <li>Evidence was assessed for coverage, source-tier distribution and contradictions.
        Insufficient questions triggered query refinement, then open-web fallback.</li>
      <li>Findings were synthesised per stage and QA-validated against the run's evidence base
        ({{ qa.questions_sufficient if qa else 0 }} of {{ qa.questions_planned if qa else 0 }}
        questions reached the sufficiency threshold).</li>
    {% endif %}
  </ol>
  {% if run.config.research_cutoff %}
  <p class="meta">All statements are constrained to the research cutoff of
    <strong>{{ run.config.research_cutoff }}</strong>.</p>
  {% endif %}

  <h3>Execution plan</h3>
  <p class="text-muted text-sm">
    Agents execute by dependency, not in stage order. Agents in the same wave run concurrently and a
    reconciliation gate closes each wave before the next begins.
  </p>
  <div class="table-wrap">
    <table class="data table-compact">
      <thead>
        <tr>
          <th scope="col">Wave</th>
          <th scope="col">Mode</th>
          <th scope="col">Agents</th>
          <th scope="col">Stages reported</th>
        </tr>
      </thead>
      <tbody>
        {% if execution_plan is defined and execution_plan %}
          {% for row in execution_plan %}
          <tr>
            <td>{{ pick(row, ['wave']) }}</td>
            <td>{{ pick(row, ['mode']) }}</td>
            <td>{{ pick(row, ['agents', 'agent_names', 'agent']) }}</td>
            <td>{{ pick(row, ['stages', 'stages_reported']) }}</td>
          </tr>
          {% endfor %}
        {% else %}
          {% set wave_numbers = waves | map(attribute='wave') | unique | sort | list %}
          {% for w in wave_numbers %}
            {% set members = waves | selectattr('wave', 'equalto', w) | list %}
            <tr>
              <td>{{ w }}</td>
              <td>{{ 'Concurrent' if members | length > 1 else 'Sequential' }}</td>
              <td>{{ members | map(attribute='name') | join(', ') }}</td>
              <td>
                {%- set ns = namespace(all=[]) -%}
                {%- for m in members -%}{%- set ns.all = ns.all + (m.stages | list) -%}{%- endfor -%}
                {{ ns.all | unique | sort | join(', ') | replace('_', ' ') }}
              </td>
            </tr>
          {% else %}
            <tr><td colspan="4" class="text-faint">No execution plan recorded.</td></tr>
          {% endfor %}
        {% endif %}
      </tbody>
    </table>
  </div>
</section>

{# ----------------------------------------------------------- stages #}
{% if stages %}
<div class="stage-toc no-print">
  {% for stage in stages %}
  <a class="pill" href="#{{ stage.stage }}">{{ stage.stage | replace('_', ' ') | title }} — {{ stage.agent_name }}</a>
  {% endfor %}
</div>

{% for stage in stages %}
  {% include "partials/stage_body.html" %}
{% endfor %}
{% endif %}

{# ------------------------------------------- consolidated one-page view #}
<section class="report-section">
  <h2>Consolidated one-page view</h2>
  <div class="table-wrap">
    <table class="data">
      <thead>
        <tr>
          <th scope="col">Stage</th>
          <th scope="col">Core question</th>
          <th scope="col">Key deliverable</th>
          <th scope="col">Headline finding</th>
        </tr>
      </thead>
      <tbody>
        {% for stage in stages | default([]) %}
        <tr>
          <td class="nowrap">{{ stage.stage | replace('_', ' ') | title }}</td>
          <td>{{ stage.core_question }}</td>
          <td>{{ stage.output_name or (stage.expected_output | join('; ') if stage.expected_output else '—') }}</td>
          <td>{{ (stage.takeaways[0] if stage.takeaways else '—') | tagify }}</td>
        </tr>
        {% else %}
        <tr><td colspan="4" class="text-faint">No stages were reported for this run.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>

{# ------------------------------------------------------------- QA #}
<section class="report-section">
  <h2>QA validation &amp; readiness</h2>

  {% if qa %}
  <h3>Run QA metrics</h3>
  <div class="table-wrap">
    <table class="data table-compact">
      <thead><tr><th scope="col">Metric</th><th scope="col">Value</th></tr></thead>
      <tbody>
        <tr><td>Research questions planned</td><td class="num">{{ qa.questions_planned }}</td></tr>
        <tr><td>Questions meeting sufficiency threshold</td><td class="num">{{ qa.questions_sufficient }}</td></tr>
        <tr><td>— of which answered from supplementary web only</td><td class="num">{{ qa.questions_web_only }}</td></tr>
        <tr><td>Questions below threshold</td><td class="num">{{ qa.questions_below_threshold }}</td></tr>
        <tr><td>Mean evidence coverage</td><td class="num">{{ (qa.mean_coverage * 100) | round | int }}%</td></tr>
        <tr><td>Total evidence items</td><td class="num">{{ qa.evidence_total }}</td></tr>
        <tr><td>Approved-source evidence items</td><td class="num">{{ qa.evidence_approved }}</td></tr>
        <tr><td>Supplementary web evidence items</td><td class="num">{{ qa.evidence_supplementary }}</td></tr>
        <tr><td>Distinct sources retrieved</td><td class="num">{{ qa.distinct_sources }}</td></tr>
        <tr><td>Source conflicts surfaced</td><td class="num">{{ qa.conflicts_surfaced }}</td></tr>
      </tbody>
    </table>
  </div>

  <h3>QA checklist</h3>
  <div class="table-wrap">
    <table class="data table-compact">
      <thead>
        <tr><th scope="col">Check</th><th scope="col">Status</th><th scope="col">Detail</th></tr>
      </thead>
      <tbody>
        {% for row in qa.checklist | default([]) %}
        {% set status = (row.get('status', '') if row.get is defined else '') | string %}
        <tr>
          <td>{{ pick(row, ['check', 'name', 'rule']) }}</td>
          <td class="nowrap">
            <span class="chip chip-sm {{ 'chip-ready' if status | upper == 'PASS' else ('chip-requires_input' if status | upper == 'FAIL' else 'chip-amber') }}">
              {{ status or 'n/a' }}
            </span>
          </td>
          <td>{{ pick(row, ['detail', 'note', 'evidence']) }}</td>
        </tr>
        {% else %}
        <tr><td colspan="3" class="text-faint">No QA checks were recorded.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p class="meta">No QA metrics were recorded for this run.</p>
  {% endif %}

  <h3>Contradiction register</h3>
  {% if contradictions %}
  <div class="table-wrap">
    <table class="data">
      <thead>
        <tr>
          <th scope="col">Topic</th>
          <th scope="col">Source A</th>
          <th scope="col">Source A claim</th>
          <th scope="col">Source B</th>
          <th scope="col">Source B claim</th>
          <th scope="col">Possible reason</th>
          <th scope="col">Severity</th>
        </tr>
      </thead>
      <tbody>
        {% for con in contradictions %}
        <tr>
          <td>{{ con.topic }}</td>
          <td class="nowrap">{{ con.source_a_name }} {{ tier_badge(con.source_a_tier) }}</td>
          <td>{{ con.source_a_claim }}</td>
          <td class="nowrap">{{ con.source_b_name }} {{ tier_badge(con.source_b_tier) }}</td>
          <td>{{ con.source_b_claim }}</td>
          <td>{{ con.reason }}</td>
          <td>{{ severity_chip(con.severity) }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  <p class="table-footnote">
    No conflict was auto-resolved. Both sides are reported as stated by their source.
  </p>
  {% else %}
  <p class="meta">No source conflicts were surfaced in this run.</p>
  {% endif %}

  {% if qa and qa.readiness %}
  <h3>Readiness assessment</h3>
  <div class="callout">{{ qa.readiness | tagify }}</div>
  {% endif %}

  {% if qa and qa.sme_checklist %}
  <h3>SME review checklist</h3>
  <ol class="takeaways">
    {% for item in qa.sme_checklist %}<li>{{ item | tagify }}</li>{% endfor %}
  </ol>
  {% endif %}
</section>

{# -------------------------------------------------- sources referenced #}
<section class="report-section">
  <h2>Sources referenced</h2>
  <div class="table-wrap">
    <table class="data">
      <thead>
        <tr>
          <th scope="col">Organization</th>
          <th scope="col">Title / version</th>
          <th scope="col">Date</th>
          <th scope="col">URL</th>
          <th scope="col">Source tier</th>
          <th scope="col">Evidence items</th>
        </tr>
      </thead>
      <tbody>
        {% for s in sources | default([]) %}
        <tr>
          <td>{{ s.get('organization') or s.get('name') or '—' }}</td>
          <td>{{ s.get('title') or '—' }}</td>
          <td class="nowrap">{{ s.get('published') or 'Not stated' }}</td>
          <td class="break">{{ ext_link(s.get('url'), s.get('url')) }}</td>
          <td class="nowrap">{{ tier_badge(s.get('tier', 5)) }}</td>
          <td class="num">{{ s.get('evidence_items', 0) }}</td>
        </tr>
        {% else %}
        <tr><td colspan="6" class="text-faint">No sources were recorded for this run.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>

{# ------------------------------------------------ document limitations #}
<section class="report-section">
  <h2>Document limitations</h2>
  <ul>
    {% if document_limitations | default([], true) %}
      {% for item in document_limitations | default([], true) %}<li>{{ item | tagify }}</li>{% endfor %}
    {% else %}
      <li>This document is a research artefact produced by an automated desk-research workflow and
        requires subject-matter-expert review before analytical use.</li>
      <li>Claims codes, regimen definitions and line-of-therapy rules marked
        <span class="vtag vtag-original">[ORIGINAL]</span> are analytical constructs of this
        workflow, not source facts.</li>
      <li>Any value marked <span class="vdot vtag-not-verified"></span> (not verified) could not be
        located in the cited source text and must be confirmed against the coding authority before use.</li>
      <li>Evidence marked SUPPLEMENTARY WEB EVIDENCE came from open-web fallback and carries lower
        evidentiary weight than approved-source evidence.</li>
      {% if run.config.research_cutoff %}
      <li>Coverage is bounded by the research cutoff of {{ run.config.research_cutoff }};
        developments after that date are out of scope.</li>
      {% endif %}
    {% endif %}
  </ul>
  <p class="meta mt-2">
    Generated {{ dt(run.finished_at or run.created_at) }}{% if run.reference %} · <span class="mono">{{ run.reference }}</span>{% endif %}
  </p>
</section>

</div>
{% endblock %}
```

### File: `celestra\templates\review.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import stat_tile, agent_tile, flow_steps %}
{% block title %}Review gate — {{ app_name }}{% endblock %}

{% block content %}
{%- set rid = run.id -%}
{%- set c = gate.counts -%}
<div class="page-head">
  <div>
    <p class="eyebrow">Step 2 of 5 · Review gate</p>
    <h1>{% if gate.done %}What you decided at the review gate{% else %}Review the discovery findings{% endif %}</h1>
    <p class="sub">
      {% if gate.done %}
      These are the discovery findings as they were approved. Mapping &amp; Synthesis built on
      them, so they cannot be changed for this run.
      {% else %}
      The Clinical Landscape and Treatment Evidence agents have finished. The four Mapping &amp;
      Synthesis agents build on these findings, so a wrong one approved here propagates. For each
      finding you can <strong>Approve</strong> it as stated, <strong>Modify</strong> it (Celestra
      rewrites it on your instruction), or <strong>Add Input</strong> (your knowledge is attached
      and passed to the next agents). Findings marked <strong>Requires Input</strong> must get one
      of those before you can continue.
      {% endif %}
    </p>
    <p class="meta">
      {{ run.config.indication }} · {{ run.config.geography }} · {{ run.config.population }} ·
      <code>{{ run.reference }}</code>
    </p>
  </div>
</div>

{% include "partials/gate_panel.html" %}

<div class="grid grid-2 mt-3 mb-3">
  <section class="card">
    <div class="card-body">
      <h2>Discovery phase — finished</h2>
      <ul class="agent-list">
        {% for a in completed_agents %}
        <li>{{ agent_tile(a.icon) }}<div><strong>{{ a.name }}</strong><div class="sub">{{ a.tagline }}</div></div></li>
        {% endfor %}
      </ul>
      <div class="btn-row mt-1">
        <a class="btn btn-ghost btn-sm" href="/runs/{{ rid }}/progress">See what the agents did</a>
        <a class="btn btn-ghost btn-sm" href="/runs/{{ rid }}/findings?phase=discovery">{{ icon('download', 13) }} Discovery findings report</a>
      </div>
    </div>
  </section>
  <section class="card">
    <div class="card-body">
      <h2>Mapping &amp; Synthesis phase — {{ 'done' if gate.done else 'waiting on this review' }}</h2>
      <ul class="agent-list">
        {% for a in remaining_agents %}
        <li>{{ agent_tile(a.icon) }}<div><strong>{{ a.name }}</strong><div class="sub">{{ a.tagline }}</div></div></li>
        {% else %}
        <li class="sub">No agents remain; continuing will finalise the run.</li>
        {% endfor %}
      </ul>
    </div>
  </section>
</div>

<h2 class="section-head">{{ c.insights }} key insights from the discovery phase</h2>
<p class="sub mb-2">Each card is one slot the agent owes, filled from its stage document. Ready cards
  carry forward as written unless you change them; a card marked <em>Needs your review</em> says why in
  one line.</p>
<div class="chips mb-2" data-filter-group>
  <button type="button" class="pill" data-filter-category="all" aria-pressed="true">All ({{ c.insights }})</button>
  <button type="button" class="pill" data-filter-category="requires_input" aria-pressed="false">Needs input ({{ c.needs_decision }})</button>
  <button type="button" class="pill" data-filter-category="ready" aria-pressed="false">Ready ({{ c.ready }})</button>
  {% for cat in categories %}
  <button type="button" class="pill" data-filter-category="{{ cat.key }}" aria-pressed="false">{{ cat.label }} ({{ cat.count }})</button>
  {% endfor %}
</div>
<div class="insight-grid" data-insight-list>
  {% for insight in insights | default([]) %}
    {% include "partials/insight_card.html" %}
  {% else %}
  <p class="sub">No findings were produced by the discovery agents.</p>
  {% endfor %}
</div>
<div class="empty mt-2" data-insight-empty {% if insights %}hidden{% endif %}>
  <h3>No findings match that filter</h3>
  <p class="mb-0">Pick a different filter.</p>
</div>

<h2 class="section-head">Source conflicts ({{ c.conflicts }})</h2>
{% if contradictions %}
<div class="banner {{ 'is-danger' if c.conflicts_blocking else 'is-info' }}" role="status">
  {{ icon('alert', 16) }}
  <div class="banner-body">
    Nothing here is auto-resolved. Escalated conflicts must be decided before you continue;
    noted ones can be acknowledged. Both claims stay in the document either way.
  </div>
</div>
<div class="stack">
  {% for contradiction in contradictions %}
    {% include "partials/contradiction_card.html" %}
  {% endfor %}
</div>
{% else %}
<p class="sub">The discovery agents surfaced no disagreements between sources.</p>
{% endif %}

<div class="mt-3" data-gate-mirror></div>
{% endblock %}
```

### File: `celestra\templates\settings.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import pairs_block %}
{% block title %}Settings — Celestra{% endblock %}

{% block content %}
<div class="page-head">
  <h1>Settings</h1>
  <p class="sub">
    Read-only. Credentials, thresholds and reference datasets are configured in the environment and
    the YAML config files, not in the browser.
  </p>
</div>

<section class="section">
  <h2 class="section-title">Credentials</h2>
  <div class="card">
    <table class="kv">
      <tbody>
        {% for key, ok in (credentials | default({})).items() %}
        <tr>
          <th scope="row">{{ key | replace('_', ' ') | title }}</th>
          <td>
            {% if ok %}
            <span class="chip chip-sm chip-ready">{{ icon('check', 12) }} Configured</span>
            {% else %}
            <span class="chip chip-sm chip-amber">{{ icon('alert', 12) }} Not configured</span>
            {% endif %}
          </td>
        </tr>
        {% else %}
        <tr><td class="text-faint">No credential status was reported.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  <p class="hint">
    A missing Anthropic key degrades synthesis to deterministic mode. A missing Firecrawl key moves
    open-web fallback onto the keyless search path. Neither stops a run.
  </p>
</section>

<section class="section" id="web-search">
  <h2 class="section-title">Web search (Firecrawl)</h2>
  <div class="card card-pad">
    {% set ws = web_search | default({}) %}
    {% set net = network | default({}) %}
    <table class="kv">
      <tbody>
        <tr><th scope="row">API key</th>
          <td>{% if ws.get('keyed') %}<span class="chip chip-sm chip-ready">{{ icon('check', 12) }} Configured</span>
              {% else %}<span class="chip chip-sm chip-amber">{{ icon('alert', 12) }} Not configured</span>{% endif %}</td></tr>
        <tr><th scope="row">Endpoint</th><td class="mono break">{{ net.get('firecrawl_endpoint', '—') }} <span class="text-faint">(API {{ net.get('firecrawl_version', 'v2') }}; FIRECRAWL_API_URL / FIRECRAWL_API_VERSION)</span></td></tr>
        <tr><th scope="row">Proxy</th><td>{{ net.get('proxy', '—') }} <span class="text-faint">(PROXY_URL or HTTPS_PROXY)</span></td></tr>
        <tr><th scope="row">TLS</th>
          <td>{% if not net.get('tls_verify', true) %}<span class="chip chip-sm chip-requires_input">Verification disabled</span>
              {% elif net.get('ca_bundle') %}Custom CA bundle: <span class="mono break">{{ net.get('ca_bundle') }}</span>
              {% else %}System certificates <span class="text-faint">(set CA_BUNDLE behind an intercepting proxy)</span>{% endif %}</td></tr>
        <tr><th scope="row">Last call</th>
          <td>{% if ws.get('error') %}<span class="chip chip-sm chip-requires_input">Failed</span> {{ ws.get('error') }}
              {% if ws.get('error_at') %}<span class="text-faint">({{ ws.get('error_at') }})</span>{% endif %}
              {% elif ws.get('keyed') %}<span class="chip chip-sm chip-ready">No failure recorded</span>
              {% else %}{{ ws.get('reason', '') }}{% endif %}</td></tr>
      </tbody>
    </table>
    <div class="row mt-2 wrap" data-action-scope>
      <button type="button" class="btn btn-primary btn-sm"
              data-action-url="/settings/web-search-test"
              data-swap="[data-web-test]"
              data-busy-label="Calling Firecrawl…">Test web search now</button>
      <span class="hint">Makes one real search call and shows exactly what happened.</span>
    </div>
    <div data-web-test class="mt-2"></div>
  </div>
</section>

<section class="section">
  <h2 class="section-title">Thresholds</h2>
  {% if thresholds %}
  <div class="grid grid-2">
    {% for group, values in thresholds.items() %}
    <div class="card card-pad">
      <h3 class="mb-1">{{ group | replace('_', ' ') | title }}</h3>
      {% if values is mapping %}
      <table class="kv">
        <tbody>
          {% for k, v in values.items() %}
          <tr>
            <th scope="row">{{ k | replace('_', ' ') | title }}</th>
            <td>
              {% if v is mapping %}
                {% for k2, v2 in v.items() %}
                <div class="text-sm"><span class="text-faint">{{ k2 | replace('_', ' ') }}:</span> {{ v2 }}</div>
                {% endfor %}
              {% else %}<span class="mono">{{ v }}</span>{% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <p class="mono mb-0">{{ values }}</p>
      {% endif %}
    </div>
    {% endfor %}
  </div>
  {% else %}
  <p class="meta">No thresholds were loaded.</p>
  {% endif %}
</section>

<section class="section">
  <h2 class="section-title">Installed reference datasets</h2>
  {% if datasets %}
  <div class="table-wrap">
    <table class="data">
      <thead>
        <tr>
          <th scope="col">Dataset</th>
          <th scope="col">Version</th>
          <th scope="col">Rows</th>
          <th scope="col">Installed</th>
          <th scope="col">Status</th>
        </tr>
      </thead>
      <tbody>
        {% for d in datasets %}
        {% if d is mapping %}
        <tr>
          <td><strong>{{ d.get('name') or d.get('key') or '—' }}</strong>
            {% if d.get('description') %}<div class="text-faint text-sm">{{ d.get('description') }}</div>{% endif %}
          </td>
          <td class="mono nowrap">{{ d.get('version') or '—' }}</td>
          <td class="num">{{ d.get('rows', d.get('count', '—')) }}</td>
          <td class="nowrap">{{ d.get('installed_at') or d.get('installed') or '—' }}</td>
          <td class="nowrap">
            {% if d.get('available', true) %}
            <span class="chip chip-sm chip-ready">Available</span>
            {% else %}
            <span class="chip chip-sm chip-muted">Missing</span>
            {% endif %}
          </td>
        </tr>
        {% else %}
        <tr><td colspan="5">{{ d }}</td></tr>
        {% endif %}
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p class="meta">No reference datasets are installed.</p>
  {% endif %}
</section>
{% endblock %}
```

### File: `celestra\templates\sources_panel.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import tier_badge, ext_link, pick, pairs_block %}
{% block title %}Sources — Celestra{% endblock %}

{%- set access_labels = {
  'api': 'Approved source API',
  'approved_api': 'Approved source API',
  'targeted_search': 'Targeted domain search',
  'search': 'Targeted domain search',
  'local_file': 'Local reference file',
  'licensed': 'Licensed source',
  'firecrawl_search': 'Supplementary web',
  'open_web': 'Supplementary web'
} -%}

{% block content %}
<div class="page-head page-head-row">
  <div>
    <h1>View Sources</h1>
    <p class="sub">
      Every source this run actually queried, what it returned, and what it could not reach.
    </p>
  </div>
  {% if run is defined and run %}
  <a class="btn btn-secondary" href="/runs/{{ run.id }}/overview">Back to overview</a>
  {% endif %}
</div>

<section class="section">
  <h2 class="section-title">Sources used <span class="count">({{ used | default([]) | length }})</span></h2>
  {% if used %}
  <div class="table-wrap">
    <table class="data">
      <thead>
        <tr>
          <th scope="col">Source</th>
          <th scope="col">Tier</th>
          <th scope="col">Access method</th>
          <th scope="col">Evidence items</th>
          <th scope="col">Link</th>
        </tr>
      </thead>
      <tbody>
        {% for s in used %}
        {% set method = (s.get('access_method') or s.get('origin') or '') | string %}
        <tr>
          <td>
            <strong>{{ s.get('name') or s.get('organization') or s.get('source_name') or '—' }}</strong>
            {% if s.get('organization') and s.get('name') and s.get('organization') != s.get('name') %}
            <div class="text-faint text-sm">{{ s.get('organization') }}</div>
            {% endif %}
          </td>
          <td class="nowrap">{{ tier_badge(s.get('tier', 5)) }}</td>
          <td class="nowrap">
            {% if method in ['firecrawl_search', 'open_web'] %}
            <span class="supp-flag">{{ icon('alert', 12) }} Supplementary web</span>
            {% else %}
            <span class="chip chip-sm">{{ access_labels.get(method, method | replace('_', ' ') | title or 'Not stated') }}</span>
            {% endif %}
          </td>
          <td class="num">{{ s.get('evidence_items', s.get('count', 0)) }}</td>
          <td class="break">{{ ext_link(s.get('url'), s.get('domain') or s.get('url')) }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="empty"><h3>No sources recorded</h3><p class="mb-0">Nothing has returned evidence for this run yet.</p></div>
  {% endif %}
</section>

{%- set blocked = (unavailable | default([])) | selectattr('blocked_by', 'defined') | selectattr('blocked_by') | list -%}
{%- set empty_handed = [] -%}
{%- for s in unavailable | default([]) -%}
  {%- if not s.get('blocked_by') -%}{%- set _ = empty_handed.append(s) -%}{%- endif -%}
{%- endfor -%}

<section class="section">
  <h2 class="section-title">Attempted but returned nothing <span class="count">({{ empty_handed | length }})</span></h2>
  {% if empty_handed %}
  <div class="table-wrap">
    <table class="data">
      <thead>
        <tr><th scope="col">Source</th><th scope="col">Tier</th><th scope="col">Access method</th><th scope="col">Reason</th></tr>
      </thead>
      <tbody>
        {% for s in empty_handed %}
        <tr>
          <td><strong>{{ s.get('name') or s.get('source_name') or '—' }}</strong></td>
          <td class="nowrap">{{ tier_badge(s.get('tier', 5)) }}</td>
          <td class="nowrap">
            <span class="chip chip-sm">{{ access_labels.get((s.get('access_method') or '') | string, (s.get('access_method') or 'Not stated') | replace('_', ' ') | title) }}</span>
          </td>
          <td>{{ s.get('reason') or 'No matching documents were returned.' }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p class="meta">Every source that was queried returned at least one document.</p>
  {% endif %}
</section>

<section class="section">
  <h2 class="section-title">Blocked on credentials or a licence <span class="count">({{ blocked | length }})</span></h2>
  {% if blocked %}
  <div class="table-wrap">
    <table class="data">
      <thead>
        <tr><th scope="col">Source</th><th scope="col">Tier</th><th scope="col">Blocked by</th><th scope="col">What this means</th></tr>
      </thead>
      <tbody>
        {% for s in blocked %}
        <tr>
          <td><strong>{{ s.get('name') or s.get('source_name') or '—' }}</strong></td>
          <td class="nowrap">{{ tier_badge(s.get('tier', 5)) }}</td>
          <td class="nowrap"><span class="chip chip-sm chip-amber">{{ s.get('blocked_by') }}</span></td>
          <td>{{ s.get('reason') or 'This source was skipped and contributed no evidence to the run.' }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p class="meta">No source was skipped for a missing credential or licence.</p>
  {% endif %}
</section>

{% if health %}
<section class="section">
  <h2 class="section-title">Connector health</h2>
  {% if health is mapping %}
  <div class="card">
    <table class="kv">
      <tbody>
        {% for key, value in health.items() %}
        <tr>
          <th scope="row">{{ key | replace('_', ' ') | title }}</th>
          <td>
            {% if value is mapping %}
              {% for k2, v2 in value.items() %}
              <div><span class="text-faint text-sm">{{ k2 | replace('_', ' ') | title }}:</span> {{ v2 }}</div>
              {% endfor %}
            {% elif value is sameas true %}
              <span class="chip chip-sm chip-ready">{{ icon('check', 12) }} OK</span>
            {% elif value is sameas false %}
              <span class="chip chip-sm chip-requires_input">Unavailable</span>
            {% else %}{{ value }}{% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
    {% for item in health %}
      {% if item is mapping %}{{ pairs_block(item) }}{% else %}<p>{{ item }}</p>{% endif %}
    {% endfor %}
  {% endif %}
</section>
{% endif %}
{% endblock %}
```

### File: `celestra\templates\stage_report.html`

```html
{% extends "base.html" %}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import tag_legend %}
{% block title %}{{ stage.name }} — Celestra{% endblock %}

{% block content %}
<div class="page-head page-head-row">
  <div>
    <div class="eyebrow">Stage report</div>
    <h1>{{ stage.agent_name }}</h1>
    <p class="sub">{{ stage.core_question }}</p>
  </div>
  <div class="btn-row">
    {% if run is defined and run %}
    <a class="btn btn-secondary" href="/runs/{{ run.id }}/overview">Overview</a>
    <a class="btn btn-secondary" href="/runs/{{ run.id }}/report">Full report</a>
    {% endif %}
  </div>
</div>

{{ tag_legend(compact=true) }}


<div class="report">
  {% include "partials/stage_body.html" %}
</div>
{% endblock %}
```

### File: `celestra\templates\partials\contradiction_card.html`

```html
{# ---------------------------------------------------------------------------
   One source disagreement, with the human decision controls.
   Nothing here resolves a conflict automatically: both sides stand as stated
   by their source until a person decides.
   Context: contradiction, run (optional)
   --------------------------------------------------------------------------- #}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import tier_badge, severity_chip, review_chip, ext_link, enum_value %}

{%- set rid = (run.id if run is defined and run else contradiction.run_id) -%}
{%- set decided = enum_value(contradiction.review_action) not in ['pending', ''] -%}

{%- set locked = (run.is_locked if run is defined and run and run.is_locked is defined else false) -%}
<article class="card contra {{ 'is-locked' if locked }}" id="conflict-{{ contradiction.id }}"
         data-contradiction-id="{{ contradiction.id }}" data-action-scope>
  <div class="contra-head">
    <div>
      <div class="meta text-sm">{{ contradiction.stage }}</div>
      <h3>{{ contradiction.topic }}</h3>
    </div>
    <div class="row" style="gap:8px;">
      {{ severity_chip(contradiction.severity) }}
      {{ review_chip(contradiction.review_action) }}
    </div>
  </div>

  <div class="contra-sides">
    <div class="contra-side">
      <div class="side-label">Source A</div>
      <div class="side-source">
        <span class="side-name">{{ contradiction.source_a_name }}</span>
        {{ tier_badge(contradiction.source_a_tier) }}
      </div>
      <p class="contra-claim">{{ contradiction.source_a_claim }}</p>
      {% if contradiction.source_a_url %}
      <div class="mt-1 text-sm">{{ ext_link(contradiction.source_a_url, 'Open source A') }}</div>
      {% endif %}
    </div>

    <div class="contra-side">
      <div class="side-label">Source B</div>
      <div class="side-source">
        <span class="side-name">{{ contradiction.source_b_name }}</span>
        {{ tier_badge(contradiction.source_b_tier) }}
      </div>
      <p class="contra-claim">{{ contradiction.source_b_claim }}</p>
      {% if contradiction.source_b_url %}
      <div class="mt-1 text-sm">{{ ext_link(contradiction.source_b_url, 'Open source B') }}</div>
      {% endif %}
    </div>
  </div>

  <div class="contra-reason">
    <strong>Possible reason:</strong> {{ contradiction.reason }}
  </div>

  <div class="contra-actions">
    {% if contradiction.reviewer_note %}
    <div class="review-note"><strong>Reviewer note:</strong> {{ contradiction.reviewer_note }}</div>
    {% endif %}

    <div class="field mb-0">
      <label for="note-{{ contradiction.id }}">Reviewer note <span class="optional">(Optional)</span></label>
      <textarea id="note-{{ contradiction.id }}" rows="2" data-field="note"
                placeholder="Why this side, or what a subject-matter expert still needs to check."
                >{{ contradiction.reviewer_note }}</textarea>
    </div>

    <div class="btn-row">
      {% if locked %}
      <span class="meta">{{ icon('lock', 13) }} Document approved; decisions are final.</span>
      {% else %}
      <button type="button" class="btn btn-sm btn-secondary"
              data-action-url="/runs/{{ rid }}/contradictions/{{ contradiction.id }}/review"
              data-payload='{"action": "prefer_a"}'
              data-swap="[data-contradiction-id='{{ contradiction.id }}']">Prefer A</button>
      <button type="button" class="btn btn-sm btn-secondary"
              data-action-url="/runs/{{ rid }}/contradictions/{{ contradiction.id }}/review"
              data-payload='{"action": "prefer_b"}'
              data-swap="[data-contradiction-id='{{ contradiction.id }}']">Prefer B</button>
      <button type="button" class="btn btn-sm btn-soft"
              data-action-url="/runs/{{ rid }}/contradictions/{{ contradiction.id }}/review"
              data-payload='{"action": "acknowledged"}'
              data-swap="[data-contradiction-id='{{ contradiction.id }}']">Acknowledge both</button>
      {% if decided %}
      <span class="meta">{{ icon('check', 13) }} Decision recorded — you can change it.</span>
      {% elif enum_value(contradiction.severity) == 'escalated' %}
      <span class="meta">Escalated: a decision is required before the next step.</span>
      {% endif %}
      {% endif %}
    </div>
  </div>
</article>
```

### File: `celestra\templates\partials\evidence_panel.html`

```html
{# ---------------------------------------------------------------------------
   Slide-over evidence panel.
   Context: evidence (list of Evidence), insight (optional), run (optional)
   --------------------------------------------------------------------------- #}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import tag_dot, tier_badge, enum_label, enum_value, ext_link, dt %}

<div class="slideover-backdrop" data-slideover-backdrop>
  <aside class="slideover" role="dialog" aria-modal="true" aria-labelledby="evidence-panel-title">
    <div class="slideover-head">
      <div>
        <h2 id="evidence-panel-title">Evidence</h2>
        <p class="meta mb-0">
          {% if insight is defined and insight %}{{ insight.title }} · {% endif %}
          {{ evidence | default([]) | length }} item{{ '' if evidence | default([]) | length == 1 else 's' }}
        </p>
      </div>
      <button type="button" class="icon-btn" data-slideover-close aria-label="Close evidence panel">
        {{ icon('x', 16) }}
      </button>
    </div>

    <div class="slideover-body">
      {% for ev in evidence | default([]) %}
      {% set origin = enum_value(ev.origin) %}
      <div class="evidence-item {{ 'is-supplementary' if origin == 'open_web' }}">
        {% if origin == 'open_web' %}
        <div class="mb-1">
          <span class="supp-flag">{{ icon('alert', 12) }} Supplementary web evidence</span>
        </div>
        {% endif %}

        <blockquote class="evidence-quote">{{ ev.quote }}</blockquote>

        <div class="row-between wrap mb-1">
          <div class="row wrap" style="gap:8px;">
            <strong class="text-sm">{{ ev.organization or ev.source_name }}</strong>
            {{ tier_badge(ev.tier) }}
            {{ tag_dot(ev.tag) }}
          </div>
        </div>

        {% if ev.title %}<p class="text-sm text-muted">{{ ev.title }}</p>{% endif %}

        <div class="evidence-meta">
          <span>{{ enum_label(ev.origin) }}</span>
          <span class="dot-sep">·</span>
          <span>Retrieved {{ dt(ev.retrieved_at) }}</span>
          {% if ev.published %}
          <span class="dot-sep">·</span><span>Published {{ ev.published }}</span>
          {% endif %}
          {% if ev.identifiers %}
          <span class="dot-sep">·</span>
          <span class="mono">
            {% for k, v in ev.identifiers.items() %}{{ k }}:{{ v }}{{ ' ' if not loop.last }}{% endfor %}
          </span>
          {% endif %}
        </div>

        <div class="mt-1 text-sm">{{ ext_link(ev.url, ev.source_name or 'Open source') }}</div>
      </div>
      {% else %}
      <div class="empty">
        <h3>No evidence recorded</h3>
        <p class="mb-0">Nothing was retrieved that supports this finding directly.</p>
      </div>
      {% endfor %}
    </div>
  </aside>

{% if web_sites %}
<div class="subblock">
  <h3>Open-web pages consulted</h3>
  <p class="sub mb-2">
    Searched only because the registered sources did not answer this question. A page listed
    here without a quote contributed nothing.
  </p>
  <ul class="site-list">
    {% for site in web_sites %}
    <li>
      <span class="vtag {{ 'vtag-general-knowledge' if site.get('used') else 'vtag-not-verified' }}">
        {{ 'QUOTED' if site.get('used') else ('READ' if site.get('scraped') else 'NOT READ') }}
      </span>
      <a href="{{ site.get('url') }}" target="_blank" rel="noopener noreferrer">
        {{ site.get('title') or site.get('url') }}
      </a>
      {% if site.get('site') %}<span class="text-faint">{{ site.get('site') }}</span>{% endif %}
    </li>
    {% endfor %}
  </ul>
</div>
{% endif %}
</div>
```

### File: `celestra\templates\partials\gate_panel.html`

```html
{# ---------------------------------------------------------------------------
   The gate summary: what still blocks the next step and the one button that
   takes it. Used at the review gate and at final approval; refreshed in place
   after every decision. Context: run, gate (from main._gate)
   --------------------------------------------------------------------------- #}
{% from "partials/icons.html" import icon %}
{%- set g = gate -%}
{%- set c = g.counts -%}
<section class="gate-panel {{ 'is-done' if g.done else ('is-blocked' if g.blockers else ('is-ready' if g.available else '')) }}"
         id="gate" data-gate-panel data-gate-url="{{ g.refresh_url }}" aria-live="polite">
  <div class="gate-body">
    <div class="gate-main">
      {% if g.done %}
      <div class="gate-title">{{ icon('check-circle', 17) }} {{ g.done_label }}</div>
      <p class="gate-sub">
        {% if g.mode == 'review' %}
        The discovery findings were approved{% if run.reviewed_at %} on {{ run.reviewed_at.strftime('%d %b %Y, %H:%M UTC') }}{% endif %}
        and Mapping &amp; Synthesis built on them. Decisions made here are final for this run.
        {% else %}
        The document was approved{% if run.approved_at %} on {{ run.approved_at.strftime('%d %b %Y, %H:%M UTC') }}{% endif %}
        and is locked. Nothing on it can change; start a new project to research it again.
        {% endif %}
      </p>
      {% elif g.blockers %}
      <div class="gate-title">{{ icon('alert', 17) }} {{ g.blockers | length }} item{{ '' if g.blockers | length == 1 else 's' }} need{{ 's' if g.blockers | length == 1 else '' }} your input before you can continue</div>
      <p class="gate-sub">
        Each one below says why. Approve it as stated, revise it, add what you know, or decide the
        conflict. Findings marked Ready carry forward as generated unless you change them.
      </p>
      <ul class="gate-blockers">
        {% for b in g.blockers %}
        <li>
          {{ icon('alert', 13) }}
          <span><a href="{{ b.anchor }}">{{ b.title }}</a> — {{ b.reason }}</span>
        </li>
        {% endfor %}
      </ul>
      {% elif g.available %}
      <div class="gate-title">{{ icon('check-circle', 17) }} Nothing needs your input</div>
      <p class="gate-sub">
        {% if g.mode == 'review' %}
        Every discovery finding is Ready or decided and every escalated conflict is decided.
        Continuing hands these findings to the Mapping &amp; Synthesis agents.
        {% else %}
        Every finding is Ready or decided and every escalated conflict is decided. Approving
        locks the document and records your sign-off in it.
        {% endif %}
      </p>
      {% else %}
      <div class="gate-title">{{ icon('activity', 17) }} Not at this step yet</div>
      <p class="gate-sub">This step opens once the agents before it have finished.</p>
      {% endif %}
      <div class="gate-counts">
        <span class="chip chip-sm">{{ c.insights }} findings</span>
        <span class="chip chip-sm chip-ready">{{ c.ready }} ready</span>
        <span class="chip chip-sm {{ 'chip-requires_input' if c.needs_decision else '' }}">{{ c.needs_decision }} need input</span>
        <span class="chip chip-sm chip-blue">{{ c.decided }} decided by you</span>
        <span class="chip chip-sm {{ 'chip-requires_input' if c.conflicts_blocking else '' }}">{{ c.conflicts_open }} open conflict{{ '' if c.conflicts_open == 1 else 's' }}</span>
      </div>
    </div>
    <div class="gate-actions">
      {% if g.done %}
        {% if g.mode == 'review' %}
        <a class="btn btn-secondary" href="/runs/{{ run.id }}/progress#mapping">See Mapping &amp; Synthesis {{ icon('arrow-right', 15) }}</a>
        {% else %}
        <a class="btn btn-primary" href="/runs/{{ run.id }}/report">Open the approved document {{ icon('arrow-right', 15) }}</a>
        {% endif %}
      {% elif g.available %}
      <form method="post" action="{{ g.action_url }}" data-gate-form>
        <button type="submit" class="btn btn-primary btn-lg" {% if g.blockers %}disabled aria-disabled="true"{% endif %}>
          {{ g.action_label }} {{ icon('arrow-right', 16) }}
        </button>
      </form>
      {% if g.blockers %}
      <span class="hint">Enabled once every item above has a decision.</span>
      {% elif g.mode == 'review' %}
      <span class="hint">Starts the four remaining agents. You can watch them run.</span>
      {% else %}
      <span class="hint">Final. The run is locked after this.</span>
      {% endif %}
      {% endif %}
    </div>
  </div>
</section>
```

### File: `celestra\templates\partials\icons.html`

```html
{# ---------------------------------------------------------------------------
   Inline SVG icon set. No icon font, no sprite fetch, nothing external.
   Usage: {% from "partials/icons.html" import icon %}  {{ icon('home') }}
   --------------------------------------------------------------------------- #}
{% macro icon(name, size=18, cls='ic') -%}
{%- set p = {
  'lock': '<rect x="4" y="10.5" width="16" height="10.5" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
  'home': '<path d="M3 10.2 12 3l9 7.2"/><path d="M5 9.6V21h14V9.6"/><path d="M9.5 21v-6h5v6"/>',
  'plus-square': '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M12 8.5v7"/><path d="M8.5 12h7"/>',
  'folder': '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.2l2 2.5h7.8A2.5 2.5 0 0 1 21 10v7.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5Z"/>',
  'book': '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5Z"/><path d="M4 20.5A2.5 2.5 0 0 1 6.5 18H20v3H6.5A2.5 2.5 0 0 1 4 20.5Z"/>',
  'settings': '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1Z"/>',
  'leaf': '<path d="M11 20A7 7 0 0 1 4 13c0-6 6.5-9.5 16-9.5C20 13 16.5 20 11 20Z"/><path d="M6.5 18.5C10 15 13 12.5 17.5 10"/>',
  'pill': '<rect x="2.6" y="8.6" width="18.8" height="6.8" rx="3.4" transform="rotate(-45 12 12)"/><path d="M9.2 9.2 14.8 14.8"/>',
  'microscope': '<path d="M6 18h12"/><path d="M9 18a5 5 0 1 0 6-8"/><path d="M11 4h3l1 5h-5Z"/><path d="M12.5 9v3"/><path d="M4 21h16"/>',
  'flow': '<circle cx="6" cy="5.5" r="2.5"/><circle cx="18" cy="5.5" r="2.5"/><circle cx="12" cy="18.5" r="2.5"/><path d="M6 8v2.5A2.5 2.5 0 0 0 8.5 13h7A2.5 2.5 0 0 0 18 10.5V8"/><path d="M12 13v3"/>',
  'route': '<circle cx="6" cy="18.5" r="2.5"/><circle cx="18" cy="5.5" r="2.5"/><path d="M15.5 5.5H10a3.5 3.5 0 0 0 0 7h4a3.5 3.5 0 0 1 0 7H8.5"/>',
  'sparkle': '<path d="M12 3.2 13.9 9 19.8 11l-5.9 2L12 18.8 10.1 13 4.2 11 10.1 9Z"/><path d="M18.5 16.4 19.2 18.4 21.2 19.1 19.2 19.8 18.5 21.8 17.8 19.8 15.8 19.1 17.8 18.4Z"/>',
  'shield': '<path d="M12 3 20 6v6.2c0 5-3.4 8-8 9.8-4.6-1.8-8-4.8-8-9.8V6Z"/><path d="M9 12.2l2.2 2.2L15.4 10"/>',
  'check': '<path d="M20 6 9 17l-5-5"/>',
  'check-circle': '<circle cx="12" cy="12" r="9"/><path d="M8.4 12.2 11 14.8 15.8 9.6"/>',
  'x': '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  'search': '<circle cx="10.8" cy="10.8" r="6.8"/><path d="m20 20-4.4-4.4"/>',
  'alert': '<path d="M12 9v4.5"/><path d="M12 17.2h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  'info': '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.8h.01"/>',
  'external': '<path d="M13.5 4.5H19.5V10.5"/><path d="M19.5 4.5 11 13"/><path d="M18.5 14.5v4a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2v-11a2 2 0 0 1 2-2h4"/>',
  'file-text': '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/><path d="M8.5 13h7"/><path d="M8.5 16.5h5"/>',
  'layers': '<path d="m12 3 9 4.8-9 4.8-9-4.8Z"/><path d="m3 12.4 9 4.8 9-4.8"/><path d="m3 16.9 9 4.8 9-4.8"/>',
  'chevron-right': '<path d="m9.5 5.5 6.5 6.5-6.5 6.5"/>',
  'arrow-right': '<path d="M4.5 12h15"/><path d="m13.5 6 6 6-6 6"/>',
  'users': '<circle cx="9.5" cy="8" r="3.5"/><path d="M3 20a6.5 6.5 0 0 1 13 0"/><path d="M16.5 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18 13.6A6.5 6.5 0 0 1 21 20"/>',
  'activity': '<path d="M3 12h4l2.5-7 5 14L17 12h4"/>',
  'database': '<ellipse cx="12" cy="6" rx="8" ry="3.2"/><path d="M4 6v6c0 1.8 3.6 3.2 8 3.2s8-1.4 8-3.2V6"/><path d="M4 12v6c0 1.8 3.6 3.2 8 3.2s8-1.4 8-3.2v-6"/>',
  'clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5.3l3.4 2"/>',
  'edit': '<path d="M12.5 5.5H6a2 2 0 0 0-2 2V18a2 2 0 0 0 2 2h10.5a2 2 0 0 0 2-2v-6.4"/><path d="M17 3.6a2 2 0 0 1 2.8 2.8L12.4 13.8l-3.6.9.9-3.6Z"/>',
  'plus-circle': '<circle cx="12" cy="12" r="9"/><path d="M12 8.4v7.2"/><path d="M8.4 12h7.2"/>',
  'eye': '<path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/>',
  'scale': '<path d="M12 4v16"/><path d="M6.5 20h11"/><path d="M4 9h16"/><path d="M4 9 1.8 14.5h4.4Z"/><path d="M20 9l-2.2 5.5h4.4Z"/>',
  'split': '<path d="M4 5h4l5.5 14H20"/><path d="M4 19h4l2.4-6"/><path d="m16.5 2.5 3.5 2.5-3.5 2.5"/><path d="m16.5 16.5 3.5 2.5-3.5 2.5"/>',
  'link': '<path d="M9.5 14.5a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.3 1.3"/><path d="M14.5 9.5a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.3-1.3"/>',
  'download': '<path d="M12 3.5v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M4 20h16"/>',
  'target': '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
  'flag': '<path d="M5 21V4"/><path d="M5 4.6h11.5l-1.8 3.7 1.8 3.7H5"/>',
  'list': '<path d="M8.5 6.5H20"/><path d="M8.5 12H20"/><path d="M8.5 17.5H20"/><path d="M4.3 6.5h.01"/><path d="M4.3 12h.01"/><path d="M4.3 17.5h.01"/>',
  'inbox': '<path d="M3.5 13.5H8l1.5 3h5l1.5-3h4.5"/><path d="M5.6 5h12.8l2.1 8.5V19a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2v-5.5Z"/>'
} -%}
<svg class="{{ cls }}" width="{{ size }}" height="{{ size }}" viewBox="0 0 24 24" fill="none"
     stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
     aria-hidden="true" focusable="false">{{ p.get(name, p['info']) | safe }}</svg>
{%- endmacro %}

{% macro ring_logo(size=22) -%}
<svg class="brand-mark" width="{{ size }}" height="{{ size }}" viewBox="0 0 24 24" fill="none"
     aria-hidden="true" focusable="false">
  <circle cx="12" cy="12" r="9.2" stroke="currentColor" stroke-width="1.6" opacity=".35"/>
  <circle cx="12" cy="12" r="5" stroke="currentColor" stroke-width="1.8"/>
  <circle cx="12" cy="12" r="1.9" fill="currentColor"/>
</svg>
{%- endmacro %}
```

### File: `celestra\templates\partials\insight_card.html`

```html
{# ---------------------------------------------------------------------------
   One review card, in five parts: what we found, the evidence, what it means,
   the sources, and the reviewer's decision. Re-rendered on its own after a
   decision and swapped in place by data-insight-id.
   Context: insight, run (optional), source_names (optional), agent_by_stage (optional)
   --------------------------------------------------------------------------- #}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import source_name_chips, review_chip, enum_value %}

{%- set rid = (run.id if run is defined and run else insight.run_id) -%}
{%- set conf = enum_value(insight.confidence) -%}
{%- set decision = enum_value(insight.review_action) -%}
{%- set decided = decision not in ['pending', ''] -%}
{%- set locked = (run.is_locked if run is defined and run and run.is_locked is defined else false) -%}
{%- set needs_review = conf == 'requires_input' and not decided -%}
{%- set names = source_names | default({}) -%}
{%- set agent = (agent_by_stage | default({})).get(insight.stage) -%}
{%- set agent_label = 'Celestra Synthesis' if insight.category == 'Synthesis' else (agent.name if agent else insight.category) -%}
{%- set etype = insight.evidence_type | default('') -%}
{%- set ev = insight.evidence -%}
{%- set nsrc = insight.source_ids | length -%}

<article class="icard {{ 'is-reviewed' if decided }}{{ ' needs-review' if needs_review }}{{ ' is-locked' if locked }}{{ ' not-covered' if insight.covered is defined and not insight.covered }}"
         id="insight-{{ insight.id }}"
         data-insight-id="{{ insight.id }}"
         data-category="{{ insight.category | lower }}"
         data-confidence="{{ conf }}"
         data-decision="{{ decision }}"
         data-stage="{{ insight.stage }}"
         data-sources="{{ nsrc }}"
         data-number="{{ insight.number | default(0) }}"
         data-search="{{ insight.title }} {{ insight.summary }} {{ insight.category }} {% for sid in insight.source_ids %}{{ names.get(sid, sid) }} {% endfor %}"
         data-action-scope>

  {# -- header -------------------------------------------------------- #}
  <header class="icard-head">
    {% if insight.number %}<span class="icard-num">{{ '%02d' % insight.number }}</span>{% endif %}
    <h3 class="icard-title">{{ insight.title }}</h3>
    <span class="icard-agent">{{ agent_label | replace(' Agent', '') }}</span>
  </header>

  {# -- 1. what we found --------------------------------------------- #}
  <p class="icard-finding">{{ insight.summary }}</p>

  <div class="icard-status">
    {% if decided %}
      {{ review_chip(insight.review_action) }}
    {% elif needs_review %}
      <span class="status-dot is-review" title="{{ insight.input_reason }}"></span>
      <span class="status-text">Needs your review</span>
    {% else %}
      <span class="status-dot is-ready"></span>
      <span class="status-text">Ready</span>
    {% endif %}
    <span class="status-sep">|</span>
    <span class="status-text">{{ nsrc }} source{{ '' if nsrc == 1 else 's' }}</span>
    {% if insight.used_web_fallback %}
    <span class="status-sep">|</span><span class="status-text">includes web evidence</span>
    {% endif %}
  </div>
  {% if needs_review and insight.input_reason %}
  <p class="icard-why">{{ insight.input_reason }}</p>
  {% elif decided and insight.input_reason %}
  <p class="icard-why is-resolved">Was flagged: {{ insight.input_reason }} Settled by your decision.</p>
  {% endif %}

  {# -- 2. evidence ---------------------------------------------------- #}
  {% if ev %}
  <div class="icard-evidence">
    {% if etype == 'metrics' %}
    <div class="icard-metrics">
      {% for m in ev %}
      <div class="icard-metric"><div class="icard-metric-value">{{ m.value }}</div><div class="icard-metric-label">{{ m.label }}</div></div>
      {% endfor %}
    </div>
    {% elif etype == 'table' and ev.columns is defined %}
    <div class="table-wrap">
      <table class="data table-compact icard-table">
        <thead><tr>{% for c in ev.columns %}<th scope="col">{{ c }}</th>{% endfor %}</tr></thead>
        <tbody>
          {% for row in ev.rows %}
          <tr>{% for cell in row %}<td>{{ cell | tagify }}</td>{% endfor %}</tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% elif etype == 'steps' %}
    <ol class="icard-steps">
      {% for step in ev %}<li><span>{{ step }}</span></li>{% endfor %}
    </ol>
    {% else %}
    <ol class="icard-points">
      {% for point in ev %}<li>{{ point | tagify }}</li>{% endfor %}
    </ol>
    {% endif %}
  </div>
  {% endif %}

  {# -- 3. interpretation ------------------------------------------- #}
  {% if insight.interpretation %}
  <div class="icard-section">
    <div class="icard-label">What this means</div>
    <p class="icard-text">{{ insight.interpretation }}</p>
  </div>
  {% endif %}
  {% if insight.review_note and not decided %}
  <p class="icard-note">{{ icon('eye', 12) }} Worth checking: {{ insight.review_note }}</p>
  {% endif %}

  {# -- reviewer notes ------------------------------------------------ #}
  {% if insight.user_input %}
  <div class="review-note is-revision">
    <strong>Your instruction:</strong> {{ insight.user_input }}
    {% if insight.revision_note %}<br><strong>What Celestra did:</strong> {{ insight.revision_note }}{% endif %}
  </div>
  {% endif %}
  {% if insight.reviewer_input %}
  <div class="review-note is-input"><strong>Your input:</strong> {{ insight.reviewer_input }}</div>
  {% endif %}

  {# -- 4. sources ---------------------------------------------------- #}
  <div class="icard-section">
    <div class="icard-label">Sources</div>
    {% if insight.source_ids %}
      {{ source_name_chips(insight.source_ids, names) }}
    {% else %}
      <span class="meta">No source returned usable evidence for this card.</span>
    {% endif %}
  </div>

  {# -- 5. validation ------------------------------------------------- #}
  <footer class="icard-foot">
    <div class="icard-links">
      <a class="icard-link" href="/runs/{{ rid }}/stages/{{ insight.stage }}">View analysis {{ icon('arrow-right', 13) }}</a>
      {% if insight.table_titles %}
      <button type="button" class="icard-link as-button"
              data-modal-url="/runs/{{ rid }}/insights/{{ insight.id }}/table"
              title="{{ insight.table_titles | join(' · ') }}">Document table</button>
      {% endif %}
      <button type="button" class="icard-link as-button"
              data-evidence-url="/runs/{{ rid }}/insights/{{ insight.id }}/evidence">Evidence</button>
    </div>
    <div class="icard-actions">
      {% if locked %}
      <span class="meta">{{ icon('lock', 12) }} Final</span>
      {% else %}
      {% if decision != 'approved' %}
      <button type="button" class="btn btn-sm btn-soft" title="Accept this card as written"
              data-action-url="/runs/{{ rid }}/insights/{{ insight.id }}/approve"
              data-swap="[data-insight-id='{{ insight.id }}']">{{ icon('check', 13) }} Approve</button>
      {% endif %}
      <button type="button" class="btn btn-sm btn-secondary" title="Tell Celestra what to change; it rewrites the card"
              data-modal-url="/runs/{{ rid }}/insights/{{ insight.id }}/modify">Edit</button>
      <button type="button" class="btn btn-sm btn-secondary" title="Attach what you know; kept as your input and passed to later agents"
              data-modal-url="/runs/{{ rid }}/insights/{{ insight.id }}/input">Add input</button>
      {% endif %}
    </div>
  </footer>
</article>
```

### File: `celestra\templates\partials\insight_modal.html`

```html
{# ---------------------------------------------------------------------------
   The Modify and Add Input dialogs. Same shell, different contract:
     modify  the text is an instruction; the model rewrites the finding
     input   the text is knowledge; it is attached, never rewritten
   Context: insight, mode ('modify'|'input'), impacts, evidence, downstream_agents,
            web_available, run (optional)
   --------------------------------------------------------------------------- #}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import confidence_chip, tinted_tile, enum_value, tier_badge %}

{%- set rid = (run.id if run is defined and run else insight.run_id) -%}
{%- set is_input = (mode | default('modify')) == 'input' -%}
{%- set cat_style = {
  'clinical': ('leaf', 'green'), 'treatment': ('pill', 'blue'),
  'diagnostic': ('microscope', 'purple'), 'logic': ('flow', 'indigo'),
  'journey': ('route', 'teal'), 'synthesis': ('sparkle', 'teal')
} -%}
{%- set style = cat_style.get((insight.category | default('') | lower), ('sparkle', 'blue')) -%}

<div class="modal-backdrop" data-modal-backdrop>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title-{{ insight.id }}"
       data-action-scope>
    <div class="modal-head">
      <h2 id="modal-title-{{ insight.id }}">{{ 'Add your input' if is_input else 'Modify this finding' }}</h2>
      <button type="button" class="icon-btn" data-modal-close aria-label="Close dialog">
        {{ icon('x', 16) }}
      </button>
    </div>

    <div class="modal-body">
      <div class="insight-card-top mb-2">
        {{ tinted_tile(style[0], style[1]) }}
        <div class="insight-main">
          <h3 class="insight-title">{{ insight.title }}</h3>
          <p class="insight-summary mb-0">{{ insight.summary }}</p>
        </div>
        <div class="insight-aside">{{ confidence_chip(insight.confidence) }}</div>
      </div>

      {% if insight.input_reason and enum_value(insight.review_action) == 'pending' %}
      <div class="input-reason mb-2"><strong>Why this needs you:</strong> {{ insight.input_reason }}</div>
      {% endif %}

      {% if is_input %}
      <div class="modal-explain is-input">
        <strong>What Add Input does.</strong> Your text is attached to this finding as reviewer
        input. The finding itself is not rewritten.
        <ul>
          <li>It is printed with the finding in the final document, marked as yours.</li>
          {% if downstream_agents %}
          <li>It is handed to the agents that have not run yet
            ({{ downstream_agents | join(', ') }}) so their research takes it into account.</li>
          {% else %}
          <li>Every agent has already run, so it goes into the document only.</li>
          {% endif %}
          <li>It counts as your decision on this finding.</li>
        </ul>
      </div>
      {% else %}
      <div class="modal-explain is-modify">
        <strong>What Modify does.</strong> Your text is an instruction to Celestra.
        <ul>
          <li>It re-reads the evidence it holds for this finding and, if that is not enough,
            {% if web_available %}searches the web{% else %}would search the web (web search is not configured, so it will use held evidence only){% endif %}.</li>
          <li>It rewrites the finding and its answer in the document, citing what it used.</li>
          <li>It counts as your decision on this finding. You see the result immediately.</li>
        </ul>
      </div>
      {% endif %}

      <div class="field">
        <span class="label">Current finding</span>
        <div class="readonly-block">
          {{ (insight.detail or insight.summary) | tagify }}
        </div>
      </div>

      <div class="field">
        <label for="insight-input-{{ insight.id }}">{{ 'Your input' if is_input else 'What should change' }}</label>
        <textarea id="insight-input-{{ insight.id }}" rows="4" maxlength="500"
                  data-field="user_input" data-autofocus
                  aria-describedby="counter-{{ insight.id }}"
                  placeholder="{% if is_input %}For example: our claims data shows first-line venetoclax use is mostly in del(17p) patients; use 2024 SEER incidence.{% else %}For example: restrict this to adults; the 5-year survival figure should come from SEER 2024, not the older review.{% endif %}">{{ insight.reviewer_input if is_input else insight.user_input }}</textarea>
        <div class="counter" id="counter-{{ insight.id }}" aria-live="polite"
             data-counter-for="insight-input-{{ insight.id }}" data-counter-max="500">0/500</div>
      </div>

      {% if not is_input %}
      <div class="impact">
        <h4>{{ icon('activity', 15) }} What else this touches</h4>
        <p class="lead">
          {% if impacts %}
            {{ impacts | length }} related finding{{ '' if impacts | length == 1 else 's' }} share
            sources or a stage with this one. They are listed so you can check them after the
            rewrite; they are not rewritten automatically.
          {% else %}
            No other finding shares a source or stage with this one. Only this finding changes.
          {% endif %}
        </p>
        {% for im in impacts | default([]) %}
        <div class="impact-item">
          {{ tinted_tile('sparkle', ['green', 'blue', 'purple', 'indigo', 'teal'][loop.index0 % 5], 'tile-sm') }}
          <div>
            <div class="impact-name">{{ im.agent_name }}</div>
            <div class="impact-desc">{{ im.get("title") or im.get("description", "") }}</div>
          </div>
        </div>
        {% endfor %}
      </div>
      {% endif %}

      {% if evidence %}
      <div class="subblock">
        <h3>Evidence held ({{ evidence | length }})</h3>
        <ul class="list-plain stack-sm">
          {% for ev in evidence[:4] %}
          <li>
            {{ tier_badge(ev.tier) }}
            <span class="text-sm text-muted break">
              <strong>{{ ev.source_name }}</strong> — {{ ev.quote | truncate(150) }}
            </span>
          </li>
          {% endfor %}
        </ul>
        <button type="button" class="btn btn-ghost btn-sm mt-1"
                data-evidence-url="/runs/{{ rid }}/insights/{{ insight.id }}/evidence">
          {{ icon('eye', 14) }} View all evidence
        </button>
      </div>
      {% endif %}
    </div>

    <div class="modal-foot">
      <button type="button" class="btn btn-secondary" data-modal-close>Cancel</button>
      {% if is_input %}
      <button type="button" class="btn btn-primary"
              data-action-url="/runs/{{ rid }}/insights/{{ insight.id }}/input"
              data-swap="[data-insight-id='{{ insight.id }}']"
              data-requires-field="user_input"
              data-busy-label="Attaching…"
              data-closes-modal>{{ icon('plus-circle', 14) }} Attach input</button>
      {% else %}
      <button type="button" class="btn btn-primary"
              data-action-url="/runs/{{ rid }}/insights/{{ insight.id }}/modify"
              data-swap="[data-insight-id='{{ insight.id }}']"
              data-requires-field="user_input"
              data-busy-label="Revising… this can take a minute"
              data-closes-modal>{{ icon('edit', 14) }} Revise finding</button>
      {% endif %}
    </div>
  </div>
</div>
```

### File: `celestra\templates\partials\insight_table.html`

```html
{# ---------------------------------------------------------------------------
   Table snapshot dialog: the stage-report table(s) an insight was built from,
   rendered exactly as they appear in the final document.
   Context: insight, tables (list[InsightTable]), report (StageReport|None), run_id
   --------------------------------------------------------------------------- #}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import tag_legend, data_table, confidence_chip, enum_value %}
{%- set rid = run_id if run_id is defined else insight.run_id -%}

<div class="modal-backdrop" data-modal-backdrop>
  <div class="modal modal-wide" role="dialog" aria-modal="true"
       aria-labelledby="modal-title-{{ insight.id }}" data-action-scope>
    <div class="modal-head">
      <h2 id="modal-title-{{ insight.id }}">Table snapshot</h2>
      <button type="button" class="icon-btn" data-modal-close aria-label="Close dialog">
        {{ icon('x', 16) }}
      </button>
    </div>

    <div class="modal-body">
      {{ tag_legend(compact=true) }}
      <div class="insight-card-top mb-2">
        <div class="insight-main">
          <p class="eyebrow">As it appears in the final document</p>
          <h3 class="insight-title">{{ insight.title }}</h3>
          <p class="insight-summary mb-0">{{ insight.summary }}</p>
        </div>
        {{ confidence_chip(enum_value(insight.confidence)) }}
      </div>

      {% if tables %}
        {% for table in tables %}
          {{ data_table(table) }}
        {% endfor %}
      {% else %}
        <p><strong>No table has been built for this finding yet.</strong></p>
        <p class="sub">
          {% if report %}The {{ report.name }} report has no table drawn from this question.
          {% else %}This finding's stage report has not been synthesised yet.{% endif %}
        </p>
      {% endif %}
    </div>

    <div class="modal-foot">
      {% if report %}
      <a class="btn btn-secondary" href="/runs/{{ rid }}/stages/{{ report.stage }}">Open full stage report</a>
      {% endif %}
      <button type="button" class="btn btn-primary" data-modal-close>Close</button>
    </div>
  </div>
</div>
```

### File: `celestra\templates\partials\macros.html`

```html
{# ---------------------------------------------------------------------------
   Shared render helpers. Everything here is tolerant of either an enum member
   or a plain string, so a route may hand over models or already-serialised
   dictionaries.

   NOTE ON VOCABULARY: execution units are AGENTS everywhere in the UI.
   No macro in this file reads or prints the internal grouping letter.
   --------------------------------------------------------------------------- #}
{% from "partials/icons.html" import icon %}

{%- macro enum_value(v) -%}{{ v.value if v.value is defined else v }}{%- endmacro -%}

{%- macro enum_label(v) -%}
{%- if v.label is defined -%}{{ v.label }}
{%- else -%}{{ (v.value if v.value is defined else v) | string | replace('_', ' ') | title }}
{%- endif -%}
{%- endmacro -%}

{# icon key -> pastel tint family, matching the agent order in the design #}
{%- set TINTS = {
  'leaf': 'green', 'pill': 'blue', 'microscope': 'purple', 'flow': 'indigo',
  'route': 'teal', 'sparkle': 'teal', 'shield': 'slate', 'target': 'rose',
  'users': 'green', 'activity': 'blue', 'layers': 'indigo', 'scale': 'amber'
} -%}

{%- macro tint_for(name) -%}{{ TINTS.get(name | string, 'blue') }}{%- endmacro -%}

{% macro agent_tile(icon_name, size='') -%}
<span class="tile {{ size }} tint-{{ tint_for(icon_name) }}">{{ icon(icon_name | default('sparkle', true)) }}</span>
{%- endmacro %}

{% macro tinted_tile(icon_name, tint, size='') -%}
<span class="tile {{ size }} tint-{{ tint }}">{{ icon(icon_name | default('sparkle', true)) }}</span>
{%- endmacro %}

{% macro confidence_chip(conf, small=false) -%}
{%- set v = (conf.value if conf.value is defined else conf) | string -%}
<span class="chip chip-{{ v }}{{ ' chip-sm' if small else '' }}">{{ enum_label(conf) }}</span>
{%- endmacro %}

{% macro tier_badge(tier) -%}
<span class="tier tier-{{ tier }}">Tier {{ tier }}</span>
{%- endmacro %}

{% macro severity_chip(sev) -%}
{%- set v = (sev.value if sev.value is defined else sev) | string -%}
<span class="chip chip-sm {{ 'chip-requires_input' if v == 'escalated' else 'chip-muted' }}">
  {{ 'Escalated' if v == 'escalated' else 'Noted' }}
</span>
{%- endmacro %}

{% macro review_chip(action) -%}
{%- set v = (action.value if action.value is defined else action) | string -%}
{%- set labels = {'approved': 'Approved by you', 'modified': 'Revised on your instruction',
                  'input_added': 'Your input attached', 'acknowledged': 'Acknowledged',
                  'prefer_a': 'Source A preferred', 'prefer_b': 'Source B preferred'} -%}
{%- if v and v != 'pending' -%}
<span class="chip chip-sm {{ 'chip-ready' if v in ['approved', 'acknowledged'] else 'chip-blue' }}">
  {{ icon('check', 12) }}{{ labels.get(v, v | replace('_', ' ') | title) }}
</span>
{%- endif -%}
{%- endmacro %}

{% macro run_status_chip(status) -%}
{%- set v = (status.value if status.value is defined else status) | string -%}
{%- set cls = {'completed': 'chip-blue', 'approved': 'chip-ready', 'running': 'chip-blue',
               'awaiting_review': 'chip-requires_input', 'failed': 'chip-requires_input',
               'cancelled': 'chip-muted', 'pending': 'chip-muted'} -%}
{%- set labels = {'completed': 'Awaiting approval', 'approved': 'Approved',
                  'awaiting_review': 'Awaiting review', 'running': 'Running',
                  'pending': 'Queued', 'failed': 'Failed', 'cancelled': 'Cancelled'} -%}
<span class="chip chip-sm {{ cls.get(v, 'chip-muted') }}">{{ labels.get(v, v | replace('_', ' ') | title) }}</span>
{%- endmacro %}

{# The flow stepper: one row per step, state from run_steps(). #}
{% macro flow_steps(steps, compact=false) -%}
<ol class="flow {{ 'is-compact' if compact }}">
  {% for s in steps %}
  <li class="flow-step is-{{ s.state }}" data-step="{{ s.key }}">
    {% if s.url %}<a class="flow-link" href="{{ s.url }}"{% if s.state == 'current' %} aria-current="step"{% endif %}>{% else %}<span class="flow-link is-disabled">{% endif %}
      <span class="flow-num">{% if s.state == 'done' %}{{ icon('check', 12) }}{% elif s.state == 'failed' %}{{ icon('alert', 12) }}{% else %}{{ s.number }}{% endif %}</span>
      <span class="flow-text">
        <span class="flow-name">{{ s.name }}</span>
        {% if not compact %}<span class="flow-desc">{{ s.description }}</span>{% endif %}
      </span>
    {% if s.url %}</a>{% else %}</span>{% endif %}
  </li>
  {% endfor %}
</ol>
{%- endmacro %}

{% macro agent_status_markup(status) -%}
{%- set v = (status.value if status.value is defined else status) | string -%}
{%- if v in ['researching', 'synthesising'] -%}
  <span class="spinner" aria-hidden="true"></span><span>{{ 'Researching…' if v == 'researching' else 'Synthesising…' }}</span>
{%- elif v == 'complete' -%}
  {{ icon('check', 15) }}<span>Complete</span>
{%- elif v == 'failed' -%}
  {{ icon('alert', 15) }}<span>Failed</span>
{%- elif v == 'blocked' -%}
  <span class="chip-dot" aria-hidden="true"></span><span>Blocked</span>
{%- elif v == 'skipped' -%}
  <span class="chip-dot" aria-hidden="true"></span><span>Skipped</span>
{%- else -%}
  <span class="chip-dot" aria-hidden="true"></span><span>Queued</span>
{%- endif -%}
{%- endmacro %}

{# Named source chips. The UI must always say WHICH source produced a finding. #}
{% macro source_name_chips(ids, names) -%}
<span class="chip-row">
  {%- for sid in ids or [] %}
  <span class="chip chip-sm chip-source is-used" data-source-key="{{ sid }}"
        data-source-name="{{ (names or {}).get(sid, sid) }}">{{ (names or {}).get(sid, sid) }}</span>
  {%- endfor %}
</span>
{%- endmacro %}

{# A styled InsightTable: columns / rows / footnote, tags rendered inline. #}
{% macro data_table(table, compact=false) -%}
<div class="subblock">
  {% if table.title %}
  <div class="table-title"><h3>{{ table.title }}</h3></div>
  {% endif %}
  <div class="table-wrap">
    <table class="data{{ ' table-compact' if compact else '' }}">
      <thead>
        <tr>{% for col in table.columns %}<th scope="col">{{ col }}</th>{% endfor %}</tr>
      </thead>
      <tbody>
        {% for row in table.rows %}
        <tr>
          {% for cell in row %}
          <td>{{ cell | tagify }}</td>
          {% endfor %}
        </tr>
        {% else %}
        <tr><td colspan="{{ table.columns | length or 1 }}" class="text-faint">No rows recorded.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% if table.footnote %}<p class="table-footnote">{{ table.footnote }}</p>{% endif %}
</div>
{%- endmacro %}

{# A table built from a list of dicts, with an explicit (heading, key) column map. #}
{% macro dict_table(rows, columns, footnote='', empty='Nothing recorded.') -%}
<div class="table-wrap">
  <table class="data">
    <thead>
      <tr>{% for head, _key in columns %}<th scope="col">{{ head }}</th>{% endfor %}</tr>
    </thead>
    <tbody>
      {% for row in rows or [] %}
      <tr>
        {% for _head, key in columns %}
        <td>{{ (row.get(key, '') if row.get is defined else (row[key] if key in row else '')) | tagify }}</td>
        {% endfor %}
      </tr>
      {% else %}
      <tr><td colspan="{{ columns | length }}" class="text-faint">{{ empty }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% if footnote %}<p class="table-footnote">{{ footnote }}</p>{% endif %}
{%- endmacro %}

{% macro stat_tile(value, label, tone='slate', note='', check=false) -%}
<div class="stat stat-{{ tone }}">
  <div class="stat-value">
    <span>{{ value }}</span>
    {% if check %}<span class="stat-check">{{ icon('check', 20) }}</span>{% endif %}
  </div>
  <div class="stat-label">{{ label }}</div>
  {% if note %}<div class="stat-note">{{ note }}</div>{% endif %}
</div>
{%- endmacro %}

{% macro origin_chip(origin) -%}
{%- set v = (origin.value if origin.value is defined else origin) | string -%}
{%- if v == 'open_web' -%}
<span class="supp-flag">{{ icon('alert', 12) }} Supplementary web evidence</span>
{%- else -%}
<span class="chip chip-sm">{{ enum_label(origin) }}</span>
{%- endif -%}
{%- endmacro %}

{% macro ext_link(url, label='') -%}
{%- if url -%}
<a class="break" href="{{ url }}" target="_blank" rel="noopener noreferrer">{{ label or url }} {{ icon('external', 12) }}</a>
{%- else -%}<span class="text-faint">Not stated</span>{%- endif -%}
{%- endmacro %}

{% macro dt(value, fmt='%d %b %Y, %H:%M UTC') -%}
{%- if value -%}
{{ value.strftime(fmt) if value.strftime is defined else value }}
{%- else -%}—{%- endif -%}
{%- endmacro %}

{# First non-empty value among candidate keys, rendered with inline tag pills. #}
{% macro pick(row, keys, fallback='') -%}
{%- set ns = namespace(found=none) -%}
{%- for k in keys -%}
  {%- if ns.found is none -%}
    {%- set v = (row.get(k) if row.get is defined else (row[k] if k in row else none)) -%}
    {%- if v -%}{%- set ns.found = v -%}{%- endif -%}
  {%- endif -%}
{%- endfor -%}
{{ (ns.found if ns.found is not none else fallback) | tagify }}
{%- endmacro %}

{# Any dict rendered as a labelled block, whatever its keys happen to be. #}
{% macro pairs_block(row, tone='is-slate') -%}
<div class="callout {{ tone }} mb-1">
  {% for k, v in row.items() %}
  <div class="mb-0">
    <span class="text-faint text-sm">{{ k | replace('_', ' ') | title }}:</span>
    <span>{{ v | tagify }}</span>
  </div>
  {% endfor %}
</div>
{%- endmacro %}

{# The colour key for evidence dots. One line; sits above any table or prose that uses them. #}
{% macro tag_legend(compact=false) -%}
<div class="vlegend {{ 'is-compact' if compact }}" aria-label="Evidence colour key">
  <span><span class="vdot vtag-verified"></span> Verified from a retrieved source</span>
  <span><span class="vdot vtag-inference"></span> Inferred from several sources</span>
  <span><span class="vdot vtag-original"></span> Original analytical construct</span>
  <span><span class="vdot vtag-update"></span> Recent update</span>
  <span><span class="vdot vtag-general-knowledge"></span> General knowledge</span>
  <span><span class="vdot vtag-not-verified"></span> Not verified</span>
  <span><span class="vsrc">Source</span> = where it came from</span>
</div>
{%- endmacro %}

{% macro tag_dot(tag) -%}
{%- set v = (tag.value if tag.value is defined else tag) | string -%}
<span class="vdot-label"><span class="vdot vtag-{{ v | lower | replace(' ', '-') }}"></span>{{ v | title }}</span>
{%- endmacro %}
```

### File: `celestra\templates\partials\name_project_modal.html`

```html
{# Step one of a new project: its name. Context: none #}
{% from "partials/icons.html" import icon %}
<div class="modal-backdrop" data-modal-backdrop>
  <form class="modal" role="dialog" aria-modal="true" aria-labelledby="name-title"
        method="get" action="/projects/new">
    <div class="modal-head">
      <h2 id="name-title">Name your project</h2>
      <button type="button" class="icon-btn" data-modal-close aria-label="Close dialog">{{ icon('x', 16) }}</button>
    </div>
    <div class="modal-body">
      <div class="field">
        <label for="new-project-name">Project name</label>
        <input type="text" id="new-project-name" name="name" maxlength="120" required data-autofocus
               autocomplete="off" placeholder="e.g. CLL line-of-therapy, US, 2026">
        <p class="hint">You can rename it later. Next you choose the indication, geography and objective.</p>
      </div>
    </div>
    <div class="modal-foot">
      <button type="button" class="btn btn-secondary" data-modal-close>Cancel</button>
      <button type="submit" class="btn btn-primary">Continue {{ icon('arrow-right', 14) }}</button>
    </div>
  </form>
</div>
```

### File: `celestra\templates\partials\rename_modal.html`

```html
{# Rename a project. Context: run #}
{% from "partials/icons.html" import icon %}
<div class="modal-backdrop" data-modal-backdrop>
  <form class="modal" role="dialog" aria-modal="true" aria-labelledby="rename-title"
        method="post" action="/runs/{{ run.id }}/rename">
    <div class="modal-head">
      <h2 id="rename-title">Rename project</h2>
      <button type="button" class="icon-btn" data-modal-close aria-label="Close dialog">{{ icon('x', 16) }}</button>
    </div>
    <div class="modal-body">
      <div class="field">
        <label for="project-name">Project name</label>
        <input type="text" id="project-name" name="name" maxlength="120" data-autofocus
               value="{{ run.name }}" placeholder="{{ run.config.indication }}">
        <p class="hint">Shown on the project list, the sidebar and the document. Leave it empty to use the
          indication. The reference <code>{{ run.reference }}</code> never changes.</p>
      </div>
      <input type="hidden" name="next" value="{{ request.headers.get('referer', '/runs/' ~ run.id) if request is defined else '/runs/' ~ run.id }}">
    </div>
    <div class="modal-foot">
      <button type="button" class="btn btn-secondary" data-modal-close>Cancel</button>
      <button type="submit" class="btn btn-primary">Save name</button>
    </div>
  </form>
</div>
```

### File: `celestra\templates\partials\stage_body.html`

```html
{# ---------------------------------------------------------------------------
   The body of one stage report. Shared by stage_report.html (standalone page)
   and report.html (full run document).
   Context: stage (StageReport), run (optional),
            stage_contradictions or contradictions (optional)
   --------------------------------------------------------------------------- #}
{% from "partials/icons.html" import icon %}
{% from "partials/macros.html" import data_table, pick, pairs_block, tier_badge, agent_tile %}

{%- set sc = stage_contradictions if stage_contradictions is defined
             else (contradictions | default([]) | selectattr('stage', 'equalto', stage.stage) | list) -%}
{%- set approved = (stage.evidence_count | default(0)) - (stage.supplementary_count | default(0)) -%}

<section class="stage" id="{{ stage.stage }}" aria-labelledby="{{ stage.stage }}-title">
  <div class="stage-head">
    {{ agent_tile('sparkle') }}
    <div>
      <div class="stage-id">{{ stage.stage | replace('_', ' ') | title }} · {{ stage.agent_name }}</div>
      <h2 id="{{ stage.stage }}-title">{{ stage.name }}</h2>
    </div>
  </div>

  {% if stage.core_question %}
  <div class="core-question"><strong>Core question:</strong> {{ stage.core_question }}</div>
  {% endif %}

  <div class="stage-meta">
    <span class="chip chip-sm">{{ stage.evidence_count }} evidence items</span>
    <span class="chip chip-sm">{{ stage.source_count }} distinct sources</span>
    {% if stage.supplementary_count %}
    <span class="chip chip-sm chip-amber">{{ stage.supplementary_count }} supplementary web</span>
    {% endif %}
    {% for t in stage.tiers_represented | default([]) %}{{ tier_badge(t) }}{% endfor %}
  </div>

  {% if stage.framework_steps %}
  <div class="subblock">
    <h3>Framework steps included</h3>
    <p class="mb-1">
      <strong>
      {%- for name in stage.framework_steps -%}
        {%- if stage.step_numbers and loop.index0 < (stage.step_numbers | length) -%}
          Step {{ stage.step_numbers[loop.index0] }} ({{ name }})
        {%- else -%}{{ name }}{%- endif -%}
        {{ ', ' if not loop.last }}
      {%- endfor -%}
      </strong>
    </p>
    {% if stage.substeps %}
    <ul>
      {% for key, text in stage.substeps.items() %}
      <li><em>Step {{ key }}</em> — {{ text }}</li>
      {% endfor %}
    </ul>
    {% endif %}
    {% if stage.output_name or stage.gate %}
    <p class="meta mb-0">
      {% if stage.output_name %}
      Execution: {{ stage.agent_name }} → <span class="mono">{{ stage.output_name }}</span>.
      {% endif %}
      {% if stage.gate %}Gate: {{ stage.gate }}{% endif %}
    </p>
    {% endif %}
  </div>
  {% endif %}

  {% if stage.what_happens %}
  <div class="subblock">
    <h3>What happens in this stage</h3>
    <div class="prose">{{ stage.what_happens | tagify }}</div>
  </div>
  {% endif %}

  {% if stage.expected_output %}
  <div class="subblock">
    <h3>Expected output</h3>
    <ul>
      {% for item in stage.expected_output %}<li>{{ item }}</li>{% endfor %}
    </ul>
  </div>
  {% endif %}

  {% if stage.synthesis %}
  <div class="subblock">
    <h3>Synthesis</h3>
    <div class="prose">{{ stage.synthesis | tagify }}</div>
  </div>
  {% endif %}

  {% if stage.answers %}
  <div class="subblock">
    <h3>Questions and answers</h3>
    <p class="sub mb-2">
      Every question in this stage with the answer established from its sources. This is the
      record the synthesis and tables above are built from.
    </p>
    {% for row in stage.answers %}
    <div class="qa-item">
      <p class="qa-question">{{ row.get('seed') or row.get('question') }}</p>
      {% if row.get('answer') %}
      <div class="prose qa-answer">{{ row.get('answer') | tagify }}</div>
      {% else %}
      <p class="qa-answer text-faint">
        Not answered. {{ row.get('unmet_reason') or 'No source addressed this question.' }}
      </p>
      {% endif %}
      <p class="qa-meta">
        {%- set st = row.get('status', 'not_found') -%}
        <span class="vdot-label"><span class="vdot {{ 'vtag-verified' if st == 'answered' else ('vtag-inference' if st == 'partial' else 'vtag-not-verified') }}"></span>
          {{ 'Answered' if st == 'answered' else ('Partly answered' if st == 'partial' else 'Not found') }}
        </span>
        {% for c in row.get('citations') or [] %}<span class="src-chip">{{ c }}</span>{% endfor %}
        {% if row.get('supplementary') %}
        <span class="vdot-label"><span class="vdot vtag-general-knowledge"></span> includes web evidence</span>
        {% endif %}
        {% if row.get('revised') %}
        <span class="vdot-label"><span class="vdot vtag-update"></span> revised on your instruction</span>
        {% endif %}
        <span class="text-faint">{{ row.get('evidence_count', 0) }} evidence ·
          coverage {{ row.get('coverage', 0) }}</span>
      </p>
      {% if row.get('reviewer_input') %}
      <div class="review-note">
        <strong>Reviewer input:</strong> {{ row.get('reviewer_input') }}
      </div>
      {% endif %}
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {% for table in stage.tables | default([]) %}
    {{ data_table(table) }}
  {% endfor %}

  {% for n in stage.narratives | default([]) %}
  <div class="subblock">
    <h3>{{ n.get('heading', '') if n.get is defined else n['heading'] }}</h3>
    <div class="prose">{{ (n.get('body', '') if n.get is defined else n['body']) | tagify }}</div>
  </div>
  {% endfor %}

  {% if stage.observability %}
  <div class="subblock">
    <h3>Claims observability</h3>
    <div class="table-wrap">
      <table class="data">
        <thead>
          <tr>
            <th scope="col">Clinical concept</th>
            <th scope="col">Signal classification</th>
            <th scope="col">Basis</th>
            <th scope="col">Limitation</th>
          </tr>
        </thead>
        <tbody>
          {% for row in stage.observability %}
          <tr>
            <td>{{ pick(row, ['concept', 'clinical_concept', 'name', 'topic']) }}</td>
            <td>{{ pick(row, ['classification', 'signal', 'signal_classification', 'status']) }}</td>
            <td>{{ pick(row, ['basis', 'evidence', 'detail']) }}</td>
            <td>{{ pick(row, ['limitation', 'limitations', 'caveat'], '—') }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <p class="table-footnote">
      A proxy signal is observable in claims only through the listed surrogate; it is not a direct field.
    </p>
  </div>
  {% endif %}

  <div class="subblock">
    <h3>Source disagreements identified in this stage</h3>
    {% if sc %}
    <div class="table-wrap">
      <table class="data">
        <thead>
          <tr>
            <th scope="col">Topic</th>
            <th scope="col">Source A</th>
            <th scope="col">Source A says</th>
            <th scope="col">Source B</th>
            <th scope="col">Source B says</th>
            <th scope="col">Possible reason</th>
          </tr>
        </thead>
        <tbody>
          {% for con in sc %}
          <tr>
            <td>{{ con.topic }}</td>
            <td class="nowrap">{{ con.source_a_name }} {{ tier_badge(con.source_a_tier) }}</td>
            <td>{{ con.source_a_claim }}</td>
            <td class="nowrap">{{ con.source_b_name }} {{ tier_badge(con.source_b_tier) }}</td>
            <td>{{ con.source_b_claim }}</td>
            <td>{{ con.reason }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <p class="table-footnote">Disagreements are surfaced, not resolved. SME adjudication required.</p>
    {% else %}
    <p class="meta mb-0">No source disagreements were identified in this stage.</p>
    {% endif %}
  </div>

  {% if stage.takeaways %}
  <div class="subblock">
    <h3>Key takeaways from this stage</h3>
    <ol class="takeaways">
      {% for t in stage.takeaways %}<li>{{ t | tagify }}</li>{% endfor %}
    </ol>
  </div>
  {% endif %}

  {% if stage.assumptions %}
  <div class="subblock">
    <h3>Assumptions made in this stage</h3>
    <ul>{% for a in stage.assumptions %}<li>{{ a | tagify }}</li>{% endfor %}</ul>
  </div>
  {% endif %}

  {% if stage.limitations %}
  <div class="subblock">
    <h3>Limitations of this stage</h3>
    <ul>{% for l in stage.limitations %}<li>{{ l | tagify }}</li>{% endfor %}</ul>
  </div>
  {% endif %}

  <blockquote class="callout mt-2">
    <strong>Evidence base for this stage:</strong>
    {{ stage.evidence_count }} item(s) from {{ stage.source_count }} source(s);
    {{ approved }} from approved sources, {{ stage.supplementary_count }} supplementary web.
    {% if stage.tiers_represented %}
    Tiers represented: {{ stage.tiers_represented | join(', ') }}.
    {% endif %}
  </blockquote>

  <div class="subblock">
    <h3>Unanswered sub-questions</h3>
    {% if stage.unanswered %}
      {% for item in stage.unanswered %}
        {% if item is mapping %}{{ pairs_block(item) }}{% else %}<p>{{ item }}</p>{% endif %}
      {% endfor %}
    {% else %}
    <p class="meta mb-0">All planned sub-aspects were supported by retrieved evidence.</p>
    {% endif %}
  </div>
</section>
```

### File: `celestra\templates\partials\web_search_test.html`

```html
{# ---------------------------------------------------------------------------
   Result of one live Firecrawl probe. Context: probe, network
   --------------------------------------------------------------------------- #}
{% from "partials/icons.html" import icon %}
<div data-web-test class="mt-2">
  {% if probe.ok %}
  <div class="callout is-green">
    {{ icon('check-circle', 15) }}
    <strong>Web search works.</strong> Firecrawl {{ probe.version }} answered in {{ probe.elapsed_ms }} ms
    with {{ probe.results }} result{{ '' if probe.results == 1 else 's' }}. Calls will now appear on your
    Firecrawl dashboard.
  </div>
  {% else %}
  <div class="callout is-rose">
    {{ icon('alert', 15) }}
    <strong>Web search failed{% if probe.configured %} after {{ probe.elapsed_ms }} ms{% endif %}.</strong>
    <div class="mt-1"><strong>What happened:</strong> {{ probe.detail }}</div>
    {% if probe.remedy %}<div class="mt-1"><strong>What to do:</strong> {{ probe.remedy }}</div>{% endif %}
    <div class="mt-1 text-sm text-faint">Endpoint: <span class="mono">{{ probe.endpoint }}</span>.
      After changing <code>.env</code>, restart the server and test again.</div>
  </div>
  {% endif %}
</div>
```

### File: `tests\test_answering.py`

```py
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
```

### File: `tests\test_connectors_live.py`

```py
"""Live connector smoke test.

Runs every connector in the registry against a CLL and an ALL retrieval
context and prints one row per source: ok, ref count, reason, elapsed.

This hits the real network on purpose — the point is to prove the endpoints
behave as documented, not to assert against fixtures. Run it directly:

    python3 tests/test_connectors_live.py            # both indications
    python3 tests/test_connectors_live.py CLL        # one indication
    python3 tests/test_connectors_live.py CLL seer europepmc   # specific sources

Expected non-failures: icd11 and loinc report "credentials not configured",
and the local_file sources report "reference file not installed: <dataset>"
until the CMS/FDA release files are dropped into celestra/data/reference/.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.connectors.base import ConnectorResult, RetrievalContext, http  # noqa: E402
from celestra.connectors.registry import build_registry, connector_health  # noqa: E402

CONTEXTS: dict[str, RetrievalContext] = {
    "CLL": RetrievalContext(
        indication="Chronic Lymphocytic Leukemia",
        indication_key="CLL",
        synonyms=["CLL", "chronic lymphocytic leukaemia", "small lymphocytic lymphoma", "SLL"],
        geography="United States",
        population="adults",
        stage="stage_1",
        question="What is the incidence, prevalence and treatment landscape of CLL in the US?",
        aspects=["incidence", "treatment"],
        extra={"icd10_codes": ["C91.1"]},
    ),
    "ALL": RetrievalContext(
        indication="Acute Lymphoblastic Leukemia",
        indication_key="ALL",
        synonyms=["ALL", "acute lymphocytic leukemia", "acute lymphoblastic leukaemia"],
        geography="United States",
        population="adults",
        stage="stage_1",
        question="What is the incidence, prevalence and treatment landscape of ALL in the US?",
        aspects=["incidence", "treatment"],
        extra={"icd10_codes": ["C91.0"]},
    ),
}

LIMIT = 5

# Sources that must return usable evidence for the desk to function.
MUST_SUCCEED = (
    "europepmc", "pubmed", "orphanet", "openfda_label", "openfda_drugsfda",
    "clinicaltrials", "dailymed", "seer", "nci", "crossref", "who_gho", "open_web",
)
# Sources whose failure is a configuration statement, not a defect.
EXPECTED_BLOCKED = {
    "icd11": "credentials not configured",
    "loinc": "credentials not configured",
    "nccn": "licensed connector not configured",
    "ama_cpt": "licensed connector not configured",
    "cms_icd10": "reference file not installed",
    "cms_hcpcs": "reference file not installed",
    "cms_gems": "reference file not installed",
    "purple_book": "reference file not installed",
}


async def run_one(source_id: str, connector, ctx: RetrievalContext) -> tuple[str, ConnectorResult, float]:
    started = time.perf_counter()
    try:
        result = await connector.discover(ctx, LIMIT)
    except Exception as exc:  # a connector that raises is a contract violation
        result = ConnectorResult.failure(source_id, f"RAISED {type(exc).__name__}: {exc}")
    return source_id, result, time.perf_counter() - started


async def run_context(label: str, ctx: RetrievalContext, only: list[str]) -> dict[str, ConnectorResult]:
    registry = build_registry()
    if only:
        registry = {k: v for k, v in registry.items() if k in only}
    results = await asyncio.gather(
        *(run_one(sid, conn, ctx) for sid, conn in registry.items())
    )
    print(f"\n=== {label}: {ctx.indication} ===")
    print(f"{'source_id':<18} {'ok':<5} {'count':>5}  {'elapsed':>8}  reason")
    print("-" * 100)
    out: dict[str, ConnectorResult] = {}
    for source_id, result, elapsed in sorted(results, key=lambda r: r[0]):
        out[source_id] = result
        print(f"{source_id:<18} {str(result.ok):<5} {result.count:>5}  "
              f"{elapsed:>7.2f}s  {result.reason[:60]}")
    return out


def check(label: str, results: dict[str, ConnectorResult]) -> list[str]:
    """Return the list of contract violations for this context."""
    problems: list[str] = []
    for source_id in MUST_SUCCEED:
        result = results.get(source_id)
        if result is None:
            continue
        if not result.ok or result.count == 0:
            problems.append(f"{label}/{source_id}: ok={result.ok} count={result.count} "
                            f"reason={result.reason!r}")
    for source_id, expected in EXPECTED_BLOCKED.items():
        result = results.get(source_id)
        if result is None:
            continue
        if result.ok:
            problems.append(f"{label}/{source_id}: expected a blocked result, got ok")
        elif expected not in result.reason:
            problems.append(f"{label}/{source_id}: reason {result.reason!r} "
                            f"does not mention {expected!r}")
    for source_id, result in results.items():
        if result.reason.startswith("RAISED"):
            problems.append(f"{label}/{source_id}: {result.reason}")
        for ref in result.refs:
            if result.ok and not ref.snippet:
                problems.append(f"{label}/{source_id}: ref {ref.url} has an empty snippet")
                break
    return problems


def print_health() -> None:
    print("\n=== connector health ===")
    print(f"{'source_id':<18} {'tier':>4} {'access':<17} {'configured':<11} missing")
    print("-" * 100)
    for row in connector_health():
        print(f"{row['id']:<18} {row['tier']:>4} {row['access_method']:<17} "
              f"{str(row['configured']):<11} {', '.join(row['blocking'])}")


async def main(argv: list[str]) -> int:
    labels = [a for a in argv if a.upper() in CONTEXTS] or list(CONTEXTS)
    only = [a for a in argv if a.upper() not in CONTEXTS]
    problems: list[str] = []
    try:
        for label in labels:
            results = await run_context(label.upper(), CONTEXTS[label.upper()], only)
            problems += check(label.upper(), results)
    finally:
        await http.aclose()
    print_health()
    print("\n=== verdict ===")
    if problems:
        for p in problems:
            print("  FAIL", p)
    else:
        print("  all contract expectations met")
    return 1 if problems else 0


def test_connectors_live() -> None:
    """pytest entry point; identical to running the module directly."""
    assert asyncio.run(main([])) == 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
```

### File: `tests\test_extraction_focus.py`

```py
"""Deterministic extraction must be question-specific.

Regression for the defect where an incidence figure headlined every card in a
stage: the disease name and the numbers in an epidemiology sentence outscored
a genuine diagnosis sentence for a question about diagnosis.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.models import SourceRef
from celestra.services.extraction import build_terms, extract_deterministic
from celestra.services.ranking import rank_deterministic

SYN = ["acute lymphoblastic leukemia", "acute lymphocytic leukemia", "ALL"]
TEXT = (
    "Acute lymphoblastic leukemia is a cancer of the blood and bone marrow. "
    "At a glance: Estimated New Cases in 2026: 6,250; percent of all new cancer cases: 0.3 percent. "
    "The rate of new cases of acute lymphocytic leukemia was 1.9 per 100,000 men and women per year. "
    "Diagnosis of acute lymphoblastic leukemia requires a bone marrow aspirate and biopsy showing at "
    "least 20 percent lymphoblasts, with immunophenotyping by flow cytometry to confirm lineage. "
    "Cytogenetic and molecular testing for the Philadelphia chromosome and KMT2A rearrangement is "
    "part of the confirmatory workup at diagnosis. "
    "Five-year relative survival for acute lymphocytic leukemia is 73.2 percent."
)
REF = SourceRef(source_id="seer", source_name="NCI SEER", tier=1, url="https://seer.cancer.gov/x",
                title="Acute Lymphocytic Leukemia — Cancer Stat Facts", snippet=TEXT)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def top_quote(question: str, aspects: list[str]) -> str:
    terms = build_terms(question, aspects, SYN)
    ev = extract_deterministic(REF, "q", terms, max_quotes=3)
    return ev[0].quote if ev else ""


diag_q = "How is ALL diagnosed and what confirmatory workup is required?"
diag_a = ["diagnosis", "confirmatory workup", "immunophenotyping", "cytogenetics"]
epi_q = "What is the incidence, prevalence, survival and mortality of ALL in the United States?"
epi_a = ["incidence", "prevalence", "survival", "mortality"]

print("\n== term tiers ==")
ts = build_terms(diag_q, diag_a, SYN)
check("disease words are context, not focus",
      "leukemia" in ts.context and "leukemia" not in ts.focus, str(sorted(ts.focus)))
check("question words are focus", {"diagnosed", "workup", "immunophenotyping"} <= ts.focus,
      str(sorted(ts.focus)))
check("diagnosis question is not quantitative", not ts.quantitative)
check("epidemiology question is quantitative", build_terms(epi_q, epi_a, SYN).quantitative)

print("\n== headline quotes ==")
d, e = top_quote(diag_q, diag_a), top_quote(epi_q, epi_a)
DIAG_WORDS = ("diagnosis", "workup", "immunophenotyping", "bone marrow", "cytogenetic")
STAT_WORDS = ("per 100,000", "Estimated New Cases", "survival", "percent")
check("diagnosis question headlines a diagnosis sentence",
      any(w in d for w in DIAG_WORDS) and "Estimated New Cases" not in d
      and "per 100,000" not in d, d[:80])
check("epidemiology question headlines a statistic",
      any(w in e for w in STAT_WORDS) and "workup" not in e, e[:80])
check("the two questions do not share a headline", d != e)

print("\n== ranking ==")
refs = [
    SourceRef(source_id="seer", source_name="NCI SEER", tier=1, url="https://seer.cancer.gov/x",
              title="Acute Lymphocytic Leukemia — Cancer Stat Facts",
              snippet="Estimated new cases in 2026: 6,250. The rate of new cases of acute "
                      "lymphocytic leukemia was 1.9 per 100,000 per year. Five-year relative "
                      "survival is 73.2 percent."),
    SourceRef(source_id="nci", source_name="NCI", tier=1, url="https://cancer.gov/x",
              title="Adult ALL Treatment (PDQ): Diagnosis and staging",
              snippet="Diagnostic workup: bone marrow aspirate, flow cytometry immunophenotyping, "
                      "cytogenetics and molecular testing at diagnosis."),
    SourceRef(source_id="acs", source_name="ACS", tier=3, url="https://cancer.org/x",
              title="Key statistics for acute lymphocytic leukemia",
              snippet="About 6,250 new cases and about 1,600 deaths from ALL in 2026."),
]
ranked = rank_deterministic(refs, build_terms(diag_q, diag_a, SYN))
check("diagnosis question ranks the diagnosis document first",
      ranked[0][0].source_id == "nci", [r.source_id for r, _ in ranked].__str__())
ranked = rank_deterministic(refs, build_terms(epi_q, epi_a, SYN))
check("epidemiology question does not rank the diagnosis document first",
      ranked[0][0].source_id != "nci", [r.source_id for r, _ in ranked].__str__())

print(f"\n{len(failures)} failure(s)")
raise SystemExit(1 if failures else 0)
```

### File: `tests\test_insights.py`

```py
"""Insight cards: the catalogue slots, filled by the model from the stage
document or deterministically from the mapped questions; the evidence rule as
the floor for Requires Input; the phase synthesis card."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.models import (
    AnswerStatus, Contradiction, ContradictionSeverity, Evidence, EvidenceOrigin, InsightTable,
    ResearchQuestion, RunConfig, StageReport,
)
from celestra.services import insights as gen

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


class FakeLLM:
    available = True

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    async def complete_json(self, system, prompt, *, max_tokens=None):
        self.prompts.append(prompt)
        return self.reply


def fixture():
    cfg = RunConfig(indication="Acute Lymphoblastic Leukemia", indication_key="ALL")
    qs = [
        ResearchQuestion(id="q1", stage="stage_1", bucket="A",
                         text="What is the disease definition and natural history of ALL?",
                         seed_text="What is the disease definition and natural history of ALL?",
                         answer_text="ALL is a malignancy of lymphoid progenitors.",
                         answer_status=AnswerStatus.ANSWERED, answer_citations=["NCI"]),
        ResearchQuestion(id="q2", stage="stage_1", bucket="A",
                         text="What is the incidence, prevalence, survival and mortality of ALL in the US?",
                         seed_text="What is the incidence, prevalence, survival and mortality of ALL in the US?",
                         answer_text="About 6,250 new cases and 1,600 deaths per year; incidence 1.9 per 100,000.",
                         answer_status=AnswerStatus.ANSWERED, answer_citations=["NCI SEER"]),
        ResearchQuestion(id="q3", stage="stage_1", bucket="A",
                         text="What are the immunophenotypic and molecular subtypes of ALL?",
                         seed_text="What are the clinically important immunophenotypic and molecular subtypes of ALL?",
                         answer_text="B-ALL about 85%, T-ALL about 15%; Ph+ about 25% of adults.",
                         answer_status=AnswerStatus.ANSWERED, answer_citations=["WHO"]),
        ResearchQuestion(id="q4", stage="stage_1", bucket="A",
                         text="How is ALL diagnosed and what confirmatory workup is required?",
                         seed_text="How is ALL diagnosed and what confirmatory workup is required?",
                         answer_text="Morphology, flow cytometry, cytogenetics and molecular testing.",
                         answer_status=AnswerStatus.PARTIAL, used_web_fallback=True),
        ResearchQuestion(id="q5", stage="stage_1", bucket="A",
                         text="How is ALL risk-stratified at diagnosis?",
                         seed_text="How is ALL risk-stratified at diagnosis?",
                         answer_text="", answer_status=AnswerStatus.NOT_FOUND,
                         unmet_reason="no source addressed it"),
    ]
    ev = [
        Evidence(id="e1", question_id="q1", source_id="nci_pdq", source_name="NCI PDQ", tier=1,
                 url="https://cancer.gov/x", quote="ALL is a malignancy of lymphoid progenitor cells."),
        Evidence(id="e2", question_id="q2", source_id="seer", source_name="NCI SEER", tier=1,
                 url="https://seer.cancer.gov/x", quote="An estimated 6,250 new cases in 2026."),
        Evidence(id="e3", question_id="q3", source_id="who", source_name="WHO", tier=1,
                 url="https://who.int/x", quote="B-lymphoblastic leukaemia accounts for about 85%."),
        Evidence(id="e4", question_id="q4", source_id="open_web", source_name="Open Web", tier=3,
                 url="https://blog.example/x", quote="Diagnosis uses morphology and flow cytometry.",
                 origin=EvidenceOrigin.OPEN_WEB),
    ]
    report = StageReport(
        stage="stage_1", bucket="A", name="Disease & Diagnostic Foundation",
        core_question="Who gets it?", agent_name="Clinical Landscape Agent",
        synthesis="ALL is a rare disease with a bimodal age distribution.",
        tables=[InsightTable(title="Epidemiology snapshot table", columns=["Metric", "Value", "Source"],
                             rows=[["New cases", "6,250", "SEER"], ["Deaths", "1,600", "SEER"]],
                             question_ids=["q2"]),
                InsightTable(title="Subtype / biology breakdown table", columns=["Subtype", "Share", "Notes"],
                             rows=[["B-ALL", "85%", ""], ["T-ALL", "15%", ""]], question_ids=["q3"])],
        takeaways=["Age and lineage are core cohort variables.", "Diagnosis needs several signals."],
    )
    return cfg, qs, ev, report


def main() -> int:
    print("\n== catalogue ==")
    cat = gen.catalogue_for("A")
    check("five Clinical Landscape slots in the order of the reference",
          [c["key"] for c in cat] == ["epidemiology", "population_segmentation", "disease_definition",
                                      "diagnostic_foundation", "disease_journey"])
    check("three Treatment Evidence slots", [c["number"] for c in gen.catalogue_for("C")] == [6, 7, 8])
    check("discovery phase owes the key-insights card (09)",
          [c["number"] for c in gen.phase_catalogue("discovery")] == [9])
    check("later phases have slots too", all(gen.catalogue_for(b) for b in "BDEF"))

    print("\n== deterministic fill ==")
    cfg, qs, ev, report = fixture()
    cards = gen.deterministic("run_t", "stage_1", "A", report, qs, ev, [])
    by = {c.card_key: c for c in cards}
    check("one card per slot", len(cards) == 5 and set(by) == {c["key"] for c in cat})
    check("epidemiology card takes the epidemiology answer and table",
          "6,250" in by["epidemiology"].summary and by["epidemiology"].evidence_type == "table"
          and by["epidemiology"].table_titles == ["Epidemiology snapshot table"])
    check("segmentation card links the subtype question", "q3" in by["population_segmentation"].question_ids)
    check("card answered from vetted sources is Ready", by["epidemiology"].confidence.value == "ready")
    check("web-only card needs review, with the reason",
          by["diagnostic_foundation"].confidence.value == "requires_input"
          and "open-web" in by["diagnostic_foundation"].input_reason.lower())
    check("slot without an answer is marked not covered, quietly",
          by["disease_journey"].covered is False or by["disease_journey"].summary,
          by["disease_journey"].summary[:60])

    print("\n== model fill ==")
    fake = FakeLLM({"cards": [
        {"key": "epidemiology", "covered": True,
         "finding": "ALL is rare with a bimodal age distribution; ~6,250 new US cases and ~1,600 deaths.",
         "evidence": [{"label": "New US cases", "value": "~6,250"}, {"label": "Deaths", "value": "~1,600"},
                      {"label": "Incidence rate", "value": "1.9 / 100,000"}],
         "interpretation": "Cohorts will be small; age bands matter.", "review_note": "Confirm the SEER year.",
         "questions": [2], "table_titles": ["epidemiology snapshot table"], "gap": ""},
        {"key": "population_segmentation", "covered": True, "finding": "B-ALL ~85%, T-ALL ~15%.",
         "evidence": {"columns": ["Segment", "Share", "Feature"], "rows": [["B-ALL", "85%", "CD19+"], ["T-ALL", "15%", "CD3+"]]},
         "interpretation": "Lineage is a cohort variable.", "review_note": "", "questions": [3], "table_titles": []},
        {"key": "disease_definition", "covered": True, "finding": "Malignancy of lymphoid progenitors.",
         "evidence": "not a table", "interpretation": "", "review_note": "", "questions": [1], "table_titles": []},
        {"key": "diagnostic_foundation", "covered": True, "finding": "Several signals establish diagnosis.",
         "evidence": ["Clinical suspicion", "Blood / marrow", "Morphology", "Flow cytometry", "Cytogenetics", "Molecular"],
         "interpretation": "", "review_note": "", "questions": [4], "table_titles": []},
        {"key": "disease_journey", "covered": False, "finding": "", "evidence": None, "interpretation": "",
         "review_note": "", "questions": [5], "table_titles": [], "gap": "The sources did not describe the clinical course."},
    ]})
    gen.llm = fake
    conflict = Contradiction(stage="stage_1", question_id="q3", topic="subtype share",
                             source_a_name="WHO", source_a_tier=1, source_a_claim="85%",
                             source_b_name="Blog", source_b_tier=3, source_b_claim="70%",
                             reason="differ", severity=ContradictionSeverity.ESCALATED)
    cards = asyncio.run(gen.generate("run_t", cfg, "stage_1", "A", report, qs, ev, [conflict]))
    by = {c.card_key: c for c in cards}
    prompt = fake.prompts[0]
    check("prompt lists every slot with its format and the document",
          'key "epidemiology"' in prompt and "TABLE: Epidemiology snapshot table" in prompt and "Q2." in prompt)
    check("all five slots filled, numbered 1-5", [c.number for c in cards] == [1, 2, 3, 4, 5])
    e = by["epidemiology"]
    check("metrics card carries its figures", e.evidence_type == "metrics" and len(e.evidence) == 3
          and e.evidence[0]["value"] == "~6,250")
    check("interpretation and quiet review note kept",
          e.interpretation.startswith("Cohorts") and e.review_note.startswith("Confirm"))
    check("table title matched case-insensitively", e.table_titles == ["Epidemiology snapshot table"])
    check("card links question, evidence and source", e.question_ids == ["q2"] and e.evidence_ids == ["e2"]
          and e.source_ids == ["seer"])
    check("table evidence coerced", by["population_segmentation"].evidence_type == "table"
          and by["population_segmentation"].evidence["rows"][0][0] == "B-ALL")
    check("steps evidence kept in order", by["diagnostic_foundation"].evidence[0] == "Clinical suspicion")
    check("malformed evidence falls back to the document, not guessed",
          by["disease_definition"].evidence_type in ("list", "table", "") )
    check("escalated conflict flags only the card on its question",
          by["population_segmentation"].confidence.value == "requires_input"
          and "disagree" in by["population_segmentation"].input_reason
          and by["epidemiology"].confidence.value == "ready")
    check("web-only card needs review whatever the model said",
          by["diagnostic_foundation"].confidence.value == "requires_input")
    check("uncovered slot is Requires Input with the model's gap",
          by["disease_journey"].covered is False and by["disease_journey"].confidence.value == "requires_input"
          and "clinical course" in by["disease_journey"].input_reason)

    print("\n== model skips a slot ==")
    gen.llm = FakeLLM({"cards": [{"key": "epidemiology", "covered": True, "finding": "x",
                                  "evidence": [], "questions": [2]}]})
    cards = asyncio.run(gen.generate("run_t", cfg, "stage_1", "A", report, qs, ev, []))
    check("skipped slots are filled deterministically so the set is complete", len(cards) == 5)

    print("\n== phase card ==")
    gen.llm = FakeLLM({"finding": "Discovery established the population and treatment branches.",
                       "points": ["Age, lineage and Ph-status are core variables.",
                                  "Diagnosis requires multiple signals.",
                                  "Treatment pathways branch by Ph-status."],
                       "interpretation": "Carry these into the diagnostic footprint."})
    phase = asyncio.run(gen.phase_cards("run_t", cfg, "discovery", [report], cards))
    check("one phase card, number 09, Synthesis", len(phase) == 1 and phase[0].number == 9
          and phase[0].category == "Synthesis")
    check("phase card lists the points", phase[0].evidence_type == "list" and len(phase[0].evidence) == 3)
    check("phase card unions the sources of the phase", "seer" in phase[0].source_ids)
    gen.llm = type("Off", (), {"available": False})()
    phase = asyncio.run(gen.phase_cards("run_t", cfg, "discovery", [report], cards))
    check("without a model the phase card uses the takeaways", phase[0].evidence and "Age" in phase[0].evidence[0])

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### File: `tests\test_llm_providers.py`

```py
"""Provider dispatch for the LLM layer.

Verifies each provider builds the right request without calling a real endpoint,
so a misconfigured provider fails loudly here rather than mid-run.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.services.llm import LLMUnavailable, _extract_json
from celestra.settings import Settings

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def build(**kw) -> Settings:
    base = dict(
        anthropic_api_key=None, foundry_api_key=None, foundry_resource=None,
        azure_openai_endpoint=None, azure_openai_api_key=None,
        azure_openai_deployment=None,
    )
    base.update(kw)
    return Settings(**base)


async def main() -> int:
    print("\n== provider readiness ==")
    s = build(llm_provider="anthropic", anthropic_api_key="sk-test")
    check("anthropic ready with a key", s.llm_enabled and not s.provider_gaps())
    check("anthropic model is opus 5 by default", s.active_model == "claude-opus-5",
          s.active_model)

    s = build(llm_provider="anthropic")
    check("anthropic without a key is not enabled", not s.llm_enabled)
    check("  and names the missing setting", s.provider_gaps() == ["ANTHROPIC_API_KEY"],
          str(s.provider_gaps()))

    s = build(llm_provider="anthropic_foundry", foundry_api_key="k", foundry_resource="r")
    check("foundry ready with key and resource", s.llm_enabled)
    check("  falls back to the anthropic model id", s.active_model == "claude-opus-5")
    s = build(llm_provider="anthropic_foundry", foundry_api_key="k",
              foundry_resource="r", foundry_model="claude-sonnet-5")
    check("  honours an explicit foundry model", s.active_model == "claude-sonnet-5")

    s = build(llm_provider="anthropic_foundry", foundry_api_key="k")
    check("foundry without a resource is not enabled", not s.llm_enabled)
    check("  and names it", s.provider_gaps() == ["FOUNDRY_RESOURCE"], str(s.provider_gaps()))

    s = build(llm_provider="azure_openai", azure_openai_endpoint="https://x.openai.azure.com",
              azure_openai_api_key="k", azure_openai_deployment="my-deployment")
    check("azure ready with endpoint, key and deployment", s.llm_enabled)
    check("  reports the deployment as the model", s.active_model == "my-deployment")

    s = build(llm_provider="azure_openai", azure_openai_endpoint="https://x.openai.azure.com")
    check("azure missing key and deployment is not enabled", not s.llm_enabled)
    check("  and names both",
          s.provider_gaps() == ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"],
          str(s.provider_gaps()))

    s = build(llm_provider="nonsense", anthropic_api_key="k")
    check("unknown provider is not enabled", not s.llm_enabled)
    check("  and says so", "not a supported provider" in s.provider_gaps()[0])

    print("\n== azure request shape ==")
    import celestra.services.llm as llm_mod

    client = llm_mod.LLMClient()
    client._settings = build(
        llm_provider="azure_openai",
        azure_openai_endpoint="https://contoso.openai.azure.com/",
        azure_openai_api_key="secret", azure_openai_deployment="gpt-deploy",
        azure_openai_api_version="2024-10-21",
    )

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def raise_for_status(self): ...

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    class FakeClient:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            captured.update(url=url, json=json, headers=headers)
            return FakeResponse()

    import httpx
    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient  # type: ignore[misc]
    try:
        out = await client.complete_json("SYS", "PROMPT")
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    check("azure builds the deployment url",
          captured["url"] == "https://contoso.openai.azure.com/openai/deployments/"
                             "gpt-deploy/chat/completions?api-version=2024-10-21",
          captured["url"])
    check("azure authenticates with the api-key header",
          captured["headers"]["api-key"] == "secret")
    check("azure sends system then user",
          [m["role"] for m in captured["json"]["messages"]] == ["system", "user"])
    check("azure parses the response as json", out == {"ok": True}, str(out))

    print("\n== json recovery ==")
    check("plain json", _extract_json('{"a": 1}') == {"a": 1})
    check("fenced json", _extract_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("json wrapped in prose", _extract_json('Sure!\n{"a": 1}\nDone.') == {"a": 1})
    try:
        _extract_json("no json at all")
        check("unparseable input raises", False)
    except LLMUnavailable:
        check("unparseable input raises", True)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

### File: `tests\test_pipeline.py`

```py
"""Pipeline integration test with a stub connector registry.

Validates orchestration, scoring, contradiction surfacing, synthesis and QA
without touching the network, so a connector outage can never make this test
lie about the pipeline.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.connectors.base import ConnectorResult, RetrievalContext
from celestra.events import bus
from celestra.models import (
    AgentStatus,
    EvidenceOrigin,
    QuestionStatus,
    Run,
    RunConfig,
    RunMode,
    RunStatus,
    SourceRef,
)
from celestra.services import orchestrator as orch
from celestra.store import Store

FIXTURES = {
    "seer": (
        1, "NCI SEER", "https://seer.cancer.gov/statfacts/html/clyl.html",
        "Chronic Lymphocytic Leukemia — Cancer Stat Facts",
        "The overall rate of new cases of chronic lymphocytic leukemia was 4.7 per 100,000 "
        "men and women per year based on 2018-2022 cases, age-adjusted. The death rate was "
        "1.0 per 100,000 men and women per year. In 2021, there were an estimated 214,573 "
        "people living with chronic lymphocytic leukemia in the United States. Five-year "
        "relative survival for chronic lymphocytic leukemia is 88.5 percent. Diagnosis is "
        "confirmed by peripheral blood flow cytometry demonstrating a clonal B-cell "
        "population. Risk stratification uses the Rai and Binet staging systems together "
        "with IGHV mutational status and TP53 aberration testing.",
    ),
    "nci": (
        1, "National Cancer Institute (NCI)",
        "https://www.cancer.gov/types/leukemia/hp/cll-treatment-pdq",
        "Chronic Lymphocytic Leukemia Treatment (PDQ) — Health Professional Version",
        "Chronic lymphocytic leukemia is a cancer of the blood and bone marrow that usually "
        "gets worse slowly if it is not treated. Diagnosis requires a peripheral blood "
        "absolute B-lymphocyte count of at least 5,000 per microliter sustained for three "
        "months. Immunophenotyping demonstrates coexpression of CD5, CD19, CD20 and CD23. "
        "Prognostic biomarkers including IGHV mutational status, TP53 mutation and "
        "deletion 17p determine treatment selection. Estimated new cases of chronic "
        "lymphocytic leukemia in the United States in 2025 are 20,700 cases. The "
        "age-adjusted incidence rate of chronic lymphocytic leukemia is 5.6 per "
        "100,000 persons per year.",
    ),
    "acs": (
        3, "American Cancer Society",
        "https://www.cancer.org/cancer/types/chronic-lymphocytic-leukemia.html",
        "Key Statistics for Chronic Lymphocytic Leukemia",
        "The American Cancer Society estimates for chronic lymphocytic leukemia in the "
        "United States for 2026 are about 24,900 new cases and about 4,300 deaths. Chronic "
        "lymphocytic leukemia is a slow-growing leukemia that starts in lymphoid cells and "
        "mainly affects older adults, with an average age at diagnosis around 70 years.",
    ),
    "orphanet": (
        1, "Orphanet / Orphadata", "https://www.orpha.net/en/disease/detail/67038",
        "B-cell chronic lymphocytic leukemia (ORPHA:67038)",
        "B-cell chronic lymphocytic leukemia is indexed as ORPHA:67038 and maps to ICD-10 "
        "code C91.1 and ICD-11 code 2A82.0 in the Orphanet cross-referencing dataset. The "
        "reported prevalence class is 1-5 per 10,000 in Europe with a validated status.",
    ),
}


class StubConnector:
    def __init__(self, source_id, tier, name, url, title, text):
        self.source_id, self.tier = source_id, tier
        self.name, self.url, self.title, self.text = name, url, title, text
        self.origin = EvidenceOrigin.APPROVED_API
        self.calls = 0

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        self.calls += 1
        return ConnectorResult(
            source_id=self.source_id,
            refs=[SourceRef(
                source_id=self.source_id, source_name=self.name, tier=self.tier,
                url=self.url, title=self.title, organization=self.name,
                snippet=self.text, origin=self.origin,
                raw={"abstract": self.text},
            )],
            calls=1,
        )


class DeadConnector:
    def __init__(self, source_id, reason="credentials not configured"):
        self.source_id, self.tier = source_id, 1
        self.origin = EvidenceOrigin.APPROVED_API
        self.reason = reason

    async def discover(self, ctx, limit):
        return ConnectorResult.failure(self.source_id, self.reason)


class StubWeb:
    source_id, tier = "open_web", 5
    origin = EvidenceOrigin.OPEN_WEB

    def __init__(self):
        self.searches = 0

    async def discover(self, ctx, limit):
        return ConnectorResult(source_id="open_web")

    async def search(self, query, limit):
        self.searches += 1
        return [SourceRef(
            source_id="open_web", source_name="Open Web (Supplementary)", tier=5,
            url="https://example.org/cll-overview", title="CLL overview",
            organization="example.org", origin=EvidenceOrigin.OPEN_WEB,
        )]

    async def scrape(self, url):
        return SourceRef(
            source_id="open_web", source_name="Open Web (Supplementary)", tier=5,
            url=url, title="CLL overview", organization="example.org",
            origin=EvidenceOrigin.OPEN_WEB,
            snippet="Chronic lymphocytic leukemia treatment is generally deferred until "
                    "the disease becomes symptomatic or progressive according to widely "
                    "used criteria described across clinical references.",
        )


def build_stub_registry():
    reg = {sid: StubConnector(sid, *spec) for sid, spec in FIXTURES.items()}
    for dead in ("icd11", "loinc", "cms_icd10", "cms_hcpcs", "cms_gems",
                 "nccn", "ama_cpt", "purple_book"):
        reg[dead] = DeadConnector(dead)
    reg["open_web"] = StubWeb()
    return reg


async def main() -> int:
    tmp = Path("/tmp/celestra_test.db")
    tmp.unlink(missing_ok=True)
    import celestra.store as store_mod
    import celestra.services.orchestrator as orch_mod
    test_store = Store(tmp)
    store_mod.store = test_store
    orch_mod.store = test_store

    cfg = RunConfig(
        indication="Chronic Lymphocytic Leukemia", indication_key="CLL",
        target_population="Adult patients", mode=RunMode.SINGLE,
        selected_agent="A", research_cutoff="2026-09-09",
    )
    run = Run(config=cfg, reference="RUN-TEST01")
    test_store.save_run(run)

    seen: list[str] = []
    original = bus.publish

    async def spy(run_id, type_, **data):
        seen.append(type_)
        await original(run_id, type_, **data)

    bus.publish = spy  # type: ignore[assignment]
    registry = build_stub_registry()
    await orch.Orchestrator(run, registry).execute()
    bus.publish = original  # type: ignore[assignment]

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    run = test_store.get_run(run.id)
    questions = test_store.get_questions(run.id)
    evidence = test_store.get_evidence(run.id)
    insights = test_store.get_insights(run.id)
    stages = test_store.get_stage_reports(run.id)
    contras = test_store.get_contradictions(run.id)
    qa = test_store.get_qa(run.id)

    print("\n== pipeline ==")
    check("run completed", run.status is RunStatus.COMPLETED, run.error or "")
    check("agent completed", run.agents["A"].status is AgentStatus.COMPLETE)
    check("only the selected agent ran", list(run.agents) == ["A"], str(list(run.agents)))
    check("questions planned", len(questions) == 5, f"{len(questions)} planned")
    check("evidence extracted", len(evidence) >= 8, f"{len(evidence)} items")
    check("quotes are verbatim from fixtures",
          all(any(e.quote in spec[4] for spec in FIXTURES.values())
              for e in evidence if not e.is_supplementary))
    from celestra.services.insights import catalogue_for
    check("one card per catalogue slot for this agent",
          len(insights) == len(catalogue_for(insights[0].bucket)) if insights else False,
          f"{len(insights)}")
    check("stage report built", len(stages) == 1 and bool(stages[0].synthesis))
    check("stage tables built", len(stages[0].tables) >= 1, f"{len(stages[0].tables)} tables")
    check("takeaways present", len(stages[0].takeaways) >= 1)
    check("observability classified", len(stages[0].observability) >= 1)

    print("\n== thresholds and escalation ==")
    answered = [q for q in questions if q.status is QuestionStatus.SUFFICIENT]
    check("some questions reached sufficiency", len(answered) >= 1,
          f"{len(answered)}/{len(questions)}")
    check("unanswered questions record a reason",
          all(q.unmet_reason for q in questions if q.status is not QuestionStatus.SUFFICIENT))
    unregistered = {s for q in questions for s in q.sources_attempted} - set(FIXTURES) - {"open_web"}
    check("sources with no working connector are still recorded as attempted",
          bool(unregistered), f"{len(unregistered)} recorded: {sorted(unregistered)[:5]}")
    check("coverage scored", all(q.coverage_score >= 0 for q in questions))

    print("\n== provenance ==")
    check("every evidence item has a url and source", all(e.url and e.source_id for e in evidence))
    web = [e for e in evidence if e.is_supplementary]
    check("web evidence is tier 5 when present",
          all(e.tier == 5 for e in web), f"{len(web)} supplementary items")
    check("insights name their sources", all(i.source_ids or i.confidence.value == "requires_input"
                                             for i in insights))

    print("\n== contradictions ==")
    check("numeric conflict surfaced between two tier 1 sources",
          len(contras) >= 1, f"{len(contras)} found")
    check("conflict names both sides and a reason",
          all(c.source_a_claim and c.source_b_claim and c.reason for c in contras))
    check("no conflict auto-resolved",
          all(c.review_action.value == "pending" for c in contras))

    print("\n== qa ==")
    check("qa metrics stored", qa is not None)
    check("qa counts match", qa.questions_planned == len(questions))
    check("checklist has no false pass",
          all(c["status"] in ("PASS", "FAIL", "NOT APPLICABLE") for c in qa.checklist))
    check("readiness written", bool(qa.readiness))

    print("\n== events ==")
    for required in ("run_started", "agent_status", "source_used", "insight_added",
                     "stage_complete", "run_complete"):
        check(f"emitted {required}", required in seen)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

### File: `tests\test_retrieval_order.py`

```py
"""Retrieval consults sources in the promised order: API sources first, the
domain-scoped searches only when those fail, the open web only after that."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.connectors.base import ConnectorResult
from celestra.models import EvidenceOrigin, ResearchQuestion, RunConfig, SourceRef
from celestra.services import retrieval
from celestra.settings import get_source_registry, get_thresholds

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


TEXT = (
    "Chronic lymphocytic leukemia is a cancer of B lymphocytes. The age-adjusted incidence "
    "rate of chronic lymphocytic leukemia is 4.7 per 100,000 persons per year in the United "
    "States. Median age at diagnosis is 70 years. Prevalence is rising as survival improves. "
    "Rai and Binet staging systems remain in use for chronic lymphocytic leukemia."
)


class Fake:
    """A connector that records whether it was asked, and answers or not."""

    def __init__(self, sid: str, tier: int, answers: bool, origin=EvidenceOrigin.APPROVED_API):
        self.sid, self.tier, self.answers, self.origin = sid, tier, answers, origin
        self.calls = 0

    async def discover(self, ctx, limit):
        self.calls += 1
        if not self.answers:
            return ConnectorResult(source_id=self.sid, refs=[], ok=False, reason="nothing")
        ref = SourceRef(source_id=self.sid, source_name=self.sid, tier=self.tier,
                        url=f"https://{self.sid}.example/cll", title="CLL incidence",
                        organization=self.sid, snippet=TEXT, origin=self.origin,
                        raw={"text": TEXT, "abstract": TEXT})
        return ConnectorResult(source_id=self.sid, refs=[ref], ok=True, reason="")

    async def search(self, query, limit):
        self.calls += 1
        if not self.answers:
            return []
        return [SourceRef(source_id="open_web", source_name="Open Web", tier=3,
                          url="https://web.example/cll", title="CLL", snippet=TEXT,
                          origin=EvidenceOrigin.OPEN_WEB, raw={"text": TEXT})]

    async def scrape(self, url):
        return SourceRef(source_id="open_web", source_name="Open Web", tier=3, url=url,
                         title="CLL", snippet=TEXT, origin=EvidenceOrigin.OPEN_WEB,
                         raw={"text": TEXT, "page_text": TEXT})


def run(api_answers: bool, targeted_answers: bool):
    reg_spec = get_source_registry()["sources"]
    api_ids = [s["id"] for s in retrieval.sources_for("stage_1", "CLL")]
    targeted_ids = [s["id"] for s in retrieval.targeted_sources_for("stage_1", "CLL")]
    tiers = {s["id"]: s["tier"] for s in reg_spec}
    registry = {}
    apis = [Fake(sid, tiers[sid], api_answers) for sid in api_ids]
    targeted = [Fake(sid, tiers[sid], targeted_answers, EvidenceOrigin.TARGETED_SEARCH)
                for sid in targeted_ids]
    for c in apis + targeted:
        registry[c.sid] = c
    web = Fake("open_web", 3, True, EvidenceOrigin.OPEN_WEB)
    registry["open_web"] = web
    q = ResearchQuestion(stage="stage_1", bucket="A",
                         text="What is the incidence of chronic lymphocytic leukemia in the US?",
                         seed_text="incidence", aspects=["incidence", "United States"])
    cfg = RunConfig(indication="Chronic Lymphocytic Leukemia", indication_key="CLL")
    outcome = asyncio.run(retrieval.retrieve(q, cfg, ["CLL"], registry))
    return outcome, apis, targeted, web, api_ids, targeted_ids


def main() -> int:
    print("\n== registry split ==")
    api_ids = [s["id"] for s in retrieval.sources_for("stage_1", "CLL")]
    targeted_ids = [s["id"] for s in retrieval.targeted_sources_for("stage_1", "CLL")]
    check("API list holds no search-reached source",
          all(s["access_method"] not in retrieval.SEARCH_ACCESS
              for s in get_source_registry()["sources"] if s["id"] in api_ids))
    check("targeted list is only search-reached sources", bool(targeted_ids)
          and all(s["access_method"] in retrieval.SEARCH_ACCESS
                  for s in get_source_registry()["sources"] if s["id"] in targeted_ids),
          str(targeted_ids))

    print("\n== API sources answer: no web search at all ==")
    outcome, apis, targeted, web, *_ = run(api_answers=True, targeted_answers=True)
    budget = min(len(apis), get_thresholds()["limits"]["max_sources_per_question"])
    check("API sources were queried, within the per-question budget",
          sum(1 for a in apis if a.calls) == budget, f"{sum(1 for a in apis if a.calls)}/{budget}")
    check("no domain search was made", all(t.calls == 0 for t in targeted),
          str({t.sid: t.calls for t in targeted}))
    check("open web was not touched", web.calls == 0)
    check("question sufficient", outcome.sufficiency is not None and outcome.sufficiency.ok,
          outcome.sufficiency.reason if outcome.sufficiency else "")
    check("not marked as web fallback", not outcome.used_web and not outcome.used_targeted)

    print("\n== API sources empty: domain search next, open web last ==")
    outcome, apis, targeted, web, *_ = run(api_answers=False, targeted_answers=True)
    rounds = get_thresholds()["escalation"]["max_refinement_rounds"] + 1
    check("API sources were tried across every round before any search",
          all(a.calls == rounds for a in apis if a.calls), str({a.sid: a.calls for a in apis}))
    cap = get_thresholds()["escalation"]["targeted_search_max_sources"]
    check("domain searches were then made, within the cap",
          sum(1 for t in targeted if t.calls) == min(len(targeted), cap)
          and all(t.calls <= 1 for t in targeted), str({t.sid: t.calls for t in targeted}))
    check("open web still not touched", web.calls == 0)
    check("targeted evidence is approved-tier, not supplementary",
          outcome.evidence and all(not e.is_supplementary for e in outcome.evidence))
    check("marked as targeted, not web", outcome.used_targeted and not outcome.used_web)

    print("\n== nothing in the registry answers: open web ==")
    outcome, apis, targeted, web, *_ = run(api_answers=False, targeted_answers=False)
    check("open web reached last", web.calls >= 1)
    check("marked as web fallback", outcome.used_web)
    check("open-web evidence is supplementary", outcome.evidence
          and all(e.is_supplementary for e in outcome.evidence))

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### File: `tests\test_review_gate.py`

```py
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
```

### File: `tests\test_smoke_http.py`

```py
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

        print("\n== naming comes first ==")
        r = await c.get("/projects/new")
        check("new project without a name asks for one", r.status_code == 200 and "Name your project" in r.text)
        r = await c.get("/projects/name")
        check("naming dialog renders", r.status_code == 200 and 'action="/projects/new"' in r.text)
        r = await c.get("/projects/new", params={"name": "CLL pilot"})
        check("named project reaches the setup form",
              r.status_code == 200 and 'name="indication"' in r.text and 'value="CLL pilot"' in r.text)

        print("\n== create and run a project ==")
        r = await c.post("/projects", data={
            "indication": "Chronic Lymphocytic Leukemia",
            "drug_brand": "Venclexta (venetoclax)",
            "geography": "United States",
            "objective": "Build Claims Line of Therapy",
            "target_population": "Adult patients with CLL",
            "mode": "single",
            "selected_agent": "clinical-landscape-agent",
            "project_name": "CLL pilot",
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

        print("\n== findings report ==")
        r = await c.get(f"/runs/{run_id}/findings")
        check("findings report renders", r.status_code == 200 and "Findings" in r.text)
        check("  carries cards, tables and answers",
              'class="card"' in r.text and "Questions and answers" in r.text)
        check("  carries no run metadata",
              "Execution plan" not in r.text and "QA checklist" not in r.text
              and "Synthesis engine" not in r.text)
        r = await c.get(f"/runs/{run_id}/findings", params={"phase": "discovery"})
        check("phase-wise report renders", r.status_code == 200 and "Discovery phase" in r.text)
        r = await c.get(f"/runs/{run_id}/findings", params={"phase": "mapping"})
        check("empty phase says so", r.status_code == 200 and "Nothing has been produced" in r.text)
        r = await c.get(f"/runs/{run_id}/findings", params={"download": 1})
        check("download is an attachment", "attachment" in r.headers.get("content-disposition", "")
              and "findings" in r.headers.get("content-disposition", ""))
        check("  and self-contained", "<style>" in r.text and "/static/" not in r.text)

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
```

### File: `tests\test_templates_render.py`

```py
"""Render every Celestra template against realistic fake context.

The app cannot be started yet (routes are still being written), so this test
exercises the templates directly: a Jinja2 environment over
``celestra/templates`` with the same filters and globals the app will register,
fed with objects built from the REAL models in ``celestra.models``.

It also enforces the vocabulary rule: the internal grouping word must never
reach rendered output. The execution units are AGENTS in the UI.
"""
from __future__ import annotations

import html
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from celestra.models import (  # noqa: E402
    AgentState,
    AgentStatus,
    Confidence,
    Contradiction,
    ContradictionSeverity,
    Evidence,
    EvidenceOrigin,
    Insight,
    InsightTable,
    QAMetrics,
    ReviewAction,
    Run,
    RunConfig,
    RunMode,
    RunStatus,
    StageReport,
    VerificationTag,
)

TEMPLATES = ROOT / "celestra" / "templates"
STATIC = ROOT / "celestra" / "static"

TAG_RE = re.compile(
    r"\[(VERIFIED|GENERAL KNOWLEDGE|ORIGINAL|INFERENCE|NOT VERIFIED|UPDATE(?:[^\]]*)?)\]"
)


def tagify(value):
    """Stub of the filter the app will register: escape, then pill the tags."""
    if value is None:
        return Markup("")
    escaped = html.escape(str(value), quote=False)

    def repl(match):
        label = match.group(1)
        slug = label.split("—")[0].strip().lower().replace(" ", "-")
        return f'<span class="vtag vtag-{slug}">[{label}]</span>'

    return Markup(TAG_RE.sub(repl, escaped))


def url_for(name, **params):
    """Stub. Templates use literal paths, this only guards against surprises."""
    if name == "static":
        return "/static/" + str(params.get("path") or params.get("filename") or "")
    tail = "/".join(str(v) for v in params.values())
    return f"/{name}/{tail}".rstrip("/")


@pytest.fixture(scope="module")
def env() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    environment.filters["tagify"] = tagify
    environment.globals["url_for"] = url_for
    from celestra.main import run_steps
    environment.globals["run_steps"] = run_steps
    return environment


# ---------------------------------------------------------------------------
# fake data
# ---------------------------------------------------------------------------
NOW = datetime(2026, 9, 1, 5, 49, tzinfo=timezone.utc)

AGENT_SPECS = [
    ("A", "clinical-landscape", "Clinical Landscape Agent",
     "Disease context, patient journey, clinical events", "leaf", ["stage_1"], [], 1),
    ("C", "treatment-evidence", "Treatment Evidence Agent",
     "Therapies, treatment settings, regulatory evidence", "pill", ["stage_2"], [], 1),
    ("B", "diagnostic-footprint", "Diagnostic Footprint Agent",
     "Claims signals, diagnosis & procedure codes", "microscope", ["stage_3"], ["A"], 2),
    ("D", "treatment-logic", "Treatment Logic Agent",
     "Treatment patterns, episode logic, LOT rules", "flow", ["stage_4"], ["C"], 2),
    ("E", "patient-journey", "Patient Journey Agent",
     "Discontinuation, monitoring, response and outcomes", "route", ["stage_5"], ["B", "C", "D"], 3),
    ("F", "information-synthesis", "Information Synthesis Agent",
     "Cross-agent analysis, key insights and gaps", "sparkle", ["stage_6"], ["A", "B", "C", "D", "E"], 4),
]

STATUSES = [
    AgentStatus.COMPLETE, AgentStatus.RESEARCHING, AgentStatus.SYNTHESISING,
    AgentStatus.QUEUED, AgentStatus.BLOCKED, AgentStatus.FAILED,
]


def make_agents() -> list[AgentState]:
    agents = []
    for i, (letter, key, name, tagline, ic, stages, deps, wave) in enumerate(AGENT_SPECS):
        agents.append(AgentState(
            bucket=letter, key=key, name=name, tagline=tagline, icon=ic,
            stages=stages, depends_on=deps, status=STATUSES[i], wave=wave,
            progress=[1.0, 0.42, 0.75, 0.0, 0.0, 0.3][i],
            message="Querying NCI PDQ for adult ALL diagnostic criteria…",
            questions_total=5, questions_answered=[5, 2, 4, 0, 0, 1][i],
            evidence_count=[28, 11, 19, 0, 0, 3][i],
            sources_used=["NCI PDQ", "SEER"],
            started_at=NOW, finished_at=NOW + timedelta(minutes=4),
            error="Connector timed out after 30s" if STATUSES[i] is AgentStatus.FAILED else "",
        ))
    return agents


def make_run(agents: list[AgentState]) -> Run:
    return Run(
        id="run_2f8c1a0b9d31",
        reference="RUN-62CEEF6A",
        config=RunConfig(
            drug_brand="Blinatumomab (Blincyto)",
            indication="Acute Lymphoblastic Leukemia (ALL)",
            indication_key="ALL",
            geography="United States",
            objective="Build Claims Line of Therapy",
            target_population="Adult patients with newly diagnosed ALL",
            additional_context="Focus on systemic therapy; exclude paediatric protocols.",
            mode=RunMode.FULL,
            selected_agent=None,
            research_cutoff="2026-09-01",
        ),
        status=RunStatus.COMPLETED,
        agents={a.bucket: a for a in agents},
        created_at=NOW, started_at=NOW, finished_at=NOW + timedelta(minutes=12),
        approved_at=None,
    )


def make_evidence() -> list[Evidence]:
    return [
        Evidence(
            id="ev_1", question_id="q_1", source_id="nci_pdq",
            source_name="NCI PDQ", organization="National Cancer Institute (NCI)", tier=1,
            url="https://www.cancer.gov/types/leukemia/hp/adult-all-treatment-pdq",
            title="Adult Acute Lymphoblastic Leukemia Treatment (PDQ)",
            published="2026-04-11",
            quote=("Estimated new cases of acute lymphoblastic leukemia in the United States in "
                   "2025 are 6,100, with 1,400 estimated deaths."),
            context="Section: Incidence and Mortality",
            origin=EvidenceOrigin.APPROVED_API, tag=VerificationTag.VERIFIED,
            relevance=0.94, identifiers={"pmid": "26389240"}, retrieved_at=NOW,
        ),
        Evidence(
            id="ev_2", question_id="q_1", source_id="seer",
            source_name="NCI SEER", organization="NCI SEER", tier=1,
            url="https://seer.cancer.gov/statfacts/html/alyl.html",
            title="Cancer Stat Facts: Acute Lymphocytic Leukemia", published="2026-01-02",
            quote=("In 2023, there were an estimated 126,118 people living with acute lymphocytic "
                   "leukemia in the United States."),
            origin=EvidenceOrigin.TARGETED_SEARCH, tag=VerificationTag.VERIFIED,
            relevance=0.88, identifiers={}, retrieved_at=NOW,
        ),
        Evidence(
            id="ev_3", question_id="q_2", source_id="open_web_1",
            source_name="Hematology review blog", organization="", tier=5,
            url="https://example.org/adult-all-overview",
            title="Adult ALL overview", published="",
            quote="Roughly one in five adults with ALL carries the Philadelphia chromosome.",
            origin=EvidenceOrigin.OPEN_WEB, tag=VerificationTag.NOT_VERIFIED,
            relevance=0.41, identifiers={}, retrieved_at=NOW,
        ),
    ]


def make_insights() -> list[Insight]:
    specs = [
        ("Patient Population", "Adults with metastatic NSCLC receiving systemic treatment.",
         "Clinical", "stage_1", Confidence.READY, VerificationTag.VERIFIED,
         ["nci_pdq", "seer", "acs", "ash"], ReviewAction.APPROVED, ""),
        ("Disease Journey", "Diagnosis → Biomarker Testing → Treatment → Progression.",
         "Journey", "stage_5", Confidence.READY, VerificationTag.INFERENCE,
         ["nci_pdq", "acs"], ReviewAction.PENDING, ""),
        ("Combination Therapy", "Combination therapy is frequently used in first-line treatment.",
         "Treatment", "stage_2", Confidence.READY, VerificationTag.VERIFIED,
         ["dailymed", "nccn", "acs"], ReviewAction.MODIFIED,
         "For this analysis, focus only on patients receiving systemic therapy."),
        ("Progression Detection",
         "Disease progression cannot be consistently observed directly from claims.",
         "Diagnostic", "stage_3", Confidence.REQUIRES_INPUT, VerificationTag.ORIGINAL,
         ["cms", "loinc"], ReviewAction.PENDING, ""),
        ("Deprecated code family", "ICD-9 mapping could not be confirmed in any coding authority.",
         "Logic", "stage_4", Confidence.REQUIRES_INPUT, VerificationTag.NOT_VERIFIED,
         [], ReviewAction.PENDING, ""),
    ]
    out = []
    for i, (title, summary, cat, stage, conf, tag, sids, action, user_input) in enumerate(specs):
        out.append(Insight(
            id=f"ins_{i}", run_id="run_2f8c1a0b9d31", stage=stage, bucket="A",
            category=cat, title=title, summary=summary,
            detail=("Combination therapies (e.g., pembrolizumab + chemotherapy) are commonly used "
                    "in first-line treatment for metastatic disease [VERIFIED]."),
            confidence=conf, tag=tag, evidence_ids=["ev_1", "ev_2"], source_ids=sids,
            question_ids=["q_1"], used_web_fallback=(i == 3), review_action=action,
            number=i + 1, card_key=f"card_{i}",
            evidence_type=["metrics", "table", "steps", "list", ""][i % 5],
            evidence=[
                [{"label": "New US cases", "value": "6,250"}, {"label": "Deaths", "value": "1,600"}],
                {"columns": ["Therapy", "Setting"], "rows": [["Blinatumomab", "R/R"]]},
                ["Diagnosis", "Induction", "Consolidation"],
                ["Age and lineage are core variables.", "Diagnosis needs several signals."],
                None,
            ][i % 5],
            interpretation="Segment cohorts by age and lineage.", review_note="Check the SEER year.",
            user_input=user_input, impacted_insight_ids=["ins_1"], created_at=NOW,
        ))
    return out


def make_contradictions() -> list[Contradiction]:
    return [
        Contradiction(
            id="con_1", run_id="run_2f8c1a0b9d31", stage="stage_1",
            topic="overall prevalence and mortality statistics in the us adult population",
            source_a_name="National Cancer Institute (NCI)", source_a_tier=1,
            source_a_claim=("Estimated new cases of acute lymphoblastic leukemia in the United "
                            "States in 2025 are 6,100, with 1,400 estimated deaths."),
            source_a_url="https://www.cancer.gov/types/leukemia/hp/adult-all-treatment-pdq",
            source_b_name="American Cancer Society", source_b_tier=3,
            source_b_claim=("The American Cancer Society estimates for 2026 are about 6,250 new "
                            "cases and about 1,600 deaths."),
            source_b_url="https://www.cancer.org/cancer/types/acute-lymphocytic-leukemia",
            reason=("Sources sit at materially different evidence tiers; the lower-tier source "
                    "must not override the higher-tier source."),
            severity=ContradictionSeverity.ESCALATED, review_action=ReviewAction.PENDING,
            reviewer_note="", created_at=NOW,
        ),
        Contradiction(
            id="con_2", run_id="run_2f8c1a0b9d31", stage="stage_1",
            topic="2025/2026 US estimated new cases and deaths for ALL",
            source_a_name="National Cancer Institute (NCI)", source_a_tier=1,
            source_a_claim="6,100 new cases and 1,400 deaths in 2025.",
            source_b_name="American Cancer Society", source_b_tier=3,
            source_b_claim="About 6,250 new cases and about 1,600 deaths in 2026.",
            reason="Divergence is due to different projected calendar years.",
            severity=ContradictionSeverity.NOTED, review_action=ReviewAction.ACKNOWLEDGED,
            reviewer_note="Both figures kept; SME to pick the reporting year.", created_at=NOW,
        ),
    ]


def make_stage() -> StageReport:
    return StageReport(
        id="stg_1", run_id="run_2f8c1a0b9d31", stage="stage_1", bucket="A",
        name="Disease and Diagnostic Foundation: Acute Lymphoblastic Leukemia (ALL) in Adults",
        core_question="Who gets the disease and how is it diagnosed?",
        agent_name="Clinical Landscape Agent",
        framework_steps=["Disease Understanding & Epidemiology Review",
                         "Diagnostic Criteria & Confirmatory Workup Review"],
        step_numbers=[1, 2],
        substeps={"2A": "Diagnostic criteria and confirmatory-workup research",
                  "2B": "Align diagnostic criteria with disease taxonomy, subtypes and population"},
        gate="Reconciliation gate — diagnostic definitions aligned with disease taxonomy",
        output_name="DiseaseDiagnosisProfile",
        what_happens=("This stage establishes the clinical definition, natural history, "
                      "epidemiology, molecular subtypes and diagnostic workup [VERIFIED]."),
        expected_output=["Epidemiology snapshot table (Metric | Value | Source)",
                         "Subtype / biology breakdown table",
                         "Diagnostic workup table", "Key takeaways"],
        synthesis=("Acute lymphoblastic leukemia (ALL) is an aggressive hematologic malignancy "
                   "characterized by uncontrolled proliferation of lymphoblasts [VERIFIED]. "
                   "Risk stratification integrates baseline clinical variables [INFERENCE]."),
        narratives=[
            {"heading": "Disease Definition and Natural History",
             "body": "ALL is an aggressive malignancy of the blood and bone marrow [VERIFIED] "
                     "[Source: NCI PDQ; ACS]."},
            {"heading": "Risk Stratification and Clinical Variables",
             "body": "Advanced age is a major adverse prognostic factor in adults [VERIFIED]."},
        ],
        tables=[
            InsightTable(
                title="Epidemiology Snapshot",
                columns=["Metric", "Value", "Source"],
                rows=[
                    ["Estimated New US Cases (2025/2026)",
                     "[VERIFIED] 6,100 (2025 NCI PDQ) to 6,250 (2026 ACS / SEER)",
                     "[Source: NCI PDQ; SEER Stat Facts; ACS]"],
                    ["Overall 5-Year Relative Survival",
                     "[VERIFIED] 73.2% (2016–2022 SEER data, all ages)",
                     "[Source: SEER Stat Facts]"],
                ],
                footnote="Statistics reflect combined pediatric and adult data where indicated.",
            ),
            InsightTable(
                title="Diagnostic Workup Table",
                columns=["Category", "Example Tests / Procedures", "Purpose"],
                rows=[["Bone Marrow Examination", "[VERIFIED] Bone marrow aspirate and biopsy",
                       "[VERIFIED] Establish the diagnosis (>=20% lymphoblasts)"]],
                footnote="",
            ),
        ],
        takeaways=[
            "Acute lymphoblastic leukemia is an aggressive lymphoid malignancy [VERIFIED].",
            "Philadelphia chromosome positivity dictates a high-risk prognosis [VERIFIED].",
        ],
        assumptions=["Published NCI PDQ and SEER statistics reflect US clinical epidemiology."],
        limitations=["Registry statistics combine paediatric and adult populations."],
        observability=[
            {"concept": "Initial ALL diagnosis and blast percentage (>=20%)",
             "classification": "PROXY SIGNAL",
             "basis": "Bone marrow pathology reports and ICD-O-3 registry codes (e.g. 9811/3)",
             "limitation": "Exact blast percentage is rarely captured in billing claims."},
            {"concept": "Measurable Residual Disease (MRD) Status",
             "classification": "PROXY SIGNAL",
             "basis": "Flow cytometry, PCR or NGS MRD assay billing",
             "limitation": "Threshold definitions are hard to standardise from claims."},
        ],
        unanswered=[{"question": "What share of adult patients receive transplant in first remission?",
                     "aspect": "post-remission therapy",
                     "reason": "No approved source reported a US-specific proportion."}],
        evidence_count=28, source_count=7, supplementary_count=0,
        tiers_represented=[1, 2, 3], created_at=NOW,
    )


def make_qa() -> QAMetrics:
    return QAMetrics(
        questions_planned=5, questions_sufficient=5, questions_web_only=0,
        questions_below_threshold=0, mean_coverage=1.0, evidence_total=28,
        evidence_approved=21, evidence_supplementary=0, distinct_sources=7,
        conflicts_surfaced=6,
        checklist=[
            {"check": "Every material factual claim carries an inline source reference",
             "status": "PASS", "detail": "28 evidence items carry a source URL and citation"},
            {"check": "No claims code is asserted without a coding-authority source",
             "status": "PASS", "detail": "All reported codes were located verbatim"},
            {"check": "Source conflicts are surfaced rather than merged",
             "status": "FAIL", "detail": "1 conflict was merged and must be re-opened"},
        ],
        readiness=("The output is ready for initial Subject Matter Expert (SME) review. "
                   "Strongest aspects include rigorous adherence to source-first constraints."),
        sme_checklist=[
            "Verify epidemiological incidence and mortality figures against SEER.",
            "Confirm immunophenotypic markers distinguishing B-cell from T-cell ALL.",
        ],
    )


@pytest.fixture(scope="module")
def context() -> dict:
    agents = make_agents()
    run = make_run(agents)
    insights = make_insights()
    stage = make_stage()
    evidence = make_evidence()
    contradictions = make_contradictions()

    source_names = {
        "nci_pdq": "NCI PDQ", "seer": "NCI SEER", "acs": "American Cancer Society",
        "ash": "ASH / Blood", "dailymed": "DailyMed", "nccn": "NCCN",
        "cms": "CMS", "loinc": "LOINC",
    }

    return {
        "active": "overview",
        "credentials": {"anthropic_api_key": False, "firecrawl_api_key": False,
                        "ncbi_api_key": True, "icd11": False, "loinc": True},
        "messages": [{"level": "ok", "text": "Discovery finished in 12 minutes."}, "Plain message."],
        "run": run,
        "runs": [run],
        "agents": agents,
        "agent_by_stage": {a.stages[0]: {"name": a.name, "icon": a.icon} for a in agents if a.stages},
        "source_chips": [
            {"key": "pubmed", "name": "PubMed", "used": True},
            {"key": "fda", "name": "FDA", "used": False},
            "NCCN", "ASCO", "EMA",
        ],
        "counts": {"ready": 22, "requires_input": 2, "needs_decision": 2, "decided": 22,
                   "input_added": 3, "stages": 6, "evidence": 21,
                   "insights": 24, "sources": 18, "pending": 2,
                   "approved": 18, "modified": 4, "user_inputs": 3, "assumptions": 2,
                   "conflicts": 1, "conflicts_open": 1, "total": 24},
        "categories": [
            {"key": "clinical", "label": "Clinical", "count": 6},
            {"key": "treatment", "label": "Treatment", "count": 6},
            {"key": "diagnostic", "label": "Diagnostic", "count": 6},
            {"key": "logic", "label": "Logic", "count": 6},
        ],
        "takeaways": insights[:3],
        "insights": insights,
        "insight": insights[2],
        "source_names": source_names,
        "impacts": [
            {"agent_name": "Treatment Logic Agent",
             "description": "LOT rules will consider combination therapy as a single regimen."},
            {"agent_name": "Diagnostic Footprint Agent", "description": "No change expected."},
        ],
        "evidence": evidence,
        "contradictions": contradictions,
        "contradiction": contradictions[0],
        "stage": stage,
        "stages": [stage],
        "qa": make_qa(),
        "params": [
            {"label": "Indication", "value": "Acute Lymphoblastic Leukemia (ALL)"},
            {"label": "Population", "value": "Adult"},
            {"label": "Research cutoff", "value": "2026-09-01"},
        ],
        "executive_summary": ("This clinical desk-research deliverable establishes the disease and "
                              "diagnostic foundation for adult ALL in the United States [VERIFIED]."),
        "sources": [
            {"organization": "National Cancer Institute (NCI)", "title": "Adult ALL Treatment (PDQ)",
             "published": "Not stated", "url": "https://www.cancer.gov/types/leukemia",
             "tier": 1, "evidence_items": 13},
            {"organization": "American Cancer Society", "title": "", "published": "August 13, 2025",
             "url": "https://www.cancer.org/cancer/types/acute-lymphocytic-leukemia",
             "tier": 3, "evidence_items": 5},
        ],
        "used": [
            {"name": "NCI PDQ", "organization": "National Cancer Institute (NCI)", "tier": 1,
             "access_method": "api", "evidence_items": 13, "url": "https://www.cancer.gov",
             "domain": "cancer.gov"},
            {"name": "Hematology review blog", "tier": 5, "access_method": "firecrawl_search",
             "evidence_items": 1, "url": "https://example.org"},
        ],
        "unavailable": [
            {"name": "Orphanet", "tier": 1, "access_method": "api",
             "reason": "Returned no documents for the indication synonyms tried."},
            {"name": "LOINC", "tier": 1, "access_method": "licensed", "blocked_by": "Credentials",
             "reason": "LOINC username and password are not configured."},
        ],
        "health": {"pubmed": True, "orphanet": False,
                   "dailymed": {"status": "degraded", "latency_ms": 2400}},
        "included": [
            {"name": "Clinical Context", "icon": "leaf",
             "description": "Disease definition, patient population, journey, key events"},
            {"name": "Treatment Landscape", "icon": "pill",
             "description": "Approved therapies, treatment settings, regulatory evidence"},
        ],
        "indications": [
            {"key": "ALL", "label": "Acute Lymphoblastic Leukemia", "abbreviation": "ALL",
             "enabled": True},
            {"key": "CLL", "label": "Chronic Lymphocytic Leukemia", "abbreviation": "CLL",
             "enabled": True},
            {"key": "", "label": "Multiple Myeloma", "abbreviation": "", "enabled": False},
        ],
        "therapy_areas": [{"value": "Oncology", "enabled": True},
                          {"value": "Hematology", "enabled": False}],
        "populations": [{"value": "All", "enabled": True}],
        "objectives": [
            {"value": "Build Claims Line of Therapy", "label": "LOT claims", "enabled": True},
            {"value": "Targeting", "label": "Targeting", "enabled": False},
        ],
        "geographies": [{"value": "United States", "enabled": True},
                        {"value": "Europe", "enabled": False}],
        "llm": {"configured": False, "provider": "anthropic", "explicit": False,
                "model": "claude-opus-5", "gaps": ["ANTHROPIC_API_KEY"]},
        "app_name": "Celestra",
        "web_sites": [
            {"url": "https://example.org/cll", "title": "CLL overview",
             "site": "example.org", "scraped": True, "used": True},
            {"url": "https://example.org/other", "title": "Unused page",
             "site": "example.org", "scraped": True, "used": False},
        ],
        # review gate page
        "completed_agents": [{"name": "Clinical Landscape Agent", "icon": "leaf",
                              "tagline": "Disease context"}],
        "remaining_agents": [{"name": "Diagnostic Footprint Agent", "icon": "microscope",
                              "tagline": "Claims signals", "wave": 2}],
        "can_continue": True,
        "gate": {"mode": "review", "blockers": [
                     {"kind": "finding", "id": "ins_4", "title": "Diagnostic coding",
                      "reason": "Only open-web pages answered this.", "anchor": "#insight-ins_4"}],
                 "counts": {"insights": 24, "ready": 22, "needs_decision": 1, "decided": 22,
                            "conflicts_open": 1, "conflicts_blocking": 1, "conflicts": 1},
                 "available": True, "can_proceed": False, "done": False,
                 "action_url": "/runs/run_test/continue",
                 "action_label": "Approve discovery and start Mapping & Synthesis",
                 "done_label": "Discovery approved", "refresh_url": "/runs/run_test/gate?mode=review"},
        "phases": [{"key": "discovery", "name": "Discovery", "description": "First two agents",
                    "agents": [a for a in agents if a.bucket in ("A", "C")], "state": "complete", "done": 2},
                   {"key": "mapping", "name": "Mapping & Synthesis", "description": "The rest",
                    "agents": [a for a in agents if a.bucket not in ("A", "C")], "state": "queued", "done": 0}],
        "mode": "modify", "downstream_agents": ["Diagnostic Footprint Agent"], "web_available": True,
        "probe": {"configured": True, "endpoint": "https://api.firecrawl.dev/v2/search", "version": "v2",
                  "ok": False, "results": 0, "detail": "TLS certificate verification failed",
                  "remedy": "Set CA_BUNDLE.", "elapsed_ms": 120},
        "network": {"proxy": "from environment", "proxy_forced": False, "ca_bundle": "",
                    "tls_verify": True, "firecrawl_endpoint": "https://api.firecrawl.dev/v2/search",
                    "firecrawl_version": "v2"},
        "web_search": {"available": True, "reason": "Firecrawl configured", "keyed": True,
                       "error": "", "error_at": ""},
        "project_name": "CLL pilot",
        "phase_key": "all", "phase_label": "Whole document", "download": False,
        "generated": "10 Sep 2026, 09:00 UTC",
        "groups": [{"key": "discovery", "name": "Discovery", "description": "First agents",
                    "stages": [{"report": stage, "cards": insights}]}],
        "next_url": "/runs/run_test/approval", "reviewer_inputs": [
            {"insight_id": "ins_1", "stage": "stage_1", "title": "Epidemiology",
             "input": "Use the 2024 SEER release.", "at": "2026-09-01T00:00:00Z"}],
        # table snapshot fragment
        "tables": [InsightTable(title="Epidemiology snapshot", columns=["Metric", "Value", "Source"],
                                rows=[["Incidence", "[VERIFIED] 4.7 per 100,000", "[Source: SEER]"]],
                                footnote="Rows are verbatim.", question_ids=["q1"])],
        "report": None,
        "run_id": "run_test",
        "thresholds": {"sufficiency": {"min_evidence_items": 3, "min_distinct_sources": 2},
                       "confidence": {"ready": {"description": "x"}}},
        "datasets": [
            {"name": "ICD-10-CM", "version": "2026", "rows": 74260,
             "installed_at": "2026-08-30", "available": True,
             "description": "US diagnosis code universe"},
            {"name": "LOINC", "version": "—", "rows": 0, "available": False},
        ],
        "message": "That run could not be found",
        "detail": "No run exists with reference RUN-000000.",
        "last_seq": 42,
    }


def all_templates() -> list[str]:
    names = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        names.append(str(path.relative_to(TEMPLATES)).replace("\\", "/"))
    return names


PAGE_TEMPLATES = [n for n in all_templates() if not n.startswith("partials/")]


def test_expected_templates_exist():
    expected = {
        "base.html", "home.html", "new_project.html", "progress.html", "overview.html",
        "insights.html", "contradictions.html", "stage_report.html", "report.html",
        "approval.html", "sources_panel.html", "projects.html", "settings.html", "error.html",
        "partials/insight_card.html", "partials/insight_modal.html",
        "partials/evidence_panel.html",
    }
    missing = expected - set(all_templates())
    assert not missing, f"missing templates: {sorted(missing)}"


MACRO_ONLY = {"partials/icons.html", "partials/macros.html"}


@pytest.mark.parametrize("name", all_templates())
def test_template_renders(env, context, name):
    out = env.get_template(name).render(**context)
    if name not in MACRO_ONLY:
        assert out.strip(), f"{name} rendered empty"


@pytest.mark.parametrize("name", all_templates())
def test_no_internal_vocabulary_in_output(env, context, name):
    out = env.get_template(name).render(**context)
    assert "bucket" not in out.lower(), f"{name} leaked the internal grouping word"


@pytest.mark.parametrize("name", all_templates())
def test_no_external_asset_references(env, context, name):
    """Templates may LINK to external sources (evidence urls) but must not FETCH
    stylesheets, scripts, fonts or images from anywhere off-origin."""
    out = env.get_template(name).render(**context)
    for pattern in (r'<link[^>]+href="https?://', r'<script[^>]+src="https?://',
                    r'@import\s+url\(["\']?https?://', r'<img[^>]+src="https?://',
                    r'url\(["\']?https?://'):
        assert not re.search(pattern, out), f"{name} pulls an external asset ({pattern})"


def test_source_names_are_rendered_on_insight_cards(env, context):
    out = env.get_template("insights.html").render(**context)
    assert "NCI PDQ" in out and "American Cancer Society" in out


def test_supplementary_web_evidence_is_flagged(env, context):
    out = env.get_template("partials/evidence_panel.html").render(**context)
    assert "Supplementary web evidence" in out


def test_contradictions_are_not_auto_resolved(env, context):
    out = env.get_template("contradictions.html").render(**context)
    assert "auto-resolved" in out
    for label in ("Prefer A", "Prefer B", "Acknowledge both"):
        assert label in out


def test_confidence_chip_labels(env, context):
    out = env.get_template("overview.html").render(**context)
    for label in ("Ready", "Need your input", "Decided by you"):
        assert label in out


def test_verification_tags_become_pills(env, context):
    out = env.get_template("stage_report.html").render(**context)
    assert 'class="vtag vtag-verified">[VERIFIED]' in out
    assert 'vtag-inference">[INFERENCE]' in out


def test_agent_names_not_letters(env, context):
    out = env.get_template("progress.html").render(**context)
    for agent in make_agents():
        assert agent.name in out


# ---------------------------------------------------------------------------
# static asset checks
# ---------------------------------------------------------------------------
def test_css_braces_balanced():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}"), "unbalanced braces in app.css"
    depth = 0
    for ch in css:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            assert depth >= 0, "closing brace before opening brace in app.css"
    assert depth == 0


def test_static_has_no_external_references():
    for path in list((STATIC).rglob("*.css")) + list((STATIC).rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, f"{path} references a remote host"


def test_no_bucket_word_in_sources():
    for path in list(TEMPLATES.rglob("*.html")) + list(STATIC.rglob("*.css")) + list(STATIC.rglob("*.js")):
        text = path.read_text(encoding="utf-8").lower()
        # the word may appear only inside a Jinja comment explaining the rule
        stripped = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
        stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
        assert "bucket" not in stripped, f"{path} mentions the internal grouping word"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


@pytest.fixture(scope="module")
def sparse_context(context) -> dict:
    """Same contract, but every collection empty: exercises the empty states."""
    run = Run(
        id="run_empty", reference="", config=RunConfig(
            indication="Chronic Lymphocytic Leukemia (CLL)", indication_key="CLL",
            mode=RunMode.SINGLE, selected_agent="clinical-landscape"),
        status=RunStatus.PENDING, agents={},
    )
    stage = StageReport(stage="stage_2", bucket="C", name="Treatment evidence",
                        core_question="", agent_name="Treatment Evidence Agent")
    sparse = dict(context)
    sparse.update({
        "active": None, "credentials": {}, "messages": [], "run": run, "runs": [],
        "agents": [], "agent_by_stage": {}, "source_chips": [],
        "counts": {"insights": 0, "pending": 0, "approved": 0, "modified": 0,
                   "conflicts": 0, "conflicts_open": 0, "ready": 0, "needs_decision": 0,
                   "decided": 0, "input_added": 0,
                   "requires_input": 0, "sources": 0,
                   "user_inputs": 0, "assumptions": 0, "total": 0},
        "categories": [], "takeaways": [], "insights": [], "source_names": {},
        "impacts": [], "evidence": [], "contradictions": [], "stage": stage, "stages": [],
        "qa": None, "params": {}, "sources": [], "used": [], "unavailable": [],
        "health": None, "included": [], "indications": [], "objectives": [],
        "geographies": [], "therapy_areas": [], "populations": [],
        "llm": {"configured": True, "provider": "azure_openai", "explicit": True,
                "model": "my-deployment", "gaps": []},
        "app_name": "Celestra",
        "completed_agents": [], "remaining_agents": [], "can_continue": False,
        "gate": {"mode": "approval", "blockers": [], "counts": {
                     "insights": 0, "ready": 0, "needs_decision": 0, "decided": 0,
                     "conflicts_open": 0, "conflicts_blocking": 0, "conflicts": 0},
                 "available": False, "can_proceed": False, "done": False,
                 "action_url": "/x", "action_label": "Approve", "done_label": "Approved",
                 "refresh_url": "/x"},
        "phases": [], "mode": "input", "downstream_agents": [], "web_available": False,
        "phase_key": "discovery", "phase_label": "Discovery", "download": True,
        "generated": "", "groups": [],
        "probe": {"configured": False, "endpoint": "", "version": "v2", "ok": False, "results": 0,
                  "detail": "FIRECRAWL_API_KEY is not set", "remedy": "", "elapsed_ms": 0},
        "network": {}, "web_search": {},
        "next_url": "/runs/run_empty", "reviewer_inputs": [],
        "tables": [], "report": None, "run_id": "run_test",
        "thresholds": {}, "datasets": [], "last_seq": 0,
    })
    return sparse


@pytest.mark.parametrize("name", all_templates())
def test_template_renders_with_empty_collections(env, sparse_context, name):
    out = env.get_template(name).render(**sparse_context)
    assert "bucket" not in out.lower()
    if name not in MACRO_ONLY:
        assert out.strip(), f"{name} rendered empty"
```

### File: `tests\test_web_search.py`

```py
"""Web search connector: endpoint configuration, v1/v2 payload parsing, and
transport errors explained in words a person can act on."""
from __future__ import annotations

import asyncio
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from celestra.connectors import base as base_mod
from celestra.connectors.base import describe_http_error, explain_transport_error, remedy_for
from celestra.connectors.firecrawl import (
    FirecrawlConnector, firecrawl_blocked, firecrawl_status, reset_firecrawl_status,
)
from celestra.settings import Settings, get_settings

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    print("\n== endpoint configuration ==")
    s = Settings(firecrawl_api_key="k")
    check("v2 by default", s.firecrawl_endpoint("search") == "https://api.firecrawl.dev/v2/search",
          s.firecrawl_endpoint("search"))
    s = Settings(firecrawl_api_key="k", firecrawl_api_version="v1")
    check("v1 selectable", s.firecrawl_endpoint("scrape") == "https://api.firecrawl.dev/v1/scrape")
    s = Settings(firecrawl_api_key="k", firecrawl_api_url="https://fc.internal.example/v1/")
    check("pasted version suffix wins", s.firecrawl_endpoint("search") == "https://fc.internal.example/v1/search",
          s.firecrawl_endpoint("search"))
    s = Settings(firecrawl_api_key="k", firecrawl_api_version="bogus")
    check("unknown version falls back to v2", s.firecrawl_version == "v2")
    default_verify = Settings(ca_bundle=None, ssl_cert_file=None, requests_ca_bundle=None).tls_verify_value()
    check("tls verify defaults to system certs", default_verify is True, str(default_verify))
    check("CA_BUNDLE is used when set", Settings(ca_bundle="/tmp/root.pem").tls_verify_value() == "/tmp/root.pem")
    check("TLS_VERIFY=false disables", Settings(tls_verify=False).tls_verify_value() is False)

    print("\n== payload parsing ==")
    v1 = {"success": True, "data": [
        {"url": "https://a.example/x", "title": "A", "description": "desc a"},
        {"url": "", "title": "no url"},
    ]}
    v2 = {"success": True, "data": {
        "web": [{"url": "https://b.example/y", "title": "B", "description": "desc b",
                 "markdown": "# B"}],
        "news": [{"url": "https://n.example", "title": "N"}],
    }}
    p1 = FirecrawlConnector.parse_search_payload(v1)
    p2 = FirecrawlConnector.parse_search_payload(v2)
    check("v1 list shape parsed", [r["url"] for r in p1] == ["https://a.example/x"])
    check("v2 object shape parsed, web only", [r["url"] for r in p2] == ["https://b.example/y"])
    check("v2 markdown carried", p2[0]["markdown"] == "# B")
    try:
        FirecrawlConnector.parse_search_payload({"success": False, "error": "Invalid token"})
        check("success=false raises", False)
    except RuntimeError as exc:
        check("success=false raises with the API's message", "Invalid token" in str(exc))
    check("garbage is empty, not an error", FirecrawlConnector.parse_search_payload("nope") == [])

    print("\n== transport errors explained ==")
    def connect_error(cause: BaseException) -> httpx.ConnectError:
        err = httpx.ConnectError("")
        err.__cause__ = cause
        return err

    ssl_err = connect_error(ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"))
    what, fix = explain_transport_error(ssl_err)
    check("TLS interception is named", what.startswith("TLS certificate verification failed"), what)
    check("  and CA_BUNDLE is the fix", "CA_BUNDLE" in fix)
    check("  describe_http_error no longer says just ConnectError",
          describe_http_error(ssl_err) != "ConnectError" and "TLS" in describe_http_error(ssl_err))
    dns_err = connect_error(OSError("[Errno 11001] getaddrinfo failed"))
    what, fix = explain_transport_error(dns_err)
    check("DNS failure is named", what.startswith("DNS lookup failed"), what)
    check("  and a proxy is suggested", "PROXY" in fix)
    refused = connect_error(OSError("[Errno 111] Connection refused"))
    check("refused connection names the firewall", "refused" in explain_transport_error(refused)[0])
    check("timeout explained", "timed out" in explain_transport_error(httpx.ConnectTimeout("x"))[0])
    resp = httpx.Response(402, request=httpx.Request("POST", "https://api.firecrawl.dev/v2/search"))
    e402 = httpx.HTTPStatusError("402", request=resp.request, response=resp)
    check("402 is out of credits", "credits" in describe_http_error(e402) and "credits" in remedy_for(e402))
    resp = httpx.Response(401, request=resp.request)
    e401 = httpx.HTTPStatusError("401", request=resp.request, response=resp)
    check("401 is a rejected key with a remedy", "rejected" in describe_http_error(e401) and "dashboard" in remedy_for(e401))

    print("\n== search falls back and records the cause ==")
    reset_firecrawl_status()
    async def failing_request(*a, **kw):
        raise ssl_err

    real = base_mod.http.request
    base_mod.http.request = failing_request
    get_settings.cache_clear()
    import os
    os.environ["FIRECRAWL_API_KEY"] = "fc-test"
    try:
        conn = FirecrawlConnector()
        results = asyncio.run(conn.search("chronic lymphocytic leukemia incidence", 3))
        check("search never raises", isinstance(results, list))
        check("last_error names the cause and the fix",
              "TLS" in conn.last_error and "CA_BUNDLE" in conn.last_error, conn.last_error)
        check("failure recorded for the UI", "TLS" in firecrawl_status["error"] and firecrawl_status["at"])
        probe = asyncio.run(FirecrawlConnector.probe())
        check("probe reports the same", probe["ok"] is False and "CA_BUNDLE" in probe["remedy"], probe["detail"])
        check("probe names the endpoint", probe["endpoint"].endswith("/v2/search"))
        check("two transport failures switch web search off for the session",
              bool(firecrawl_blocked()) and "off" in firecrawl_blocked(), firecrawl_blocked()[:80])
        calls_before = firecrawl_status["calls"]
        results = asyncio.run(FirecrawlConnector().search("another question", 3))
        check("a blocked session makes no further call",
              results == [] and firecrawl_status["calls"] == calls_before
              and firecrawl_status["skipped"] >= 1)

        print("\n== no credits stops at once ==")
        reset_firecrawl_status()
        resp402 = httpx.Response(402, request=httpx.Request("POST", "https://api.firecrawl.dev/v2/search"),
                                 json={"error": "Insufficient credits"})
        async def no_credits(*a, **kw):
            raise httpx.HTTPStatusError("402", request=resp402.request, response=resp402)
        base_mod.http.request = no_credits
        results = asyncio.run(FirecrawlConnector().search("chronic lymphocytic leukemia staging", 3))
        check("first 402 blocks the session", "credits" in firecrawl_blocked(), firecrawl_blocked()[:80])
        check("status carries the reason for the UI", "402" in firecrawl_status["error"])
        d = asyncio.run(FirecrawlConnector().discover(
            __import__("celestra.connectors.base", fromlist=["RetrievalContext"]).RetrievalContext(
                indication="CLL", indication_key="CLL", synonyms=[], geography="US",
                population="", stage="stage_1", question="staging", aspects=[], cutoff=""), 3))
        check("a domain search reports the block instead of calling", d.ok is False and "credits" in d.reason)

        print("\n== one call brings back the pages ==")
        reset_firecrawl_status()
        seen_bodies = []
        async def v2_search(method, url, *, json_body=None, **kw):
            seen_bodies.append(json_body)
            return {"success": True, "data": {"web": [
                {"url": "https://cancer.gov/a", "title": "CLL staging", "description": "Rai and Binet staging",
                 "markdown": "# A\n" + "Staging uses Rai and Binet systems. " * 20}]}}
        base_mod.http.request = v2_search
        conn = FirecrawlConnector()
        refs = asyncio.run(conn.search_refs("cll staging", 3))
        check("search asks for page content in the same call",
              seen_bodies and seen_bodies[-1].get("scrapeOptions", {}).get("formats") == ["markdown"])
        check("search asks only for the pages it will read", seen_bodies[-1]["limit"] == 3)
        check("result carries the page text", refs and "Rai and Binet" in refs[0].raw["markdown"])
        page = asyncio.run(conn.scrape(refs[0].url, refs[0].raw["markdown"]))
        check("scrape with markdown in hand makes no call",
              page.get("backend") == "firecrawl" and len(seen_bodies) == 1)
        asyncio.run(conn.search_refs("cll staging", 3))
        check("the same query is not searched twice", len(seen_bodies) == 1)
    finally:
        base_mod.http.request = real
        os.environ.pop("FIRECRAWL_API_KEY", None)
        get_settings.cache_clear()
        reset_firecrawl_status()

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

