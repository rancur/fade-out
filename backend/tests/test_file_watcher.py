"""Tests for the crash-safe file-watcher 'seen' state and dispatch flow."""

import sqlite3

import pytest

from app.services.file_watcher import (
    MIN_AUDIO_FILE_BYTES,
    STATUS_DONE,
    STATUS_PROCESSING,
    FileWatcherService,
    _compute_file_hash,
    _SeenFilesDB,
    _StabilityTracker,
)

# Real audio comfortably clears the size floor; use it for dispatch fixtures.
_VALID_AUDIO = b"\0" * (MIN_AUDIO_FILE_BYTES + 16)


class TestSeenFilesDB:
    def test_processing_is_not_done(self, tmp_path):
        db = _SeenFilesDB(str(tmp_path / "seen.db"))
        db.begin_processing("h1", "/a/b.flac", "audio")
        # A file still processing must NOT be treated as done.
        assert db.is_done("h1") is False
        db.close()

    def test_mark_done(self, tmp_path):
        db = _SeenFilesDB(str(tmp_path / "seen.db"))
        db.begin_processing("h1", "/a/b.flac", "audio")
        db.mark_done("h1", "/a/b.flac", "audio")
        assert db.is_done("h1") is True
        db.close()

    def test_clear_allows_retry(self, tmp_path):
        db = _SeenFilesDB(str(tmp_path / "seen.db"))
        db.begin_processing("h1", "/a/b.flac", "audio")
        db.clear("h1")
        assert db.is_done("h1") is False
        db.close()

    def test_crash_mid_processing_reprocesses_on_restart(self, tmp_path):
        path = str(tmp_path / "seen.db")
        # First run crashes after begin_processing but before mark_done.
        db1 = _SeenFilesDB(path)
        db1.begin_processing("h1", "/a/b.flac", "audio")
        db1.close()  # simulate crash: no mark_done

        # Restart: the file is still 'processing', so it is NOT skipped.
        db2 = _SeenFilesDB(path)
        assert db2.is_done("h1") is False
        db2.mark_done("h1", "/a/b.flac", "audio")
        assert db2.is_done("h1") is True
        db2.close()

    def test_done_survives_restart(self, tmp_path):
        path = str(tmp_path / "seen.db")
        db1 = _SeenFilesDB(path)
        db1.mark_done("h1", "/a/b.flac", "audio")
        db1.close()
        db2 = _SeenFilesDB(path)
        assert db2.is_done("h1") is True
        db2.close()

    def test_legacy_schema_rows_adopted_as_done(self, tmp_path):
        path = str(tmp_path / "seen.db")
        # Build a legacy DB (no status/updated_at columns).
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE seen_files ("
            "  file_hash TEXT PRIMARY KEY,"
            "  file_path TEXT NOT NULL,"
            "  file_type TEXT NOT NULL,"
            "  seen_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO seen_files VALUES (?, ?, ?, ?)",
            ("legacy1", "/x.flac", "audio", 123.0),
        )
        conn.commit()
        conn.close()

        # Opening with the new store migrates and adopts the row as done.
        db = _SeenFilesDB(path)
        assert db.is_done("legacy1") is True
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(seen_files)")}
        assert "status" in cols and "updated_at" in cols
        db.close()


def _make_service(tmp_path) -> FileWatcherService:
    async def _noop(_path):
        return None

    service = FileWatcherService(on_audio_file=_noop, on_video_file=_noop)
    service._seen_db.close()
    service._seen_db = _SeenFilesDB(str(tmp_path / "seen.db"))
    return service


