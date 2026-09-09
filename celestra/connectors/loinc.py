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
