"""Azure Responses native web-search boundary with canonical source output."""
from __future__ import annotations

import inspect
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from ..connectors._util import clean, clip
from ..connectors.base import http
from ..models import EvidenceOrigin, SourceRef
from ..settings import Settings, get_settings
from .web_page_hydration import hydrate_web_page

_PROVIDER = "azure_web_search"


class AzureWebSearchParseError(ValueError):
    """A provider payload cannot safely become canonical open-web evidence."""


@dataclass(frozen=True)
class AzureResponsesStream:
    query: str
    queries: list[str]
    results: list[dict[str, Any]]
    consulted_sources: list[dict[str, Any]]
    url_citations: list[dict[str, Any]]
    tool_calls: int


@dataclass(frozen=True)
class WebSourceAudit:
    """Provider audit facts that accompany a normalized public-web source."""

    provider: str
    url: str
    queries: list[str]
    hydration_status: str
    consulted_sources: list[dict[str, Any]]
    url_citations: list[dict[str, Any]]


@dataclass
class AzureWebSearchOutcome:
    refs: list[SourceRef] = field(default_factory=list)
    audits: list[WebSourceAudit] = field(default_factory=list)
    ok: bool = False
    reason: str = ""
    tool_calls: int = 0
    request_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, reason: str, *, request_body: dict[str, Any] | None = None) -> AzureWebSearchOutcome:
        return cls(reason=f"native web search failed: {reason}", request_body=request_body or {})


