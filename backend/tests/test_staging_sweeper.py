"""``source_tagger.sweep_staging`` reclaims orphaned ``.fadeout-tagging``
staging copies left behind when a retag run dies between
``write_tagged_copy`` and ``register_and_promote``. See
docs/superpowers/plans/2026-09-23-backfill-safe-unattended.md, Task 4.

THIS FUNCTION DELETES FILES, so these tests are weighted toward what must
NOT happen: a fresh temp that might belong to a run currently in flight must
never be removed (test_leaves_a_fresh_file_alone -- the most important test
in this file), and the guards (basename, allowlist) must refuse rather than
wipe when a caller hands it the wrong path.

FIXTURE WARNING (see task brief): both guards in ``sweep_staging`` return
the same shape (``{"refused": True, "reason": ...}``) and are checked in
order -- basename first, then the allowlist. A test meant to exercise the
allowlist guard must use a directory whose basename genuinely IS
``.fadeout-tagging`` (otherwise it silently exercises the basename guard
instead and would still pass after the allowlist check was deleted). Every
guard test below asserts on ``result["reason"]`` to make sure it reached the
guard its name claims, not just that some refusal happened.
"""

import os
import time

import pytest

from app.config import settings
from app.services import source_tagger


@pytest.fixture
def watch_dirs(tmp_path, monkeypatch):
    """Mirrors test_source_renamer.py's fixture of the same name: points
    ``source_renamer.allowed_roots()`` at throwaway directories so
    ``sweep_staging``'s allowlist check (which reads those roots live) can
    be exercised without touching anything real."""
    audio = tmp_path / "audio"
    video = tmp_path / "video"
    audio.mkdir()
    video.mkdir()
    monkeypatch.setattr(settings, "WATCH_AUDIO_PATH", str(audio))
    monkeypatch.setattr(settings, "WATCH_VIDEO_PATH", str(video))
    return audio, video


def _staging_dir(watch_dirs):
    audio, _video = watch_dirs
    staging = audio / source_tagger.TEMP_DIRNAME
    staging.mkdir()
    return staging


def _touch(path, size=1024, age_hours=None):
    path.write_bytes(b"x" * size)
    if age_hours is not None:
        mtime = time.time() - age_hours * 3600
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# Age guard -- the substance of this module
# ---------------------------------------------------------------------------

def test_file_older_than_threshold_is_removed_and_size_reported(watch_dirs):
    staging = _staging_dir(watch_dirs)
    orphan = _touch(staging / "orphan.flac", size=4096, age_hours=8)

    result = source_tagger.sweep_staging(str(staging), older_than_hours=6.0)

    assert result["refused"] is False
    assert not orphan.exists(), "the old orphan must actually be removed"
    assert result["removed_count"] == 1
    assert result["reclaimed_bytes"] == 4096
    [entry] = result["removed"]
    assert entry["path"] == str(orphan)
    assert entry["bytes"] == 4096


def test_file_newer_than_threshold_is_left_alone(watch_dirs):
    """The most important test in this file: a fresh temp may belong to a
    run currently in flight, and deleting it mid-write is the one outcome
    the sweeper must never produce."""
    staging = _staging_dir(watch_dirs)
    fresh = _touch(staging / "in-flight.flac", size=2048, age_hours=1)

    result = source_tagger.sweep_staging(str(staging), older_than_hours=6.0)

    assert fresh.exists(), "a fresh in-flight temp must never be deleted"
    assert fresh.read_bytes() == b"x" * 2048
    assert result["removed"] == []
    assert result["removed_count"] == 0
    assert result["reclaimed_bytes"] == 0
    assert any(entry["path"] == str(fresh) for entry in result["skipped"])


def test_mixed_ages_only_removes_the_old_one(watch_dirs):
    staging = _staging_dir(watch_dirs)
    old = _touch(staging / "old.flac", size=100, age_hours=10)
    fresh = _touch(staging / "fresh.flac", size=100, age_hours=0.1)

    result = source_tagger.sweep_staging(str(staging), older_than_hours=6.0)

    assert not old.exists()
    assert fresh.exists()
    assert [e["path"] for e in result["removed"]] == [str(old)]


# ---------------------------------------------------------------------------
# Basename guard
# ---------------------------------------------------------------------------

def test_non_staging_directory_is_refused_and_nothing_is_touched(watch_dirs):
    audio, _video = watch_dirs
    normal_dir = audio / "not-staging"
    normal_dir.mkdir()
    victim = _touch(normal_dir / "definitely-not-a-temp.flac", size=999, age_hours=99)

    result = source_tagger.sweep_staging(str(normal_dir), older_than_hours=6.0)

    assert result["refused"] is True
    assert "basename" in result["reason"]
    assert result["removed"] == []
    assert result["removed_count"] == 0
    assert victim.exists(), "a normal directory must never be swept, even if old"
    assert victim.read_bytes() == b"x" * 999


