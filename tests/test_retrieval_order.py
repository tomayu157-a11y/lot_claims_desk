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
    check("domain searches were then made", all(t.calls == 1 for t in targeted))
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
