from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(slots=True)
class ProgressEvent:
    stage: str
    processed: int
    total: int
    matched: int = 0
    review: int = 0
    unmatched: int = 0
    errors: int = 0
    message: str | None = None


class EventBus:
    """Pub/sub with per-stage coalescing."""

    def __init__(self, *, min_interval: float = 0.1) -> None:
        self._subscribers: set[asyncio.Queue[ProgressEvent]] = set()
        self._pending: dict[str, ProgressEvent] = {}
        self._last_emit: dict[str, float] = {}
        self._min_interval = min_interval
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[ProgressEvent]:
        q: asyncio.Queue[ProgressEvent] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[ProgressEvent]) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    async def publish(self, event: ProgressEvent) -> None:
        now = time.monotonic()
        last = self._last_emit.get(event.stage, 0.0)
        if now - last >= self._min_interval:
            self._last_emit[event.stage] = now
            await self._fan_out(event)
            self._pending.pop(event.stage, None)
        else:
            self._pending[event.stage] = event

    async def flush(self) -> None:
        for stage, event in list(self._pending.items()):
            self._last_emit[stage] = time.monotonic()
            await self._fan_out(event)
        self._pending.clear()

    async def _fan_out(self, event: ProgressEvent) -> None:
        async with self._lock:
            queues = list(self._subscribers)
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass
