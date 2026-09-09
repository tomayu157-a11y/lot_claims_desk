"""NCI PDQ health-professional treatment summaries.

Scrape of cancer.gov PDQ pages. Each h2/h3 section becomes its own SourceRef
so a stage question can be answered from the section that actually addresses
it (Incidence and Mortality, Prognostic Factors, Treatment Options ...) rather
than from one undifferentiated page blob.
"""
from __future__ import annotations

import time

from selectolax.parser import HTMLParser

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, matches_any
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

BASE = "https://www.cancer.gov"

# Verified PDQ health-professional URLs.
PAGES: dict[str, str] = {
    "CLL": "/types/leukemia/hp/cll-treatment-pdq",
    "ALL": "/types/leukemia/hp/adult-all-treatment-pdq",
    "AML": "/types/leukemia/hp/adult-aml-treatment-pdq",
    "CML": "/types/leukemia/hp/cml-treatment-pdq",
}

# Sections with no clinical payload.
SKIP_HEADINGS = (
    "about this pdq summary", "changes to this summary", "latest updates",
    "key references", "references", "current clinical trials",
)


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


class NciPdqConnector:
    """PDQ section extraction for the run's indication."""

    source_id = "nci"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "National Cancer Institute (NCI) PDQ"

    def page_candidates(self, ctx: RetrievalContext) -> list[str]:
        key = (ctx.indication_key or "").upper()
        if key in PAGES:
            return [BASE + PAGES[key]]
        return [BASE + p for p in PAGES.values()]

    def _sections(self, html: str) -> list[tuple[str, str]]:
        """Walk the summary body, accumulating paragraph text under the last
        heading seen. cancer.gov nests sections, so a flat walk is more robust
        than relying on the section element tree."""
        tree = HTMLParser(html)
        tree.strip_tags(["script", "style", "noscript"])
        root = tree.css_first("div#cgvBody") or tree.css_first("main") or tree.body
        if root is None:
            return []
        out: list[tuple[str, list[str]]] = []
        for node in root.css("h2, h3, h4, p, li"):
            text = clean(node.text(separator=" ", strip=True))
            if not text:
                continue
            if node.tag in ("h2", "h3", "h4"):
                out.append((text, []))
            elif out and len(text) > 40:
                out[-1][1].append(text)
        return [(h, " ".join(body)) for h, body in out
                if body and h.lower() not in SKIP_HEADINGS
                and not any(h.lower().startswith(s) for s in SKIP_HEADINGS)]

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        terms = ctx.or_terms()
        try:
            chosen_url, sections = "", []
            for url in self.page_candidates(ctx):
                html = await http.get_text(url)
                calls += 1
                found = self._sections(html)
                if not found:
                    continue
                title_node = HTMLParser(html).css_first("title")
                title = clean(title_node.text()) if title_node else ""
                if len(self.page_candidates(ctx)) == 1 or matches_any(title, terms):
                    chosen_url, sections = url, found
                    break
                if not sections:
                    chosen_url, sections = url, found
            if not sections:
                return ConnectorResult(source_id=self.source_id, refs=[], ok=True,
                                       reason="no PDQ summary matched the indication",
                                       calls=calls,
                                       elapsed_ms=int((time.perf_counter() - started) * 1000))

            # Sections whose heading or body mentions the indication first.
            def rank(item: tuple[str, str]) -> tuple[int, int]:
                heading, body = item
                return (0 if matches_any(heading, terms) else 1, -len(body))

            refs = []
            for heading, body in sorted(sections, key=rank)[:max(1, limit)]:
                anchor = heading.lower().replace(" ", "-")
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=chosen_url,
                    title=f"PDQ: {heading}",
                    organization="National Cancer Institute",
                    published="",
                    identifiers={"pdq_summary": _slug(chosen_url), "section": heading},
                    snippet=clip(body, 1400),
                    raw={"heading": heading, "section_text": body,
                         "anchor": anchor, "text": body},
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
