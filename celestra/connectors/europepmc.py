"""Europe PMC.

The primary literature surface: `resultType=core` returns title AND abstract
AND every identifier (pmid, pmcid, doi) in a single call, so one request yields
quotable text instead of a second round trip per record.

Queries are always field-scoped (TITLE / ABSTRACT / JOURNAL / PUB_TYPE); an
unscoped query matches full text and drowns the result set in noise.
"""
from __future__ import annotations

import time

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, first_str, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
ARTICLE_URL = "https://europepmc.org/article/{source}/{ident}"


def phrase_clause(terms: list[str], fields: tuple[str, ...] = ("TITLE", "ABSTRACT")) -> str:
    """OR-join every synonym across the given field scopes."""
    parts = [f'{field}:"{t}"' for t in terms for field in fields if t]
    return "(" + " OR ".join(parts) + ")" if parts else ""


def article_url(rec: dict) -> str:
    doi = clean(rec.get("doi"))
    pmid = clean(rec.get("pmid"))
    pmcid = clean(rec.get("pmcid"))
    if pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    if pmcid:
        return f"https://europepmc.org/article/PMC/{pmcid}"
    if doi:
        return f"https://doi.org/{doi}"
    return ARTICLE_URL.format(source=clean(rec.get("source")) or "MED", ident=clean(rec.get("id")))


def journal_of(rec: dict) -> str:
    info = rec.get("journalInfo") or {}
    journal = info.get("journal") or {}
    return first_str(journal.get("title"), rec.get("journalTitle"), rec.get("bookOrReportDetails"))


def ref_from_record(rec: dict, source_id: str, source_name: str, tier: int,
                    origin: EvidenceOrigin) -> SourceRef:
    """Build a fully populated SourceRef. `raw` keeps the abstract intact so the
    extraction service can pull verbatim quotes from it."""
    abstract = clean(rec.get("abstractText"))
    title = clean(rec.get("title"))
    journal = journal_of(rec)
    pub_types = [clean(t) for t in (rec.get("pubTypeList") or {}).get("pubType", [])]
    identifiers = {
        k: v for k, v in {
            "pmid": clean(rec.get("pmid")),
            "pmcid": clean(rec.get("pmcid")),
            "doi": clean(rec.get("doi")),
            "epmc_id": clean(rec.get("id")),
            "source": clean(rec.get("source")),
        }.items() if v
    }
    return SourceRef(
        source_id=source_id,
        source_name=source_name,
        tier=tier,
        url=article_url(rec),
        title=title,
        organization=journal or source_name,
        published=first_str(rec.get("firstPublicationDate"), rec.get("pubYear")),
        identifiers=identifiers,
        snippet=clip(abstract or title, 1200),
        raw={
            "title": title,
            "abstract": abstract,
            "journal": journal,
            "authors": clean(rec.get("authorString")),
            "pub_types": pub_types,
            "cited_by": rec.get("citedByCount"),
            "is_open_access": rec.get("isOpenAccess") == "Y",
            "mesh": [clean(m.get("descriptorName"))
                     for m in (rec.get("meshHeadingList") or {}).get("meshHeading", [])][:20],
            "text": join_sections({"Title": title, "Abstract": abstract}),
        },
        origin=origin,
    )


class EuropePmcConnector:
    """Core literature search over Europe PMC."""

    source_id = "europepmc"
    tier = 2
    origin = EvidenceOrigin.APPROVED_API

    def __init__(self, source_id: str = "europepmc", source_name: str = "Europe PMC",
                 tier: int = 2) -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.tier = tier

    def build_query(self, ctx: RetrievalContext, extra_clause: str = "") -> str:
        clause = phrase_clause(ctx.or_terms())
        parts = [p for p in (clause, extra_clause) if p]
        query = " AND ".join(parts)
        if ctx.cutoff:
            query = f'{query} AND (FIRST_PDATE:[1900-01-01 TO {ctx.cutoff}])'
        return query

    async def search(self, query: str, limit: int, sort: str = "P_PDATE_D desc") -> list[dict]:
        payload = await http.get_json(
            SEARCH_URL,
            params={
                "query": query,
                "resultType": "core",
                "format": "json",
                "pageSize": max(1, min(limit, 100)),
                "sort": sort,
            },
        )
        return (payload.get("resultList") or {}).get("result") or []

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            query = self.build_query(ctx)
            records = await self.search(query, limit)
            calls += 1
            if not records:
                # Narrow scope produced nothing: widen from title/abstract to
                # the whole record rather than reporting a dead source.
                query = " OR ".join(f'"{t}"' for t in ctx.or_terms())
                records = await self.search(query, limit)
                calls += 1
            refs = [
                ref_from_record(r, self.source_id, self.source_name, self.tier, self.origin)
                for r in records
            ]
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id,
            refs=refs,
            ok=True,
            reason="" if refs else "no results",
            calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


async def fetch_full_text(pmcid: str) -> str:
    """Open-access full text for a PMCID, or "" when the article is not in the
    OA subset. A 404 here is the normal case, not an error."""
    pmcid = clean(pmcid).upper()
    if not pmcid:
        return ""
    if not pmcid.startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    try:
        xml = await http.get_text(FULLTEXT_URL.format(pmcid=pmcid))
    except Exception:  # noqa: BLE001 - absence of full text is expected
        return ""
    from ._util import strip_tags

    return strip_tags(xml)
