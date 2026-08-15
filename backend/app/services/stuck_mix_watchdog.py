"""Watchdog for mixes that were ingested but never published.

"Sandstorm Frequency" was ingested on 2026-08-12, failed to publish, and then
sat there. Nothing in the system was responsible for noticing; the way it
surfaced was a human wondering where the mix had got to.

This closes that: any mix that has been in the system past a reasonable window
without reaching a platform is reported. It reads the same DB the pipeline
writes, so it cannot be fooled by an in-memory notion of what is running — a
pipeline that died mid-flight still leaves a mix that is old and unpublished,
and that is exactly what this looks for.

Deduplication is done against the activity log rather than in memory, so a
container restart cannot turn one stuck mix into a repeating alert.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select

from app.database import async_session_factory
from app.models import Mix
from app.services import app_config

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1800  # 30 minutes

# Windows, in hours, before a mix counts as stuck.
DEFAULT_UNPUBLISHED_HOURS = 6
# A draft is deliberately waiting on a human, so it gets a much longer leash —
# but not an infinite one. A draft nobody ever came back to is still a mix
# that silently never shipped.
DEFAULT_DRAFT_HOURS = 72

# Don't re-alert about the same mix more often than this.
REALERT_AFTER_HOURS = 24

EVENT = "stuck_mix"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class StuckMixWatchdog:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_result: Dict[str, Any] = {
            "state": "unknown",
            "detail": "never run",
            "stuck": [],
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Stuck-mix watchdog started (every %ds)", CHECK_INTERVAL_SECONDS)

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
        # Let ingest/boot settle before the first sweep.
        await asyncio.sleep(60)
        while self._running:
            try:
                await self.check(alert=True)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Stuck-mix check failed")
                self._last_result = {
                    "state": "unknown",
                    "detail": "last check raised",
                    "stuck": [],
                }
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    # ------------------------------------------------------------------

    async def _window_hours(self) -> tuple[int, int]:
        """Alert windows, overridable via app settings."""
        unpublished = DEFAULT_UNPUBLISHED_HOURS
        draft = DEFAULT_DRAFT_HOURS
        try:
            value = await app_config.resolve("stuck_mix_hours")
            if value:
                unpublished = int(value)
        except Exception:  # pragma: no cover - setting absent
            pass
        try:
            value = await app_config.resolve("stuck_draft_hours")
            if value:
                draft = int(value)
        except Exception:  # pragma: no cover - setting absent
            pass
        return unpublished, draft

    async def check(self, alert: bool = False) -> Dict[str, Any]:
        """Find mixes ingested but not published inside their window."""
        unpublished_hours, draft_hours = await self._window_hours()
        now = datetime.now(timezone.utc)

        async with async_session_factory() as session:
            rows = (
                await session.execute(
                    select(Mix).where(
                        Mix.source == "pipeline",
                        or_(
                            Mix.youtube_url.is_(None),
                            Mix.soundcloud_url.is_(None),
                        ),
                    )
                )
            ).scalars().all()
            mixes = [
                {
                    "id": m.id,
                    "title": m.title,
                    "status": m.pipeline_status,
                    "step": m.pipeline_step,
                    "error": m.pipeline_error,
                    "created_at": _aware(m.created_at),
                    "completed_at": _aware(m.pipeline_completed_at),
                    "soundcloud_url": m.soundcloud_url,
                    "youtube_url": m.youtube_url,
                    "mixcloud_url": m.mixcloud_url,
                }
                for m in rows
            ]

        stuck: List[Dict[str, Any]] = []
        for mix in mixes:
            created = mix["created_at"]
            if created is None:
                # No timestamp is not evidence of freshness.
                age_hours = None
            else:
                age_hours = (now - created).total_seconds() / 3600.0

            published_anywhere = bool(
                mix["soundcloud_url"] or mix["youtube_url"] or mix["mixcloud_url"]
            )
            status = mix["status"] or "unknown"

            if status == "completed" and published_anywhere:
                continue

            is_draft = status == "draft_review"
            window = draft_hours if is_draft else unpublished_hours

            if age_hours is not None and age_hours < window:
                continue

            reason = (
                "awaiting owner review past the review window"
                if is_draft
                else (
                    "published to some targets but not all"
                    if published_anywhere
                    else "never published to any target"
                )
            )
            stuck.append({
                **{k: v for k, v in mix.items() if k not in ("created_at", "completed_at")},
                "age_hours": round(age_hours, 1) if age_hours is not None else None,
                "window_hours": window,
                "reason": reason,
                "kind": "draft_unreviewed" if is_draft else "unpublished",
            })

        self._last_result = {
            "state": "ok",
            "checked_at": now.isoformat(),
            "stuck_count": len(stuck),
            "stuck": stuck,
            "windows": {
                "unpublished_hours": unpublished_hours,
                "draft_hours": draft_hours,
            },
        }

        if alert and stuck:
            for entry in stuck:
                await self._alert(entry)

        return self._last_result

    async def _alert(self, entry: Dict[str, Any]) -> None:
        from app.services import activity_log
        from app.services.notification_service import get_notification_service

        # Dedupe against the persistent log, not memory: a crash loop must not
        # turn one stuck mix into an alert storm.
        try:
            since = datetime.now(timezone.utc) - timedelta(hours=REALERT_AFTER_HOURS)
            _items, total = await activity_log.query(
                limit=1, mix_id=entry["id"], event=EVENT, since=since,
            )
            if total:
                logger.debug("Stuck-mix alert for %s already raised recently", entry["id"])
                return
        except Exception:  # pragma: no cover - defensive
            pass

        age = f"{entry['age_hours']}h" if entry["age_hours"] is not None else "unknown age"
        message = (
            f"Mix \"{entry['title']}\" has been in the system {age} "
            f"(window {entry['window_hours']}h) and is {entry['reason']}. "
            f"Pipeline status: {entry['status']}"
            + (f" at {entry['step']}" if entry["step"] else "")
            + (f" — {entry['error']}" if entry["error"] else "")
        )

        await activity_log.warn(
            EVENT, message, mix_id=entry["id"], context=entry,
        )
        try:
            await get_notification_service().notify(
                "stuck_mix",
                mix_id=entry["id"],
                title=f"Unpublished mix: {entry['title']}",
                message=message,
                data=entry,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Stuck-mix notification failed for %s", entry["id"])


_watchdog: Optional[StuckMixWatchdog] = None


def get_stuck_mix_watchdog() -> StuckMixWatchdog:
    global _watchdog
    if _watchdog is None:
        _watchdog = StuckMixWatchdog()
    return _watchdog
