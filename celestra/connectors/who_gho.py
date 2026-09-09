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
            specific |= {t for t in tokens(term) if len(t) > 4}
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
