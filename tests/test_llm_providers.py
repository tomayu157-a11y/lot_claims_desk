"""Provider dispatch for the LLM layer.

Verifies each provider builds the right request without calling a real endpoint,
so a misconfigured provider fails loudly here rather than mid-run.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.services.llm import LLMUnavailable, _extract_json
from celestra.settings import Settings

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def build(**kw) -> Settings:
    base = dict(
        anthropic_api_key=None, foundry_api_key=None, foundry_resource=None,
        azure_openai_endpoint=None, azure_openai_api_key=None,
        azure_openai_deployment=None,
    )
    base.update(kw)
    return Settings(**base)


async def main() -> int:
    print("\n== provider readiness ==")
    s = build(llm_provider="anthropic", anthropic_api_key="sk-test")
    check("anthropic ready with a key", s.llm_enabled and not s.provider_gaps())
    check("anthropic model is opus 5 by default", s.active_model == "claude-opus-5",
          s.active_model)

    s = build(llm_provider="anthropic")
    check("anthropic without a key is not enabled", not s.llm_enabled)
    check("  and names the missing setting", s.provider_gaps() == ["ANTHROPIC_API_KEY"],
          str(s.provider_gaps()))

    s = build(llm_provider="anthropic_foundry", foundry_api_key="k", foundry_resource="r")
    check("foundry ready with key and resource", s.llm_enabled)
    check("  falls back to the anthropic model id", s.active_model == "claude-opus-5")
    s = build(llm_provider="anthropic_foundry", foundry_api_key="k",
              foundry_resource="r", foundry_model="claude-sonnet-5")
    check("  honours an explicit foundry model", s.active_model == "claude-sonnet-5")

    s = build(llm_provider="anthropic_foundry", foundry_api_key="k")
    check("foundry without a resource is not enabled", not s.llm_enabled)
    check("  and names it", s.provider_gaps() == ["FOUNDRY_RESOURCE"], str(s.provider_gaps()))

    s = build(llm_provider="azure_openai", azure_openai_endpoint="https://x.openai.azure.com",
              azure_openai_api_key="k", azure_openai_deployment="my-deployment")
    check("azure ready with endpoint, key and deployment", s.llm_enabled)
    check("  reports the deployment as the model", s.active_model == "my-deployment")

    s = build(llm_provider="azure_openai", azure_openai_endpoint="https://x.openai.azure.com")
    check("azure missing key and deployment is not enabled", not s.llm_enabled)
    check("  and names both",
          s.provider_gaps() == ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"],
          str(s.provider_gaps()))

    s = build(llm_provider="nonsense", anthropic_api_key="k")
    check("unknown provider is not enabled", not s.llm_enabled)
    check("  and says so", "not a supported provider" in s.provider_gaps()[0])

    print("\n== azure request shape ==")
    import celestra.services.llm as llm_mod

    client = llm_mod.LLMClient()
    client._settings = build(
        llm_provider="azure_openai",
        azure_openai_endpoint="https://contoso.openai.azure.com/",
        azure_openai_api_key="secret", azure_openai_deployment="gpt-deploy",
        azure_openai_api_version="2024-10-21",
    )

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def raise_for_status(self): ...

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    class FakeClient:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            captured.update(url=url, json=json, headers=headers)
            return FakeResponse()

    import httpx
    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient  # type: ignore[misc]
    try:
        out = await client.complete_json("SYS", "PROMPT")
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    check("azure builds the deployment url",
          captured["url"] == "https://contoso.openai.azure.com/openai/deployments/"
                             "gpt-deploy/chat/completions?api-version=2024-10-21",
          captured["url"])
    check("azure authenticates with the api-key header",
          captured["headers"]["api-key"] == "secret")
    check("azure sends system then user",
          [m["role"] for m in captured["json"]["messages"]] == ["system", "user"])
    check("azure parses the response as json", out == {"ok": True}, str(out))

    print("\n== json recovery ==")
    check("plain json", _extract_json('{"a": 1}') == {"a": 1})
    check("fenced json", _extract_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("json wrapped in prose", _extract_json('Sure!\n{"a": 1}\nDone.') == {"a": 1})
    try:
        _extract_json("no json at all")
        check("unparseable input raises", False)
    except LLMUnavailable:
        check("unparseable input raises", True)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
