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
