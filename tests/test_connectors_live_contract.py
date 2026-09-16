from celestra.connectors.base import ConnectorResult
from tests.test_connectors_live import check


def test_configured_optional_connector_is_not_expected_to_be_blocked():
    result = ConnectorResult(source_id="loinc", ok=True, reason="no results")

    problems = check("ALL", {"loinc": result}, configured_sources={"loinc"})

    assert problems == []


def test_explicitly_ignored_live_source_does_not_fail_the_contract():
    result = ConnectorResult.failure(
        "open_web", "Firecrawl credits are exhausted (402).",
    )

    problems = check("ALL", {"open_web": result}, ignored_sources={"open_web"})

    assert problems == []
