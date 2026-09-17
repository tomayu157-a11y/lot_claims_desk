from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import httpx
import pytest

from celestra.connectors.firecrawl import FirecrawlConnector
from celestra.models import EvidenceOrigin
from celestra.services.azure_web_search import (
    AzureWebSearchClient,
    AzureWebSearchParseError,
    parse_responses_stream,
)
from celestra.services.web_page_hydration import hydrate_web_page
from celestra.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
SUCCESS_LINES = (FIXTURES / "azure_web_search_success.sse").read_text(encoding="utf-8").splitlines()
UNUSABLE_LINES = (FIXTURES / "azure_web_search_unusable.sse").read_text(encoding="utf-8").splitlines()
QUERY = "ALL claims line definition"


class FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code
        self.headers = {"x-provider-debug": "must-not-leak"}
        self.request = httpx.Request("POST", "https://azure.example/openai/v1/responses")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("status", request=self.request,
                                        response=httpx.Response(self.status_code, request=self.request))

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeStreamingClient:
    def __init__(self, lines: list[str], status_code: int = 200, failure: Exception | None = None) -> None:
        self.lines = lines
        self.status_code = status_code
        self.failure = failure
        self.request: SimpleNamespace | None = None

    def stream(self, method: str, url: str, *, headers: dict[str, str], json: dict):
        self.request = SimpleNamespace(method=method, url=url, headers=headers, json=json)
        if self.failure:
            raise self.failure
        return FakeStreamResponse(self.lines, self.status_code)


class FakeDirectHttp:
    def __init__(self, html: str | Exception) -> None:
        self.html = html
        self.urls: list[str] = []

    async def get_text(self, url: str, **kwargs: object) -> str:
        self.urls.append(url)
        if isinstance(self.html, Exception):
            raise self.html
        return self.html


def azure_settings() -> Settings:
    return Settings(
        llm_provider="azure_openai",
        azure_openai_endpoint="https://azure.example/",
        azure_openai_api_key="test-key",
        azure_openai_deployment="test-deployment",
    )


@pytest.mark.asyncio
async def test_search_posts_the_required_azure_responses_request_and_normalizes_typed_output() -> None:
    stream = FakeStreamingClient(SUCCESS_LINES)
    hydrated_text = "Direct page text. " * 20
    outcome = await AzureWebSearchClient(
        settings=azure_settings(), streaming_client=stream,
        direct_http=FakeDirectHttp(f"<main>{hydrated_text}</main>"),
    ).search(QUERY, limit=1, on_status=lambda message: None)

    assert stream.request is not None
    request = stream.request
    assert request.url == "https://azure.example/openai/v1/responses"
    assert request.headers["api-key"] == "test-key"
    assert request.json == {
        "model": "test-deployment",
        "tools": [{"type": "web_search"}],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources", "web_search_call.results"],
        "input": QUERY,
        "stream": True,
        "store": False,
    }
    assert outcome.ok is True
    assert len(outcome.refs) == len(outcome.audits) == 1
    ref = outcome.refs[0]
    assert ref.source_id == "open_web"
    assert ref.origin is EvidenceOrigin.OPEN_WEB
    assert ref.identifiers["domain"] == "example.org"
    assert ref.identifiers["search_backend"] == "azure_web_search"
    assert ref.raw["query"] == QUERY
    assert ref.raw["result_snippet"].startswith("A sufficiently long")
    assert ref.raw["page_text"] == hydrated_text.strip()
    assert ref.raw["text"] == hydrated_text.strip()
    assert ref.raw["azure_consulted_sources"]
    assert ref.raw["azure_url_citations"]
    assert outcome.audits[0].hydration_status == "hydrated"


def test_parser_uses_item_types_not_output_order_and_ignores_unknown_events() -> None:
    parsed = parse_responses_stream(["event: response.created", "data: {}", *SUCCESS_LINES])
    assert parsed.query == QUERY
    assert parsed.results[0]["url"] == "https://example.org/method"
    assert parsed.url_citations[0]["title"] == "Method"


def test_parser_rejects_completed_response_without_a_web_search_call() -> None:
    payload = {"type": "response.completed", "response": {"output": [{"type": "message", "content": []}]}}
    with pytest.raises(AzureWebSearchParseError, match="web_search_call"):
        parse_responses_stream([f"data: {json.dumps(payload)}"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lines", "status_code", "failure"),
    [
        (SUCCESS_LINES, 401, None),
        (SUCCESS_LINES, 200, httpx.ReadTimeout("timeout")),
        (["data: not-json"], 200, None),
    ],
    ids=["non-2xx", "timeout", "malformed-stream"],
)
async def test_native_search_transport_or_stream_failure_returns_one_typed_failure(
    lines: list[str], status_code: int, failure: Exception | None,
) -> None:
    outcome = await AzureWebSearchClient(
        settings=azure_settings(), streaming_client=FakeStreamingClient(lines, status_code, failure),
        direct_http=FakeDirectHttp(""),
    ).search(QUERY, limit=1)

    assert outcome.ok is False
    assert outcome.refs == []
    assert outcome.audits == []
    assert outcome.reason.startswith("native web search failed:")
    assert "must-not-leak" not in outcome.reason
    assert "test-key" not in outcome.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["main", "article", "div#content", "div.content", "body"])
