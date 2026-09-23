"""Pipeline orchestrator -- state-machine that drives each mix through all steps."""

import asyncio
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory
from app.models import Mix, PipelineStep
from app.services import app_config
from app.services.platform_errors import FailureCause, classify_failure

logger = logging.getLogger(__name__)

PIPELINE_STEPS: List[str] = [
    "detect",
    "analyze",
    "generate_description",
    "generate_art",
    "upload_soundcloud",
    "verify_soundcloud",
    "upload_youtube",
    "verify_youtube",
    "upload_mixcloud",
    "verify_mixcloud",
    "cross_link",
    "complete",
]

# --- Phase structure -------------------------------------------------------
#
# The flat list above is still the canonical ORDER (the UI renders it, and the
# step-index math depends on it), but it is no longer the unit of execution.
#
# Incident 2026-08-12: the orchestrator walked PIPELINE_STEPS strictly in
# order and returned on the first failure. SoundCloud's OAuth grant was dead,
# upload_soundcloud failed, and the run stopped — leaving upload_youtube
# "pending" forever even though YouTube was healthy the entire time. The mix
# was published nowhere and nothing said so; a human had to notice.
#
# So: prep is shared and genuinely sequential (nothing can publish without
# artwork and a description), but each publish target is an INDEPENDENT leg.
# One leg's failure blocks only the rest of ITS OWN leg. Legs run one after
# another rather than concurrently on purpose — a multi-GB upload on home
# upstream should not compete with another one — but failure never propagates
# sideways.
PREP_STEPS: List[str] = [
    "detect",
    "analyze",
    "generate_description",
    "generate_art",
]

PLATFORM_LEGS: Dict[str, List[str]] = {
    "soundcloud": ["upload_soundcloud", "verify_soundcloud"],
    "youtube": ["upload_youtube", "verify_youtube"],
    "mixcloud": ["upload_mixcloud", "verify_mixcloud"],
}

# Steps that need more than one platform to have landed.
POST_PUBLISH_STEPS: List[str] = ["cross_link"]

PLATFORM_URL_FIELD = {
    "soundcloud": "soundcloud_url",
    "youtube": "youtube_url",
    "mixcloud": "mixcloud_url",
}


def leg_for_step(step_name: str) -> Optional[str]:
    """The platform leg a step belongs to, or None for shared steps."""
    for platform, steps in PLATFORM_LEGS.items():
        if step_name in steps:
            return platform
    return None

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 5
VIDEO_POLL_INTERVAL = 300  # 5 minutes

# Video-completeness gate (see check_video_complete): a stalled OBS→NAS sync
# leaves a partial video the upload step must wait out, not upload.
VIDEO_MIN_DURATION_RATIO = 0.9
VIDEO_STABLE_SECONDS = 180


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING = "waiting"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"  # was "running" when the app restarted
    # An upstream step in the SAME leg failed, so this one was never attempted.
    # Distinct from PENDING, which used to be the resting place of every step
    # a failure had silently stranded.
    BLOCKED = "blocked"


# Mix-level publish outcomes.
MIX_STATUS_PARTIAL = "partial"


# report_progress throttles: WS emit at most once per second per (mix, step);
# the DB row (the recovery path) is written at most every 5 seconds.
PROGRESS_EMIT_INTERVAL = 1.0
PROGRESS_DB_INTERVAL = 5.0


StepHandler = Callable[[str, AsyncSession], Coroutine[Any, Any, Optional[dict]]]


@dataclass
class StepResult:
    """Outcome of one step attempt sequence."""

    ok: bool
    output: Optional[dict] = None
    error: Optional[str] = None
    cause: Optional[FailureCause] = None

    def __bool__(self) -> bool:  # keeps `if await _execute_step(...)` honest
        return self.ok


