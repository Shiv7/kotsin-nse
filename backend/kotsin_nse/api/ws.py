"""One WebSocket of state diffs to the UI.

Polling every page every second against a process that is also running a trading loop is a waste;
one socket pushing a small snapshot is cheaper and keeps the UI honest about staleness — the client
knows exactly when it last heard from the engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.clients:
            return
        text = json.dumps(payload, default=str)
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


async def pump(hub: Hub, snapshot, interval_s: float = 2.0) -> None:
    while True:
        with contextlib.suppress(Exception):
            await hub.broadcast(snapshot())
        await asyncio.sleep(interval_s)


async def handle(ws: WebSocket, hub: Hub) -> None:
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:  # noqa: BLE001
        hub.disconnect(ws)
