"""Shorts endpoints: list/stats, backlog scan, manual upload, edit, skip.

Same conventions as the catalog router: long-running work (scan, upload,
metadata regen) returns 202 and runs in an asyncio task with its own session;
progress lands in the activity log (``shorts_*`` events, streamed over the
existing WS activity channel) and the scan summary in
``AppSettings.settings_json["shorts_last_scan"]``.
"""

import asyncio
import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func as sa_func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AppSettings, Short
from app.services.shorts_pipeline import (
    LAST_SCAN_KEY,
    VALID_STATUSES,
    get_shorts_service,
    serialize_short,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/shorts", tags=["shorts"])

_scan_task: Optional[asyncio.Task] = None


def _spawn(name: str, coro) -> asyncio.Task:
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task) -> None:
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logger.error("shorts %s task failed: %s", name, exc)

    task.add_done_callback(_done)
    return task


# --- Schemas ---


class ShortListResponse(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    page: int
    page_size: int


class ShortUpdateBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None


async def _get_short(db: AsyncSession, short_id: str) -> Short:
    short = (
        await db.execute(select(Short).where(Short.id == short_id))
    ).scalar_one_or_none()
    if not short:
        raise HTTPException(status_code=404, detail="Short not found")
    return short


# --- List + stats ---


@router.get("", response_model=ShortListResponse)
async def list_shorts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Filename/title substring filter"),
    sort: Literal["newest", "oldest"] = Query("newest"),
    db: AsyncSession = Depends(get_db),
):
    """Paginated shorts listing, newest-detected first by default."""
    conditions = []
    if status:
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail=f"Unknown status '{status}'")
        conditions.append(Short.status == status)
    if q:
        conditions.append(
            or_(Short.file_path.ilike(f"%{q}%"), Short.title.ilike(f"%{q}%"))
        )

    base = select(Short)
    count_query = select(sa_func.count()).select_from(Short)
    for cond in conditions:
        base = base.where(cond)
        count_query = count_query.where(cond)

    order = (
        (Short.detected_at.asc(), Short.id.asc())
        if sort == "oldest"
        else (Short.detected_at.desc(), Short.id.desc())
    )
    total = (await db.execute(count_query)).scalar() or 0
    rows = (
        (
            await db.execute(
                base.order_by(*order).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return ShortListResponse(
        items=[serialize_short(s) for s in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/stats")
async def shorts_stats():
    """Daily-cap indicator: uploads today vs cap, queue depth, quota state."""
    return await get_shorts_service().stats()


# --- Backlog scan ---


@router.post("/scan", status_code=202)
async def trigger_scan():
    """Scan the watch folder and ingest everything not yet in the table."""
    global _scan_task
    if _scan_task and not _scan_task.done():
        return {"status": "already_running"}
    _scan_task = _spawn("scan", get_shorts_service().scan())
    return {"status": "started"}


@router.get("/scan/status")
async def scan_status(db: AsyncSession = Depends(get_db)):
    """Whether a scan is running + the last completed run's summary."""
    row = (
        await db.execute(select(AppSettings).where(AppSettings.id == 1))
    ).scalar_one_or_none()
    last = ((row.settings_json or {}).get(LAST_SCAN_KEY)) if row else None
    return {
        "running": bool(_scan_task and not _scan_task.done()),
        "last_scan": last,
    }


# --- Per-short actions ---


@router.post("/{short_id}/upload", status_code=202)
async def upload_short(short_id: str, db: AsyncSession = Depends(get_db)):
    """Manually upload one short now (bypasses the daily cap, not the quota)."""
    short = await _get_short(db, short_id)
    if short.status in ("uploaded", "uploading"):
        raise HTTPException(
            status_code=409, detail=f"Short is already {short.status}"
        )
    if not short.title or not short.description:
        raise HTTPException(
            status_code=409,
            detail="Short has no metadata yet — wait for analysis or regenerate",
        )
    _spawn("upload", get_shorts_service().upload_short_by_id(short_id, manual=True))
    return {"status": "started", "id": short_id}


@router.post("/{short_id}/skip")
async def skip_short(short_id: str, db: AsyncSession = Depends(get_db)):
    """Mark a short as skipped (never auto-uploaded)."""
    short = await _get_short(db, short_id)
    if short.status in ("uploaded", "uploading"):
        raise HTTPException(status_code=409, detail=f"Cannot skip an {short.status} short")
    short.status = "skipped"
    await db.flush()
    return serialize_short(short)


@router.post("/{short_id}/unskip")
async def unskip_short(short_id: str, db: AsyncSession = Depends(get_db)):
    """Bring a skipped short back into the pipeline.

    With metadata already generated it re-queues for the next drain pass;
    otherwise (e.g. a catalog-dedupe skip) it re-runs the full analyze +
    metadata pipeline in the background.
    """
    short = await _get_short(db, short_id)
    if short.status != "skipped":
        raise HTTPException(status_code=409, detail="Short is not skipped")
    short.error = None
    if short.title and short.description:
        short.status = "queued"
        await db.flush()
        return serialize_short(short)
    short.status = "detected"
    await db.flush()
    await db.commit()
    _spawn("process", get_shorts_service().process_short(short_id))
    return serialize_short(short)


@router.put("/{short_id}")
async def update_short(
    short_id: str, body: ShortUpdateBody, db: AsyncSession = Depends(get_db)
):
    """Edit title/description/tags before upload."""
    short = await _get_short(db, short_id)
    if short.status in ("uploaded", "uploading"):
        raise HTTPException(
            status_code=409, detail=f"Cannot edit an {short.status} short"
        )
    if body.title is None and body.description is None and body.tags is None:
        raise HTTPException(status_code=400, detail="No edits provided")
    if body.title is not None:
        short.title = body.title[:100]
    if body.description is not None:
        short.description = body.description[:5000]
    if body.tags is not None:
        short.tags = body.tags
    await db.flush()
    return serialize_short(short)


@router.post("/{short_id}/regenerate-metadata", status_code=202)
async def regenerate_metadata(short_id: str, db: AsyncSession = Depends(get_db)):
    """Re-run the LLM title/description/tags generation in the background."""
    short = await _get_short(db, short_id)
    if short.status in ("uploaded", "uploading"):
        raise HTTPException(
            status_code=409, detail=f"Cannot regenerate an {short.status} short"
        )
    _spawn("regen", get_shorts_service().regenerate_metadata(short_id))
    return {"status": "started", "id": short_id}
