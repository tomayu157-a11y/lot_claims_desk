"""Exact code facts for the rules stage.

A market basket is only useful if the codes on it are right, and codes are
facts with registries, not opinions to research. This module asks those
registries directly, one agent at a time, and hands the answers to the rules
writer as given:

  HCPCS Level II   NLM Clinical Tables (the CMS release, searchable by name)
  NDC              FDA NDC Directory through openFDA (product NDCs, brand,
                   labeler, route, dosage form)
  identity, class  RxNorm and RxClass (RxCUI, brand names, ATC class)
  schedule         the FDA label through openFDA (cycle length, days on and
                   off, route) and its indications, for attribution
  HCPCS <-> NDC    the CMS ASP NDC-HCPCS crosswalk, the quarterly file
                   Medicare Part B pays from
  procedures       the CMS ICD-10-PCS code file (transplant, CAR-T)

Every call is a free public API or a public CMS file. Nothing here touches
CPT or NCCN. Results are cached on disk for a month so a rebuild costs
nothing, and every failure is reported as an empty answer with a reason
rather than raised, so a registry outage never stops the build.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

from ..connectors._util import clean
from ..connectors.base import http
from ..settings import DATA_DIR, REFERENCE_DIR

log = logging.getLogger("celestra.codes")

HCPCS_URL = "https://clinicaltables.nlm.nih.gov/api/hcpcs/v3/search"
NDC_URL = "https://api.fda.gov/drug/ndc.json"
LABEL_URL = "https://api.fda.gov/drug/label.json"
RXNORM = "https://rxnav.nlm.nih.gov/REST"
PCS_ZIP = "https://www.cms.gov/files/zip/{fy}-icd-10-pcs-codes-file.zip"
ASP_ZIP = "https://www.cms.gov/files/zip/{month}-{year}-ndc-hcpcs-crosswalk-final.zip"

CACHE_PATH = DATA_DIR / "cache" / "code_lookup.json"
CACHE_TTL = 30 * 86400

SOURCE_NAMES = {
    "hcpcs": "NLM Clinical Tables (HCPCS)",
    "ndc": "FDA NDC Directory (openFDA)",
    "rxnorm": "RxNorm / RxClass (NLM)",
    "label": "FDA Drug Labeling (openFDA)",
    "crosswalk": "CMS ASP NDC-HCPCS Crosswalk",
    "pcs": "CMS ICD-10-PCS Code File",
}

_SCHEDULE = re.compile(
    r"[^.]*\b(cycle|every \d+ (?:days|weeks)|days? \d+\s*(?:-|–|to|through)\s*\d+|"
    r"continuous(?:ly)? (?:intravenous )?infus|treatment-free|once (?:daily|weekly)|"
    r"twice daily|weekly|daily)\b[^.]*\.", re.I)


# -- cache ------------------------------------------------------------------------------
class _Cache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, key: str) -> Any:
        row = self._load().get(key)
        if not row or time.time() - float(row.get("at", 0)) > CACHE_TTL:
            return None
        return row.get("value")

    def put(self, key: str, value: Any) -> None:
        data = self._load()
        data[key] = {"at": time.time(), "value": value}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass


_cache = _Cache(CACHE_PATH)


def _tokens(name: str) -> list[str]:
    """The words of an agent name worth matching on: 'inotuzumab ozogamicin'
    -> ['inotuzumab', 'ozogamicin']; short and generic words are dropped."""
    stop = {"injection", "hydrochloride", "sulfate", "sodium", "acetate", "pegol", "for",
            "and", "the", "recombinant", "products", "product"}
    out = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 4 and w not in stop]
    return out or [name.lower()]


def _mentions(text: str, name: str) -> bool:
    low = (text or "").lower()
    toks = _tokens(name)
    return any(t in low for t in toks[:2])


# -- registries ---------------------------------------------------------------------------
async def hcpcs_for(name: str) -> dict[str, Any]:
    """HCPCS Level II codes whose descriptor names the agent. C-codes are the
    temporary pass-through codes that precede a J-code and are flagged."""
    try:
        payload = await http.get_json(HCPCS_URL, params={
            "terms": name, "maxList": 20, "sf": "long_desc,short_desc",
            "df": "code,short_desc,long_desc"}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return {"codes": [], "error": f"{type(exc).__name__}"}
    rows = payload[3] if isinstance(payload, list) and len(payload) > 3 and isinstance(payload[3], list) else []
    codes = []
    for row in rows:
        if not row or len(row) < 2:
            continue
        code = str(row[0]).strip()
        desc = str(row[2] if len(row) > 2 and row[2] else row[1]).strip()
        if not _mentions(desc, name):
            continue
        if code.startswith("G") or re.match(r"^patients? (not )?receiv", desc, re.I):
            continue  # quality-measure codes name drugs without billing them
        status = "temporary C-code" if code.startswith("C") else ("Q-code" if code.startswith("Q") else "")
        codes.append({"code": code, "description": desc[:120], "status": status})
    return {"codes": codes[:8]}


async def ndc_for(name: str) -> dict[str, Any]:
    """Product NDCs from the FDA NDC Directory, by generic or brand name."""
    q = clean(name).replace('"', "")
    try:
        payload = await http.get_json(NDC_URL, params={
            "search": f'(generic_name:"{q}" OR brand_name:"{q}")', "limit": 40}, timeout=25)
    except Exception as exc:  # noqa: BLE001
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return {"ndcs": [], "error": "no NDC listed" if status == 404 else f"{type(exc).__name__}"}
    out, seen, brands, routes, forms, labelers = [], set(), [], [], [], []
    for r in (payload or {}).get("results") or []:
        gen = str(r.get("generic_name") or "")
        brand = str(r.get("brand_name") or "")
        if not (_mentions(gen, name) or _mentions(brand, name)):
            continue
        pndc = str(r.get("product_ndc") or "")
        if not pndc or pndc in seen:
            continue
        seen.add(pndc)
        route = ", ".join(r.get("route") or [])
        out.append({"ndc": pndc, "brand": brand, "labeler": str(r.get("labeler_name") or "")[:60],
                    "route": route, "dosage_form": str(r.get("dosage_form") or "")[:40],
                    "packages": [p.get("package_ndc") for p in (r.get("packaging") or [])[:3]]})
        if brand and brand.upper() not in [b.upper() for b in brands]:
            brands.append(brand)
        if route and route not in routes:
            routes.append(route)
        form = str(r.get("dosage_form") or "")
        if form and form not in forms:
            forms.append(form)
        lab = str(r.get("labeler_name") or "")
        if lab and lab not in labelers:
            labelers.append(lab)
    return {"ndcs": out[:12], "ndc_count": len(out), "brands": brands[:6], "routes": routes[:4],
            "dosage_forms": forms[:4], "labelers": labelers[:8]}


async def rxnorm_for(name: str) -> dict[str, Any]:
    """RxCUI, brand names and ATC class from RxNorm and RxClass."""
    out: dict[str, Any] = {"rxcui": "", "brands": [], "atc": []}
    try:
        payload = await http.get_json(f"{RXNORM}/rxcui.json", params={"name": name, "search": 1}, timeout=20)
        ids = ((payload or {}).get("idGroup") or {}).get("rxnormId") or []
        if ids:
            out["rxcui"] = str(ids[0])
            rel = await http.get_json(f"{RXNORM}/rxcui/{ids[0]}/related.json", params={"tty": "BN"}, timeout=20)
            for grp in ((rel or {}).get("relatedGroup") or {}).get("conceptGroup") or []:
                for c in grp.get("conceptProperties") or []:
                    if c.get("name") and c["name"] not in out["brands"]:
                        out["brands"].append(c["name"])
        cls = await http.get_json(f"{RXNORM}/rxclass/class/byDrugName.json",
                                  params={"drugName": name, "relaSource": "ATC"}, timeout=20)
        for item in ((cls or {}).get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo") or []:
            c = item.get("rxclassMinConceptItem") or {}
            label = f"{c.get('classId', '')} {c.get('className', '')}".strip()
            if label and label not in out["atc"]:
                out["atc"].append(label)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}"
    out["brands"] = out["brands"][:6]
    out["atc"] = out["atc"][:4]
    return out


async def label_for(name: str) -> dict[str, Any]:
    """Route, the dosing schedule sentences and the indications from the FDA label."""
    q = clean(name).replace('"', "")
    try:
        payload = await http.get_json(LABEL_URL, params={
            "search": f'(openfda.generic_name:"{q}" OR openfda.brand_name:"{q}" OR openfda.substance_name:"{q}")',
            "limit": 1}, timeout=25)
    except Exception as exc:  # noqa: BLE001
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return {"schedule": "", "indications": "", "error": "no label" if status == 404 else f"{type(exc).__name__}"}
    results = (payload or {}).get("results") or []
    if not results:
        return {"schedule": "", "indications": ""}
    r = results[0]
    fda = r.get("openfda") or {}
    dosing = re.sub(r"<[^>]+>", " ", " ".join(r.get("dosage_and_administration") or []))
    dosing = re.sub(r"\s+", " ", dosing)
    found = []
    for m in _SCHEDULE.finditer(dosing):
        s = m.group(0).strip()
        if len(s) > 25 and s not in found:
            found.append(s[:260])
    # The sentences that state the schedule outrank the ones that mention a cycle in passing.
    def score(s: str) -> int:
        low = s.lower()
        return (3 * ("consists of" in low or "treatment-free" in low)
                + 2 * bool(re.search(r"every \d+ (days|weeks)|days? \d+\s*(-|–|to|through)\s*\d+", low))
                + 1 * ("once daily" in low or "weekly" in low or "continuous" in low)
                - 2 * ("hospitalization" in low or "monitor" in low))
    sentences = sorted(found, key=score, reverse=True)[:6]
    ind = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(r.get("indications_and_usage") or [])))
    route = ", ".join(fda.get("route") or [])
    if not route:
        low = dosing.lower()
        for word, label in (("intravenous", "INTRAVENOUS"), ("subcutaneous", "SUBCUTANEOUS"),
                            ("intramuscular", "INTRAMUSCULAR"), ("orally", "ORAL"), ("oral", "ORAL")):
            if word in low:
                route = f"{label} (from label text)"
                break
    return {
        "set_id": r.get("set_id", ""),
        "brand": (fda.get("brand_name") or [""])[0],
        "route": route,
        "schedule": " ".join(sentences)[:1400],
        "indications": ind[:900],
    }


# -- CMS files ------------------------------------------------------------------------------
async def _download(url: str) -> bytes:
    client = await http.client()
    resp = await client.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content


def _fiscal_years(today: date | None = None) -> list[int]:
    today = today or date.today()
    fy = today.year + 1 if today.month >= 10 else today.year
    return [fy, fy - 1]


async def pcs_file() -> Path | None:
    """The CMS ICD-10-PCS code file, downloaded once into the reference folder."""
    for fy in _fiscal_years():
        path = REFERENCE_DIR / f"icd10pcs_codes_{fy}.txt"
        if path.exists():
            return path
    for fy in _fiscal_years():
        try:
            data = await _download(PCS_ZIP.format(fy=fy))
            z = zipfile.ZipFile(io.BytesIO(data))
            name = next((n for n in z.namelist() if re.match(r"icd10pcs_codes_\d{4}\.txt$", n)), None)
            if not name:
                continue
            REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
            path = REFERENCE_DIR / f"icd10pcs_codes_{fy}.txt"
            path.write_bytes(z.read(name))
            return path
        except Exception as exc:  # noqa: BLE001
            log.info("ICD-10-PCS FY%s not fetched: %s", fy, type(exc).__name__)
    return None


async def procedure_codes(terms: list[str], limit: int = 40) -> dict[str, Any]:
    """ICD-10-PCS codes whose title matches any of the terms (regexes or words)."""
    path = await pcs_file()
    if path is None:
        return {"codes": [], "error": "ICD-10-PCS file not available"}
    pats = [re.compile(t, re.I) for t in terms]
    out = []
    for line in path.read_text(encoding="latin-1").splitlines():
        m = re.match(r"^([A-Z0-9]{7})\s+(.+)$", line.strip())
        if not m:
            continue
        code, title = m.group(1), m.group(2).strip()
        if any(p.search(title) or p.search(code) for p in pats):
            out.append({"system": "ICD-10-PCS", "code": code, "description": title[:120]})
            if len(out) >= limit:
                break
    return {"codes": out, "file": path.name}


def _quarters(today: date | None = None) -> list[tuple[str, int]]:
    today = today or date.today()
    names = ["january", "april", "july", "october"]
    q = (today.month - 1) // 3
    out = []
    # the coming quarter is published a few weeks early; then current, then previous
    for step in (1, 0, -1, -2):
        idx, year = q + step, today.year
        while idx > 3:
            idx, year = idx - 4, year + 1
        while idx < 0:
            idx, year = idx + 4, year - 1
        out.append((names[idx], year))
    return out


async def crosswalk_file() -> Path | None:
    """The CMS ASP NDC-HCPCS crosswalk, saved as a clean CSV the local-file
    connector can also read: preamble stripped, header normalised."""
    existing = sorted(REFERENCE_DIR.glob("asp_ndc_hcpcs_crosswalk_*.csv"))
    if existing:
        return existing[-1]
    for month, year in _quarters():
        try:
            data = await _download(ASP_ZIP.format(month=month, year=year))
            z = zipfile.ZipFile(io.BytesIO(data))
            name = next((n for n in z.namelist()
                         if "asp ndc-hcpcs crosswalk" in n.lower() and n.lower().endswith(".csv")), None)
            if not name:
                continue
            rows = list(csv.reader(io.StringIO(z.read(name).decode("latin-1"))))
            start = next((i for i, r in enumerate(rows) if r and r[0].strip().upper().endswith("_CODE")), None)
            if start is None:
                continue
            header = ["hcpcs"] + [re.sub(r"[^a-z0-9]+", "_", c.strip().lower()).strip("_") for c in rows[start][1:]]
            REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
            path = REFERENCE_DIR / f"asp_ndc_hcpcs_crosswalk_{year}{['','01','04','07','10'][['january','april','july','october'].index(month) + 1]}.csv"
            with path.open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(header)
                for r in rows[start + 1:]:
                    if r and r[0].strip():
                        w.writerow([c.strip() for c in r])
            return path
        except Exception as exc:  # noqa: BLE001
            log.info("ASP crosswalk %s %s not fetched: %s", month, year, type(exc).__name__)
    return None


_XW_ROWS: list[dict[str, str]] | None = None


async def crosswalk_for(name: str) -> dict[str, Any]:
    global _XW_ROWS
    path = await crosswalk_file()
    if path is None:
        return {"rows": [], "error": "ASP crosswalk not available"}
    if _XW_ROWS is None:
        with path.open(encoding="utf-8", newline="") as fh:
            _XW_ROWS = list(csv.DictReader(fh))
    hits = []
    for r in _XW_ROWS:
        text = f"{r.get('drug_name', '')} {r.get('short_description', '')}"
        if _mentions(text, name):
            hits.append({"hcpcs": r.get("hcpcs", ""), "description": r.get("short_description", ""),
                         "ndc": r.get("ndc", ""), "drug": r.get("drug_name", "").strip(),
                         "labeler": r.get("labeler_name", ""), "dose": r.get("hcpcs_dosage", "")})
    return {"rows": hits[:12], "count": len(hits), "file": path.name}


# -- one agent, every registry -------------------------------------------------------------
async def lookup_agent(name: str) -> dict[str, Any]:
    key = f"agent:{clean(name).lower()}"
    hit = _cache.get(key)
    if hit is not None:
        return hit
    hcpcs, ndc, rx, label, xw = await asyncio.gather(
        hcpcs_for(name), ndc_for(name), rxnorm_for(name), label_for(name), crosswalk_for(name))
    brands = []
    for b in [*(ndc.get("brands") or []), *(rx.get("brands") or []), label.get("brand", "")]:
        if b and b.upper() not in [x.upper() for x in brands]:
            brands.append(b)
    xw_codes = {r["hcpcs"] for r in xw.get("rows") or [] if r.get("hcpcs")}
    codes = list(hcpcs.get("codes") or [])
    known = {c["code"] for c in codes}
    for r in xw.get("rows") or []:
        if r.get("hcpcs") and r["hcpcs"] not in known:
            codes.append({"code": r["hcpcs"], "description": r.get("description", ""), "status": ""})
            known.add(r["hcpcs"])
    for c in codes:
        if c["code"] in xw_codes:
            c["status"] = (c["status"] + "; " if c["status"] else "") + "on the CMS ASP crosswalk"
    routes = label.get("route") or ", ".join(ndc.get("routes") or [])
    out = {
        "agent": name,
        "rxcui": rx.get("rxcui", ""),
        "brands": brands[:6],
        "atc": rx.get("atc") or [],
        "route": routes,
        "dosage_forms": ndc.get("dosage_forms") or [],
        "hcpcs": codes[:8],
        "ndcs": ndc.get("ndcs") or [],
        "ndc_count": ndc.get("ndc_count", 0),
        "labelers": ndc.get("labelers") or [],
        "crosswalk": (xw.get("rows") or [])[:6],
        "schedule": label.get("schedule", ""),
        "indications": label.get("indications", ""),
        "sources": [SOURCE_NAMES[k] for k, ok in (
            ("hcpcs", bool(hcpcs.get("codes"))), ("ndc", bool(ndc.get("ndcs"))),
            ("rxnorm", bool(rx.get("rxcui"))), ("label", bool(label.get("schedule") or label.get("indications"))),
            ("crosswalk", bool(xw.get("rows")))) if ok],
        "errors": {k: v.get("error") for k, v in (("hcpcs", hcpcs), ("ndc", ndc), ("rxnorm", rx),
                                                   ("label", label), ("crosswalk", xw)) if v.get("error")},
    }
    _cache.put(key, out)
    return out


async def lookup_agents(names: list[str], limit: int = 40, concurrency: int = 4) -> dict[str, dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)
    seen: list[str] = []
    for n in names:
        k = clean(n).lower()
        if k and k not in [s.lower() for s in seen]:
            seen.append(clean(n))
    seen = seen[:limit]

    async def one(n: str) -> tuple[str, dict[str, Any]]:
        async with sem:
            try:
                return n, await lookup_agent(n)
            except Exception as exc:  # noqa: BLE001
                log.warning("code lookup failed for %s: %s", n, exc)
                return n, {"agent": n, "hcpcs": [], "ndcs": [], "brands": [], "atc": [], "sources": [],
                           "errors": {"lookup": type(exc).__name__}}

    pairs = await asyncio.gather(*(one(n) for n in seen))
    return dict(pairs)


# -- what the rules writer reads ---------------------------------------------------------------
def describe(codes: dict[str, dict[str, Any]]) -> str:
    lines = []
    for name, c in codes.items():
        h = ", ".join(f"{x['code']}{' (' + x['status'] + ')' if x.get('status') else ''}" for x in c.get("hcpcs") or []) or "none in HCPCS"
        n = ", ".join(x["ndc"] for x in (c.get("ndcs") or [])[:4]) or "none in the NDC Directory"
        extra = f" (+{c.get('ndc_count', 0) - 4} more)" if c.get("ndc_count", 0) > 4 else ""
        brands = ", ".join(c.get("brands") or []) or "no brand"
        atc = "; ".join(c.get("atc") or []) or "class not in ATC"
        lines.append(f"- {name}: brands {brands}; ATC {atc}; route {c.get('route') or 'unknown'}; "
                     f"HCPCS {h}; NDC {n}{extra}; sources {', '.join(c.get('sources') or []) or 'none answered'}")
        if c.get("schedule"):
            lines.append(f"    schedule (label): {c['schedule'][:500]}")
        if c.get("indications"):
            lines.append(f"    indications (label): {c['indications'][:300]}")
    return "\n".join(lines)


def basket_rows(codes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The code columns of the basket, exactly as the registries gave them."""
    rows = []
    for name, c in codes.items():
        rows.append({
            "agent": name,
            "brand": ", ".join(c.get("brands") or [])[:60],
            "class": (c.get("atc") or [""])[0][:60],
            "role": "",
            "route": c.get("route") or "",
            "hcpcs": [x["code"] for x in c.get("hcpcs") or []],
            "ndc": [x["ndc"] for x in (c.get("ndcs") or [])[:4]],
            "ndc_count": c.get("ndc_count", 0),
            "days_of_supply": None,
            "grace_days": None,
            "source": ", ".join(c.get("sources") or []),
        })
    return rows
