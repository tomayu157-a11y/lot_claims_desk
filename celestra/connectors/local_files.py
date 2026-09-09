"""Local reference files under celestra/data/reference/.

Four datasets, all official release files that have no anonymous API:
icd10cm (CMS ICD-10-CM order/code files), hcpcs (CMS HCPCS release),
gems (CMS ICD-9 to ICD-10 general equivalence mappings) and purple_book
(FDA Purple Book monthly export).

Formats supported: the fixed-width ICD-10-CM order file, the plain
"code<space>description" code file, .csv and .tsv. Rows are searchable by
free text and by code prefix.

A missing file is a configuration fact, not an outage: the connector fails
with "reference file not installed: <dataset>" and attaches a single hint ref
whose `raw` names the filenames it looked for, so the UI can tell an operator
exactly what to drop in. Because the result is ok=False the orchestrator will
not treat the hint as evidence.
"""
from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..models import EvidenceOrigin, SourceRef
from ..settings import REFERENCE_DIR
from ._util import clean, clip
from .base import ConnectorResult, RetrievalContext

# The ICD-10-CM order file is fixed width: order number, code, valid flag,
# short description, long description.
ORDER_LINE = re.compile(r"^(\d{5})\s+([A-Z0-9]{3,7})\s+([01])\s+(.{1,60}?)\s{2,}(.+)$")
CODE_LINE = re.compile(r"^([A-Z0-9]{3,7})\s+(.+)$")


@dataclass(frozen=True)
class Dataset:
    key: str
    label: str
    # Filenames are matched as globs, in order of preference.
    patterns: tuple[str, ...]
    code_columns: tuple[str, ...] = ()
    text_columns: tuple[str, ...] = ()
    citation_url: str = ""
    notes: str = ""


DATASETS: dict[str, Dataset] = {
    "icd10cm": Dataset(
        key="icd10cm",
        label="CMS ICD-10-CM Release Files",
        patterns=("icd10cm_order_*.txt", "icd10cm_codes_*.txt", "icd10cm*.txt",
                  "icd10cm*.csv", "icd10cm*.tsv"),
        code_columns=("code", "icd10cm", "icd_10_cm_code", "diagnosis_code"),
        text_columns=("long_description", "description", "short_description"),
        citation_url="https://www.cms.gov/medicare/coding-billing/icd-10-codes",
    ),
    "hcpcs": Dataset(
        key="hcpcs",
        label="CMS HCPCS Release Files",
        patterns=("hcpcs*.csv", "hcpcs*.tsv", "hcpcs*.txt", "HCPC*.txt"),
        code_columns=("hcpc", "code", "hcpcs_code"),
        text_columns=("long_description", "long description", "description",
                      "short_description"),
        citation_url="https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system",
    ),
    "gems": Dataset(
        key="gems",
        label="CMS ICD-9-CM to ICD-10-CM GEMs",
        patterns=("*gem*.txt", "*gems*.csv", "*gems*.tsv"),
        code_columns=("icd10", "icd9", "code"),
        text_columns=("flags", "description"),
        citation_url="https://www.cms.gov/medicare/coding-billing/icd-10-codes",
        notes="Supplies the ICD-9 leg of the three-system crosswalk.",
    ),
    "purple_book": Dataset(
        key="purple_book",
        label="FDA Purple Book",
        patterns=("purple*book*.csv", "purplebook*.csv", "purple*book*.tsv"),
        code_columns=("bla_number", "applicationnumber", "application_number",
                      "bla number"),
        text_columns=("proprietary_name", "proper_name", "proprietary name",
                      "proper name", "product", "description"),
        citation_url="https://purplebooksearch.fda.gov/",
    ),
}


@dataclass
class Row:
    code: str
    text: str
    fields: dict[str, str] = field(default_factory=dict)

    def line(self) -> str:
        return f"{self.code} — {self.text}".strip(" —")


def _normalise_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def _parse_delimited(path: Path, dataset: Dataset) -> list[Row]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: list[Row] = []
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        headers = [_normalise_header(h) for h in (reader.fieldnames or [])]
        code_key = next((c for c in dataset.code_columns
                         if _normalise_header(c) in headers), "")
        text_keys = [c for c in dataset.text_columns if _normalise_header(c) in headers]
        for raw_row in reader:
            record = {_normalise_header(k): clean(v) for k, v in raw_row.items() if k}
            code = record.get(_normalise_header(code_key), "") if code_key else ""
            if not code:
                code = next((v for v in record.values() if v), "")
            text = " ".join(record.get(_normalise_header(k), "") for k in text_keys).strip()
            if not text:
                text = " ".join(v for k, v in record.items()
                                if v and k != _normalise_header(code_key))
            rows.append(Row(code=code, text=clean(text), fields=record))
    return rows


def _parse_text(path: Path) -> list[Row]:
    rows: list[Row] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            match = ORDER_LINE.match(line)
            if match:
                order, code, valid, short, long = match.groups()
                rows.append(Row(code=clean(code), text=clean(long),
                                fields={"order": order, "code": clean(code),
                                        "valid_for_billing": valid,
                                        "short_description": clean(short),
                                        "long_description": clean(long)}))
                continue
            match = CODE_LINE.match(line.strip())
            if match:
                code, text = match.groups()
                rows.append(Row(code=clean(code), text=clean(text),
                                fields={"code": clean(code), "description": clean(text)}))
                continue
            parts = line.split()
            if len(parts) >= 2:
                # GEMs style: icd9 icd10 flags
                rows.append(Row(code=clean(parts[1]), text=clean(" ".join(parts)),
                                fields={"source_code": parts[0], "target_code": parts[1],
                                        "flags": " ".join(parts[2:])}))
    return rows


