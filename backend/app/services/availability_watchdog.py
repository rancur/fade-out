"""Watchdog for mixes that published but are not actually watchable.

A mix went up: both legs published, verify_youtube
saw a public video, the pipeline completed. Fifteen minutes later a Content ID
claim blocked the video in the US. Nothing in the system re-looked, so the
first anyone heard of it was the owner noticing the mix was missing from
YouTube.

verify_youtube cannot catch this — it runs seconds after the upload, before
Content ID has finished. So this re-checks recently published mixes on a timer
and reports any whose YouTube video is gone, or blocked in the home region
(``YOUTUBE_HOME_REGION``).

Like the stuck-mix watchdog, deduplication lives in the activity log so a
container restart cannot turn one blocked video into a repeating alert, and a
check that could not reach YouTube reports ``unknown`` — never an empty
all-clear.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import AppSettings, Mix

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 6 * 3600
# Claims can land days after upload, but rarely weeks. One videos.list call
# covers 50 ids for 1 quota unit, so the window is cheap either way.
LOOKBACK_DAYS = 30
REALERT_AFTER_HOURS = 48

EVENT = "mix_unavailable"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AvailabilityWatchdog:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_result: Dict[str, Any] = {
            "state": "unknown",
            "detail": "never run",
            "unavailable": [],
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Availability watchdog started (every %ds)", CHECK_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def last_result(self) -> Dict[str, Any]:
        return self._last_result

    async def _loop(self) -> None:
        await asyncio.sleep(120)
        while self._running:
            try:
                await self.check(alert=True)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Availability check failed")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    # ------------------------------------------------------------------

    async def _recent_youtube_mixes(self) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        async with async_session_factory() as session:
            app_settings = await session.get(AppSettings, 1)
            rows = (
                await session.execute(
                    select(Mix).where(
                        Mix.source == "pipeline",
                        Mix.youtube_video_id.is_not(None),
                    )
                )
            ).scalars().all()
            mixes = [
                {
                    "id": m.id,
                    "title": m.title,
                    "video_id": m.youtube_video_id,
                    "youtube_url": m.youtube_url,
                }
                for m in rows
                if (_aware(m.pipeline_completed_at) or _aware(m.created_at) or cutoff) >= cutoff
            ]
        sj = (app_settings.settings_json or {}) if app_settings else {}
        return mixes, sj

    async def check(self, alert: bool = False) -> Dict[str, Any]:
        """Re-check recently published YouTube videos for removal or a home-region block."""
        from app.services.youtube_uploader import YouTubeUploader, region_block_reason

        region = (settings.YOUTUBE_HOME_REGION or "").upper()
        now = datetime.now(timezone.utc)
        mixes, sj = await self._recent_youtube_mixes()

        try:
            found = await YouTubeUploader(db_settings_json=sj).get_videos(
                [m["video_id"] for m in mixes]
            )
        except Exception as exc:
            # Could not look: that is not the same as "nothing is blocked".
            self._last_result = {
                "state": "unknown",
                "checked_at": now.isoformat(),
                "detail": f"YouTube lookup failed: {exc}",
                "unavailable": [],
            }
            return self._last_result

        unavailable: List[Dict[str, Any]] = []
        for mix in mixes:
            item = found.get(mix["video_id"])
            if item is None:
                reason = "not returned by the YouTube API (deleted or removed)"
            else:
                reason = region_block_reason(
                    item.get("contentDetails", {}).get("regionRestriction"), region
                )
                if reason is None and item.get("status", {}).get("uploadStatus") in (
                    "rejected", "failed", "deleted",
                ):
                    status = item["status"]
                    reason = (
                        f"uploadStatus={status['uploadStatus']} "
                        f"({status.get('rejectionReason') or status.get('failureReason') or 'no reason given'})"
                    )
            if reason:
                unavailable.append({**mix, "platform": "youtube", "reason": reason})

        self._last_result = {
            "state": "ok",
            "checked_at": now.isoformat(),
            "home_region": region,
            "checked_count": len(mixes),
            "unavailable_count": len(unavailable),
            "unavailable": unavailable,
        }

        if alert:
            for entry in unavailable:
                await self._alert(entry)

        return self._last_result

    async def _alert(self, entry: Dict[str, Any]) -> None:
        from app.services import activity_log
        from app.services.notification_service import get_notification_service

        try:
            since = datetime.now(timezone.utc) - timedelta(hours=REALERT_AFTER_HOURS)
            _items, total = await activity_log.query(
                limit=1, mix_id=entry["id"], event=EVENT, since=since,
            )
            if total:
                logger.debug("Unavailable-mix alert for %s already raised recently", entry["id"])
                return
        except Exception:  # pragma: no cover - defensive
            pass

        message = (
            f"Mix \"{entry['title']}\" is published to YouTube but {entry['reason']}: "
            f"{entry['youtube_url']}. Usually a Content ID claim — review it in "
            f"YouTube Studio (trim/mute the claimed segment, or dispute)."
        )
        await activity_log.warn(EVENT, message, mix_id=entry["id"], context=entry)
        try:
            await get_notification_service().notify(
                EVENT,
                mix_id=entry["id"],
                title=f"Mix not watchable on YouTube: {entry['title']}",
                message=message,
                data=entry,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Unavailable-mix notification failed for %s", entry["id"])


_watchdog: Optional[AvailabilityWatchdog] = None


def get_availability_watchdog() -> AvailabilityWatchdog:
    global _watchdog
    if _watchdog is None:
        _watchdog = AvailabilityWatchdog()
    return _watchdog
