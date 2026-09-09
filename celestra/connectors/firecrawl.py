"""Web search and page scraping.

Two modes, one class:

* `open_web` — the tier 5 supplementary fallback. origin=OPEN_WEB.
* `targeted_search` — a domain-restricted search serving acs, lls, cibmtr,
  who, cdc_icd10, cms and fda. origin=TARGETED_SEARCH and the tier is the
  registry tier for that source, NOT 5. A targeted search of cdc.gov is
  approved-domain evidence; calling it tier 5 would understate it.

With `FIRECRAWL_API_KEY` set, the Firecrawl API (v2 by default; v1 via
FIRECRAWL_API_VERSION, any base via FIRECRAWL_API_URL) does search and scrape.
Without it the connector still works through a keyless chain, tried in order
until one returns on-topic results:

  1. Bing HTML        (www.bing.com/search)
  2. DuckDuckGo HTML  (html.duckduckgo.com)
  3. DuckDuckGo Lite  (lite.duckduckgo.com)
  4. Domain index     (sitemap harvest: the restricted domain for a targeted
                     search, OPEN_WEB_DOMAIN_PANEL for the open-web source)

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

import httpx
from selectolax.parser import HTMLParser

from ..models import EvidenceOrigin, SourceRef
from ..settings import get_settings, get_thresholds
from ._util import clean, clip, html_text, tokens
from .base import ConnectorResult, RetrievalContext, describe_http_error, http, remedy_for

DDG_HTML = "https://html.duckduckgo.com/html/"
DDG_LITE = "https://lite.duckduckgo.com/lite/"
BING_HTML = "https://www.bing.com/search"

log = logging.getLogger("celestra.connector.web")

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

MAX_SCRAPES = 3                 # open-web pages read per question
MAX_SCRAPES_TARGETED = 2        # per approved domain, per question
MIN_PAGE_CHARS = 200
FIRECRAWL_RETRIES = 1
FIRECRAWL_TIMEOUT = 25.0

# Domains the open-web fallback harvests when no search front-end is reachable.
# Public, high-credibility health publishers whose sitemaps carry topical URLs.
# This is a last resort for the unrestricted `open_web` source only; results are
# still tier 5 and still labelled SUPPLEMENTARY WEB EVIDENCE.
OPEN_WEB_DOMAIN_PANEL = ("cancer.org", "cancer.gov", "medlineplus.gov", "cms.gov")


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
    """Does the result actually answer the query, or just echo one word of it?

    A search front-end under bot pressure answers a multi-word clinical query
    with results for its first word alone — "chronic lymphocytic leukemia
    incidence" comes back as dictionary entries for "chronic". Requiring a
    single token match lets all of that through, so a multi-token query must
    match at least two distinct tokens.
    """
    wanted = {t for t in tokens(query) if len(t) > 3}
    if not wanted:
        return True
    matched = wanted & tokens(" ".join(t for t in texts if t))
    return len(matched) >= (2 if len(wanted) >= 2 else 1)


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

# The most recent Firecrawl failure across every instance, in words, with the
# time it happened. Empty once a call succeeds. The Settings page and the
# banner read this so a failing key is visible without opening a log.
firecrawl_status: dict = {
    "error": "", "at": "",
    # A definitive refusal (no credits, key rejected) or repeated transport
    # failures stop every further Firecrawl call in this process. Questions
    # then rely on the registry sources and are reported as unanswered where
    # those did not suffice; the run never stops or hangs on the web.
    "blocked": "", "consecutive_failures": 0, "calls": 0, "skipped": 0,
}

# Search results for the process, so a refined query or a second agent asking
# the same thing never pays for the same search twice.
_SEARCH_CACHE: dict[tuple[str, int, bool], list[dict]] = {}


def _record_firecrawl_failure(message: str, *, status: int | None = None,
                              transport: bool = False) -> None:
    from datetime import datetime, timezone

    firecrawl_status["error"] = message
    firecrawl_status["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds") if message else ""
    if not message:
        firecrawl_status["consecutive_failures"] = 0
        return
    if status == 402:
        firecrawl_status["blocked"] = (
            "Firecrawl credits are exhausted (402). Web search is off for the rest of this "
            "session; questions the registry sources cannot answer stay unanswered. Top up "
            "the account and restart to re-enable it."
        )
    elif status in (401, 403):
        firecrawl_status["blocked"] = (
            f"Firecrawl rejected the API key ({status}). Web search is off until the key "
            "in .env is fixed and the server restarted."
        )
    elif transport:
        firecrawl_status["consecutive_failures"] += 1
        limit = int(get_thresholds()["escalation"].get("firecrawl_max_consecutive_failures", 2))
        if firecrawl_status["consecutive_failures"] >= limit:
            firecrawl_status["blocked"] = (
                f"Firecrawl could not be reached {limit} times in a row ({message[:120]}). "
                "Web search is off for the rest of this session so the run does not wait "
                "on it; fix the network setting shown on the Settings page and restart."
            )
    if firecrawl_status["blocked"]:
        log.warning("firecrawl disabled for this session: %s", firecrawl_status["blocked"])


def firecrawl_blocked() -> str:
    """Why Firecrawl must not be called right now, or '' when it may be."""
    return str(firecrawl_status.get("blocked") or "")


def reset_firecrawl_status() -> None:
    firecrawl_status.update({"error": "", "at": "", "blocked": "",
                             "consecutive_failures": 0, "calls": 0, "skipped": 0})
    _SEARCH_CACHE.clear()

# domain -> harvested sitemap URLs, or [] when the domain will not serve them.
# Process-lifetime, because a sitemap changes far more slowly than a run.
_SITEMAP_CACHE: dict[str, list[str]] = {}


def _title_from_url(url: str) -> str:
    """A readable label from a URL path, for results that carry no title."""
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"\.(html?|aspx|php)$", "", tail)
    return re.sub(r"[-_]+", " ", tail).strip().title() or url


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
        # Which backend last answered, and why a preferred one did not. Read by
        # the health check and the sources panel.
        self.last_backend: str = ""
        self.last_error: str = ""
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
    @staticmethod
    def _firecrawl_headers() -> dict[str, str]:
        key = (get_settings().firecrawl_api_key or "").strip()
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "Accept": "application/json"}

    @staticmethod
    def parse_search_payload(payload: dict) -> list[dict]:
        """Results from either API shape.

        v1 returns `data` as a list. v2 returns `data` as an object keyed by
        source (`web`, `news`, `images`); only `web` carries pages worth
        quoting. Both are accepted so a version change never means an empty
        result that looks like "nothing on the web".
        """
        if not isinstance(payload, dict):
            return []
        if payload.get("success") is False:
            raise RuntimeError(str(payload.get("error") or "Firecrawl returned success=false"))
        data = payload.get("data")
        items: list = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = list(data.get("web") or [])
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = clean(item.get("url"))
            if not url:
                continue
            out.append({
                "url": url,
                "title": clean(item.get("title")),
                "snippet": clean(item.get("description") or item.get("snippet")),
                "markdown": clean(item.get("markdown")),
                "backend": "firecrawl",
            })
        return out

    async def _firecrawl_search(self, query: str, limit: int,
                                with_content: bool = True) -> list[dict]:
        """One search call. With `with_content` the pages come back in the same
        call as markdown, so no separate scrape is paid for or waited on.
        `limit` is the number of pages actually wanted, never more."""
        s = get_settings()
        scoped = self._scoped(query)
        limit = max(1, min(limit, 10))
        key = (scoped, limit, with_content)
        if key in _SEARCH_CACHE:
            return [dict(r) for r in _SEARCH_CACHE[key]]
        body: dict = {"query": scoped, "limit": limit}
        if s.firecrawl_version == "v2":
            body["sources"] = ["web"]
        if with_content:
            body["scrapeOptions"] = {"formats": ["markdown"], "onlyMainContent": True}
        firecrawl_status["calls"] += 1
        payload = await http.request(
            "POST", s.firecrawl_endpoint("search"), json_body=body,
            headers=self._firecrawl_headers(), use_cache=False,
            retries=FIRECRAWL_RETRIES, timeout=FIRECRAWL_TIMEOUT,
        )
        results = self.parse_search_payload(payload)
        _SEARCH_CACHE[key] = [dict(r) for r in results]
        return results

    @classmethod
    async def probe(cls, query: str = "chronic lymphocytic leukemia incidence") -> dict:
        """One live Firecrawl call, reported in words. Used by `run.py --check`
        and the Settings page so a broken key or a blocked network is
        diagnosed where the person is looking, not in a log."""
        s = get_settings()
        started = time.perf_counter()
        info = {
            "configured": s.firecrawl_enabled,
            "endpoint": s.firecrawl_endpoint("search"),
            "version": s.firecrawl_version,
            "ok": False, "results": 0, "detail": "", "remedy": "", "elapsed_ms": 0,
        }
        if not s.firecrawl_enabled:
            info["detail"] = "FIRECRAWL_API_KEY is not set"
            info["remedy"] = ("Add FIRECRAWL_API_KEY to .env and restart. Until then the "
                              "open-web fallback uses a keyless path that many networks block.")
            return info
        conn = cls()
        try:
            hits = await conn._firecrawl_search(query, 3)
            info["ok"] = bool(hits)
            info["results"] = len(hits)
            if not hits:
                info["detail"] = "the key was accepted but the probe query returned no results"
                info["remedy"] = "Unusual for this query; retry, then check the Firecrawl status page."
        except Exception as exc:  # noqa: BLE001 - this is the diagnostic
            info["detail"] = describe_http_error(exc)
            info["remedy"] = remedy_for(exc)
            # A failed probe is a real failed call and counts like one.
            _record_firecrawl_failure(
                f"{info['detail']}. {info['remedy']}".strip(),
                status=getattr(getattr(exc, "response", None), "status_code", None),
                transport=isinstance(exc, (httpx.TransportError, OSError)),
            )
        else:
            _record_firecrawl_failure("")
        info["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return info

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

        Last resort: when every search front-end is unreachable or bot-blocked,
        a site's own sitemap still yields real, on-topic URLs. A domain-scoped
        instance harvests its own domain; the unrestricted open-web instance
        harvests OPEN_WEB_DOMAIN_PANEL. Sitemap locations come from robots.txt
        where the site publishes them, so no per-domain path is hardcoded.
        """
        wanted = {t for t in tokens(query) if len(t) > 3}
        if not wanted:
            return []
        domains = [self.domain] if self.domain else list(OPEN_WEB_DOMAIN_PANEL)
        results: list[dict] = []
        for domain in domains:
            results.extend(await self._sitemap_urls(domain, wanted, limit))
            if len(results) >= limit * 2:
                break
        return results[:limit * 2]

    async def _sitemap_urls(self, domain: str, wanted: set[str],
                            limit: int) -> list[dict]:
        """Harvested URLs for one domain, memoised for the process.

        A sitemap describes the whole site, so it is worth fetching once and
        reusing for every question. Without this, a domain that blocks the
        request (cdc.gov, who.int and lls.org all do) was re-probed for every
        question in every stage, and each failed probe costs several seconds
        across the robots.txt and sitemap URL candidates. An empty result is
        cached too, because "this domain will not serve us" is exactly the
        answer worth remembering.
        """
        cached = _SITEMAP_CACHE.get(domain)
        if cached is None:
            cached = await self._harvest_sitemap(domain)
            _SITEMAP_CACHE[domain] = cached
        if not cached:
            return []
        scored = []
        for url in cached:
            overlap = len(tokens(url) & wanted)
            if overlap:
                scored.append((overlap, url))
        scored.sort(key=lambda t: -t[0])
        return [
            {"url": url, "title": _title_from_url(url), "snippet": "",
             "backend": "domain-index"}
            for _, url in scored[: limit * 2]
        ]

    async def _harvest_sitemap(self, domain: str) -> list[str]:
        queue = [f"https://www.{domain}/sitemap.xml",
                 f"https://{domain}/sitemap.xml",
                 f"https://www.{domain}/sitemap_index.xml"]
        for robots in (f"https://www.{domain}/robots.txt",
                       f"https://{domain}/robots.txt"):
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
                queue.extend(found[:8])
                continue
            locs.extend(found)

        # Ranking happens per query in _sitemap_urls; this returns the raw
        # index so one harvest serves every question in the run.
        return [unquote(u) for u in locs]

    async def search(self, query: str, limit: int) -> list[dict]:
        """Ranked results as {url, title, snippet, backend}. Never raises."""
        query = clean(query)
        if not query:
            return []

        keyed = get_settings().firecrawl_enabled
        if not keyed and breaker.open:
            return []
        if keyed and firecrawl_blocked():
            # A refused key or exhausted credits: do not spend a call, and do
            # not drag the run through the keyless engines either. The
            # question falls back to whatever the registry sources gave.
            firecrawl_status["skipped"] += 1
            self.last_error = firecrawl_blocked()
            return []

        backends = []
        if keyed:
            backends.append(("firecrawl", lambda: self._firecrawl_search(query, limit)))
        backends.extend([
            ("bing", lambda: self._bing(query, limit)),
            ("duckduckgo", lambda: self._ddg(query, limit)),
            ("duckduckgo-lite", lambda: self._ddg(query, limit, lite=True)),
        ])
        if get_thresholds()["escalation"].get("enable_domain_index"):
            backends.append(("domain-index", lambda: self._domain_index(query, limit)))

        last_error = "no backend returned results"
        for name, backend in backends:
            try:
                results = await backend()
            except Exception as exc:  # noqa: BLE001 - fall through to the next backend
                detail = describe_http_error(exc) if isinstance(exc, Exception) else str(exc)
                last_error = f"{name}: {detail}"
                if name == "firecrawl":
                    # Firecrawl is configured and billed. Falling through to a
                    # keyless engine without saying so is how a broken key
                    # looks exactly like a working one. Say what broke and
                    # what fixes it; "ConnectError" alone helps nobody.
                    remedy = remedy_for(exc) if isinstance(exc, Exception) else ""
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    log.warning("firecrawl search failed: %s. %s Falling back to a "
                                "keyless engine for query=%r",
                                detail, remedy, query[:120])
                    self.last_error = f"{detail}. {remedy}".strip()
                    _record_firecrawl_failure(
                        self.last_error, status=status,
                        transport=isinstance(exc, (httpx.TransportError, OSError)),
                    )
                    if firecrawl_blocked() and breaker.open:
                        return []
                elif breaker.open:
                    # The keyless engines have already proved unreachable
                    # on this network; do not pay their timeouts again.
                    continue
                continue
            if name == "firecrawl":
                _record_firecrawl_failure("")
            self.last_backend = name
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
                        query, item.get("title", ""), item.get("snippet", ""), url,
                        str(item.get("markdown", ""))[:3000]):
                    continue
                if url not in {k["url"] for k in kept}:
                    kept.append(item)
            if kept:
                if name != "firecrawl":
                    breaker.record_success()
                return kept[:limit]

        if not any(n == "firecrawl" and not firecrawl_blocked() for n, _ in backends):
            # Every engine tried was keyless (or Firecrawl was already
            # blocked); count the miss towards the keyless breaker so a
            # blocked network stops costing a timeout chain per question.
            breaker.record_failure(last_error)
        return []

    async def scrape(self, url: str, markdown: str = "") -> dict:
        """Page text as {url, title, text, backend}. Never raises. Pass the
        markdown a search already returned to skip the scrape call."""
        url = clean(url)
        if not url:
            return {}
        if markdown and len(clean(markdown)) >= MIN_PAGE_CHARS:
            return {"url": url, "title": "", "text": clean(markdown), "backend": "firecrawl"}
        if get_settings().firecrawl_enabled and not firecrawl_blocked():
            try:
                firecrawl_status["calls"] += 1
                payload = await http.request(
                    "POST", get_settings().firecrawl_endpoint("scrape"),
                    json_body={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                    headers=self._firecrawl_headers(), use_cache=False,
                    retries=FIRECRAWL_RETRIES, timeout=FIRECRAWL_TIMEOUT,
                )
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise RuntimeError(str(payload.get("error") or "success=false"))
                data = (payload.get("data") if isinstance(payload, dict) else None) or {}
                text = clean(data.get("markdown") or data.get("content"))
                if text:
                    meta = data.get("metadata") or {}
                    return {"url": url, "title": clean(meta.get("title")),
                            "text": text, "backend": "firecrawl"}
            except Exception as exc:  # noqa: BLE001 - fall back to a direct fetch
                detail = describe_http_error(exc)
                log.warning("firecrawl scrape failed: %s. %s Fetching %s directly.",
                            detail, remedy_for(exc), url)
                self.last_error = f"scrape: {detail}"
                _record_firecrawl_failure(
                    self.last_error,
                    status=getattr(getattr(exc, "response", None), "status_code", None),
                    transport=isinstance(exc, (httpx.TransportError, OSError)),
                )
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

    def refs_from_results(self, results: list[dict], query: str = "") -> list[SourceRef]:
        """Search results as SourceRefs, carrying any page text the search
        already returned so a later scrape can be skipped."""
        refs: list[SourceRef] = []
        for item in results:
            url = clean(item.get("url"))
            if not url:
                continue
            text = clean(item.get("markdown")) or clean(item.get("snippet"))
            refs.append(SourceRef(
                source_id=self.source_id, source_name=self.source_name, tier=self.tier,
                url=url, title=clean(item.get("title")) or url,
                organization=self.organization,
                identifiers={"domain": _domain_of(url),
                             "search_backend": item.get("backend", "")},
                snippet=clip(text, 1500),
                raw={"query": query, "search_backend": item.get("backend", ""),
                     "markdown": clean(item.get("markdown")),
                     "result_snippet": clean(item.get("snippet")),
                     "page_text": text[:20000], "text": text[:20000]},
                origin=self.origin,
            ))
        return refs

    async def search_refs(self, query: str, limit: int) -> list[SourceRef]:
        return self.refs_from_results(await self.search(query, limit), query)

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        calls = 0
        if get_settings().firecrawl_enabled and firecrawl_blocked():
            return ConnectorResult.failure(self.source_id, firecrawl_blocked()[:160])
        try:
            query = clean(ctx.extra.get("search_query") or "") if ctx.extra else ""
            query = query or self.build_query(ctx)
            results = await self.search(query, max(1, min(limit,
                                        MAX_SCRAPES_TARGETED if self.domain else MAX_SCRAPES)))
            calls += 1
            if not results:
                return ConnectorResult(
                    source_id=self.source_id, refs=[], ok=False,
                    reason="no web search backend returned on-topic results",
                    calls=calls,
                    elapsed_ms=int((time.perf_counter() - started) * 1000))

            refs: list[SourceRef] = []
            cap = MAX_SCRAPES_TARGETED if self.domain else MAX_SCRAPES
            for item in results[:min(limit, cap)]:
                page = await self.scrape(item["url"], item.get("markdown", ""))
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
