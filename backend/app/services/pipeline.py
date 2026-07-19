"""Pipeline orchestrator -- state-machine that drives each mix through all steps."""

import asyncio
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory
from app.models import Mix, PipelineStep

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

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 5
VIDEO_POLL_INTERVAL = 300  # 5 minutes


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING = "waiting"
    PAUSED = "paused"


StepHandler = Callable[[str, AsyncSession], Coroutine[Any, Any, Optional[dict]]]


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
                await activity_log.error(
                    "pipeline_error",
                    f"Pipeline error at step {stage}: {data.get('error', 'failed')}",
                    mix_id=mix_id,
                    stage=stage,
                    context={"retryable": True, **{k: v for k, v in data.items() if k != "step"}},
                )
        except Exception:  # pragma: no cover - defensive
            logger.debug("activity mirror failed for %s", event_type, exc_info=True)

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

            for step_name in PIPELINE_STEPS:
                if step_name == "complete":
                    await self._mark_complete(mix_id)
                    break

                success = await self._execute_step(mix_id, step_name)
                if not success:
                    await self._mark_failed(mix_id, step_name)
                    return

                # Draft mode pause after generate_art
                if settings.DRAFT_MODE and step_name == "generate_art":
                    logger.info("DRAFT_MODE: pausing pipeline for mix %s after generate_art", mix_id)
                    await self._set_mix_status(mix_id, "draft_review", step_name)
                    await self._emit("draft_ready", mix_id)
                    return

            logger.info("Pipeline completed for mix %s", mix_id)

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

        # Find next step after current
        try:
            idx = PIPELINE_STEPS.index(current_step)
        except ValueError:
            logger.error("Unknown step %s for mix %s", current_step, mix_id)
            return

        remaining_steps = PIPELINE_STEPS[idx + 1:]
        if not remaining_steps:
            return

        task = asyncio.create_task(self._run_remaining(mix_id, remaining_steps))
        self._running_pipelines[mix_id] = task
        task.add_done_callback(lambda _t: self._running_pipelines.pop(mix_id, None))

    async def _run_remaining(self, mix_id: str, steps: List[str]) -> None:
        async with self._semaphore:
            await self._set_mix_status(mix_id, "running")
            for step_name in steps:
                if step_name == "complete":
                    await self._mark_complete(mix_id)
                    break

                success = await self._execute_step(mix_id, step_name)
                if not success:
                    await self._mark_failed(mix_id, step_name)
                    return

    async def retry_step(self, mix_id: str, step_name: str) -> bool:
        """Retry a specific failed step, then continue pipeline."""
        success = await self._execute_step(mix_id, step_name, force=True)
        if success:
            # Continue from next step
            try:
                idx = PIPELINE_STEPS.index(step_name)
            except ValueError:
                return success
            remaining = PIPELINE_STEPS[idx + 1:]
            if remaining:
                task = asyncio.create_task(self._run_remaining(mix_id, remaining))
                self._running_pipelines[mix_id] = task
                task.add_done_callback(lambda _t: self._running_pipelines.pop(mix_id, None))
        return success

    async def _execute_step(
        self, mix_id: str, step_name: str, force: bool = False
    ) -> bool:
        """Execute a single step with retries and exponential backoff."""
        while self._paused:
            await asyncio.sleep(2)

        handler = self._handlers.get(step_name)
        if handler is None:
            logger.warning("No handler registered for step %s, skipping", step_name)
            await self._record_step(mix_id, step_name, StepStatus.SKIPPED)
            return True

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
                    return True

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

            try:
                async with async_session_factory() as session:
                    output = await handler(mix_id, session)
                    await session.commit()

                elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                await self._record_step(
                    mix_id, step_name, StepStatus.COMPLETED,
                    started_at=started_at, output=output, retry_count=attempt - 1,
                )
                await self._emit("step_completed", mix_id, {
                    "step": step_name, "elapsed_seconds": elapsed,
                })
                logger.info("Step %s completed for mix %s in %.1fs", step_name, mix_id, elapsed)
                return True

            except _VideoNotReady:
                logger.info("Video not ready for mix %s at step %s, entering wait", mix_id, step_name)
                await self._record_step(
                    mix_id, step_name, StepStatus.WAITING,
                    started_at=started_at, retry_count=attempt - 1,
                )
                resolved = await self._wait_for_video(mix_id)
                if resolved:
                    # Re-attempt after video found
                    continue
                else:
                    await self._record_step(
                        mix_id, step_name, StepStatus.FAILED,
                        started_at=started_at, error="Video file never appeared",
                        retry_count=attempt - 1,
                    )
                    return False

            except Exception as exc:
                logger.exception(
                    "Step %s failed for mix %s (attempt %d): %s",
                    step_name, mix_id, attempt, exc,
                )
                if attempt < MAX_RETRIES:
                    backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.info("Retrying step %s in %ds", step_name, backoff)
                    try:
                        from app.services import activity_log

                        await activity_log.warn(
                            "step_retry",
                            f"Step {step_name} failed (attempt {attempt}/{MAX_RETRIES}): "
                            f"{exc}. Retrying in {backoff}s.",
                            mix_id=mix_id,
                            stage=step_name,
                            context={"attempt": attempt, "backoff_seconds": backoff},
                        )
                    except Exception:  # pragma: no cover - defensive
                        pass
                    await asyncio.sleep(backoff)
                else:
                    await self._record_step(
                        mix_id, step_name, StepStatus.FAILED,
                        started_at=started_at, error=str(exc),
                        retry_count=attempt - 1,
                    )
                    await self._emit("error", mix_id, {
                        "step": step_name, "error": str(exc),
                    })
                    return False

        return False

    async def _wait_for_video(self, mix_id: str, max_checks: int = 48) -> bool:
        """Poll for video file availability (default: check every 5min up to 4 hours)."""
        for i in range(max_checks):
            await asyncio.sleep(VIDEO_POLL_INTERVAL)
            async with async_session_factory() as session:
                mix = await session.get(Mix, mix_id)
                if mix and mix.video_file_path and os.path.exists(mix.video_file_path):
                    logger.info("Video file found for mix %s: %s", mix_id, mix.video_file_path)
                    return True
            logger.debug("Video check %d/%d for mix %s -- not found yet", i + 1, max_checks, mix_id)
        return False

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
        async with async_session_factory() as session:
            step = PipelineStep(
                mix_id=mix_id,
                step_name=step_name,
                status=status.value,
                started_at=started_at or datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc) if status in (
                    StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED
                ) else None,
                output_json=output,
                error=error,
                retry_count=retry_count,
            )
            session.add(step)
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

    async def _mark_complete(self, mix_id: str) -> None:
        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            if mix:
                mix.pipeline_status = "completed"
                mix.pipeline_step = "complete"
                mix.pipeline_completed_at = datetime.now(timezone.utc)
                await session.commit()
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


class _VideoNotReady(Exception):
    """Raised by upload_youtube handler when video file is missing."""
    pass


VideoNotReady = _VideoNotReady
