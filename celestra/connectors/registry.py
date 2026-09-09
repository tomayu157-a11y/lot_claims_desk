"""Connector registry.

`build_registry()` turns config/sources.yaml into live connector instances:
tier, display name and domain always come from the YAML, never from a constant
in code, so adding or re-tiering a source is a config change.

Three families are assembled by access_method rather than by a `connector:`
key:

* targeted_search  -> a domain-scoped FirecrawlConnector carrying the source's
  own registry tier and origin=TARGETED_SEARCH (not tier 5).
* licensed         -> LicensedConnector, which always fails with
  "licensed connector not configured" so the UI shows the blocker honestly
  instead of silently dropping NCCN or AMA CPT from the source list.
* local_file       -> LocalFilesConnector bound to the source's local_dataset.
"""
from __future__ import annotations

from typing import Any, Callable

from ..models import EvidenceOrigin
from ..settings import get_settings, get_source_registry
from .base import Connector, ConnectorResult, RetrievalContext
from .cdc_wonder import CdcWonderConnector
from .clinicaltrials import ClinicalTrialsConnector
from .crossref import CrossrefConnector
from .dailymed import DailyMedConnector
from .europepmc import EuropePmcConnector
from .firecrawl import FirecrawlConnector
from .guideline_discovery import GuidelineDiscoveryConnector
from .icd11 import Icd11Connector
from .local_files import LocalFilesConnector, available_datasets
from .loinc import LoincConnector
from .nci_pdq import NciPdqConnector
from .nlm_clinical_tables import (NlmHcpcsConnector, NlmIcd9CmConnector,
                                  NlmIcd10CmConnector, NlmLoincConnector,
                                  NlmRxTermsConnector)
from .openfda import (FaersConnector, OpenFdaDrugsFdaConnector, OpenFdaLabelConnector,
                      OpenFdaNdcConnector)
from .orphanet import OrphanetConnector
from .pubmed import PubMedConnector
from .seer import SeerConnector
from .who_gho import WhoGhoConnector

# Publishing venues used to narrow guideline discovery. These are venue hints,
# not pinned documents: the query still selects by publication type and date.
GUIDELINE_JOURNALS: dict[str, tuple[list[str], str]] = {
    "esmo": (["Annals of oncology", "Ann Oncol", "ESMO open"],
             "European Society for Medical Oncology"),
    "eha": (["HemaSphere", "Haematologica"],
            "European Hematology Association"),
    "iwcll": (["Blood", "Blood advances"],
              "International Workshop on Chronic Lymphocytic Leukemia"),
    "ashpublications": (["Blood", "Blood advances", "Hematology"],
                        "American Society of Hematology"),
}

# DOI prefixes owned by a society, used by the Crossref connector.
DOI_PREFIXES: dict[str, str] = {"asco": "10.1200"}


class LicensedConnector:
    """Placeholder for a source that needs a paid licence (NCCN, AMA CPT).

    It never scrapes and never invents an alternative: it reports the blocker
    so the run can substitute an approved open source explicitly.
    """

    origin = EvidenceOrigin.APPROVED_API
    REASON = "licensed connector not configured"

    def __init__(self, source_id: str, source_name: str, tier: int) -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.tier = tier

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        return ConnectorResult.failure(self.source_id, self.REASON)


