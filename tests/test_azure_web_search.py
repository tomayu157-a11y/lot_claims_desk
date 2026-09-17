from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import httpx
import pytest

from celestra.connectors.base import (
    HttpClient,
    _PinnedNetworkBackend,
    _resolve_public_destination,
)
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

    async def get_public_text(self, url: str) -> str:
        return await self.get_text(url)


class FakeRestrictedResponse:
    def __init__(self, status_code: int, text: str = "", location: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.headers = {"location": location} if location else {}
        self.request = httpx.Request("GET", "https://public.example/page")

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "status", request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


class FakeRestrictedClient:
    def __init__(self, responses: list[FakeRestrictedResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.is_closed = False

    async def get(self, url: str, *, follow_redirects: bool) -> FakeRestrictedResponse:
        assert follow_redirects is False
        self.urls.append(url)
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None


class RebindingRestrictedClient(FakeRestrictedClient):
    """Represents an HTTP stack that resolves the hostname again at connect time."""

    async def get(self, url: str, *, follow_redirects: bool) -> FakeRestrictedResponse:
        host = httpx.URL(url).host
        self.connect_addresses.append(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)[0][4][0])
        return await super().get(url, follow_redirects=follow_redirects)

    def __init__(self, responses: list[FakeRestrictedResponse]) -> None:
        super().__init__(responses)
        self.connect_addresses: list[str] = []


class FakePinnedRestrictedClient(FakeRestrictedClient):
    def __init__(self, destination, responses: list[FakeRestrictedResponse]) -> None:
        super().__init__(responses)
        self.destination = destination

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None


class RecordingNetworkBackend:
    def __init__(self) -> None:
        self.connections: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, **kwargs):
        self.connections.append((host, port))
        return object()

    async def connect_unix_socket(self, path, **kwargs):
        raise AssertionError("restricted hydration must not use Unix sockets")

    async def sleep(self, seconds) -> None:
        return None


def _address(host: str, address: str):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    return family, socket.SOCK_STREAM, 6, "", sockaddr


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
async def test_transport_failure_does_not_expose_provider_diagnostics_or_credentials() -> None:
    sentinel = "transport-secret-must-not-leak"
    stream = FakeStreamingClient(SUCCESS_LINES, failure=httpx.ConnectError(sentinel))
    statuses: list[str] = []

    outcome = await AzureWebSearchClient(
        settings=azure_settings(), streaming_client=stream, direct_http=FakeDirectHttp(""),
    ).search(QUERY, limit=1, on_status=statuses.append)

    user_visible = "\n".join([outcome.reason, *statuses, json.dumps(outcome.request_body)])
    assert outcome.reason == "native web search failed: connection failure"
    assert sentinel not in user_visible
    assert "test-key" not in user_visible


@pytest.mark.asyncio
async def test_normalization_uses_matching_citation_title_before_url_fallback() -> None:
    payload = {
        "type": "response.completed",
        "response": {"output": [
            {"type": "message", "content": [{"type": "output_text", "annotations": [
                {"type": "url_citation", "url": "https://example.org/citation-title",
                 "title": "Citation supplied title"},
            ]}]},
            {"type": "web_search_call", "action": {"type": "search", "query": QUERY}, "results": [
                {"type": "web_search_result", "url": "https://example.org/citation-title",
                 "title": "", "snippet": "Long enough result snippet. " * 10},
            ]},
        ]},
    }
    outcome = await AzureWebSearchClient(
        settings=azure_settings(), streaming_client=FakeStreamingClient([f"data: {json.dumps(payload)}"]),
        direct_http=FakeDirectHttp("<body>Direct page text without a title. " * 20 + "</body>"),
    ).search(QUERY, limit=1)

    assert outcome.ok is True
    assert outcome.refs[0].title == "Citation supplied title"


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
@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "169.254.1.1", "::1", "fc00::1", "fe80::1",
], ids=["ipv4-loopback", "ipv4-private", "ipv4-link-local", "ipv6-loopback", "ipv6-private", "ipv6-link-local"])
async def test_restricted_direct_fetch_rejects_non_public_resolved_destinations(monkeypatch, address: str) -> None:
    client = HttpClient()
    restricted = FakeRestrictedClient([FakeRestrictedResponse(200, "never fetched")])
    client._client = restricted
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [_address(host, address)])

    with pytest.raises(ValueError, match="public"):
        await client.get_public_text("https://public.example/page")

    assert restricted.urls == []