def _records(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise AzureWebSearchParseError("malformed native stream") from exc
        if not isinstance(payload, dict):
            raise AzureWebSearchParseError("malformed native stream")
        yield payload


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _annotations(output: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    for item in output:
        if item.get("type") != "message":
            continue
        for content in _as_dicts(item.get("content")):
            for annotation in _as_dicts(content.get("annotations")):
                if annotation.get("type") == "url_citation" and clean(annotation.get("url")):
                    annotations.append(annotation)
    return annotations


def parse_responses_stream(lines: Iterable[str]) -> AzureResponsesStream:
    """Parse only the completed Azure Responses payload, keyed by item type."""
    for payload in _records(lines):
        event_type = clean(payload.get("type"))
        if event_type in {"error", "response.failed", "response.incomplete"}:
            raise AzureWebSearchParseError("native provider reported an error")
        if event_type != "response.completed":
            continue
        response = payload.get("response")
        if not isinstance(response, dict):
            raise AzureWebSearchParseError("malformed completed response")
        output = _as_dicts(response.get("output"))
        calls = [item for item in output if item.get("type") == "web_search_call"]
        if not calls:
            raise AzureWebSearchParseError("completed response has no web_search_call")

        queries: list[str] = []
        results: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        for call in calls:
            action = call.get("action") if isinstance(call.get("action"), dict) else {}
            query = clean(action.get("query"))
            if query and query not in queries:
                queries.append(query)
            for candidate in action.get("queries") or []:
                candidate = clean(candidate)
                if candidate and candidate not in queries:
                    queries.append(candidate)
            results.extend(_as_dicts(call.get("results")))
            sources.extend(_as_dicts(action.get("sources")))

        return AzureResponsesStream(
            query=queries[0] if queries else "",
            queries=queries,
            results=results,
            consulted_sources=sources,
            url_citations=_annotations(output),
            tool_calls=len(calls),
        )
    raise AzureWebSearchParseError("stream ended before a completed response")


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _url(value: Any) -> str:
    url = clean(value)
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    return url


def _by_url(records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if url := _url(record.get("url")):
            grouped.setdefault(url, []).append(record)
    return grouped


def _safe_failure_reason(exc: Exception) -> str:
    """Map transport failures to a finite UI-safe vocabulary."""
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if 400 <= status < 500:
            return "request rejected"
        if 500 <= status < 600:
            return "provider unavailable"
        return "provider request failed"
    if isinstance(exc, (httpx.TransportError, OSError)):
        return "connection failure"
    if isinstance(exc, ValueError):
        return "invalid provider response"
    return "provider request failed"


async def normalize_azure_sources(
    stream: AzureResponsesStream,
    *,
    limit: int,
    http_client=None,
) -> tuple[list[SourceRef], list[WebSourceAudit]]:
    """Hydrate Azure URLs then return only canonical open-web source refs."""
    results = _by_url(stream.results)
    consulted = _by_url(stream.consulted_sources)
    citations = _by_url(stream.url_citations)
    urls = list(dict.fromkeys([*results, *consulted, *citations]))[:max(0, limit)]
    refs: list[SourceRef] = []
    audits: list[WebSourceAudit] = []
    for url in urls:
        result = results.get(url, [{}])[0]
        snippet = clean(result.get("snippet"))
        hydration = await hydrate_web_page(url, snippet=snippet, http_client=http_client)
        if hydration.status == "unusable":
            continue
        source_audit = consulted.get(url, [])
        citation_audit = citations.get(url, [])
        citation_title = next((clean(citation.get("title")) for citation in citation_audit
                               if clean(citation.get("title"))), "")
        title = clean(result.get("title")) or clean(hydration.title) or citation_title or url
        text = hydration.text[:20_000]
        refs.append(SourceRef(
            source_id="open_web",
            source_name="Open Web (Supplementary)",
            tier=5,
            url=url,
            title=title,
            organization="Open web",
            identifiers={"domain": _domain(url), "search_backend": _PROVIDER},
            snippet=clip(text, 1500),
            raw={
                "query": stream.query,
                "search_backend": _PROVIDER,
                "result_snippet": snippet,
                "page_text": text,
                "text": text,
                "azure_consulted_sources": source_audit,
                "azure_url_citations": citation_audit,
            },
            origin=EvidenceOrigin.OPEN_WEB,
        ))
        audits.append(WebSourceAudit(
            provider=_PROVIDER,
            url=url,
            queries=list(stream.queries),
            hydration_status=hydration.status,
            consulted_sources=source_audit,
            url_citations=citation_audit,
        ))
    return refs, audits


async def _notify(on_status, message: str) -> None:
    if on_status is None:
        return
    result = on_status(message)
    if inspect.isawaitable(result):
        await result


class AzureWebSearchClient:
    """The sole provider-specific boundary for Azure's native web search."""

    def __init__(self, *, settings: Settings | None = None, streaming_client=None, direct_http=None) -> None:
        self.settings = settings or get_settings()
        self._streaming_client = streaming_client
        self._direct_http = direct_http

    def _request_body(self, search_brief: str) -> dict[str, Any]:
        return {
            "model": self.settings.azure_openai_deployment or "",
            "tools": [{"type": "web_search"}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources", "web_search_call.results"],
            "input": search_brief,
            "stream": True,
            "store": False,
        }

    def _unavailable_reason(self) -> str:
        if self.settings.provider != "azure_openai":
            return "Azure OpenAI is not the configured provider"
        if not (self.settings.azure_openai_endpoint and self.settings.azure_openai_api_key
                and self.settings.azure_openai_deployment):
            return "Azure OpenAI endpoint, key, or deployment is not configured"
        return ""

    async def search(self, search_brief: str, limit: int, on_status=None) -> AzureWebSearchOutcome:
        """Run the native tool and return canonical sources, never raw SSE data."""
        request_body = self._request_body(search_brief)
        if reason := self._unavailable_reason():
            return AzureWebSearchOutcome.failure(reason, request_body=request_body)
        await _notify(on_status, "Native web research started")
        endpoint = (self.settings.azure_openai_endpoint or "").rstrip("/")
        url = f"{endpoint}/openai/v1/responses"
        try:
            client = self._streaming_client or await http.client()
            lines: list[str] = []
            async with client.stream(
                "POST", url,
                headers={"api-key": self.settings.azure_openai_api_key or ""},
                json=request_body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    lines.append(line)
            stream = parse_responses_stream(lines)
            attributable = [*stream.results, *stream.consulted_sources, *stream.url_citations]
            if not any(_url(item.get("url")) for item in attributable):
                return AzureWebSearchOutcome.failure("no attributable URL", request_body=request_body)
            refs, audits = await normalize_azure_sources(
                stream, limit=limit, http_client=self._direct_http,
            )
        except AzureWebSearchParseError as exc:
            return AzureWebSearchOutcome.failure(str(exc), request_body=request_body)
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            return AzureWebSearchOutcome.failure(
                _safe_failure_reason(exc), request_body=request_body,
            )

        if not refs:
            return AzureWebSearchOutcome.failure("no usable text", request_body=request_body)
        await _notify(on_status, "Native web research found usable sources")
        return AzureWebSearchOutcome(
            refs=refs,
            audits=audits,
            ok=True,
            tool_calls=stream.tool_calls,
            request_body=request_body,
        )
