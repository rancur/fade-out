"""Mix CRUD and pipeline trigger endpoints."""

from datetime import datetime
from typing import List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Mix, PipelineStep

# Lazy import to avoid circular dependency -- the orchestrator is set at app startup
def _get_orchestrator():
    from app.main import orchestrator
    return orchestrator

router = APIRouter(prefix="/api/mixes", tags=["mixes"])


# --- Schemas ---

class TracklistItem(BaseModel):
    title: str
    artist: str
    timestamp: Optional[str] = None


class PipelineStepOut(BaseModel):
    id: int
    step_name: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    retry_count: int = 0
    output_json: Optional[dict] = None

    class Config:
        from_attributes = True


class MixOut(BaseModel):
    id: str
    title: str
    audio_file_path: Optional[str] = None
    video_file_path: Optional[str] = None
    duration_seconds: Optional[float] = None
    genres: Optional[list] = None
    vibes: Optional[list] = None
    energy_profile: Optional[list] = None
    tracklist: Optional[list] = None
    description_soundcloud: Optional[str] = None
    description_youtube: Optional[str] = None
    title_youtube: Optional[str] = None
    tags: Optional[list] = None
    cover_art_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    soundcloud_url: Optional[str] = None
    youtube_url: Optional[str] = None
    youtube_playlist_id: Optional[str] = None
    pipeline_status: Optional[str] = None
    pipeline_step: Optional[str] = None
    pipeline_error: Optional[str] = None
    pipeline_started_at: Optional[datetime] = None
    pipeline_completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata_json: Optional[dict] = None

    class Config:
        from_attributes = True


class MixDetail(MixOut):
    steps: List[PipelineStepOut] = []


class MixCreate(BaseModel):
    title: str
    audio_file_path: Optional[str] = None
    video_file_path: Optional[str] = None
    metadata_json: Optional[dict] = None


class MixUpdate(BaseModel):
    title: Optional[str] = None
    description_soundcloud: Optional[str] = None
    description_youtube: Optional[str] = None
    title_youtube: Optional[str] = None
    tags: Optional[list] = None
    genres: Optional[list] = None
    vibes: Optional[list] = None
    tracklist: Optional[list] = None
    cover_art_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    metadata_json: Optional[dict] = None


class MixListResponse(BaseModel):
    items: List[MixOut]
    total: int
    page: int
    page_size: int


# --- Endpoints ---

