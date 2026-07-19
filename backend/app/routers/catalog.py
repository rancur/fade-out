"""Back-catalog management endpoints: sync, unified mix list, proposals, apply.

Background jobs (sync / apply / improve) follow the reread-tracklist pattern:
the endpoint returns 202 immediately and the work runs in an asyncio task with
its own session; progress lands in the activity log and the sync summary in
``AppSettings.settings_json``.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func as sa_func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AppSettings, Mix, MixProposal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/catalog", tags=["catalog"])

VALID_PLATFORMS = {"youtube", "soundcloud", "both"}
VALID_FIELDS = {"title", "description", "thumbnail", "playlist", "tags"}
OPEN_STATUSES = ("draft", "approved", "applying")

# Background task handles (module-level so status endpoints can report them).
_sync_task: Optional[asyncio.Task] = None
_apply_task: Optional[asyncio.Task] = None
_improve_task: Optional[asyncio.Task] = None
_backfill_task: Optional[asyncio.Task] = None
_regen_thumbs_task: Optional[asyncio.Task] = None
_playlists_task: Optional[asyncio.Task] = None


def _spawn(name: str, coro) -> asyncio.Task:
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task) -> None:
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logger.error("catalog %s task failed: %s", name, exc)

    task.add_done_callback(_done)
    return task


# --- Schemas ---


class CatalogMixOut(BaseModel):
    id: str
    title: str
    source: str
    platforms: List[str]
    youtube_video_id: Optional[str] = None
    soundcloud_track_id: Optional[str] = None
    youtube_url: Optional[str] = None
    soundcloud_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    artwork_url: Optional[str] = None
    duration_seconds: Optional[float] = None
    youtube_published_at: Optional[str] = None
    soundcloud_published_at: Optional[str] = None
    title_locked: bool = False
    open_proposals: int = 0


class CatalogMixListResponse(BaseModel):
    items: List[CatalogMixOut]
    total: int
    page: int
    page_size: int


class ProposalOut(BaseModel):
    id: str
    mix_id: str
    mix_title: Optional[str] = None
    platform: str
    field: str
    current_value: Optional[str] = None
    proposed_value: Optional[str] = None
    status: str
    created_by: str
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None


class ProposalListResponse(BaseModel):
    items: List[ProposalOut]
    total: int


class ProposalCreate(BaseModel):
    platform: Literal["youtube", "soundcloud", "both"]
    field: Literal["title", "description", "thumbnail", "playlist", "tags"]
    proposed_value: str
    current_value: Optional[str] = None
    status: Literal["draft", "approved"] = "draft"


class BulkApproveBody(BaseModel):
    ids: List[str]


class LockTitleBody(BaseModel):
    locked: bool


class PlatformEdit(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None


class MixEditBody(BaseModel):
    youtube: Optional[PlatformEdit] = None
    soundcloud: Optional[PlatformEdit] = None
    apply: bool = False


class ImproveBody(BaseModel):
    mix_ids: Optional[Any] = None  # list of ids | "all_generic" | omitted


class BackfillBody(BaseModel):
    mix_ids: Optional[List[str]] = None  # subset of mix ids, or omitted for all


class RegenThumbsBody(BaseModel):
    mix_ids: Any = None  # list of ids | "all" | "raid-trains"


class OrganizePlaylistsBody(BaseModel):
    mix_ids: Optional[Any] = None  # list of ids | "all" | omitted


# --- Helpers ---


def _serialize_proposal(p: MixProposal, mix_title: Optional[str] = None) -> ProposalOut:
    return ProposalOut(
        id=p.id,
        mix_id=p.mix_id,
        mix_title=mix_title,
        platform=p.platform,
        field=p.field,
        current_value=p.current_value,
        proposed_value=p.proposed_value,
        status=p.status,
        created_by=p.created_by,
        error=p.error,
        created_at=p.created_at,
        updated_at=p.updated_at,
        applied_at=p.applied_at,
    )


async def _get_proposal(db: AsyncSession, proposal_id: str) -> MixProposal:
    result = await db.execute(
        select(MixProposal).where(MixProposal.id == proposal_id)
    )
    proposal = result.scalar_one_or_none()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal


def _catalog_mix_out(mix: Mix, open_count: int) -> CatalogMixOut:
    catalog_meta = ((mix.metadata_json or {}).get("catalog") or {})
    yt_meta = catalog_meta.get("youtube") or {}
    sc_meta = catalog_meta.get("soundcloud") or {}
    platforms = []
    if mix.youtube_video_id or mix.youtube_url:
        platforms.append("youtube")
    if mix.soundcloud_track_id or mix.soundcloud_url:
        platforms.append("soundcloud")
    return CatalogMixOut(
        id=mix.id,
        title=mix.title,
        source=mix.source or "pipeline",
        platforms=platforms,
        youtube_video_id=mix.youtube_video_id,
        soundcloud_track_id=mix.soundcloud_track_id,
        youtube_url=mix.youtube_url,
        soundcloud_url=mix.soundcloud_url,
        thumbnail_url=yt_meta.get("thumbnail_url"),
        artwork_url=sc_meta.get("artwork_url"),
        duration_seconds=mix.duration_seconds,
        youtube_published_at=yt_meta.get("published_at"),
        soundcloud_published_at=sc_meta.get("published_at"),
        title_locked=bool(mix.title_locked),
        open_proposals=open_count,
    )


# --- Sync ---


@router.post("/sync", status_code=202)
async def trigger_sync():
    """Kick off a background back-catalog sync (no-op if one is running)."""
    global _sync_task
    from app.services.catalog_sync import run_catalog_sync

    if _sync_task and not _sync_task.done():
        return {"status": "already_running"}
    _sync_task = _spawn("sync", run_catalog_sync())
    return {"status": "started"}


@router.get("/sync/status")
async def sync_status(db: AsyncSession = Depends(get_db)):
    """Whether a sync is running + the last completed run's summary."""
    from app.services.catalog_sync import LAST_SYNC_KEY

    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    last = ((row.settings_json or {}).get(LAST_SYNC_KEY)) if row else None
    return {
        "running": bool(_sync_task and not _sync_task.done()),
        "last_sync": last,
    }


