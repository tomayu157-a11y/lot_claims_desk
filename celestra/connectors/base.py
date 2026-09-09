"""Connector protocol plus the shared HTTP client.

Every source adapter implements `discover` and, where the source supports it,
`hydrate`. A connector never raises to the caller: it returns a ConnectorResult
carrying either refs or a failure reason, so one dead source can never take
down a run. The orchestrator uses that reason to decide whether to refine the
query or escalate to open-web fallback.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..models import EvidenceOrigin, SourceRef
from ..settings import DATA_DIR, get_settings

log = logging.getLogger("celestra.connector")

USER_AGENT = "Celestra-DeskResearch/1.0 (clinical desk research; contact research@example.org)"


@dataclass
class ConnectorResult:
    source_id: str
    refs: list[SourceRef] = field(default_factory=list)
    ok: bool = True
    reason: str = ""
    calls: int = 0
    elapsed_ms: int = 0

    @property
    def count(self) -> int:
        return len(self.refs)

    @classmethod
    def failure(cls, source_id: str, reason: str) -> "ConnectorResult":
        return cls(source_id=source_id, ok=False, reason=reason)


@dataclass
class RetrievalContext:
    """What a connector needs to know about the question being answered."""
    indication: str
    indication_key: str
    synonyms: list[str]
    geography: str
    population: str
    stage: str
    question: str
    aspects: list[str] = field(default_factory=list)
    cutoff: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def term(self) -> str:
        return self.indication

    def or_terms(self) -> list[str]:
        seen, out = set(), []
        for t in [self.indication, *self.synonyms]:
            k = t.lower().strip()
            if k and k not in seen:
                seen.add(k)
                out.append(t)
        return out


class Connector(Protocol):
    source_id: str
    tier: int
    origin: EvidenceOrigin

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult: ...


class _DiskCache:
    """Content-addressed response cache. Keeps repeat runs fast and polite to
    rate-limited public APIs."""

    def __init__(self, root: Path, ttl: int) -> None:
        self.root = root
        self.ttl = ttl
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> Any | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            if time.time() - p.stat().st_mtime > self.ttl:
                return None
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def set(self, key: str, value: Any) -> None:
        try:
            self._path(key).write_text(json.dumps(value), encoding="utf-8")
        except (OSError, TypeError):
            pass


class HttpClient:
    """Shared async HTTP client with retry, backoff, caching and a concurrency
    gate. One instance is reused for the process lifetime."""

    def __init__(self) -> None:
        s = get_settings()
        limits = get_settings()
        self._settings = s
        from ..settings import get_thresholds
        th = get_thresholds()["limits"]
        self.timeout = th["connector_timeout_seconds"]
        self.retries = th["connector_retries"]
        self._sem = asyncio.Semaphore(th["max_concurrent_connectors"])
        self._cache = _DiskCache(DATA_DIR / "cache", th["http_cache_ttl_seconds"])
        self._client: httpx.AsyncClient | None = None
        del limits

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @staticmethod
    def _key(method: str, url: str, params: Any, body: Any) -> str:
        blob = json.dumps([method, url, params, body], sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        as_json: bool = True,
        use_cache: bool = True,
    ) -> Any:
        """Returns parsed JSON or text. Raises on final failure so the calling
        connector can translate it into a ConnectorResult reason."""
        key = self._key(method, url, params, json_body or data)
        if use_cache and method.upper() == "GET":
            hit = self._cache.get(key)
            if hit is not None:
                return hit

        last_exc: Exception | None = None
        async with self._sem:
            client = await self.client()
            for attempt in range(self.retries + 1):
                try:
                    resp = await client.request(
                        method, url, params=params, json=json_body,
                        data=data, headers=headers,
                    )
                    if resp.status_code in (429, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError(
                            f"retryable {resp.status_code}", request=resp.request, response=resp
                        )
                    resp.raise_for_status()
                    out = resp.json() if as_json else resp.text
                    if use_cache and method.upper() == "GET":
                        self._cache.set(key, out)
                    return out
                except (httpx.HTTPError, ValueError) as exc:
                    last_exc = exc
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in (401, 403, 404) or attempt == self.retries:
                        break
                    await asyncio.sleep(0.5 * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    async def get_json(self, url: str, **kw: Any) -> Any:
        return await self.request("GET", url, **kw)

    async def get_text(self, url: str, **kw: Any) -> str:
        kw["as_json"] = False
        return await self.request("GET", url, **kw)


http = HttpClient()


def describe_http_error(exc: Exception) -> str:
    """Turn a transport exception into a reason string the UI can show."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "credentials required (401)"
    if status == 403:
        return "access forbidden (403)"
    if status == 404:
        return "not found (404)"
    if status == 429:
        return "rate limited (429)"
    if status:
        return f"HTTP {status}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return type(exc).__name__
