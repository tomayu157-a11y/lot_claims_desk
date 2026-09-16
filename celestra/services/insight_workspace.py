"""Persistent, insight-scoped research chat lifecycle."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from fastapi import HTTPException

from ..models import (
    Evidence,
    InsightWorkspace,
    InsightWorkspaceMessage,
    InsightWorkspaceSource,
    WorkspaceEvent,
    WorkspaceEventType,
    WorkspaceMessageRole,
    WorkspaceMessageState,
    utcnow,
    workspace_id,
)
from .insight_research import ResearchContext, answer_turn, summarize_context
from .llm import llm


class InsightWorkspaceService:
    def __init__(
        self,
        store,
        registry_factory: Callable[[], dict],
        answerer=answer_turn,
        summarizer=summarize_context,
        llm_client=llm,
        lock_registry=None,
        active_token_limit: int = 16_000,
        reduced_token_target: int = 10_000,
    ) -> None:
        self.store = store
        self.registry_factory = registry_factory
        self.answerer = answerer
        self.summarizer = summarizer
        self.llm_client = llm_client
        self.lock_registry = lock_registry if lock_registry is not None else {}
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
        key = workspace_id(run_id, insight_id)
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
        questions = [
            question
            for question in self.store.get_questions(run_id)
            if question.id in question_ids
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
            question=questions[0],
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
    ) -> dict[str, str]:
        canonical_ids = {source.id: source.id for source in workspace.sources}
        canonical_by_key = {
            (source.source_id, source.url, source.quote): source.id
            for source in workspace.sources
        }
        source_id_map: dict[str, str] = {}
        for item in evidence:
            if not item.url.startswith(("https://", "http://")):
                continue
            if item.id in canonical_ids:
                source_id_map[item.id] = canonical_ids[item.id]
                continue
            canonical_id = canonical_by_key.get(self._source_key(item))
            if canonical_id:
                source_id_map[item.id] = canonical_id
                continue
            workspace.sources.append(
                InsightWorkspaceSource(
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
            )
            canonical_ids[item.id] = item.id
            canonical_by_key[self._source_key(item)] = item.id
            source_id_map[item.id] = item.id
        return source_id_map

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
                source_id_map = self._merge_sources(workspace, result.evidence)
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
