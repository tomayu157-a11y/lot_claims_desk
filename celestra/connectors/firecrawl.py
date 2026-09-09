"""Web search and page scraping.

Two modes, one class:

* `open_web` — the tier 5 supplementary fallback. origin=OPEN_WEB.
* `targeted_search` — a domain-restricted search serving acs, lls, cibmtr,
  who, cdc_icd10, cms and fda. origin=TARGETED_SEARCH and the tier is the
  registry tier for that source, NOT 5. A targeted search of cdc.gov is
  approved-domain evidence; calling it tier 5 would understate it.

With `FIRECRAWL_API_KEY` set, the Firecrawl v1 API does search and scrape.
Without it the connector still works through a keyless chain, tried in order
until one returns on-topic results:

  1. DuckDuckGo HTML  (html.duckduckgo.com)
  2. DuckDuckGo Lite  (lite.duckduckgo.com)
  3. Bing HTML/RSS
  4. Domain index — sitemap.xml harvest, only for a domain-restricted search

Every keyless result passes a relevance gate before it is kept: search
front-ends under bot pressure happily return a plausible-looking page of
results for the first word of the query alone, and unfiltered noise is worse
than an honest empty result. Whatever the backend, the page itself is then
fetched and its text extracted, so a ref always carries quotable text rather
than a search-result teaser.
"""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from selectolax.parser import HTMLParser

from ..models import EvidenceOrigin, SourceRef
from ..settings import get_settings
from ._util import clean, clip, html_text, tokens
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

FIRECRAWL_SEARCH = "https://api.firecrawl.dev/v1/search"
FIRECRAWL_SCRAPE = "https://api.firecrawl.dev/v1/scrape"
DDG_HTML = "https://html.duckduckgo.com/html/"
DDG_LITE = "https://lite.duckduckgo.com/lite/"
BING_HTML = "https://www.bing.com/search"

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

