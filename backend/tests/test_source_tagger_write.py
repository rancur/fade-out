"""Tagging writes a verified copy; it never edits the original in place."""
import os

import pytest
from mutagen.flac import FLAC

from app.services import source_tagger

pytest.importorskip("mutagen")


def _make_flac(path):
    """A real, tiny, valid FLAC. Synthesised rather than fixtured so the test
    exercises mutagen's actual rewrite path."""
    import subprocess
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "1", "-c:a", "flac", "-y", str(path)],
        check=True, capture_output=True,
    )


def test_temp_dir_is_a_dot_dir_beside_the_source():
    d = source_tagger.temp_dir_for("/watch/audio/x.flac")
    assert d == "/watch/audio/.fadeout-tagging"
    assert os.path.basename(d).startswith("."), "must be hidden from casual listing"


def test_writes_tags_into_a_copy_and_leaves_the_original_alone(tmp_path):
    src = tmp_path / "orig.flac"
    _make_flac(src)
    before = src.read_bytes()

    out = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T"}, None, None
    )

    assert out != str(src)
    assert src.read_bytes() == before, "the original must not be modified"
    assert FLAC(out)["ARTIST"] == ["Will See"]
    assert FLAC(out)["TITLE"] == ["T"]


def test_tagging_changes_the_dedupe_hash(tmp_path):
    """The premise of the design, asserted rather than assumed."""
    from app.services.file_watcher import compute_file_hash

    src = tmp_path / "orig.flac"
    _make_flac(src)
    out = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T"}, None, None
    )
    assert compute_file_hash(out) != compute_file_hash(str(src))


def test_padding_is_added_so_later_edits_are_in_place(tmp_path):
    src = tmp_path / "orig.flac"
    _make_flac(src)
    out = source_tagger.write_tagged_copy(str(src), {"ARTIST": "Will See"}, None, None)
    padding = sum(b.length for b in FLAC(out).metadata_blocks if b.code == 1)
    assert padding >= 32768, "without padding every future edit rewrites the file"


def test_duration_mismatch_fails_and_cleans_up(tmp_path):
    src = tmp_path / "orig.flac"
    _make_flac(src)
    with pytest.raises(RuntimeError, match="duration"):
        source_tagger.write_tagged_copy(
            str(src), {"ARTIST": "Will See"}, None, expected_duration=9999.0
        )
    leftovers = list((tmp_path / source_tagger.TEMP_DIRNAME).glob("*")) \
        if (tmp_path / source_tagger.TEMP_DIRNAME).exists() else []
    assert leftovers == [], "a failed write must not leave a temp file behind"