# --- Tracklist backfill ---


@router.post("/backfill-tracklists", status_code=202)
async def trigger_backfill(body: Optional[BackfillBody] = None):
    """Kick off a background tracklist backfill (no-op if one is running).

    Matches local audio files to imported mixes missing tracklists, analyzes
    each match sequentially, and drafts APPROVED description proposals — the
    apply worker is not auto-run.
    """
    global _backfill_task
    from app.services.catalog_backfill import run_backfill

    if _backfill_task and not _backfill_task.done():
        return {"status": "already_running"}
    _backfill_task = _spawn("backfill", run_backfill(body.mix_ids if body else None))
    return {"status": "started"}


@router.get("/backfill-status")
async def backfill_status(db: AsyncSession = Depends(get_db)):
    """Whether a backfill is running + the last completed run's summary."""
    from app.services.catalog_backfill import LAST_BACKFILL_KEY

    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    last = ((row.settings_json or {}).get(LAST_BACKFILL_KEY)) if row else None
    return {
        "running": bool(_backfill_task and not _backfill_task.done()),
        "last_backfill": last,
    }


@router.post("/backfill-cancel")
async def cancel_backfill():
    """Ask the running backfill to stop after the mix it is currently on."""
    from app.services.catalog_backfill import request_cancel

    if not (_backfill_task and not _backfill_task.done()):
        return {"status": "not_running"}
    await request_cancel()
    return {"status": "cancelling"}


# --- Brand thumbnail regeneration ---


@router.post("/regen-thumbnails", status_code=202)
async def trigger_regen_thumbnails(body: RegenThumbsBody):
    """Regenerate brand-design thumbnails/covers in the background.

    ``mix_ids`` is a list of mix ids, ``"all"`` (every cataloged mix), or
    ``"raid-trains"`` (title-matched raid trains). Renders land in the
    ``/output`` art paths and become APPROVED thumbnail proposals — the apply
    worker pushes them (not auto-run). Single-flight.
    """
    global _regen_thumbs_task
    from app.services.catalog_thumbnails import run_regen_thumbnails

    mix_ids = body.mix_ids
    if isinstance(mix_ids, str) and mix_ids not in ("all", "raid-trains"):
        raise HTTPException(
            status_code=422,
            detail="mix_ids must be a list, 'all', or 'raid-trains'",
        )
    if mix_ids is not None and not isinstance(mix_ids, (str, list)):
        raise HTTPException(status_code=422, detail="mix_ids must be a list or string")

    if _regen_thumbs_task and not _regen_thumbs_task.done():
        return {"status": "already_running"}
    _regen_thumbs_task = _spawn("regen_thumbs", run_regen_thumbnails(mix_ids))
    return {"status": "started"}