MAX_SCRAPES = 4
MIN_PAGE_CHARS = 200


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _unwrap_redirect(href: str) -> str:
    """DuckDuckGo and Bing wrap result links; recover the destination."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    if "duckduckgo.com" in host and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    if "bing.com" in host and "/ck/a" in parsed.path:
        import base64

        match = re.search(r"[?&]u=a1([^&]+)", href)
        if match:
            blob = match.group(1) + "=" * (-len(match.group(1)) % 4)
            try:
                return base64.urlsafe_b64decode(blob).decode("utf-8", "ignore")
            except (ValueError, UnicodeDecodeError):
                return href
    return href


def is_relevant(query: str, *texts: str) -> bool:
    """At least one distinctive query token must appear in the result.

    Cheap, but it is what stops a degraded search front-end from injecting
    dictionary pages for the first word of a clinical query.
    """
    wanted = {t for t in tokens(query) if len(t) > 3}
    if not wanted:
        return True
    haystack = tokens(" ".join(t for t in texts if t))
    return bool(wanted & haystack)


class FirecrawlConnector:
    """Open-web or domain-restricted search with page text extraction."""

    def __init__(self, source_id: str = "open_web",
                 source_name: str = "Open Web (Supplementary)",
                 tier: int = 5, domain: str | None = None,
                 organization: str = "", search_hint: str = "") -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.domain = (domain or "").lower().removeprefix("www.") or None
        self.tier = tier
        self.organization = organization or source_name
        self.search_hint = search_hint
        # A domain restriction is what separates targeted search from open web.
        self.origin = (EvidenceOrigin.TARGETED_SEARCH if self.domain
                       else EvidenceOrigin.OPEN_WEB)

    # -- query -----------------------------------------------------------
    def build_query(self, ctx: RetrievalContext) -> str:
        parts = [ctx.indication]
        if self.search_hint:
            parts.append(self.search_hint)
        elif ctx.aspects:
            parts.extend(ctx.aspects[:2])
        elif ctx.question:
            parts.append(ctx.question)
        return clean(" ".join(parts))

    def _scoped(self, query: str) -> str:
        return f"site:{self.domain} {query}" if self.domain else query

    # -- backends --------------------------------------------------------
    async def _firecrawl_search(self, query: str, limit: int) -> list[dict]:
        key = get_settings().firecrawl_api_key
        payload = await http.request(
            "POST", FIRECRAWL_SEARCH,
            json_body={"query": self._scoped(query), "limit": max(1, min(limit, 20))},
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            use_cache=False,
        )
        out = []
        for item in (payload.get("data") or []):
            url = clean(item.get("url"))
            if url:
                out.append({"url": url, "title": clean(item.get("title")),
                            "snippet": clean(item.get("description")), "backend": "firecrawl"})
        return out

    async def _ddg(self, query: str, limit: int, lite: bool = False) -> list[dict]:
        url = DDG_LITE if lite else DDG_HTML
        html = await http.request(
            "POST", url, data={"q": self._scoped(query)},
            headers={"User-Agent": BROWSER_UA,
                     "Content-Type": "application/x-www-form-urlencoded"},
            as_json=False, use_cache=False,
        )
        tree = HTMLParser(html)
        out = []
        anchors = tree.css("a.result__a") or tree.css("a.result-link")
        for anchor in anchors[:limit * 3]:
            href = _unwrap_redirect(anchor.attributes.get("href", ""))
            if not href.startswith("http"):
                continue
            parent = anchor.parent.parent if anchor.parent else None
            snippet_node = parent.css_first(".result__snippet") if parent else None
            out.append({"url": href, "title": clean(anchor.text()),
                        "snippet": clean(snippet_node.text()) if snippet_node else "",
                        "backend": "duckduckgo-lite" if lite else "duckduckgo"})
        return out

    async def _bing(self, query: str, limit: int) -> list[dict]:
        html = await http.get_text(
            BING_HTML,
            params={"q": self._scoped(query), "count": max(10, limit * 2)},
            headers={"User-Agent": BROWSER_UA},
        )
        tree = HTMLParser(html)
        out = []
        for item in tree.css("li.b_algo")[:limit * 3]:
            anchor = item.css_first("h2 a")
            if anchor is None:
                continue
            href = _unwrap_redirect(anchor.attributes.get("href", ""))
            if not href.startswith("http"):
                continue
            para = item.css_first("p")
            out.append({"url": href, "title": clean(anchor.text()),
                        "snippet": clean(para.text()) if para else "", "backend": "bing"})
        return out

    async def _domain_index(self, query: str, limit: int) -> list[dict]:
        """Harvest the restricted domain's own sitemap and rank URLs by token
        overlap with the query. Used only when no search backend answers, and
        only when a domain restriction makes the result set meaningful."""
        if not self.domain:
            return []
        wanted = {t for t in tokens(query) if len(t) > 3}
        locs: list[str] = []
        seen_maps: set[str] = set()
        queue = [f"https://www.{self.domain}/sitemap.xml", f"https://{self.domain}/sitemap.xml"]
        while queue and len(locs) < 5000 and len(seen_maps) < 6:
            candidate = queue.pop(0)
            if candidate in seen_maps:
                continue
            seen_maps.add(candidate)
            try:
                xml = await http.get_text(candidate, headers={"User-Agent": BROWSER_UA})
            except Exception:  # noqa: BLE001 - try the next sitemap
                continue
            found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
            if "<sitemapindex" in xml:
                # Follow child sitemaps whose own URL looks topical first.
                children = sorted(found, key=lambda u: -len(wanted & tokens(u)))
                queue.extend(children[:3])
                continue
            locs.extend(found)
        scored = [(len(wanted & tokens(unquote(u))), u) for u in locs]
        scored = [(s, u) for s, u in scored if s]
        scored.sort(key=lambda pair: (-pair[0], len(pair[1])))
        return [{"url": u, "title": "", "snippet": "", "backend": "domain-index"}
                for _, u in scored[:limit * 2]]

    # -- public API ------------------------------------------------------
    async def search(self, query: str, limit: int) -> list[dict]:
        """Ranked results as {url, title, snippet, backend}. Never raises."""
        query = clean(query)
        if not query:
            return []
        backends = []
        if get_settings().firecrawl_enabled:
            backends.append(lambda: self._firecrawl_search(query, limit))
        backends.extend([
            lambda: self._ddg(query, limit),
            lambda: self._ddg(query, limit, lite=True),
            lambda: self._bing(query, limit),
            lambda: self._domain_index(query, limit),
        ])
        for backend in backends:
            try:
                results = await backend()
            except Exception:  # noqa: BLE001 - fall through to the next backend
                continue
            kept = []
            for item in results:
                if self.domain and _domain_of(item["url"]) != self.domain \
                        and not _domain_of(item["url"]).endswith("." + self.domain):
                    continue
                if item["backend"] != "domain-index" and not is_relevant(
                        query, item.get("title", ""), item.get("snippet", ""), item["url"]):
                    continue
                if item["url"] not in {k["url"] for k in kept}:
                    kept.append(item)
            if kept:
                return kept[:limit]
        return []

    async def scrape(self, url: str) -> dict:
        """Page text as {url, title, text, backend}. Never raises."""
        url = clean(url)
        if not url:
            return {}
        if get_settings().firecrawl_enabled:
            try:
                key = get_settings().firecrawl_api_key
                payload = await http.request(
                    "POST", FIRECRAWL_SCRAPE,
                    json_body={"url": url, "formats": ["markdown"]},
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    use_cache=False,
                )
                data = payload.get("data") or {}
                text = clean(data.get("markdown") or data.get("content"))
                if text:
                    meta = data.get("metadata") or {}
                    return {"url": url, "title": clean(meta.get("title")),
                            "text": text, "backend": "firecrawl"}
            except Exception:  # noqa: BLE001 - fall back to a direct fetch
                pass
        try:
            html = await http.get_text(url, headers={"User-Agent": BROWSER_UA})
        except Exception:  # noqa: BLE001 - an unreachable page is not an error
            return {}
        tree = HTMLParser(html)
        title_node = tree.css_first("title")
        # Prefer the article body; fall back to the whole document.
        text = ""
        for selector in ("main", "article", "div#content", "div.content"):
            text = html_text(html, selector)
            if len(text) >= MIN_PAGE_CHARS:
                break
        if len(text) < MIN_PAGE_CHARS:
            text = html_text(html)
        return {"url": url, "title": clean(title_node.text()) if title_node else "",
                "text": text, "backend": "direct"}

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        try:
            query = self.build_query(ctx)
            results = await self.search(query, max(1, limit))
            calls += 1
            if not results:
                return ConnectorResult(
                    source_id=self.source_id, refs=[], ok=False,
                    reason="no web search backend returned on-topic results",
                    calls=calls,
                    elapsed_ms=int((time.perf_counter() - started) * 1000))

            refs: list[SourceRef] = []
            for item in results[:min(limit, MAX_SCRAPES)]:
                page = await self.scrape(item["url"])
                calls += 1
                text = clean(page.get("text"))
                if len(text) < MIN_PAGE_CHARS:
                    text = clean(item.get("snippet"))
                if not text:
                    continue  # a ref with no quotable text is worthless downstream
                title = clean(item.get("title")) or clean(page.get("title")) or item["url"]
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=item["url"],
                    title=title,
                    organization=self.organization,
                    published="",
                    identifiers={"domain": _domain_of(item["url"]),
                                 "search_backend": item.get("backend", "")},
                    snippet=clip(text, 1500),
                    raw={
                        "query": self._scoped(query),
                        "search_backend": item.get("backend", ""),
                        "scrape_backend": page.get("backend", "snippet"),
                        "result_snippet": clean(item.get("snippet")),
                        "page_text": text[:20000],
                        "text": text[:20000],
                    },
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=bool(refs),
            reason="" if refs else "no page text could be extracted", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
