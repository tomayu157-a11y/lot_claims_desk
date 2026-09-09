"""Context handed from one agent to the next.

The framework's dependency edges exist for a reason: step 2 decides WHICH
tests matter, and step 5 maps those tests to codes. Without a handoff, the
downstream agent would search a code authority for the disease name and get
nothing useful. So each agent publishes the entities it discovered, and
downstream agents receive them in their retrieval context.

Extraction is pattern-based over the evidence actually retrieved, optionally
sharpened by a model. It never invents an entity that no quote mentions.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..models import Evidence, RunConfig
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.handoff")

# ICD-10-CM: a letter, two digits, optional dotted extension. Excludes U and
# the letter I/O confusions that produce false hits on ordinary prose.
_ICD10 = re.compile(r"\b([A-TV-Z]\d{2}(?:\.\d{1,4})?)\b")
_ICD11 = re.compile(r"\b(\d[A-Z]\d{2}(?:\.\d{1,2})?)\b")
_LOINC = re.compile(r"\b(\d{4,5}-\d)\b")
_HCPCS = re.compile(r"\b([A-V]\d{4})\b")
_NDC = re.compile(r"\b(\d{4,5}-\d{3,4}-\d{1,2})\b")
_APPNO = re.compile(r"\b((?:NDA|BLA|ANDA)\s?\d{6})\b", re.I)

# Concepts a code-mapping agent can actually search a code authority for.
_TEST_LEXICON = [
    "flow cytometry", "immunophenotyping", "bone marrow biopsy",
    "bone marrow aspirate", "bone marrow aspiration", "karyotype",
    "conventional cytogenetics", "fluorescence in situ hybridization", "FISH",
    "polymerase chain reaction", "RT-PCR", "next-generation sequencing",
    "measurable residual disease", "minimal residual disease",
    "complete blood count", "peripheral blood smear", "lymph node biopsy",
    "beta-2 microglobulin", "direct antiglobulin test", "computed tomography",
    "IGHV mutational status", "TP53 mutation", "deletion 17p", "del(17p)",
    "BCR::ABL1", "KMT2A", "immunoglobulin heavy chain",
    "serum lactate dehydrogenase", "coagulation", "lumbar puncture",
]


def _findall(pattern: re.Pattern, texts: list[str], limit: int = 40) -> list[str]:
    seen, out = set(), []
    for t in texts:
        for m in pattern.findall(t):
            v = m.strip().upper().replace(" ", "")
            if v not in seen:
                seen.add(v)
                out.append(m.strip())
                if len(out) >= limit:
                    return out
    return out


def _lexicon_hits(texts: list[str], limit: int = 20) -> list[str]:
    blob = " ".join(texts).lower()
    out = []
    for concept in _TEST_LEXICON:
        if concept.lower() in blob and concept not in out:
            out.append(concept)
            if len(out) >= limit:
                break
    return out


def _drug_names(evidence: list[Evidence]) -> tuple[list[str], list[str], list[str]]:
    """Generic names, application numbers and SPL set ids come from the
    identifiers the label connectors attach, not from parsing prose."""
    generics, apps, setids = [], [], []
    for e in evidence:
        ident = e.identifiers or {}
        for key, bucket in (
            ("generic_name", generics),
            ("application_number", apps),
            ("set_id", setids),
        ):
            val = ident.get(key)
            if val and val not in bucket:
                bucket.append(val)
    texts = [e.quote for e in evidence]
    for app in _findall(_APPNO, texts, 20):
        norm = app.upper().replace(" ", "")
        if norm not in apps:
            apps.append(norm)
    return generics[:40], apps[:40], setids[:40]


async def build(
    bucket: str, cfg: RunConfig, evidence: list[Evidence]
) -> dict[str, Any]:
    """Entities this agent discovered, for downstream agents to use."""
    if not evidence:
        return {}
    texts = [f"{e.title} {e.quote}" for e in evidence]
    generics, apps, setids = _drug_names(evidence)

    out: dict[str, Any] = {}
    if codes := _findall(_ICD10, texts, 30):
        out["icd10_codes"] = codes
    if codes := _findall(_ICD11, texts, 20):
        out["icd11_codes"] = codes
    if codes := _findall(_LOINC, texts, 30):
        out["loinc_codes"] = codes
    if codes := _findall(_HCPCS, texts, 30):
        out["hcpcs_codes"] = codes
    if codes := _findall(_NDC, texts, 30):
        out["ndc_codes"] = codes
    if tests := _lexicon_hits(texts):
        out["test_names"] = tests
    if generics:
        out["drugs"] = generics
    if apps:
        out["application_numbers"] = apps
    if setids:
        out["set_ids"] = setids

    if llm.available:
        try:
            enriched = await llm.complete_json(
                "You extract named clinical entities that a downstream code-mapping or "
                "safety agent can search for. You only list entities that appear in the "
                "supplied quotes. You never add an entity that is not mentioned.",
                f"Indication: {cfg.indication}\n\nQuotes:\n"
                + "\n".join(f"- {t[:400]}" for t in texts[:30])
                + '\n\nReturn JSON: {"test_names": [str], "drugs": [str], '
                '"subtypes": [str], "biomarkers": [str], "regimens": [str]}. '
                "Each list at most 15 entries, each a short searchable term. "
                "Empty list where the quotes name none.",
                max_tokens=1500,
            )
            for key in ("test_names", "drugs", "subtypes", "biomarkers", "regimens"):
                values = [str(v).strip() for v in (enriched or {}).get(key, []) if str(v).strip()]
                if values:
                    merged = out.get(key, []) + values
                    out[key] = list(dict.fromkeys(merged))[:20]
        except LLMUnavailable:
            pass

    if out:
        log.info("agent %s handoff: %s", bucket,
                 {k: len(v) if isinstance(v, list) else v for k, v in out.items()})
    return out


def merge(existing: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    out = dict(existing)
    for key, value in addition.items():
        if isinstance(value, list):
            out[key] = list(dict.fromkeys(out.get(key, []) + value))[:60]
        else:
            out[key] = value
    return out


def for_agent(bucket: str, context: dict[str, Any]) -> dict[str, Any]:
    """What this agent should actually receive. Narrow rather than dumping the
    whole context, so a connector's query stays focused."""
    wants = {
        "A": (),
        "C": ("subtypes", "biomarkers"),
        "B": ("test_names", "icd10_codes", "icd11_codes", "subtypes", "biomarkers"),
        "D": ("drugs", "application_numbers", "set_ids", "hcpcs_codes", "regimens"),
        "E": ("drugs", "regimens", "icd10_codes", "test_names", "loinc_codes"),
        "F": ("drugs", "regimens", "test_names", "icd10_codes", "subtypes"),
        "G": (),
    }.get(bucket, ())
    out = {k: context[k] for k in wants if context.get(k)}
    # What the reviewer wrote at the gate goes to every agent that runs after
    # it. It is the one piece of context that is human, not extracted.
    notes = [
        str(n.get("input") if isinstance(n, dict) else n).strip()
        for n in (context.get("reviewer_inputs") or [])
    ]
    notes = [n for n in notes if n]
    if notes:
        out["reviewer_notes"] = notes[:12]
    return out
