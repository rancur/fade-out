"""WebSocket live pipeline-status surface.

A single ``ConnectionManager`` fans pipeline events out to every connected
client. It is registered as an orchestrator event listener at app startup
(``orchestrator.on_event(manager.broadcast_event)``), so every
``pipeline_started`` / ``step_completed`` / ``upload_complete`` / ``error`` /
``draft_ready`` event the state machine emits is streamed live. On connect the
client is sent a one-shot ``snapshot`` with the current queue counts so a fresh
dashboard is populated without waiting for the next event.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import func as sa_func, select

from app.database import async_session_factory
from app.models import Mix

logger = logging.getLogger(__name__)

router = APIRouter()

_ACTIVE_STATUSES = [
    "running", "analyzing", "generating",
    "uploading_soundcloud", "uploading_youtube", "verifying",
]
_QUEUED_STATUSES = ["pending"]


class ConnectionManager:
    """Track connected WebSocket clients and broadcast events to all of them."""

    def __init__(self) -> None:
        self._connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.append(websocket)
        logger.debug("WS client connected (%d total)", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if websocket in self._connections:
                self._connections.remove(websocket)
        logger.debug("WS client disconnected (%d total)", len(self._connections))

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Send a JSON message to every client, pruning any that fail."""
        async with self._lock:
            targets = list(self._connections)

        dead: List[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    if ws in self._connections:
                        self._connections.remove(ws)

    async def broadcast_event(
        self, event_type: str, mix_id: Optional[str], data: Optional[dict] = None
    ) -> None:
        """Orchestrator event listener: (event_type, mix_id, data) -> broadcast."""
        await self.broadcast(
            {"event": event_type, "mix_id": mix_id, "data": data or {}}
        )


manager = ConnectionManager()


async def status_snapshot() -> Dict[str, int]:
    """Return current queue counts (active / queued / completed / failed)."""
    async with async_session_factory() as session:
        async def _count(statuses: List[str]) -> int:
            res = await session.execute(
                select(sa_func.count()).select_from(Mix).where(
                    Mix.pipeline_status.in_(statuses)
                )
            )
            return res.scalar() or 0

        return {
            "active": await _count(_ACTIVE_STATUSES),
            "queued": await _count(_QUEUED_STATUSES),
            "completed": await _count(["completed"]),
            # See routers/pipeline.pipeline_status: an interrupted mix has not
            # shipped either, so it stays counted rather than disappearing.
            "failed": await _count(["failed", "interrupted"]),
        }


@router.websocket("/api/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    """Live pipeline-status stream. Sends a snapshot on connect, then events."""
    await manager.connect(websocket)
    try:
        try:
            snapshot = await status_snapshot()
            await websocket.send_json({"event": "snapshot", "data": snapshot})
        except Exception:
            logger.exception("Failed to send initial WS snapshot")

        # Keep the socket open. We don't require inbound traffic, but support a
        # simple "ping"->"pong" keepalive so clients can detect a live socket.
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_json({"event": "pong"})
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception:
        logger.exception("WS status connection error")
        await manager.disconnect(websocket)
