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
    scan_task: asyncio.Task | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