async def test_direct_hydration_uses_the_document_content_preference_order(selector: str) -> None:
    body = "Usable direct page text. " * 20
    if selector == "body":
        html = f"<html><body>{body}</body></html>"
    else:
        html = f"<html><body><{selector if selector in ('main', 'article') else 'div'}"
        html += " id='content'" if selector == "div#content" else " class='content'" if selector == "div.content" else ""
        html += f">{body}</{selector if selector in ('main', 'article') else 'div'}></body></html>"
    outcome = await hydrate_web_page("https://example.org/page", http_client=FakeDirectHttp(html))
    assert outcome.status == "hydrated"
    assert outcome.text == body.strip()


@pytest.mark.asyncio
async def test_direct_hydration_uses_article_before_falling_back_to_body() -> None:
    page_chrome = "Page chrome that must not become evidence. " * 10
    article_text = "Article evidence selected before body fallback. " * 10
    html = f"<html><body>{page_chrome}<article>{article_text}</article></body></html>"

    outcome = await hydrate_web_page("https://example.org/page", http_client=FakeDirectHttp(html))

    assert outcome.status == "hydrated"
    assert outcome.text == article_text.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["", "ftp://example.org/file", "https:///missing-host", "not a url"])
async def test_direct_hydration_accepts_only_http_urls_with_a_hostname(url: str) -> None:
    direct_http = FakeDirectHttp("<main>Never fetched</main>")
    outcome = await hydrate_web_page(url, http_client=direct_http)
    assert outcome.status == "unusable"
    assert direct_http.urls == []


@pytest.mark.asyncio
async def test_direct_hydration_retains_long_result_snippet_after_fetch_failure() -> None:
    snippet = "Result snippet retained when direct fetch fails. " * 10
    outcome = await hydrate_web_page("https://example.org/page", snippet=snippet,
                                     http_client=FakeDirectHttp(httpx.ConnectError("blocked")))
    assert outcome.status == "snippet"
    assert outcome.text == snippet.strip()


@pytest.mark.asyncio
async def test_direct_hydration_rejects_short_page_and_short_result_snippet() -> None:
    outcome = await hydrate_web_page("https://example.org/page", snippet="Too short.",
                                     http_client=FakeDirectHttp("<main>Short.</main>"))
    assert outcome.status == "unusable"
    assert outcome.text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("firecrawl_result", [
    {"url": "https://example.org/method", "title": "Method", "snippet": "Firecrawl parity text. " * 20,
     "backend": "firecrawl"},
])
async def test_azure_refs_keep_the_open_web_canonical_contract_of_firecrawl_results(
    firecrawl_result: dict,
) -> None:
    firecrawl_ref = FirecrawlConnector().refs_from_results([firecrawl_result], QUERY)[0]
    azure_outcome = await AzureWebSearchClient(
        settings=azure_settings(), streaming_client=FakeStreamingClient(SUCCESS_LINES),
        direct_http=FakeDirectHttp("<main>Azure parity text. " * 20 + "</main>"),
    ).search(QUERY, limit=1)
    azure_ref = azure_outcome.refs[0]

    assert (azure_ref.source_id, azure_ref.origin, azure_ref.tier) == (
        firecrawl_ref.source_id, firecrawl_ref.origin, firecrawl_ref.tier,
    )
    for key in ("query", "search_backend", "result_snippet", "page_text", "text"):
        assert key in azure_ref.raw
        assert key in firecrawl_ref.raw
    assert azure_ref.raw["azure_consulted_sources"]
    assert azure_ref.raw["azure_url_citations"]


@pytest.mark.asyncio
async def test_unusable_azure_result_returns_no_ref_or_audit() -> None:
    outcome = await AzureWebSearchClient(
        settings=azure_settings(), streaming_client=FakeStreamingClient(UNUSABLE_LINES),
        direct_http=FakeDirectHttp("<main>Short.</main>"),
    ).search(QUERY, limit=1)
    assert outcome.ok is False
    assert outcome.refs == []
    assert outcome.audits == []
    assert outcome.reason == "native web search failed: no usable text"


@pytest.mark.asyncio
async def test_completed_search_without_an_attributable_url_is_a_typed_failure() -> None:
    payload = {
        "type": "response.completed",
        "response": {"output": [{"type": "web_search_call", "action": {"type": "search"}}]},
    }
    outcome = await AzureWebSearchClient(
        settings=azure_settings(), streaming_client=FakeStreamingClient([f"data: {json.dumps(payload)}"]),
        direct_http=FakeDirectHttp(""),
    ).search(QUERY, limit=1)

    assert outcome.ok is False
    assert outcome.refs == []
    assert outcome.reason == "native web search failed: no attributable URL"
