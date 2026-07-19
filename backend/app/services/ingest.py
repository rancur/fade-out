"""Auto-ingest coordinator: turns watched files into pipeline runs.

The :class:`FileWatcherService` calls back with one file at a time (audio or
video) once it has become size-stable. A full mix run needs the audio (the
``detect``/``analyze`` steps require it) and, for the YouTube upload, the
matching video. This coordinator pairs the two by filename stem and starts a
single pipeline run per stem.

Pairing is done by probing both watch directories on disk rather than relying on
callback ordering: by the time either callback fires the sibling file (if any)
is already present and stable, so a drop of ``2026-07-15 Set.flac`` +
``2026-07-15 Set.mkv`` is ingested as one mix regardless of which callback the
watcher dispatches first. A per-stem guard prevents the second callback from
starting a duplicate run.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from app.config import settings
from app.database import async_session_factory
from app.models import Mix, PipelineStep
from app.services.file_watcher import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from app.services.pipeline import PIPELINE_STEPS

logger = logging.getLogger(__name__)


class IngestCoordinator:
    """Pairs watched audio/video files and kicks off one pipeline run per stem."""

    def __init__(self, orchestrator) -> None:
        self._orchestrator = orchestrator
        self._ingested_stems: set[str] = set()
        self._lock = asyncio.Lock()

    async def ingest_audio(self, path: str) -> None:
        await self._ingest(path, "audio")

    async def ingest_video(self, path: str) -> None:
        await self._ingest(path, "video")

    async def _ingest(self, path: str, kind: str) -> None:
        stem = Path(path).stem

        if kind == "audio":
            audio: Optional[str] = path
            video = self._find_sibling(settings.WATCH_VIDEO_PATH, stem, VIDEO_EXTENSIONS)
        else:
            video = path
            audio = self._find_sibling(settings.WATCH_AUDIO_PATH, stem, AUDIO_EXTENSIONS)

        # Audio is mandatory -- detect/analyze cannot run without it. A video
        # that has no matching audio yet simply waits for its audio sibling.
        if not audio:
            logger.info(
                "Ingest: video %s has no matching audio yet; waiting for audio sibling.",
                path,
            )
            return

        async with self._lock:
            if stem in self._ingested_stems:
                logger.debug("Ingest: stem %r already ingested; skipping.", stem)
                return
            self._ingested_stems.add(stem)

        try:
            await self._create_and_start(stem, audio, video)
        except Exception:
            # Roll back the guard so a transient failure can be retried on the
            # next watcher dispatch/restart instead of being lost forever.
            async with self._lock:
                self._ingested_stems.discard(stem)
            logger.exception("Ingest failed for stem %r (audio=%s video=%s)", stem, audio, video)
            raise

    @staticmethod
    def _find_sibling(directory: str, stem: str, extensions: set[str]) -> Optional[str]:
        """Return a file in *directory* whose stem matches, else None."""
        if not directory or not os.path.isdir(directory):
            return None
        for fname in os.listdir(directory):
            p = Path(fname)
            if p.stem == stem and p.suffix.lower() in extensions:
                return os.path.join(directory, fname)
        return None

    async def _create_and_start(self, stem: str, audio: str, video: Optional[str]) -> None:
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
            mix_id,
            stem,
            audio,
            video,
        )
        await self._orchestrator.start_pipeline(mix_id)
