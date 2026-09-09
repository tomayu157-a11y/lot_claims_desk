"""PubMed via NCBI E-utilities.

Three calls, never one-per-record: esearch for the id set, one batched
esummary for the metadata of every id, one batched efetch for the abstracts.
`tool` and `email` are sent on every request (NCBI requires them) and
`api_key` whenever settings carry one; without a key NCBI caps us at 3 req/s,
which is why the calls are sequential.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from ..models import EvidenceOrigin, SourceRef
from ..settings import get_settings
from ._util import clean, clip, first_str, join_sections
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
ARTICLE_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"


def eutils_params(**extra: object) -> dict[str, object]:
    s = get_settings()
    params: dict[str, object] = {"tool": s.ncbi_tool, "email": s.ncbi_email}
    if s.ncbi_api_key:
        params["api_key"] = s.ncbi_api_key
    params.update(extra)
    return params


def _abstract_from_article(article: ET.Element) -> str:
    """Concatenate labelled AbstractText blocks in document order."""
    chunks: list[str] = []
    for node in article.iter("AbstractText"):
        label = node.attrib.get("Label", "").strip()
        text = clean("".join(node.itertext()))
        if not text:
            continue
        chunks.append(f"{label}: {text}" if label else text)
    return " ".join(chunks)


def parse_efetch(xml_text: str) -> dict[str, dict]:
    """pmid -> {abstract, title, journal, pub_types, mesh}."""
    out: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for art in root.iter("PubmedArticle"):
        pmid_node = art.find("./MedlineCitation/PMID")
        pmid = clean(pmid_node.text if pmid_node is not None else "")
        if not pmid:
            continue
        title_node = art.find("./MedlineCitation/Article/ArticleTitle")
        journal_node = art.find("./MedlineCitation/Article/Journal/Title")
        out[pmid] = {
            "abstract": _abstract_from_article(art),
            "title": clean("".join(title_node.itertext())) if title_node is not None else "",
            "journal": clean(journal_node.text) if journal_node is not None else "",
            "pub_types": [clean(n.text) for n in art.iter("PublicationType") if n.text],
            "mesh": [clean(n.text) for n in art.iter("DescriptorName") if n.text][:20],
        }
    return out


class PubMedConnector:
    """Literature discovery against PubMed."""

    source_id = "pubmed"
    tier = 2
    origin = EvidenceOrigin.APPROVED_API
    source_name = "PubMed"

    def build_term(self, ctx: RetrievalContext) -> str:
        synonyms = " OR ".join(f'"{t}"[Title/Abstract]' for t in ctx.or_terms())
        term = f"({synonyms})" if synonyms else clean(ctx.indication)
        if ctx.cutoff:
            term = f'{term} AND ("1900/01/01"[PDAT] : "{ctx.cutoff}"[PDAT])'
        return term

    async def _esearch(self, term: str, limit: int) -> list[str]:
        payload = await http.get_json(
            f"{EUTILS}/esearch.fcgi",
            params=eutils_params(db="pubmed", term=term, retmode="json",
                                 retmax=max(1, min(limit, 100)), sort="date"),
        )
        return [clean(i) for i in ((payload.get("esearchresult") or {}).get("idlist") or [])]

    async def _esummary(self, pmids: list[str]) -> dict[str, dict]:
        if not pmids:
            return {}
        payload = await http.get_json(
            f"{EUTILS}/esummary.fcgi",
            params=eutils_params(db="pubmed", id=",".join(pmids), retmode="json"),
        )
        result = payload.get("result") or {}
        return {p: result[p] for p in pmids if isinstance(result.get(p), dict)}

    async def _efetch(self, pmids: list[str]) -> dict[str, dict]:
        if not pmids:
            return {}
        xml_text = await http.get_text(
            f"{EUTILS}/efetch.fcgi",
            params=eutils_params(db="pubmed", id=",".join(pmids), retmode="xml", rettype="abstract"),
        )
        return parse_efetch(xml_text)

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            pmids = await self._esearch(self.build_term(ctx), limit)
            calls += 1
            if not pmids:
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason="no results", calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))
            summaries = await self._esummary(pmids)
            calls += 1
            details = await self._efetch(pmids)
            calls += 1

            refs: list[SourceRef] = []
            for pmid in pmids:
                summary = summaries.get(pmid, {})
                detail = details.get(pmid, {})
                title = first_str(summary.get("title"), detail.get("title"))
                abstract = clean(detail.get("abstract"))
                journal = first_str(summary.get("fulljournalname"), detail.get("journal"))
                ids = {clean(a.get("idtype")): clean(a.get("value"))
                       for a in (summary.get("articleids") or [])}
                identifiers = {k: v for k, v in {
                    "pmid": pmid,
                    "pmcid": ids.get("pmcid", ""),
                    "doi": ids.get("doi", ""),
                }.items() if v}
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=ARTICLE_URL.format(pmid=pmid),
                    title=title,
                    organization=journal or self.source_name,
                    published=first_str(summary.get("pubdate"), summary.get("epubdate")),
                    identifiers=identifiers,
                    snippet=clip(abstract or title, 1200),
                    raw={
                        "title": title,
                        "abstract": abstract,
                        "journal": journal,
                        "authors": ", ".join(clean(a.get("name")) for a in
                                             (summary.get("authors") or [])[:12]),
                        "pub_types": detail.get("pub_types") or summary.get("pubtype") or [],
                        "mesh": detail.get("mesh") or [],
                        "text": join_sections({"Title": title, "Abstract": abstract}),
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


async def convert_ids(ids: list[str]) -> dict[str, dict]:
    """PMC ID converter: pmid/pmcid/doi crosswalk. Returns {input_id: record}."""
    ids = [clean(i) for i in ids if clean(i)]
    if not ids:
        return {}
    s = get_settings()
    try:
        payload = await http.get_json(
            IDCONV,
            params={"ids": ",".join(ids), "format": "json",
                    "tool": s.ncbi_tool, "email": s.ncbi_email},
        )
    except Exception:  # noqa: BLE001 - the crosswalk is an enrichment, not a gate
        return {}
    out: dict[str, dict] = {}
    for rec in payload.get("records") or []:
        key = clean(rec.get("pmid")) or clean(rec.get("pmcid")) or clean(rec.get("doi"))
        if key:
            out[key] = rec
    return out
