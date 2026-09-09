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
