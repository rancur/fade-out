"""Activity-log API: the running event history, newest first, paginated."""

from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services import activity_log

router = APIRouter(prefix="/api/activity", tags=["activity"])


class ActivityItem(BaseModel):
    id: int
    ts: str
    level: str
    event: Optional[str] = None
    message: Optional[str] = None
    mix_id: Optional[str] = None
    filename: Optional[str] = None
    platform: Optional[str] = None
    stage: Optional[str] = None
    context: Optional[Any] = None


class ActivityResponse(BaseModel):
    items: List[ActivityItem]
    total: int
    limit: int
    offset: int


def _parse_iso(value: Optional[str], name: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"Invalid ISO datetime for '{name}': {value!r}"
        )


@router.get("", response_model=ActivityResponse)
async def list_activity(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    level: Optional[str] = Query(None, description="info | warn | error"),
    mix_id: Optional[str] = Query(None),
    event: Optional[str] = Query(None, description="Exact event key, e.g. upload_result"),
    q: Optional[str] = Query(None, description="Case-insensitive substring search on message"),
    platform: Optional[str] = Query(None, description="soundcloud | youtube | mixcloud"),
    since: Optional[str] = Query(None, description="ISO timestamp lower bound (inclusive)"),
    until: Optional[str] = Query(None, description="ISO timestamp upper bound (inclusive)"),
    before_id: Optional[int] = Query(
        None, ge=1,
        description="Cursor: only rows with id < before_id (for infinite scroll)",
    ),
):
    """Return the activity log newest-first with filtering + cursor pagination.

    ``limit``/``offset`` paging keeps working; ``before_id`` + ``limit`` is the
    cursor path for infinite scroll (``total`` stays the full filtered count).
    """
    items, total = await activity_log.query(
        limit=limit,
        offset=offset,
        level=level,
        mix_id=mix_id,
        event=event,
        q=q,
        platform=platform,
        since=_parse_iso(since, "since"),
        until=_parse_iso(until, "until"),
        before_id=before_id,
    )
    return ActivityResponse(items=items, total=total, limit=limit, offset=offset)