def _build_from_key(key: str, source: dict[str, Any]) -> Connector | None:
    """Instantiate the connector named by the YAML `connector:` key."""
    sid = source["id"]
    name = source.get("name", sid)
    tier = int(source.get("tier", 5))
    domain = source.get("domain") or None

    simple: dict[str, Callable[[], Connector]] = {
        "pubmed": PubMedConnector,
        "orphanet": OrphanetConnector,
        "seer": SeerConnector,
        "nci_pdq": NciPdqConnector,
        "cdc_wonder": CdcWonderConnector,
        "who_gho": WhoGhoConnector,
        "openfda_label": OpenFdaLabelConnector,
        "openfda_drugsfda": OpenFdaDrugsFdaConnector,
        "openfda_ndc": OpenFdaNdcConnector,
        "faers": FaersConnector,
        "dailymed": DailyMedConnector,
        "clinicaltrials": ClinicalTrialsConnector,
        "icd11": Icd11Connector,
        "loinc": LoincConnector,
    }
    if key in simple:
        return simple[key]()

    # NLM Clinical Tables share one base class and differ only by table, so
    # they take the source id, name and tier from the YAML like the others.
    nlm: dict[str, type] = {
        "nlm_icd10cm": NlmIcd10CmConnector,
        "nlm_icd9cm": NlmIcd9CmConnector,
        "nlm_hcpcs": NlmHcpcsConnector,
        "nlm_loinc": NlmLoincConnector,
        "nlm_rxterms": NlmRxTermsConnector,
    }
    if key in nlm:
        return nlm[key](source_id=sid, name=name, tier=tier)
    if key == "europepmc":
        return EuropePmcConnector(source_id=sid, source_name=name, tier=tier)
    if key == "crossref":
        return CrossrefConnector(source_id=sid, source_name=name, tier=tier,
                                 prefix=DOI_PREFIXES.get(sid))
    if key == "guideline_discovery":
        journals, organization = GUIDELINE_JOURNALS.get(sid, ([], name))
        return GuidelineDiscoveryConnector(source_id=sid, source_name=name, tier=tier,
                                           journal=journals, organization=organization)
    if key == "local_files":
        return LocalFilesConnector(source_id=sid, dataset=source["local_dataset"],
                                   source_name=name, tier=tier)
    if key == "firecrawl":
        return FirecrawlConnector(source_id=sid, source_name=name, tier=tier,
                                  domain=domain,
                                  search_hint=source.get("search_hint", ""))
    return None


def build_connector(source: dict[str, Any]) -> Connector | None:
    """One YAML source entry -> one connector instance, or None if unmappable."""
    sid = source["id"]
    name = source.get("name", sid)
    tier = int(source.get("tier", 5))
    access = source.get("access_method", "")
    key = source.get("connector")

    if access == "licensed" or (not key and access == "licensed"):
        return LicensedConnector(sid, name, tier)
    if key:
        return _build_from_key(key, source)
    if access == "targeted_search":
        # Domain-scoped web search that keeps the source's own tier.
        return FirecrawlConnector(source_id=sid, source_name=name, tier=tier,
                                  domain=source.get("domain"),
                                  organization=name,
                                  search_hint=source.get("search_hint", ""))
    return None


def build_registry(include_disabled: bool = False) -> dict[str, Connector]:
    """source id -> live connector, for every enabled source in sources.yaml."""
    registry: dict[str, Connector] = {}
    for source in get_source_registry().get("sources", []):
        if not source.get("enabled", True) and not include_disabled:
            continue
        connector = build_connector(source)
        if connector is not None:
            registry[source["id"]] = connector
    return registry


def _missing_credentials(source: dict[str, Any]) -> list[str]:
    settings = get_settings()
    required = list(source.get("requires_credentials") or [])
    missing = [name for name in required if not getattr(settings, name.lower(), None)]
    if source.get("access_method") == "targeted_search" or \
            source.get("access_method") == "firecrawl_search":
        # Keyless fallback exists, so this is a degradation, not a blocker.
        if not settings.firecrawl_api_key:
            missing.append("FIRECRAWL_API_KEY (optional: keyless fallback in use)")
    return missing


def connector_health() -> list[dict[str, Any]]:
    """Per-source readiness for the UI: is this source actually usable today,
    and if not, what is missing."""
    datasets = available_datasets()
    registry = build_registry()
    rows: list[dict[str, Any]] = []
    for source in get_source_registry().get("sources", []):
        sid = source["id"]
        access = source.get("access_method", "")
        missing = _missing_credentials(source)
        blocking = [m for m in missing if "optional" not in m]

        if access == "licensed":
            configured = False
            blocking = blocking or ["licensed dataset"]
        elif access == "local_file":
            dataset = source.get("local_dataset", "")
            configured = bool(datasets.get(dataset))
            if not configured:
                blocking = blocking or [f"reference file: {dataset}"]
        else:
            configured = sid in registry and not blocking

        rows.append({
            "id": sid,
            "name": source.get("name", sid),
            "tier": int(source.get("tier", 5)),
            "access_method": access,
            "connector": source.get("connector"),
            "enabled": bool(source.get("enabled", True)),
            "configured": configured,
            "missing_credentials": missing,
            "blocking": blocking,
        })
    return rows