@pytest.mark.asyncio
async def test_restricted_direct_fetch_rejects_a_redirect_to_a_private_destination(monkeypatch) -> None:
    client = HttpClient()
    restricted = FakeRestrictedClient([
        FakeRestrictedResponse(302, location="http://private.example/internal"),
    ])
    client._client = restricted
    client._public_client_factory = lambda destination: restricted
    addresses = {
        "public.example": "93.184.216.34",
        "private.example": "10.0.0.1",
    }
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, **kwargs: [_address(host, addresses[host])],
    )

    with pytest.raises(ValueError, match="public"):
        await client.get_public_text("https://public.example/page")

    assert restricted.urls == ["https://public.example/page"]


@pytest.mark.asyncio
async def test_restricted_direct_fetch_accepts_a_public_https_destination(monkeypatch) -> None:
    client = HttpClient()
    restricted = FakeRestrictedClient([FakeRestrictedResponse(200, "Public page text")])
    client._client = restricted
    client._public_client_factory = lambda destination: restricted
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, **kwargs: [_address(host, "93.184.216.34")],
    )

    assert await client.get_public_text("https://public.example/page") == "Public page text"
    assert restricted.urls == ["https://public.example/page"]


@pytest.mark.asyncio
async def test_restricted_direct_fetch_pins_public_resolution_before_a_dns_rebind(monkeypatch) -> None:
    """Hydration must never give a validated hostname back to a re-resolving client."""
    client = HttpClient()
    rebinding = RebindingRestrictedClient([FakeRestrictedResponse(200, "must not be used")])
    client._client = rebinding
    calls = 0

    def resolve(host, port, **kwargs):
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if calls == 1 else "10.0.0.1"
        return [_address(host, address)]

    pinned_clients: list[FakePinnedRestrictedClient] = []

    def pinned_client(destination):
        fake = FakePinnedRestrictedClient(destination, [FakeRestrictedResponse(200, "Pinned public page")])
        pinned_clients.append(fake)
        return fake

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    client._public_client_factory = pinned_client

    assert await client.get_public_text("https://public.example/page") == "Pinned public page"
    assert calls == 1
    assert rebinding.connect_addresses == []
    assert pinned_clients[0].destination.hostname == "public.example"
    assert str(pinned_clients[0].destination.addresses[0]) == "93.184.216.34"
    assert pinned_clients[0].urls == ["https://public.example/page"]


@pytest.mark.asyncio
async def test_pinned_network_backend_connects_only_to_the_validated_numeric_address(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_address(host, "93.184.216.34")],
    )
    destination = _resolve_public_destination("https://public.example/page")
    backend = RecordingNetworkBackend()
    pinned = _PinnedNetworkBackend(destination, backend=backend)

    await pinned.connect_tcp("public.example", 443)

    assert backend.connections == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_restricted_direct_fetch_pins_each_public_https_redirect_target(monkeypatch) -> None:
    client = HttpClient()
    addresses = {
        "public.example": "93.184.216.34",
        "redirected.example": "2001:4860:4860::8888",
    }
    responses = [
        FakeRestrictedResponse(302, location="https://redirected.example/final"),
        FakeRestrictedResponse(200, "Redirected public page"),
    ]
    pinned_clients: list[FakePinnedRestrictedClient] = []

    def pinned_client(destination):
        fake = FakePinnedRestrictedClient(destination, [responses.pop(0)])
        pinned_clients.append(fake)
        return fake

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [_address(host, addresses[host])],
    )
    client._public_client_factory = pinned_client

    assert await client.get_public_text("https://public.example/start") == "Redirected public page"
    assert [item.destination.hostname for item in pinned_clients] == [
        "public.example", "redirected.example",
    ]
    assert [str(item.destination.addresses[0]) for item in pinned_clients] == [
        "93.184.216.34", "2001:4860:4860::8888",
    ]
    assert pinned_clients[0].urls == ["https://public.example/start"]
    assert pinned_clients[1].urls == ["https://redirected.example/final"]


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
