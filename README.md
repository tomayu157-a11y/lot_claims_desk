# Celestra — Clinical Desk Research

Answers a fixed set of clinical research questions for an indication, from a
registry of approved sources, and renders the result in the browser with
per-claim provenance. Built for US claims line-of-therapy work.

The unit of work is an **agent**. Seven of them cover the 15-step Phase 1
Clinical Foundation framework, and they execute by dependency, not in stage
order.

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python run.py --setup              # creates .env, checks the install
python run.py --demo               # seeds a run so the UI has content
python run.py                      # start the server
```

Then open the URL the banner prints, normally <http://localhost:8000>.

Two things that look like a broken app but are not:

- **Do not open `0.0.0.0:8000`.** That is the bind address, not a reachable
  one. Use `localhost`.
- **If the banner warns that the port is in use**, an older server is still
  running and your browser is probably hitting it. Open the port the banner
  names, or stop the old process first:

  ```bash
  lsof -ti:8000 | xargs kill                  # macOS/Linux
  netstat -ano | findstr :8000                # Windows, then taskkill /PID <pid> /F
  ```

`python run.py --check` renders the real pages and reports what is wrong if
anything is, which is the fastest way to tell a stale checkout from a
misaddressed browser. It prints the git revision, as does the startup banner.

### Commands

| Command | Does |
|---|---|
| `python run.py` | start the server |
| `python run.py --setup` | check dependencies, create `.env`, prepare data directories |
| `python run.py --demo` | seed a completed run offline, no network or credentials |
| `python run.py --check` | verify the app can serve its pages, print the revision |

### The demo run

`--demo` drives the real pipeline through bundled fixtures, so what you see is
the actual orchestrator, scoring, contradiction detection and report rendering
rather than canned screenshots. It needs no network and no API keys. Some
questions deliberately stay unanswered, which is what exercises the
below-threshold and blocked-source paths in the UI.

For a live run, use **New Project** in the app. That queries the real sources
and takes roughly one to three minutes for all agents.

### Tests

```bash
pip install -r requirements-dev.txt
python tests/test_pipeline.py          # orchestration, thresholds, QA, events
python tests/test_llm_providers.py     # provider dispatch
python tests/test_smoke_http.py        # every route and interaction
python -m pytest tests/test_templates_render.py -q
```

## Hosting on Render, and moving projects between instances

Projects live in one SQLite file under the data directory. On Render the
filesystem is wiped on every deploy, so attach a **persistent disk** to the
service and point the app at it:

1. Render dashboard → your web service → Disks → Add disk, mount path
   `/var/data` (1 GB is plenty).
2. Environment → add `CELESTRA_DATA_DIR=/var/data`.
3. Redeploy. The database, HTTP cache and reference files now survive deploys.

To load projects you ran locally onto the hosted version: on your laptop open
Projects and press **Export** on a project (a JSON file downloads), then on
the hosted Projects page open **Import a project** and upload that file. The
run opens at the step it was on. Importing the same file again overwrites the
copy on the host. The reverse direction works the same way.

## Production

```bash
uvicorn celestra.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Use one worker. Runs execute in-process and the event bus is per-process, so a
second worker would split a run's live stream. Scaling out means moving the bus
to Redis.

## Two modes

**Run all agents.** Every agent, in one linear flow with a human gate in the
middle and a sign-off at the end:

```
1  Discovery            Clinical Landscape + Treatment Evidence run in parallel
2  Review gate          you decide the discovery findings before they propagate
3  Mapping & Synthesis  Diagnostic Footprint, Treatment Logic, Patient Journey,
                        Information Synthesis run by dependency wave
4  Final approval       anything still needing input is listed; approve to lock
5  Approved document    the signed-off research document
```

`/runs/{id}` always sends you to the step the run is on. The live page shows
the agents grouped by phase and stays viewable after they finish.

At both gates the rule is the same: every finding marked **Requires Input** and
every escalated source conflict needs a decision before you can continue;
findings marked **Ready** carry forward as generated unless you change them.
A finding takes one of three decisions:

- **Approve** accepts it as written.
- **Modify** sends your instruction to the model, which re-reads the held
  evidence, searches the web if that is not enough, and rewrites the finding
  and its answer in the document.
- **Add Input** attaches your own knowledge. It is not rewritten; it is printed
  with the finding in the document and handed to every agent that runs after
  the gate.

Approval locks the run: no further edits are accepted, the QA checklist records
the sign-off, and the document is marked Approved.

**Run a single agent.** One agent on its own, for when you only need that
output. Dependencies outside the selection are ignored rather than forcing a
full run.

## How a question gets answered

Getting an answer is the priority, so retrieval escalates rather than giving up:

1. **Approved sources first.** Every source registered for that stage and
   indication is queried in parallel through its own adapter.
2. **Refine and retry.** If the result misses the sufficiency threshold, the
   query is rewritten and retried, twice by default.
3. **Open-web fallback.** Still short, and the run falls back to web search and
   page scraping.

Fallback evidence is never laundered into looking primary. It is tier 5, it
carries `EvidenceOrigin.OPEN_WEB`, and it renders as **SUPPLEMENTARY WEB
EVIDENCE** everywhere it appears. A question answered only from the open web can
never reach high confidence.

