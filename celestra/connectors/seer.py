"""NCI SEER Cancer Stat Facts.

HTML scrape only. api.seer.cancer.gov does not carry the Stat Facts
statistics, so the fact sheet page is parsed directly: the "At a Glance"
boxes, the survival stat box with its reporting period, the incidence /
mortality / prevalence prose, and the first data table.

Indication keys map to page slugs. When a key is unknown the connector tries
the known slugs and keeps whichever page's title matches the context terms —
that is the firecrawl-free fallback; no web search is involved.
"""
from __future__ import annotations

import re
import time

from selectolax.parser import HTMLParser

from ..models import EvidenceOrigin, SourceRef
from ._util import clean, clip, join_sections, matches_any
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

STATFACTS = "https://seer.cancer.gov/statfacts/html/{slug}.html"

# Verified slugs. CLL and ALL are the two indications this desk covers; the
# rest are here so an unknown key still has somewhere sensible to look.
SLUGS: dict[str, str] = {
    "CLL": "clyl",
    "ALL": "alyl",
    "AML": "amyl",
    "CML": "cmyl",
    "NHL": "nhl",
    "MM": "mulmy",
}

# Prose we want verbatim; each matches a sentence in the fact sheet body.
PROSE_MARKERS = (
    "rate of new cases",
    "death rate",
    "people living with",
    "lifetime risk",
    "percent of men and women will be diagnosed",
    "most frequently diagnosed among",
    "most frequent among",
)


def _glance_pairs(tree: HTMLParser) -> dict[str, str]:
    """The 'Estimated New Cases in <year> / 22,760' label-value boxes."""
    pairs: dict[str, str] = {}
    for box in tree.css(".glance-factSheet .glanceBox p"):
        spans = [clean(s.text()) for s in box.css("span")]
        if len(spans) >= 2 and spans[0]:
            pairs[spans[0]] = spans[1]
    for box in tree.css(".glance-factSheet .statBox"):
        label = clean(box.css_first("p").text()) if box.css_first("p") else ""
        value = clean(box.css_first("strong").text()) if box.css_first("strong") else ""
        period = clean(box.css_first("span").text()) if box.css_first("span") else ""
        if label and value:
            pairs[label] = f"{value} ({period})" if period else value
    return pairs


def _prose(tree: HTMLParser) -> list[str]:
    out: list[str] = []
    for p in tree.css("p"):
        text = clean(p.text(separator=" ", strip=True))
        if len(text) < 60:
            continue
        low = text.lower()
        if any(m in low for m in PROSE_MARKERS):
            out.append(text)
    # De-duplicate while keeping page order.
    seen, unique = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique[:8]


def _first_table(tree: HTMLParser, max_rows: int = 12) -> dict[str, list]:
    table = tree.css_first("table")
    if table is None:
        return {}
    rows = []
    for tr in table.css("tr")[:max_rows]:
        cells = [clean(c.text()) for c in tr.css("th,td")]
        if any(cells):
            rows.append(cells)
    return {"rows": rows} if rows else {}


def _reporting_periods(text: str) -> list[str]:
    return sorted(set(re.findall(r"(?:19|20)\d{2}\s*[–-]\s*(?:19|20)\d{2}", text)))[:6]


class SeerConnector:
    """Cancer Stat Facts scrape for the run's indication."""

    source_id = "seer"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "NCI SEER"

    def slug_candidates(self, ctx: RetrievalContext) -> list[str]:
        key = (ctx.indication_key or "").upper()
        if key in SLUGS:
            return [SLUGS[key]]
        # Unknown key: try every known slug and keep the page whose title
        # matches the context terms.
        return list(SLUGS.values())

    async def _fetch(self, slug: str) -> tuple[str, str]:
        url = STATFACTS.format(slug=slug)
        return url, await http.get_text(url)

    def _build_ref(self, url: str, html: str) -> SourceRef | None:
        tree = HTMLParser(html)
        title_node = tree.css_first("title")
        title = clean(title_node.text()) if title_node else "SEER Cancer Stat Facts"
        glance = _glance_pairs(tree)
        prose = _prose(tree)
        if not glance and not prose:
            return None
        glance_text = "; ".join(f"{k}: {v}" for k, v in glance.items())
        body = join_sections({
            "At a glance": glance_text,
            "Statistics": " ".join(prose),
        }, limit=6000)
        page_text = clean(tree.body.text(separator=" ", strip=True)) if tree.body else ""
        return SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=url,
            title=title,
            organization="National Cancer Institute, Surveillance, Epidemiology, and End Results Program",
            published="",
            identifiers={"slug": url.rsplit("/", 1)[-1].replace(".html", "")},
            snippet=clip(body, 1500),
            raw={
                "at_a_glance": glance,
                "statistics": prose,
                "reporting_periods": _reporting_periods(page_text),
                "table": _first_table(tree),
                "text": body,
            },
            origin=self.origin,
        )

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        terms = ctx.or_terms()
        best: SourceRef | None = None
        try:
            for slug in self.slug_candidates(ctx):
                url, html = await self._fetch(slug)
                calls += 1
                ref = self._build_ref(url, html)
                if ref is None:
                    continue
                if len(self.slug_candidates(ctx)) == 1 or matches_any(ref.title, terms):
                    best = ref
                    break
                best = best or ref
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        refs = [best] if best else []
        return ConnectorResult(
            source_id=self.source_id, refs=refs[:limit], ok=True,
            reason="" if refs else "no stat facts page matched the indication",
            calls=calls, elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
