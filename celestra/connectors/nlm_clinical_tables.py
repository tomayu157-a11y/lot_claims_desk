"""NLM Clinical Tables — no-authentication code lookups.

The National Library of Medicine publishes free, keyless search APIs over the
same code sets that otherwise require a licence, an account, or a downloaded
release file. That matters here because it is the difference between the code
stage answering its questions and reporting five blockers:

  ICD-10-CM   CMS release files are not installed
  ICD-9-CM    no GEMs file, so the three-system crosswalk had no legacy leg
  HCPCS       CMS release files are not installed
  LOINC       the Regenstrief search API needs an account

These stay secondary to the official release files: when a local dataset is
installed the local connector answers first and this one corroborates. What it
must never do is silently substitute for CPT, which is licensed and has no
free equivalent.

Search is driven by the entities upstream agents discovered — test names,
drug names, code anchors — which is the whole point of the handoff: the
diagnostic stage decides which tests matter and this stage maps them to codes.
Searching a code authority for the disease name alone returns almost nothing.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..models import EvidenceOrigin, SourceRef
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

log = logging.getLogger("celestra.connector.nlm")

BASE = "https://clinicaltables.nlm.nih.gov/api"


class _ClinicalTablesConnector:
    """Shared behaviour. Subclasses declare their table and field mapping."""

    table: str = ""
    version: str = "v3"
    search_fields: str = ""
    display_fields: str = ""
    origin = EvidenceOrigin.APPROVED_API
    tier = 1
    source_name = "NLM Clinical Tables"
    system_label = ""
    browse_url = ""
    # Which handoff keys supply the search terms, best first.
    context_keys: tuple[str, ...] = ()

    def __init__(self, source_id: str, name: str | None = None, tier: int | None = None) -> None:
        self.source_id = source_id
        if name:
            self.source_name = name
        if tier is not None:
            self.tier = tier

    # -- term selection --------------------------------------------------
    def terms_for(self, ctx: RetrievalContext) -> list[str]:
        """Upstream entities first, then the indication and its synonyms.

        Deduplicated case-insensitively and capped, because each term is a
        separate request and the point is coverage, not exhaustiveness.
        """
        terms: list[str] = []
        for key in self.context_keys:
            value = (ctx.extra or {}).get(key)
            if isinstance(value, list):
                terms += [str(v) for v in value]
        terms += self.fallback_terms(ctx)
        seen: set[str] = set()
        out: list[str] = []
        for t in terms:
            t = t.strip()
            key = t.lower()
            if len(t) < 3 or key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out[:8]

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        return ctx.or_terms()

    # -- request ---------------------------------------------------------
    async def _search(self, term: str, limit: int) -> list[list[str]]:
        params: dict[str, Any] = {"terms": term, "maxList": min(limit, 20)}
        if self.search_fields:
            params["sf"] = self.search_fields
        if self.display_fields:
            params["df"] = self.display_fields
        payload = await http.get_json(f"{BASE}/{self.table}/{self.version}/search", params=params)
        if not isinstance(payload, list) or len(payload) < 4:
            return []
        rows = payload[3]
        return [[str(c) for c in row] for row in rows if row] if isinstance(rows, list) else []

    def build_ref(self, row: list[str], term: str) -> SourceRef | None:
        if len(row) < 2:
            return None
        code = row[0].strip()
        description = " — ".join(p.strip() for p in row[1:] if p and p.strip())
        if not code or not description:
            return None
        # The quote is written so the code and its system survive extraction
        # into evidence, which is what the report table needs.
        quote = (
            f"{self.system_label} code {code} is defined as \"{description}\" in the "
            f"{self.source_name} {self.system_label} table."
        )
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=self.browse_url.format(code=code) if self.browse_url else f"{BASE}/{self.table}",
            title=f"{self.system_label} {code} — {description[:90]}",
            organization="U.S. National Library of Medicine",
            identifiers={"code": code, "system": self.system_label, "matched_term": term},
            snippet=quote,
            origin=self.origin,
            raw={"code": code, "description": description, "system": self.system_label,
                 "text": quote, "matched_term": term},
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}

    def _rank(self, refs: list[SourceRef], ctx: RetrievalContext) -> list[SourceRef]:
        """Order by overlap with what was actually asked.

        These tables match on substrings, so a broad term returns rows from
        unrelated conditions. Ranking by overlap keeps the on-topic codes and
        drops the noise when better rows exist.
        """
        wanted = self._tokens(" ".join([*ctx.or_terms(), *(ctx.aspects or [])]))
        if not wanted:
            return refs

        def score(ref: SourceRef) -> int:
            return len(self._tokens(ref.title) & wanted)

        scored = [(score(r), i, r) for i, r in enumerate(refs)]
        on_topic = [t for t in scored if t[0] > 0]
        chosen = on_topic or scored
        chosen.sort(key=lambda t: (-t[0], t[1]))
        return [t[2] for t in chosen]

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        terms = self.terms_for(ctx)
        if not terms:
            return ConnectorResult.failure(self.source_id, "no search term available")

        refs: list[SourceRef] = []
        seen_codes: set[str] = set()
        errors: list[str] = []
        calls = 0

        for term in terms:
            if len(refs) >= limit:
                break
            try:
                rows = await self._search(term, max(limit, 10))
                calls += 1
            except Exception as exc:  # noqa: BLE001 - one bad term must not end the lookup
                errors.append(describe_http_error(exc))
                continue
            for row in rows:
                ref = self.build_ref(row, term)
                if ref is None:
                    continue
                code = ref.identifiers["code"]
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                refs.append(ref)
                if len(refs) >= limit:
                    break

        if refs:
            return ConnectorResult(
                source_id=self.source_id, refs=self._rank(refs, ctx)[:limit], calls=calls
            )
        if errors:
            return ConnectorResult.failure(self.source_id, errors[0])
        return ConnectorResult.failure(
            self.source_id, f"no {self.system_label} match for: {', '.join(terms[:3])}"
        )


class NlmIcd10CmConnector(_ClinicalTablesConnector):
    table = "icd10cm"
    search_fields = "code,name"
    display_fields = "code,name"
    system_label = "ICD-10-CM"
    source_name = "NLM Clinical Tables (ICD-10-CM)"
    browse_url = "https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search?terms={code}"
    context_keys = ("icd10_codes",)


class NlmIcd9CmConnector(_ClinicalTablesConnector):
    table = "icd9cm_dx"
    search_fields = "long_name"
    display_fields = "code,long_name"
    system_label = "ICD-9-CM"
    source_name = "NLM Clinical Tables (ICD-9-CM)"
    browse_url = "https://clinicaltables.nlm.nih.gov/api/icd9cm_dx/v3/search?terms={code}"

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        """ICD-9-CM predates current subtype naming.

        Its leukemia titles say "lymphoid" where ICD-10-CM and current clinical
        usage say "lymphocytic" or "lymphoblastic", so searching the modern
        name alone returns nothing useful. The legacy spellings are added as
        extra search terms; which code comes back is still decided by the
        table, not by this connector.
        """
        terms = list(ctx.or_terms())
        legacy = []
        for term in terms:
            lowered = term.lower()
            for modern, historic in (("lymphocytic", "lymphoid"),
                                     ("lymphoblastic", "lymphoid")):
                if modern in lowered:
                    legacy.append(lowered.replace(modern, historic))
        return [*terms, *legacy, "lymphoid leukemia"]


class NlmHcpcsConnector(_ClinicalTablesConnector):
    table = "hcpcs"
    search_fields = "long_desc"
    display_fields = "code,short_desc,long_desc"
    system_label = "HCPCS"
    source_name = "NLM Clinical Tables (HCPCS)"
    browse_url = "https://clinicaltables.nlm.nih.gov/api/hcpcs/v3/search?terms={code}"
    context_keys = ("drugs", "hcpcs_codes", "test_names")

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        # HCPCS indexes products and services, never diseases. Without upstream
        # drug or test names there is nothing meaningful to look up.
        return []


class NlmLoincConnector(_ClinicalTablesConnector):
    table = "loinc_items"
    display_fields = "LOINC_NUM,LONG_COMMON_NAME"
    system_label = "LOINC"
    source_name = "NLM Clinical Tables (LOINC)"
    browse_url = "https://loinc.org/{code}/"
    context_keys = ("test_names", "loinc_codes")

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        return []


class NlmRxTermsConnector(_ClinicalTablesConnector):
    table = "rxterms"
    display_fields = "DISPLAY_NAME,STRENGTHS_AND_FORMS"
    system_label = "RxTerms"
    source_name = "NLM Clinical Tables (RxTerms)"
    tier = 2
    browse_url = "https://clinicaltables.nlm.nih.gov/api/rxterms/v3/search?terms={code}"
    context_keys = ("drugs",)

    def fallback_terms(self, ctx: RetrievalContext) -> list[str]:
        return []
