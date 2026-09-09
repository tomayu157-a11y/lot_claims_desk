"""Programmatic guideline discovery.

This is the answer to "how do we find the guideline PMID without pinning it".
A guideline is discovered by publication type, not by a hardcoded identifier:
Europe PMC is queried for `PUB_TYPE:"Practice Guideline"` OR `"Guideline"` OR
`"Consensus Development Conference"` intersected with the indication in the
title, sorted newest first. The newest edition therefore wins automatically
when a society republishes.

An optional `journal` or `organization` hint narrows the same query to the
publishing venue — Ann Oncol for ESMO, Blood / ashpublications for iwCLL and
ASH — instead of naming a specific paper. NO PMID IS EVER HARDCODED.
"""
from __future__ import annotations

import time

from ..models import EvidenceOrigin
from ._util import clean, matches_any
from .base import ConnectorResult, RetrievalContext, describe_http_error
from .europepmc import EuropePmcConnector, phrase_clause, ref_from_record

GUIDELINE_PUB_TYPES = (
    "Practice Guideline",
    "Guideline",
    "Consensus Development Conference",
)

# Title words that mark a document as guidance even when the publication type
# metadata has not caught up.
GUIDELINE_TITLE_TERMS = (
    "guideline", "guidelines", "recommendation", "recommendations",
    "consensus", "practice statement", "clinical practice",
)


class GuidelineDiscoveryConnector:
    """Discover the current guideline for an indication, optionally per venue."""

    origin = EvidenceOrigin.APPROVED_API

    def __init__(self, source_id: str, source_name: str, tier: int = 1,
                 journal: str | list[str] | None = None,
                 organization: str = "") -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.tier = tier
        self.journals = [journal] if isinstance(journal, str) else list(journal or [])
        self.organization = organization
        self._epmc = EuropePmcConnector(source_id=source_id, source_name=source_name,
                                        tier=tier)

    def build_query(self, ctx: RetrievalContext, with_journal: bool = True) -> str:
        pub_types = " OR ".join(f'PUB_TYPE:"{p}"' for p in GUIDELINE_PUB_TYPES)
        title_terms = phrase_clause(ctx.or_terms(), fields=("TITLE",))
        parts = [f"({pub_types})"]
        if title_terms:
            parts.append(title_terms)
        if with_journal and self.journals:
            parts.append("(" + " OR ".join(f'JOURNAL:"{j}"' for j in self.journals) + ")")
        query = " AND ".join(parts)
        if ctx.cutoff:
            query = f"{query} AND (FIRST_PDATE:[1900-01-01 TO {ctx.cutoff}])"
        return query

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            records = []
            if self.journals:
                records = await self._epmc.search(self.build_query(ctx), limit)
                calls += 1
            if not records:
                # Venue hint too narrow (or absent): fall back to publication
                # type alone rather than reporting no guideline exists.
                records = await self._epmc.search(self.build_query(ctx, with_journal=False),
                                                  limit)
                calls += 1
            if not records:
                # Last resort: title-level guidance wording. Some societies
                # publish guidance that Europe PMC has not typed yet.
                title_clause = phrase_clause(ctx.or_terms(), fields=("TITLE",))
                wording = " OR ".join(f'TITLE:"{t}"' for t in GUIDELINE_TITLE_TERMS)
                query = " AND ".join(p for p in [title_clause, f"({wording})"] if p)
                records = await self._epmc.search(query, limit)
                calls += 1

            refs = [
                ref_from_record(r, self.source_id, self.source_name, self.tier, self.origin)
                for r in records
            ]
            for ref in refs:
                ref.organization = self.organization or ref.organization
                ref.raw["discovered_by"] = "publication-type search"
                ref.raw["journal_hint"] = self.journals

            if self.journals:
                # Keep venue matches first without discarding the rest: a
                # society's guideline sometimes appears in a partner journal.
                refs.sort(key=lambda r: 0 if matches_any(
                    clean(r.raw.get("journal")), self.journals) else 1)
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs[:limit], ok=True,
            reason="" if refs else "no guideline publication found", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
