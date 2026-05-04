import asyncio
import pytest

from local2spoti.events import EventBus, ProgressEvent


async def test_subscribers_receive_events():
    bus = EventBus(min_interval=0.0)
    queue = await bus.subscribe()
    await bus.publish(ProgressEvent(stage="discovery", processed=1, total=10))
    e = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert e.processed == 1


async def test_coalescing_drops_intermediate():
    bus = EventBus(min_interval=0.05)
    queue = await bus.subscribe()
    for i in range(10):
        await bus.publish(ProgressEvent(stage="match", processed=i, total=10))
    await asyncio.sleep(0.1)
    await bus.flush()
    received: list[int] = []
    while not queue.empty():
        received.append(queue.get_nowait().processed)
    assert len(received) <= 3
    assert received[-1] == 9


async def test_unsubscribe():
    bus = EventBus(min_interval=0.0)
    q = await bus.subscribe()
    await bus.unsubscribe(q)
    await bus.publish(ProgressEvent(stage="x", processed=1, total=1))
    assert q.empty()
