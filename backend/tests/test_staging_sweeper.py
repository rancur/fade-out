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

CTIME WARNING: the age guard now ages off the MORE RECENT of
``st_mtime``/``st_ctime`` (see the fix for the bug this file's tests missed
originally -- ``shutil.copy2`` carries the SOURCE's mtime onto a staged
copy, so mtime alone is not trustworthy). ``os.utime`` can backdate mtime,
but ctime cannot be backdated through any public API -- calling
``os.utime`` on a path still bumps THAT path's real ctime to the moment of
the call, regardless of what mtime/atime values are passed. Tests below
that need a file to look genuinely old therefore use the ``fake_stat``
fixture (which substitutes a controlled ``st_ctime`` at the exact
``os.stat`` call ``sweep_staging`` makes) rather than relying on
``os.utime`` alone for that. Tests that only need a genuinely FRESH file
don't need it -- a file's real ctime is fresh at creation time regardless.
"""

import os
import time
from types import SimpleNamespace

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


@pytest.fixture
def fake_stat(monkeypatch):
    """Lets a test give any path a controlled ``st_ctime`` for the purposes
    of ``sweep_staging``'s age guard (see the CTIME WARNING above for why
    this exists instead of just backdating with ``os.utime``).

    ``sweep_staging`` calls ``os.stat(path, follow_symlinks=False)`` as a
    plain module-level call specifically so this is possible to intercept;
    every field other than the ones a test explicitly overrides passes
    through untouched from the real ``os.stat``.

    Returns a ``set_ctime(path, hours_ago)`` function; call it for any path
    that needs a fabricated age.
    """
    overrides = {}
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        real = real_stat(path, *args, **kwargs)
        key = os.path.normpath(os.fspath(path))
        if key in overrides:
            return SimpleNamespace(
                st_mtime=real.st_mtime,
                st_ctime=overrides[key],
                st_size=real.st_size,
            )
        return real

    monkeypatch.setattr(os, "stat", _stat)

    def _set_ctime(path, hours_ago):
        overrides[os.path.normpath(str(path))] = time.time() - hours_ago * 3600

    return _set_ctime


def _staging_dir(watch_dirs):
    audio, _video = watch_dirs
    staging = audio / source_tagger.TEMP_DIRNAME
    staging.mkdir()
    return staging


def _touch(path, size=1024, age_hours=None, fake_stat=None):
    """Write ``path`` and, if ``age_hours`` is given, backdate its mtime.

    If ``fake_stat`` (the fixture above) is also given, the SAME age is
    applied to the path's effective ``st_ctime`` too -- simulating a
    genuinely old file (both timestamps old), as opposed to the
    copy2-staged-with-an-old-mtime scenario, which
    ``test_copy_staged_from_an_old_mtime_source_is_left_alone_while_fresh``
    below exercises directly through the real ``write_tagged_copy``.
    """
    path.write_bytes(b"x" * size)
    if age_hours is not None:
        mtime = time.time() - age_hours * 3600
        os.utime(path, (mtime, mtime))
        if fake_stat is not None:
            fake_stat(path, age_hours)
    return path


# ---------------------------------------------------------------------------
# Age guard -- the substance of this module
# ---------------------------------------------------------------------------

def test_file_older_than_threshold_is_removed_and_size_reported(watch_dirs, fake_stat):
    staging = _staging_dir(watch_dirs)
    orphan = _touch(staging / "orphan.flac", size=4096, age_hours=8, fake_stat=fake_stat)

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


def test_mixed_ages_only_removes_the_old_one(watch_dirs, fake_stat):
    staging = _staging_dir(watch_dirs)
    old = _touch(staging / "old.flac", size=100, age_hours=10, fake_stat=fake_stat)
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

def test_dry_run_removes_nothing_but_reports_the_same_set(watch_dirs, fake_stat):
    staging = _staging_dir(watch_dirs)
    old_a = _touch(staging / "a.flac", size=10, age_hours=10, fake_stat=fake_stat)
    old_b = _touch(staging / "b.flac", size=20, age_hours=20, fake_stat=fake_stat)
    fresh = _touch(staging / "c.flac", size=30, age_hours=0.1)

    live = source_tagger.sweep_staging(str(staging), older_than_hours=6.0)
    # Sanity: prove the live run above actually removed the two old files
    # and left the fresh one, before comparing it against the dry run.
    assert not (staging / "a.flac").exists()
    assert not (staging / "b.flac").exists()
    assert (staging / "c.flac").exists()

    # Re-create the same fixture for the dry run.
    old_a = _touch(staging / "a.flac", size=10, age_hours=10, fake_stat=fake_stat)
    old_b = _touch(staging / "b.flac", size=20, age_hours=20, fake_stat=fake_stat)
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

def test_unremovable_file_does_not_abort_the_sweep(watch_dirs, monkeypatch, fake_stat):
    staging = _staging_dir(watch_dirs)
    stubborn = _touch(staging / "stubborn.flac", size=50, age_hours=10, fake_stat=fake_stat)
    reclaimable = _touch(
        staging / "reclaimable.flac", size=60, age_hours=10, fake_stat=fake_stat
    )

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


# ---------------------------------------------------------------------------
# Blocker 4 -- the test that would have caught the real bug: a staging copy
# through the ACTUAL write_tagged_copy path, from a genuinely old-mtime
# source, must not be swept while it's fresh.
# ---------------------------------------------------------------------------

def _make_real_flac(path, duration=1):
    """A real, tiny, valid FLAC via ffmpeg -- same pattern as
    test_source_tagger_idempotent.py's ``_make_flac``. Needed here (rather
    than the synthetic ``b"x" * size`` fixtures the rest of this file uses)
    because this test exercises ``write_tagged_copy`` for real, and that
    function opens the copy with mutagen."""
    import subprocess
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", str(duration), "-c:a", "flac", "-y", str(path)],
        check=True, capture_output=True,
    )


# These two tests split what was originally ONE test
# (``test_copy_staged_from_an_old_mtime_source_survives_a_concurrent_sweep``)
# covering the combined fix: with BOTH halves present, or with only the
# ctime half present (the copy's real ctime is naturally fresh moments
# after ``shutil.copy2``, regardless of the ``os.utime`` stamp), the
# combined test passed identically -- so a mutation reverting only the
# ``os.utime`` stamp in ``write_tagged_copy`` left it green. Each test below
# defeats the OTHER half so only the one it names can save the file.

def test_write_tagged_copy_mtime_stamp_survives_a_stale_ctime_view(
    watch_dirs, monkeypatch, fake_stat
):
    """Isolates ``write_tagged_copy``'s own ``os.utime(temp_path, None)``
    stamp from ``sweep_staging``'s ctime-aware aging (see the module
    docstring's CTIME WARNING).

    ``fake_stat`` pins the sweep's view of the staged copy's ``st_ctime`` to
    the same ~200-day-old value as the SOURCE (standing in for a sweep/
    filesystem timing combination where ctime doesn't yet read as fresh),
    while leaving ``st_mtime`` untouched -- real, whatever
    ``write_tagged_copy`` actually set it to. With ctime forced old,
    ``max(mtime, ctime)`` can only come out fresh via the ``os.utime``
    stamp. If that stamp were missing, the copy's mtime would still carry
    the source's ~200-day-old value (via ``copystat``) and this must fail.
    """
    import mutagen.flac as mutagen_flac

    audio, _video = watch_dirs
    src = audio / "archival-recording.flac"
    _make_real_flac(src)

    # 200 days old, matching the review's own reproduction.
    old_mtime = time.time() - 200 * 24 * 3600
    os.utime(str(src), (old_mtime, old_mtime))
    assert (time.time() - os.stat(src).st_mtime) / 3600 > 4700, (
        "sanity: the source itself must actually be ~200 days old"
    )

    staging = audio / source_tagger.TEMP_DIRNAME
    concurrent_sweep_result = {}
    real_flac_cls = mutagen_flac.FLAC
    call_count = {"n": 0}

    def flac_with_concurrent_sweep(path, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # This is write_tagged_copy's FIRST ``FLAC(temp_path)`` call --
            # right after ``shutil.copy2`` and the ``os.utime`` stamp (if
            # present), before tags are set or ``audio.save()`` has run.
            # Pin THIS path's ctime old, then simulate a sweep landing here,
            # concurrently with the still-in-progress write.
            fake_stat(path, 200 * 24)
            concurrent_sweep_result["result"] = source_tagger.sweep_staging(
                str(staging), older_than_hours=6.0
            )
        return real_flac_cls(path, *args, **kwargs)

    # ``write_tagged_copy`` does ``from mutagen.flac import FLAC`` INSIDE
    # the function body, re-resolving the name on every call -- patching
    # the module attribute here is what that fresh import picks up.
    monkeypatch.setattr(mutagen_flac, "FLAC", flac_with_concurrent_sweep)

    temp_path = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See"}, None, None
    )

    assert "result" in concurrent_sweep_result, (
        "sanity: the concurrent sweep hook must actually have fired"
    )
    assert os.path.exists(temp_path), (
        "the os.utime mtime stamp alone must save a copy whose ctime the "
        "sweep sees as old"
    )
    assert concurrent_sweep_result["result"]["removed"] == []
    assert concurrent_sweep_result["result"]["removed_count"] == 0


def test_sweep_ctime_aware_aging_survives_when_mtime_alone_reads_as_old(
    watch_dirs, monkeypatch
):
    """Isolates ``sweep_staging``'s ctime-aware ``max(mtime, ctime)`` aging
    from ``write_tagged_copy``'s ``os.utime`` stamp -- the other half of the
    same combined fix.

    ``write_tagged_copy``'s own ``os.utime(temp_path, None)`` call is
    neutralised here (a no-op), so the staged copy's mtime is left exactly
    as ``shutil.copy2`` set it -- the SOURCE's ~200-day-old mtime, carried
    by ``copystat``. Its REAL ``st_ctime`` is untouched and genuinely
    fresh, since the file was in fact just created moments ago -- nothing
    here fakes it. Only ``sweep_staging`` taking the ctime into account at
    all can save it under these conditions; aging off mtime alone would
    sweep it.
    """
    import mutagen.flac as mutagen_flac

    audio, _video = watch_dirs
    src = audio / "archival-recording.flac"
    _make_real_flac(src)

    old_mtime = time.time() - 200 * 24 * 3600
    os.utime(str(src), (old_mtime, old_mtime))

    # Neutralise ONLY write_tagged_copy's own ``os.utime(temp_path, None)``
    # call ("stamp to current time" -- a plain ``None`` times argument, no
    # ``ns``). ``shutil.copy2``'s ``copystat`` must still take effect
    # untouched -- it calls ``os.utime(dst, ns=(atime_ns, mtime_ns), ...)``
    # with the SOURCE's explicit old timestamps, which is the very
    # mechanism this test relies on to leave the copy's mtime reading old.
    # A blanket no-op would neutralise that call too and defeat the setup.
    real_utime = os.utime

    def _utime_neutralize_now_stamp(path, *args, **kwargs):
        times = args[0] if args else kwargs.get("times")
        if times is None and kwargs.get("ns") is None:
            return None
        return real_utime(path, *args, **kwargs)

    monkeypatch.setattr(os, "utime", _utime_neutralize_now_stamp)

    staging = audio / source_tagger.TEMP_DIRNAME
    concurrent_sweep_result = {}
    real_flac_cls = mutagen_flac.FLAC
    call_count = {"n": 0}

    def flac_with_concurrent_sweep(path, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Captured BEFORE audio.save() runs (that real write would
            # bump mtime to "now" on its own, closing the window this test
            # means to exercise) -- proves the utime neutralisation above
            # actually left the copy's mtime reading as old at the moment
            # the sweep runs, not just after the fact.
            concurrent_sweep_result["mtime_at_sweep_time"] = os.stat(path).st_mtime
            concurrent_sweep_result["result"] = source_tagger.sweep_staging(
                str(staging), older_than_hours=6.0
            )
        return real_flac_cls(path, *args, **kwargs)

    monkeypatch.setattr(mutagen_flac, "FLAC", flac_with_concurrent_sweep)

    temp_path = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See"}, None, None
    )

    assert "result" in concurrent_sweep_result, (
        "sanity: the concurrent sweep hook must actually have fired"
    )
    assert (
        time.time() - concurrent_sweep_result["mtime_at_sweep_time"]
    ) / 3600 > 100, (
        "sanity: with the utime stamp neutralised, the copy's mtime must "
        "still read as old at the moment the sweep ran"
    )
    assert os.path.exists(temp_path), (
        "ctime-aware aging alone must save a copy whose mtime reads as old"
    )
    assert concurrent_sweep_result["result"]["removed"] == []
    assert concurrent_sweep_result["result"]["removed_count"] == 0
