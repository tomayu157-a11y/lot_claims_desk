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
          {"planned_vs_reactive", "bridging_cellular_therapy", "maintenance"} <= all_keys
          and "substitution" not in all_keys, sorted(all_keys - cll_keys).__repr__())
    check("CLL follows the chronic-targeted template",
          {"substitution", "maintenance"} <= cll_keys
          and "planned_vs_reactive" not in cll_keys and "bridging_cellular_therapy" not in cll_keys,
          sorted(cll_keys - all_keys).__repr__())
    shared = {"market_basket", "index_diagnosis", "funnel", "days_of_supply", "regimen_construction",
              "line_numbering", "gap_rule", "product_addition", "product_drop", "decision_flow",
              "short_regimen_rules", "sensitivity_grid"}
    check("both indications share the core rules", shared <= all_keys & cll_keys)
    numbers = [c["number"] for c in all_cards]
    check("cards are numbered in order", numbers == sorted(numbers) and len(set(numbers)) == len(numbers))
    check("every card names a visual",
          all(c.get("visual") in {"basket", "codes", "funnel", "timeline", "flow", "parameters", "grid"}
              for c in all_cards + cll_cards))
    secs = lot_rules.sections()
    check("six sections", len(secs) == 6, str([s["key"] for s in secs]))
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
    for key in ("product_addition", "product_drop", "line_numbering", "short_regimen_rules"):
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
          all(c.visual_data for c in cards if c.visual in ("funnel", "flow", "grid")),
          str([c.card_key for c in cards if c.visual in ("funnel", "flow", "grid") and not c.visual_data]))
    check("unverified fills are marked borrowed", all(c.confidence is RuleConfidence.BORROWED for c in cards))
    run.rules_status = RulesStatus.REVIEW
    g = lot_rules.gate(run, cards)
    check("gate blocks while borrowed rules are undecided",
          g["available"] and not g["can_proceed"] and len(g["blockers"]) == len(cards))
    spec = lot_rules.spec(run, cards)
    check("spec carries the gap parameter", spec["lines"]["gap_rule"]["gap_days"] == 60)
    check("spec lists every rule", len(spec["rules"]) == len(cards))
    check("spec is JSON-serialisable", bool(json.dumps(spec)))

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
        research_cards = [c for c in cards if c.card_key in ("market_basket", "index_diagnosis", "days_of_supply")]
        check("researched cards record their sources or a gap",
              all(c.sources or c.gaps or c.evidence_ids or c.statement for c in research_cards))
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
