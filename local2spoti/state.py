from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from .config import Settings
from .events import EventBus
from .ratelimit import TokenBucket


@dataclass
class AppState:
    settings: Settings
    db_conn: aiosqlite.Connection | None = None
    bus: EventBus = field(default_factory=lambda: EventBus(min_interval=0.1))
    spotify_bucket: TokenBucket = field(
        default_factory=lambda: TokenBucket(rate=3.0, capacity=30.0)
    )
    # Three independent task slots so a Spotify match, an AcoustID deep
    # scan, and an AI matching run can all be in flight simultaneously.
    # Each endpoint only gates on its own slot; the Stop button cancels
    # all three; /api/reset refuses if any are running.
    scan_task: asyncio.Task | None = None
    deep_scan_task: asyncio.Task | None = None
    ai_scan_task: asyncio.Task | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def any_job_running(self) -> bool:
        for t in (self.scan_task, self.deep_scan_task, self.ai_scan_task):
            if t is not None and not t.done():
                return True
        return False
