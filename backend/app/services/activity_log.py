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
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from sqlalchemy import delete, desc, func, select

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


def _normalize_dt(value: Optional[datetime]) -> Optional[datetime]:
    """Make a datetime timezone-aware (assume UTC when naive)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def query(
    limit: int = 100,
    offset: int = 0,
    level: Optional[str] = None,
    mix_id: Optional[str] = None,
    event: Optional[str] = None,
    q: Optional[str] = None,
    platform: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    before_id: Optional[int] = None,
) -> Tuple[List[dict], int]:
    """Return (items, total) newest-first with optional filters.

    Filters: level/mix/event/platform exact, ``q`` case-insensitive substring
    on message, ``since``/``until`` timestamp range, and ``before_id`` cursor
    (rows with id strictly below it) for infinite scroll. ``total`` counts all
    rows matching the filters excluding the cursor, so pagination UIs keep a
    stable total while scrolling.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    since = _normalize_dt(since)
    until = _normalize_dt(until)

    filters = []
    if level:
        filters.append(ActivityEvent.level == level)
    if mix_id:
        filters.append(ActivityEvent.mix_id == mix_id)
    if event:
        filters.append(ActivityEvent.event == event)
    if platform:
        filters.append(ActivityEvent.platform == platform)
    if q:
        filters.append(ActivityEvent.message.ilike(f"%{q}%"))
    if since:
        filters.append(ActivityEvent.ts >= since)
    if until:
        filters.append(ActivityEvent.ts <= until)

    async with async_session_factory() as session:
        count_stmt = select(func.count()).select_from(ActivityEvent)
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = (await session.execute(count_stmt)).scalar() or 0

        base = select(ActivityEvent)
        for f in filters:
            base = base.where(f)
        if before_id is not None:
            base = base.where(ActivityEvent.id < before_id)
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


async def prune(
    retention_days: Optional[int] = None,
    max_rows: Optional[int] = None,
) -> dict:
    """Delete activity rows older than the retention window or beyond the cap.

    Returns {"deleted_by_age": n, "deleted_by_cap": m}. Best-effort — never
    raises (it runs from a background task).
    """
    from app.services import app_config

    if retention_days is None:
        retention_days = int(await app_config.resolve("activity_retention_days"))
    if max_rows is None:
        max_rows = int(await app_config.resolve("activity_max_rows"))

    result = {"deleted_by_age": 0, "deleted_by_cap": 0}
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        async with async_session_factory() as session:
            aged = await session.execute(
                delete(ActivityEvent).where(ActivityEvent.ts < cutoff)
            )
            result["deleted_by_age"] = aged.rowcount or 0

            total = (
                await session.execute(select(func.count()).select_from(ActivityEvent))
            ).scalar() or 0
            if total > max_rows:
                # id of the max_rows-th newest row: everything below it goes.
                threshold_id = (
                    await session.execute(
                        select(ActivityEvent.id)
                        .order_by(desc(ActivityEvent.id))
                        .offset(max_rows - 1)
                        .limit(1)
                    )
                ).scalar()
                if threshold_id is not None:
                    capped = await session.execute(
                        delete(ActivityEvent).where(ActivityEvent.id < threshold_id)
                    )
                    result["deleted_by_cap"] = capped.rowcount or 0
            await session.commit()
        if result["deleted_by_age"] or result["deleted_by_cap"]:
            logger.info(
                "activity_log: pruned %d aged + %d over-cap rows",
                result["deleted_by_age"], result["deleted_by_cap"],
            )
    except Exception:  # pragma: no cover - defensive
        logger.exception("activity_log: prune failed")
    return result
