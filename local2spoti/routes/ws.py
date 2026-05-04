from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/progress")
async def progress(ws: WebSocket) -> None:
    await ws.accept()
    state = ws.app.state.app_state
    queue = await state.bus.subscribe()
    try:
        while True:
            event = await queue.get()
            await ws.send_text(json.dumps(asdict(event)))
    except WebSocketDisconnect:
        pass
    finally:
        await state.bus.unsubscribe(queue)
