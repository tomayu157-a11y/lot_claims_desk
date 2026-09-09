"""Runs the agents.

Execution is driven by the dependency graph in framework.yaml, not by stage
order. Agents with no unmet dependency form a wave and run concurrently; a
reconciliation gate closes each wave before the next begins. Two modes are
supported: every agent end to end, or a single agent on its own.

Everything user-visible calls these units "agents". The bucket letters are an
internal key and never leave this module in rendered form.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import defaultdict
from datetime import date

from ..events import bus
from ..models import (
    AgentState,
    AgentStatus,
    Confidence,
    Contradiction,
    Evidence,
    Insight,
    QuestionStatus,
    ResearchQuestion,
    Run,
    RunMode,
    RunStatus,
    StageReport,
    utcnow,
)
from ..settings import get_framework, get_questions, get_thresholds
from ..store import store
from . import contradictions as contra
from . import handoff
from . import planner, qa, retrieval, synthesis
from .scoring import confidence_for

log = logging.getLogger("celestra.orchestrator")

CATEGORY_BY_BUCKET = {
    "A": "Clinical", "C": "Treatment", "B": "Diagnostic",
    "D": "Logic", "E": "Journey", "F": "Synthesis", "G": "Validation",
}


def agent_key(bucket: str) -> str:
    name = get_framework()["buckets"][bucket]["agent_name"]
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def bucket_for_agent_key(key: str) -> str | None:
    for letter in get_framework()["buckets"]:
        if agent_key(letter) == key:
            return letter
    return None


def build_agent_states(selected: list[str]) -> dict[str, AgentState]:
    fw = get_framework()["buckets"]
    waves = compute_waves(selected)
    wave_of = {b: i + 1 for i, wave in enumerate(waves) for b in wave}
    out: dict[str, AgentState] = {}
    for letter in selected:
        spec = fw[letter]
        out[letter] = AgentState(
            bucket=letter,
            key=agent_key(letter),
            name=spec["agent_name"],
            tagline=spec["agent_tagline"],
            icon=spec.get("agent_icon", "dot"),
            stages=list(spec.get("stages") or []),
            depends_on=[d for d in (spec.get("depends_on") or []) if d in selected],
            wave=wave_of.get(letter, 1),
        )
    return out


def compute_waves(selected: list[str]) -> list[list[str]]:
    """Topological layering. Dependencies outside the selection are ignored,
    which is what makes single-agent mode possible."""
    fw = get_framework()["buckets"]
    remaining = set(selected)
    done: set[str] = set()
    waves: list[list[str]] = []
    max_parallel = get_thresholds()["limits"]["max_parallel_agents"]

    while remaining:
        ready = sorted(
            b for b in remaining
            if all(d in done for d in (fw[b].get("depends_on") or []) if d in selected)
        )
        if not ready:  # cycle guard; should never fire with the shipped config
            ready = sorted(remaining)
        for i in range(0, len(ready), max_parallel):
            waves.append(ready[i : i + max_parallel])
        done |= set(ready)
        remaining -= set(ready)
    return waves


def all_buckets() -> list[str]:
    return [b for b, spec in get_framework()["buckets"].items() if spec.get("mode") != "governance"]


class Orchestrator:
    def __init__(self, run: Run, registry: dict) -> None:
        self.run = run
        self.registry = registry
        self.cfg = run.config
        ind = planner.indication_config(self.cfg.indication_key)
        self.synonyms = list(ind.get("synonyms") or [])
        if self.cfg.indication not in self.synonyms:
            self.synonyms.insert(0, self.cfg.indication)
        self.questions: list[ResearchQuestion] = []
        self.evidence: list[Evidence] = []
        self.insights: list[Insight] = []
        self.contradictions: list[Contradiction] = []
        self.stages: list[StageReport] = []

    # -- event helpers ---------------------------------------------------
    async def _emit(self, type_: str, **data) -> None:
        await bus.publish(self.run.id, type_, **data)

    async def _agent(self, state: AgentState, status: AgentStatus | None = None,
                     *, progress: float | None = None, message: str | None = None) -> None:
        if status is not None:
            state.status = status
        if progress is not None:
            state.progress = round(min(max(progress, 0.0), 1.0), 3)
        if message is not None:
            state.message = message
        self.run.agents[state.bucket] = state
        store.save_run(self.run)
        await self._emit(
            "agent_status",
            agent_key=state.key, agent_name=state.name, status=state.status.value,
            progress=state.progress, message=state.message,
            questions_total=state.questions_total,
            questions_answered=state.questions_answered,
            evidence_count=state.evidence_count,
        )

    # -- main ------------------------------------------------------------
    async def execute(self) -> None:
        selected = (
            all_buckets()
            if self.cfg.mode is RunMode.FULL
            else [self.cfg.selected_agent or "A"]
        )
        self.run.agents = build_agent_states(selected)
        self.run.status = RunStatus.RUNNING
        self.run.started_at = utcnow()
        store.save_run(self.run)

        waves = compute_waves(selected)
        await self._emit(
            "run_started",
            mode=self.cfg.mode.value,
            indication=self.cfg.indication,
            agents=[
                {"key": a.key, "name": a.name, "tagline": a.tagline,
                 "icon": a.icon, "wave": a.wave}
                for a in self.run.agents.values()
            ],
            waves=[[agent_key(b) for b in w] for w in waves],
        )

        try:
            for index, wave in enumerate(waves, start=1):
                await self._emit("wave_started", wave=index,
                                 agents=[agent_key(b) for b in wave])
                await asyncio.gather(*(self._run_agent(b) for b in wave))
                await self._emit(
                    "wave_complete", wave=index,
                    gate=get_framework()["buckets"][wave[0]].get("gate", ""),
                )
            await self._finalise()
        except asyncio.CancelledError:
            self.run.status = RunStatus.CANCELLED
            store.save_run(self.run)
            await self._emit("run_failed", error="cancelled")
            raise
        except Exception as exc:
            log.exception("run %s failed", self.run.id)
            self.run.status = RunStatus.FAILED
            self.run.error = f"{type(exc).__name__}: {exc}"
            self.run.finished_at = utcnow()
            store.save_run(self.run)
            await self._emit("run_failed", error=self.run.error)
        finally:
            await bus.close(self.run.id)

    async def _run_agent(self, bucket: str) -> None:
        state = self.run.agents[bucket]
        state.started_at = utcnow()
        await self._agent(state, AgentStatus.RESEARCHING, progress=0.02,
                          message="Planning research questions")

        try:
            stages = state.stages or []
            agent_questions: list[ResearchQuestion] = []
            for stage in stages:
                agent_questions += await planner.plan_stage(self.run.id, self.cfg, stage)

            state.questions_total = len(agent_questions)
            await self._agent(state, progress=0.06,
                              message=f"{len(agent_questions)} questions planned")
            if agent_questions:
                store.save_questions(self.run.id, agent_questions)

            inbound_context = handoff.for_agent(bucket, self.run.context)
            if inbound_context:
                await self._agent(state, message=(
                    "Using upstream context: "
                    + ", ".join(f"{len(v)} {k.replace('_', ' ')}"
                                for k, v in inbound_context.items())
                ))

            agent_evidence: list[Evidence] = []
            agent_contra: list[Contradiction] = []

            for i, question in enumerate(agent_questions, start=1):
                question.status = QuestionStatus.RETRIEVING
                await self._emit("question_status", agent_key=state.key,
                                 question_id=question.id, text=question.text,
                                 status=question.status.value)

                async def on_source(sid, sname, ok, count, reason, _s=state):
                    if ok and count and sid not in _s.sources_used:
                        _s.sources_used.append(sid)
                    await self._emit("source_used", agent_key=_s.key, source_id=sid,
                                     source_name=sname, ok=ok, count=count, reason=reason)

                outcome = await retrieval.retrieve(
                    question, self.cfg, self.synonyms, self.registry, on_source,
                    context=inbound_context,
                )
                retrieval.apply_outcome(question, outcome)
                agent_evidence += outcome.evidence

                found = await contra.detect(question, outcome.evidence)
                agent_contra += found
                for c in found:
                    await self._emit("contradiction_added", agent_key=state.key,
                                     topic=c.topic, severity=c.severity.value)

                if question.status is QuestionStatus.SUFFICIENT:
                    state.questions_answered += 1
                state.evidence_count = len(agent_evidence)

                insight = self._insight_for(question, outcome.evidence, found, bucket)
                if insight:
                    self.insights.append(insight)
                    await self._emit(
                        "insight_added", agent_key=state.key, insight_id=insight.id,
                        title=insight.title, confidence=insight.confidence.value,
                        category=insight.category, sources=insight.source_ids,
                    )

                await self._agent(
                    state, progress=0.06 + 0.74 * (i / max(len(agent_questions), 1)),
                    message=(
                        f"{question.seed_text[:70] or question.text[:70]} — "
                        f"{'answered' if question.status is QuestionStatus.SUFFICIENT else question.status.value}"
                    ),
                )

            store.save_questions(self.run.id, agent_questions)
            store.save_evidence(self.run.id, agent_evidence)
            agent_contra = contra.dedupe(agent_contra)
            store.save_contradictions(self.run.id, agent_contra)
            store.save_insights(self.run.id, [i for i in self.insights if i.bucket == bucket])

            self.questions += agent_questions
            self.evidence += agent_evidence
            self.contradictions += agent_contra

            await self._agent(state, AgentStatus.SYNTHESISING, progress=0.84,
                              message="Synthesising findings")
            for stage in stages:
                sq = [q for q in agent_questions if q.stage == stage]
                se = [e for e in agent_evidence if e.question_id in {q.id for q in sq}]
                sc = [c for c in agent_contra if c.stage == stage]
                report = await synthesis.build_stage_report(
                    self.run.id, self.cfg, stage, bucket, sq, se, sc
                )
                self.stages.append(report)
                store.save_stage_reports(self.run.id, [report])
                await self._emit("stage_complete", agent_key=state.key, stage=stage,
                                 name=report.name, evidence_count=report.evidence_count,
                                 source_count=report.source_count,
                                 tables=len(report.tables))

            produced = await handoff.build(bucket, self.cfg, agent_evidence)
            if produced:
                self.run.context = handoff.merge(self.run.context, produced)
                store.save_run(self.run)
                await self._emit("context_published", agent_key=state.key,
                                 entities={k: len(v) if isinstance(v, list) else 1
                                           for k, v in produced.items()})

            state.finished_at = utcnow()
            await self._agent(
                state, AgentStatus.COMPLETE, progress=1.0,
                message=f"{state.questions_answered}/{state.questions_total} questions "
                        f"answered from {len(state.sources_used)} sources",
            )
        except Exception as exc:
            log.exception("agent %s failed", bucket)
            state.error = f"{type(exc).__name__}: {exc}"
            state.finished_at = utcnow()
            await self._agent(state, AgentStatus.FAILED, message=state.error[:160])

    # -- insights --------------------------------------------------------
    def _insight_title(self, question: ResearchQuestion) -> str:
        meta = get_questions()["stage_meta"][question.stage]
        seeds = planner.seeds_for(self.cfg.indication_key, question.stage)
        steps = meta.get("framework_steps") or []
        if question.seed_text in seeds:
            idx = seeds.index(question.seed_text)
            if idx < len(steps):
                return str(steps[idx])
        text = re.sub(r"\s*\([^)]*\)", "", question.seed_text or question.text).strip(" ?")
        text = re.sub(r"^(what|which|how|where)\s+(are|is|do|does)?\s*", "", text, flags=re.I)
        return text[:80].strip().capitalize() or "Finding"

    def _insight_for(
        self, question: ResearchQuestion, evidence: list[Evidence],
        found: list[Contradiction], bucket: str,
    ) -> Insight | None:
        conf = confidence_for(question, evidence, found)
        best = sorted(evidence, key=lambda e: (e.tier, -e.relevance))
        summary = (
            re.sub(r"\s+", " ", best[0].quote)[:260]
            if best
            else (question.unmet_reason or "No usable evidence was retrieved.")
        )
        source_ids: list[str] = []
        for e in best:
            if e.source_id not in source_ids:
                source_ids.append(e.source_id)
        return Insight(
            run_id=self.run.id, stage=question.stage, bucket=bucket,
            category=CATEGORY_BY_BUCKET.get(bucket, "Clinical"),
            title=self._insight_title(question),
            summary=summary,
            detail=" ".join(re.sub(r"\s+", " ", e.quote) for e in best[1:4]),
            confidence=conf,
            evidence_ids=[e.id for e in evidence],
            source_ids=source_ids,
            question_ids=[question.id],
            used_web_fallback=question.used_web_fallback,
        )

    # -- finalisation ----------------------------------------------------
    async def _finalise(self) -> None:
        metrics = qa.build_metrics(
            self.cfg, self.questions, self.evidence, self.contradictions, self.stages
        )
        metrics = await qa.polish_readiness(metrics, self.cfg)
        store.save_qa(self.run.id, metrics)

        self.run.status = RunStatus.COMPLETED
        self.run.finished_at = utcnow()
        store.save_run(self.run)

        counts = defaultdict(int)
        for i in self.insights:
            counts[i.confidence.value] += 1
        await self._emit(
            "run_complete",
            redirect=f"/runs/{self.run.id}/overview",
            insights=len(self.insights),
            sources=metrics.distinct_sources,
            high=counts[Confidence.HIGH.value],
            medium=counts[Confidence.MEDIUM.value],
            requires_input=counts[Confidence.REQUIRES_INPUT.value],
            rejected=counts[Confidence.REJECTED.value],
            questions_answered=metrics.questions_sufficient,
            questions_planned=metrics.questions_planned,
            duration=round(self.run.duration_seconds, 1),
        )


def new_reference() -> str:
    return f"RUN-{uuid.uuid4().hex[:8].upper()}"


def default_cutoff() -> str:
    return date.today().isoformat()
