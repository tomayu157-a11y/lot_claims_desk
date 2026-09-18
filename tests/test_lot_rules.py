"""The line-of-therapy rules stage, end to end.

Part one checks the pieces without a server: the catalogue is filtered per
indication, blank cards carry the standard parameters, template scenarios
draw real timelines, the specification and the sign-off gate read the cards
correctly, and a reviewer's edit is recorded with client provenance.

Part two drives the real ASGI app with the offline demo registry: a finished
demo run is approved, the rules stage is started, it reaches review, borrowed
rules block sign-off until a person decides them, and the approved rules
produce the specification and the business-rules document.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

import celestra.main as app_mod
import celestra.services.lot_rules as rules_mod
import celestra.services.orchestrator as orch_mod
import celestra.store as store_mod
from celestra.demo import build_registry, seed
from celestra.models import (
    ReviewAction,
    RuleConfidence,
    RuleProvenance,
    RulesStatus,
    Run,
    RunConfig,
    RunMode,
    RunStatus,
    utcnow,
)
from celestra.services import lot_rules
from celestra.store import Store

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def unit_checks() -> None:
    print("\n== catalogue ==")
    all_cards = lot_rules.catalogue_for("ALL")
    cll_cards = lot_rules.catalogue_for("CLL")
    all_keys = {c["key"] for c in all_cards}
    cll_keys = {c["key"] for c in cll_cards}
    check("ALL follows the curative-intent template",
          {"protocol_phases", "planned_vs_reactive", "bridging_cellular_therapy", "maintenance"} <= all_keys
          and "substitution" not in all_keys, sorted(all_keys - cll_keys).__repr__())
    check("CLL follows the chronic-targeted template",
          {"substitution", "maintenance"} <= cll_keys
          and "planned_vs_reactive" not in cll_keys and "bridging_cellular_therapy" not in cll_keys
          and "protocol_phases" not in cll_keys,
          sorted(cll_keys - all_keys).__repr__())
    check("the cleansing and sensitivity cards are gone",
          not ({"short_regimen_rules", "sensitivity_grid"} & (all_keys | cll_keys)))
    shared = {"market_basket", "index_diagnosis", "funnel", "days_of_supply", "regimen_construction",
              "line_numbering", "gap_rule", "product_addition", "product_drop", "decision_flow"}
    check("both indications share the core rules", shared <= all_keys & cll_keys)
    numbers = [c["number"] for c in all_cards]
    check("cards are numbered in order", numbers == sorted(numbers) and len(set(numbers)) == len(numbers))
    check("every card names a visual",
          all(c.get("visual") in {"basket", "codes", "funnel", "timeline", "flow", "parameters", "grid"}
              for c in all_cards + cll_cards))
    secs = lot_rules.sections()
    check("four sections", len(secs) == 4 and [x["key"] for x in secs] == [
        "market_basket", "patient_funnel", "episodes_regimens", "lot_rules"], str([x["key"] for x in secs]))
    items = lot_rules.research_items(next(c for c in all_cards if c["key"] == "market_basket"))
    check("code questions never reach the web", items and not items[0]["web"])
    items = lot_rules.research_items(next(c for c in all_cards if c["key"] == "gap_rule"))
    check("methodology questions may reach the web", items and items[0]["web"])
    check("every card belongs to a section",
          {c["section"] for c in all_cards + cll_cards} <= {s["key"] for s in secs})

    print("\n== blank cards and template scenarios ==")
    cfg = RunConfig(indication="Chronic Lymphocytic Leukemia (CLL)", indication_key="CLL",
                    drug_brand="Venclexta", mode=RunMode.FULL, research_cutoff=orch_mod.default_cutoff())
    run = Run(config=cfg, reference="RUN-TEST", status=RunStatus.APPROVED)
    cards = lot_rules.blank_cards(run)
    check("one blank card per catalogue entry", len(cards) == len(cll_cards))
    gap = next(c for c in cards if c.card_key == "gap_rule")
    gp = {p.key: p for p in gap.parameters}
    check("gap rule carries the standard 60-day parameter",
          "gap_days" in gp and gp["gap_days"].value == 60 and gp["gap_days"].provenance is RuleProvenance.STANDARD,
          str({k: v.value for k, v in gp.items()}))
    check("standard parameters list their alternatives", bool(gp["gap_days"].alternatives))

    digest = {"agents": [{"agent": "venetoclax"}, {"agent": "ibrutinib"}]}
    scenarios = lot_rules.template_scenarios("gap_rule", {"gap_days": 60}, digest)
    check("gap rule draws two scenarios", len(scenarios) == 2)
    lanes = {e.lane for s in scenarios for e in s.events}
    check("scenarios use the document's agents", "Venetoclax" in lanes and "Line" in lanes, str(lanes))
    check("the first scenario advances the line",
          any(e.label == "2L" for e in scenarios[0].events) and "2L" in scenarios[0].expected)
    check("the second scenario stays on one line",
          not any(e.label == "2L" for e in scenarios[1].events))
    check("events fit their duration",
          all(e.end_day <= s.duration_days for s in scenarios for e in s.events))
    scenarios90 = lot_rules.template_scenarios("gap_rule", {"gap_days": 90}, digest)
    check("the parameter value drives the picture",
          scenarios90[0].title != scenarios[0].title and "90" in scenarios90[0].title)
    for key in ("product_addition", "product_drop", "line_numbering"):
        sc = lot_rules.template_scenarios(key, {}, digest)
        check(f"{key} has a worked scenario", bool(sc) and all(s.events for s in sc))
    funnel = lot_rules.template_visual("funnel", {}, digest)
    flow = lot_rules.template_visual("decision_flow", {"gap_days": 60}, digest)
    check("funnel template has ordered steps", isinstance(funnel, list) and len(funnel) >= 4)
    check("decision flow template is a ladder of checks",
          isinstance(flow, list) and all({"question", "yes", "no"} <= set(r) for r in flow))

    print("\n== deterministic fill, spec, gate ==")
    for c in cards:
        lot_rules.deterministic_fill(c, digest)
    check("every card has a statement without a model", all(c.statement for c in cards))
    check("timeline cards have scenarios",
          all(c.scenarios for c in cards if c.visual == "timeline"),
          str([c.card_key for c in cards if c.visual == "timeline" and not c.scenarios]))
    check("visual cards have visual data",
          all(c.visual_data for c in cards if c.visual in ("funnel", "flow")),
          str([c.card_key for c in cards if c.visual in ("funnel", "flow") and not c.visual_data]))
    all_run = Run(config=RunConfig(indication="Acute Lymphoblastic Leukemia (ALL)", indication_key="ALL",
                                   drug_brand="Blincyto", mode=RunMode.FULL,
                                   research_cutoff=orch_mod.default_cutoff()),
                  reference="RUN-ALL", status=RunStatus.APPROVED)
    phases = lot_rules.template_scenarios("protocol_phases", {}, {"agents": [{"agent": "vincristine"}, {"agent": "blinatumomab"}]})
    check("protocol phases draw one line across every phase",
          len(phases) == 2 and sum(1 for e in phases[0].events if e.kind == "line") == 1
          and any(e.label == "2L" for e in phases[1].events))
    check("ALL blank cards include the protocol-phase card",
          any(c.card_key == "protocol_phases" for c in lot_rules.blank_cards(all_run)))
    check("unverified fills are marked borrowed", all(c.confidence is RuleConfidence.BORROWED for c in cards))
    run.rules_status = RulesStatus.REVIEW
    g = lot_rules.gate(run, cards)
    check("gate blocks while borrowed rules are undecided",
          g["available"] and not g["can_proceed"] and len(g["blockers"]) == len(cards))
    spec = lot_rules.spec(run, cards)
    check("spec carries the gap parameter", spec["lines"]["gap_rule"]["gap_days"] == 60)
    check("spec lists every rule", len(spec["rules"]) == len(cards))
    check("spec is JSON-serialisable", bool(json.dumps(spec)))
    check("spec no longer carries cleansing or sensitivity",
          "cleansing" not in spec and "sensitivity" not in spec and "procedures" in spec)

    print("\n== registry facts on the basket ==")
    digest_codes = {"codes": {
        "blinatumomab": {"agent": "blinatumomab", "brands": ["BLINCYTO"], "atc": ["L01FX Other monoclonal antibodies"],
                         "route": "INTRAVENOUS", "hcpcs": [{"code": "J9039", "description": "Injection, blinatumomab", "status": ""}],
                         "ndcs": [{"ndc": "55513-160"}], "ndc_count": 1,
                         "sources": ["NLM Clinical Tables (HCPCS)", "FDA NDC Directory (openFDA)"]},
        "ponatinib": {"agent": "ponatinib", "brands": ["Iclusig"], "atc": ["L01EA BCR-ABL tyrosine kinase inhibitors"],
                      "route": "ORAL", "hcpcs": [], "ndcs": [{"ndc": "63020-533"}], "ndc_count": 9,
                      "sources": ["FDA NDC Directory (openFDA)"]},
    }}
    basket = next(c for c in cards if c.card_key == "market_basket")
    basket.visual_data = [{"agent": "Blincyto", "role": "branded", "hcpcs": ["J9229"], "ndc": ["0000-0000"]}]
    lot_rules.apply_basket_facts(basket, digest_codes)
    rows = {r["agent"]: r for r in basket.visual_data}
    check("model's wrong code is replaced by the registry's", rows["Blincyto"]["hcpcs"] == ["J9039"])
    check("brand name matched to its generic", rows["Blincyto"]["ndc"] == ["55513-160"])
    check("agents the model left out are appended for the reviewer",
          "ponatinib" in rows and rows["ponatinib"]["role"] == "" and "not placed" in rows["ponatinib"]["source"])
    visual = lot_rules.template_visual("market_basket", {"grace_multiplier": 2}, digest_codes)
    check("basket without a model is built from the registries",
          any(r["agent"] == "ponatinib" and r["days_of_supply"] == 30 and r["grace_days"] == 60 for r in visual))
    cleaned = lot_rules.clean_sources(["stage_2 FDA-approved therapies answer", "FDA label for Blincyto",
                                       "market basket research notes", "local reference file", "NLM HCPCS table",
                                       "fda label for blincyto", "Firm standard convention"])
    check("internal labels are dropped from sources", cleaned == ["FDA label for Blincyto", "NLM HCPCS table"], str(cleaned))

    print("\n== timeline layout ==")
    from celestra.models import RuleScenario, TimelineEvent as TE
    sc = RuleScenario(title="overlap", duration_days=300, expected="", events=[
        TE(lane="Doxorubicin", kind="claim", start_day=0, end_day=50, label="doxorubicin claim"),
        TE(lane="Doxorubicin", kind="claim", start_day=0, end_day=50, label="doxorubicin claim"),
        TE(lane="Doxorubicin", kind="episode", start_day=0, end_day=120, label="doxorubicin episode"),
        TE(lane="Doxorubicin", kind="claim", start_day=130, end_day=140, label="a very long label that cannot fit"),
        TE(lane="Doxorubicin", kind="claim", start_day=141, end_day=300, label="next"),
        TE(lane="Index", kind="dx", start_day=0, end_day=0, label="Index diagnosis"),
        TE(lane="Line", kind="line", start_day=0, end_day=300, label="1L"),
    ])
    lay = lot_rules.timeline_layout(sc, 360)
    dox = next(l for l in lay["lanes"] if l["lane"] == "Doxorubicin")
    check("overlapping events go on separate rows", len(dox["rows"]) == 3, str(len(dox["rows"])))
    check("no two bars on one row overlap",
          all(row[i]["left"] + row[i]["width"] <= row[i + 1]["left"] + 0.01
              for row in dox["rows"] for i in range(len(row) - 1)))
    modes = {e["label"]: e["label_mode"] for row in dox["rows"] for e in row}
    check("a label that fits stays inside its bar", modes["doxorubicin episode"] == "in", str(modes))
    check("a label with no room goes to the key",
          modes["a very long label that cannot fit"] == "tip" and lay["key"], str(modes))
    idx = next(l for l in lay["lanes"] if l["lane"] == "Index")
    check("diagnosis is a point marker", idx["rows"][0][0]["point"])
    check("the line lane is flagged", next(l for l in lay["lanes"] if l["lane"] == "Line")["is_line"])
    wide = lot_rules.timeline_layout(sc, 900)
    wmodes = {e["label"]: e["label_mode"] for l in wide["lanes"] for row in l["rows"] for e in row}
    check("a wider track fits more labels inside", wmodes["doxorubicin claim"] == "in")

    print("\n== reviewer decisions ==")
    edited = lot_rules.apply_review(gap, "edit", "Client uses 90 days for oral agents.", {"gap_days": "90"})
    check("edit records the client's value",
          {p.key: p.value for p in edited.parameters}["gap_days"] == 90
          and {p.key: p.provenance for p in edited.parameters}["gap_days"] is RuleProvenance.CLIENT)
    check("edit is a decision", edited.review_action is ReviewAction.MODIFIED and not edited.needs_decision)
    check("scenarios are redrawn with the new value", "90" in edited.scenarios[0].title)
    other = next(c for c in cards if c.card_key == "product_addition")
    lot_rules.apply_review(other, "approve", "", {})
    check("approve settles the card", other.review_action is ReviewAction.APPROVED and not other.needs_decision)
    third = next(c for c in cards if c.card_key == "line_numbering")
    lot_rules.apply_review(third, "input", "Index date is the first venetoclax claim.", {})
    check("input settles the card and keeps the note",
          third.review_action is ReviewAction.INPUT_ADDED and "venetoclax" in third.reviewer_note)
    g = lot_rules.gate(run, cards)
    check("gate counts decisions", g["counts"]["decided"] == 3 and g["counts"]["needs"] == len(cards) - 3)


async def wait_rules(store: Store, run_id: str, wanted: set[RulesStatus], timeout: float = 600) -> RulesStatus:
    for _ in range(int(timeout * 2)):
        run = store.get_run(run_id)
        if run and run.rules_status in wanted:
            return run.rules_status
        await asyncio.sleep(0.5)
    run = store.get_run(run_id)
    return run.rules_status if run else RulesStatus.FAILED


async def http_flow() -> None:
    db = Path("/tmp/celestra_rules.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    store = Store(db)
    for mod in (store_mod, orch_mod, app_mod, rules_mod):
        mod.store = store
    import celestra.demo as demo_mod
    demo_mod.store = store
    app_mod._REGISTRY = build_registry()

    async def fake_lookup(names, limit=40, concurrency=4):
        return {n: {"agent": n, "brands": [n.title()], "atc": ["L01XX"], "route": "ORAL", "hcpcs": [],
                    "ndcs": [{"ndc": "00000-000"}], "ndc_count": 1, "sources": ["FDA NDC Directory (openFDA)"],
                    "schedule": "", "indications": "", "errors": {}} for n in names[:limit]}

    async def fake_procs(terms, limit=40):
        return {"codes": [{"system": "ICD-10-PCS", "code": "XW033C7", "description": "CAR-T, peripheral vein"}]}

    rules_mod.code_lookup.lookup_agents = fake_lookup
    rules_mod.code_lookup.procedure_codes = fake_procs

    print("\n== a demo run, approved ==")
    run = await seed("CLL")
    check("demo run completed", run.status is RunStatus.COMPLETED, run.status.value)
    run.status = RunStatus.APPROVED
    run.approved_at = utcnow()
    store.save_run(run)
    run_id = run.id

    transport = httpx.ASGITransport(app=app_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/runs/{run_id}/report")
        check("approved document offers the rules stage", r.status_code == 200 and f"/runs/{run_id}/rules" in r.text)
        r = await c.get(f"/runs/{run_id}/rules")
        check("rules page shows the start state", r.status_code == 200 and "rules/start" in r.text)
        check("the stepper includes the rules step", "LOT rules" in r.text)

        print("\n== building the rules ==")
        r = await c.post(f"/runs/{run_id}/rules/start")
        check("start redirects to the workspace", r.status_code == 303 and r.headers["location"].endswith("/rules"))
        r = await c.get(f"/runs/{run_id}/rules")
        check("workspace polls while building", r.status_code == 200 and "data-rules-poll" in r.text)
        status = await wait_rules(store, run_id, {RulesStatus.REVIEW, RulesStatus.FAILED})
        run = store.get_run(run_id)
        check("rules reach review", status is RulesStatus.REVIEW, f"{status.value}: {run.rules_error}")
        cards = store.get_rule_cards(run_id)
        expected = len(lot_rules.catalogue_for("CLL"))
        check("every catalogue card was written", len(cards) == expected, f"{len(cards)} of {expected}")
        check("cards are saved in order", [c.number for c in cards] == sorted(c.number for c in cards))
        check("every card has a statement", all(c.statement for c in cards))
        check("timeline cards carry scenarios", all(c.scenarios for c in cards if c.visual == "timeline"))
        basket = next(c for c in cards if c.card_key == "market_basket")
        check("basket rows carry registry codes",
              isinstance(basket.visual_data, list) and basket.visual_data
              and all(r.get("ndc") == ["00000-000"] for r in basket.visual_data), str(basket.visual_data[:1]))
        check("no internal source labels on any card",
              not any(lot_rules._INTERNAL_SOURCE.match(s) for c in cards for s in c.sources))
        r = await c.get(f"/runs/{run_id}/rules/status")
        check("status endpoint reports review", r.json()["status"] == "review" and r.json()["cards"] == expected)

        print("\n== the workspace ==")
        r = await c.get(f"/runs/{run_id}/rules")
        check("workspace renders the review state", r.status_code == 200 and "data-rules-tabs" in r.text)
        check("cards are drawn", r.text.count('class="rcard') >= expected, str(r.text.count('class="rcard')))
        check("timelines are drawn", "tl-bar" in r.text)
        check("the funnel is drawn", "funnel-step" in r.text)
        check("the decision ladder is drawn", "flow-step" in r.text)
        check("no internal vocabulary", "bucket" not in r.text.lower())
        r = await c.get(f"/runs/{run_id}/rules/gate")
        check("gate panel renders", r.status_code == 200 and "need" in r.text)

        print("\n== decisions gate the sign-off ==")
        r = await c.post(f"/runs/{run_id}/rules/approve")
        check("approval refused while rules need a decision", r.status_code == 409, f"HTTP {r.status_code}")
        gap = next(c for c in cards if c.card_key == "gap_rule")
        r = await c.get(f"/runs/{run_id}/rules/cards/{gap.id}/edit")
        check("edit dialog renders", r.status_code == 200 and "param_gap_days" in r.text)
        r = await c.post(f"/runs/{run_id}/rules/cards/{gap.id}/edit",
                         data={"note": "Client convention: 90 days.", "param_gap_days": "90"})
        check("edit returns the card", r.status_code == 200 and "90" in r.text)
        saved = store.get_rule_card(run_id, gap.id)
        check("edit persisted with client provenance",
              saved is not None and {p.key: p.value for p in saved.parameters}["gap_days"] == 90
              and {p.key: p.provenance for p in saved.parameters}["gap_days"] is RuleProvenance.CLIENT)
        for card in cards:
            if card.id == gap.id:
                continue
            if card.needs_decision:
                r = await c.post(f"/runs/{run_id}/rules/cards/{card.id}/approve", data={"note": ""})
                if r.status_code != 200:
                    check(f"approve {card.card_key}", False, f"HTTP {r.status_code}")
        r = await c.get(f"/runs/{run_id}/rules/gate")
        check("gate clears once every rule is decided", "Every rule is verified or decided" in r.text)
        r = await c.post(f"/runs/{run_id}/rules/approve")
        check("rules approved", r.status_code == 303, f"HTTP {r.status_code}")
        run = store.get_run(run_id)
        check("run records the approval",
              run.rules_status is RulesStatus.APPROVED and run.rules_approved_at is not None
              and run.phase == "rules_approved")
        r = await c.post(f"/runs/{run_id}/rules/cards/{gap.id}/approve", data={"note": ""})
        check("decisions are locked after approval", r.status_code == 409, f"HTTP {r.status_code}")

        print("\n== outputs ==")
        r = await c.get(f"/runs/{run_id}/rules/spec.json")
        check("spec downloads", r.status_code == 200 and "attachment" in r.headers.get("content-disposition", ""))
        spec = r.json()
        check("spec carries the client's gap", spec["lines"]["gap_rule"]["gap_days"] == 90)
        check("spec status is approved", spec["status"] == "approved")
        r = await c.get(f"/runs/{run_id}/rules/document")
        check("rules document renders", r.status_code == 200 and "tl-bar" in r.text)
        r = await c.get(f"/runs/{run_id}/rules/document", params={"download": 1})
        check("rules document downloads", "attachment" in r.headers.get("content-disposition", ""))
        r = await c.get(f"/runs/{run_id}", follow_redirects=False)
        check("run root opens the rules workspace", r.headers.get("location", "").endswith("/rules"))
        r = await c.post(f"/runs/{run_id}/rules/start", follow_redirects=False)
        check("rebuild is refused after approval", r.status_code == 303)
        check("cards survive the refused rebuild", len(store.get_rule_cards(run_id)) == expected)


async def main() -> int:
    unit_checks()
    await http_flow()
    print(f"\n{len(failures)} failure(s)")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
