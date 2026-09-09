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

  1. Bing HTML        (www.bing.com/search)
  2. DuckDuckGo HTML  (html.duckduckgo.com)
  3. DuckDuckGo Lite  (lite.duckduckgo.com)
  4. Domain index     (the restricted domain's own sitemap; targeted search only)

Every keyless result passes a relevance gate before it is kept: search
front-ends under bot pressure happily return a plausible-looking page of
results for the first word of the query alone, and unfiltered noise is worse
than an honest empty result. Whatever the backend, the page itself is then
fetched and its text extracted, so a ref always carries quotable text rather
than a search-result teaser.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

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

log = logging.getLogger("celestra.connector.web")

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
    """Search engines wrap result links. Recover the real target.

    Bing encodes it as `u=a1<urlsafe-base64>` on /ck/a; DuckDuckGo puts it in
    `uddg=` on /l/. An unwrappable link is returned unchanged so the caller can
    reject it on its own terms.
    """
    if not href:
        return ""
    href = href.replace("&amp;", "&")
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query)

    if "bing.com" in host and parsed.path.startswith("/ck/"):
        raw = (query.get("u") or [""])[0]
        if raw.startswith("a1"):
            payload = raw[2:]
            payload += "=" * (-len(payload) % 4)
            try:
                decoded = base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
            except (ValueError, UnicodeDecodeError):
                return ""
            return decoded if decoded.startswith("http") else ""
        return ""

    if "duckduckgo.com" in host and parsed.path.startswith("/l/"):
        target = (query.get("uddg") or [""])[0]
        return unquote(target) if target else ""

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


class _SearchBreaker:
    """Stops re-attempting web search once the network has proved it is blocked.

    Keyless search backends are frequently unreachable: rate limits, bot
    challenges, or an egress policy. Without a breaker every question pays the
    full backend timeout chain for nothing, which is the difference between a
    run taking one minute and twenty. After `threshold` consecutive total
    failures the breaker opens and search returns immediately with a reason the
    UI can show. Any single success closes it again.

    A configured Firecrawl key is the supported path and never trips it.
    """

    threshold = 3

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.open = False
        self.reason = ""

    def record_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold and not self.open:
            self.open = True
            self.reason = (
                "open-web search unavailable from this network "
                f"({reason}); configure FIRECRAWL_API_KEY to enable fallback"
            )
            log.warning("web-search breaker opened: %s", self.reason)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.open:
            log.info("web-search breaker closed")
        self.open = False
        self.reason = ""


# Shared by every domain-scoped instance, because they all use the same
# backends and therefore all fail for the same reason.
breaker = _SearchBreaker()


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
            use_cache=False,
        )
        tree = HTMLParser(html)
        out: list[dict] = []
        seen: set[str] = set()

        # Prefer the structured result blocks, which carry a snippet.
        for item in tree.css("li.b_algo"):
            anchor = item.css_first("h2 a") or item.css_first("a[href]")
            if anchor is None:
                continue
            href = _unwrap_redirect(anchor.attributes.get("href", ""))
            if not href.startswith("http") or href in seen:
                continue
            seen.add(href)
            para = item.css_first("p")
            out.append({"url": href, "title": clean(anchor.text()),
                        "snippet": clean(para.text()) if para else "", "backend": "bing"})

        # Bing rotates its result markup, so fall back to every wrapped link on
        # the page rather than depending on one class name.
        if len(out) < limit:
            for anchor in tree.css('a[href*="/ck/a"]'):
                href = _unwrap_redirect(anchor.attributes.get("href", ""))
                if not href.startswith("http") or href in seen:
                    continue
                title = clean(anchor.text())
                if len(title) < 8:
                    continue
                seen.add(href)
                out.append({"url": href, "title": title, "snippet": "", "backend": "bing"})
                if len(out) >= limit * 3:
                    break
        return out

    async def _domain_index(self, query: str, limit: int) -> list[dict]:
        """Harvest the restricted domain's own sitemap and rank its URLs by
        token overlap with the query.

        Last resort, and only for a domain-restricted search: when every search
        front-end is unreachable or bot-blocked, a site's own sitemap still
        yields real, on-domain, on-topic URLs. Sitemap locations come from
        robots.txt where the site publishes them, so nothing is hardcoded per
        domain.
        """
        if not self.domain:
            return []
        wanted = {t for t in tokens(query) if len(t) > 3}
        if not wanted:
            return []
        queue = [f"https://www.{self.domain}/sitemap.xml",
                 f"https://{self.domain}/sitemap.xml",
                 f"https://www.{self.domain}/sitemap_index.xml"]
        for robots in (f"https://www.{self.domain}/robots.txt",
                       f"https://{self.domain}/robots.txt"):
            try:
                text = await http.get_text(robots, headers={"User-Agent": BROWSER_UA})
            except Exception:  # noqa: BLE001 - robots.txt is optional
                continue
            queue.extend(re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)[:8])
            break

        locs: list[str] = []
        seen_maps: set[str] = set()
        while queue and len(locs) < 20000 and len(seen_maps) < 12:
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
                # Follow the child sitemaps whose own URL looks topical first.
                children = sorted(found, key=lambda u: -len(wanted & tokens(u)))
                queue.extend(children[:8])
                continue
            locs.extend(found)

        scored = [(len(wanted & tokens(unquote(u))), u) for u in locs]
        scored = [(score, u) for score, u in scored if score]
        scored.sort(key=lambda pair: (-pair[0], len(pair[1])))
        return [{"url": u, "title": "", "snippet": "", "backend": "domain-index"}
                for _, u in scored[:limit * 2]]

    async def search(self, query: str, limit: int) -> list[dict]:
        """Ranked results as {url, title, snippet, backend}. Never raises."""
        query = clean(query)
        if not query:
            return []

        keyed = get_settings().firecrawl_enabled
        if not keyed and breaker.open:
            return []

        backends = []
        if keyed:
            backends.append(("firecrawl", lambda: self._firecrawl_search(query, limit)))
        backends.extend([
            ("bing", lambda: self._bing(query, limit)),
            ("duckduckgo", lambda: self._ddg(query, limit)),
            ("duckduckgo-lite", lambda: self._ddg(query, limit, lite=True)),
            ("domain-index", lambda: self._domain_index(query, limit)),
        ])

        last_error = "no backend returned results"
        for name, backend in backends:
            try:
                results = await backend()
            except Exception as exc:  # noqa: BLE001 - fall through to the next backend
                last_error = f"{name}: {type(exc).__name__}"
                continue
            kept: list[dict] = []
            for item in results:
                url = item.get("url", "")
                if self.domain:
                    host = _domain_of(url)
                    if host != self.domain and not host.endswith("." + self.domain):
                        continue
                # Sitemap URLs carry no title or snippet, so their relevance
                # was already decided by the token match on the URL itself.
                if item.get("backend") != "domain-index" and not is_relevant(
                        query, item.get("title", ""), item.get("snippet", ""), url):
                    continue
                if url not in {k["url"] for k in kept}:
                    kept.append(item)
            if kept:
                if not keyed:
                    breaker.record_success()
                return kept[:limit]

        if not keyed:
            breaker.record_failure(last_error)
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
