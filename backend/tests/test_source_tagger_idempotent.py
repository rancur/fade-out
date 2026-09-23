"""already_tagged lets a re-run skip files that already carry the target
tags, which is what makes a 498 GB backfill cheap to repeat after an
interruption. See docs/superpowers/plans/2026-09-23-backfill-safe-unattended.md,
Task 1.
"""
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import source_tagger


def _make_flac(path, duration=1):
    """A real, tiny, valid FLAC. Synthesised rather than fixtured so the
    test exercises mutagen's actual read path -- same pattern as
    test_source_tagger_write.py."""
    import subprocess
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", str(duration), "-c:a", "flac", "-y", str(path)],
        check=True, capture_output=True,
    )


def _make_cover(path):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)


def _fake_mix(**attrs):
    """Mirrors the real Mix model's field names exactly -- same fields used
    by build_tags and by test_source_tagger_rails.py's _fake_loader."""
    base = dict(
        id="m1", title="Test Mix", audio_file_path=None, video_file_path=None,
        genres=None, tracklist=None, cover_art_path=None, soundcloud_url=None,
        youtube_url=None, created_at=datetime(2026, 9, 18), source="watch",
        duration_seconds=1.0, pipeline_status="completed",
    )
    base.update(attrs)
    return SimpleNamespace(**base)


# --- already_tagged: unit tests -------------------------------------------

def test_freshly_tagged_file_reports_true(tmp_path):
    src = tmp_path / "orig.flac"
    _make_flac(src)
    tags = {"ARTIST": "Will See", "ALBUM": "Will See Mixes", "TITLE": "Test Mix"}

    tagged = source_tagger.write_tagged_copy(str(src), tags, None, None)
    final = tmp_path / "final.flac"
    os.replace(tagged, final)

    assert source_tagger.already_tagged(str(final), tags, None) is True


def test_untagged_file_reports_false(tmp_path):
    src = tmp_path / "orig.flac"
    _make_flac(src)
    tags = {"ARTIST": "Will See", "ALBUM": "Will See Mixes", "TITLE": "Test Mix"}

    assert source_tagger.already_tagged(str(src), tags, None) is False


def test_retitled_mix_reports_false(tmp_path):
    """A retitled mix's TITLE differs from the target -- it MUST be re-tagged,
    not skipped."""
    src = tmp_path / "orig.flac"
    _make_flac(src)
    old_tags = {"ARTIST": "Will See", "ALBUM": "Will See Mixes", "TITLE": "Old Title"}
    new_tags = {"ARTIST": "Will See", "ALBUM": "Will See Mixes", "TITLE": "New Title"}

    tagged = source_tagger.write_tagged_copy(str(src), old_tags, None, None)
    final = tmp_path / "final.flac"
    os.replace(tagged, final)

    assert source_tagger.already_tagged(str(final), new_tags, None) is False


def test_missing_cover_art_reports_false_once_art_is_available(tmp_path):
    """Tagged without cover art; cover art now exists -- must NOT be
    reported as already-tagged, since a real run would now embed it."""
    src = tmp_path / "orig.flac"
    _make_flac(src)
    tags = {"ARTIST": "Will See", "ALBUM": "Will See Mixes", "TITLE": "Test Mix"}

    tagged = source_tagger.write_tagged_copy(str(src), tags, None, None)
    final = tmp_path / "final.flac"
    os.replace(tagged, final)

    cover = tmp_path / "cover.png"
    _make_cover(cover)

    assert source_tagger.already_tagged(str(final), tags, str(cover)) is False


