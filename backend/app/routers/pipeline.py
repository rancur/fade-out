"""Pipeline control endpoints."""

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Mix

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

# In-memory pause flag (would be in Redis/DB in production)
_pipeline_paused = False


class PipelineStatusResponse(BaseModel):
    paused: bool
    active: int
    queued: int
    completed: int
    failed: int


class QueueItem(BaseModel):
    mix_id: str
    title: str
    pipeline_status: Optional[str] = None
    pipeline_step: Optional[str] = None

    class Config:
        from_attributes = True


class QueueResponse(BaseModel):
    items: List[QueueItem]
    total: int


class PauseResponse(BaseModel):
    paused: bool
    message: str


@router.get("/status", response_model=PipelineStatusResponse)
async def pipeline_status(db: AsyncSession = Depends(get_db)):
    """Get overall pipeline status with counts of active, queued, completed, and failed mixes."""
    active_statuses = ["analyzing", "generating", "uploading_soundcloud", "uploading_youtube", "verifying"]
    queued_statuses = ["pending"]
    completed_statuses = ["completed"]
    # "interrupted" counts here too: a mix a restart cut off is awaiting an
    # automatic resume, and until it lands it is a mix that has not shipped.
    # Leaving it out of every bucket is how it would vanish from the dashboard.
    failed_statuses = ["failed", "interrupted"]

    active_result = await db.execute(
        select(sa_func.count()).select_from(Mix).where(Mix.pipeline_status.in_(active_statuses))
    )
    queued_result = await db.execute(
        select(sa_func.count()).select_from(Mix).where(Mix.pipeline_status.in_(queued_statuses))
    )
    completed_result = await db.execute(
        select(sa_func.count()).select_from(Mix).where(Mix.pipeline_status.in_(completed_statuses))
    )
    failed_result = await db.execute(
        select(sa_func.count()).select_from(Mix).where(Mix.pipeline_status.in_(failed_statuses))
    )

    return PipelineStatusResponse(
        paused=_pipeline_paused,
        active=active_result.scalar() or 0,
        queued=queued_result.scalar() or 0,
        completed=completed_result.scalar() or 0,
        failed=failed_result.scalar() or 0,
    )


@router.post("/pause", response_model=PauseResponse)
async def pause_pipeline():
    """Pause all pipeline processing."""
    global _pipeline_paused
    _pipeline_paused = True
    return PauseResponse(paused=True, message="Pipeline paused. No new steps will be started.")


@router.post("/resume", response_model=PauseResponse)
async def resume_pipeline():
    """Resume pipeline processing."""
    global _pipeline_paused
    _pipeline_paused = False
    return PauseResponse(paused=False, message="Pipeline resumed. Queued items will begin processing.")


@router.get("/queue", response_model=QueueResponse)
async def get_queue(db: AsyncSession = Depends(get_db)):
    """Get all queued and in-progress pipeline items."""
    active_or_queued = [
        "pending", "analyzing", "generating",
        "uploading_soundcloud", "uploading_youtube",
        "verifying", "draft_review",
    ]
    result = await db.execute(
        select(Mix)
        .where(Mix.pipeline_status.in_(active_or_queued))
        .order_by(Mix.created_at.asc())
    )
    rows = result.scalars().all()

    items = [
        QueueItem(
            mix_id=r.id,
            title=r.title,
            pipeline_status=r.pipeline_status,
            pipeline_step=r.pipeline_step,
        )
        for r in rows
    ]
    return QueueResponse(items=items, total=len(items))
