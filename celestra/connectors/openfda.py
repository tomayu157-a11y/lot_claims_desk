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
