"""Auto-ingest coordinator: turns watched files into pipeline runs.

The :class:`FileWatcherService` calls back with one file at a time (audio or
video) once it has become size-stable. A full mix run needs the audio (the
``detect``/``analyze`` steps require it) and, for the YouTube upload, the
matching video.

**Out-of-order / different-time pairing.** Audio and video for the same set can
arrive at very different times and in either order — a 3-hour video takes far
longer to copy over SMB than its audio. A lone drop therefore does NOT run
immediately: it enters a PENDING state keyed by its recording date (or filename
stem) and waits for its sibling. Every new drop re-scans all pending entries,
and a background sweeper re-probes the disk so a sibling that appears without a
watcher callback is still paired. When both sides are present the pair runs as a
single mix. If a sibling never shows within ``PAIRING_WAIT_SECONDS`` the wait
expires: an audio-only drop still runs (a SoundCloud-only mix is a valid
outcome, YouTube simply skips), while a video-only drop is logged as
unpaired/expired and never run (audio is mandatory). Nothing is ever run "half"
before its sibling has had a fair chance to arrive, and nothing is silently
dropped.
"""

import asyncio
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

# Matches an ISO-style date token (YYYY-MM-DD) anywhere in a filename. Used as a
# robustness fallback so a DJ does not have to hand-match audio/video filenames:
# a mix recorded on a given date pairs with the video from the same date even if
# the human-readable names differ (e.g. "Twitch DJs ... (2026-07-15).flac" pairs
# with "will-see-...-2026-07-15.mkv").
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _extract_date(name: str) -> Optional[str]:
    m = _DATE_RE.search(name)
    return m.group(1) if m else None


from app.config import settings
from app.database import async_session_factory
from app.models import Mix, PipelineStep
from app.services import activity_log
from app.services.file_watcher import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from app.services.pipeline import PIPELINE_STEPS

logger = logging.getLogger(__name__)


# Module-level handle to the running coordinator so read-only surfaces (the
# /api/system/health endpoint) can inspect pending pairs without threading the
# instance through every layer.
_active_coordinator: "Optional[IngestCoordinator]" = None


def get_active_coordinator() -> "Optional[IngestCoordinator]":
    return _active_coordinator


class _PendingPair:
    """A drop waiting for its sibling."""

    __slots__ = ("audio", "video", "first_seen")

    def __init__(self) -> None:
        self.audio: Optional[str] = None
        self.video: Optional[str] = None
        self.first_seen: float = 0.0


