"""Web search connector: endpoint configuration, v1/v2 payload parsing, and
transport errors explained in words a person can act on."""
from __future__ import annotations

import asyncio
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from celestra.connectors import base as base_mod
from celestra.connectors.base import describe_http_error, explain_transport_error, remedy_for
from celestra.connectors.firecrawl import (
    FirecrawlConnector, firecrawl_blocked, firecrawl_status, reset_firecrawl_status,
)
from celestra.settings import Settings, get_settings

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    print("\n== endpoint configuration ==")
    s = Settings(firecrawl_api_key="k")
    check("v2 by default", s.firecrawl_endpoint("search") == "https://api.firecrawl.dev/v2/search",
          s.firecrawl_endpoint("search"))
    s = Settings(firecrawl_api_key="k", firecrawl_api_version="v1")
    check("v1 selectable", s.firecrawl_endpoint("scrape") == "https://api.firecrawl.dev/v1/scrape")
    s = Settings(firecrawl_api_key="k", firecrawl_api_url="https://fc.internal.example/v1/")
    check("pasted version suffix wins", s.firecrawl_endpoint("search") == "https://fc.internal.example/v1/search",
          s.firecrawl_endpoint("search"))
    s = Settings(firecrawl_api_key="k", firecrawl_api_version="bogus")
    check("unknown version falls back to v2", s.firecrawl_version == "v2")
    default_verify = Settings(ca_bundle=None, ssl_cert_file=None, requests_ca_bundle=None).tls_verify_value()
    check("tls verify defaults to system certs", default_verify is True, str(default_verify))
    check("CA_BUNDLE is used when set", Settings(ca_bundle="/tmp/root.pem").tls_verify_value() == "/tmp/root.pem")
    check("TLS_VERIFY=false disables", Settings(tls_verify=False).tls_verify_value() is False)

    print("\n== payload parsing ==")
    v1 = {"success": True, "data": [
        {"url": "https://a.example/x", "title": "A", "description": "desc a"},
        {"url": "", "title": "no url"},
    ]}
    v2 = {"success": True, "data": {
        "web": [{"url": "https://b.example/y", "title": "B", "description": "desc b",
                 "markdown": "# B"}],
        "news": [{"url": "https://n.example", "title": "N"}],
    }}
    p1 = FirecrawlConnector.parse_search_payload(v1)
    p2 = FirecrawlConnector.parse_search_payload(v2)
    check("v1 list shape parsed", [r["url"] for r in p1] == ["https://a.example/x"])
    check("v2 object shape parsed, web only", [r["url"] for r in p2] == ["https://b.example/y"])
    check("v2 markdown carried", p2[0]["markdown"] == "# B")
    try:
        FirecrawlConnector.parse_search_payload({"success": False, "error": "Invalid token"})
        check("success=false raises", False)
    except RuntimeError as exc:
        check("success=false raises with the API's message", "Invalid token" in str(exc))
    check("garbage is empty, not an error", FirecrawlConnector.parse_search_payload("nope") == [])

    print("\n== transport errors explained ==")
    def connect_error(cause: BaseException) -> httpx.ConnectError:
        err = httpx.ConnectError("")
        err.__cause__ = cause
        return err

    ssl_err = connect_error(ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"))
    what, fix = explain_transport_error(ssl_err)
    check("TLS interception is named", what.startswith("TLS certificate verification failed"), what)
    check("  and CA_BUNDLE is the fix", "CA_BUNDLE" in fix)
    check("  describe_http_error no longer says just ConnectError",
          describe_http_error(ssl_err) != "ConnectError" and "TLS" in describe_http_error(ssl_err))
    dns_err = connect_error(OSError("[Errno 11001] getaddrinfo failed"))
    what, fix = explain_transport_error(dns_err)
    check("DNS failure is named", what.startswith("DNS lookup failed"), what)
    check("  and a proxy is suggested", "PROXY" in fix)
    refused = connect_error(OSError("[Errno 111] Connection refused"))
    check("refused connection names the firewall", "refused" in explain_transport_error(refused)[0])
    check("timeout explained", "timed out" in explain_transport_error(httpx.ConnectTimeout("x"))[0])
    resp = httpx.Response(402, request=httpx.Request("POST", "https://api.firecrawl.dev/v2/search"))
    e402 = httpx.HTTPStatusError("402", request=resp.request, response=resp)
    check("402 is out of credits", "credits" in describe_http_error(e402) and "credits" in remedy_for(e402))
    resp = httpx.Response(401, request=resp.request)
    e401 = httpx.HTTPStatusError("401", request=resp.request, response=resp)
    check("401 is a rejected key with a remedy", "rejected" in describe_http_error(e401) and "dashboard" in remedy_for(e401))

    print("\n== search falls back and records the cause ==")
    reset_firecrawl_status()
    async def failing_request(*a, **kw):
        raise ssl_err

    real = base_mod.http.request
    base_mod.http.request = failing_request
    get_settings.cache_clear()
    import os
    os.environ["FIRECRAWL_API_KEY"] = "fc-test"
    try:
        conn = FirecrawlConnector()
        results = asyncio.run(conn.search("chronic lymphocytic leukemia incidence", 3))
        check("search never raises", isinstance(results, list))
        check("last_error names the cause and the fix",
              "TLS" in conn.last_error and "CA_BUNDLE" in conn.last_error, conn.last_error)
        check("failure recorded for the UI", "TLS" in firecrawl_status["error"] and firecrawl_status["at"])
        probe = asyncio.run(FirecrawlConnector.probe())
        check("probe reports the same", probe["ok"] is False and "CA_BUNDLE" in probe["remedy"], probe["detail"])
        check("probe names the endpoint", probe["endpoint"].endswith("/v2/search"))
        check("two transport failures switch web search off for the session",
              bool(firecrawl_blocked()) and "off" in firecrawl_blocked(), firecrawl_blocked()[:80])
        calls_before = firecrawl_status["calls"]
        results = asyncio.run(FirecrawlConnector().search("another question", 3))
        check("a blocked session makes no further call",
              results == [] and firecrawl_status["calls"] == calls_before
              and firecrawl_status["skipped"] >= 1)

        print("\n== no credits stops at once ==")
        reset_firecrawl_status()
        resp402 = httpx.Response(402, request=httpx.Request("POST", "https://api.firecrawl.dev/v2/search"),
                                 json={"error": "Insufficient credits"})
        async def no_credits(*a, **kw):
            raise httpx.HTTPStatusError("402", request=resp402.request, response=resp402)
        base_mod.http.request = no_credits
        results = asyncio.run(FirecrawlConnector().search("chronic lymphocytic leukemia staging", 3))
        check("first 402 blocks the session", "credits" in firecrawl_blocked(), firecrawl_blocked()[:80])
        check("status carries the reason for the UI", "402" in firecrawl_status["error"])
        d = asyncio.run(FirecrawlConnector().discover(
            __import__("celestra.connectors.base", fromlist=["RetrievalContext"]).RetrievalContext(
                indication="CLL", indication_key="CLL", synonyms=[], geography="US",
                population="", stage="stage_1", question="staging", aspects=[], cutoff=""), 3))
        check("a domain search reports the block instead of calling", d.ok is False and "credits" in d.reason)

        print("\n== one call brings back the pages ==")
        reset_firecrawl_status()
        seen_bodies = []
        async def v2_search(method, url, *, json_body=None, **kw):
            seen_bodies.append(json_body)
            return {"success": True, "data": {"web": [
                {"url": "https://cancer.gov/a", "title": "CLL staging", "description": "Rai and Binet staging",
                 "markdown": "# A\n" + "Staging uses Rai and Binet systems. " * 20}]}}
        base_mod.http.request = v2_search
        conn = FirecrawlConnector()
        refs = asyncio.run(conn.search_refs("cll staging", 3))
        check("search asks for page content in the same call",
              seen_bodies and seen_bodies[-1].get("scrapeOptions", {}).get("formats") == ["markdown"])
        check("search asks only for the pages it will read", seen_bodies[-1]["limit"] == 3)
        check("result carries the page text", refs and "Rai and Binet" in refs[0].raw["markdown"])
        page = asyncio.run(conn.scrape(refs[0].url, refs[0].raw["markdown"]))
        check("scrape with markdown in hand makes no call",
              page.get("backend") == "firecrawl" and len(seen_bodies) == 1)
        asyncio.run(conn.search_refs("cll staging", 3))
        check("the same query is not searched twice", len(seen_bodies) == 1)
    finally:
        base_mod.http.request = real
        os.environ.pop("FIRECRAWL_API_KEY", None)
        get_settings.cache_clear()
        reset_firecrawl_status()

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
