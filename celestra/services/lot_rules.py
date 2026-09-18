"""The line-of-therapy rules stage.

After the research document is approved, the fixed set of rule cards in
config/lot_rules.yaml is filled for the indication: from the approved
document first (basket, codes, regimens, phases, observability), then from
targeted research where the document is silent, then by the model, which
writes each rule's statement, rationale, parameters with provenance, a
confidence class, the worked scenarios that illustrate it, and the gaps an
expert must settle. Without a model the cards are filled with the standard
conventions and template scenarios, so the workspace always renders.

Nothing here reads claims data. The output is what a data run executes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from ..events import bus
from ..models import (
    Evidence,
    ResearchQuestion,
    ReviewAction,
    RuleCard,
    RuleConfidence,
    RuleParameter,
    RuleProvenance,
    RuleScenario,
    Run,
    RulesStatus,
    TimelineEvent,
    utcnow,
)
from ..settings import get_lot_rules, get_questions, get_thresholds
from ..store import store
from . import code_lookup, retrieval
from .llm import LLMUnavailable, llm

log = logging.getLogger("celestra.rules")

_ICD10 = re.compile(r"\b(C9[0-6]\.\d{1,2}|C9[0-6]\d{1,2}|D4[5-7]\.?\d?)\b")

_SYSTEM = (
    "You are a senior claims-analytics consultant writing the line-of-therapy business rules "
    "for a client. You write each rule exactly as it would appear in the signed rules document: "
    "precise, parameterised, with its rationale. You draw only on the research document and "
    "research notes you are given. Where they do not settle a point you use the firm's "
    "standard convention and say so, and you name what an expert must still decide. You never "
    "invent a drug, a code, a guideline or a figure. Codes come from the registries you are "
    "given (HCPCS, NDC, ICD-10-PCS) and are copied, never inferred from prose."
)

# Source labels that are internal to this tool and must not appear in a rule.
_INTERNAL_SOURCE = re.compile(
    r"^(stage[_ ]?\d.*|.*\banswer$|.*research notes?$|local reference file|open web|firm standard.*|"
    r"approved document.*|market basket.*|the research.*)$", re.I)


# -- catalogue ------------------------------------------------------------------
def catalogue_for(indication_key: str) -> list[dict]:
    cfg = get_lot_rules()
    template = (cfg.get("indication_templates") or {}).get(indication_key, "curative_intent")
    optional = {k for keys in (cfg.get("templates") or {}).values() for k in keys}
    keep = set((cfg.get("templates") or {}).get(template) or [])
    out = []
    for card in cfg.get("cards") or []:
        if card["key"] in optional and card["key"] not in keep:
            continue
        out.append(dict(card))
    return out


def sections() -> list[dict]:
    return [dict(s) for s in get_lot_rules().get("sections") or []]


def _default_parameters(card: dict) -> list[RuleParameter]:
    out = []
    for p in card.get("parameters") or []:
        out.append(RuleParameter(
            key=p["key"], label=p["label"], value=p.get("default"), unit=str(p.get("unit") or ""),
            provenance=RuleProvenance(p.get("provenance", "standard")),
            alternatives=list(p.get("alternatives") or []),
            justification="Firm standard convention." if p.get("provenance", "standard") == "standard" else "",
        ))
    return out


def blank_cards(run: Run) -> list[RuleCard]:
    return [
        RuleCard(run_id=run.id, section=c["section"], card_key=c["key"], number=int(c["number"]),
                 title=c["title"], objective=c.get("objective", ""), visual=c.get("visual", "parameters"),
                 parameters=_default_parameters(c))
        for c in catalogue_for(run.config.indication_key)
    ]


# -- what the approved document already settles ------------------------------------
def document_digest(run: Run) -> dict[str, Any]:
    """The parts of the approved document a rules writer reaches for: the
    agents it names, the diagnosis codes it found, every answer and table,
    what claims can see, and what the reviewer added. Drug codes are NOT
    read from prose here: `enrich_digest` asks the registries."""
    reports = store.get_stage_reports(run.id)
    evidence = store.get_evidence(run.id)
    questions = store.get_questions(run.id)
    lexicon = ((get_questions().get("indications") or {}).get(run.config.indication_key) or {}
               ).get("claims_lexicon") or {}

    agents: dict[str, dict[str, Any]] = {}
    for name in [*(run.context.get("drugs") or []), *(lexicon.get("drugs") or [])]:
        key = str(name).strip().lower()
        if key and key not in agents:
            agents[key] = {"agent": str(name).strip(), "mentions": 0, "codes": [], "ndcs": []}
    blob_by_q = {q.id: q for q in questions}
    icd: dict[str, str] = {}
    for e in evidence:
        text = f"{e.title} {e.quote}"
        low = text.lower()
        for key, row in agents.items():
            if key in low:
                row["mentions"] += 1
        q = blob_by_q.get(e.question_id)
        if q is not None and q.stage == "stage_3":
            for code in _ICD10.findall(text):
                icd.setdefault(code, text[:160])
    ordered = sorted(agents.values(), key=lambda r: (-r["mentions"], r["agent"]))

    answers = []
    tables = []
    observability = []
    for r in reports:
        for row in r.answers:
            if row.get("answer"):
                answers.append({"stage": r.stage, "agent": r.agent_name or r.name,
                                "question": row.get("seed") or row.get("question"),
                                "answer": str(row["answer"])[:900],
                                "citations": list(row.get("citations") or [])[:4]})
        for t in r.tables[:6]:
            tables.append({"stage": r.stage, "title": t.title, "columns": list(t.columns),
                           "rows": [[str(c)[:90] for c in row] for row in t.rows[:8]]})
        observability += [dict(o) for o in r.observability[:8]]
    limit = int((get_thresholds().get("rules") or {}).get("lookup_agents_max", 40))
    return {
        "indication": run.config.indication,
        "agents": ordered[:limit],
        "codes": {},
        "procedures": [],
        "icd10": [{"code": c, "context": ctx} for c, ctx in list(icd.items())[:20]],
        "answers": answers,
        "tables": tables,
        "observability": observability[:30],
        "reviewer_inputs": [
            {"title": r.get("title"), "input": r.get("input")}
            for r in (run.context.get("reviewer_inputs") or []) if isinstance(r, dict)
        ],
        "lexicon": lexicon,
    }


_PROCEDURE_TERMS = [r"chimeric antigen receptor", r"autoleucel", r"^302[34]3[GY]"]


async def enrich_digest(digest: dict[str, Any], on_progress=None) -> dict[str, Any]:
    """Ask the registries for every agent's codes, brands, class, route and
    label schedule, and the CMS procedure file for transplant and CAR-T."""
    names = [a["agent"] for a in digest.get("agents") or []]
    if on_progress:
        await on_progress(f"Looking up codes for {len(names)} agents")
    digest["codes"] = await code_lookup.lookup_agents(names)
    for a in digest.get("agents") or []:
        c = digest["codes"].get(a["agent"]) or {}
        a["codes"] = [x["code"] for x in c.get("hcpcs") or []]
        a["ndcs"] = [x["ndc"] for x in c.get("ndcs") or []]
    procs = await code_lookup.procedure_codes(_PROCEDURE_TERMS)
    digest["procedures"] = procs.get("codes") or []
    digest["procedures_error"] = procs.get("error", "")
    return digest


def _digest_text(d: dict[str, Any]) -> str:
    parts = [f"Indication: {d['indication']}"]
    if d.get("codes"):
        parts.append("AGENTS AND THEIR CODES (from the registries: NLM HCPCS, FDA NDC Directory, RxNorm, "
                     "FDA label, CMS ASP crosswalk; copy these exactly):\n" + code_lookup.describe(d["codes"]))
    else:
        parts.append("AGENTS (from the document):\n" + "\n".join(
            f"- {a['agent']}: mentioned {a['mentions']}x" for a in d["agents"][:30]))
    if d.get("procedures"):
        parts.append("PROCEDURE CODES (CMS ICD-10-PCS file):\n" + "\n".join(
            f"- {c['code']}: {c['description']}" for c in d["procedures"][:36]))
    if d["icd10"]:
        parts.append("DIAGNOSIS CODES FOUND IN THE DOCUMENT:\n" + "\n".join(
            f"- {c['code']}: {c['context']}" for c in d["icd10"]))
    parts.append("ANSWERS IN THE APPROVED DOCUMENT (cite the sources named, never the section):\n" + "\n".join(
        f"- {a['question']}\n   answer: {a['answer']}\n   sources: {', '.join(a['citations']) or 'not cited'}"
        for a in d["answers"]))
    for t in d["tables"][:10]:
        rows = "\n".join("   " + " | ".join(r) for r in t["rows"][:6])
        parts.append(f"TABLE: {t['title']}\n   columns: {' | '.join(t['columns'])}\n{rows}")
    if d["observability"]:
        parts.append("CLAIMS OBSERVABILITY:\n" + "\n".join(
            "- " + "; ".join(f"{k}: {str(v)[:90]}" for k, v in o.items()) for o in d["observability"]))
    if d["reviewer_inputs"]:
        parts.append("REVIEWER INPUTS:\n" + "\n".join(
            f"- {r['title']}: {r['input']}" for r in d["reviewer_inputs"]))
    return "\n\n".join(parts)[:30000]


# -- research where the document is silent -------------------------------------
_STAGE_FOR_RESEARCH = {
    "market_basket": "stage_2", "days_of_supply": "stage_4", "bridging_cellular_therapy": "stage_4",
    "indication_attribution": "stage_2", "protocol_phases": "stage_4",
}


class WebBudget:
    """How many rules questions may still reach the web in this build."""

    def __init__(self, limit: int) -> None:
        self.left = max(0, int(limit))

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def research_items(card: dict) -> list[dict]:
    """`research:` entries as dicts: a bare string means the question may
    reach the web; `{text, web}` says so explicitly."""
    out = []
    for item in card.get("research") or []:
        if isinstance(item, dict) and item.get("text"):
            out.append({"text": str(item["text"]), "web": bool(item.get("web", True))})
        elif isinstance(item, str) and item.strip():
            out.append({"text": item, "web": True})
    return out


async def research_card(run: Run, card: dict, registry: dict, budget: WebBudget | None = None,
                        context: dict | None = None) -> tuple[str, list[str], list[Evidence]]:
    """Targeted questions for one card, through the same tiers as the
    research phase: registry sources first, then a domain search, then the
    open web (Firecrawl, then Azure native search). A question marked
    `web: false`, or one past the build's web budget, stops at the registry
    sources. Returns the merged answer, its citations and the evidence kept."""
    texts: list[str] = []
    cites: list[str] = []
    kept: list[Evidence] = []
    timeout = float((get_thresholds().get("rules") or {}).get("research_timeout_seconds", 300))
    for item in research_items(card):
        text = item["text"].replace("{indication}", run.config.indication)
        q = ResearchQuestion(run_id=run.id, stage=_STAGE_FOR_RESEARCH.get(card["key"], "stage_6"),
                             bucket="R", text=text, seed_text=text,
                             aspects=[card["title"], card.get("objective", "")])
        allow_web = bool(item["web"]) and (budget is None or budget.take())
        try:
            outcome = await asyncio.wait_for(
                retrieval.retrieve(q, run.config, [run.config.indication], registry,
                                   context=context, allow_open_web=allow_web),
                timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("rules research timed out for %s", card["key"])
            continue
        except Exception:  # noqa: BLE001 - a failed lookup is a gap, not a crash
            log.exception("rules research failed for %s", card["key"])
            continue
        retrieval.apply_outcome(q, outcome)
        store.save_questions(run.id, [q])
        store.save_evidence(run.id, outcome.evidence)
        if q.answer_text:
            texts.append(q.answer_text)
            cites += q.answer_citations
        kept += outcome.evidence
    return "\n\n".join(texts), list(dict.fromkeys(cites)), kept


# -- the model writes the section ----------------------------------------------------
def _card_brief(card: dict, research: str) -> str:
    params = "; ".join(
        f"{p['label']} (key {p['key']}, standard {p.get('default')} {p.get('unit', '')})"
        for p in card.get("parameters") or []) or "none"
    return (f'- key "{card["key"]}": {card["title"]}. Objective: {card.get("objective", "")}. '
            f"Visual: {card.get('visual', 'parameters')}. Parameters: {params}."
            + (f"\n  Research notes: {research[:2500]}" if research else ""))


_VISUAL_SHAPES = {
    "basket": ('"visual_data": [{"agent": str, "brand": str, "class": str, "role": "branded"|'
               '"generic"|"supportive", "route": str, "hcpcs": [str], "ndc": [str], '
               '"days_of_supply": int, "grace_days": int, "source": str}]'),
    "codes": '"visual_data": [{"system": str, "code": str, "description": str, "use": str}]',
    "funnel": '"visual_data": [{"step": str, "criteria": str, "rationale": str}] (5-7 steps in order)',
    "flow": ('"visual_data": [{"question": str, "yes": str, "no": str}] — the ordered checks applied '
             'to each regimen transition; "yes"/"no" name the outcome or "next check"'),
    "grid": '"visual_data": [{"scenario": str, "condition": str, "action": str, "expected_effect": str}]',
    "parameters": '"visual_data": null',
    "timeline": '"visual_data": null',
}


async def fill_section(run: Run, section: dict, cards: list[dict], digest: str,
                       research: dict[str, str]) -> dict[str, dict]:
    if not llm.available:
        return {}
    prompt = (
        f"Indication: {run.config.indication}. Geography: {run.config.geography}. "
        f"Objective: {run.config.objective}.\n\n"
        "THE APPROVED RESEARCH DOCUMENT (digest):\n" + digest + "\n\n"
        f"SECTION {section['number']}: {section['title']} — {section.get('objective', '')}\n"
        "CARDS TO WRITE:\n" + "\n".join(_card_brief(c, research.get(c["key"], "")) for c in cards)
        + "\n\nReturn JSON: {\"cards\": [{\"key\": str, \"statement\": str, \"rationale\": str, "
        "\"confidence\": \"verified_indication\"|\"verified_class\"|\"borrowed\"|\"original\", "
        "\"parameters\": [{\"key\": str, \"value\": str|number, \"provenance\": "
        "\"standard\"|\"research\", \"justification\": str, \"source\": str}], "
        "\"scenarios\": [{\"title\": str, \"duration_days\": int, \"expected\": str, "
        "\"events\": [{\"lane\": str, \"kind\": \"dx\"|\"claim\"|\"episode\"|\"regimen\"|\"line\"|\"gap\", "
        "\"start_day\": int, \"end_day\": int, \"label\": str}]}], "
        "\"sources\": [str], \"gaps\": [str], "
        + " | ".join(sorted(set(_VISUAL_SHAPES.values()))) + "}]}.\n"
        "- statement: the rule as it will appear in the signed document, 1-3 sentences, with the "
        "parameter values written in.\n"
        "- rationale: 1-2 sentences for the client, citing the document or research where it applies.\n"
        "- confidence: verified_indication only when a source states the rule for this disease; "
        "verified_class when a haematology framework states it; borrowed when it is general oncology "
        "or firm convention; original when it is an analytical construct.\n"
        "- parameters: every parameter listed for the card. Keep the standard value unless the document "
        "or research justifies another; then set provenance research and cite the source.\n"
        "- scenarios (timeline cards): 2 worked cases with real agent names from the document, 250-400 "
        "days, lanes per agent plus one 'Line' lane whose bars are labelled 1L, 2L; expected says what "
        "the rule concludes. Other visuals: 1 scenario or none.\n"
        "- visual_data in the shape given for the card's visual; use the document's agents and codes, "
        "never invented ones; for the basket, one row per agent the document names, role from its use.\n"
        "- gaps: what an expert must decide before the rule is used, one sentence each; empty if none.\n"
        "- sources: the organisation or document that supports the rule (for example 'FDA label for "
        "Blincyto', 'NLM HCPCS table', 'NCI PDQ adult ALL treatment', 'ESMO ALL guideline 2016'). Never a "
        "section of this document, a stage name, 'research notes' or 'firm convention'.\n"
        "- market basket: one row per agent in the registry list, with the HCPCS and NDC values copied "
        "exactly as given (empty lists where the registries listed none); role from the label's "
        "indications and the document (branded = a brand-name anti-disease agent; generic = a "
        "generic anti-disease agent; supportive = not anti-disease); days_of_supply and grace_days "
        "from the label schedule (a 28-day infusion cycle with a 14-day break is 42 days of supply; "
        "a daily oral is 30). Agents whose label shows no use in this disease are left out and named "
        "in gaps.\n"
        "- where an agent's label schedule includes a treatment-free interval longer than the gap "
        "threshold, say so in the gap rule's scheduled_breaks parameter with the agent and the days."
    )
    try:
        result = await llm.complete_json(_SYSTEM, prompt, max_tokens=7000)
    except LLMUnavailable:
        return {}
    except Exception:  # noqa: BLE001
        log.exception("rules section %s failed", section["key"])
        return {}
    out: dict[str, dict] = {}
    for item in (result or {}).get("cards") or []:
        if isinstance(item, dict) and item.get("key"):
            out[str(item["key"])] = item
    return out


# -- template scenarios: what the workspace shows without a model ---------------------
def _agents_for_examples(digest: dict[str, Any]) -> tuple[str, str]:
    # The most-mentioned agents first; among equals, the ones the registries
    # bill with a HCPCS code; otherwise the document's own order.
    ranked = sorted((a for a in digest.get("agents") or [] if a.get("agent")),
                    key=lambda a: (-int(a.get("mentions") or 0), 0 if a.get("codes") else 1))
    names = [a["agent"] for a in ranked]
    a = names[0].title() if names else "Agent A"
    b = names[1].title() if len(names) > 1 else "Agent B"
    return a, b


def template_scenarios(card_key: str, params: dict[str, Any], digest: dict[str, Any]) -> list[RuleScenario]:
    a, b = _agents_for_examples(digest)
    gap = int(params.get("gap_days") or 60)
    planned = int(params.get("planned_window_days") or 28)
    look = int(params.get("attribution_lookback_months") or 6) * 30
    E = TimelineEvent
    S = RuleScenario
    if card_key == "gap_rule":
        return [
            S(title=f"Gap over {gap} days: the line advances", duration_days=330, expected="Second regimen is 2L",
              events=[E(lane=a, kind="regimen", start_day=0, end_day=90, label=a),
                      E(lane="Gap", kind="gap", start_day=90, end_day=90 + gap + 30, label=f"{gap + 30} days"),
                      E(lane=a, kind="regimen", start_day=90 + gap + 30, end_day=300, label=a),
                      E(lane="Line", kind="line", start_day=0, end_day=90, label="1L"),
                      E(lane="Line", kind="line", start_day=90 + gap + 30, end_day=300, label="2L")]),
            S(title=f"Gap of {gap} days or less: same line", duration_days=330, expected="Both regimens are 1L",
              events=[E(lane=a, kind="regimen", start_day=0, end_day=90, label=a),
                      E(lane="Gap", kind="gap", start_day=90, end_day=90 + max(gap - 15, 10), label=f"{max(gap - 15, 10)} days"),
                      E(lane=a, kind="regimen", start_day=90 + max(gap - 15, 10), end_day=300, label=a),
                      E(lane="Line", kind="line", start_day=0, end_day=300, label="1L")]),
        ]
    if card_key == "line_numbering":
        return [
            S(title="First anti-disease regimen after the index date is 1L", duration_days=300,
              expected=f"1L starts with {a}; nothing before it counts as a line",
              events=[E(lane="Index", kind="dx", start_day=0, end_day=0, label="Index diagnosis"),
                      E(lane=a, kind="regimen", start_day=30, end_day=270, label=a),
                      E(lane="Line", kind="line", start_day=30, end_day=270, label=f"1L {a}")]),
            S(title="Supportive therapy before the first regimen is Line 0", duration_days=300,
              expected="Pre-treatment is reported as L0, not as 1L",
              events=[E(lane="Index", kind="dx", start_day=0, end_day=0, label="Index diagnosis"),
                      E(lane="Supportive", kind="claim", start_day=7, end_day=28, label="Supportive care"),
                      E(lane=a, kind="regimen", start_day=35, end_day=280, label=a),
                      E(lane="Line", kind="line", start_day=7, end_day=28, label="L0"),
                      E(lane="Line", kind="line", start_day=35, end_day=280, label=f"1L {a}")]),
        ]
    if card_key == "protocol_phases":
        return [
            S(title="One frontline protocol: every phase stays in line 1", duration_days=900,
              expected="Induction through maintenance is 1L; agents change by phase, the line does not",
              events=[E(lane="Index", kind="dx", start_day=0, end_day=0, label="Index diagnosis"),
                      E(lane="Phase", kind="episode", start_day=5, end_day=35, label="Induction"),
                      E(lane="Phase", kind="episode", start_day=36, end_day=180, label="Consolidation"),
                      E(lane="Phase", kind="episode", start_day=181, end_day=260, label="Delayed intensification"),
                      E(lane="Phase", kind="episode", start_day=261, end_day=900, label="Maintenance"),
                      E(lane=a, kind="regimen", start_day=5, end_day=260, label=a),
                      E(lane=b, kind="regimen", start_day=261, end_day=900, label=b),
                      E(lane="Line", kind="line", start_day=5, end_day=900, label="1L")]),
            S(title="Relapse during maintenance starts line 2", duration_days=700,
              expected="A relapse code followed by a salvage agent starts 2L",
              events=[E(lane="Index", kind="dx", start_day=0, end_day=0, label="Index diagnosis"),
                      E(lane=a, kind="regimen", start_day=5, end_day=420, label=f"{a} (protocol)"),
                      E(lane="Relapse", kind="dx", start_day=430, end_day=430, label="Relapse code"),
                      E(lane=b, kind="regimen", start_day=440, end_day=700, label=f"{b} (salvage)"),
                      E(lane="Line", kind="line", start_day=5, end_day=420, label="1L"),
                      E(lane="Line", kind="line", start_day=440, end_day=700, label="2L")]),
        ]
    if card_key == "product_addition":
        return [S(title="A second agent is added", duration_days=300, expected=f"{a} + {b} starts 2L",
                  events=[E(lane=a, kind="regimen", start_day=0, end_day=240, label=a),
                          E(lane=b, kind="regimen", start_day=120, end_day=300, label=b),
                          E(lane="Line", kind="line", start_day=0, end_day=120, label=f"1L {a}"),
                          E(lane="Line", kind="line", start_day=120, end_day=300, label=f"2L {a}+{b}")])]
    if card_key == "planned_vs_reactive":
        return [
            S(title=f"Added within {planned} days: planned, same line", duration_days=300,
              expected="Consolidation agent belongs to 1L",
              events=[E(lane=a, kind="regimen", start_day=0, end_day=200, label=a),
                      E(lane=b, kind="regimen", start_day=planned - 7, end_day=200, label=b),
                      E(lane="Line", kind="line", start_day=0, end_day=200, label=f"1L {a}+{b}")]),
            S(title=f"Added after {planned} days to deepen response: new line", duration_days=300,
              expected="Reactive addition starts 2L",
              events=[E(lane=a, kind="regimen", start_day=0, end_day=200, label=a),
                      E(lane=b, kind="regimen", start_day=150, end_day=300, label=b),
                      E(lane="Line", kind="line", start_day=0, end_day=150, label="1L"),
                      E(lane="Line", kind="line", start_day=150, end_day=300, label=f"2L {a}+{b}")]),
        ]
    if card_key == "product_drop":
        return [S(title="One agent dropped for toxicity, the other continues", duration_days=300,
                  expected="Same line; named after the longer regimen",
                  events=[E(lane=a, kind="regimen", start_day=0, end_day=280, label=a),
                          E(lane=b, kind="regimen", start_day=0, end_day=40, label=b),
                          E(lane="Line", kind="line", start_day=0, end_day=280, label=f"1L {a}+{b}")])]
    if card_key == "substitution":
        return [S(title="Switch to an agent of a different class", duration_days=300, expected="Switch starts 2L",
                  events=[E(lane=a, kind="regimen", start_day=0, end_day=120, label=a),
                          E(lane=b, kind="regimen", start_day=130, end_day=300, label=b),
                          E(lane="Line", kind="line", start_day=0, end_day=120, label="1L"),
                          E(lane="Line", kind="line", start_day=130, end_day=300, label="2L")])]
    if card_key == "bridging_cellular_therapy":
        return [S(title="Bridging therapy before cellular therapy", duration_days=240,
                  expected="Bridging and the definitive therapy share one line",
                  events=[E(lane=a, kind="regimen", start_day=0, end_day=60, label=f"{a} (bridging)"),
                          E(lane="Cellular therapy", kind="claim", start_day=70, end_day=75, label="CAR-T / transplant"),
                          E(lane="Line", kind="line", start_day=0, end_day=240, label="2L")])]
    if card_key == "maintenance":
        return [S(title="Maintenance after the regimen ends", duration_days=360,
                  expected="Maintenance reported as 1L maintenance, no new line",
                  events=[E(lane=a, kind="regimen", start_day=0, end_day=150, label=a),
                          E(lane="Maintenance", kind="episode", start_day=160, end_day=360, label="Maintenance agent"),
                          E(lane="Line", kind="line", start_day=0, end_day=150, label="1L"),
                          E(lane="Line", kind="line", start_day=160, end_day=360, label="1L maintenance")])]
    if card_key == "regimen_construction":
        w = int(params.get("regimen_window_days") or 28)
        return [S(title=f"Agents starting within {w} days form one regimen", duration_days=200,
                  expected=f"{a} + {b} is one combination regimen",
                  events=[E(lane=a, kind="episode", start_day=0, end_day=180, label=a),
                          E(lane=b, kind="episode", start_day=w - 10, end_day=180, label=b),
                          E(lane="Regimen", kind="regimen", start_day=0, end_day=180, label=f"{a}+{b}")])]
    if card_key == "indication_attribution":
        return [S(title="Drug used for another indication before diagnosis", duration_days=look + 200,
                  expected="Pre-diagnosis use with another indication is not counted",
                  events=[E(lane="Other indication dx", kind="dx", start_day=0, end_day=5, label="Other dx"),
                          E(lane=a, kind="claim", start_day=20, end_day=look - 40, label=f"{a} (other use)"),
                          E(lane="Index diagnosis", kind="dx", start_day=look, end_day=look + 5, label="Index dx"),
                          E(lane=a, kind="regimen", start_day=look + 20, end_day=look + 200, label=f"{a} (counted)")])]
    return []


def template_visual(card_key: str, params: dict[str, Any], digest: dict[str, Any]) -> Any:
    if card_key == "market_basket":
        if digest.get("codes"):
            rows = code_lookup.basket_rows(digest["codes"])
            for r in rows:
                agent = code_lookup.clean(r.get("agent")).lower()
                brands = [b.strip().lower() for b in (r.get("brand") or "").split(",") if b.strip()]
                distinct_brand = any(b and b != agent and agent not in b for b in brands)
                r["role"] = ("branded" if distinct_brand and (r.get("hcpcs") or r.get("ndc"))
                             else ("generic" if r.get("hcpcs") or r.get("ndc") else ""))
                r["days_of_supply"] = 30 if "ORAL" in (r.get("route") or "") else 21
                r["grace_days"] = r["days_of_supply"] * int(params.get("grace_multiplier") or 2)
            return rows[:40]
        return [{"agent": a["agent"].title(), "brand": "", "class": "", "role": "",
                 "route": "", "hcpcs": [], "ndc": [], "days_of_supply": 30,
                 "grace_days": 60, "source": "registries not reached"} for a in digest.get("agents") or []][:25]
    if card_key == "index_diagnosis":
        return [{"system": "ICD-10-CM", "code": c["code"], "description": c["context"][:100], "use": "index"}
                for c in digest.get("icd10") or []]
    if card_key == "funnel":
        return [
            {"step": "Base population", "criteria": "At least one claim with an index diagnosis code in the study period",
             "rationale": "The broadest population with documented disease."},
            {"step": "Confirmed diagnosis", "criteria": f"{params.get('confirmation_claims', 2)} diagnosis claims at least "
             f"{params.get('confirmation_gap_days', 30)} days apart, or one claim plus a disease-directed therapy",
             "rationale": "Removes rule-out and miscoded encounters."},
            {"step": "Washout", "criteria": f"No disease activity in the first {params.get('washout_months', 12)} months of data",
             "rationale": "Keeps newly treated patients whose first line is observable."},
            {"step": "Continuous activity", "criteria": f"A claim in each of the {params.get('activity_quarters_before', 2)} "
             "quarters before index and every quarter after", "rationale": "A proxy for stable enrolment in open claims."},
            {"step": "Treated", "criteria": "At least one disease-directed therapy after index",
             "rationale": "Lines of therapy exist only for treated patients."},
        ]
    if card_key == "decision_flow":
        gap = params.get("gap_days", 60)
        return [
            {"question": "Is the added or changed agent supportive or maintenance only?", "yes": "No new line; report with the regimen", "no": "next check"},
            {"question": f"Treatment-free gap over {gap} days before this regimen?", "yes": "New line", "no": "next check"},
            {"question": "New agent added?", "yes": "next check", "no": "next check (drop or continuation)"},
            {"question": f"Added within the planned window ({params.get('planned_window_days', 28)} days) or pre-specified?", "yes": "Same line", "no": "New line"},
            {"question": "Agent dropped or same agents continue?", "yes": "Same line; name by the longer or larger regimen", "no": "New line"},
        ]
    return None


def _params_dict(card: RuleCard) -> dict[str, Any]:
    return {p.key: p.value for p in card.parameters}


def deterministic_fill(card: RuleCard, digest: dict[str, Any]) -> None:
    params = _params_dict(card)
    if not card.statement:
        card.statement = f"{card.objective}. Standard convention applied; parameters as listed."
        card.rationale = "The approved document does not settle this point; the firm's standard convention is used until an expert confirms it."
        card.confidence = RuleConfidence.BORROWED
    if not card.scenarios:
        card.scenarios = template_scenarios(card.card_key, params, digest)
    if card.visual_data is None:
        card.visual_data = template_visual(card.card_key, params, digest)


def apply_model_fill(card: RuleCard, item: dict, digest: dict[str, Any], catalogue: dict) -> None:
    card.statement = re.sub(r"\s+", " ", str(item.get("statement") or "")).strip()[:900]
    card.rationale = re.sub(r"\s+", " ", str(item.get("rationale") or "")).strip()[:700]
    try:
        card.confidence = RuleConfidence(str(item.get("confidence") or "borrowed"))
    except ValueError:
        card.confidence = RuleConfidence.BORROWED
    by_key = {p.key: p for p in card.parameters}
    for p in item.get("parameters") or []:
        if not isinstance(p, dict) or p.get("key") not in by_key:
            continue
        target = by_key[p["key"]]
        if p.get("value") not in (None, ""):
            target.value = p["value"]
        try:
            target.provenance = RuleProvenance(str(p.get("provenance") or target.provenance.value))
        except ValueError:
            pass
        if p.get("justification"):
            target.justification = str(p["justification"])[:300]
        if p.get("source"):
            target.source = str(p["source"])[:200]
    scenarios = []
    for sc in item.get("scenarios") or []:
        if not isinstance(sc, dict) or not sc.get("title"):
            continue
        events = []
        for ev in sc.get("events") or []:
            try:
                events.append(TimelineEvent(lane=str(ev.get("lane") or "")[:40], kind=str(ev.get("kind") or "regimen"),
                                            start_day=int(ev.get("start_day") or 0), end_day=int(ev.get("end_day") or 0),
                                            label=str(ev.get("label") or "")[:60]))
            except (TypeError, ValueError):
                continue
        if events:
            scenarios.append(RuleScenario(title=str(sc["title"])[:120], events=events,
                                          duration_days=max(int(sc.get("duration_days") or 300), max(e.end_day for e in events)),
                                          expected=str(sc.get("expected") or "")[:240]))
    card.scenarios = scenarios or template_scenarios(card.card_key, _params_dict(card), digest)
    vd = item.get("visual_data")
    if card.card_key == "bridging_cellular_therapy" and digest.get("procedures"):
        vd = card.visual_data  # registry procedure codes, set by the build
    card.visual_data = vd if isinstance(vd, list) and vd else template_visual(card.card_key, _params_dict(card), digest)
    card.sources = clean_sources([str(s) for s in (item.get("sources") or [])])
    card.gaps = [str(g)[:240] for g in (item.get("gaps") or [])][:6]
    if card.card_key == "market_basket":
        apply_basket_facts(card, digest)


def clean_sources(items: list[str]) -> list[str]:
    """Sources as a reader would recognise them: no stage names, no internal
    labels, no duplicates."""
    out: list[str] = []
    for raw in items:
        text = re.sub(r"\s+", " ", str(raw or "")).strip(" .;")
        if not text or _INTERNAL_SOURCE.match(text):
            continue
        if text.lower() not in [o.lower() for o in out]:
            out.append(text[:120])
    return out[:10]


def apply_basket_facts(card: RuleCard, digest: dict[str, Any]) -> None:
    """The model decides which agents belong and their role; the registries
    decide the codes. Rows the model wrote get their HCPCS, NDC, brand and
    route overwritten from the lookups; agents it left out are appended with
    an empty role so the reviewer sees them."""
    codes = digest.get("codes") or {}
    if not codes or not isinstance(card.visual_data, list):
        return
    by_key = {code_lookup.clean(k).lower(): v for k, v in codes.items()}

    def find(name: str) -> dict[str, Any] | None:
        key = code_lookup.clean(name).lower()
        if key in by_key:
            return by_key[key]
        for k, v in by_key.items():
            if key and (key in k or k in key):
                return v
            if any(b.lower() == key for b in v.get("brands") or []):
                return v
        return None

    seen: set[str] = set()
    rows = []
    for r in card.visual_data:
        if not isinstance(r, dict):
            continue
        fact = find(str(r.get("agent") or ""))
        if fact:
            seen.add(fact["agent"])
            r["hcpcs"] = [x["code"] for x in fact.get("hcpcs") or []]
            r["ndc"] = [x["ndc"] for x in (fact.get("ndcs") or [])[:4]]
            r["ndc_count"] = fact.get("ndc_count", 0)
            r["brand"] = r.get("brand") or ", ".join(fact.get("brands") or [])[:60]
            r["route"] = r.get("route") or fact.get("route") or ""
            r["class"] = r.get("class") or (fact.get("atc") or [""])[0][:60]
            r["source"] = ", ".join(fact.get("sources") or []) or r.get("source", "")
        else:
            r["hcpcs"], r["ndc"] = [], []
            r["source"] = "not in the registries"
        rows.append(r)
    for name, fact in codes.items():
        if name in seen:
            continue
        rows.append({"agent": name, "brand": ", ".join(fact.get("brands") or [])[:60],
                     "class": (fact.get("atc") or [""])[0][:60], "role": "", "route": fact.get("route") or "",
                     "hcpcs": [x["code"] for x in fact.get("hcpcs") or []],
                     "ndc": [x["ndc"] for x in (fact.get("ndcs") or [])[:4]], "ndc_count": fact.get("ndc_count", 0),
                     "days_of_supply": None, "grace_days": None,
                     "source": ", ".join(fact.get("sources") or []) + " (not placed by the model)"})
    card.visual_data = rows


# -- orchestration ----------------------------------------------------------------------
async def _status(run: Run, status: RulesStatus | None, message: str, progress: float) -> None:
    if status is not None:
        run.rules_status = status
    run.rules_message = message
    store.save_run(run)
    await bus.publish(run.id, "rules_status", status=run.rules_status.value, message=message,
                      progress=round(progress, 3))


async def build_rules(run_id: str, registry: dict) -> None:
    run = store.get_run(run_id)
    if run is None:
        return
    await bus.reopen(run_id)
    run.rules_started_at = utcnow()
    run.rules_error = ""
    await _status(run, RulesStatus.RUNNING, "Reading the approved document", 0.05)
    try:
        catalogue = {c["key"]: c for c in catalogue_for(run.config.indication_key)}
        cards = blank_cards(run)
        digest = document_digest(run)

        async def progress(msg: str) -> None:
            await _status(run, None, msg, 0.08)

        await enrich_digest(digest, progress)
        digest_text = _digest_text(digest)
        rules_cfg = get_thresholds().get("rules") or {}
        budget = WebBudget(int(rules_cfg.get("max_web_questions", 3)))
        context = {"drugs": [a["agent"] for a in digest.get("agents") or []][:20]}

        research: dict[str, str] = {}
        research_cites: dict[str, list[str]] = {}
        research_ev: dict[str, list[str]] = {}
        to_research = [c for c in catalogue.values() if research_items(c)]
        for i, c in enumerate(to_research, start=1):
            await _status(run, None, f"Researching: {c['title']}", 0.12 + 0.43 * i / max(len(to_research), 1))
            text, cites, ev = await research_card(run, c, registry, budget, context)
            research[c["key"]], research_cites[c["key"]] = text, cites
            research_ev[c["key"]] = [e.id for e in ev]

        by_key = {c.card_key: c for c in cards}
        if "bridging_cellular_therapy" in by_key and digest.get("procedures"):
            # The CMS procedure codes ride on the transplant card so the
            # reviewer and the specification see them next to the rule.
            by_key["bridging_cellular_therapy"].visual_data = [
                {"system": p["system"], "code": p["code"], "description": p["description"],
                 "use": "CAR-T" if p["code"].startswith("XW") else "transplant"}
                for p in digest["procedures"]]
        for j, sec in enumerate(sections(), start=1):
            sec_cards = [catalogue[c.card_key] for c in cards if c.section == sec["key"]]
            if not sec_cards:
                continue
            await _status(run, None, f"Writing section {sec['number']}: {sec['title']}",
                          0.55 + 0.4 * j / len(sections()))
            filled = await fill_section(run, sec, sec_cards, digest_text, research)
            for spec in sec_cards:
                card = by_key[spec["key"]]
                if spec["key"] in filled:
                    apply_model_fill(card, filled[spec["key"]], digest, spec)
                deterministic_fill(card, digest)
                card.sources = clean_sources(card.sources + research_cites.get(spec["key"], []))
                card.evidence_ids = research_ev.get(spec["key"], [])
            store.save_rule_cards(run.id, [by_key[s["key"]] for s in sec_cards])

        run.rules_finished_at = utcnow()
        await _status(run, RulesStatus.REVIEW, f"{len(cards)} rule cards ready for review", 1.0)
        await bus.publish(run.id, "rules_complete", redirect=f"/runs/{run.id}/rules")
    except Exception as exc:  # noqa: BLE001
        log.exception("rules build failed for %s", run_id)
        run.rules_error = f"{type(exc).__name__}: {exc}"
        await _status(run, RulesStatus.FAILED, run.rules_error[:200], 1.0)
    finally:
        await bus.close(run_id)


# -- outputs --------------------------------------------------------------------------------
def spec(run: Run, cards: list[RuleCard]) -> dict[str, Any]:
    """The machine-readable rules specification the data run executes."""
    by_key = {c.card_key: c for c in cards}

    def params(key: str) -> dict[str, Any]:
        c = by_key.get(key)
        return {p.key: p.value for p in c.parameters} if c else {}

    basket = by_key.get("market_basket")
    return {
        "celestra_rules_spec": 1,
        "indication": run.config.indication,
        "indication_key": run.config.indication_key,
        "geography": run.config.geography,
        "generated": utcnow().isoformat(),
        "status": run.rules_status.value,
        "market_basket": basket.visual_data if basket else [],
        "index_diagnosis": {"codes": (by_key["index_diagnosis"].visual_data if "index_diagnosis" in by_key else []),
                            **params("index_diagnosis")},
        "funnel": {"steps": (by_key["funnel"].visual_data if "funnel" in by_key else []), **params("funnel")},
        "indication_attribution": params("indication_attribution"),
        "episodes": {**params("days_of_supply"), **params("regimen_construction")},
        "lines": {k: params(k) for k in ("line_numbering", "protocol_phases", "gap_rule", "planned_vs_reactive",
                                         "product_drop") if k in by_key},
        "procedures": [
            {"system": e.get("system"), "code": e.get("code"), "description": e.get("description")}
            for e in (by_key["bridging_cellular_therapy"].visual_data or []) if isinstance(e, dict) and e.get("code")
        ] if "bridging_cellular_therapy" in by_key and isinstance(by_key["bridging_cellular_therapy"].visual_data, list) else [],
        "decision_flow": by_key["decision_flow"].visual_data if "decision_flow" in by_key else [],
        "rules": [
            {"key": c.card_key, "number": c.number, "section": c.section, "title": c.title,
             "statement": c.statement, "confidence": c.confidence.value,
             "parameters": [p.model_dump(mode="json") for p in c.parameters],
             "scenarios": [s.model_dump(mode="json") for s in c.scenarios],
             "sources": c.sources, "gaps": c.gaps, "review": c.review_action.value,
             "reviewer_note": c.reviewer_note}
            for c in cards
        ],
    }


def gate(run: Run, cards: list[RuleCard]) -> dict[str, Any]:
    blockers = [{"id": c.id, "title": c.title, "reason": f"{c.confidence.label}: a person must confirm it.",
                 "anchor": f"#rule-{c.id}"} for c in cards if c.needs_decision]
    return {
        "blockers": blockers,
        "available": run.rules_status is RulesStatus.REVIEW,
        "can_proceed": run.rules_status is RulesStatus.REVIEW and not blockers,
        "done": run.rules_status is RulesStatus.APPROVED,
        "counts": {
            "cards": len(cards),
            "decided": sum(1 for c in cards if c.review_action.is_decided),
            "needs": len(blockers),
            "verified": sum(1 for c in cards if c.confidence in (RuleConfidence.VERIFIED_INDICATION, RuleConfidence.VERIFIED_CLASS)),
            "borrowed": sum(1 for c in cards if c.confidence is RuleConfidence.BORROWED),
            "original": sum(1 for c in cards if c.confidence is RuleConfidence.ORIGINAL),
        },
    }


def apply_review(card: RuleCard, action: str, note: str, edits: dict[str, Any]) -> RuleCard:
    """A reviewer's decision on a rule: approve as written, edit parameters
    (provenance becomes client), or add a note. Any of them settles the card."""
    changed = []
    for p in card.parameters:
        if p.key in edits and str(edits[p.key]).strip() not in ("", str(p.value)):
            raw = str(edits[p.key]).strip()
            try:
                p.value = int(raw) if re.fullmatch(r"-?\d+", raw) else (float(raw) if re.fullmatch(r"-?\d+\.\d+", raw) else raw)
            except ValueError:
                p.value = raw
            p.provenance = RuleProvenance.CLIENT
            p.justification = note[:300] if note else "Set by the reviewer."
            changed.append(p.label)
    if action == "edit" and changed:
        card.review_action = ReviewAction.MODIFIED
    elif action == "input":
        card.review_action = ReviewAction.INPUT_ADDED
    else:
        card.review_action = ReviewAction.APPROVED
    card.reviewer_note = note[:500]
    card.reviewed_at = utcnow()
    if changed:
        # Re-draw the worked scenarios with the new values so the picture
        # matches the parameters the reviewer chose. The agent names come from
        # the lanes the model already used; the model's visual data is kept.
        lanes = [e.lane for sc in card.scenarios for e in sc.events if e.lane not in ("Line", "Gap")]
        digest = {"agents": [{"agent": n} for n in dict.fromkeys(lanes)]}
        card.scenarios = template_scenarios(card.card_key, _params_dict(card), digest) or card.scenarios
    return card


def serialise(card: RuleCard) -> str:
    return json.dumps(card.model_dump(mode="json"), indent=2)


# -- timeline layout: rows within lanes, labels that never collide ---------------------
def timeline_layout(scenario: Any, track_px: int = 360) -> dict[str, Any]:
    """Place a scenario's events on tracks. Events in one lane that overlap
    in time go on separate rows; a label goes inside its bar when it fits,
    after it when the space to the next bar allows, and otherwise into the
    key below the chart. Positions are percentages of the track."""
    events = list(getattr(scenario, "events", None) or [])
    total = int(getattr(scenario, "duration_days", 0) or 0)
    total = max(total, max((int(e.end_day) for e in events), default=0), 1)
    px = max(int(track_px or 360), 120)
    char_px = 6.4
    lanes: list[dict[str, Any]] = []
    order: dict[str, int] = {}
    for e in events:
        if e.lane not in order:
            order[e.lane] = len(lanes)
            lanes.append({"lane": e.lane, "is_line": e.lane == "Line", "rows": []})
    key: list[dict[str, Any]] = []
    for lane in lanes:
        mine = sorted((e for e in events if e.lane == lane["lane"]), key=lambda e: (e.start_day, e.end_day))
        rows: list[list[Any]] = []
        row_end: list[int] = []
        for e in mine:
            start, end = int(e.start_day), int(e.end_day)
            placed = False
            for idx, last in enumerate(row_end):
                if start >= last:
                    rows[idx].append(e)
                    row_end[idx] = max(end, start + 1)
                    placed = True
                    break
            if not placed:
                rows.append([e])
                row_end.append(max(end, start + 1))
        laid: list[list[dict[str, Any]]] = []
        for row in rows:
            out: list[dict[str, Any]] = []
            for n, e in enumerate(row):
                start, end = int(e.start_day), int(e.end_day)
                point = e.kind == "dx" or end <= start
                left = round(start / total * 100, 2)
                width = round(max((end - start) / total * 100, 1.2), 2)
                item = {"kind": e.kind, "label": e.label, "start_day": start, "end_day": end,
                        "left": left, "width": width, "point": point, "label_mode": "in"}
                if not point:
                    need = len(e.label) * char_px + 12
                    have = width / 100 * px
                    nxt = row[n + 1] if n + 1 < len(row) else None
                    gap_pct = ((int(nxt.start_day) - end) / total * 100) if nxt else (100 - left - width)
                    gap_px = gap_pct / 100 * px
                    if have >= need:
                        item["label_mode"] = "in"
                    elif gap_px >= need:
                        item["label_mode"] = "out"
                    else:
                        item["label_mode"] = "tip"
                        key.append({"kind": e.kind, "label": e.label, "start_day": start, "end_day": end})
                out.append(item)
            laid.append(out)
        lane["rows"] = laid or [[]]
    return {"lanes": lanes, "total": total, "key": key}