class IngestCoordinator:
    """Pairs watched audio/video files and kicks off one pipeline run per set."""

    def __init__(self, orchestrator) -> None:
        self._orchestrator = orchestrator
        self._ingested_keys: set[str] = set()
        self._pending: Dict[str, _PendingPair] = {}
        self._lock = asyncio.Lock()
        self._sweep_task: Optional[asyncio.Task] = None
        self._running = False
        global _active_coordinator
        _active_coordinator = self

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background pairing/expiry sweeper."""
        if self._running:
            return
        self._running = True
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        self._running = False
        if self._sweep_task:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Watcher callbacks
    # ------------------------------------------------------------------

    async def ingest_audio(self, path: str) -> None:
        await self._ingest(path, "audio")

    async def ingest_video(self, path: str) -> None:
        await self._ingest(path, "video")

    async def _ingest(self, path: str, kind: str) -> None:
        key = self._pairing_key(path)

        async with self._lock:
            if key in self._ingested_keys:
                logger.debug("Ingest: key %r already ingested; skipping %s.", key, path)
                await activity_log.info(
                    "duplicate_drop",
                    f"Duplicate drop ignored (already ingested): {os.path.basename(path)}",
                    filename=os.path.basename(path),
                    context={"key": key, "kind": kind},
                )
                return
            entry = self._pending.setdefault(key, _PendingPair())
            if entry.first_seen == 0.0:
                entry.first_seen = time.time()
            if kind == "audio":
                entry.audio = path
            else:
                entry.video = path

        await activity_log.info(
            "file_ingest_received",
            f"{kind.capitalize()} drop received: {os.path.basename(path)}",
            filename=os.path.basename(path),
            context={"key": key, "kind": kind},
        )

        await self._resolve(key)
        # Re-scan every other pending entry: a newly-arrived file may be the
        # sibling something dropped earlier has been waiting for.
        await self._rescan_pending(exclude=key)

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    @staticmethod
    def _pairing_key(path: str) -> str:
        """The key that a set's audio and video must share to be paired.

        The recording date when present (so differently-named audio/video for
        the same date pair), else the filename stem.
        """
        name = os.path.basename(path)
        return _extract_date(name) or Path(name).stem

    async def _resolve(self, key: str) -> None:
        """Fill in the missing side from disk and start the run if complete."""
        async with self._lock:
            entry = self._pending.get(key)
            if entry is None or key in self._ingested_keys:
                return
            known_name = os.path.basename(entry.audio or entry.video or "")
            audio = entry.audio
            video = entry.video

        if not audio:
            audio = self._find_sibling(settings.WATCH_AUDIO_PATH, known_name, AUDIO_EXTENSIONS)
        if not video:
            video = self._find_sibling(settings.WATCH_VIDEO_PATH, known_name, VIDEO_EXTENSIONS)

        async with self._lock:
            entry = self._pending.get(key)
            if entry is None or key in self._ingested_keys:
                return
            entry.audio = entry.audio or audio
            entry.video = entry.video or video
            audio, video = entry.audio, entry.video

        if audio and video:
            await self._try_start(key, audio, video, paired=True)
        elif audio:
            await activity_log.info(
                "pending_waiting",
                f"Audio waiting for its video sibling: {os.path.basename(audio)}",
                filename=os.path.basename(audio),
                context={"key": key, "have": "audio", "waiting_for": "video"},
            )
        elif video:
            await activity_log.info(
                "pending_waiting",
                f"Video waiting for its audio sibling: {os.path.basename(video)}",
                filename=os.path.basename(video),
                context={"key": key, "have": "video", "waiting_for": "audio"},
            )

    async def _rescan_pending(self, exclude: Optional[str] = None) -> None:
        async with self._lock:
            keys = [k for k in self._pending if k != exclude]
        for k in keys:
            await self._resolve(k)

    @classmethod
    def _find_sibling(cls, directory: str, source_name: str, extensions: set) -> Optional[str]:
        """Find the file in *directory* that pairs with *source_name*.

        Pairing strategy, most-specific first:
          1. Exact stem match (``2026-07-15 Set.flac`` <-> ``2026-07-15 Set.mkv``).
          2. Date fallback: a file whose name contains the same ``YYYY-MM-DD``
             token as *source_name*. If several files share the date, the most
             recently modified wins (and a warning is logged) so an old
             back-catalog file never shadows the fresh drop.
        Returns an absolute path, or ``None`` if nothing pairs.
        """
        if not source_name or not directory or not os.path.isdir(directory):
            return None

        source_stem = Path(source_name).stem
        candidates: List[str] = []
        for fname in os.listdir(directory):
            p = Path(fname)
            if p.suffix.lower() not in extensions:
                continue
            if p.stem == source_stem:
                return os.path.join(directory, fname)
            candidates.append(fname)

        source_date = _extract_date(source_name)
        if not source_date:
            return None
        dated = [
            os.path.join(directory, f)
            for f in candidates
            if _extract_date(f) == source_date
        ]
        if not dated:
            return None
        if len(dated) > 1:
            dated.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            logger.warning(
                "Ingest: %d files in %s match date %s for %r; pairing newest (%s).",
                len(dated), directory, source_date, source_name, os.path.basename(dated[0]),
            )
        return dated[0]

    # ------------------------------------------------------------------
    # Expiry sweeper
    # ------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(settings.PAIRING_SWEEP_INTERVAL_SECONDS)
                await self._sweep_once()
            except asyncio.CancelledError:
                break
            except Exception:  # pragma: no cover - defensive
                logger.exception("Ingest sweep loop error")

    async def _sweep_once(self) -> None:
        async with self._lock:
            keys = list(self._pending.keys())
        for key in keys:
            # Re-probe disk in case a sibling appeared without a callback.
            await self._resolve(key)
            async with self._lock:
                entry = self._pending.get(key)
                if entry is None or key in self._ingested_keys:
                    continue
                age = time.time() - entry.first_seen
                if age < settings.PAIRING_WAIT_SECONDS:
                    continue
                audio, video = entry.audio, entry.video

            if audio:
                await activity_log.warn(
                    "pairing_expired_audio_only",
                    (
                        f"No video sibling for {os.path.basename(audio)} within "
                        f"{settings.PAIRING_WAIT_SECONDS}s; proceeding audio-only."
                    ),
                    filename=os.path.basename(audio),
                    context={"key": key, "waited_seconds": settings.PAIRING_WAIT_SECONDS},
                )
                await self._try_start(key, audio, None, paired=False)
            else:
                async with self._lock:
                    self._pending.pop(key, None)
                await activity_log.error(
                    "pairing_expired_unpaired",
                    (
                        f"Video {os.path.basename(video or key)} expired unpaired: no "
                        f"audio arrived within {settings.PAIRING_WAIT_SECONDS}s. Not run "
                        f"(audio is required)."
                    ),
                    filename=os.path.basename(video) if video else None,
                    context={"key": key, "waited_seconds": settings.PAIRING_WAIT_SECONDS},
                )

    # ------------------------------------------------------------------
    # Start a run
    # ------------------------------------------------------------------

    async def _try_start(
        self, key: str, audio: str, video: Optional[str], paired: bool
    ) -> None:
        # Disk-space guard: never half-ingest a multi-GB set into a full volume.
        if not self._disk_ok():
            await activity_log.error(
                "disk_low",
                (
                    f"Refusing to ingest {os.path.basename(audio)}: free space on the "
                    f"output volume is below {settings.MIN_FREE_DISK_GB} GB. Left pending "
                    f"for retry."
                ),
                filename=os.path.basename(audio),
                context={"key": key},
            )
            return  # leave pending; the sweeper will retry once space frees up

        async with self._lock:
            if key in self._ingested_keys:
                return
            self._ingested_keys.add(key)
            self._pending.pop(key, None)

        stem = Path(audio).stem
        try:
            mix_id = await self._create_and_start(stem, audio, video)
        except Exception:
            async with self._lock:
                self._ingested_keys.discard(key)
            logger.exception("Ingest failed for key %r (audio=%s video=%s)", key, audio, video)
            await activity_log.error(
                "ingest_failed",
                f"Failed to create/start mix for {os.path.basename(audio)}",
                filename=os.path.basename(audio),
                context={"key": key},
            )
            raise
        else:
            await activity_log.info(
                "mix_created",
                (
                    f"Mix created from {'paired audio+video' if video else 'audio-only'} "
                    f"drop: {stem}"
                ),
                mix_id=mix_id,
                filename=os.path.basename(audio),
                context={
                    "key": key,
                    "audio": os.path.basename(audio),
                    "video": os.path.basename(video) if video else None,
                    "paired": paired,
                },
            )

    @staticmethod
    def _disk_ok() -> bool:
        try:
            path = settings.OUTPUT_COVER_ART_PATH
            usage = shutil.disk_usage(path if os.path.isdir(path) else "/")
            return (usage.free / (1024**3)) >= settings.MIN_FREE_DISK_GB
        except Exception:  # pragma: no cover - defensive
            return True  # never block ingest on an unreadable disk stat

    async def _create_and_start(
        self, stem: str, audio: str, video: Optional[str]
    ) -> str:
        mix_id = str(uuid4())
        async with async_session_factory() as session:
            mix = Mix(
                id=mix_id,
                title=stem,
                audio_file_path=audio,
                video_file_path=video,
                pipeline_status="pending",
                pipeline_started_at=datetime.now(timezone.utc),
            )
            session.add(mix)
            for name in PIPELINE_STEPS:
                if name == "complete":
                    continue
                session.add(PipelineStep(mix_id=mix_id, step_name=name, status="pending"))
            await session.commit()

        logger.info(
            "Auto-ingest: created mix %s from stem %r (audio=%s video=%s); starting pipeline.",
            mix_id, stem, audio, video,
        )
        await self._orchestrator.start_pipeline(mix_id)
        return mix_id

    # ------------------------------------------------------------------
    # Introspection (health endpoint)
    # ------------------------------------------------------------------

    def pending_snapshot(self) -> List[dict]:
        now = time.time()
        out: List[dict] = []
        for key, entry in self._pending.items():
            out.append(
                {
                    "key": key,
                    "has_audio": entry.audio is not None,
                    "has_video": entry.video is not None,
                    "waiting_for": "video" if entry.audio and not entry.video
                    else "audio" if entry.video and not entry.audio
                    else None,
                    "age_seconds": round(now - entry.first_seen, 1) if entry.first_seen else 0,
                }
            )
        return out
