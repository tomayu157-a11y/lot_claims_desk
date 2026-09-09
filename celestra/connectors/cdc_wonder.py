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

Two operational facts observed on 2026-09-09: WONDER enforces a minimum of 15
seconds between API requests (HTTP 200 with a "Request rate exceeded" message),
and it validates the by-variable / measure-button combination server-side, so a
rejected template is reported verbatim in the failure reason instead of being
retried blindly.
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
        parameter("O_age", "D158.V51"),
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
                return ConnectorResult.failure(
                    self.source_id, clip(f"WONDER rejected the request: {messages[0]}", 200))
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
