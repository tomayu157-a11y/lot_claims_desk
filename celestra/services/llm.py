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
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: Any = None
        self._sem = asyncio.Semaphore(self._settings.llm_max_concurrency)

    @property
    def available(self) -> bool:
        return self._settings.llm_enabled

    @property
    def model(self) -> str:
        return self._settings.llm_model

    def _ensure(self) -> Any:
        if not self.available:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not configured")
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(
                api_key=self._settings.anthropic_api_key,
                base_url=self._settings.anthropic_base_url,
                timeout=float(self._settings.llm_timeout_seconds),
            )
        return self._client

    async def complete(self, system: str, prompt: str, *, max_tokens: int | None = None) -> str:
        client = self._ensure()
        async with self._sem:
            try:
                msg = await client.messages.create(
                    model=self._settings.llm_model,
                    max_tokens=max_tokens or self._settings.llm_max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
            except Exception as exc:  # provider errors must not crash a run
                log.warning("llm call failed: %s", exc)
                raise LLMUnavailable(str(exc)) from exc
        return "".join(getattr(b, "text", "") for b in msg.content)

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
