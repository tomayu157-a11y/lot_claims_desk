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
    Answer,
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
from . import insights as insight_gen
from . import planner, qa, retrieval, synthesis
from .scoring import assess_confidence

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


def phase_of(bucket: str) -> str:
    return str(get_framework()["buckets"][bucket].get("phase") or "discovery")


def phase_spec(key: str) -> dict:
    spec = dict((get_framework().get("phases") or {}).get(key) or {})
    spec.setdefault("name", key.replace("_", " ").title())
    spec.setdefault("description", "")
    spec["key"] = key
    return spec


def phases_for(selected: list[str]) -> list[dict]:
    """The phases this run passes through, each with its agents, in order."""
    order = list((get_framework().get("phases") or {}).keys()) or ["discovery", "mapping"]
    out = []
    for key in order:
        agents = [b for b in selected if phase_of(b) == key]
        if agents:
            out.append({**phase_spec(key), "agents": [agent_key(b) for b in agents]})
    return out


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
        self.answers: list[Answer] = []
        self.insights: list[Insight] = []
        self.contradictions: list[Contradiction] = []
        self.stages: list[StageReport] = []
        # Quote fingerprints already used as an insight headline.
        self._used_summaries: set[str] = set()

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
                 "icon": a.icon, "wave": a.wave, "phase": phase_of(a.bucket)}
                for a in self.run.agents.values()
            ],
            waves=[[agent_key(b) for b in w] for w in waves],
            phases=phases_for(selected),
        )
        await self._emit_phase(self.run.phase)

        try:
            paused = await self._run_waves(waves, start_index=1)
            if not paused:
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

    async def _run_waves(self, waves: list[list[str]], start_index: int) -> bool:
        """Run waves from `start_index` (1-based). Returns True when the run
        paused at the human review gate rather than finishing."""
        gate = self.run.review_after_wave if self.cfg.mode is RunMode.FULL else 0
        for index, wave in enumerate(waves, start=1):
            if index < start_index:
                continue
            await self._emit("wave_started", wave=index,
                             agents=[agent_key(b) for b in wave])
            await asyncio.gather(*(self._run_agent(b) for b in wave))
            await self._emit(
                "wave_complete", wave=index,
                gate=get_framework()["buckets"][wave[0]].get("gate", ""),
            )
            await self._phase_cards_if_complete()
            if gate and index == gate and index < len(waves):
                await self._pause_for_review(index, waves)
                return True
        return False

    async def _web_notice_if_needed(self, state: AgentState) -> None:
        """Say once, on the live page and in the run, when web search has been
        switched off for the session, so unanswered questions are explained
        where the person is looking."""
        from ..connectors.firecrawl import firecrawl_blocked

        reason = firecrawl_blocked()
        if not reason or self.run.context.get("web_search_notice") == reason:
            return
        self.run.context["web_search_notice"] = reason
        store.save_run(self.run)
        await self._emit("notice", level="warning", agent_key=state.key,
                         text=f"Web search switched off: {reason}")

    async def _phase_cards_if_complete(self) -> None:
        """A phase whose agents have all finished owes its phase-level cards,
        written from every stage document in it. Once per phase."""
        if self.cfg.mode is not RunMode.FULL:
            # A phase is only complete when every agent in it ran.
            return
        done_phases = set(self.run.context.get("phase_cards_done") or [])
        for spec in phases_for([a.bucket for a in self.run.agents.values()]):
            key = spec["key"]
            if key in done_phases or not insight_gen.phase_catalogue(key):
                continue
            buckets = [b for b in self.run.agents if phase_of(b) == key]
            if not all(self.run.agents[b].status is AgentStatus.COMPLETE for b in buckets):
                continue
            stages = {st for b in buckets for st in (self.run.agents[b].stages or [])}
            reports = [r for r in self.stages if r.stage in stages]
            cards = await insight_gen.phase_cards(
                self.run.id, self.cfg, key, reports, [i for i in self.insights if i.stage in stages]
            )
            for card in cards:
                self.insights.append(card)
                await self._emit_insight(agent_key(card.bucket), card)
            if cards:
                store.save_insights(self.run.id, cards)
            done_phases.add(key)
            self.run.context["phase_cards_done"] = sorted(done_phases)
            store.save_run(self.run)

    async def _emit_phase(self, key: str) -> None:
        spec = phase_spec(key)
        await self._emit("phase_started", phase=key, name=spec["name"],
                         description=spec["description"])

    async def _pause_for_review(self, wave_index: int, waves: list[list[str]]) -> None:
        """Stop after a wave and hand the findings so far to a reviewer.

        Downstream agents build on these findings, so a wrong one propagates.
        Reviewing here is cheaper than reviewing everything at the end."""
        self.run.status = RunStatus.AWAITING_REVIEW
        self.run.resume_from_wave = wave_index + 1
        store.save_run(self.run)
        needs_input = [i for i in self.insights if i.needs_decision]
        await self._emit(
            "review_required",
            redirect=f"/runs/{self.run.id}/review",
            wave=wave_index,
            insights=len(self.insights),
            requires_input=len(needs_input),
            conflicts=len(self.contradictions),
            remaining_agents=[agent_key(b) for w in waves[wave_index:] for b in w],
        )

    async def resume(self) -> None:
        """Continue a run paused at the review gate. State is reloaded from the
        store, because the process that paused it may not be the one resuming.

        The web handler that triggers this marks the run RUNNING before the
        task starts, so the live page it redirects to is never bounced back to
        the review page by a stale status. Both statuses are therefore valid
        here; what matters is that a resume point was recorded.
        """
        if self.run.resume_from_wave < 2 or self.run.status not in (
            RunStatus.AWAITING_REVIEW, RunStatus.RUNNING
        ):
            return
        # The channel was closed when phase one ended. Bring it back before
        # anything is published, or the resumed run streams into the void.
        await bus.reopen(self.run.id)
        self.questions = store.get_questions(self.run.id)
        self.evidence = store.get_evidence(self.run.id)
        self.insights = store.get_insights(self.run.id)
        self.contradictions = store.get_contradictions(self.run.id)
        self.answers = store.get_answers(self.run.id)
        self.stages = store.get_stage_reports(self.run.id)
        self._used_summaries = {
            re.sub(r"\s+", " ", i.summary).strip()[:120].lower() for i in self.insights
        }
        self.run.status = RunStatus.RUNNING
        self.run.reviewed_at = self.run.reviewed_at or utcnow()
        store.save_run(self.run)

        selected = [a.bucket for a in self.run.agents.values()]
        waves = compute_waves(selected)
        await self._emit("run_resumed", from_wave=self.run.resume_from_wave,
                         waves=[[agent_key(b) for b in w] for w in waves],
                         phases=phases_for(selected))
        await self._emit_phase("mapping")
        try:
            paused = await self._run_waves(waves, start_index=self.run.resume_from_wave)
            if not paused:
                await self._finalise()
        except asyncio.CancelledError:
            self.run.status = RunStatus.CANCELLED
            store.save_run(self.run)
            await self._emit("run_failed", error="cancelled")
            raise
        except Exception as exc:
            log.exception("run %s failed on resume", self.run.id)
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
            agent_answers: list[Answer] = []
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

                hard_limit = float(get_thresholds()["limits"].get(
                    "question_hard_timeout_seconds", 480))
                try:
                    outcome = await asyncio.wait_for(
                        retrieval.retrieve(
                            question, self.cfg, self.synonyms, self.registry, on_source,
                            context=inbound_context,
                        ),
                        timeout=hard_limit,
                    )
                except asyncio.TimeoutError:
                    # The agent must never sit on one question. Report it as
                    # unanswered with the reason and move on.
                    log.warning("q=%s abandoned after %ss", question.id, int(hard_limit))
                    outcome = retrieval.RetrievalOutcome(
                        skipped=f"abandoned after {int(hard_limit)}s; sources did not respond",
                    )
                    outcome.sufficiency = retrieval.assess(question, [])
                retrieval.apply_outcome(question, outcome)
                await self._web_notice_if_needed(state)
                agent_evidence += outcome.evidence
                for a in outcome.answers:
                    a.run_id, a.stage = self.run.id, question.stage
                agent_answers += outcome.answers

                found = await contra.detect(question, outcome.evidence)
                agent_contra += found
                for c in found:
                    await self._emit("contradiction_added", agent_key=state.key,
                                     topic=c.topic, severity=c.severity.value)

                if question.status is QuestionStatus.SUFFICIENT:
                    state.questions_answered += 1
                state.evidence_count = len(agent_evidence)

                # Persist per question, not per agent. A long agent that fails
                # on question four should not discard the first three answers.
                store.save_questions(self.run.id, [question])
                store.save_evidence(self.run.id, outcome.evidence)
                if outcome.answers:
                    store.save_answers(self.run.id, outcome.answers)
                # Conflicts are deliberately not persisted per question: the same
                # claim pair surfaces on every question both sources answered, and
                # deduplication runs once the agent has seen them all. Saving here
                # made the duplicates permanent regardless of that pass.


                await self._agent(
                    state, progress=0.06 + 0.74 * (i / max(len(agent_questions), 1)),
                    message=(
                        f"{question.seed_text[:70] or question.text[:70]} — "
                        f"{'answered' if question.status is QuestionStatus.SUFFICIENT else question.status.value}"
                    ),
                )

            store.save_questions(self.run.id, agent_questions)
            store.save_evidence(self.run.id, agent_evidence)
            store.save_answers(self.run.id, agent_answers)
            agent_contra = contra.dedupe(agent_contra)
            store.save_contradictions(self.run.id, agent_contra)
            self.questions += agent_questions
            self.evidence += agent_evidence
            self.answers += agent_answers
            self.contradictions += agent_contra

            await self._agent(state, AgentStatus.SYNTHESISING, progress=0.84,
                              message="Synthesising findings")
            for stage in stages:
                sq = [q for q in agent_questions if q.stage == stage]
                se = [e for e in agent_evidence if e.question_id in {q.id for q in sq}]
                sc = [c for c in agent_contra if c.stage == stage]
                sa = [a for a in agent_answers if a.question_id in {q.id for q in sq}]
                report = await synthesis.build_stage_report(
                    self.run.id, self.cfg, stage, bucket, sq, se, sc, sa
                )
                self.stages.append(report)
                store.save_stage_reports(self.run.id, [report])
                await self._emit("stage_complete", agent_key=state.key, stage=stage,
                                 name=report.name, evidence_count=report.evidence_count,
                                 source_count=report.source_count,
                                 tables=len(report.tables))

                # Cards are the fixed slots this agent owes, filled from the
                # stage document it just wrote (by the model when one is
                # configured, from the mapped questions otherwise).
                await self._agent(state, message=f"Filling review cards for {report.name}")
                cards = await insight_gen.generate(
                    self.run.id, self.cfg, stage, bucket, report, sq, se, sc,
                    CATEGORY_BY_BUCKET.get(bucket, "Clinical"),
                )
                for card in cards:
                    if not card.table_titles:
                        titles = [
                            t.title for t in report.tables
                            if set(t.question_ids) & set(card.question_ids)
                        ]
                        card.table_titles = titles or [t.title for t in report.tables][:1]
                    self.insights.append(card)
                    await self._emit_insight(state.key, card)
                if cards:
                    store.save_insights(self.run.id, cards)

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
    async def _emit_insight(self, agent_key_: str, insight: Insight) -> None:
        await self._emit(
            "insight_added", agent_key=agent_key_, insight_id=insight.id,
            title=insight.title, confidence=insight.confidence.value,
            category=insight.category, sources=insight.source_ids,
        )

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
        conf, reason = assess_confidence(question, evidence, found)
        best = sorted(evidence, key=lambda e: (e.tier, -e.relevance))

        # Two questions in a stage often retrieve the same document, and its
        # strongest quote would then headline both cards. Take the best quote
        # this run has not already used as a headline, so every card says
        # something different. The full evidence set is unchanged.
        # The established answer is what the question actually asked for. A raw
        # quote is the fallback, and only when no answer was reached.
        summary = ""
        if question.answer_text:
            answer = re.sub(r"\s+", " ", question.answer_text).strip()
            fingerprint = answer[:120].lower()
            if fingerprint not in self._used_summaries:
                self._used_summaries.add(fingerprint)
                summary = answer[:400]
        for candidate in ([] if summary else best):
            text = re.sub(r"\s+", " ", candidate.quote).strip()
            fingerprint = text[:120].lower()
            if fingerprint not in self._used_summaries:
                self._used_summaries.add(fingerprint)
                summary = text[:260]
                break
        if not summary:
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
            detail=" ".join(self._fresh_detail(best[1:6])),
            confidence=conf,
            input_reason=reason,
            evidence_ids=[e.id for e in evidence],
            source_ids=source_ids,
            question_ids=[question.id],
            used_web_fallback=question.used_web_fallback,
        )

    def _fresh_detail(self, candidates: list[Evidence], limit: int = 3) -> list[str]:
        """Supporting quotes not already shown on another card.

        Without this the same passages repeated under every finding in a
        stage, which reads as padding and hides how much distinct evidence
        there actually is."""
        out: list[str] = []
        for ev in candidates:
            text = re.sub(r"\s+", " ", ev.quote).strip()
            fingerprint = text[:120].lower()
            if fingerprint in self._used_summaries:
                continue
            self._used_summaries.add(fingerprint)
            out.append(text)
            if len(out) >= limit:
                break
        return out

    # -- finalisation ----------------------------------------------------
    async def _finalise(self) -> None:
        metrics = qa.build_metrics(
            self.cfg, self.questions, self.evidence, self.contradictions, self.stages
        )
        metrics = qa.build_narrative(self.cfg, metrics, self.stages, self.questions)
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
            redirect=f"/runs/{self.run.id}/approval",
            insights=len(self.insights),
            sources=metrics.distinct_sources,
            ready=counts[Confidence.READY.value],
            requires_input=counts[Confidence.REQUIRES_INPUT.value],
            needs_decision=sum(1 for i in self.insights if i.needs_decision),
            questions_answered=metrics.questions_sufficient,
            questions_planned=metrics.questions_planned,
            duration=round(self.run.duration_seconds, 1),
        )


def new_reference() -> str:
    return f"RUN-{uuid.uuid4().hex[:8].upper()}"


def default_cutoff() -> str:
    return date.today().isoformat()