# ---------------------------------------------------------------------------
# Allowlist guard
# ---------------------------------------------------------------------------

def test_staging_dir_outside_allowed_roots_is_refused(watch_dirs, tmp_path):
    # Basename genuinely IS TEMP_DIRNAME -- this must reach the allowlist
    # guard specifically, not the basename guard (see FIXTURE WARNING).
    elsewhere = tmp_path / "elsewhere" / source_tagger.TEMP_DIRNAME
    elsewhere.mkdir(parents=True)
    victim = _touch(elsewhere / "orphan.flac", size=777, age_hours=99)

    result = source_tagger.sweep_staging(str(elsewhere), older_than_hours=6.0)

    assert result["refused"] is True
    assert "allowed roots" in result["reason"]
    assert result["removed"] == []
    assert victim.exists(), "a staging dir outside the watch roots must never be swept"


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------

def test_dry_run_removes_nothing_but_reports_the_same_set(watch_dirs):
    staging = _staging_dir(watch_dirs)
    old_a = _touch(staging / "a.flac", size=10, age_hours=10)
    old_b = _touch(staging / "b.flac", size=20, age_hours=20)
    fresh = _touch(staging / "c.flac", size=30, age_hours=0.1)

    live = source_tagger.sweep_staging(str(staging), older_than_hours=6.0)
    # Sanity: prove the live run above actually removed the two old files
    # and left the fresh one, before comparing it against the dry run.
    assert not (staging / "a.flac").exists()
    assert not (staging / "b.flac").exists()
    assert (staging / "c.flac").exists()

    # Re-create the same fixture for the dry run.
    old_a = _touch(staging / "a.flac", size=10, age_hours=10)
    old_b = _touch(staging / "b.flac", size=20, age_hours=20)
    fresh = _touch(staging / "c.flac", size=30, age_hours=0.1)

    dry = source_tagger.sweep_staging(str(staging), older_than_hours=6.0, dry_run=True)

    assert old_a.exists() and old_b.exists() and fresh.exists(), (
        "dry_run must remove nothing"
    )
    live_paths = {e["path"] for e in live["removed"]}
    dry_paths = {e["path"] for e in dry["removed"]}
    assert dry_paths == live_paths == {str(old_a), str(old_b)}
    assert dry["reclaimed_bytes"] == live["reclaimed_bytes"] == 30
    assert dry["dry_run"] is True


# ---------------------------------------------------------------------------
# Never-fatal contract
# ---------------------------------------------------------------------------

def test_unremovable_file_does_not_abort_the_sweep(watch_dirs, monkeypatch):
    staging = _staging_dir(watch_dirs)
    stubborn = _touch(staging / "stubborn.flac", size=50, age_hours=10)
    reclaimable = _touch(staging / "reclaimable.flac", size=60, age_hours=10)

    real_remove = os.remove

    def flaky_remove(path, *a, **kw):
        if os.path.abspath(path) == os.path.abspath(str(stubborn)):
            raise PermissionError(f"permission denied: {path}")
        return real_remove(path, *a, **kw)

    monkeypatch.setattr(os, "remove", flaky_remove)

    result = source_tagger.sweep_staging(str(staging), older_than_hours=6.0)

    assert stubborn.exists(), "the file os.remove refuses must be left in place"
    assert not reclaimable.exists(), "the other file must still be reclaimed"
    assert result["removed_count"] == 1
    assert result["removed"][0]["path"] == str(reclaimable)
    assert any(
        e["path"] == str(stubborn) and "permission denied" in e["reason"].lower()
        for e in result["skipped"]
    )


# ---------------------------------------------------------------------------
# Symlinks -- never followed, never removed
# ---------------------------------------------------------------------------

def test_symlink_in_staging_is_left_alone(watch_dirs, tmp_path):
    staging = _staging_dir(watch_dirs)
    target = tmp_path / "outside-target.flac"
    target.write_bytes(b"do not touch")
    link = staging / "link.flac"
    link.symlink_to(target)
    old_mtime = time.time() - 10 * 3600
    os.utime(str(link), (old_mtime, old_mtime), follow_symlinks=False)

    result = source_tagger.sweep_staging(str(staging), older_than_hours=6.0)

    assert link.is_symlink()
    assert target.exists()
    assert target.read_bytes() == b"do not touch"
    assert result["removed"] == []


# ---------------------------------------------------------------------------
# Nonexistent staging directory -- nothing to sweep, not a refusal
# ---------------------------------------------------------------------------

def test_nonexistent_staging_dir_is_a_noop_not_a_refusal(watch_dirs):
    audio, _video = watch_dirs
    staging = audio / source_tagger.TEMP_DIRNAME  # never created

    result = source_tagger.sweep_staging(str(staging), older_than_hours=6.0)

    assert result["refused"] is False
    assert result["removed"] == []
    assert result["removed_count"] == 0
