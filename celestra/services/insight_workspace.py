"""Persistent, insight-scoped research chat lifecycle."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

from fastapi import HTTPException

from ..models import (
    AppliedInsightRevision,
    AppliedRevisionResult,
    Evidence,
    InsightWorkspace,
    InsightWorkspaceMessage,
    InsightWorkspaceSource,
    ReviewAction,
    WorkspaceEvent,
    WorkspaceEventType,
    WorkspaceMessageRole,
    WorkspaceMessageState,
    is_http_url,
    utcnow,
    workspace_id,
)
from ..store import InsightCardRevisionCommit, StaleInsightRevision
from .insight_reconciliation import (
    MUTABLE_CARD_FIELDS,
    InsightCardReconciler,
    insight_card_content,
    insight_card_digest,
    is_complete_card_proposal,
)
from .insight_research import (
    ProposalUnsupported,
    ResearchContext,
    answer_turn,
    build_proposal,
    summarize_context,
)
from .llm import LLMUnavailable, llm

_application_lock_registry: dict[tuple[str, str], asyncio.Lock] = {}


class InsightWorkspaceService:
    def __init__(
        self,
        store,
        registry_factory: Callable[[], dict],
        answerer=answer_turn,
        summarizer=summarize_context,
        proposal_builder=build_proposal,
        llm_client=llm,
        lock_registry=None,
        active_token_limit: int = 16_000,
        reduced_token_target: int = 10_000,
    ) -> None:
        self.store = store
        self.registry_factory = registry_factory
        self.answerer = answerer
        self.summarizer = summarizer
        self.proposal_builder = proposal_builder
        self.llm_client = llm_client
        self.lock_registry = (
            lock_registry if lock_registry is not None else _application_lock_registry
        )
        self.active_token_limit = active_token_limit
        self.reduced_token_target = reduced_token_target

    def load(self, run_id: str, insight_id: str) -> InsightWorkspace:
        run = self.store.get_run(run_id)
        insight = self.store.get_insight(run_id, insight_id)
        if run is None or insight is None:
            raise HTTPException(404, "Insight not found")

        workspace = self.store.get_insight_workspace(run_id, insight_id)
        if workspace is None:
            workspace = InsightWorkspace(
                id=workspace_id(run_id, insight_id),
                run_id=run_id,
                insight_id=insight_id,
            )
            self.store.save_insight_workspace(workspace)
        return workspace

    def _lock_for(self, run_id: str, insight_id: str) -> asyncio.Lock:
        key = (run_id, insight_id)
        lock = self.lock_registry.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.lock_registry[key] = lock
        return lock

    def _research_context(
        self,
        run_id: str,
        insight_id: str,
        workspace: InsightWorkspace,
    ) -> ResearchContext:
        run = self.store.get_run(run_id)
        insight = self.store.get_insight(run_id, insight_id)
        if run is None or insight is None:
            raise HTTPException(404, "Insight not found")

        question_ids = set(insight.question_ids)
        questions_by_id = {
            question.id: question
            for question in self.store.get_questions(run_id)
            if question.id in question_ids
        }
        questions = [
            questions_by_id[question_id]
            for question_id in insight.question_ids
            if question_id in questions_by_id
        ]
        if not questions:
            raise HTTPException(409, "This insight has no linked research question.")

        linked_evidence_ids = set(insight.evidence_ids)
        evidence_by_id: dict[str, Evidence] = {}
        for question in questions:
            for item in self.store.get_evidence_for(run_id, question.id):
                if not linked_evidence_ids or item.id in linked_evidence_ids:
                    evidence_by_id[item.id] = item
        for source in workspace.sources:
            if source.question_id in question_ids:
                evidence_by_id.setdefault(
                    source.id,
                    Evidence(
                        id=source.id,
                        question_id=source.question_id,
                        source_id=source.source_id,
                        source_name=source.source_name,
                        organization=source.organization,
                        tier=source.tier,
                        url=source.url,
                        title=source.title,
                        published=source.published,
                        quote=source.quote,
                        context=source.context,
                        origin=source.origin,
                        tag=source.tag,
                        relevance=source.relevance,
                        identifiers=source.identifiers,
                        retrieved_at=source.retrieved_at,
                    ),
                )
        return ResearchContext(
            insight=insight,
            questions=questions,
            evidence=list(evidence_by_id.values()),
            config=run.config,
            continuity_summary=workspace.continuity_summary,
            messages=self._active_messages(workspace),
        )

    @staticmethod
    def _active_messages(workspace: InsightWorkspace) -> list[InsightWorkspaceMessage]:
        boundary = workspace.summarized_through_message_id
        if not boundary:
            return list(workspace.messages)
        for index, message in enumerate(workspace.messages):
            if message.id == boundary:
                return list(workspace.messages[index + 1 :])
        return list(workspace.messages)

    @staticmethod
    def _token_estimate(text: str) -> int:
        return max(1, len(text) // 4)

    def _active_token_estimate(self, workspace: InsightWorkspace) -> int:
        content = workspace.continuity_summary + "\n".join(
            message.content for message in self._active_messages(workspace)
        )
        return self._token_estimate(content)

    async def _summarize_if_needed(self, workspace: InsightWorkspace) -> None:
        if self._active_token_estimate(workspace) <= self.active_token_limit:
            return

        active = self._active_messages(workspace)
        retained = active[-6:]
        retained_prompt = "\n".join(message.content for message in retained)
        if self._token_estimate(retained_prompt) > self.reduced_token_target:
            raise RuntimeError("Conversation context is too large to summarize safely.")

        retain_from = max(0, len(active) - 6)
        selected: list[InsightWorkspaceMessage] = []
        remaining = list(active)
        for message in active[:retain_from]:
            if message.state is not WorkspaceMessageState.COMPLETED:
                continue
            selected.append(message)
            remaining.pop(0)
            prompt = workspace.continuity_summary + "\n".join(
                item.content for item in remaining
            )
            if self._token_estimate(prompt) <= self.reduced_token_target:
                break

        if not selected:
            raise RuntimeError("Conversation context is too large to summarize safely.")

        summary = await self.summarizer(
            workspace.continuity_summary,
            selected,
            self.llm_client,
        )
        workspace.continuity_summary = summary
        workspace.summarized_through_message_id = selected[-1].id
        workspace.updated_at = utcnow()
        self.store.save_insight_workspace(workspace)
        if self._active_token_estimate(workspace) > self.reduced_token_target:
            raise RuntimeError("Conversation context remains too large to answer safely.")

    @staticmethod
    def _source_key(item: Evidence) -> tuple[str, str, str]:
        return item.source_id, item.url, item.quote

    def _merge_sources(
        self,
        workspace: InsightWorkspace,
        evidence: list[Evidence],
        source_audit: dict[str, dict] | None = None,
    ) -> dict[str, str]:
        source_audit = source_audit or {}
        canonical_sources = {source.id: source for source in workspace.sources}
        canonical_by_key = {
            (source.source_id, source.url, source.quote): source.id
            for source in workspace.sources
        }
        source_id_map: dict[str, str] = {}
        for item in evidence:
            if not is_http_url(item.url):
                continue
            canonical_id = item.id if item.id in canonical_sources else canonical_by_key.get(
                self._source_key(item)
            )
            if canonical_id:
                source = canonical_sources[canonical_id]
            else:
                source = InsightWorkspaceSource(
                    id=item.id,
                    question_id=item.question_id,
                    source_id=item.source_id,
                    source_name=item.source_name,
                    organization=item.organization,
                    tier=item.tier,
                    url=item.url,
                    title=item.title,
                    published=item.published,
                    quote=item.quote,
                    context=item.context,
                    origin=item.origin,
                    tag=item.tag,
                    relevance=item.relevance,
                    identifiers=item.identifiers,
                    retrieved_at=item.retrieved_at,
                )
                workspace.sources.append(source)
                canonical_sources[item.id] = source
                canonical_by_key[self._source_key(item)] = item.id
                canonical_id = item.id
            self._merge_source_audit(source, source_audit.get(item.url))
            source_id_map[item.id] = canonical_id
        return source_id_map

    @staticmethod
    def _merge_source_audit(source: InsightWorkspaceSource, audit: dict | None) -> None:
        if not audit:
            return
        if provider := str(audit.get("search_provider") or ""):
            source.search_provider = provider
        if "search_queries" in audit:
            source.search_queries = list(audit["search_queries"] or [])
        if hydration_status := str(audit.get("hydration_status") or ""):
            source.hydration_status = hydration_status
        if "citation_metadata" in audit:
            source.citation_metadata = list(audit["citation_metadata"] or [])
        source.supported_answer = source.supported_answer or bool(audit.get("supported_answer"))

    async def _persist_failed(
        self,
        workspace: InsightWorkspace,
        assistant: InsightWorkspaceMessage,
    ) -> None:
        assistant.state = WorkspaceMessageState.FAILED
        assistant.error = "Research could not be completed. Try again."
        assistant.updated_at = utcnow()
        workspace.updated_at = utcnow()
        self.store.save_insight_workspace(workspace)

    async def send(
        self,
        run_id: str,
        insight_id: str,
        user_text: str,
    ) -> AsyncIterator[WorkspaceEvent]:
        text = user_text.strip()[:4000]
        if not text:
            raise HTTPException(400, "Write a message first.")

        async with self._lock_for(run_id, insight_id):
            workspace = self.load(run_id, insight_id)
            user = InsightWorkspaceMessage(
                role=WorkspaceMessageRole.USER,
                state=WorkspaceMessageState.COMPLETED,
                content=text,
            )
            assistant = InsightWorkspaceMessage(
                role=WorkspaceMessageRole.ASSISTANT,
                state=WorkspaceMessageState.PENDING,
            )
            workspace.messages.extend([user, assistant])
            workspace.updated_at = utcnow()
            self.store.save_insight_workspace(workspace)
            yield WorkspaceEvent(
                type=WorkspaceEventType.MESSAGE_SAVED,
                message_id=user.id,
            )

            event_queue: asyncio.Queue[WorkspaceEvent | None] = asyncio.Queue()
            producer: asyncio.Task | None = None

            async def status(name: str) -> None:
                await event_queue.put(
                    WorkspaceEvent(
                        type=WorkspaceEventType.RESEARCH_STATUS,
                        message_id=assistant.id,
                        detail=name,
                    )
                )

            try:
                await self._summarize_if_needed(workspace)

                async def produce_answer():
                    try:
                        return await self.answerer(
                            self._research_context(run_id, insight_id, workspace),
                            text,
                            self.registry_factory(),
                            status,
                            self.llm_client,
                        )
                    finally:
                        await event_queue.put(None)

                producer = asyncio.create_task(produce_answer())
                while True:
                    event = await event_queue.get()
                    if event is None:
                        break
                    yield event
                result = await producer
                if result.retryable:
                    await self._persist_failed(workspace, assistant)
                    yield WorkspaceEvent(
                        type=WorkspaceEventType.ERROR,
                        message_id=assistant.id,
                        detail=assistant.error,
                    )
                    return
                source_id_map = self._merge_sources(
                    workspace,
                    result.evidence,
                    result.source_audit,
                )
                assistant.content = result.text
                assistant.state = WorkspaceMessageState.COMPLETED
                assistant.source_ids = [
                    source_id_map[source_id]
                    for source_id in result.source_evidence_ids
                    if source_id in source_id_map
                ]
                assistant.used_web_fallback = result.searched
                assistant.web_sites = result.sites
                assistant.updated_at = utcnow()
                workspace.updated_at = utcnow()
                self.store.save_insight_workspace(workspace)
                yield WorkspaceEvent(
                    type=WorkspaceEventType.ANSWER_DELTA,
                    message_id=assistant.id,
                    text=assistant.content,
                )
                yield WorkspaceEvent(
                    type=WorkspaceEventType.ANSWER_COMPLETED,
                    message_id=assistant.id,
                    source_ids=assistant.source_ids,
                    sources=[
                        source
                        for source in workspace.sources
                        if source.id in assistant.source_ids
                    ],
                )
            except asyncio.CancelledError:
                if producer is not None and not producer.done():
                    producer.cancel()
                    try:
                        await producer
                    except asyncio.CancelledError:
                        pass
                await self._persist_failed(workspace, assistant)
                raise
            except Exception:  # noqa: BLE001 - provider failures are normalized for retry
                await self._persist_failed(workspace, assistant)
                yield WorkspaceEvent(
                    type=WorkspaceEventType.ERROR,
                    message_id=assistant.id,
                    detail=assistant.error,
                )
            finally:
                if producer is not None and not producer.done():
                    producer.cancel()
                    try:
                        await producer
                    except asyncio.CancelledError:
                        pass
                if assistant.state is WorkspaceMessageState.PENDING:
                    await self._persist_failed(workspace, assistant)

    async def propose(self, run_id: str, insight_id: str):
        async with self._lock_for(run_id, insight_id):
            run = self.store.get_run(run_id)
            if run is None or self.store.get_insight(run_id, insight_id) is None:
                raise HTTPException(404, "Insight not found")
            if run.is_locked:
                raise HTTPException(409, "This document is approved and locked. Start a new project to change it.")

            workspace = self.load(run_id, insight_id)
            try:
                draft = await self.proposal_builder(
                    self._research_context(run_id, insight_id, workspace),
                    self.registry_factory(),
                    self.llm_client,
                )
            except LLMUnavailable as exc:
                raise HTTPException(503, "The model is unavailable. Try again.") from exc
            except ProposalUnsupported as exc:
                raise HTTPException(422, str(exc)) from exc
            source_id_map = self._merge_sources(
                workspace,
                draft.evidence,
                draft.source_audit,
            )
            after_content = draft.proposal.after_content
            if after_content is not None:
                after_content = after_content.model_copy(
                    update={
                        "evidence_ids": [
                            source_id_map[source_id]
                            for source_id in after_content.evidence_ids
                            if source_id in source_id_map
                        ]
                    }
                )
            proposal = draft.proposal.model_copy(
                update={
                    "source_ids": [
                        source_id_map[source_id]
                        for source_id in draft.proposal.source_ids
                        if source_id in source_id_map
                    ],
                    "web_sites": self._deduplicated_sites(draft.proposal.web_sites + draft.sites),
                    "after_content": after_content,
                }
            )
            workspace.pending_proposal = proposal
            workspace.updated_at = utcnow()
            self.store.save_insight_workspace(workspace)
            return proposal

    @staticmethod
    def _deduplicated_sites(sites: list[dict]) -> list[dict]:
        unique: list[dict] = []
        seen: set[str] = set()
        for site in sites:
            key = json.dumps(site, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                unique.append(site)
        return unique

    @staticmethod
    def _stale_proposal_error() -> HTTPException:
        return HTTPException(
            409,
            "The finding changed after this proposal was created. Regenerate it.",
        )

    async def apply(
        self,
        run_id: str,
        insight_id: str,
        proposal_id: str,
    ) -> AppliedRevisionResult:
        async with self._lock_for(run_id, insight_id):
            run = self.store.get_run(run_id)
            insight = self.store.get_insight(run_id, insight_id)
            if run is None or insight is None:
                raise HTTPException(404, "Insight not found")
            workspace = self.load(run_id, insight_id)
            proposal = workspace.pending_proposal
            if proposal is None or proposal.id != proposal_id:
                raise HTTPException(409, "This proposal is no longer available. Regenerate it.")
            if run.is_locked:
                raise HTTPException(
                    409,
                    "This document is approved and locked. Start a new project to change it.",
                )
            if not is_complete_card_proposal(proposal):
                raise HTTPException(
                    409,
                    "This proposal uses the previous format. Regenerate it before applying.",
                )
            unsupported_fields = InsightCardReconciler.unsupported_factual_fields(
                proposal.before_content,
                proposal.after_content,
                proposal.support_by_field,
            )
            if unsupported_fields:
                labels = {
                    "summary": "Summary",
                    "detail": "Detail",
                    "evidence": "Structured evidence",
                    "interpretation": "Interpretation",
                }
                raise HTTPException(
                    409,
                    "Evidence support is required before this factual update can be applied: "
                    + ", ".join(labels[field] for field in unsupported_fields) + ".",
                )
            current_digest = insight_card_digest(insight)
            if proposal.base_content_digest != current_digest:
                raise self._stale_proposal_error()

            before_content = insight_card_content(insight)
            source_ids = list(dict.fromkeys(proposal.source_ids))
            sources_by_id = {source.id: source for source in workspace.sources}
            if (
                source_ids != list(dict.fromkeys(proposal.after_content.evidence_ids))
                or any(source_id not in sources_by_id for source_id in source_ids)
            ):
                raise HTTPException(409, "The proposal sources changed. Regenerate it.")
            proposal_sources = [sources_by_id[source_id] for source_id in source_ids]
            if (
                proposal.after_content.source_ids
                != list(dict.fromkeys(source.source_id for source in proposal_sources))
                or any(source.question_id not in insight.question_ids for source in proposal_sources)
            ):
                raise HTTPException(409, "The proposal sources changed. Regenerate it.")
            latest_input = next(
                (
                    message.content.strip()[:500]
                    for message in reversed(workspace.messages)
                    if (
                        message.id in proposal.basis_message_ids
                        and message.role is WorkspaceMessageRole.USER
                        and message.state is WorkspaceMessageState.COMPLETED
                        and message.content.strip()
                    )
                ),
                "",
            )
            updated_insight = insight.model_copy(deep=True)
            active_evidence = [
                Evidence(
                    id=source.id,
                    question_id=source.question_id,
                    source_id=source.source_id,
                    source_name=source.source_name,
                    organization=source.organization,
                    tier=source.tier,
                    url=source.url,
                    title=source.title,
                    published=source.published,
                    quote=source.quote,
                    context=source.context,
                    origin=source.origin,
                    tag=source.tag,
                    relevance=source.relevance,
                    identifiers=source.identifiers,
                    retrieved_at=source.retrieved_at,
                )
                for source in proposal_sources
            ]
            derived = InsightCardReconciler.derive_state(
                proposal.after_content.covered,
                proposal.after_content.input_reason,
                active_evidence,
            )
            applied_content = proposal.after_content.model_copy(
                update={
                    "covered": derived.covered,
                    "input_reason": derived.input_reason,
                    "used_web_fallback": derived.used_web_fallback,
                }
            )
            for field in MUTABLE_CARD_FIELDS:
                setattr(updated_insight, field, getattr(applied_content, field))
            updated_insight.review_action = ReviewAction.MODIFIED
            updated_insight.confidence = derived.confidence
            updated_insight.tag = derived.tag
            updated_insight.reviewed_at = utcnow()
            updated_insight.revision_note = proposal.change_note[:400]
            updated_insight.user_input = latest_input
            existing_evidence_ids = {evidence.id for evidence in self.store.get_evidence(run_id)}
            new_evidence = [
                Evidence(
                    id=source.id,
                    question_id=source.question_id,
                    source_id=source.source_id,
                    source_name=source.source_name,
                    organization=source.organization,
                    tier=source.tier,
                    url=source.url,
                    title=source.title,
                    published=source.published,
                    quote=source.quote,
                    context=source.context,
                    origin=source.origin,
                    tag=source.tag,
                    relevance=source.relevance,
                    identifiers=source.identifiers,
                    retrieved_at=source.retrieved_at,
                )
                for source in proposal_sources
                if source.id not in existing_evidence_ids
            ]

            workspace.applied_revisions.append(
                AppliedInsightRevision(
                    proposal_id=proposal.id,
                    previous_summary=before_content.summary,
                    applied_summary=updated_insight.summary,
                    source_ids=source_ids,
                    before_content=before_content,
                    after_content=applied_content,
                    changed_fields=proposal.changed_fields,
                    unchanged_fields=proposal.unchanged_fields,
                    support_by_field=proposal.support_by_field,
                    change_reasons=proposal.change_reasons,
                    base_content_digest=proposal.base_content_digest,
                )
            )
            workspace.pending_proposal = None
            workspace.messages.append(
                InsightWorkspaceMessage(
                    role=WorkspaceMessageRole.SYSTEM,
                    state=WorkspaceMessageState.COMPLETED,
                    content="Approved revision applied to the finding.",
                    source_ids=source_ids,
                )
            )
            workspace.updated_at = utcnow()

            try:
                self.store.commit_insight_card_revision(
                    InsightCardRevisionCommit(
                        insight=updated_insight,
                        new_evidence=new_evidence,
                        workspace=workspace,
                        expected_content_digest=current_digest,
                    )
                )
            except StaleInsightRevision as exc:
                raise self._stale_proposal_error() from exc
            return AppliedRevisionResult(insight=updated_insight, workspace=workspace)
