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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
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
        trigger_name = Path(path).name

        if kind == "audio":
            audio: Optional[str] = path
            video = self._find_sibling(settings.WATCH_VIDEO_PATH, trigger_name, VIDEO_EXTENSIONS)
        else:
            video = path
            audio = self._find_sibling(settings.WATCH_AUDIO_PATH, trigger_name, AUDIO_EXTENSIONS)

        # Audio is mandatory -- detect/analyze cannot run without it. A video
        # that has no matching audio yet simply waits for its audio sibling.
        if not audio:
            logger.info(
                "Ingest: video %s has no matching audio yet; waiting for audio sibling.",
                path,
            )
            return

        # Key the dedup guard (and the mix title) on the *audio* stem, not the
        # triggering file's stem. When audio and video have different names but
        # pair via the date fallback, both the audio- and video-triggered
        # callbacks resolve to the same audio, so keying on the audio stem
        # prevents a duplicate mix being created from the second callback.
        stem = Path(audio).stem

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

    @classmethod
    def _find_sibling(cls, directory: str, source_name: str, extensions: set[str]) -> Optional[str]:
        """Find the file in *directory* that pairs with *source_name*.

        Pairing strategy, most-specific first:
          1. Exact stem match (``2026-07-15 Set.flac`` <-> ``2026-07-15 Set.mkv``).
          2. Date fallback: a file whose name contains the same ``YYYY-MM-DD``
             token as *source_name*. This lets a DJ drop differently-named audio
             and video for the same recording date without hand-matching stems.
             If several files share the date, the most recently modified wins
             (and a warning is logged) so an old back-catalog file never
             shadows the fresh drop.
        Returns an absolute path, or ``None`` if nothing pairs.
        """
        if not directory or not os.path.isdir(directory):
            return None

        source_stem = Path(source_name).stem
        candidates: list[str] = []
        for fname in os.listdir(directory):
            p = Path(fname)
            if p.suffix.lower() not in extensions:
                continue
            # 1. Exact stem match short-circuits -- preserves prior behavior.
            if p.stem == source_stem:
                return os.path.join(directory, fname)
            candidates.append(fname)

        # 2. Date fallback.
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
        else:
            logger.info(
                "Ingest: paired %r with %s via date fallback (%s).",
                source_name, os.path.basename(dated[0]), source_date,
            )
        return dated[0]

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
