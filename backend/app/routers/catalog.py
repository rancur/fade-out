"""Back-catalog management endpoints: sync, unified mix list, proposals, apply.

Background jobs (sync / apply / improve) follow the reread-tracklist pattern:
the endpoint returns 202 immediately and the work runs in an asyncio task with
its own session; progress lands in the activity log and the sync summary in
``AppSettings.settings_json``.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
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
_retag_task: Optional[asyncio.Task] = None

_RETAG_SUMMARY_STATUSES = ("ok", "skipped", "disabled", "failed", "dry_run", "unknown")

# Key under AppSettings.settings_json where the resume cursor is persisted,
# following the same precedent as catalog_sync's LAST_SYNC_KEY.
RETAG_PROGRESS_KEY = "retag_progress"


def _empty_retag_summary() -> Dict[str, int]:
    return {status: 0 for status in _RETAG_SUMMARY_STATUSES}


# Live progress for the retag backfill, polled by GET /retag/status. Reset at
# the start of each run by ``_run_retag``. ``summary`` tallies every rename
# AND tag outcome across the run (two entries per mix -- one derived from the
# renamer's outcome via ``_rename_outcome_status``, since it has no top-level
# ``status`` key of its own; one read straight off the tagger's), so a run in
# which every mix came back "disabled" (the setting is off) cannot be
# mistaken for one that actually did the work -- ``processed == total`` alone
# can't tell the two apart. Any status this summary doesn't recognise lands
# in "unknown" rather than being dropped, so ``sum(summary.values())`` always
# equals ``2 * processed`` and a silently-changed outcome shape shows up as a
# nonzero "unknown" count instead of quietly vanishing.
_retag_state: Dict[str, Any] = {
    "running": False,
    "processed": 0,
    "total": 0,
    "results": [],
    "summary": _empty_retag_summary(),
}


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
    # Uniqueness engine (default ON): per-mix LLM hook + hash-varied scene,
    # enforced unique via the used_creative registry. False = legacy fixed
    # per-genre motif hook/scene.
    force_unique: bool = True


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

# Post-boot settle delay before an auto-resumed backfill starts, so the file
# watchers, DB init, and the rest of startup are done competing for sqlite.
BACKFILL_AUTO_RESUME_DELAY_SECONDS = 60.0


async def kickoff_backfill_auto_resume(
    delay_seconds: float = BACKFILL_AUTO_RESUME_DELAY_SECONDS,
) -> bool:
    """Boot self-heal: restart a backfill the last shutdown killed mid-run.

    Called from the app lifespan as a background task. Fires only when the
    ``backfill_auto_resume`` setting is on AND the persisted last-run summary
    never reached completion (status ``running``/``cancelled`` — see
    ``catalog_backfill.last_run_incomplete``). The restart goes through the
    same ``_backfill_task`` handle as the manual endpoint, so single-flight
    and ``/backfill-status`` reporting keep working. Returns True when a
    resume was started.
    """
    global _backfill_task
    from app.services import activity_log, app_config
    from app.services.catalog_backfill import last_run_incomplete, run_backfill

    if not bool(await app_config.resolve("backfill_auto_resume")):
        return False
    if not await last_run_incomplete():
        return False
    await asyncio.sleep(delay_seconds)
    if _backfill_task and not _backfill_task.done():
        return False  # manually (re)triggered during the settle delay
    logger.info("Auto-resuming interrupted tracklist backfill")
    await activity_log.info(
        "catalog_backfill",
        "Last tracklist backfill never completed (restart/deploy) — "
        "auto-resuming.",
    )
    _backfill_task = _spawn("backfill", run_backfill())
    return True


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
    ``"raid-trains"`` (title-matched raid trains). ``force_unique`` (default
    ``true``) routes every render through the uniqueness engine — a per-mix
    hook + varied scene enforced unique by the ``used_creative`` registry;
    ``false`` restores the legacy fixed per-genre motif look. Renders land in
    the ``/output`` art paths and become APPROVED thumbnail proposals — the
    apply worker pushes them (not auto-run). Single-flight.
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
    _regen_thumbs_task = _spawn(
        "regen_thumbs", run_regen_thumbnails(mix_ids, force_unique=body.force_unique)
    )
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


# --- Uniqueness registry observability ---


@router.get("/uniqueness/stats")
async def uniqueness_stats(db: AsyncSession = Depends(get_db)):
    """Per-kind claim counts + the most recent claims from the uniqueness
    registry (titles / thumbnail hooks / scene descriptors)."""
    from app.services import uniqueness

    return await uniqueness.stats(db)


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


@router.post("/mixes/{mix_id}/rename-source")
async def rename_mix_source(
    mix_id: str,
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Rename this mix's source files on disk to match its current title.

    The automatic hooks only fire when a run completes or a title proposal is
    applied, so this is how an already-titled back-catalog mix gets the new
    naming. ``dry_run=true`` reports the plan without touching anything, and
    works even while the feature is switched off -- the preview you want before
    letting fade-out rename recordings on the NAS.
    """
    result = await db.execute(select(Mix).where(Mix.id == mix_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Mix not found")

    from app.services import source_renamer

    return await source_renamer.rename_sources_for_mix(
        mix_id, reason="manual", dry_run=dry_run
    )


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


# --- Source file retag backfill ---
#
# Retroactively applies the same rename-then-tag treatment the pipeline now
# runs automatically at completion to the back-catalog of already-published
# mixes. The real run rewrites roughly 498 GB of irreplaceable multi-gigabyte
# FLAC recordings in place, so ``dry_run`` defaults to True -- a bare POST
# must be the safe, report-only form, never the destructive one.


def _rename_outcome_status(outcome: Dict[str, Any]) -> str:
    """Derive a tag-style status label from ``rename_sources_for_mix``'s
    actual return shape.

    Unlike ``tag_sources_for_mix``, the renamer has no top-level ``status``
    key at all -- its result is ``{mix_id, reason, dry_run, renamed, skipped,
    errors, ...}`` (see ``source_renamer.rename_sources_for_mix``). Reading
    ``outcome.get("status")`` on it is always ``None``, which used to make
    every rename outcome fall out of the summary silently. This derives an
    equivalent status from the fields the renamer actually populates:

    - any ``errors`` entry        -> "failed"
    - a ``skipped`` entry whose ``reason`` is ``"disabled"`` -> "disabled"
    - a non-empty ``renamed`` list -> "ok"
    - otherwise                   -> "skipped" (covers dry runs, no-title /
      missing-mix skips, and refusals that aren't the disabled-setting case)
    """
    if outcome.get("errors"):
        return "failed"
    if any(
        isinstance(entry, dict) and entry.get("reason") == "disabled"
        for entry in (outcome.get("skipped") or [])
    ):
        return "disabled"
    if outcome.get("renamed"):
        return "ok"
    return "skipped"


def _retag_candidates_stmt(offset: int, limit: Optional[int]):
    """Build the retag candidate-selection query.

    Shared verbatim by the pre-flight count in ``POST /retag`` and the actual
    selection in ``_run_retag`` so the two can never drift apart -- same
    WHERE, same ORDER BY, same OFFSET/LIMIT. The ordering is a deterministic
    total order: ``Mix.created_at`` alone is not guaranteed unique (mixes
    inserted in the same tick, e.g. via the back-catalog sync, tie), so
    ``Mix.id`` breaks ties. Without a total order, ``offset``-based paging
    across two calls (or a resume midway through) is not guaranteed to
    select disjoint rows.
    """
    stmt = (
        select(Mix.id)
        .where(Mix.pipeline_status == "completed")
        .order_by(Mix.created_at, Mix.id)
        .offset(offset)
    )
    if limit:
        stmt = stmt.limit(limit)
    return stmt


async def _resume_offset(session: AsyncSession) -> int:
    """Translate the persisted ``retag_progress`` cursor into an offset.

    With no saved cursor -- the very first run, or a prior run whose last
    mix has since been deleted from the candidate set -- resumption starts
    from the beginning. This must never raise: ``resume=true`` against a
    clean slate is exactly as safe as a bare run.
    """
    result = await session.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    progress = ((row.settings_json or {}).get(RETAG_PROGRESS_KEY)) if row else None
    last_mix_id = (progress or {}).get("last_mix_id")
    if not last_mix_id:
        return 0

    ordered = await session.execute(
        select(Mix.id)
        .where(Mix.pipeline_status == "completed")
        .order_by(Mix.created_at, Mix.id)
    )
    ordered_ids = [row[0] for row in ordered.all()]
    try:
        return ordered_ids.index(last_mix_id) + 1
    except ValueError:
        return 0


async def _persist_retag_progress(
    mix_id: str, processed: int, total: int, dry_run: bool
) -> None:
    """Write the resume cursor after a single mix, committing immediately.

    Called after EVERY mix (not just at the end of the run) so a ``kill -9``
    mid-run still leaves a usable checkpoint -- persisting only on
    completion would defeat the entire purpose of a resumable backfill.
    Best-effort and wrapped like every other persistence call in this
    module: a failure to save the cursor must not take down a run that is
    otherwise successfully touching irreplaceable files.
    """
    from app.database import async_session_factory

    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(AppSettings).where(AppSettings.id == 1)
            )
            row = result.scalar_one_or_none()
            if not row:
                row = AppSettings(id=1)
                session.add(row)
            merged = dict(row.settings_json or {})
            merged[RETAG_PROGRESS_KEY] = {
                "last_mix_id": mix_id,
                "processed": processed,
                "total": total,
                "dry_run": dry_run,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            row.settings_json = merged
            await session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to persist retag progress cursor")


async def _emit_retag_activity(
    level: str, event: str, message: str, **kwargs: Any
) -> None:
    """Best-effort activity-log emit for the retag backfill.

    Durable per-mix outcomes for a run that rewrites ~498 GB of irreplaceable
    FLAC masters must not disappear when the process restarts -- that's the
    entire point of Task 3. But losing this log line is a far better outcome
    than losing the run itself, so this is wrapped exactly like every other
    best-effort activity emit in this codebase (see ``handlers._emit_activity``,
    ``auth._emit_auth``): a broken activity log must never abort the backfill.
    ``activity_log.log`` already never raises by its own contract, but this
    wrapper does not rely on that -- it catches independently, so a future
    change to ``activity_log`` (or a test double that raises) can't take the
    backfill down with it.
    """
    try:
        from app.services import activity_log

        await activity_log.log(level, event, message, **kwargs)
    except Exception:  # pragma: no cover - defensive
        logger.debug("retag activity emit failed for %s", event, exc_info=True)


async def _sweep_retag_staging() -> Dict[str, Any]:
    """Reclaim orphaned ``.fadeout-tagging`` staging copies before a run
    starts touching any real files.

    One staging directory can exist directly under each of
    ``source_renamer.allowed_roots()`` (``write_tagged_copy`` always stages
    directly alongside the source file, and the watch folders themselves are
    flat -- see ``source_tagger``'s module docstring), so this sweeps each
    root's staging directory in turn. ``source_tagger.sweep_staging`` does
    real filesystem I/O (scandir/stat/remove over however many orphans have
    accumulated), so it runs off the event loop via ``asyncio.to_thread``
    rather than blocking it.

    Best-effort like everything else in this module: a sweep failure must
    never stop the backfill it is meant to make safer to run unattended.
    ``sweep_staging`` itself never raises, but this wrapper does not rely on
    that holding forever -- it catches independently, mirroring
    ``_emit_retag_activity``.
    """
    from app.services import source_renamer, source_tagger

    totals: Dict[str, Any] = {"removed_count": 0, "reclaimed_bytes": 0, "roots": []}
    try:
        for watch_root in source_renamer.allowed_roots():
            staging_root = os.path.join(watch_root, source_tagger.TEMP_DIRNAME)
            outcome = await asyncio.to_thread(
                source_tagger.sweep_staging, staging_root
            )
            totals["roots"].append(outcome)
            totals["removed_count"] += outcome.get("removed_count", 0)
            totals["reclaimed_bytes"] += outcome.get("reclaimed_bytes", 0)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Sweeping retag staging directories failed")
    return totals


async def _run_retag(
    dry_run: bool, limit: Optional[int], offset: int = 0, resume: bool = False
) -> None:
    """Rename then tag source files for each completed-pipeline mix.

    ``source_renamer.rename_sources_for_mix`` / ``source_tagger.tag_sources_for_mix``
    are both best-effort by contract (never raise), so one mix's failure never
    aborts the run. Rename is called before tag for each mix, matching the
    order the pipeline itself uses at completion. Progress is published to the
    module-level ``_retag_state`` dict polled by ``GET /retag/status``, and a
    resume cursor is persisted to ``AppSettings.settings_json`` after each
    mix (see ``_persist_retag_progress``).

    Durable outcomes also go to the activity log (``app.services.activity_log``),
    which is what survives a process restart when ``_retag_state`` does not:
    one ``retag_run_started`` event with the run parameters and candidate
    total, exactly one ``retag_mix_processed`` event per mix carrying its
    ``mix_id`` and both the rename and tag status (at ``error`` level when
    either failed, ``info`` otherwise), and one ``retag_run_completed`` event
    with the final summary. Every emit goes through ``_emit_retag_activity``,
    which is best-effort -- an activity-log failure can never abort this run.

    Before any of that, orphaned ``.fadeout-tagging`` staging copies from a
    previous run that died mid-flight are reclaimed (see
    ``_sweep_retag_staging`` / ``source_tagger.sweep_staging``) -- otherwise
    a repeatedly-interrupted backfill just keeps accumulating multi-gigabyte
    orphans that nothing else will ever reclaim.
    """
    from app.database import async_session_factory
    from app.services import source_renamer, source_tagger

    try:
        sweep = await _sweep_retag_staging()
        await _emit_retag_activity(
            "info",
            "retag_staging_swept",
            f"Reclaimed {sweep['removed_count']} orphaned staging file(s) "
            f"({sweep['reclaimed_bytes']} bytes) before starting the retag run.",
            context=sweep,
        )

        async with async_session_factory() as session:
            stmt = _retag_candidates_stmt(offset, limit)
            mix_ids = [row[0] for row in (await session.execute(stmt)).all()]

        _retag_state["running"] = True
        _retag_state["processed"] = 0
        _retag_state["total"] = len(mix_ids)
        _retag_state["results"] = []
        summary = _empty_retag_summary()
        _retag_state["summary"] = summary

        await _emit_retag_activity(
            "info",
            "retag_run_started",
            f"Retag backfill started: {len(mix_ids)} candidate mix(es) "
            f"(dry_run={dry_run}, limit={limit}, offset={offset}, resume={resume}).",
            context={
                "dry_run": dry_run,
                "limit": limit,
                "offset": offset,
                "resume": resume,
                "candidates": len(mix_ids),
            },
        )

        for mix_id in mix_ids:
            rename = await source_renamer.rename_sources_for_mix(
                mix_id, reason="retag_backfill", dry_run=dry_run
            )
            tag = await source_tagger.tag_sources_for_mix(
                mix_id, reason="retag_backfill", dry_run=dry_run
            )
            _retag_state["results"].append(
                {"mix_id": mix_id, "rename": rename, "tag": tag}
            )
            rename_status = _rename_outcome_status(rename)
            tag_status = tag.get("status")
            for status in (rename_status, tag_status):
                summary[status if status in summary else "unknown"] += 1

            mix_level = "error" if "failed" in (rename_status, tag_status) else "info"
            await _emit_retag_activity(
                mix_level,
                "retag_mix_processed",
                f"Retag mix {mix_id}: rename={rename_status}, tag={tag_status}.",
                mix_id=mix_id,
                context={"rename_status": rename_status, "tag_status": tag_status},
            )

            _retag_state["processed"] += 1
            await _persist_retag_progress(
                mix_id, _retag_state["processed"], _retag_state["total"], dry_run
            )

        await _emit_retag_activity(
            "warn" if summary.get("failed") else "info",
            "retag_run_completed",
            f"Retag backfill completed: {_retag_state['processed']}/"
            f"{_retag_state['total']} mixes processed.",
            context={
                "summary": dict(summary),
                "processed": _retag_state["processed"],
                "total": _retag_state["total"],
                "dry_run": dry_run,
            },
        )
    finally:
        _retag_state["running"] = False


@router.post("/retag", status_code=202)
async def catalog_retag(
    dry_run: bool = Query(default=True),
    limit: Optional[int] = Query(default=None),
    offset: int = Query(default=0),
    resume: bool = Query(default=False),
):
    """Rename and tag the source files of already-published (completed) mixes.

    Defaults to ``dry_run=True``: a bare, un-parameterised POST is the safe,
    report-only form. The real run rewrites multi-gigabyte FLACs across
    roughly 498 GB of irreplaceable recordings, so the destructive form
    (``dry_run=false``) is opt-in only, never the default. Only mixes with
    ``pipeline_status == "completed"`` are candidates; rename runs before tag
    for each one. Single-flight: a run already in progress returns 409
    instead of starting a second concurrent pass.

    ``limit``/``offset`` genuinely paginate now (ordered by ``created_at``,
    ``id``): a batch-two call with ``offset=limit`` picks up exactly where
    batch one left off, never re-selecting the same rows. ``resume=true``
    ignores ``offset`` and instead continues after the mix the last run
    persisted as its cursor (``AppSettings.settings_json["retag_progress"]``);
    with no saved cursor it starts from the beginning rather than erroring.
    """
    global _retag_task
    from app.database import async_session_factory

    async with async_session_factory() as session:
        # Resolve the resume cursor (if any) into a concrete offset in the
        # same session/await window as the pre-flight count below, well
        # before the single-flight guard. Mirrors the id-selection in
        # ``_run_retag`` exactly (same statement builder), so this
        # pre-flight number is the actual count of mixes the run is about
        # to touch -- a plain COUNT(*) ignoring limit/offset would
        # over-report.
        effective_offset = await _resume_offset(session) if resume else offset
        candidate_stmt = _retag_candidates_stmt(effective_offset, limit)
        candidates = len((await session.execute(candidate_stmt)).all())

    # No await between this check and the assignment below: asyncio is
    # single-threaded, so an uninterrupted block is atomic. A yield here
    # would let two concurrent POSTs both pass the guard and start two
    # overlapping passes over the same multi-gigabyte files. The resume
    # cursor lookup above happens strictly BEFORE this window, not inside
    # it, so it cannot reintroduce that race.
    if _retag_task and not _retag_task.done():
        raise HTTPException(
            status_code=409, detail="A retag run is already in progress."
        )

    _retag_task = _spawn(
        "retag", _run_retag(dry_run, limit, effective_offset, resume)
    )
    return {
        "started": True,
        "dry_run": dry_run,
        "candidates": int(candidates),
        "offset": effective_offset,
    }


@router.get("/retag/status")
async def catalog_retag_status():
    """Whether a retag run is in progress + its live processed/total/results."""
    return dict(_retag_state)
