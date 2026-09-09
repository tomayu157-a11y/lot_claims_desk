"""Full-text hydration for the documents that survived ranking.

Discovery deliberately fetches only metadata. Once ranking has chosen the few
documents worth reading, this fetches their full text from the same source
API and returns an enriched copy of the ref. A hydration miss is normal: most
articles are not open access, and the abstract still counts as evidence.
"""
from __future__ import annotations

import logging

from ..models import SourceRef

log = logging.getLogger("celestra.hydration")


def _ident(ref: SourceRef, *keys: str) -> str:
    for k in keys:
        v = (ref.identifiers or {}).get(k)
        if v:
            return str(v)
    return ""


async def hydrate(ref: SourceRef, registry: dict) -> SourceRef:
    """Return the ref with full text in raw["text"], or the ref unchanged."""
    text = ""
    try:
        if pmcid := _ident(ref, "pmcid"):
            from ..connectors.europepmc import fetch_full_text

            text = await fetch_full_text(pmcid)
        elif nct := _ident(ref, "nct", "nct_id"):
            conn = registry.get("clinicaltrials")
            if conn is not None and hasattr(conn, "hydrate"):
                study = await conn.hydrate(nct)
                proto = (study or {}).get("protocolSection") or {}
                parts = []
                for section in ("descriptionModule", "eligibilityModule", "armsInterventionsModule",
                                "outcomesModule", "designModule"):
                    node = proto.get(section) or {}
                    parts += [str(v) for v in node.values() if isinstance(v, str)]
                text = " ".join(parts)
        elif setid := _ident(ref, "set_id", "setid"):
            conn = registry.get("dailymed")
            if conn is not None and hasattr(conn, "spl_xml"):
                from ..connectors._util import strip_tags

                text = strip_tags(await conn.spl_xml(setid))
    except Exception as exc:  # noqa: BLE001 - hydration is an enrichment, never a gate
        log.debug("hydration failed for %s: %s", ref.url, exc)
        return ref

    if not text or len(text) <= len(ref.snippet or ""):
        return ref
    enriched = ref.model_copy(deep=True)
    enriched.raw = dict(enriched.raw or {})
    enriched.raw["text"] = text
    enriched.raw["hydrated"] = True
    return enriched
