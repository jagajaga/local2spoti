import asyncio
import pytest

from local2spoti.ratelimit import TokenBucket


async def test_acquire_immediate_when_full():
    b = TokenBucket(rate=10, capacity=5)
    for _ in range(5):
        await b.acquire()  # all immediate


async def test_acquire_blocks_when_empty(monkeypatch):
    b = TokenBucket(rate=100, capacity=2)
    await b.acquire()
    await b.acquire()
    start = asyncio.get_event_loop().time()
    await b.acquire()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.005


async def test_drain():
    b = TokenBucket(rate=10, capacity=5)
    b.drain()
    assert b.tokens == 0


async def test_set_pause_until_blocks():
    b = TokenBucket(rate=1000, capacity=5)
    loop = asyncio.get_event_loop()
    b.pause_for(0.05)
    start = loop.time()
    await b.acquire()
    assert loop.time() - start >= 0.04
