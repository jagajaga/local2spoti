from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Async token bucket. Capacity tokens; refills `rate` tokens per second."""

    def __init__(self, *, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self._last = time.monotonic()
        self._pause_until = 0.0
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta > 0:
            self.tokens = min(self.capacity, self.tokens + delta * self.rate)
            self._last = now

    async def acquire(self, n: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self._pause_until:
                    sleep = self._pause_until - now
                else:
                    self._refill()
                    if self.tokens >= n:
                        self.tokens -= n
                        return
                    sleep = (n - self.tokens) / self.rate
            await asyncio.sleep(max(sleep, 0.001))

    def drain(self) -> None:
        self.tokens = 0

    def pause_for(self, seconds: float) -> None:
        self._pause_until = max(self._pause_until, time.monotonic() + seconds)
        self.drain()
