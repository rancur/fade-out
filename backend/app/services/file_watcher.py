"""Watchdog-based file monitoring service for audio and video files."""

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.config import settings

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".flac", ".wav"}
VIDEO_EXTENSIONS = {".mkv"}
HASH_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB for dedup hash

# Minimum size for a file to be considered a real recording. A genuine mix is
# always hundreds of MB to several GB; anything below this floor is an empty or
# truncated artifact -- a stray ``touch``, an interrupted SMB copy, or a
# name-normalized phantom sitting next to the real drop -- and must never be
# ingested. Such a file goes "stable" instantly (its size never changes) and
# would otherwise spin up a full pipeline on non-audio. Ingest itself never
# modifies the source -- it only ever opens it for reading. The single exception
# is the opt-in ``rename_source_files`` feature (see ``source_renamer``), which
# renames a finished mix's sources in place; a rename cannot change a file's
# dedupe hash, so it can never cause a re-ingest. This guard is purely about not
# acting on a garbage/empty file.
MIN_AUDIO_FILE_BYTES = 1024 * 1024  # 1 MB floor


async def _safe_activity(level: str, event: str, message: str, **kwargs) -> None:
    """Emit an activity-log entry without ever letting a logging failure break
    the watcher. Imported lazily to keep this low-level module dependency-light.
    """
    try:
        from app.services import activity_log

        await activity_log.log(level, event, message, **kwargs)
    except Exception:  # pragma: no cover - defensive
        logger.debug("activity emit failed for %s", event, exc_info=True)


STATUS_PROCESSING = "processing"
STATUS_DONE = "done"


