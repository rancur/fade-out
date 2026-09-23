"""End-to-end: a tagged file must not look new to the watcher.

This is the guarantee the whole out-of-place design exists to provide. It is
asserted against the watcher's own dedupe state, not a mock of it.
"""
import os
import subprocess

import pytest

from app.services import file_watcher, source_tagger


def _make_flac(path):
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "1", "-c:a", "flac", "-y", str(path)],
        check=True, capture_output=True,
    )


def test_tagged_file_is_already_known_to_the_watcher(tmp_path):
    watch = tmp_path / "audio"
    watch.mkdir()
    src = watch / "mix.flac"
    _make_flac(src)

    db = file_watcher.open_seen_files_db(str(tmp_path / "seen.db"))
    # The untagged original has been ingested already, as in production.
    db.mark_done(file_watcher.compute_file_hash(str(src)), str(src), "audio")

    temp = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T"}, None, None
    )
    source_tagger.register_and_promote(temp, str(src), "audio", seen_db=db)

    # The watcher's question, asked exactly as the watcher asks it.
    assert db.is_done(file_watcher.compute_file_hash(str(src))), (
        "the tagged file is unknown to the watcher -- it would be re-ingested "
        "and the mix re-uploaded"
    )


def test_the_staging_dir_is_invisible_to_the_watcher_scan(tmp_path):
    """The watcher lists one level; the staging dir must not yield candidates."""
    watch = tmp_path / "audio"
    watch.mkdir()
    src = watch / "mix.flac"
    _make_flac(src)
    source_tagger.write_tagged_copy(str(src), {"ARTIST": "Will See"}, None, None)

    entries = os.listdir(str(watch))
    assert source_tagger.TEMP_DIRNAME in entries
    audio_files = [
        e for e in entries
        if os.path.isfile(os.path.join(str(watch), e))
        and os.path.splitext(e)[1].lower() in file_watcher.AUDIO_EXTENSIONS
    ]
    assert audio_files == ["mix.flac"], (
        "a non-recursive listdir must not surface the staged copy as a new recording"
    )
