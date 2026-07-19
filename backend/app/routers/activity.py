"""Activity-log API: the running event history, newest first, paginated."""

from typing import Any, List, Optional

from fastapi import APIRouter, Query
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


@router.get("", response_model=ActivityResponse)
async def list_activity(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    level: Optional[str] = Query(None, description="info | warn | error"),
    mix_id: Optional[str] = Query(None),
    event: Optional[str] = Query(None),
):
    """Return the activity log newest-first, filtered by level / mix / event."""
    items, total = await activity_log.query(
        limit=limit, offset=offset, level=level, mix_id=mix_id, event=event
    )
    return ActivityResponse(items=items, total=total, limit=limit, offset=offset)
