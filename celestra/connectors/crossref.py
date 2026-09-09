"""Crossref works search.

Also serves the `asco` source id through the `prefix` constructor argument:
DOI prefix 10.1200 is ASCO's, so a prefix-filtered bibliographic query is how
ASCO guidance is discovered without scraping asco.org.

Crossref frequently omits `abstract`. Since the extraction layer needs
quotable text, records that come back without one are enriched in a single
batched Europe PMC DOI lookup rather than left as bare titles.
"""
from __future__ import annotations

import time

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections, strip_tags
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

WORKS_URL = "https://api.crossref.org/works"
SELECT = "DOI,title,container-title,author,published,abstract,type,publisher,URL,issued"


def _author_string(item: dict) -> str:
    names = []
    for a in (item.get("author") or [])[:12]:
        name = " ".join(x for x in [clean(a.get("given")), clean(a.get("family"))] if x)
        if name:
            names.append(name)
    return ", ".join(names)


def _published(item: dict) -> str:
    for key in ("published", "issued", "published-print", "published-online"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts:
            return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))
    return ""


class CrossrefConnector:
    """Bibliographic search over Crossref, optionally pinned to a DOI prefix."""

    origin = EvidenceOrigin.APPROVED_API

    def __init__(self, source_id: str = "crossref", source_name: str = "Crossref",
                 tier: int = 2, prefix: str | None = None,
                 organization: str = "") -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.tier = tier
        self.prefix = prefix
        self.organization = organization

    def build_params(self, ctx: RetrievalContext, limit: int) -> dict[str, object]:
        bib = " ".join([ctx.indication, *(ctx.aspects[:3] or [])]).strip() or ctx.indication
        filters = []
        if self.prefix:
            filters.append(f"prefix:{self.prefix}")
        if ctx.cutoff:
            filters.append(f"until-pub-date:{ctx.cutoff}")
        params: dict[str, object] = {
            "query.bibliographic": bib,
            "select": SELECT,
            "rows": max(1, min(limit, 100)),
            "sort": "score",
        }
        if filters:
            params["filter"] = ",".join(filters)
        return params

    async def _abstracts_by_doi(self, dois: list[str]) -> dict[str, dict]:
        """One Europe PMC call for every DOI missing an abstract."""
        if not dois:
            return {}
        query = " OR ".join(f'DOI:"{d}"' for d in dois[:25])
        try:
            payload = await http.get_json(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": query, "resultType": "core", "format": "json",
                        "pageSize": len(dois[:25])},
            )
        except Exception:  # noqa: BLE001 - enrichment only
            return {}
        out = {}
        for rec in (payload.get("resultList") or {}).get("result") or []:
            doi = clean(rec.get("doi")).lower()
            if doi:
                out[doi] = rec
        return out

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            payload = await http.get_json(WORKS_URL, params=self.build_params(ctx, limit))
            calls += 1
            items = (payload.get("message") or {}).get("items") or []

            missing = [clean(i.get("DOI")) for i in items if not i.get("abstract")]
            enrichment = await self._abstracts_by_doi([d for d in missing if d])
            if enrichment:
                calls += 1

            refs: list[SourceRef] = []
            for item in items:
                doi = clean(item.get("DOI"))
                title = clean(item.get("title"))
                journal = clean(item.get("container-title"))
                epmc = enrichment.get(doi.lower(), {})
                abstract = strip_tags(clean(item.get("abstract"))) or clean(epmc.get("abstractText"))
                identifiers = {k: v for k, v in {
                    "doi": doi,
                    "pmid": clean(epmc.get("pmid")),
                    "pmcid": clean(epmc.get("pmcid")),
                }.items() if v}
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=clean(item.get("URL")) or (f"https://doi.org/{doi}" if doi else WORKS_URL),
                    title=title,
                    organization=self.organization or journal or clean(item.get("publisher")),
                    published=_published(item),
                    identifiers=identifiers,
                    snippet=clip(abstract or f"{title}. {journal}.", 1200),
                    raw={
                        "title": title,
                        "abstract": abstract,
                        "journal": journal,
                        "publisher": clean(item.get("publisher")),
                        "type": clean(item.get("type")),
                        "authors": _author_string(item),
                        "text": join_sections({
                            "Title": title,
                            "Journal": journal,
                            "Abstract": abstract,
                        }),
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
