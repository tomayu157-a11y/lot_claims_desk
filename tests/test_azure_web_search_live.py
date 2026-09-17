from __future__ import annotations

import os

import pytest

from celestra.services.azure_web_search import AzureWebSearchClient

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_AZURE_WEB_SEARCH") != "1",
    reason="set RUN_LIVE_AZURE_WEB_SEARCH=1 to run the Azure contract test",
)


@pytest.mark.asyncio
async def test_azure_native_web_search_live_contract() -> None:
    outcome = await AzureWebSearchClient().search(
        "official definition of administrative healthcare claims data", limit=1,
    )
    assert outcome.ok is True
    assert outcome.request_body["store"] is False
    assert outcome.tool_calls >= 1
    assert outcome.refs and outcome.refs[0].url
    assert outcome.audits
    assert any(audit.url_citations for audit in outcome.audits)