@router.get("/regen-thumbnails/status")
async def regen_thumbnails_status(db: AsyncSession = Depends(get_db)):
    """Whether a thumbnail regen is running + the last completed run's summary."""
    from app.services.catalog_thumbnails import LAST_REGEN_THUMBS_KEY

    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    last = ((row.settings_json or {}).get(LAST_REGEN_THUMBS_KEY)) if row else None
    return {
        "running": bool(_regen_thumbs_task and not _regen_thumbs_task.done()),
        "last_regen": last,
    }


# --- Playlist organization ---


@router.post("/organize-playlists", status_code=202)
async def trigger_organize_playlists(body: Optional[OrganizePlaylistsBody] = None):
    """Organize cataloged mixes into YT + SC playlists in the background.

    ``mix_ids`` is a list of mix ids or ``"all"`` (default). Classification is
    series-first (Will See Wednesdays / Second Saturdays), then genre buckets;
    missing playlists are created ("Will See | {bucket}") after fuzzy-matching
    the existing ones. YouTube writes share the apply quota budget (pausing +
    resuming across runs); idempotent. Single-flight.
    """
    global _playlists_task
    from app.services.catalog_playlists import run_organize_playlists

    mix_ids = body.mix_ids if body else None
    if isinstance(mix_ids, str) and mix_ids != "all":
        raise HTTPException(status_code=422, detail="mix_ids must be a list or 'all'")
    if mix_ids is not None and not isinstance(mix_ids, (str, list)):
        raise HTTPException(status_code=422, detail="mix_ids must be a list or 'all'")

    if _playlists_task and not _playlists_task.done():
        return {"status": "already_running"}
    _playlists_task = _spawn("playlists", run_organize_playlists(mix_ids))
    return {"status": "started"}


@router.get("/organize-playlists/status")
async def organize_playlists_status(db: AsyncSession = Depends(get_db)):
    """Whether a playlist run is going + the last completed run's summary."""
    from app.services.catalog_playlists import LAST_PLAYLISTS_KEY

    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    last = ((row.settings_json or {}).get(LAST_PLAYLISTS_KEY)) if row else None
    return {
        "running": bool(_playlists_task and not _playlists_task.done()),
        "last_playlists": last,
    }


# --- Unified mix list ---


def _latest_published_expr():
    """SQL expression for a mix's most recent platform publish date.

    Platform dates live inside ``metadata_json.catalog.<platform>.published_at``
    (ISO strings), so they must be pulled out with sqlite ``json_extract`` to
    sort in SQL — sorting must happen before LIMIT/OFFSET for pagination to be
    correct. ``datetime()`` normalizes both the ISO strings (``T`` separator,
    tz offsets) and the ``created_at`` column to comparable UTC strings.
    sqlite's two-arg ``max`` is a scalar max but returns NULL when either arg
    is NULL, hence the coalesce dance; missing platform dates fall back to
    ``created_at``.
    """
    yt = sa_func.datetime(
        sa_func.json_extract(Mix.metadata_json, "$.catalog.youtube.published_at")
    )
    sc = sa_func.json_extract(Mix.metadata_json, "$.catalog.soundcloud.published_at")
    sc = sa_func.datetime(sc)
    latest = sa_func.max(sa_func.coalesce(yt, sc), sa_func.coalesce(sc, yt))
    return sa_func.coalesce(latest, sa_func.datetime(Mix.created_at))


def _catalog_order_by(sort: str):
    """ORDER BY clauses for a catalog sort key (id tie-break keeps pages stable)."""
    published = _latest_published_expr()
    if sort == "oldest":
        return (published.asc(), Mix.id.asc())
    if sort == "title":
        return (sa_func.lower(Mix.title).asc(), Mix.id.asc())
    if sort == "duration":
        # DESC puts NULL durations last in sqlite (NULL sorts smallest).
        return (Mix.duration_seconds.desc(), Mix.id.asc())
    return (published.desc(), Mix.id.asc())  # newest (default)


