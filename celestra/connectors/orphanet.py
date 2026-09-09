"""Orphanet / Orphadata.

Three products are used: rd-cross-referencing (name -> ORPHAcode plus the
ICD-10 / ICD-11 / MeSH / OMIM / UMLS crosswalk), rd-epidemiology (prevalence
and annual incidence rows) and rd-natural_history (age of onset, inheritance).

`resolve_codes` is the orchestrator's provisional terminology anchor, so it is
a first-class public coroutine rather than an internal step of `discover`.
Verified today: CLL -> ORPHA 67038 / ICD-10 C91.1 / ICD-11 2A82.0;
ALL -> ORPHA 513 / ICD-10 C91.0 with NO ICD-11 mapping. The absence of an
ICD-11 row is reported as absent, never back-filled.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

BASE = "https://api.orphadata.com"
NAME_URL = BASE + "/rd-cross-referencing/orphacodes/names/{name}"
EPI_URL = BASE + "/rd-epidemiology/orphacodes/{code}"
NH_URL = BASE + "/rd-natural_history/orphacodes/{code}"
ORPHA_PAGE = "https://www.orpha.net/en/disease/detail/{code}"

# Crosswalk sources we surface. Everything else in ExternalReference is kept in
# `raw` but not promoted into identifiers.
CROSSWALK_SOURCES = ("ICD-10", "ICD-11", "MeSH", "OMIM", "UMLS", "MONDO", "GARD")


def _results(payload: Any) -> dict[str, Any]:
    """Orphadata returns `data.results` as an object for a single hit and a
    list when several match. Normalise to the first object."""
    data = (payload or {}).get("data") or {}
    results = data.get("results")
    if isinstance(results, list):
        return results[0] if results else {}
    return results or {}


def _definition(record: dict) -> str:
    for block in record.get("SummaryInformation") or []:
        text = clean(block.get("Definition"))
        if text:
            return text
    return ""


def _prevalence_lines(epi: dict) -> list[str]:
    lines = []
    for row in epi.get("Prevalence") or []:
        parts = [
            clean(row.get("PrevalenceType")),
            clean(row.get("PrevalenceClass")),
            f"mean {clean(row.get('ValMoy'))}" if clean(row.get("ValMoy")) else "",
            f"in {clean(row.get('PrevalenceGeographic'))}" if row.get("PrevalenceGeographic") else "",
            f"({clean(row.get('PrevalenceValidationStatus'))})"
            if row.get("PrevalenceValidationStatus") else "",
        ]
        line = " ".join(p for p in parts if p)
        if line:
            lines.append(line)
    return lines


class OrphanetConnector:
    """Rare-disease epidemiology plus the terminology crosswalk."""

    source_id = "orphanet"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "Orphanet / Orphadata"

    async def _by_name(self, name: str) -> dict:
        payload = await http.get_json(NAME_URL.format(name=quote(clean(name))),
                                      params={"lang": "en"})
        return _results(payload)

    async def resolve_codes(self, name: str) -> dict[str, Any]:
        """Resolve a disease name to its ORPHAcode and terminology crosswalk.

        Returns {} when nothing resolves. Missing systems are simply absent
        from `codes` — an unmapped ICD-11 is a fact about the disease, not a
        gap to be filled in.
        """
        try:
            record = await self._by_name(name)
        except Exception:  # noqa: BLE001 - the anchor degrades, it never raises
            return {}
        if not record:
            return {}
        codes: dict[str, list[str]] = {}
        for ref in record.get("ExternalReference") or []:
            source = clean(ref.get("Source"))
            value = clean(ref.get("Reference"))
            if not source or not value:
                continue
            codes.setdefault(source, [])
            if value not in codes[source]:
                codes[source].append(value)
        orpha = clean(record.get("ORPHAcode"))
        return {
            "orphacode": orpha,
            "preferred_term": clean(record.get("Preferred term")),
            "synonyms": [clean(s) for s in record.get("Synonym") or []],
            "definition": _definition(record),
            "url": ORPHA_PAGE.format(code=orpha) if orpha else clean(record.get("OrphanetURL")),
            "codes": {k: v for k, v in codes.items() if k in CROSSWALK_SOURCES},
            "all_codes": codes,
            "icd10": (codes.get("ICD-10") or [""])[0],
            "icd11": (codes.get("ICD-11") or [""])[0],
            "mesh": (codes.get("MeSH") or [""])[0],
            "umls": (codes.get("UMLS") or [""])[0],
            "unmapped": [s for s in ("ICD-10", "ICD-11", "MeSH", "UMLS") if not codes.get(s)],
        }

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        refs: list[SourceRef] = []
        try:
            anchor: dict[str, Any] = {}
            for candidate in ctx.or_terms():
                anchor = await self.resolve_codes(candidate)
                calls += 1
                if anchor.get("orphacode"):
                    break
            if not anchor.get("orphacode"):
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason="no ORPHAcode for indication", calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))

            code = anchor["orphacode"]
            url = anchor.get("url") or ORPHA_PAGE.format(code=code)
            identifiers = {k: v for k, v in {
                "orphacode": code,
                "icd10": anchor.get("icd10", ""),
                "icd11": anchor.get("icd11", ""),
                "mesh": anchor.get("mesh", ""),
                "umls": anchor.get("umls", ""),
            }.items() if v}

            crosswalk_text = "; ".join(
                f"{system}: {', '.join(values)}" for system, values in
                (anchor.get("codes") or {}).items()
            )
            unmapped = anchor.get("unmapped") or []
            definition = anchor.get("definition", "")
            refs.append(SourceRef(
                source_id=self.source_id,
                source_name=self.source_name,
                tier=self.tier,
                url=url,
                title=f"Orphanet {anchor.get('preferred_term', '')} (ORPHA:{code})".strip(),
                organization="Orphanet",
                published="",
                identifiers=identifiers,
                snippet=clip(join_sections({
                    "Definition": definition,
                    "Terminology crosswalk": crosswalk_text,
                    "Not mapped": ", ".join(unmapped),
                }), 1400),
                raw={
                    "anchor": anchor,
                    "definition": definition,
                    "crosswalk": anchor.get("codes"),
                    "unmapped_systems": unmapped,
                    "text": join_sections({
                        "Preferred term": anchor.get("preferred_term", ""),
                        "Definition": definition,
                        "Synonyms": ", ".join(anchor.get("synonyms") or []),
                        "Terminology crosswalk": crosswalk_text,
                    }),
                },
                origin=self.origin,
            ))

            epi = _results(await http.get_json(EPI_URL.format(code=code), params={"lang": "en"}))
            calls += 1
            lines = _prevalence_lines(epi)
            if lines:
                body = " | ".join(lines)
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=url,
                    title=f"Orphanet epidemiology: {anchor.get('preferred_term', '')}",
                    organization="Orphanet",
                    published=clean(epi.get("Date"))[:10],
                    identifiers=identifiers,
                    snippet=clip(body, 1200),
                    raw={"prevalence": epi.get("Prevalence") or [], "text": body},
                    origin=self.origin,
                ))

            nat = _results(await http.get_json(NH_URL.format(code=code), params={"lang": "en"}))
            calls += 1
            onset = ", ".join(clean(x) for x in nat.get("AverageAgeOfOnset") or [])
            inheritance = ", ".join(clean(x) for x in nat.get("TypeOfInheritance") or [])
            if onset or inheritance:
                body = join_sections({
                    "Average age of onset": onset,
                    "Type of inheritance": inheritance,
                    "Typology": clean(nat.get("Typology")),
                })
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=url,
                    title=f"Orphanet natural history: {anchor.get('preferred_term', '')}",
                    organization="Orphanet",
                    published=clean(nat.get("Date"))[:10],
                    identifiers=identifiers,
                    snippet=clip(body, 800),
                    raw={"natural_history": nat, "text": body},
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            if refs:
                # Partial success beats a dead source: keep what resolved.
                return ConnectorResult(source_id=self.source_id, refs=refs[:limit], ok=True,
                                       reason=f"partial: {describe_http_error(exc)}", calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs[:limit], ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
