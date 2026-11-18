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
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select

from app.database import async_session_factory
from app.models import Mix
from app.services import app_config
from app.services import soundcloud_reconcile as sc_reconcile
from app.services.duplicate_guard import STATUS_DUPLICATE

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1800  # 30 minutes

# Windows, in hours, before a mix counts as stuck.
DEFAULT_UNPUBLISHED_HOURS = 6
# A draft is deliberately waiting on a human, so it gets a much longer leash —
# but not an infinite one. A draft nobody ever came back to is still a mix
# that silently never shipped.
DEFAULT_DRAFT_HOURS = 72

# Don't re-alert about the same mix more often than this. The FIRST alert is
# the one that matters; the repeat exists so a genuinely stuck publish is not
# forgotten, not to nag. A draft the owner has consciously parked gets a much
# longer leash than a mix that failed to publish.
REALERT_AFTER_HOURS = 48
REALERT_AFTER_HOURS_DRAFT = 168  # weekly

EVENT = "stuck_mix"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class StuckMixWatchdog:
    def __init__(
        self, sc_track_lister: Optional[sc_reconcile.TrackLister] = None
    ) -> None:
        # Lists the SoundCloud account's tracks right now. Injectable so tests
        # never talk to SoundCloud.
        self._sc_track_lister = sc_track_lister or sc_reconcile.default_track_lister
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

    async def check(
        self, alert: bool = False, verify: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Find mixes ingested but not published inside their window.

        ``verify`` (default: same as ``alert``) checks each stuck mix against
        SoundCloud as it is right now before anything is reported, so a
        report never repeats a stale "live on SoundCloud" and never asks for a
        re-upload of a recording the owner already published himself.
        """
        if verify is None:
            verify = alert
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
                    "soundcloud_track_id": m.soundcloud_track_id,
                    "duration_seconds": m.duration_seconds,
                    "title_youtube": m.title_youtube,
                    "audio_file_path": m.audio_file_path,
                    "video_file_path": m.video_file_path,
                    "duplicate_of": (
                        (m.metadata_json or {}).get("duplicate_of")
                        if isinstance(m.metadata_json, dict) else None
                    ),
                }
                for m in rows
            ]
            pipeline_sc_keys: set = set()
            if verify:
                # Every SoundCloud upload any pipeline mix ever recorded. Any
                # other track on the account was uploaded by the owner.
                for tid, url in (
                    await session.execute(
                        select(Mix.soundcloud_track_id, Mix.soundcloud_url).where(
                            Mix.source == "pipeline"
                        )
                    )
                ).all():
                    if tid:
                        pipeline_sc_keys.add(str(tid))
                    if url:
                        pipeline_sc_keys.add(sc_reconcile._permalink(url))

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

            # A duplicate is parked on purpose: the recording is already out
            # under another mix. Reporting it would only re-ask a settled
            # question.
            if status == STATUS_DUPLICATE or mix["duplicate_of"]:
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
                **{
                    k: v for k, v in mix.items()
                    if k not in ("created_at", "completed_at", "duplicate_of")
                },
                "age_hours": round(age_hours, 1) if age_hours is not None else None,
                "window_hours": window,
                "reason": reason,
                "kind": "draft_unreviewed" if is_draft else "unpublished",
            })

        superseded: List[Dict[str, Any]] = []
        if verify and stuck:
            stuck, superseded = await self._verify_soundcloud(
                stuck, pipeline_sc_keys, persist=alert,
            )

        self._last_result = {
            "state": "ok",
            "superseded": superseded,
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

    async def _verify_soundcloud(
        self,
        stuck: List[Dict[str, Any]],
        pipeline_keys: set,
        *,
        persist: bool,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Re-check each stuck mix's SoundCloud state against the live API.

        Returns (still stuck, superseded by an owner upload). A superseded mix
        is parked as a duplicate when ``persist`` is set, so it does not come
        back on the next sweep.
        """
        tracks: Optional[List[Dict[str, Any]]] = None
        error: Optional[str] = None
        try:
            tracks = await self._sc_track_lister()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("Stuck-mix SoundCloud check failed: %s", error)

        still: List[Dict[str, Any]] = []
        superseded: List[Dict[str, Any]] = []
        for entry in stuck:
            verdict = sc_reconcile.verdict_for(entry, tracks, pipeline_keys, error)
            entry["soundcloud_check"] = verdict.to_dict()
            if (
                verdict.recorded == sc_reconcile.SC_GONE
                and not (entry.get("youtube_url") or entry.get("mixcloud_url"))
            ):
                entry["reason"] = (
                    "not live on any target (its SoundCloud upload has since "
                    "been deleted)"
                )
            if entry.get("kind") != "draft_unreviewed" and verdict.canonical:
                superseded.append(entry)
                if persist:
                    await self._park_as_owner_copy(entry, verdict)
                continue
            still.append(entry)
        return still, superseded

    async def _park_as_owner_copy(
        self, entry: Dict[str, Any], verdict: sc_reconcile.SoundCloudVerdict
    ) -> None:
        from app.services.duplicate_guard import DuplicateMatch, mark_duplicate

        canon = verdict.to_dict()["canonical"]
        reasons = list(canon["match"]) + ["owner's own SoundCloud upload"]
        if verdict.recorded == sc_reconcile.SC_GONE:
            reasons.append(f"pipeline upload {verdict.recorded_ref} no longer exists")
        match = DuplicateMatch(
            duplicate_of=f"soundcloud:{canon['track_id']}",
            title=canon["title"] or canon["url"],
            reasons=reasons,
            published=True,
        )
        try:
            await mark_duplicate(entry["id"], match, stage="stuck_mix_watchdog")
            async with async_session_factory() as session:
                mix = await session.get(Mix, entry["id"])
                if mix is not None:
                    meta = dict(mix.metadata_json or {})
                    meta["canonical_upload"] = {"platform": "soundcloud", **canon}
                    mix.metadata_json = meta
                    await session.commit()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not park %s as an owner copy", entry["id"])

    async def _alert(self, entry: Dict[str, Any]) -> None:
        from app.services import activity_log
        from app.services.notification_service import get_notification_service

        # Dedupe against the persistent log, not memory: a crash loop must not
        # turn one stuck mix into an alert storm.
        window = (
            REALERT_AFTER_HOURS_DRAFT
            if entry.get("kind") == "draft_unreviewed"
            else REALERT_AFTER_HOURS
        )
        try:
            since = datetime.now(timezone.utc) - timedelta(hours=window)
            _items, total = await activity_log.query(
                limit=1, mix_id=entry["id"], event=EVENT, since=since,
            )
            if total:
                logger.debug("Stuck-mix alert for %s already raised recently", entry["id"])
                return
        except Exception:  # pragma: no cover - defensive
            pass

        age = f"{entry['age_hours']}h" if entry["age_hours"] is not None else "unknown age"
        check = entry.get("soundcloud_check")
        error = entry["error"]
        if (
            error and check is not None
            and check["recorded"] != sc_reconcile.SC_LIVE
            and re.search(r"live on [^;]*soundcloud", error, re.I)
        ):
            # The stored note says SoundCloud is live; SoundCloud now says
            # otherwise (or could not be asked). Don't repeat the stale claim.
            error = re.sub(
                r"live on [^;]*soundcloud[^;]*;?\s*", "", error, flags=re.I
            ).strip(" ;—-") or None
        message = (
            f"Mix \"{entry['title']}\" has been in the system {age} "
            f"(window {entry['window_hours']}h) and is {entry['reason']}. "
            f"Pipeline status: {entry['status']}"
            + (f" at {entry['step']}" if entry["step"] else "")
            + (f" — {error}" if error else "")
        )
        if check is not None:
            # pipeline_error was written when the publish step ran; this is
            # what SoundCloud says now, and it wins.
            message += " " + sc_reconcile.SoundCloudVerdict(
                recorded=check["recorded"],
                recorded_ref=check["recorded_ref"],
                error=check["error"],
            ).summary()

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
