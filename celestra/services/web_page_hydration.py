"""Provider-neutral direct HTML hydration for open-web search results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from ..connectors._util import clean, html_text
from ..connectors.base import http
from ..connectors.firecrawl import MIN_PAGE_CHARS

MAX_PAGE_CHARS = 20_000
_CONTENT_SELECTORS = ("main", "article", "div#content", "div.content", "body")


@dataclass(frozen=True)
class WebPageHydration:
    """The usable text and its provenance without exposing provider details."""

    status: str
    text: str = ""
    title: str = ""


def _is_http_url(url: str) -> bool:
    parsed = urlparse(clean(url))
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def _page_text(html: str) -> str:
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001 - html_text supplies its tolerant fallback
        tree = None
    for selector in _CONTENT_SELECTORS:
        if selector != "body" and tree is not None and tree.css_first(selector) is None:
            continue
        text = clean(html_text(html, selector))
        if len(text) >= MIN_PAGE_CHARS:
            return text[:MAX_PAGE_CHARS]
    return ""


def _title(html: str) -> str:
    try:
        node = HTMLParser(html).css_first("title")
    except Exception:  # noqa: BLE001 - malformed HTML is already handled by html_text
        return ""
    return clean(node.text()) if node else ""


async def hydrate_web_page(
    url: str,
    *,
    snippet: str = "",
    http_client: Any | None = None,
) -> WebPageHydration:
    """Fetch a public page through the configured shared HTTP boundary.

    A long result snippet remains usable when direct fetching fails. Short
    snippets are deliberately not promoted into evidence.
    """
    normalized_url = clean(url)
    normalized_snippet = clean(snippet)[:MAX_PAGE_CHARS]
    if not _is_http_url(normalized_url):
        return WebPageHydration("unusable")

    try:
        client = http_client or http
        html = await client.get_text(normalized_url)
        text = _page_text(html)
        if text:
            return WebPageHydration("hydrated", text=text, title=_title(html))
    except Exception:  # noqa: BLE001 - direct hydration is enrichment
        return (WebPageHydration("snippet", text=normalized_snippet)
                if len(normalized_snippet) >= MIN_PAGE_CHARS else WebPageHydration("unusable"))

    if len(normalized_snippet) >= MIN_PAGE_CHARS:
        return WebPageHydration("snippet", text=normalized_snippet)
    return WebPageHydration("unusable")
