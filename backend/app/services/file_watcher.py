"""Watchdog-based file monitoring service for audio and video files."""

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.config import settings

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".flac"}
VIDEO_EXTENSIONS = {".mkv"}
HASH_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB for dedup hash


class _SeenFilesDB:
    """SQLite-backed tracker of already-processed files to survive restarts."""

    def __init__(self, db_path: str = "data/seen_files.db") -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_files ("
            "  file_hash TEXT PRIMARY KEY,"
            "  file_path TEXT NOT NULL,"
            "  file_type TEXT NOT NULL,"
            "  seen_at REAL NOT NULL"
            ")"
        )
        self._conn.commit()

    def is_seen(self, file_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_files WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return row is not None

    def mark_seen(self, file_hash: str, file_path: str, file_type: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_files (file_hash, file_path, file_type, seen_at) "
            "VALUES (?, ?, ?, ?)",
            (file_hash, file_path, file_type, time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _compute_file_hash(path: str) -> str:
    """MD5 of first 10 MB for fast dedup."""
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        data = f.read(HASH_CHUNK_SIZE)
        md5.update(data)
    return md5.hexdigest()


class _StabilityTracker:
    """Tracks file sizes and detects when a file has stopped growing."""

    def __init__(self, stable_seconds: int) -> None:
        self._stable_seconds = stable_seconds
        # path -> (last_size, last_change_time)
        self._files: Dict[str, tuple[int, float]] = {}

    def update(self, path: str) -> None:
        """Record current file size."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        prev = self._files.get(path)
        if prev is None or prev[0] != size:
            self._files[path] = (size, time.time())
        # else size unchanged, keep existing timestamp

    def is_stable(self, path: str) -> bool:
        """Return True if file size has not changed for stable_seconds."""
        entry = self._files.get(path)
        if entry is None:
            return False
        _, last_change = entry
        return (time.time() - last_change) >= self._stable_seconds

    def remove(self, path: str) -> None:
        self._files.pop(path, None)

    @property
    def tracked_paths(self) -> list[str]:
        return list(self._files.keys())


class _WatchHandler(FileSystemEventHandler):
    """Filesystem event handler that queues new files for processing."""

    def __init__(
        self,
        allowed_extensions: set[str],
        file_type: str,
        tracker: _StabilityTracker,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__()
        self._extensions = allowed_extensions
        self._file_type = file_type
        self._tracker = tracker
        self._loop = loop

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._handle(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._handle(event.src_path)

    def _handle(self, path: str) -> None:
        ext = Path(path).suffix.lower()
        if ext not in self._extensions:
            return
        logger.debug("File activity detected: %s", path)
        self._tracker.update(path)


class FileWatcherService:
    """Watches audio and video directories and triggers callbacks on stable new files."""

    def __init__(
        self,
        on_audio_file: Callable[[str], asyncio.coroutines],
        on_video_file: Callable[[str], asyncio.coroutines],
        audio_path: Optional[str] = None,
        video_path: Optional[str] = None,
        stable_seconds: Optional[int] = None,
    ) -> None:
        self._audio_path = audio_path or settings.WATCH_AUDIO_PATH
        self._video_path = video_path or settings.WATCH_VIDEO_PATH
        self._stable_seconds = stable_seconds or settings.FILE_STABLE_SECONDS
        self._on_audio = on_audio_file
        self._on_video = on_video_file
        self._observer: Optional[Observer] = None
        self._audio_tracker = _StabilityTracker(self._stable_seconds)
        self._video_tracker = _StabilityTracker(self._stable_seconds)
        self._seen_db = _SeenFilesDB()
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start watching directories."""
        loop = asyncio.get_running_loop()

        for path in (self._audio_path, self._video_path):
            os.makedirs(path, exist_ok=True)

        self._observer = Observer()
        self._observer.schedule(
            _WatchHandler(AUDIO_EXTENSIONS, "audio", self._audio_tracker, loop),
            self._audio_path,
            recursive=False,
        )
        self._observer.schedule(
            _WatchHandler(VIDEO_EXTENSIONS, "video", self._video_tracker, loop),
            self._video_path,
            recursive=False,
        )
        self._observer.start()
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_stability())
        logger.info(
            "FileWatcher started: audio=%s video=%s stable_seconds=%d",
            self._audio_path,
            self._video_path,
            self._stable_seconds,
        )

        # Scan existing files on startup
        await self._scan_existing()

    async def _scan_existing(self) -> None:
        """Scan directories for files that arrived while we were offline."""
        for dirpath, extensions, file_type, tracker in [
            (self._audio_path, AUDIO_EXTENSIONS, "audio", self._audio_tracker),
            (self._video_path, VIDEO_EXTENSIONS, "video", self._video_tracker),
        ]:
            if not os.path.isdir(dirpath):
                continue
            for fname in os.listdir(dirpath):
                ext = Path(fname).suffix.lower()
                if ext in extensions:
                    full_path = os.path.join(dirpath, fname)
                    tracker.update(full_path)
                    logger.info("Found existing file on startup: %s", full_path)

    async def _poll_stability(self) -> None:
        """Periodically check if tracked files have become stable."""
        while self._running:
            await asyncio.sleep(10)
            await self._check_tracker(self._audio_tracker, "audio", self._on_audio)
            await self._check_tracker(self._video_tracker, "video", self._on_video)

    async def _check_tracker(
        self,
        tracker: _StabilityTracker,
        file_type: str,
        callback: Callable[[str], asyncio.coroutines],
    ) -> None:
        for path in list(tracker.tracked_paths):
            if not os.path.exists(path):
                tracker.remove(path)
                continue

            tracker.update(path)

            if not tracker.is_stable(path):
                continue

            # File is stable -- check dedup
            try:
                file_hash = await asyncio.to_thread(_compute_file_hash, path)
            except OSError as exc:
                logger.warning("Cannot hash %s: %s", path, exc)
                tracker.remove(path)
                continue

            if self._seen_db.is_seen(file_hash):
                logger.info("Skipping duplicate file: %s (hash=%s)", path, file_hash)
                tracker.remove(path)
                continue

            self._seen_db.mark_seen(file_hash, path, file_type)
            tracker.remove(path)
            logger.info("Stable new %s file detected: %s", file_type, path)

            try:
                await callback(path)
            except Exception:
                logger.exception("Error in %s callback for %s", file_type, path)

    async def stop(self) -> None:
        """Stop watching."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        self._seen_db.close()
        logger.info("FileWatcher stopped")