@router.get("/mixes", response_model=CatalogMixListResponse)
async def list_catalog_mixes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    platform: Optional[Literal["yt-only", "sc-only", "both"]] = Query(None),
    source: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Title substring filter"),
    sort: Literal["newest", "oldest", "title", "duration"] = Query("newest"),
    db: AsyncSession = Depends(get_db),
):
    """Unified paginated catalog listing with platform/source/title filters."""
    has_yt = or_(Mix.youtube_video_id.isnot(None), Mix.youtube_url.isnot(None))
    has_sc = or_(Mix.soundcloud_track_id.isnot(None), Mix.soundcloud_url.isnot(None))
    no_yt = (Mix.youtube_video_id.is_(None)) & (Mix.youtube_url.is_(None))
    no_sc = (Mix.soundcloud_track_id.is_(None)) & (Mix.soundcloud_url.is_(None))

    conditions = []
    if platform == "yt-only":
        conditions.append(has_yt & no_sc)
    elif platform == "sc-only":
        conditions.append(has_sc & no_yt)
    elif platform == "both":
        conditions.append(has_yt & has_sc)
    if source:
        conditions.append(Mix.source == source)
    if q:
        conditions.append(Mix.title.ilike(f"%{q}%"))

    base = select(Mix)
    count_query = select(sa_func.count()).select_from(Mix)
    for cond in conditions:
        base = base.where(cond)
        count_query = count_query.where(cond)

    total = (await db.execute(count_query)).scalar() or 0
    rows = (
        (
            await db.execute(
                base.order_by(*_catalog_order_by(sort))
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    open_counts: Dict[str, int] = {}
    if rows:
        counts_result = await db.execute(
            select(MixProposal.mix_id, sa_func.count())
            .where(
                MixProposal.mix_id.in_([m.id for m in rows]),
                MixProposal.status.in_(OPEN_STATUSES),
            )
            .group_by(MixProposal.mix_id)
        )
        open_counts = {mix_id: n for mix_id, n in counts_result.all()}

    return CatalogMixListResponse(
        items=[_catalog_mix_out(m, open_counts.get(m.id, 0)) for m in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# --- Mix editor ---


@router.put("/mixes/{mix_id}")
async def edit_catalog_mix(
    mix_id: str, body: MixEditBody, db: AsyncSession = Depends(get_db)
):
    """Save mix edits as user proposals (auto-approved; applied only when
    ``apply: true``)."""
    result = await db.execute(select(Mix).where(Mix.id == mix_id))
    mix = result.scalar_one_or_none()
    if not mix:
        raise HTTPException(status_code=404, detail="Mix not found")

    created: List[MixProposal] = []

    def _add(platform: str, field: str, proposed: str, current: Optional[str]):
        proposal = MixProposal(
            mix_id=mix.id,
            platform=platform,
            field=field,
            current_value=current,
            proposed_value=proposed,
            status="approved",
            created_by="user",
        )
        db.add(proposal)
        created.append(proposal)

    for platform, edit in (("youtube", body.youtube), ("soundcloud", body.soundcloud)):
        if not edit:
            continue
        current_title = mix.title_youtube if platform == "youtube" else mix.title
        current_desc = (
            mix.description_youtube if platform == "youtube" else mix.description_soundcloud
        )
        if edit.title is not None:
            _add(platform, "title", edit.title, current_title)
        if edit.description is not None:
            _add(platform, "description", edit.description, current_desc)
        if edit.tags is not None:
            _add(platform, "tags", json.dumps(edit.tags), json.dumps(mix.tags or []))

    if not created:
        raise HTTPException(status_code=400, detail="No edits provided")

    await db.flush()
    out = [_serialize_proposal(p, mix.title) for p in created]
    await db.commit()

    if body.apply:
        global _apply_task
        from app.services.catalog_apply import run_apply

        if not _apply_task or _apply_task.done():
            _apply_task = _spawn("apply", run_apply())

    return {"proposals": out, "applying": body.apply}


@router.post("/mixes/{mix_id}/lock-title")
async def lock_title(
    mix_id: str, body: LockTitleBody, db: AsyncSession = Depends(get_db)
):
    """Lock (or unlock) a mix's title against AI proposals."""
    result = await db.execute(select(Mix).where(Mix.id == mix_id))
    mix = result.scalar_one_or_none()
    if not mix:
        raise HTTPException(status_code=404, detail="Mix not found")
    mix.title_locked = body.locked
    await db.flush()
    return {"id": mix.id, "title_locked": mix.title_locked}


# --- Proposals ---


@router.get("/proposals", response_model=ProposalListResponse)
async def list_proposals(
    status: Optional[str] = Query(None),
    mix_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List proposals (newest first) with optional status/mix filters."""
    base = select(MixProposal, Mix.title).join(Mix, MixProposal.mix_id == Mix.id)
    count_query = select(sa_func.count()).select_from(MixProposal)
    if status:
        base = base.where(MixProposal.status == status)
        count_query = count_query.where(MixProposal.status == status)
    if mix_id:
        base = base.where(MixProposal.mix_id == mix_id)
        count_query = count_query.where(MixProposal.mix_id == mix_id)

    total = (await db.execute(count_query)).scalar() or 0
    rows = (
        await db.execute(
            base.order_by(MixProposal.created_at.desc(), MixProposal.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()

    return ProposalListResponse(
        items=[_serialize_proposal(p, title) for p, title in rows],
        total=total,
    )


@router.post("/mixes/{mix_id}/proposals", response_model=ProposalOut, status_code=201)
async def create_proposal(
    mix_id: str, body: ProposalCreate, db: AsyncSession = Depends(get_db)
):
    """Create a user-authored proposal for a mix."""
    result = await db.execute(select(Mix).where(Mix.id == mix_id))
    mix = result.scalar_one_or_none()
    if not mix:
        raise HTTPException(status_code=404, detail="Mix not found")
    if body.field == "playlist" and body.platform != "youtube":
        raise HTTPException(status_code=400, detail="Playlist proposals are YouTube-only")

    proposal = MixProposal(
        mix_id=mix.id,
        platform=body.platform,
        field=body.field,
        current_value=body.current_value,
        proposed_value=body.proposed_value,
        status=body.status,
        created_by="user",
    )
    db.add(proposal)
    await db.flush()
    return _serialize_proposal(proposal, mix.title)


@router.post("/proposals/{proposal_id}/approve", response_model=ProposalOut)
async def approve_proposal(proposal_id: str, db: AsyncSession = Depends(get_db)):
    """Approve a draft (or retry a failed) proposal for the apply queue."""
    proposal = await _get_proposal(db, proposal_id)
    if proposal.status not in ("draft", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve a proposal in status '{proposal.status}'",
        )
    proposal.status = "approved"
    proposal.error = None
    await db.flush()
    await db.refresh(proposal)  # updated_at is a server-side onupdate default
    return _serialize_proposal(proposal)


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalOut)
async def reject_proposal(proposal_id: str, db: AsyncSession = Depends(get_db)):
    """Reject a draft/approved proposal."""
    proposal = await _get_proposal(db, proposal_id)
    if proposal.status not in ("draft", "approved", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reject a proposal in status '{proposal.status}'",
        )
    proposal.status = "rejected"
    await db.flush()
    await db.refresh(proposal)  # updated_at is a server-side onupdate default
    return _serialize_proposal(proposal)


@router.post("/proposals/approve-bulk")
async def approve_bulk(body: BulkApproveBody, db: AsyncSession = Depends(get_db)):
    """Approve many draft proposals at once, then auto-run the apply worker."""
    result = await db.execute(
        select(MixProposal).where(MixProposal.id.in_(body.ids))
    )
    approved = 0
    for proposal in result.scalars().all():
        if proposal.status in ("draft", "failed"):
            proposal.status = "approved"
            proposal.error = None
            approved += 1
    await db.commit()

    global _apply_task
    from app.services.catalog_apply import run_apply

    if approved and (not _apply_task or _apply_task.done()):
        _apply_task = _spawn("apply", run_apply())

    return {"approved": approved, "applying": approved > 0}


# --- Apply + improve triggers ---


@router.post("/apply", status_code=202)
async def trigger_apply():
    """Run the apply worker in the background (no-op if already running)."""
    global _apply_task
    from app.services.catalog_apply import run_apply

    if _apply_task and not _apply_task.done():
        return {"status": "already_running"}
    _apply_task = _spawn("apply", run_apply())
    return {"status": "started"}


@router.post("/improve", status_code=202)
async def trigger_improve(body: ImproveBody):
    """Run AI improve (title triage + draft proposals) in the background."""
    global _improve_task
    from app.services.catalog_improve import run_improve

    if _improve_task and not _improve_task.done():
        return {"status": "already_running"}
    mix_ids = body.mix_ids
    if isinstance(mix_ids, str) and mix_ids != "all_generic":
        raise HTTPException(status_code=422, detail="mix_ids must be a list or 'all_generic'")
    _improve_task = _spawn("improve", run_improve(mix_ids))
    return {"status": "started"}
