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
    assert padding >= source_tagger.FLAC_PADDING_BYTES, "without padding every future edit rewrites the file"


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


def test_zero_duration_read_fails_with_expected_duration(tmp_path, monkeypatch):
    """A truncated or corrupt write that reads as 0.0s must be rejected."""
    src = tmp_path / "orig.flac"
    _make_flac(src)

    # Monkeypatch FLAC to return 0.0 length when verify reads it back.
    from mutagen.flac import FLAC as RealFLAC

    original_init = RealFLAC.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # Only patch on the verify read (the second call to FLAC in write_tagged_copy).
        if hasattr(self, "_patched"):
            return
        if args and str(args[0]).endswith(os.path.basename(str(src))):
            self.info.length = 0.0
            self._patched = True

    monkeypatch.setattr(RealFLAC, "__init__", patched_init)

    with pytest.raises(RuntimeError, match="no audio duration"):
        source_tagger.write_tagged_copy(
            str(src), {"ARTIST": "Will See"}, None, expected_duration=1.0
        )
    leftovers = list((tmp_path / source_tagger.TEMP_DIRNAME).glob("*")) \
        if (tmp_path / source_tagger.TEMP_DIRNAME).exists() else []
    assert leftovers == [], "a failed write must not leave a temp file behind"


def test_expected_duration_none_skips_verification(tmp_path):
    """expected_duration=None deliberately skips verification and succeeds."""
    src = tmp_path / "orig.flac"
    _make_flac(src)
    out = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See"}, None, expected_duration=None
    )
    assert os.path.exists(out), "write should succeed with expected_duration=None"
    assert FLAC(out)["ARTIST"] == ["Will See"]


def test_sequential_calls_produce_different_temp_paths(tmp_path):
    """Concurrent calls must not collide on the same temp path."""
    src = tmp_path / "orig.flac"
    _make_flac(src)
    out1 = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T1"}, None, None
    )
    out2 = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T2"}, None, None
    )
    assert out1 != out2, "each call should produce a unique temp path"
    assert os.path.exists(out1) and os.path.exists(out2)


def test_cover_art_mime_detection(tmp_path):
    """PNG cover art yields image/png; JPEG yields image/jpeg."""
    src = tmp_path / "orig.flac"
    _make_flac(src)

    # Test PNG
    png_path = tmp_path / "cover.png"
    png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)  # Minimal PNG header
    out_png = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See"}, str(png_path), None
    )
    assert FLAC(out_png).pictures[0].mime == "image/png"

    # Test JPEG
    jpg_path = tmp_path / "cover.jpg"
    jpg_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)  # Minimal JPEG header
    out_jpg = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See"}, str(jpg_path), None
    )
    assert FLAC(out_jpg).pictures[0].mime == "image/jpeg"


def test_padding_uses_constant(tmp_path):
    """Padding assertion should use the FLAC_PADDING_BYTES constant."""
    src = tmp_path / "orig.flac"
    _make_flac(src)
    out = source_tagger.write_tagged_copy(str(src), {"ARTIST": "Will See"}, None, None)
    padding = sum(b.length for b in FLAC(out).metadata_blocks if b.code == 1)
    assert padding >= source_tagger.FLAC_PADDING_BYTES
