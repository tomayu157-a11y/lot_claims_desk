"""Reviewer files reach a question's answering calls as framing only: a
separate block, never a numbered DOCUMENT, never citable, and never used for
search terms, connector context or query drafting."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celestra.connectors.base import ConnectorResult
from celestra.connectors.firecrawl import firecrawl_blocked
from celestra.models import EvidenceOrigin, ResearchQuestion, RunConfig, SourceRef
from celestra.services import answering, ranking, retrieval
from celestra.services.answering import answer_batch
from celestra.services.extraction import build_terms
from celestra.services.llm import LLMUnavailable

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


TEXT = ("The overall rate of new cases of chronic lymphocytic leukemia was 4.7 per 100,000 men "
        "and women per year based on 2018-2022 cases, age-adjusted.")
DOC = SourceRef(source_id="seer", source_name="NCI SEER", organization="NCI SEER", tier=1,
                url="https://seer.cancer.gov/x", title="CLL Stat Facts", snippet=TEXT)
Q = "What is the incidence of CLL in the United States?"
TERMS = build_terms(Q, ["incidence"], ["chronic lymphocytic leukemia"])
REVIEWER_TEXT = ("Frontline venetoclax-obinutuzumab is preferred for del(17p) patients under "
                 "our payer policy.")
DOCS = [{"filename": "Payer policy.pdf", "card": "First-line regimens",
         "sections": [{"heading": "Frontline regimens covered", "text": REVIEWER_TEXT}]}]


class FakeLLM:
    def __init__(self, payload):
        self.payload, self.available, self.prompts = payload, True, []

    async def complete_json(self, system, prompt, **kw):
        self.prompts.append(prompt)
        return self.payload


class NoLLM:
    available = False

    async def complete_json(self, *a, **kw):
        raise LLMUnavailable("no model in this test")

    async def complete(self, *a, **kw):
        raise LLMUnavailable("no model in this test")


async def ask(payload, documents):
    fake = FakeLLM(payload)
    original = answering.llm
    answering.llm = fake
    try:
        ans, ev = await answer_batch(Q, ["incidence"], [DOC], "q1", TERMS,
                                     reviewer_documents=documents)
    finally:
        answering.llm = original
    return ans, ev, (fake.prompts[0] if fake.prompts else "")


class Recorder:
    """A connector that answers with one document and records its context."""

    def __init__(self, sid, tier, origin=EvidenceOrigin.APPROVED_API):
        self.sid, self.tier, self.origin, self.extras = sid, tier, origin, []

    def _ref(self, sid=None, url=None):
        return SourceRef(source_id=sid or self.sid, source_name=self.sid, tier=self.tier,
                         url=url or f"https://{self.sid}.example/cll", title="CLL incidence",
                         organization=self.sid, snippet=TEXT, origin=self.origin,
                         raw={"text": TEXT, "abstract": TEXT})

    async def discover(self, ctx, limit):
        self.extras.append(dict(ctx.extra))
        return ConnectorResult(source_id=self.sid, refs=[self._ref()], ok=True, reason="")

    async def search(self, query, limit):
        return [self._ref("open_web", "https://web.example/cll")]

    async def scrape(self, url):
        return self._ref("open_web", url)


def run_retrieval(context):
    calls, drafts, upstream = [], [], []

    async def spy_answer(*args, **kwargs):
        calls.append({"round": kwargs.get("round_index"), "docs": kwargs.get("reviewer_documents")})
        return None, []

    async def spy_draft(question, cfg, notes):
        drafts.append(list(notes))
        return "chronic lymphocytic leukemia incidence united states"

    real_terms = retrieval.build_terms

    def spy_terms(question, aspects, synonyms, up=None):
        upstream.extend(up or [])
        return real_terms(question, aspects, synonyms, up)

    tiers = {s["id"]: s["tier"] for s in retrieval.get_source_registry()["sources"]}
    ids = [s["id"] for s in retrieval.sources_for("stage_1", "CLL")]
    ids += [s["id"] for s in retrieval.targeted_sources_for("stage_1", "CLL")]
    registry = {sid: Recorder(sid, tiers[sid]) for sid in ids}
    registry["open_web"] = Recorder("open_web", 3, EvidenceOrigin.OPEN_WEB)
    q = ResearchQuestion(stage="stage_1", bucket="A", text=Q, seed_text="incidence",
                         aspects=["incidence"])
    cfg = RunConfig(indication="Chronic Lymphocytic Leukemia", indication_key="CLL")

    saved = (retrieval.answer_batch, retrieval.draft_search_query, retrieval.build_terms,
             retrieval.llm, ranking.llm)
    retrieval.answer_batch, retrieval.draft_search_query = spy_answer, spy_draft
    retrieval.build_terms, retrieval.llm, ranking.llm = spy_terms, NoLLM(), NoLLM()
    try:
        asyncio.run(retrieval.retrieve(q, cfg, ["CLL"], registry, context=context))
    finally:
        (retrieval.answer_batch, retrieval.draft_search_query, retrieval.build_terms,
         retrieval.llm, ranking.llm) = saved
    extras = [e for c in registry.values() for e in c.extras]
    return calls, drafts, upstream, extras


async def answering_checks():
    print("\n== answering: a separate, unnumbered block ==")
    _, _, prompt = await ask({"status": "not_found", "answer": "", "support": []}, DOCS)
    check("block present", "REVIEWER-SUPPLIED FILES" in prompt)
    check("file and card named",
          '--- REVIEWER FILE: Payer policy.pdf (attached to "First-line regimens") ---' in prompt)
    check("section heading and text", "### Frontline regimens covered" in prompt and REVIEWER_TEXT in prompt)
    check("told never to cite it", "never quote or cite" in prompt)
    check("only the source is a DOCUMENT", prompt.count("=== DOCUMENT") == 1)
    check("block comes before the sources", prompt.index("REVIEWER-SUPPLIED FILES") < prompt.index("=== DOCUMENT 0"))
    _, _, plain = await ask({"status": "not_found", "answer": "", "support": []}, None)
    check("no block without files", "REVIEWER-SUPPLIED FILES" not in plain)

    print("\n== answering: a reviewer file cannot be cited ==")
    ans, ev, _ = await ask({"status": "answered", "answer": "Venetoclax is preferred.",
                            "support": [{"document": 0, "quote": REVIEWER_TEXT, "relevance": 0.9}]}, DOCS)
    check("quote from the reviewer file is not evidence", ev == [] and ans is None)
    ans, ev, _ = await ask({"status": "answered", "answer": "Venetoclax is preferred.",
                            "support": [{"document": 1, "quote": REVIEWER_TEXT, "relevance": 0.9}]}, DOCS)
    check("the reviewer file has no document index", ev == [] and ans is None)


def retrieval_checks():
    print("\n== retrieval: every answering call gets the files, nothing else does ==")
    context = {"reviewer_notes": ["Use the 2024 SEER release."], "reviewer_documents": DOCS,
               "drugs": ["venetoclax"]}
    calls, drafts, upstream, extras = run_retrieval(context)
    check("answering was called", bool(calls))
    check("every answering call carries the files", all(c["docs"] == DOCS for c in calls),
          str([c["round"] for c in calls if c["docs"] != DOCS]))
    if not firecrawl_blocked():
        check("  including the open-web fallback", any(c["round"] == 99 and c["docs"] == DOCS for c in calls))
    check("connectors never see the files", extras and all("reviewer_documents" not in e for e in extras))
    check("connectors still get notes and upstream terms",
          any("reviewer_notes" in e and "drugs" in e for e in extras))
    check("query drafting gets typed notes only",
          all(d == ["Use the 2024 SEER release."] for d in drafts), str(drafts))
    check("files never become search terms",
          not any("Payer" in str(t) or "filename" in str(t) for t in upstream), str(upstream)[:200])
    check("upstream terms still used", "venetoclax" in upstream)

    calls, *_ = run_retrieval({"reviewer_notes": ["Use the 2024 SEER release."]})
    check("no files, no reviewer block", calls and all(not c["docs"] for c in calls))


def main() -> int:
    asyncio.run(answering_checks())
    retrieval_checks()
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
