"""A tagged file must be known to the watcher BEFORE it becomes visible.

If it is promoted first, there is a window in which a watch folder holds a
file whose hash is unknown -- the watcher ingests it and re-uploads a mix that
is already published. That is the exact failure this ordering prevents.
"""
import os

from app.services import file_watcher, source_tagger


class _RecordingSeenDB:
    """Records the order of operations against real state."""

    def __init__(self):
        self.done = set()
        self.events = []
        self.close_called = False

    def mark_done(self, file_hash, file_path, file_type):
        self.events.append(("mark_done", file_hash))
        self.done.add(file_hash)

    def is_done(self, file_hash):
        return file_hash in self.done

    def close(self):
        self.close_called = True


def test_hash_is_registered_before_the_file_is_promoted(tmp_path, monkeypatch):
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")
    final = tmp_path / "x.flac"

    db = _RecordingSeenDB()
    order = []

    real_rename = os.rename

    def spy_rename(a, b):
        order.append("rename")
        return real_rename(a, b)

    monkeypatch.setattr(os, "rename", spy_rename)
    db_mark = db.mark_done

    def spy_mark(h, p, t):
        order.append("mark_done")
        return db_mark(h, p, t)

    db.mark_done = spy_mark

    source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=db)

    assert order == ["mark_done", "rename"], f"wrong order: {order}"


def test_promoted_file_hash_is_marked_done(tmp_path):
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")
    final = tmp_path / "x.flac"
    db = _RecordingSeenDB()

    expected = file_watcher.compute_file_hash(str(temp))
    source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=db)

    assert db.is_done(expected), "the watcher would re-ingest this file"
    assert final.exists()
    assert not temp.exists()


def test_promote_replaces_the_original_contents(tmp_path):
    final = tmp_path / "x.flac"
    final.write_bytes(b"original")
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")

    source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=_RecordingSeenDB())
    assert final.read_bytes() == b"tagged-content"


def test_injected_seen_db_is_not_closed(tmp_path):
    """An injected seen_db is owned by the caller and must not be closed."""
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")
    final = tmp_path / "x.flac"
    db = _RecordingSeenDB()

    source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=db)

    assert not db.close_called, "caller-injected seen_db must not be closed"


def test_rename_failure_removes_temp_file_and_reraises(tmp_path, monkeypatch):
    """If os.rename fails, the temp file is cleaned up and exception re-raised."""
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")
    final = tmp_path / "x.flac"
    db = _RecordingSeenDB()

    def failing_rename(a, b):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "rename", failing_rename)

    try:
        source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=db)
        assert False, "should have raised OSError"
    except OSError as e:
        assert "simulated rename failure" in str(e)

    # Temp file should be cleaned up
    assert not temp.exists(), "temp file should be removed on rename failure"
    # Staging dir should be empty
    assert not list(temp.parent.iterdir()), "staging dir should be empty"


def test_compute_file_hash_failure_removes_temp_file_and_reraises(tmp_path, monkeypatch):
    """A raising compute_file_hash (e.g. an I/O error reading the temp file
    off a flaky NAS mount) must not orphan a multi-gigabyte temp file --
    nothing else ever sweeps .fadeout-tagging/."""
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")
    final = tmp_path / "x.flac"
    db = _RecordingSeenDB()

    def failing_hash(path):
        raise OSError("simulated I/O error reading temp file")

    monkeypatch.setattr(file_watcher, "compute_file_hash", failing_hash)

    try:
        source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=db)
        assert False, "should have raised OSError"
    except OSError as e:
        assert "simulated I/O error" in str(e)

    assert not temp.exists(), "temp file should be removed when compute_file_hash raises"
    assert not list(temp.parent.iterdir()), "staging dir should be empty"
    assert not final.exists(), "the original must never be touched"


def test_mark_done_failure_removes_temp_file_and_reraises(tmp_path, monkeypatch):
    """A raising mark_done (sqlite3.OperationalError: database is locked is a
    live failure mode while the running watcher holds its own connection to
    the same seen_files.db) must not orphan the temp file either."""
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")
    final = tmp_path / "x.flac"
    db = _RecordingSeenDB()

    def failing_mark_done(h, p, t):
        raise RuntimeError("database is locked")

    db.mark_done = failing_mark_done

    try:
        source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=db)
        assert False, "should have raised RuntimeError"
    except RuntimeError as e:
        assert "database is locked" in str(e)

    assert not temp.exists(), "temp file should be removed when mark_done raises"
    assert not list(temp.parent.iterdir()), "staging dir should be empty"
    assert not final.exists(), "the original must never be touched"


def test_rename_failure_leaves_mark_done_row(tmp_path, monkeypatch):
    """A failed os.rename does NOT roll back the mark_done row."""
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")
    final = tmp_path / "x.flac"
    db = _RecordingSeenDB()

    expected_hash = file_watcher.compute_file_hash(str(temp))

    def failing_rename(a, b):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "rename", failing_rename)

    try:
        source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=db)
    except OSError:
        pass

    # The mark_done row is deliberately NOT rolled back
    assert db.is_done(expected_hash), "mark_done row must persist even after rename failure"
