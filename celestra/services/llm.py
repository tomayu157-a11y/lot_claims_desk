"""LLM access with an honest degraded mode.

The app must run end to end without an API key, so every caller of this module
is required to have a deterministic fallback. `available` tells callers which
path to take; it is surfaced in the UI so nobody mistakes rule-based synthesis
for model-written synthesis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from ..settings import get_settings

log = logging.getLogger("celestra.llm")


class LLMUnavailable(RuntimeError):
    """Raised when no API key is configured, or the provider call failed."""


def _extract_json(text: str) -> Any:
    """Models sometimes wrap JSON in prose or a fenced block. Recover it."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    raise LLMUnavailable("model did not return parseable JSON")


class LLMClient:
    """One interface over three providers.

    Anthropic and Microsoft Foundry both speak the Messages API and use the
    official SDK. Azure OpenAI speaks a different wire format and has no
    Anthropic SDK, so it goes over HTTP. Callers see the same two methods and
    the same LLMUnavailable failure in every case.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: Any = None
        self._sem = asyncio.Semaphore(self._settings.llm_max_concurrency)

    @property
    def available(self) -> bool:
        return self._settings.llm_enabled

    @property
    def provider(self) -> str:
        return self._settings.provider

    @property
    def model(self) -> str:
        return self._settings.active_model

    def describe(self) -> str:
        if not self.available:
            gaps = ", ".join(self._settings.provider_gaps())
            return f"not configured ({self.provider}: missing {gaps})"
        return f"{self.provider} · {self.model}"

    # -- provider clients ------------------------------------------------
    def _anthropic(self) -> Any:
        if self._client is None:
            s = self._settings
            if s.provider == "anthropic_foundry":
                from anthropic import AnthropicFoundry

                self._client = AnthropicFoundry(
                    api_key=s.foundry_api_key,
                    resource=s.foundry_resource,
                    timeout=float(s.llm_timeout_seconds),
                )
            else:
                from anthropic import AsyncAnthropic

                self._client = AsyncAnthropic(
                    api_key=s.anthropic_api_key,
                    base_url=s.anthropic_base_url,
                    timeout=float(s.llm_timeout_seconds),
                )
        return self._client

    async def _complete_anthropic(
        self, system: str, prompt: str, max_tokens: int
    ) -> str:
        client = self._anthropic()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            # Synthesis and extraction are judgement work, so let the model
            # decide how much reasoning each call needs.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self._settings.llm_effort},
        }
        result = client.messages.create(**kwargs)
        msg = await result if hasattr(result, "__await__") else result
        # Thinking blocks carry .thinking, not .text, so this yields the answer only.
        return "".join(getattr(b, "text", "") or "" for b in msg.content)

    async def _complete_azure(self, system: str, prompt: str, max_tokens: int) -> str:
        """Azure AI Foundry / Azure OpenAI deployment over its REST API."""
        import httpx

        s = self._settings
        endpoint = (s.azure_openai_endpoint or "").rstrip("/")
        url = (
            f"{endpoint}/openai/deployments/{s.azure_openai_deployment}"
            f"/chat/completions?api-version={s.azure_openai_api_version}"
        )
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_completion_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=float(s.llm_timeout_seconds)) as http:
            resp = await http.post(
                url,
                json=payload,
                headers={"api-key": s.azure_openai_api_key or "", "Content-Type": "application/json"},
            )
            if resp.status_code == 400 and "max_completion_tokens" in resp.text:
                # Older Azure API versions and non-reasoning deployments still
                # take max_tokens; retry once rather than failing the run.
                payload["max_tokens"] = payload.pop("max_completion_tokens")
                resp = await http.post(
                    url,
                    json=payload,
                    headers={"api-key": s.azure_openai_api_key or "",
                             "Content-Type": "application/json"},
                )
            resp.raise_for_status()
            data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMUnavailable("azure deployment returned no choices")
        return str((choices[0].get("message") or {}).get("content") or "")

    # -- public API ------------------------------------------------------
    async def complete(self, system: str, prompt: str, *, max_tokens: int | None = None) -> str:
        if not self.available:
            raise LLMUnavailable(
                f"{self.provider} is not configured: missing "
                + ", ".join(self._settings.provider_gaps())
            )
        budget = max_tokens or self._settings.llm_max_tokens
        async with self._sem:
            try:
                if self.provider == "azure_openai":
                    return await self._complete_azure(system, prompt, budget)
                return await self._complete_anthropic(system, prompt, budget)
            except LLMUnavailable:
                raise
            except Exception as exc:  # a provider error must not kill a run
                log.warning("llm call failed (%s): %s", self.provider, exc)
                raise LLMUnavailable(f"{self.provider}: {exc}") from exc

    async def complete_json(
        self, system: str, prompt: str, *, max_tokens: int | None = None
    ) -> Any:
        text = await self.complete(
            system + "\n\nRespond with JSON only. No prose, no code fence.",
            prompt,
            max_tokens=max_tokens,
        )
        return _extract_json(text)


llm = LLMClient()
