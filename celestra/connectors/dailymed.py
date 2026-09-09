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
