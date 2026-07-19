"""Apply worker for approved mix proposals.

Consumes ``approved`` :class:`MixProposal` rows sequentially (oldest first) and
pushes each to the platform APIs:

* YouTube — ``videos.update`` (snippet fetched first, only the target field
  mutated; title<=100 / description<=5000), ``thumbnails.set`` for thumbnail
  proposals (proposed_value is a file path), ``playlistItems`` insert/delete
  for playlist membership changes.
* SoundCloud — ``PUT /tracks/:id`` with ``track[title]`` /
  ``track[description]`` / ``track[tag_list]``, artwork via
  ``track[artwork_data]`` multipart.

Quota: YouTube writes cost 50 units each, tracked per-day in
``AppSettings.settings_json["catalog_yt_quota"] = {"date", "used"}`` against
``settings.YOUTUBE_DAILY_QUOTA_BUDGET``. When the next write would exceed the
budget, remaining proposals stay ``approved`` (queued), an activity warning is
emitted, and the run stops — the next run (or next day) resumes them.

Status transitions: approved -> applying -> applied | failed (error captured).
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import AppSettings, Mix, MixProposal
from app.services import activity_log
from app.services.soundcloud_uploader import SoundCloudUploader
from app.services.youtube_uploader import YouTubeUploader

logger = logging.getLogger(__name__)

YT_WRITE_COST = 50
QUOTA_KEY = "catalog_yt_quota"


# Factory hooks (monkeypatchable in tests).
def get_youtube_uploader(db_settings_json: Optional[Dict[str, Any]]) -> YouTubeUploader:
    return YouTubeUploader(db_settings_json)


def get_soundcloud_uploader(
    db_settings_json: Optional[Dict[str, Any]],
    on_tokens_refreshed: Optional[Any] = None,
) -> SoundCloudUploader:
    return SoundCloudUploader(db_settings_json, on_tokens_refreshed)


def _decode_structured(value: Optional[str]) -> Any:
    """Decode a JSON-encoded proposed_value (tags/playlist), or None."""
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _proposal_platforms(proposal: MixProposal) -> List[str]:
    if proposal.platform == "both":
        return ["youtube", "soundcloud"]
    return [proposal.platform]


def _yt_cost(proposal: MixProposal) -> int:
    """YouTube quota units this proposal will consume."""
    platforms = _proposal_platforms(proposal)
    if "youtube" not in platforms:
        return 0
    if proposal.field == "playlist":
        value = _decode_structured(proposal.proposed_value) or {}
        ops = len(value.get("add", []) or []) + len(value.get("remove", []) or [])
        return YT_WRITE_COST * max(ops, 1)
    return YT_WRITE_COST


async def _apply_to_youtube(
    uploader: YouTubeUploader, proposal: MixProposal, mix: Mix
) -> None:
    if not mix.youtube_video_id:
        raise RuntimeError("Mix has no youtube_video_id")
    vid = mix.youtube_video_id

    if proposal.field == "title":
        await uploader.update_video_fields(vid, title=proposal.proposed_value)
        mix.title_youtube = proposal.proposed_value
    elif proposal.field == "description":
        await uploader.update_video_fields(vid, description=proposal.proposed_value)
        mix.description_youtube = proposal.proposed_value
    elif proposal.field == "tags":
        tags = _decode_structured(proposal.proposed_value) or []
        await uploader.update_video_fields(vid, tags=tags)
        mix.tags = tags
    elif proposal.field == "thumbnail":
        await uploader.set_thumbnail(vid, proposal.proposed_value)
        mix.thumbnail_path = proposal.proposed_value
    elif proposal.field == "playlist":
        value = _decode_structured(proposal.proposed_value) or {}
        for playlist_id in value.get("add", []) or []:
            await uploader.add_video_to_playlist(playlist_id, vid)
            mix.youtube_playlist_id = playlist_id
        for playlist_id in value.get("remove", []) or []:
            await uploader.remove_video_from_playlist(playlist_id, vid)
            if mix.youtube_playlist_id == playlist_id:
                mix.youtube_playlist_id = None
    else:
        raise RuntimeError(f"Unknown proposal field: {proposal.field}")


async def _apply_to_soundcloud(
    uploader: SoundCloudUploader, proposal: MixProposal, mix: Mix
) -> None:
    if not mix.soundcloud_track_id:
        raise RuntimeError("Mix has no soundcloud_track_id")
    tid = mix.soundcloud_track_id

    if proposal.field == "title":
        await uploader.update_track_fields(tid, title=proposal.proposed_value)
        mix.title = proposal.proposed_value
    elif proposal.field == "description":
        await uploader.update_track_fields(tid, description=proposal.proposed_value)
        mix.description_soundcloud = proposal.proposed_value
    elif proposal.field == "tags":
        tags = _decode_structured(proposal.proposed_value) or []
        await uploader.update_track_fields(tid, tags=tags)
        mix.tags = tags
    elif proposal.field == "thumbnail":
        await uploader.update_track_fields(tid, artwork_path=proposal.proposed_value)
        mix.cover_art_path = proposal.proposed_value
    elif proposal.field == "playlist":
        raise RuntimeError("Playlist proposals are YouTube-only")
    else:
        raise RuntimeError(f"Unknown proposal field: {proposal.field}")


async def run_apply() -> Dict[str, Any]:
    """Apply all approved proposals sequentially. Returns a run summary."""
    summary: Dict[str, Any] = {"applied": 0, "failed": 0, "queued": 0, "paused": False}
    budget = settings.YOUTUBE_DAILY_QUOTA_BUDGET
    today = date.today().isoformat()

    async with async_session_factory() as session:
        settings_result = await session.execute(
            select(AppSettings).where(AppSettings.id == 1)
        )
        settings_row = settings_result.scalar_one_or_none()
        if not settings_row:
            settings_row = AppSettings(id=1)
            session.add(settings_row)
            await session.flush()
        sj = dict(settings_row.settings_json or {})
        quota = sj.get(QUOTA_KEY) or {}
        used = int(quota.get("used", 0)) if quota.get("date") == today else 0

        async def _persist_sc_tokens(access_token: str, refresh_token: str) -> None:
            merged = dict(settings_row.settings_json or {})
            merged["soundcloud_access_token"] = access_token
            merged["soundcloud_refresh_token"] = refresh_token
            settings_row.settings_json = merged

        yt_uploader: Optional[YouTubeUploader] = None
        sc_uploader: Optional[SoundCloudUploader] = None

        result = await session.execute(
            select(MixProposal)
            .where(MixProposal.status == "approved")
            .order_by(MixProposal.created_at, MixProposal.id)
        )
        proposals = list(result.scalars().all())

        for proposal in proposals:
            cost = _yt_cost(proposal)
            if cost and used + cost > budget:
                remaining = [p for p in proposals if p.status == "approved"]
                summary["paused"] = True
                summary["queued"] = len(remaining)
                await activity_log.warn(
                    "catalog_apply",
                    (
                        f"YouTube quota budget reached ({used}/{budget} units used); "
                        f"{len(remaining)} approved proposals left queued for the next run."
                    ),
                    context={"used": used, "budget": budget},
                )
                break

            mix = (
                await session.execute(select(Mix).where(Mix.id == proposal.mix_id))
            ).scalar_one_or_none()
            if mix is None:
                proposal.status = "failed"
                proposal.error = "Mix no longer exists"
                summary["failed"] += 1
                await session.commit()
                continue

            proposal.status = "applying"
            await session.commit()

            # A YouTube write consumes quota whether or not it succeeds.
            used += cost
            try:
                for platform in _proposal_platforms(proposal):
                    if platform == "youtube":
                        if yt_uploader is None:
                            yt_uploader = get_youtube_uploader(sj)
                        await _apply_to_youtube(yt_uploader, proposal, mix)
                    else:
                        if sc_uploader is None:
                            sc_uploader = get_soundcloud_uploader(
                                sj, _persist_sc_tokens
                            )
                        await _apply_to_soundcloud(sc_uploader, proposal, mix)
                proposal.status = "applied"
                proposal.applied_at = datetime.now(timezone.utc)
                proposal.error = None
                summary["applied"] += 1
            except Exception as exc:
                logger.exception(
                    "Failed to apply proposal %s (%s/%s)",
                    proposal.id, proposal.platform, proposal.field,
                )
                proposal.status = "failed"
                proposal.error = str(exc)[:2000]
                summary["failed"] += 1
                await activity_log.error(
                    "catalog_apply",
                    f"Proposal {proposal.field} for mix {proposal.mix_id} failed: {exc}",
                    mix_id=proposal.mix_id,
                    platform=proposal.platform,
                )
            await session.commit()

        # Persist quota usage.
        merged = dict(settings_row.settings_json or {})
        merged[QUOTA_KEY] = {"date": today, "used": used}
        settings_row.settings_json = merged
        await session.commit()

    if summary["applied"] or summary["failed"]:
        await activity_log.info(
            "catalog_apply",
            (
                f"Apply run finished: {summary['applied']} applied, "
                f"{summary['failed']} failed, {summary['queued']} queued."
            ),
            context=summary,
        )
    return summary