Every finding in the UI names the sources behind it, and the sources panel
additionally lists what was attempted and returned nothing, with the reason.

## Thresholds

All of it is `celestra/config/thresholds.yaml`; none of it is hardcoded.

| Setting | Default | Meaning |
|---|---|---|
| `min_evidence_items` | 3 | usable quotes needed |
| `min_distinct_sources` | 2 | from different sources |
| `min_primary_tier_items` | 1 | at least one at tier 2 or better |
| `min_coverage_score` | 0.60 | weighted aspect + tier + volume score |
| `min_quote_length` | 40 | shorter quotes do not count as support |
| `max_refinement_rounds` | 2 | query rewrites before fallback |
| `escalate_on_tier_gap` | 2 | tier distance that escalates a conflict |

Every finding is in exactly one of two states. **Ready**: a tier 1 or 2 source
answered it and no conflict is open. **Requires Input**: a person has to act,
because nothing usable was found, only the open web answered it, or an
escalated source conflict is undecided. The card says which. A question that
fails every strategy is reported as unanswered with the reason and the sources
tried, rather than being quietly dropped.

## Contradictions

Conflicts between sources are surfaced and never resolved. Both sides are shown
as their source stated them, with tier badges, and a human decides. Conflicts
are deduplicated on the claim pair so one disagreement is one decision.

## Configuration is data

| File | Holds |
|---|---|
| `config/framework.yaml` | 15 steps, dependency graph, agent display names |
| `config/research_questions.yaml` | 62 seed questions across ALL and CLL |
| `config/sources.yaml` | 35 sources with tier, stage and access method |
| `config/thresholds.yaml` | every sufficiency and escalation limit |

Adding an indication is a YAML edit. Adding a source is a YAML entry plus one
adapter.

## LLM provider

Pick one in `.env`. Without any of them the app still runs end to end, with
planning, ranking, extraction and synthesis on the deterministic engine: real
evidence and real citations, weaker prose. The UI says which mode it is in.

| `LLM_PROVIDER` | For | Needs |
|---|---|---|
| `anthropic` | Claude via the Anthropic API | `ANTHROPIC_API_KEY` |
| `anthropic_foundry` | Claude deployed on Microsoft Foundry | `FOUNDRY_API_KEY`, `FOUNDRY_RESOURCE` |
| `azure_openai` | A model deployed in Azure AI Foundry / Azure OpenAI | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT` |

For Azure, `AZURE_OPENAI_DEPLOYMENT` is the name you gave the deployment, not
the underlying model name. Settings and `/healthz` report the active provider
and name any setting it is still missing.

## Other credentials

| Key | Absent means |
|---|---|
| `FIRECRAWL_API_KEY` | open-web fallback uses a keyless search path, which several networks block outright |

**Web search failing with a connection error.** `python run.py --check`, or
the "Test web search now" button on the Settings page, makes one real
Firecrawl call and prints the cause and the fix. The usual ones are a corporate
proxy (set `HTTPS_PROXY` or `PROXY_URL`), a TLS-intercepting proxy (export its
root certificate as PEM and set `CA_BUNDLE`), or DNS with no route out. The
Firecrawl endpoint is `FIRECRAWL_API_URL` + `FIRECRAWL_API_VERSION` (v2 by
default; v1 still accepted). While a configured key is failing, every page
shows a banner saying so, and the fallback uses the keyless path.
| `NCBI_API_KEY` | E-utilities limited to 3 requests/second instead of 10 |
| `ICD11_CLIENT_ID` / `SECRET` | ICD-11 codes unavailable; those questions report the blocker |
| `LOINC_USERNAME` / `PASSWORD` | LOINC search unavailable; the free NLM tables still answer most of it |

## Known source limits

These are properties of the sources, not bugs, and the app reports them rather
than guessing around them.

- **CPT** is licensed by the AMA. No anonymous full-code API exists and the
  pages must not be scraped.
- **NCCN** guideline content is licensed. The run substitutes NCI PDQ, iwCLL,
  ASH and the current European guideline, and says so.
- **ICD-10-CM, HCPCS, GEMs and the Purple Book** are distributed as files. Drop
  them in `celestra/data/reference/`; see the README there.
- **DailyMed's `search_string` parameter is silently ignored** and returns the
  entire catalogue. The adapter uses `drug_name` and `application_number`.
- **SEER statistics are not in `api.seer.cancer.gov`.** The Stat Facts pages are
  scraped.
- **CDC WONDER is POST-only** and needs an ICD-10 code family, which the code
  stage supplies.

## Tests

```bash
python tests/test_pipeline.py        # orchestration, thresholds, QA, events
python tests/test_connectors_live.py # every adapter against its live endpoint
python tests/test_templates_render.py
```

## Layout

```
celestra/
  config/       four YAML files; the whole behaviour surface
  connectors/   one adapter per source, plus the registry
  services/     planner, retrieval, extraction, scoring,
                contradictions, synthesis, qa, orchestrator, llm
  templates/    Jinja2, no CDN, no framework
  static/       handwritten CSS and vanilla JS
  models.py     shared contracts
  store.py      SQLite document store
  events.py     SSE bus
  main.py       FastAPI routes
```
