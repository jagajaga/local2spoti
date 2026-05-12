from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import aiosqlite

from .config import Settings
from .events import EventBus
from .ratelimit import TokenBucket


@dataclass
class AppState:
    settings: Settings
    db_conn: aiosqlite.Connection | None = None
    bus: EventBus = field(default_factory=lambda: EventBus(min_interval=0.1))
    # Spotify rate limit: documented sustained ~180 req/min (3/sec). In
    # practice their per-endpoint or burst limits trip 429s well before
    # that, especially on /search. Tuned down to 2/sec sustained + 15-token
    # burst — keeps under their threshold while still ~120 calls/min.
    # Each 429 we hit pauses the whole bucket for Retry-After seconds, so
    # avoiding 429s in the first place is much faster than 'go faster +
    # hope nothing trips'.
    spotify_bucket: TokenBucket = field(default_factory=lambda: TokenBucket(rate=2.0, capacity=15.0))
    # Three independent task slots so a Spotify match, an AcoustID deep
    # scan, and an AI matching run can all be in flight simultaneously.
    # Each endpoint only gates on its own slot; the Stop button cancels
    # all three; /api/reset refuses if any are running.
    scan_task: asyncio.Task | None = None
    deep_scan_task: asyncio.Task | None = None
    ai_scan_task: asyncio.Task | None = None
    # Auto-cycle: drives the match → AI(review) → AI(unmatched) loop in a
    # single button press. Lives in its own slot because it *delegates*
    # work to the other slots and would otherwise block itself on the
    # same-slot guard.
    auto_cycle_task: asyncio.Task | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def any_job_running(self) -> bool:
        return any(
            t is not None and not t.done()
            for t in (self.scan_task, self.deep_scan_task, self.ai_scan_task, self.auto_cycle_task)
        )
