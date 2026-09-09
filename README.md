# Celestra — Clinical Desk Research

Answers a fixed set of clinical research questions for an indication, from a
registry of approved sources, and renders the result in the browser with
per-claim provenance. Built for US claims line-of-therapy work.

The unit of work is an **agent**. Seven of them cover the 15-step Phase 1
Clinical Foundation framework, and they execute by dependency, not in stage
order.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env        # optional; the app runs without any key
python run.py               # http://localhost:8000
```

Production:

```bash
uvicorn celestra.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Use one worker. Runs execute in-process and the event bus is per-process, so a
second worker would split a run's live stream. Scaling out means moving the bus
to Redis.

## Two modes

**Run all agents.** Every agent, ordered by the dependency graph. Agents with no
unmet dependency form a wave and run concurrently; a reconciliation gate closes
each wave before the next begins.

```
wave 1   Clinical Landscape        Treatment Evidence
wave 2   Diagnostic Footprint      Treatment Logic
wave 3   Patient Journey
wave 4   Information Synthesis
```

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

Confidence is a property of the evidence, not of the writing: **High**,
**Medium**, **Requires Input**, **Rejected**. A question that fails every
strategy is reported as unanswered with the reason and the sources tried, rather
than being quietly dropped.

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

## Credentials

Nothing is required. Each absent key degrades one thing, visibly, and the app
reports which in the UI.

| Key | Absent means |
|---|---|
| `ANTHROPIC_API_KEY` | planning, ranking, extraction and synthesis run on the deterministic engine. Real evidence and real citations, weaker prose. |
| `FIRECRAWL_API_KEY` | open-web fallback uses a keyless search path |
| `NCBI_API_KEY` | E-utilities limited to 3 requests/second instead of 10 |
| `ICD11_CLIENT_ID` / `SECRET` | ICD-11 codes unavailable; those questions report the blocker |
| `LOINC_USERNAME` / `PASSWORD` | LOINC codes unavailable; same |

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
