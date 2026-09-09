"""In-process pub/sub that backs the Server-Sent Events stream.

Each run owns a broadcast channel. Subscribers get their own queue, so a slow
browser tab can never block the orchestrator. A bounded replay buffer lets a
tab that connects late, or reconnects, catch up without re-running anything.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any

MAX_REPLAY = 400
QUEUE_MAXSIZE = 1000


class Event:
    __slots__ = ("seq", "run_id", "type", "data", "ts")

    def __init__(self, seq: int, run_id: str, type_: str, data: dict[str, Any]) -> None:
        self.seq = seq
        self.run_id = run_id
        self.type = type_
        self.data = data
        self.ts = datetime.now(timezone.utc).isoformat()

    def to_sse(self) -> str:
        payload = json.dumps({"seq": self.seq, "type": self.type, "ts": self.ts, **self.data})
        return f"id: {self.seq}\nevent: {self.type}\ndata: {payload}\n\n"


class _Channel:
    def __init__(self) -> None:
        self.seq = 0
        self.replay: deque[Event] = deque(maxlen=MAX_REPLAY)
        self.subscribers: set[asyncio.Queue[Event]] = set()
        self.closed = False


class EventBus:
    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}
        self._lock = asyncio.Lock()

    def _channel(self, run_id: str) -> _Channel:
        ch = self._channels.get(run_id)
        if ch is None:
            ch = _Channel()
            self._channels[run_id] = ch
        return ch

    async def publish(self, run_id: str, type_: str, **data: Any) -> None:
        # `run_id` and `type` are supplied by the channel and the envelope. A
        # caller passing them again would collide with these parameters, so
        # they are dropped rather than allowed to raise mid-run.
        data.pop("run_id", None)
        data.pop("type", None)
        async with self._lock:
            ch = self._channel(run_id)
            ch.seq += 1
            event = Event(ch.seq, run_id, type_, data)
            ch.replay.append(event)
            dead: list[asyncio.Queue[Event]] = []
            for q in ch.subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                ch.subscribers.discard(q)

    async def subscribe(self, run_id: str, last_seq: int = 0) -> asyncio.Queue[Event]:
        async with self._lock:
            ch = self._channel(run_id)
            q: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
            for event in ch.replay:
                if event.seq > last_seq:
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        break
            ch.subscribers.add(q)
            return q

    async def unsubscribe(self, run_id: str, q: asyncio.Queue[Event]) -> None:
        async with self._lock:
            ch = self._channels.get(run_id)
            if ch:
                ch.subscribers.discard(q)

    async def close(self, run_id: str) -> None:
        await self.publish(run_id, "stream_end", reason="run finished")
        async with self._lock:
            ch = self._channels.get(run_id)
            if ch:
                ch.closed = True

    async def reopen(self, run_id: str) -> None:
        """Take a closed channel back into service for a resumed run.

        The replay buffer keeps the phase-one history so a late tab still
        sees what happened, but the two events that ended that phase are
        dropped: a replayed `stream_end` would tear the new stream down at
        once, and a replayed `review_required` would bounce the live page
        straight back to the review it just left.
        """
        async with self._lock:
            ch = self._channel(run_id)
            ch.closed = False
            kept = [e for e in ch.replay if e.type not in ("stream_end", "review_required")]
            ch.replay.clear()
            ch.replay.extend(kept)

    def is_closed(self, run_id: str) -> bool:
        ch = self._channels.get(run_id)
        return bool(ch and ch.closed)


bus = EventBus()
