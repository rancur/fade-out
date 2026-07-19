"""Persistent activity/event log for the fade-out pipeline.

A single append-only store (the ``activity_events`` table) records every
important thing the app does so the running history survives restarts and can
be surfaced live in the UI sidebar and via ``GET /api/activity``.

Design goals:

* **Never break the caller.** Emitting an activity entry is best-effort — a
  failure to write the log must never take down a pipeline step or the watcher,
  so every write is wrapped and swallowed with an internal warning.
* **Cheap to call from anywhere.** ``await activity_log.info(...)`` from any
  async code path (watcher, ingest, pipeline, handlers, auth) writes one row.
* **Live fan-out (optional).** Listeners registered via :func:`add_listener`
  (e.g. a websocket broadcaster) receive each serialized entry so a connected
  UI can update without waiting for its next poll. Polling still works with no
  listeners attached, which is the default the sidebar relies on.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from sqlalchemy import desc, func, select

from app.database import async_session_factory
from app.models import ActivityEvent

logger = logging.getLogger(__name__)

VALID_LEVELS = {"info", "warn", "error"}

# Optional live listeners: called with the serialized event dict after a
# successful write. Kept module-level so this stays a lightweight singleton with
# no import cycles.
_listeners: List[Callable[[dict], Any]] = []


def add_listener(fn: Callable[[dict], Any]) -> None:
    """Register a listener invoked with each serialized event after it is stored."""
    _listeners.append(fn)


def serialize(row: ActivityEvent) -> dict:
    """Turn an ActivityEvent row into a JSON-safe dict."""
    ts = row.ts or datetime.now(timezone.utc)
    return {
        "id": row.id,
        "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "level": row.level,
        "event": row.event,
        "message": row.message,
        "mix_id": row.mix_id,
        "filename": row.filename,
        "platform": row.platform,
        "stage": row.stage,
        "context": row.context,
    }


async def log(
    level: str,
    event: str,
    message: str,
    *,
    mix_id: Optional[str] = None,
    filename: Optional[str] = None,
    platform: Optional[str] = None,
    stage: Optional[str] = None,
    context: Optional[dict] = None,
) -> None:
    """Write one activity entry. Best-effort — never raises to the caller."""
    if level not in VALID_LEVELS:
        level = "info"
    payload: Optional[dict] = None
    try:
        async with async_session_factory() as session:
            row = ActivityEvent(
                ts=datetime.now(timezone.utc),
                level=level,
                event=event,
                message=(message or "")[:2000],
                mix_id=mix_id,
                filename=filename,
                platform=platform,
                stage=stage,
                context=context or None,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            payload = serialize(row)
    except Exception:  # pragma: no cover - defensive
        logger.exception("activity_log: failed to persist event %r", event)
        return

    # Mirror it to the app logger so it also shows up in container logs.
    log_fn = {"error": logger.error, "warn": logger.warning}.get(level, logger.info)
    log_fn("[activity] %s: %s", event, message)

    for fn in list(_listeners):
        try:
            result = fn(payload)
            if hasattr(result, "__await__"):
                await result  # type: ignore[func-returns-value]
        except Exception:  # pragma: no cover - defensive
            logger.exception("activity_log: listener failed for %r", event)


async def info(event: str, message: str, **kwargs: Any) -> None:
    await log("info", event, message, **kwargs)


async def warn(event: str, message: str, **kwargs: Any) -> None:
    await log("warn", event, message, **kwargs)


async def error(event: str, message: str, **kwargs: Any) -> None:
    await log("error", event, message, **kwargs)


async def query(
    limit: int = 100,
    offset: int = 0,
    level: Optional[str] = None,
    mix_id: Optional[str] = None,
    event: Optional[str] = None,
) -> Tuple[List[dict], int]:
    """Return (items, total) newest-first with optional level/mix/event filters."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    async with async_session_factory() as session:
        base = select(ActivityEvent)
        count_stmt = select(func.count()).select_from(ActivityEvent)
        if level:
            base = base.where(ActivityEvent.level == level)
            count_stmt = count_stmt.where(ActivityEvent.level == level)
        if mix_id:
            base = base.where(ActivityEvent.mix_id == mix_id)
            count_stmt = count_stmt.where(ActivityEvent.mix_id == mix_id)
        if event:
            base = base.where(ActivityEvent.event == event)
            count_stmt = count_stmt.where(ActivityEvent.event == event)

        total = (await session.execute(count_stmt)).scalar() or 0
        rows = (
            (
                await session.execute(
                    base.order_by(desc(ActivityEvent.ts), desc(ActivityEvent.id))
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
    return [serialize(r) for r in rows], total
