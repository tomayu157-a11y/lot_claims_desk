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
from ..settings import DATA_DIR, configure_tls, get_settings, system_certs_active

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
            s = get_settings()
            configure_tls()
            verify: Any = s.tls_verify_value()
            if verify is True and system_certs_active():
                import ssl  # noqa: PLC0415

                import truststore  # noqa: PLC0415

                verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            if verify is False:
                log.warning("TLS_VERIFY=false: certificate verification is disabled for "
                            "every outbound call. Use CA_BUNDLE instead where possible.")
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout, connect=min(self.timeout, 15)),
                "follow_redirects": True,
                "headers": {"User-Agent": USER_AGENT},
                "verify": verify,
                "trust_env": True,          # HTTPS_PROXY / NO_PROXY / SSL_CERT_FILE
            }
            if s.proxy_url:
                kwargs["proxy"] = s.proxy_url
            self._client = httpx.AsyncClient(**kwargs)
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


def _root_cause(exc: BaseException) -> str:
    """The deepest message in the exception chain. httpx wraps an SSL or socket
    error in ConnectError with an empty message; the useful text is below it."""
    seen: list[str] = []
    cur: BaseException | None = exc
    while cur is not None and len(seen) < 6:
        text = str(cur).strip()
        if text and text not in seen:
            seen.append(text)
        cur = cur.__cause__ or cur.__context__
    return seen[-1] if seen else type(exc).__name__


def explain_transport_error(exc: Exception) -> tuple[str, str]:
    """(what went wrong, what to do about it) for a connection-level failure.

    'ConnectError' on its own has sent more than one person to check an API
    key that was fine. The cause is almost always the network the process is
    on, and each cause has a different fix.
    """
    cause = _root_cause(exc)
    low = cause.lower()
    if "certificate_verify_failed" in low or "ssl" in low and "verif" in low:
        if system_certs_active():
            remedy = (
                "Something on this network intercepts HTTPS and its certificate is not "
                "trusted even by the operating system store. Export the proxy's root "
                "certificate as PEM and set CA_BUNDLE=/path/to/bundle.pem in .env. "
                "TLS_VERIFY=false disables checking entirely, as a last resort."
            )
        else:
            remedy = (
                "Something on this network intercepts HTTPS (a corporate proxy or firewall). "
                "Run `pip install -r requirements.txt` so the `truststore` package is "
                "installed: Celestra then verifies against the operating system's "
                "certificate store, which already trusts the proxy (that is why the "
                "browser works). Otherwise export the proxy's root certificate as PEM and "
                "set CA_BUNDLE=/path/to/bundle.pem. TLS_VERIFY=false is the last resort."
            )
        return (f"TLS certificate verification failed ({cause[:160]})", remedy)
    if "ssl" in low or "tls" in low or "handshake" in low:
        return (
            f"TLS handshake failed ({cause[:160]})",
            "A proxy or firewall is interfering with HTTPS. Set CA_BUNDLE to the "
            "network's root certificate, or PROXY_URL if a proxy is required.",
        )
    if ("getaddrinfo" in low or "name or service not known" in low
            or "nodename nor servname" in low or "temporary failure in name resolution" in low
            or "no address associated" in low):
        return (
            f"DNS lookup failed ({cause[:160]})",
            "This process cannot resolve internet hostnames. Check the machine is online, "
            "and if it reaches the web only through a proxy set HTTPS_PROXY or PROXY_URL.",
        )
    if "proxy" in low or "407" in low or "tunnel" in low:
        return (
            f"proxy refused the connection ({cause[:160]})",
            "Check HTTPS_PROXY / PROXY_URL, including credentials if the proxy needs them.",
        )
    if "refused" in low or "unreachable" in low or "no route" in low or "reset" in low:
        return (
            f"connection refused or reset ({cause[:160]})",
            "The host is blocked from this network. Ask for api.firecrawl.dev to be "
            "allowed through the firewall, or set PROXY_URL to a proxy that can reach it.",
        )
    if isinstance(exc, httpx.TimeoutException) or "timed out" in low or "timeout" in low:
        return (
            "connection timed out",
            "The host did not answer. A firewall that drops packets silently looks like "
            "this; try HTTPS_PROXY / PROXY_URL, or raise connector_timeout_seconds.",
        )
    return (
        f"connection failed ({cause[:160]})",
        "Check that this machine can reach the internet from a terminal "
        "(curl https://api.firecrawl.dev) and set HTTPS_PROXY / CA_BUNDLE as needed.",
    )


def describe_http_error(exc: Exception) -> str:
    """Turn a transport exception into a reason string the UI can show."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "credentials rejected (401)"
    if status == 402:
        return "payment required (402): the account has no credits"
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
    if isinstance(exc, (httpx.TransportError, OSError)):
        return explain_transport_error(exc)[0]
    return f"{type(exc).__name__}: {str(exc)[:120]}" if str(exc) else type(exc).__name__


def remedy_for(exc: Exception) -> str:
    """What to do about a failure, when there is something to do."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "The API key was rejected. Copy it again from the provider's dashboard into .env."
    if status == 402:
        return "The account is out of credits. Top it up on the provider's dashboard."
    if status == 403:
        return "The key is valid but not allowed to call this endpoint; check the plan."
    if status == 429:
        return "Rate limited. Wait, or lower max_concurrent_connectors in thresholds.yaml."
    if status:
        return ""
    if isinstance(exc, (httpx.TransportError, OSError)):
        return explain_transport_error(exc)[1]
    return ""