def test_regenerated_cover_art_same_path_different_content_reports_false(tmp_path):
    """Finding 6: the cover art check must compare CONTENT, not just
    presence. A mix's artwork can be regenerated at the SAME
    ``cover_art_path`` with different bytes (e.g. a redesigned thumbnail) --
    a presence-only check ("some picture is embedded" and "a cover art file
    exists" are both true) would report already-tagged forever and leave
    the STALE embedded art in place. Both images here are the same length,
    so this specifically exercises the SHA-256 digest comparison, not the
    cheaper size check that precedes it."""
    src = tmp_path / "orig.flac"
    _make_flac(src)
    tags = {"ARTIST": "Will See", "ALBUM": "Will See Mixes", "TITLE": "Test Mix"}

    cover = tmp_path / "cover.png"
    _make_cover(cover)

    tagged = source_tagger.write_tagged_copy(str(src), tags, str(cover), None)
    final = tmp_path / "final.flac"
    os.replace(tagged, final)

    assert source_tagger.already_tagged(str(final), tags, str(cover)) is True

    # Artwork regenerated at the SAME path -- same length, different bytes.
    cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff" * 100)

    assert source_tagger.already_tagged(str(final), tags, str(cover)) is False


def test_already_tagged_does_not_modify_the_file(tmp_path):
    """A header read only: size and mtime must be byte-for-byte unchanged
    after the check."""
    src = tmp_path / "orig.flac"
    _make_flac(src)
    tags = {"ARTIST": "Will See", "ALBUM": "Will See Mixes", "TITLE": "Test Mix"}

    tagged = source_tagger.write_tagged_copy(str(src), tags, None, None)
    final = tmp_path / "final.flac"
    os.replace(tagged, final)

    before_stat = os.stat(final)
    before_bytes = final.read_bytes()

    # Call it twice -- once for the True case, once (with mismatched tags)
    # for the False case -- neither may touch the file.
    source_tagger.already_tagged(str(final), tags, None)
    source_tagger.already_tagged(str(final), {**tags, "TITLE": "Other"}, None)

    after_stat = os.stat(final)
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime == before_stat.st_mtime
    assert final.read_bytes() == before_bytes


# --- tag_sources_for_mix wiring --------------------------------------------

@pytest.mark.asyncio
async def test_tag_sources_for_mix_skips_an_already_tagged_file(tmp_path, monkeypatch):
    src = tmp_path / "already.flac"
    _make_flac(src)
    mix = _fake_mix(audio_file_path=str(src))
    tags = source_tagger.build_tags(mix)

    tagged = source_tagger.write_tagged_copy(str(src), tags, None, None)
    os.replace(tagged, src)

    async def _on():
        return True

    async def _load(mix_id):
        return mix

    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(source_tagger, "_load_mix", _load)
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda p: True)

    def boom(*a, **k):
        raise AssertionError("an already-tagged file must not be rewritten")

    monkeypatch.setattr(source_tagger, "write_tagged_copy", boom)

    result = await source_tagger.tag_sources_for_mix("m1", reason="test")

    assert result["status"] == "skipped"
    assert result["reason"] == "already tagged"


@pytest.mark.asyncio
async def test_dry_run_over_an_already_tagged_file_reports_skip_not_tag(tmp_path, monkeypatch):
    """The false-green this task exists to prevent: a dry run over an
    already-tagged library must say it would SKIP, not that it would TAG --
    the operator reads this output as a go/no-go signal before a 498 GB
    operation."""
    src = tmp_path / "already.flac"
    _make_flac(src)
    mix = _fake_mix(audio_file_path=str(src))
    tags = source_tagger.build_tags(mix)

    tagged = source_tagger.write_tagged_copy(str(src), tags, None, None)
    os.replace(tagged, src)

    async def _on():
        return True

    async def _load(mix_id):
        return mix

    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(source_tagger, "_load_mix", _load)
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda p: True)

    def boom(*a, **k):
        raise AssertionError("dry run must not write")

    monkeypatch.setattr(source_tagger, "write_tagged_copy", boom)

    result = await source_tagger.tag_sources_for_mix("m1", reason="test", dry_run=True)

    assert result["status"] == "dry_run"
    assert "already tagged" in result["reason"]
    assert "skip" in result["reason"].lower()
    assert result["actions"][0]["already_tagged"] is True