class TestCheckTrackerDispatch:
    async def test_success_marks_done_and_dedupes(self, tmp_path):
        service = _make_service(tmp_path)
        f = tmp_path / "mix.flac"
        f.write_bytes(b"a" * 2_000_000)  # >= 1 MB floor

        tracker = _StabilityTracker(0)
        tracker.update(str(f))
        tracker.update(str(f))  # 2nd observation satisfies STABILITY_CONFIRMATIONS

        calls = []

        async def cb(path):
            calls.append(path)

        await service._check_tracker(tracker, "audio", cb)
        assert calls == [str(f)]

        file_hash = _compute_file_hash(str(f))
        assert service._seen_db.is_done(file_hash) is True

        # A re-detected identical file is skipped (not processed twice).
        tracker.update(str(f))
        tracker.update(str(f))
        await service._check_tracker(tracker, "audio", cb)
        assert calls == [str(f)]
        service._seen_db.close()

    async def test_callback_failure_clears_state_for_retry(self, tmp_path):
        service = _make_service(tmp_path)
        f = tmp_path / "mix.flac"
        f.write_bytes(b"a" * 2_000_000)  # >= 1 MB floor
        file_hash = _compute_file_hash(str(f))

        tracker = _StabilityTracker(0)
        tracker.update(str(f))
        tracker.update(str(f))

        async def boom(_path):
            raise RuntimeError("processing blew up")

        # Failure must not raise out of the watcher and must not mark done.
        await service._check_tracker(tracker, "audio", boom)
        assert service._seen_db.is_done(file_hash) is False

        # Retry now succeeds and marks done.
        ok = []

        async def cb(path):
            ok.append(path)

        tracker.update(str(f))
        tracker.update(str(f))
        await service._check_tracker(tracker, "audio", cb)
        assert ok == [str(f)]
        assert service._seen_db.is_done(file_hash) is True
        service._seen_db.close()


class TestScanExistingGate:
    async def test_scan_skips_existing_by_default(self, tmp_path, monkeypatch):
        from app.services.file_watcher import FileWatcherService

        audio = tmp_path / "audio"
        audio.mkdir()
        (audio / "old.flac").write_bytes(b"x")

        seen = []

        async def _noop(_p):
            seen.append(_p)

        svc = FileWatcherService(on_audio_file=_noop, on_video_file=_noop,
                                 audio_path=str(audio), video_path=str(tmp_path / "video"))
        monkeypatch.setattr("app.services.file_watcher.settings.WATCH_INGEST_EXISTING_ON_START", False)
        await svc._scan_existing()
        # Nothing tracked -> nothing will be dispatched.
        assert svc._audio_tracker.tracked_paths == []
        svc._seen_db.close()

    async def test_scan_tracks_existing_when_enabled(self, tmp_path, monkeypatch):
        from app.services.file_watcher import FileWatcherService

        audio = tmp_path / "audio"
        video = tmp_path / "video"
        audio.mkdir(); video.mkdir()
        (audio / "old.flac").write_bytes(b"x")

        async def _noop(_p):
            return None

        svc = FileWatcherService(on_audio_file=_noop, on_video_file=_noop,
                                 audio_path=str(audio), video_path=str(video))
        monkeypatch.setattr("app.services.file_watcher.settings.WATCH_INGEST_EXISTING_ON_START", True)
        await svc._scan_existing()
        assert str(audio / "old.flac") in svc._audio_tracker.tracked_paths
        svc._seen_db.close()

    async def test_empty_or_undersized_file_is_never_dispatched(self, tmp_path):
        """A 0-byte / truncated drop must be skipped, not ingested.

        A stray ``touch`` or interrupted copy leaves an empty file that goes
        size-stable instantly; ingesting it would spin up a pipeline on
        non-audio. The guard drops it without ever invoking the callback.
        """
        service = _make_service(tmp_path)

        empty = tmp_path / "phantom.flac"
        empty.write_bytes(b"")
        tiny = tmp_path / "partial.flac"
        tiny.write_bytes(b"x" * 1024)  # 1 KB, well under the floor

        tracker = _StabilityTracker(0)
        calls = []

        async def cb(path):
            calls.append(path)

        for p in (empty, tiny):
            tracker.update(str(p))
            await service._check_tracker(tracker, "audio", cb)

        assert calls == []  # neither was dispatched
        # And nothing was recorded as processed/done for them.
        assert service._seen_db.is_done(_compute_file_hash(str(empty))) is False
        assert service._seen_db.is_done(_compute_file_hash(str(tiny))) is False
        service._seen_db.close()