@router.get("", response_model=MixListResponse)
async def list_mixes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None, description="Filter by pipeline_status"),
    db: AsyncSession = Depends(get_db),
):
    """List all mixes with pagination and optional status filter."""
    query = select(Mix)
    count_query = select(sa_func.count()).select_from(Mix)

    if status:
        query = query.where(Mix.pipeline_status == status)
        count_query = count_query.where(Mix.pipeline_status == status)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(Mix.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    rows = result.scalars().all()

    return MixListResponse(
        items=[MixOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{mix_id}", response_model=MixDetail)
async def get_mix(mix_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single mix with all its pipeline steps."""
    result = await db.execute(
        select(Mix).options(selectinload(Mix.steps)).where(Mix.id == mix_id)
    )
    mix = result.scalar_one_or_none()
    if not mix:
        raise HTTPException(status_code=404, detail="Mix not found")
    # Convert to dict to avoid lazy-load issues with Pydantic
    mix_dict = {c.name: getattr(mix, c.name) for c in mix.__table__.columns}
    mix_dict["steps"] = [
        {c.name: getattr(s, c.name) for c in s.__table__.columns}
        for s in mix.steps
    ]
    return MixDetail(**mix_dict)


@router.post("", response_model=MixOut, status_code=201)
async def create_mix(body: MixCreate, db: AsyncSession = Depends(get_db)):
    """Manually create a mix and trigger its pipeline."""
    mix = Mix(
        id=str(uuid4()),
        title=body.title,
        audio_file_path=body.audio_file_path,
        video_file_path=body.video_file_path,
        metadata_json=body.metadata_json,
        pipeline_status="pending",
        pipeline_started_at=datetime.utcnow(),
    )
    db.add(mix)
    await db.flush()

    # Create initial pipeline steps
    step_names = [
        "detect", "analyze", "generate_description", "generate_art",
        "upload_soundcloud", "upload_youtube", "verify_soundcloud",
        "verify_youtube", "cross_link",
    ]
    for name in step_names:
        db.add(PipelineStep(mix_id=mix.id, step_name=name, status="pending"))

    await db.flush()
    await db.refresh(mix)

    # Kick off the pipeline asynchronously
    orch = _get_orchestrator()
    await orch.start_pipeline(mix.id)

    return MixOut.model_validate(mix)


@router.put("/{mix_id}", response_model=MixOut)
async def update_mix(mix_id: str, body: MixUpdate, db: AsyncSession = Depends(get_db)):
    """Update mix metadata, typically during draft review."""
    result = await db.execute(select(Mix).where(Mix.id == mix_id))
    mix = result.scalar_one_or_none()
    if not mix:
        raise HTTPException(status_code=404, detail="Mix not found")

    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(mix, key, value)

    await db.flush()
    await db.refresh(mix)
    return MixOut.model_validate(mix)


@router.delete("/{mix_id}", status_code=204)
async def delete_mix(mix_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a mix and all associated data."""
    result = await db.execute(select(Mix).where(Mix.id == mix_id))
    mix = result.scalar_one_or_none()
    if not mix:
        raise HTTPException(status_code=404, detail="Mix not found")
    await db.delete(mix)
    await db.flush()


@router.post("/{mix_id}/approve", response_model=MixOut)
async def approve_mix(mix_id: str, db: AsyncSession = Depends(get_db)):
    """Approve a draft mix for publishing."""
    result = await db.execute(select(Mix).where(Mix.id == mix_id))
    mix = result.scalar_one_or_none()
    if not mix:
        raise HTTPException(status_code=404, detail="Mix not found")
    if mix.pipeline_status != "draft_review":
        raise HTTPException(
            status_code=400,
            detail=f"Mix is not in draft_review status (current: {mix.pipeline_status})",
        )

    mix.pipeline_status = "resuming"
    await db.flush()
    await db.refresh(mix)

    # Resume pipeline from where it paused (after generate_art)
    orch = _get_orchestrator()
    await orch.resume_pipeline(mix.id)

    return MixOut.model_validate(mix)


@router.post("/{mix_id}/retry", response_model=MixOut)
async def retry_mix(mix_id: str, db: AsyncSession = Depends(get_db)):
    """Retry a failed pipeline from the failed step."""
    result = await db.execute(
        select(Mix).options(selectinload(Mix.steps)).where(Mix.id == mix_id)
    )
    mix = result.scalar_one_or_none()
    if not mix:
        raise HTTPException(status_code=404, detail="Mix not found")
    if mix.pipeline_status != "failed":
        raise HTTPException(
            status_code=400,
            detail=f"Mix is not in failed status (current: {mix.pipeline_status})",
        )

    # Find the failed step and reset it
    for step in mix.steps:
        if step.status == "failed":
            step.status = "pending"
            step.error = None
            step.retry_count += 1

    mix.pipeline_status = "pending"
    mix.pipeline_error = None
    await db.flush()
    await db.refresh(mix)

    # Re-start the full pipeline (it will skip already-completed steps)
    orch = _get_orchestrator()
    await orch.start_pipeline(mix.id)

    return MixOut.model_validate(mix)


@router.post("/{mix_id}/retry-step/{step_name}", response_model=MixOut)
async def retry_step(mix_id: str, step_name: str, db: AsyncSession = Depends(get_db)):
    """Retry a specific pipeline step."""
    result = await db.execute(
        select(Mix).options(selectinload(Mix.steps)).where(Mix.id == mix_id)
    )
    mix = result.scalar_one_or_none()
    if not mix:
        raise HTTPException(status_code=404, detail="Mix not found")

    target_step = None
    for step in mix.steps:
        if step.step_name == step_name:
            target_step = step
            break

    if not target_step:
        raise HTTPException(status_code=404, detail=f"Step '{step_name}' not found")

    target_step.status = "pending"
    target_step.error = None
    target_step.retry_count += 1

    if mix.pipeline_status == "failed":
        mix.pipeline_status = "pending"
        mix.pipeline_error = None

    await db.flush()
    await db.refresh(mix)

    # Retry the specific step and continue from there
    orch = _get_orchestrator()
    await orch.retry_step(mix.id, step_name)

    return MixOut.model_validate(mix)