def resolve_path(dataset: Dataset, root: Path | None = None) -> Path | None:
    root = root or REFERENCE_DIR
    if not root.exists():
        return None
    for pattern in dataset.patterns:
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[-1]  # newest release when several years are present
    return None


def available_datasets(root: Path | None = None) -> dict[str, bool]:
    """Which reference files are installed. The UI shows this so an operator
    can see the blockers without reading a run log."""
    return {key: resolve_path(ds, root) is not None for key, ds in DATASETS.items()}


def load_rows(dataset_key: str, root: Path | None = None) -> list[Row]:
    dataset = DATASETS[dataset_key]
    path = resolve_path(dataset, root)
    if path is None:
        return []
    if path.suffix.lower() in (".csv", ".tsv"):
        return _parse_delimited(path, dataset)
    return _parse_text(path)


def search_rows(rows: Iterable[Row], terms: list[str], code_prefixes: list[str],
                limit: int) -> list[Row]:
    """Match by code prefix first (exact coding intent), then by free text."""
    prefixes = tuple(p.upper() for p in code_prefixes if p)
    lowered = [t.lower() for t in terms if t]
    by_code, by_text = [], []
    for row in rows:
        code = row.code.upper()
        if prefixes and code.startswith(prefixes):
            by_code.append(row)
        elif lowered and any(t in row.text.lower() for t in lowered):
            by_text.append(row)
        if len(by_code) >= limit * 4:
            break
    return (by_code + by_text)[:limit]


class LocalFilesConnector:
    """One installed reference dataset, searchable by term and code prefix."""

    origin = EvidenceOrigin.LOCAL_FILE

    def __init__(self, source_id: str, dataset: str, source_name: str = "",
                 tier: int = 1, root: Path | None = None) -> None:
        if dataset not in DATASETS:
            raise KeyError(f"unknown reference dataset: {dataset}")
        self.source_id = source_id
        self.dataset_key = dataset
        self.dataset = DATASETS[dataset]
        self.source_name = source_name or self.dataset.label
        self.tier = tier
        self.root = root or REFERENCE_DIR

    def available_datasets(self) -> dict[str, bool]:
        return available_datasets(self.root)

    def installed(self) -> bool:
        return resolve_path(self.dataset, self.root) is not None

    def _missing(self) -> ConnectorResult:
        expected = list(self.dataset.patterns)
        hint = SourceRef(
            source_id=self.source_id,
            source_name=self.source_name,
            tier=self.tier,
            url=self.dataset.citation_url,
            title=f"{self.dataset.label} not installed",
            organization="local reference file",
            identifiers={"dataset": self.dataset_key},
            snippet=(f"Install the {self.dataset.label} release file in "
                     f"{self.root} — expected one of: {', '.join(expected)}."),
            raw={
                "dataset": self.dataset_key,
                "expected_filenames": expected,
                "reference_dir": str(self.root),
                "citation_url": self.dataset.citation_url,
            },
            origin=self.origin,
        )
        result = ConnectorResult.failure(
            self.source_id, f"reference file not installed: {self.dataset_key}")
        result.refs = [hint]  # hint only: ok=False keeps it out of the evidence pool
        return result

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        started = time.perf_counter()
        if not self.installed():
            return self._missing()
        try:
            path = resolve_path(self.dataset, self.root)
            rows = load_rows(self.dataset_key, self.root)
            prefixes = [clean(c) for c in (ctx.extra.get("code_prefixes")
                                           or ctx.extra.get("icd10_codes") or [])]
            # HCPCS and the Purple Book are indexed by product, not by disease,
            # so upstream entities (drug and test names) are searched alongside
            # the indication's own synonyms.
            # Key names must match what services/handoff.py publishes, which is
            # "drugs" and "test_names"; "drug_names" never appears and silently
            # contributed nothing.
            terms = ctx.or_terms() + [
                clean(t) for key in ("search_terms", "drugs", "test_names", "regimens")
                for t in (ctx.extra.get(key) or [])
            ]
            hits = search_rows(rows, [t for t in terms if t], prefixes, max(1, limit))
            refs: list[SourceRef] = []
            if hits:
                body = "\n".join(h.line() for h in hits)
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=self.dataset.citation_url or f"file://{path}",
                    title=f"{self.dataset.label}: {len(hits)} matching rows",
                    organization="local reference file",
                    published="",
                    identifiers={"dataset": self.dataset_key,
                                 "file": path.name if path else ""},
                    snippet=clip(body, 1500),
                    raw={
                        "dataset": self.dataset_key,
                        "file": str(path),
                        "row_count": len(rows),
                        "matches": [{"code": h.code, "text": h.text, **h.fields}
                                    for h in hits],
                        "text": body,
                    },
                    origin=self.origin,
                ))
        except (OSError, ValueError, csv.Error) as exc:
            return ConnectorResult.failure(
                self.source_id, f"reference file unreadable: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, type(exc).__name__)
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no matching rows", calls=0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