class _SeenFilesDB:
    """Durable, crash-safe tracker of processed files.

    A file moves through two states:

    * ``processing`` — recorded *before* the callback runs, so a crash mid-scan
      leaves a durable breadcrumb.
    * ``done`` — recorded only *after* the callback returns successfully.

    On restart only ``done`` files are skipped. A file left in ``processing`` by
    a crash (or whose callback raised) is therefore re-processed rather than
    silently dropped, and a genuinely-finished file is never processed twice.

    Durability is provided by SQLite in WAL mode with ``synchronous=FULL`` and a
    single atomic ``INSERT OR REPLACE`` commit per transition — no partial or
    lost state across a crash.
    """

    def __init__(self, db_path: str = "data/seen_files.db") -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # Crash-safe durability: WAL survives a process kill, FULL sync flushes
        # each commit to disk before returning.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_files ("
            "  file_hash TEXT PRIMARY KEY,"
            "  file_path TEXT NOT NULL,"
            "  file_type TEXT NOT NULL,"
            "  status TEXT NOT NULL DEFAULT 'done',"
            "  updated_at REAL NOT NULL"
            ")"
        )
        self._migrate_legacy_schema()
        self._conn.commit()

    def _migrate_legacy_schema(self) -> None:
        """Add the status/updated_at columns to a pre-existing legacy table.

        Older DBs only had (file_hash, file_path, file_type, seen_at) and treated
        any present row as processed; those rows are adopted as ``done``.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(seen_files)")}
        if not cols:
            return
        if "status" not in cols:
            self._conn.execute(
                "ALTER TABLE seen_files ADD COLUMN status TEXT NOT NULL DEFAULT 'done'"
            )
        if "updated_at" not in cols:
            self._conn.execute("ALTER TABLE seen_files ADD COLUMN updated_at REAL")
            self._conn.execute(
                "UPDATE seen_files SET updated_at = COALESCE(updated_at, ?)",
                (time.time(),),
            )

    def is_done(self, file_hash: str) -> bool:
        """True only when the file has been fully processed."""
        row = self._conn.execute(
            "SELECT 1 FROM seen_files WHERE file_hash = ? AND status = ?",
            (file_hash, STATUS_DONE),
        ).fetchone()
        return row is not None

    def begin_processing(self, file_hash: str, file_path: str, file_type: str) -> None:
        """Durably record that processing has started (before the callback)."""
        self._set_status(file_hash, file_path, file_type, STATUS_PROCESSING)

    def mark_done(self, file_hash: str, file_path: str, file_type: str) -> None:
        """Durably record successful processing (after the callback)."""
        self._set_status(file_hash, file_path, file_type, STATUS_DONE)

    def clear(self, file_hash: str) -> None:
        """Drop a record so a failed file is retried on the next scan/restart."""
        self._conn.execute("DELETE FROM seen_files WHERE file_hash = ?", (file_hash,))
        self._conn.commit()

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Follow a source file that ``source_renamer`` moved.

        Cosmetic only -- dedupe is keyed on ``file_hash``, which a rename cannot
        change, so a stale path here can never cause a re-ingest. Kept current
        anyway so the forensic trail in this table stays honest.
        """
        self._conn.execute(
            "UPDATE seen_files SET file_path = ? WHERE file_path = ?",
            (new_path, old_path),
        )
        self._conn.commit()

    def _set_status(
        self, file_hash: str, file_path: str, file_type: str, status: str
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO seen_files "
            "(file_hash, file_path, file_type, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_hash, file_path, file_type, status, time.time()),
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


def compute_file_hash(path: str) -> str:
    """Public alias of the dedupe hash.

    ``source_tagger`` must compute the byte-identical value, because writing
    FLAC tags changes the file head and therefore this hash; the tagger
    re-registers the new hash so the watcher does not treat a tagged file as a
    new recording and re-ingest an already-published mix. Keep this and
    ``_compute_file_hash`` the same function, not merely similar.
    """
    return _compute_file_hash(path)


def open_seen_files_db(db_path: str = "data/seen_files.db") -> "_SeenFilesDB":
    """Open the dedupe database the watcher uses.

    Exposed so the tagger can register a tagged file's new hash against the
    same table the watcher reads.
    """
    return _SeenFilesDB(db_path)


class _StabilityTracker:
    """Tracks file sizes and detects when a file has stopped growing.

    Stability requires BOTH a quiet period (size unchanged for
    ``stable_seconds``) AND a minimum number of consecutive same-size
    observations (``confirmations``). The confirmation count guards against a
    slow SMB copy that briefly pauses mid-write being mistaken for a finished
    file. ``last_size`` is retained so callers can enforce a minimum-size floor.
    """

    def __init__(self, stable_seconds: int, confirmations: int = 2) -> None:
        self._stable_seconds = stable_seconds
        self._confirmations = max(1, confirmations)
        # path -> (last_size, last_change_time, stable_observations)
        self._files: Dict[str, tuple[int, float, int]] = {}

    def update(self, path: str) -> None:
        """Record current file size."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        prev = self._files.get(path)
        if prev is None or prev[0] != size:
            # New or grew/shrank: reset the timer and the confirmation counter.
            self._files[path] = (size, time.time(), 1)
        else:
            # Size held steady: bump the consecutive-observation counter.
            self._files[path] = (prev[0], prev[1], prev[2] + 1)

    def size(self, path: str) -> Optional[int]:
        entry = self._files.get(path)
        return entry[0] if entry else None

    def is_stable(self, path: str) -> bool:
        """True once size has held for stable_seconds AND enough confirmations."""
        entry = self._files.get(path)
        if entry is None:
            return False
        _, last_change, observations = entry
        return (
            (time.time() - last_change) >= self._stable_seconds
            and observations >= self._confirmations
        )

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


# Module-level handle to the running watcher, mirroring
# ``ingest.get_active_coordinator``. Lets ``source_renamer`` keep the seen-files
# record in step with a rename without threading the instance through the
# pipeline.
_active_watcher: "Optional[FileWatcherService]" = None


def get_active_watcher() -> "Optional[FileWatcherService]":
    return _active_watcher


class FileWatcherService:
    """Watches audio and video directories and triggers callbacks on stable new files."""

    def __init__(
        self,
        on_audio_file: Callable[[str], Awaitable[None]],
        on_video_file: Callable[[str], Awaitable[None]],
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
        self._audio_tracker = _StabilityTracker(
            self._stable_seconds, settings.STABILITY_CONFIRMATIONS
        )
        self._video_tracker = _StabilityTracker(
            self._stable_seconds, settings.STABILITY_CONFIRMATIONS
        )
        self._seen_db = _SeenFilesDB()
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    def note_renamed(self, old_path: str, new_path: str) -> None:
        """Point this watcher's seen-files record at a renamed source file."""
        self._seen_db.rename_path(old_path, new_path)

    async def start(self) -> None:
        """Start watching directories."""
        loop = asyncio.get_running_loop()
        global _active_watcher
        _active_watcher = self

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
        """Scan directories for files that arrived while we were offline.

        Gated behind ``WATCH_INGEST_EXISTING_ON_START`` (default off): the watch
        folders permanently hold a back-catalog, so sweeping them on startup
        would auto-ingest the entire history. When disabled we only react to
        files created/modified after the watcher is running.
        """
        if not settings.WATCH_INGEST_EXISTING_ON_START:
            logger.info(
                "Skipping startup scan of existing files "
                "(WATCH_INGEST_EXISTING_ON_START=False); watching for new drops only."
            )
            return
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
        callback: Callable[[str], Awaitable[None]],
    ) -> None:
        for path in list(tracker.tracked_paths):
            if not os.path.exists(path):
                tracker.remove(path)
                continue

            tracker.update(path)

            if not tracker.is_stable(path):
                continue

            # Partial-file / stray-file safety: the file has stopped growing,
            # but reject anything under the minimum-size floor outright. This
            # kills the 0-byte / stray-file class (a real set is always well
            # over a megabyte) so a half-written or junk drop is never ingested.
            size = tracker.size(path)
            if size is not None and size < settings.MIN_FILE_SIZE_BYTES:
                logger.warning(
                    "Skipping undersized %s file (%d bytes < %d floor): %s",
                    file_type, size, settings.MIN_FILE_SIZE_BYTES, path,
                )
                await _safe_activity(
                    "warn",
                    "file_skipped",
                    f"Skipped undersized {file_type} file "
                    f"({size} bytes < {settings.MIN_FILE_SIZE_BYTES}): {os.path.basename(path)}",
                    filename=os.path.basename(path),
                    context={"size_bytes": size, "reason": "undersized"},
                )
                tracker.remove(path)
                continue

            # File is stable -- check dedup
            try:
                file_hash = await asyncio.to_thread(_compute_file_hash, path)
            except OSError as exc:
                logger.warning("Cannot hash %s: %s", path, exc)
                tracker.remove(path)
                continue

            if self._seen_db.is_done(file_hash):
                logger.info("Skipping already-processed file: %s (hash=%s)", path, file_hash)
                await _safe_activity(
                    "info",
                    "file_skipped",
                    f"Skipped already-processed {file_type} file: {os.path.basename(path)}",
                    filename=os.path.basename(path),
                    context={"reason": "duplicate"},
                )
                tracker.remove(path)
                continue

            await _safe_activity(
                "info",
                "file_detected",
                f"Stable new {file_type} file detected: {os.path.basename(path)}",
                filename=os.path.basename(path),
                context={"file_type": file_type, "size_bytes": size},
            )

            # Durably mark 'processing' BEFORE the callback and drop it from the
            # stability tracker so it is dispatched exactly once. If we crash
            # mid-callback the record stays 'processing' (not 'done'), so the
            # next startup scan re-processes it instead of losing the file.
            self._seen_db.begin_processing(file_hash, path, file_type)
            tracker.remove(path)
            logger.info("Stable new %s file detected: %s", file_type, path)

            try:
                await callback(path)
            except Exception:
                logger.exception("Error in %s callback for %s", file_type, path)
                # Not done: clear the record so it is retried next scan/restart.
                self._seen_db.clear(file_hash)
            else:
                self._seen_db.mark_done(file_hash, path, file_type)

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
        global _active_watcher
        if _active_watcher is self:
            _active_watcher = None
        logger.info("FileWatcher stopped")