async def _ffprobe_container_duration(video_path: str) -> Optional[float]:
    """Container duration in seconds via ffprobe, or None when it does not
    parse — an unfinalized/truncated MKV reports ``duration=N/A``. Same
    subprocess pattern as shorts_pipeline.ffprobe_clip (ffmpeg ships in the
    container image)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", video_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await proc.communicate()
    except OSError:  # pragma: no cover - ffprobe missing from the image
        return None
    if proc.returncode != 0:
        return None
    try:
        raw = (json.loads(out.decode(errors="replace") or "{}").get("format") or {}).get(
            "duration"
        )
        return float(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None


async def check_video_complete(
    video_path: str, audio_duration: Optional[float]
) -> Tuple[bool, str]:
    """Is the video fully synced/finalized? Returns ``(complete, reason)``.

    Prod incident this guards against: the OBS→NAS sync stalled mid-copy and
    the paired MKV held 10m40s of a ~115-minute set (ffprobe duration=N/A,
    decode ending "File ended prematurely") — which upload_youtube would have
    happily published. Cheap gates, in order:

    1. The container duration must parse — a truncated/unfinalized MKV
       reports no duration at all.
    2. When the mix knows its audio duration, the video must cover at least
       ``VIDEO_MIN_DURATION_RATIO`` of it (a partial copy can still carry a
       parseable duration).
    3. The file must be untouched for ``VIDEO_STABLE_SECONDS`` — a fresh
       mtime means it is still growing.

    The probe is subprocess-only; callers keep it outside any DB write
    transaction (the poll loop closes its session before calling this).
    """
    duration = await _ffprobe_container_duration(video_path)
    if duration is None:
        return False, (
            "video unfinalized/truncated: no container duration "
            f"({os.path.basename(video_path)})"
        )
    if audio_duration and duration < VIDEO_MIN_DURATION_RATIO * audio_duration:
        return False, (
            f"video {duration:.0f}s < 90% of audio {audio_duration:.0f}s — "
            "likely partial sync"
        )
    try:
        age = time.time() - os.path.getmtime(video_path)
    except OSError as exc:
        return False, f"video file unreadable: {exc}"
    if age < VIDEO_STABLE_SECONDS:
        return False, (
            f"video file modified {age:.0f}s ago — still syncing "
            f"(needs {VIDEO_STABLE_SECONDS}s of stability)"
        )
    return True, "ok"


class PipelineOrchestrator:
    """Manages step-by-step mix processing with retries, concurrency, and pause/resume."""

    def __init__(self) -> None:
        self._handlers: Dict[str, StepHandler] = {}
        self._running_pipelines: Dict[str, asyncio.Task] = {}
        self._max_concurrent: int = settings.MAX_CONCURRENT_PIPELINES
        self._paused: bool = False
        self._event_listeners: List[Callable] = []
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._video_poll_tasks: Dict[str, asyncio.Task] = {}
        # Live progress throttle state per (mix_id, step_name)
        self._progress_last_emit: Dict[Tuple[str, str], float] = {}
        self._progress_last_db: Dict[Tuple[str, str], float] = {}
        self._progress_last_percent: Dict[Tuple[str, str], int] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_handler(self, step_name: str, handler: StepHandler) -> None:
        """Register an async handler for a pipeline step."""
        if step_name not in PIPELINE_STEPS:
            raise ValueError(f"Unknown step: {step_name}")
        self._handlers[step_name] = handler
        logger.debug("Registered handler for step: %s", step_name)

    def on_event(self, listener: Callable) -> None:
        """Register a listener called with (event_type, mix_id, data)."""
        self._event_listeners.append(listener)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def _emit(self, event_type: str, mix_id: str, data: Optional[dict] = None) -> None:
        data = data or {}
        await self._activity_from_event(event_type, mix_id, data)
        for listener in self._event_listeners:
            try:
                result = listener(event_type, mix_id, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Event listener error for %s", event_type)

    async def _activity_from_event(
        self, event_type: str, mix_id: str, data: dict
    ) -> None:
        """Mirror an orchestrator event into the persistent activity log."""
        try:
            from app.services import activity_log

            stage = data.get("step")
            if event_type == "pipeline_started":
                await activity_log.info("pipeline_started", "Pipeline started", mix_id=mix_id)
            elif event_type == "step_completed":
                elapsed = data.get("elapsed_seconds")
                msg = f"Step {stage} completed"
                if elapsed is not None:
                    msg += f" in {elapsed:.1f}s"
                await activity_log.info("step_completed", msg, mix_id=mix_id, stage=stage)
            elif event_type == "draft_ready":
                await activity_log.info(
                    "draft_paused",
                    "DRAFT_MODE: pipeline paused for review before any upload",
                    mix_id=mix_id,
                )
            elif event_type == "upload_complete":
                await activity_log.info(
                    "upload_complete", "Pipeline completed — all uploads done", mix_id=mix_id
                )
            elif event_type == "error":
                cause = data.get("cause") or {}
                await activity_log.error(
                    "pipeline_error",
                    f"Pipeline error at step {stage}: {data.get('error', 'failed')}",
                    mix_id=mix_id,
                    stage=stage,
                    platform=cause.get("platform") or data.get("platform"),
                    context={
                        "retryable": data.get("retryable", True),
                        **{k: v for k, v in data.items() if k != "step"},
                    },
                )
            elif event_type == "platform_failed":
                cause = data.get("cause") or {}
                await activity_log.error(
                    "platform_failed",
                    f"{data.get('platform')} leg failed at {stage or data.get('step')}: "
                    f"{cause.get('summary') or data.get('error', 'failed')}",
                    mix_id=mix_id,
                    stage=data.get("step"),
                    platform=data.get("platform"),
                    context={k: v for k, v in data.items() if k != "step"},
                )
            elif event_type == "publish_incomplete":
                await activity_log.warn(
                    "publish_incomplete",
                    data.get("error", "publish incomplete"),
                    mix_id=mix_id,
                    context={
                        "published": data.get("published"),
                        "failed": data.get("failed"),
                    },
                )
        except Exception:  # pragma: no cover - defensive
            logger.debug("activity mirror failed for %s", event_type, exc_info=True)

    # ------------------------------------------------------------------
    # Live step progress
    # ------------------------------------------------------------------

    async def report_progress(
        self,
        mix_id: str,
        step_name: str,
        percent: Optional[int],
        detail: Optional[str] = None,
    ) -> None:
        """Report live progress for a running step. Never raises.

        Throttled per (mix_id, step): the ``step_progress`` WS event is
        emitted at most once per second (a terminal 100% always goes out),
        and the PipelineStep DB row is updated at most every 5 seconds. The
        WS stream is the live path; the DB row is the recovery path.
        Crossing a 25/50/75/100% milestone also writes an activity entry so
        the feed alone can explain a long upload.
        """
        try:
            key = (mix_id, step_name)
            now = time.monotonic()
            pct = None
            if percent is not None:
                pct = max(0, min(100, int(percent)))

            # Milestone activity entries (25/50/75/100), checked before the
            # emit throttle so a crossing is never silently swallowed.
            if pct is not None:
                last_pct = self._progress_last_percent.get(key, -1)
                if pct // 25 > last_pct // 25 and pct >= 25:
                    milestone = (pct // 25) * 25
                    try:
                        from app.services import activity_log

                        msg = f"Step {step_name} {milestone}%"
                        if detail:
                            msg += f" ({detail})"
                        await activity_log.info(
                            "progress_milestone", msg, mix_id=mix_id, stage=step_name,
                        )
                    except Exception:  # pragma: no cover - defensive
                        pass
                self._progress_last_percent[key] = pct

            # WS emit throttle: >=1/sec, but a terminal 100% always emits.
            last_emit = self._progress_last_emit.get(key, 0.0)
            is_final = pct is not None and pct >= 100
            if (now - last_emit) >= PROGRESS_EMIT_INTERVAL or is_final:
                self._progress_last_emit[key] = now
                await self._emit("step_progress", mix_id, {
                    "step": step_name, "progress": pct, "detail": detail,
                })

            # DB write throttle: at most every 5s (own short session).
            last_db = self._progress_last_db.get(key, 0.0)
            if (now - last_db) >= PROGRESS_DB_INTERVAL or is_final:
                self._progress_last_db[key] = now
                try:
                    async with async_session_factory() as session:
                        await session.execute(
                            update(PipelineStep)
                            .where(
                                PipelineStep.mix_id == mix_id,
                                PipelineStep.step_name == step_name,
                            )
                            .values(progress=pct, progress_detail=detail)
                        )
                        await session.commit()
                except Exception:
                    logger.debug(
                        "progress DB update failed for %s/%s", mix_id, step_name,
                        exc_info=True,
                    )
        except Exception:  # pragma: no cover - defensive
            logger.debug("report_progress failed for %s/%s", mix_id, step_name, exc_info=True)

    def _clear_progress_state(self, mix_id: str, step_name: str) -> None:
        key = (mix_id, step_name)
        self._progress_last_emit.pop(key, None)
        self._progress_last_db.pop(key, None)
        self._progress_last_percent.pop(key, None)

    def _make_progress_cb(self, mix_id: str, step_name: str) -> Callable:
        async def progress_cb(percent: Optional[int], detail: Optional[str] = None) -> None:
            await self.report_progress(mix_id, step_name, percent, detail)

        return progress_cb

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------

    def pause(self) -> None:
        self._paused = True
        logger.info("Pipeline orchestrator paused globally")

    def resume(self) -> None:
        self._paused = False
        logger.info("Pipeline orchestrator resumed")

    @property
    def is_paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------

    async def start_pipeline(self, mix_id: str) -> None:
        """Kick off the full pipeline for a mix."""
        if mix_id in self._running_pipelines:
            logger.warning("Pipeline already running for mix %s", mix_id)
            return

        task = asyncio.create_task(self._run_pipeline(mix_id))
        self._running_pipelines[mix_id] = task
        task.add_done_callback(lambda _t: self._running_pipelines.pop(mix_id, None))

    async def _run_pipeline(self, mix_id: str) -> None:
        async with self._semaphore:
            logger.info("Pipeline started for mix %s", mix_id)
            await self._emit("pipeline_started", mix_id)

            async with async_session_factory() as session:
                mix = await session.get(Mix, mix_id)
                if not mix:
                    logger.error("Mix %s not found", mix_id)
                    return
                mix.pipeline_status = "running"
                mix.pipeline_started_at = datetime.now(timezone.utc)
                await session.commit()

            # Phase 1 — shared prep. A prep failure genuinely blocks every
            # platform, so it still stops the run.
            for step_name in PREP_STEPS:
                result = await self._execute_step(mix_id, step_name)
                if not result.ok:
                    await self._block_unreached(mix_id, after_prep_failure=step_name)
                    await self._mark_failed(mix_id, step_name)
                    return

                # Draft mode pause after generate_art (DB setting, env fallback)
                if step_name == "generate_art" and await app_config.resolve("draft_mode"):
                    logger.info("DRAFT_MODE: pausing pipeline for mix %s after generate_art", mix_id)
                    await self._set_mix_status(mix_id, "draft_review", step_name)
                    await self._emit("draft_ready", mix_id)
                    return

            # Phase 2 + 3 — independent publish legs, then finalize.
            await self._run_publish_phase(mix_id)

            logger.info("Pipeline finished for mix %s", mix_id)

    async def resume_pipeline(self, mix_id: str) -> None:
        """Resume a paused (draft) pipeline from where it left off."""
        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            if not mix:
                logger.error("Mix %s not found for resume", mix_id)
                return
            current_step = mix.pipeline_step

        if not current_step:
            logger.warning("No pipeline step recorded for mix %s", mix_id)
            return

        try:
            idx = PIPELINE_STEPS.index(current_step)
        except ValueError:
            logger.error("Unknown step %s for mix %s", current_step, mix_id)
            return

        remaining_prep = [s for s in PIPELINE_STEPS[idx + 1:] if s in PREP_STEPS]

        task = asyncio.create_task(self._run_remaining(mix_id, remaining_prep))
        self._running_pipelines[mix_id] = task
        task.add_done_callback(lambda _t: self._running_pipelines.pop(mix_id, None))

    async def _run_remaining(self, mix_id: str, prep_steps: List[str]) -> None:
        """Finish any leftover prep, then run the publish phase."""
        async with self._semaphore:
            await self._set_mix_status(mix_id, "running")
            for step_name in prep_steps:
                result = await self._execute_step(mix_id, step_name)
                if not result.ok:
                    await self._block_unreached(mix_id, after_prep_failure=step_name)
                    await self._mark_failed(mix_id, step_name)
                    return
            await self._run_publish_phase(mix_id)

    # ------------------------------------------------------------------
    # Publish phase — one independent leg per platform
    # ------------------------------------------------------------------

    async def _run_publish_phase(
        self, mix_id: str, platforms: Optional[List[str]] = None
    ) -> Dict[str, dict]:
        """Run each platform leg independently, then cross-link and finalize.

        ``platforms`` limits the run to specific legs (a targeted retry);
        by default every leg is attempted. A leg that raises does not stop the
        next one — that isolation is the whole point of this method.
        """
        targets = platforms or list(PLATFORM_LEGS)
        outcomes: Dict[str, dict] = {}

        for platform in targets:
            steps = PLATFORM_LEGS.get(platform)
            if not steps:
                continue
            try:
                outcomes[platform] = await self._run_leg(mix_id, platform, steps)
            except Exception as exc:  # pragma: no cover - belt and braces
                # A leg must never be able to take the process (or the other
                # legs) down with it.
                logger.exception("Platform leg %s crashed for mix %s", platform, mix_id)
                outcomes[platform] = {
                    "status": "failed",
                    "error": f"leg crashed: {exc}",
                    "cause": {"kind": "unknown", "summary": str(exc)},
                }

        # Cross-link only makes sense once more than one platform has landed;
        # the handler already no-ops otherwise, and a cross-link failure must
        # not un-publish anything.
        if any(o.get("status") == "published" for o in outcomes.values()):
            for step_name in POST_PUBLISH_STEPS:
                result = await self._execute_step(mix_id, step_name)
                if not result.ok:
                    # Cross-linking is cosmetic relative to publishing: it must
                    # not un-publish a mix, but it must not vanish either.
                    logger.warning(
                        "Post-publish step %s failed for mix %s: %s",
                        step_name, mix_id, result.error,
                    )
        else:
            for step_name in POST_PUBLISH_STEPS:
                await self._mark_pending_blocked(
                    mix_id, step_name, "not attempted — nothing published",
                )

        await self._finalize(mix_id)
        return outcomes

    async def _run_leg(self, mix_id: str, platform: str, steps: List[str]) -> dict:
        """Execute one platform's steps. Returns its outcome record.

        Outcome ``status`` is one of:
          published — the upload landed and verified
          skipped   — the platform is not configured (nothing was attempted)
          failed    — an upload/verify step failed; later steps in THIS leg
                      are recorded BLOCKED so they never sit at "pending"
        """
        url: Optional[str] = None
        skipped_reason: Optional[str] = None

        for idx, step_name in enumerate(steps):
            result = await self._execute_step(mix_id, step_name)

            if not result.ok:
                for blocked in steps[idx + 1:]:
                    await self._record_step(
                        mix_id, blocked, StepStatus.BLOCKED,
                        error=f"not attempted — {step_name} failed for {platform}",
                    )
                cause = result.cause.to_dict() if result.cause else None
                await self._emit("platform_failed", mix_id, {
                    "platform": platform,
                    "step": step_name,
                    "error": result.error or "failed",
                    "cause": cause,
                })
                return {
                    "status": "failed",
                    "failed_step": step_name,
                    "error": result.error,
                    "cause": cause,
                }

            output = result.output or {}
            url = url or output.get(PLATFORM_URL_FIELD[platform])
            if output.get("skipped") and not url:
                skipped_reason = output.get("reason") or "not configured"

        if skipped_reason and not url:
            return {"status": "skipped", "reason": skipped_reason}
        return {"status": "published", "url": url}

    async def retry_platform(self, mix_id: str, platform: str) -> dict:
        """Re-run ONE platform leg, then re-finalize the mix.

        This is the endpoint that makes a partial publish self-serve: on
        2026-08-12 the only way to publish the YouTube leg of a mix whose
        SoundCloud leg had failed was for a human to drive it by hand.
        """
        if platform not in PLATFORM_LEGS:
            raise ValueError(f"Unknown platform: {platform}")

        async with self._semaphore:
            await self._set_mix_status(mix_id, "running")
            # Clear the leg's failed/blocked rows so the run is a real attempt.
            for step_name in PLATFORM_LEGS[platform]:
                await self._reset_step_if(
                    mix_id, step_name,
                    statuses=(
                        StepStatus.FAILED.value,
                        StepStatus.BLOCKED.value,
                        StepStatus.INTERRUPTED.value,
                    ),
                )
            outcomes = await self._run_publish_phase(mix_id, platforms=[platform])
        return outcomes.get(platform, {"status": "unknown"})

    async def retry_step(self, mix_id: str, step_name: str) -> bool:
        """Retry a single step, then continue only what that step gates.

        A platform step continues its own leg and re-finalizes; it no longer
        drags the other platforms' steps along behind it.
        """
        result = await self._execute_step(mix_id, step_name, force=True)
        if not result.ok:
            await self._finalize(mix_id)
            return False

        platform = leg_for_step(step_name)
        if platform:
            steps = PLATFORM_LEGS[platform]
            rest = steps[steps.index(step_name) + 1:]
            for later in rest:
                later_result = await self._execute_step(mix_id, later)
                if not later_result.ok:
                    break
            await self._finalize(mix_id)
            return True

        if step_name in PREP_STEPS:
            remaining_prep = [
                s for s in PREP_STEPS[PREP_STEPS.index(step_name) + 1:]
            ]
            task = asyncio.create_task(self._run_remaining(mix_id, remaining_prep))
            self._running_pipelines[mix_id] = task
            task.add_done_callback(lambda _t: self._running_pipelines.pop(mix_id, None))
            return True

        await self._finalize(mix_id)
        return True

    async def _execute_step(
        self, mix_id: str, step_name: str, force: bool = False
    ) -> "StepResult":
        """Execute a single step with retries and exponential backoff.

        Returns a :class:`StepResult` so callers can see the handler output
        (did the platform actually publish, or was it skipped?) and the
        classified proximate cause of a failure — an auth rejection is not the
        same event as a network blip, and the alert has to say which it was.
        """
        while self._paused:
            await asyncio.sleep(2)

        handler = self._handlers.get(step_name)
        if handler is None:
            logger.warning("No handler registered for step %s, skipping", step_name)
            await self._record_step(mix_id, step_name, StepStatus.SKIPPED)
            return StepResult(ok=True, output={"skipped": True, "reason": "no handler"})

        async with async_session_factory() as session:
            # Check if already completed (unless forced)
            if not force:
                existing = (
                    await session.execute(
                        select(PipelineStep).where(
                            PipelineStep.mix_id == mix_id,
                            PipelineStep.step_name == step_name,
                            PipelineStep.status == StepStatus.COMPLETED.value,
                        )
                    )
                ).scalar_one_or_none()
                if existing:
                    logger.info("Step %s already completed for mix %s, skipping", step_name, mix_id)
                    return StepResult(ok=True, output=existing.output_json or {})

        for attempt in range(1, MAX_RETRIES + 1):
            while self._paused:
                await asyncio.sleep(2)

            logger.info(
                "Executing step %s for mix %s (attempt %d/%d)",
                step_name, mix_id, attempt, MAX_RETRIES,
            )
            try:
                from app.services import activity_log

                await activity_log.info(
                    "step_started",
                    f"Step {step_name} started"
                    + (f" (retry {attempt}/{MAX_RETRIES})" if attempt > 1 else ""),
                    mix_id=mix_id,
                    stage=step_name,
                    context={"attempt": attempt},
                )
            except Exception:  # pragma: no cover - defensive
                pass
            started_at = datetime.now(timezone.utc)
            await self._set_mix_status(mix_id, "running", step_name)
            await self._record_step(
                mix_id, step_name, StepStatus.RUNNING,
                started_at=started_at, retry_count=attempt - 1,
            )

            try:
                async with async_session_factory() as session:
                    output = await self._call_handler(handler, mix_id, session, step_name)
                    await session.commit()

                elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                await self._record_step(
                    mix_id, step_name, StepStatus.COMPLETED,
                    started_at=started_at, output=output, retry_count=attempt - 1,
                )
                self._clear_progress_state(mix_id, step_name)
                await self._emit("step_completed", mix_id, {
                    "step": step_name, "elapsed_seconds": elapsed,
                })
                logger.info("Step %s completed for mix %s in %.1fs", step_name, mix_id, elapsed)
                return StepResult(ok=True, output=output or {})

            except _VideoNotReady as not_ready:
                logger.info(
                    "Video not ready for mix %s at step %s (%s), entering wait",
                    mix_id, step_name, not_ready,
                )
                await self._record_step(
                    mix_id, step_name, StepStatus.WAITING,
                    started_at=started_at, retry_count=attempt - 1,
                )
                resolved, wait_reason = await self._wait_for_video(mix_id)
                if resolved:
                    # Re-attempt after video found + complete
                    continue
                else:
                    cause = classify_failure(not_ready, step_name)
                    cause.summary = wait_reason
                    await self._record_step(
                        mix_id, step_name, StepStatus.FAILED,
                        started_at=started_at, error=wait_reason,
                        retry_count=attempt - 1,
                    )
                    await self._emit("error", mix_id, {
                        "step": step_name, "error": wait_reason,
                        "cause": cause.to_dict(), "platform": cause.platform,
                    })
                    return StepResult(ok=False, error=wait_reason, cause=cause)

            except Exception as exc:
                cause = classify_failure(exc, step_name)
                logger.exception(
                    "Step %s failed for mix %s (attempt %d): %s [%s]",
                    step_name, mix_id, attempt, exc, cause.kind,
                )
                # A rejected credential is not a transient fault. Retrying it
                # two more times only delays the operator learning that a
                # token needs replacing — and on 08-12 those retries were
                # what pushed the run into the browser fallback whose timeout
                # then became the reported "cause".
                is_last = attempt >= MAX_RETRIES or not cause.retryable
                if not is_last:
                    backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.info("Retrying step %s in %ds", step_name, backoff)
                    try:
                        from app.services import activity_log

                        await activity_log.warn(
                            "step_retry",
                            f"Step {step_name} failed (attempt {attempt}/{MAX_RETRIES}): "
                            f"{cause.summary}. Retrying in {backoff}s.",
                            mix_id=mix_id,
                            stage=step_name,
                            context={"attempt": attempt, "backoff_seconds": backoff,
                                     "cause_kind": cause.kind},
                        )
                    except Exception:  # pragma: no cover - defensive
                        pass
                    await asyncio.sleep(backoff)
                else:
                    if not cause.retryable and attempt < MAX_RETRIES:
                        logger.warning(
                            "Step %s for mix %s failed with a non-retryable %s error; "
                            "not burning the remaining %d attempt(s)",
                            step_name, mix_id, cause.kind, MAX_RETRIES - attempt,
                        )
                    # The step row records the PROXIMATE cause, not just the
                    # outermost exception text.
                    await self._record_step(
                        mix_id, step_name, StepStatus.FAILED,
                        started_at=started_at, error=cause.summary,
                        retry_count=attempt - 1,
                    )
                    await self._emit("error", mix_id, {
                        "step": step_name,
                        "error": cause.summary,
                        "cause": cause.to_dict(),
                        "platform": cause.platform,
                        "credential": cause.credential,
                        "retryable": cause.retryable,
                    })
                    return StepResult(ok=False, error=cause.summary, cause=cause)

        return StepResult(ok=False, error=f"{step_name} exhausted all attempts")

    async def _call_handler(
        self, handler: StepHandler, mix_id: str, session: AsyncSession, step_name: str
    ) -> Optional[dict]:
        """Invoke a handler, passing a progress callback when it accepts one.

        Handlers opt in by accepting a ``progress_cb`` keyword (or ``**kwargs``);
        untouched two-argument handlers keep working unchanged.
        """
        accepts_progress = False
        try:
            params = inspect.signature(handler).parameters
            accepts_progress = "progress_cb" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            accepts_progress = False

        if accepts_progress:
            return await handler(
                mix_id, session, progress_cb=self._make_progress_cb(mix_id, step_name)
            )
        return await handler(mix_id, session)

    async def _wait_for_video(
        self, mix_id: str, max_checks: Optional[int] = None
    ) -> Tuple[bool, str]:
        """Poll every 5min until the video exists AND passes the completeness
        gate. Returns ``(resolved, last_reason)``.

        ``max_checks`` resolves from the ``video_wait_max_checks`` setting
        (default 96 = 8 hours of 5-minute polls) — a stalled OBS→NAS sync can
        take hours to recover, and the whole point is to wait it out
        autonomously. On exhaustion the caller fails the step with the last
        gate reason so the UI shows WHY it was waiting.
        """
        if max_checks is None:
            max_checks = int(await app_config.resolve("video_wait_max_checks"))
        last_reason = "Video file never appeared"
        for i in range(max_checks):
            await asyncio.sleep(VIDEO_POLL_INTERVAL)
            path: Optional[str] = None
            audio_duration: Optional[float] = None
            async with async_session_factory() as session:
                mix = await session.get(Mix, mix_id)
                if mix:
                    path = mix.video_file_path
                    audio_duration = mix.duration_seconds
            if path and os.path.exists(path):
                # Session above is closed — the ffprobe subprocess never runs
                # inside a DB transaction.
                complete, reason = await check_video_complete(path, audio_duration)
                if complete:
                    logger.info("Video file ready for mix %s: %s", mix_id, path)
                    return True, "ok"
                last_reason = reason
                logger.info(
                    "Video check %d/%d for mix %s — %s",
                    i + 1, max_checks, mix_id, reason,
                )
            else:
                logger.debug(
                    "Video check %d/%d for mix %s -- not found yet",
                    i + 1, max_checks, mix_id,
                )
        return False, last_reason

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _record_step(
        self,
        mix_id: str,
        step_name: str,
        status: StepStatus,
        started_at: Optional[datetime] = None,
        output: Optional[dict] = None,
        error: Optional[str] = None,
        retry_count: int = 0,
    ) -> None:
        """Upsert the (mix_id, step_name) step row — one row per step per mix.

        Historically each recording inserted a NEW row, so mixes accumulated
        an initial set of eternally-"pending" rows plus one row per attempt
        (18 rows for a 9-step pipeline, observed in production). The latest
        attempt now wins in place.
        """
        completed_at = datetime.now(timezone.utc) if status in (
            StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED,
            StepStatus.BLOCKED,
        ) else None

        async with async_session_factory() as session:
            existing = (
                await session.execute(
                    select(PipelineStep)
                    .where(
                        PipelineStep.mix_id == mix_id,
                        PipelineStep.step_name == step_name,
                    )
                    .order_by(PipelineStep.id.desc())
                )
            ).scalars().first()

            if existing is not None:
                existing.status = status.value
                existing.started_at = started_at or existing.started_at or datetime.now(timezone.utc)
                existing.completed_at = completed_at
                existing.output_json = output
                existing.error = error
                existing.retry_count = retry_count
                if status is StepStatus.COMPLETED:
                    existing.progress = 100
                    existing.progress_detail = None
                elif status is StepStatus.RUNNING and retry_count == 0:
                    existing.progress = None
                    existing.progress_detail = None
            else:
                session.add(PipelineStep(
                    mix_id=mix_id,
                    step_name=step_name,
                    status=status.value,
                    started_at=started_at or datetime.now(timezone.utc),
                    completed_at=completed_at,
                    output_json=output,
                    error=error,
                    retry_count=retry_count,
                    progress=100 if status is StepStatus.COMPLETED else None,
                ))
            await session.commit()

    async def _set_mix_status(
        self, mix_id: str, status: str, step: Optional[str] = None
    ) -> None:
        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            if mix:
                mix.pipeline_status = status
                if step:
                    mix.pipeline_step = step
                await session.commit()

    async def _reset_step_if(
        self, mix_id: str, step_name: str, statuses: Tuple[str, ...]
    ) -> None:
        """Put a step back to PENDING when it is in one of ``statuses``."""
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(PipelineStep)
                    .where(
                        PipelineStep.mix_id == mix_id,
                        PipelineStep.step_name == step_name,
                    )
                    .order_by(PipelineStep.id.desc())
                )
            ).scalars().first()
            if row is not None and row.status in statuses:
                row.status = StepStatus.PENDING.value
                row.error = None
                await session.commit()

    async def _block_unreached(
        self, mix_id: str, after_prep_failure: str
    ) -> None:
        """Mark every still-pending step BLOCKED after a prep failure.

        Prep genuinely gates all publishing, but the steps behind it must
        still say WHY they never ran instead of resting at "pending".
        """
        try:
            idx = PIPELINE_STEPS.index(after_prep_failure)
        except ValueError:  # pragma: no cover - defensive
            return
        reason = f"not attempted — {after_prep_failure} failed"
        for step_name in PIPELINE_STEPS[idx + 1:]:
            if step_name == "complete":
                continue
            await self._mark_pending_blocked(mix_id, step_name, reason)

    async def _mark_pending_blocked(
        self, mix_id: str, step_name: str, reason: str
    ) -> None:
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(PipelineStep)
                    .where(
                        PipelineStep.mix_id == mix_id,
                        PipelineStep.step_name == step_name,
                    )
                    .order_by(PipelineStep.id.desc())
                )
            ).scalars().first()
            if row is None:
                session.add(PipelineStep(
                    mix_id=mix_id, step_name=step_name,
                    status=StepStatus.BLOCKED.value, error=reason,
                    completed_at=datetime.now(timezone.utc),
                ))
                await session.commit()
            elif row.status == StepStatus.PENDING.value:
                row.status = StepStatus.BLOCKED.value
                row.error = reason
                row.completed_at = datetime.now(timezone.utc)
                await session.commit()

    async def leg_states(self, mix_id: str) -> Dict[str, dict]:
        """Per-platform publish state, derived from the DB — never guessed.

        The step rows plus the mix's platform URLs are the ground truth. A leg
        is reported as:

          published  — its steps completed and a platform URL exists
          skipped    — its steps completed but the platform is unconfigured
          failed     — a step failed (carries the recorded proximate cause)
          blocked    — never attempted because its own upload failed earlier
          pending    — genuinely not run yet

        There is no default-to-fine branch: an unrecognized combination is
        reported as ``unknown``, because "we don't know" must never render as
        "published".
        """
        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            rows = (
                await session.execute(
                    select(PipelineStep).where(PipelineStep.mix_id == mix_id)
                )
            ).scalars().all()

        by_name: Dict[str, PipelineStep] = {}
        for row in rows:
            prev = by_name.get(row.step_name)
            if prev is None or (row.id or 0) > (prev.id or 0):
                by_name[row.step_name] = row

        states: Dict[str, dict] = {}
        for platform, steps in PLATFORM_LEGS.items():
            url = getattr(mix, PLATFORM_URL_FIELD[platform], None) if mix else None
            statuses = [
                (by_name[s].status if s in by_name else StepStatus.PENDING.value)
                for s in steps
            ]
            entry: Dict[str, Any] = {"url": url}

            failed_step = next(
                (s for s in steps
                 if s in by_name and by_name[s].status == StepStatus.FAILED.value),
                None,
            )
            if failed_step is not None:
                entry.update({
                    "status": "failed",
                    "failed_step": failed_step,
                    "error": by_name[failed_step].error,
                })
            elif all(s == StepStatus.COMPLETED.value for s in statuses):
                entry["status"] = "published" if url else "skipped"
                if not url:
                    upload_out = (by_name[steps[0]].output_json or {}) if steps[0] in by_name else {}
                    entry["reason"] = upload_out.get("reason", "platform not configured")
            elif any(s == StepStatus.BLOCKED.value for s in statuses):
                entry["status"] = "blocked"
            elif all(s in (StepStatus.PENDING.value, StepStatus.SKIPPED.value)
                     for s in statuses):
                entry["status"] = "skipped" if all(
                    s == StepStatus.SKIPPED.value for s in statuses
                ) else "pending"
            elif any(s in (StepStatus.RUNNING.value, StepStatus.WAITING.value)
                     for s in statuses):
                entry["status"] = "running"
            else:
                entry["status"] = "unknown"
            states[platform] = entry
        return states

    async def _finalize(self, mix_id: str) -> str:
        """Set the mix's terminal publish state from its per-leg outcomes.

        Three honest outcomes: everything that could publish did (completed),
        some did and some did not (partial), or none did (failed). ``partial``
        is a first-class state — before this, a mix that reached YouTube but
        not SoundCloud had no way to say so.
        """
        states = await self.leg_states(mix_id)
        published = [p for p, s in states.items() if s["status"] == "published"]
        # Fail closed: only "published" and "skipped" (nothing to publish to)
        # are acceptable resting states. pending/blocked/running/unknown all
        # count as not-done, because an unfinished leg rendering as fine is
        # exactly how a mix went missing for two days.
        failed = [
            p for p, s in states.items()
            if s["status"] not in ("published", "skipped")
        ]

        summary = {
            "legs": states,
            "published": published,
            "failed": failed,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

        if not failed:
            await self._store_publish_summary(mix_id, summary)
            await self._mark_complete(mix_id)
            return "completed"

        # Nothing further will run for the failed legs, so their untouched
        # rows are blocked, not pending, and so is `complete`.
        for platform in failed:
            for step_name in PLATFORM_LEGS[platform]:
                await self._mark_pending_blocked(
                    mix_id, step_name, f"not attempted — {platform} leg failed",
                )
        await self._mark_pending_blocked(
            mix_id, "complete",
            f"publish incomplete — failed: {', '.join(sorted(failed))}",
        )

        detail = "; ".join(
            f"{p}: {states[p].get('error') or states[p]['status']}" for p in sorted(failed)
        )
        if published:
            status = MIX_STATUS_PARTIAL
            error_text = (
                f"Partial publish — live on {', '.join(sorted(published))}; "
                f"not published to {', '.join(sorted(failed))}. {detail}"
            )
        else:
            status = "failed"
            error_text = f"Publish failed on every target. {detail}"

        summary["status"] = status
        await self._store_publish_summary(mix_id, summary)

        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            if mix:
                mix.pipeline_status = status
                mix.pipeline_error = error_text
                await session.commit()

        try:
            from app.services import activity_log

            await activity_log.error(
                "publish_partial" if published else "publish_failed",
                error_text,
                mix_id=mix_id,
                context={"published": published, "failed": failed},
            )
        except Exception:  # pragma: no cover - defensive
            pass

        await self._emit("publish_incomplete", mix_id, {
            "published": published,
            "failed": failed,
            "legs": states,
            "error": error_text,
        })
        logger.warning("Mix %s finished %s: %s", mix_id, status, error_text)
        return status

    async def _store_publish_summary(self, mix_id: str, summary: dict) -> None:
        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            if not mix:
                return
            meta = dict(mix.metadata_json or {})
            meta["publish"] = summary
            mix.metadata_json = meta
            await session.commit()

    async def _mark_complete(self, mix_id: str) -> None:
        # Write the completed status FIRST, before rename/tag. source_tagger
        # hard-gates on mix.pipeline_status == "completed" (only published
        # mixes are tagged); writing it after the rename/tag block meant that
        # gate always tripped and the tagger silently no-op'd on every real
        # run. Renaming and tagging are best-effort cosmetic follow-ups to a
        # run whose real work (verified uploads) is already done, so if the
        # process dies between this write and the rename/tag calls below, the
        # database is telling the truth -- the publish genuinely succeeded.
        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            if mix:
                mix.pipeline_status = "completed"
                mix.pipeline_step = "complete"
                mix.pipeline_error = None
                mix.pipeline_completed_at = datetime.now(timezone.utc)
                await session.commit()

        # Rename the sources to match the generated title now that every upload
        # has been verified -- the video has passed its completeness gate and no
        # uploader is holding the file open. Off by default; see source_renamer.
        # The renamer never raises, and the try/except keeps it that way even if
        # that ever changes: a rename must not be able to un-complete a run.
        try:
            from app.services import source_renamer

            await source_renamer.rename_sources_for_mix(
                mix_id, reason="pipeline_complete"
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Source rename failed for mix %s", mix_id)

        # After renaming, so the tags land on the final path. Best-effort by
        # the same contract as renaming: this returns a status dict and never
        # raises, so this try/except is defense in depth only -- it must not
        # be able to un-complete a run either. Kept in its own try/except
        # (separate from the renamer's, above) so a tagging failure is never
        # misattributed as a rename failure in the logs.
        try:
            from app.services import source_tagger

            await source_tagger.tag_sources_for_mix(mix_id, reason="pipeline_complete")
        except Exception:  # pragma: no cover - defensive
            logger.exception("Source tagging failed for mix %s", mix_id)

        # Close out the pre-created "complete" row too, so a finished mix has
        # no step left sitting at "pending".
        await self._record_step(mix_id, "complete", StepStatus.COMPLETED)
        await self._emit("upload_complete", mix_id)
        logger.info("Pipeline fully completed for mix %s", mix_id)

    async def _mark_failed(self, mix_id: str, step_name: str) -> None:
        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            if mix:
                mix.pipeline_status = "failed"
                mix.pipeline_step = step_name
                mix.pipeline_error = f"Failed at step: {step_name}"
                await session.commit()
        await self._emit("error", mix_id, {"step": step_name})

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def active_count(self) -> int:
        return len(self._running_pipelines)

    @property
    def active_mix_ids(self) -> list[str]:
        return list(self._running_pipelines.keys())


# --------------------------------------------------------------------------
# Boot recovery
# --------------------------------------------------------------------------

# A mix cut off by a restart is re-driven automatically, but only so many
# times. A mix that reliably kills the process (an OOM on analysis, say) must
# not turn into a restart loop, so after this many automatic resumes it is
# left "failed" for a human with the reason spelled out.
MAX_INTERRUPT_RESUMES = 3

# Let the app finish wiring handlers and watchers before re-driving anything,
# and stagger the resumes so several interrupted mixes do not all start heavy
# analysis at the same moment.
RESUME_SETTLE_SECONDS = 20.0
RESUME_STAGGER_SECONDS = 5.0

# Bookkeeping key inside Mix.metadata_json.
RESUME_COUNT_KEY = "interrupt_resumes"


async def sweep_interrupted_at_boot() -> dict:
    """Mark orphaned in-flight work as interrupted after a restart.

    At boot no orchestrator tasks exist, so any PipelineStep still "running"
    and any Mix still pipeline_status "running" was cut off mid-flight by the
    previous shutdown. Both become "interrupted".

    "interrupted" is deliberately NOT "failed": a restart says nothing about
    the mix, only about the process, and a terminal status is how a crash used
    to turn into silent data loss (the set simply never published and nobody
    found out for days). ``resume_interrupted_at_boot`` re-drives these; until
    it does, the status is retryable from the API and visible in the UI.
    """
    swept = {"steps": 0, "mixes": 0}
    try:
        async with async_session_factory() as session:
            steps = (
                await session.execute(
                    select(PipelineStep).where(PipelineStep.status == StepStatus.RUNNING.value)
                )
            ).scalars().all()
            for step in steps:
                step.status = StepStatus.INTERRUPTED.value
                step.progress_detail = None
            swept["steps"] = len(steps)

            mixes = (
                await session.execute(
                    select(Mix).where(Mix.pipeline_status == "running")
                )
            ).scalars().all()
            for mix in mixes:
                mix.pipeline_status = StepStatus.INTERRUPTED.value
                mix.pipeline_error = "interrupted by restart"
            swept["mixes"] = len(mixes)

            await session.commit()

        if swept["steps"] or swept["mixes"]:
            logger.warning(
                "Startup sweep: %d running step(s) and %d running mix(es) "
                "marked interrupted (cut off by restart)",
                swept["steps"], swept["mixes"],
            )
            try:
                from app.services import activity_log

                await activity_log.warn(
                    "interrupted_sweep",
                    f"Startup sweep: {swept['steps']} in-flight step(s) and "
                    f"{swept['mixes']} running mix(es) marked interrupted after "
                    "restart — queued for automatic resume",
                    context=swept,
                )
            except Exception:  # pragma: no cover - defensive
                pass
    except Exception:  # pragma: no cover - defensive
        logger.exception("Startup interrupted-sweep failed")
    return swept


class _VideoNotReady(Exception):
    """Raised by the upload_youtube handler when the video file is missing or
    fails the completeness gate (still syncing / truncated). The orchestrator
    parks the step in WAITING and polls via _wait_for_video."""
    pass


VideoNotReady = _VideoNotReady


async def resume_interrupted_at_boot(
    orchestrator: "PipelineOrchestrator",
    delay_seconds: float = RESUME_SETTLE_SECONDS,
) -> dict:
    """Re-drive mixes that a restart cut off mid-pipeline.

    ``sweep_interrupted_at_boot`` parks them in "interrupted"; this puts them
    back to work. Without it a transient crash (OOM, deploy, power blip) was a
    permanent, silent loss: the mix sat there failed and the set never
    published.

    Bounded on purpose. Each automatic resume increments
    ``metadata_json[RESUME_COUNT_KEY]``; past ``MAX_INTERRUPT_RESUMES`` the mix
    is left "failed" with an explicit reason instead of being restarted again,
    so a mix that kills the process cannot loop forever. The counter is only
    ever bumped by the automatic path — a human retry is not spent against it.

    Off (config ``resume_interrupted_mixes``) the mixes stay "interrupted":
    still retryable from the API/UI and still reported by the stuck-mix
    watchdog. Nothing is ever silently dropped either way.
    """
    result = {"resumed": 0, "exhausted": 0, "skipped": False}
    from app.services import activity_log

    try:
        if not bool(await app_config.resolve("resume_interrupted_mixes")):
            result["skipped"] = True
            return result

        if delay_seconds:
            await asyncio.sleep(delay_seconds)

        async with async_session_factory() as session:
            mixes = (
                await session.execute(
                    select(Mix).where(
                        Mix.pipeline_status == StepStatus.INTERRUPTED.value
                    )
                )
            ).scalars().all()

            to_resume: List[str] = []
            exhausted: List[Tuple[str, str]] = []

            for mix in mixes:
                meta = dict(mix.metadata_json or {})
                attempts = int(meta.get(RESUME_COUNT_KEY) or 0)
                if attempts >= MAX_INTERRUPT_RESUMES:
                    mix.pipeline_status = StepStatus.FAILED.value
                    mix.pipeline_error = (
                        f"interrupted by restart {attempts} times "
                        f"(limit {MAX_INTERRUPT_RESUMES}) — not resumed again "
                        "automatically; retry manually once the cause is fixed"
                    )
                    exhausted.append((mix.id, mix.title))
                    continue

                meta[RESUME_COUNT_KEY] = attempts + 1
                mix.metadata_json = meta  # reassign: JSON columns don't track mutation
                mix.pipeline_status = StepStatus.PENDING.value
                mix.pipeline_error = None
                to_resume.append(mix.id)

            if to_resume:
                steps = (
                    await session.execute(
                        select(PipelineStep).where(
                            PipelineStep.mix_id.in_(to_resume),
                            PipelineStep.status.in_(
                                [
                                    StepStatus.INTERRUPTED.value,
                                    StepStatus.FAILED.value,
                                    StepStatus.BLOCKED.value,
                                ]
                            ),
                        )
                    )
                ).scalars().all()
                for step in steps:
                    step.status = StepStatus.PENDING.value
                    step.error = None
                    step.retry_count = (step.retry_count or 0) + 1

            await session.commit()

        for mix_id, title in exhausted:
            result["exhausted"] += 1
            logger.error(
                "Mix %s (%s) hit the interrupted-resume limit; leaving it failed",
                mix_id, title,
            )
            await activity_log.error(
                "interrupted_resume",
                f"'{title}' has been interrupted by a restart "
                f"{MAX_INTERRUPT_RESUMES} times and was NOT resumed again — "
                "something is killing the pipeline; retry it by hand once fixed",
                mix_id=mix_id,
            )

        for mix_id in to_resume:
            logger.warning("Resuming mix %s after restart interruption", mix_id)
            await activity_log.warn(
                "interrupted_resume",
                "Mix was cut off by a restart — resuming the pipeline "
                "automatically from the interrupted step",
                mix_id=mix_id,
            )
            await orchestrator.start_pipeline(mix_id)
            result["resumed"] += 1
            if RESUME_STAGGER_SECONDS:
                await asyncio.sleep(RESUME_STAGGER_SECONDS)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Boot resume of interrupted mixes failed")
    return result
