"""Tagging writes a verified copy; it never edits the original in place."""
import os

import pytest
from mutagen.flac import FLAC

from app.services import source_tagger


def _make_flac(path):
    """A real, tiny, valid FLAC. Synthesised rather than fixtured so the test
    exercises mutagen's actual rewrite path."""
    import subprocess
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "1", "-c:a", "flac", "-y", str(path)],
        check=True, capture_output=True,
    )


def _make_flac_with_audio_data(path, duration=10):
    """A FLAC with a non-trivial amount of encoded audio data (a sine wave,
    not silence).

    Silence compresses so well that ``_make_flac``'s 1-second fixture's
    entire audio payload is dwarfed by the 64 KB padding block added on
    every tagged write -- a tagged copy of that fixture is always larger
    than the untagged original no matter how much of its (tiny) audio is
    cut, which would mask a truncated write rather than let a test catch
    it. A sine wave over several seconds produces audio data large enough
    that truncating it actually shrinks the file below the source.
    """
    import subprocess
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         "-c:a", "flac", "-y", str(path)],
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


def test_truncated_staged_copy_is_rejected(tmp_path, monkeypatch):
    """A short write (ENOSPC, an I/O error, a disk that fills mid-copy) must
    be rejected before it can be promoted over the original.

    The STREAMINFO duration a parse gives back is a header read, not a
    measurement: shutil.copy2 carries it over from the original verbatim, so
    a truncated payload still parses as valid FLAC and still reports the
    full original duration. Verified by hand against a real FLAC: deleting
    half the file's bytes still reports the full duration, passes
    ``if not actual``, passes the ``abs(actual - expected) > 1.0`` check, and
    would be promoted over the original. Only a raw size comparison against
    the source catches it -- specifically a comparison of the AUDIO PAYLOAD
    (everything after the last metadata block), not of total file size; see
    ``_audio_payload_offset``. This is the single most important test on
    this branch -- it must fail if the payload-size check in
    write_tagged_copy is removed (verified by hand: with that check
    commented out, this test fails because write_tagged_copy returns the
    truncated path instead of raising).
    """
    src = tmp_path / "orig.flac"
    _make_flac_with_audio_data(src)

    from mutagen.flac import FLAC as RealFLAC

    original_save = RealFLAC.save

    def truncating_save(self, *args, **kwargs):
        # Write tags/padding normally, then simulate a short write by
        # chopping well into the audio frame region -- found by walking the
        # metadata block chain to its end, exactly like a real FLAC decoder
        # would, so the truncation never touches a metadata block. That
        # means the re-read below still parses cleanly and still reports
        # the full original duration from STREAMINFO: the whole premise of
        # this test is that neither the parse check nor the duration check
        # can see this failure, only the size check can.
        original_save(self, *args, **kwargs)
        with open(self.filename, "rb") as fh:
            assert fh.read(4) == b"fLaC"
            audio_start = 4
            while True:
                block_header = fh.read(4)
                is_last = bool(block_header[0] & 0x80)
                block_len = int.from_bytes(block_header[1:4], "big")
                fh.seek(block_len, 1)
                audio_start = fh.tell()
                if is_last:
                    break
        total_size = os.path.getsize(self.filename)
        # Keep only the first 10% of the audio frame data.
        cutoff = audio_start + (total_size - audio_start) // 10
        with open(self.filename, "r+b") as fh:
            fh.truncate(cutoff)

    monkeypatch.setattr(RealFLAC, "save", truncating_save)

    with pytest.raises(RuntimeError, match="audio payload.*smaller than the source"):
        source_tagger.write_tagged_copy(
            str(src), {"ARTIST": "Will See"}, None, None
        )

    leftovers = list((tmp_path / source_tagger.TEMP_DIRNAME).glob("*")) \
        if (tmp_path / source_tagger.TEMP_DIRNAME).exists() else []
    assert leftovers == [], "a truncated write must not leave a temp file behind"


class _FakeSeenDB:
    """Minimal stand-in for ``_SeenFilesDB``, just enough for register_and_promote."""

    def __init__(self):
        self.done = set()

    def mark_done(self, file_hash, file_path, file_type):
        self.done.add(file_hash)

    def is_done(self, file_hash):
        return file_hash in self.done

    def close(self):
        pass


def test_retagging_an_already_tagged_file_succeeds(tmp_path):
    """Regression test for the false positive fixed in write_tagged_copy:
    once a file already carries the 64 KB padding block this module adds,
    mutagen writes new metadata INTO that padding on a re-tag, so the total
    file size does not change even though the write is perfectly healthy --
    ``pipeline.py`` tags on every ``pipeline_complete``, so any mix re-run
    after already being tagged must not be rejected as "truncated".

    Verified by hand: reverting the check in ``write_tagged_copy`` to the
    old ``temp_size <= src_size`` form makes this test fail, because the
    second (byte-identical-size) tagging raises instead of succeeding.
    """
    src = tmp_path / "orig.flac"
    _make_flac_with_audio_data(src)

    tags = {"ARTIST": "Will See", "TITLE": "T"}

    once = source_tagger.write_tagged_copy(str(src), tags, None, None)
    once_path = tmp_path / "once.flac"
    os.replace(once, once_path)

    # Re-tag the already-tagged file with the SAME tags -- this is the
    # byte-identical case: the new metadata fits inside the existing
    # padding block, so total file size does not change.
    twice = source_tagger.write_tagged_copy(str(once_path), tags, None, None)

    assert os.path.getsize(twice) == os.path.getsize(once_path), (
        "a re-tag of an already-tagged file with identical tags should not "
        "change the total file size -- the new metadata fits in the "
        "existing padding block; if this assertion fails the test fixture, "
        "not the check under test, needs attention"
    )

    # Prove it survives the full pipeline, not just write_tagged_copy: the
    # file must be promotable.
    final = tmp_path / "final.flac"
    db = _FakeSeenDB()
    source_tagger.register_and_promote(twice, str(final), "audio", seen_db=db)
    assert os.path.isfile(final), "a healthy re-tag must be promoted, not rejected"
    assert not os.path.exists(twice)


def test_zero_duration_read_fails_with_expected_duration(tmp_path, monkeypatch):
    """A truncated or corrupt write that reads as 0.0s must be rejected."""
    src = tmp_path / "orig.flac"
    _make_flac(src)

    # Monkeypatch FLAC.__init__ to zero out info.length on every FLAC() call
    # against this path -- both the in-function edit read and the verify
    # re-read (write_tagged_copy calls FLAC() twice), since only the
    # combination of "still parses" + "reports zero duration" is what this
    # test needs to exercise the RuntimeError branch.
    from mutagen.flac import FLAC as RealFLAC

    original_init = RealFLAC.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if args and str(args[0]).endswith(os.path.basename(str(src))):
            self.info.length = 0.0

    monkeypatch.setattr(RealFLAC, "__init__", patched_init)

    with pytest.raises(RuntimeError, match="no audio duration"):
        source_tagger.write_tagged_copy(
            str(src), {"ARTIST": "Will See"}, None, expected_duration=1.0
        )
    leftovers = list((tmp_path / source_tagger.TEMP_DIRNAME).glob("*")) \
        if (tmp_path / source_tagger.TEMP_DIRNAME).exists() else []
    assert leftovers == [], "a failed write must not leave a temp file behind"


def test_expected_duration_none_skips_only_the_duration_check(tmp_path):
    """expected_duration=None gates only the duration comparison -- the
    parse re-read and the size check are unconditional and still run. This
    is asserted, not assumed: FLAC(out) below only succeeds if the file
    write_tagged_copy returned actually parses as valid FLAC, which is
    exactly the re-read verify performs internally."""
    src = tmp_path / "orig.flac"
    _make_flac(src)
    out = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See"}, None, expected_duration=None
    )
    assert os.path.exists(out), "write should succeed with expected_duration=None"
    # Proves the unconditional parse check actually ran and passed -- a
    # write that failed to parse would have raised inside write_tagged_copy
    # before ever returning a path for this to open.
    assert FLAC(out)["ARTIST"] == ["Will See"]


def test_sequential_calls_produce_different_temp_paths(tmp_path):
    """Two sequential calls get different temp paths.

    The calls here are sequential, not concurrent -- this does not prove
    anything about a race. What it proves is that temp-path uniqueness
    comes from the uuid4 component in the filename, not from timing (e.g.
    a coarse timestamp), which is the part that would matter under real
    concurrency.
    """
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


def test_corrupt_write_fails_and_cleans_up_even_with_none_duration(tmp_path, monkeypatch):
    """A corrupt write that does not parse as FLAC must fail and cleanup, even when expected_duration=None."""
    src = tmp_path / "orig.flac"
    _make_flac(src)

    # Monkeypatch FLAC so that the post-write re-read (the verify step) raises.
    from mutagen.flac import FLAC as RealFLAC

    original_init = RealFLAC.__init__
    call_count = [0]

    def patched_init(self, *args, **kwargs):
        call_count[0] += 1
        # First call (editing the copy) succeeds; second call (verify re-read) fails.
        if call_count[0] == 2:
            raise ValueError("simulated corrupt FLAC: re-read failed")
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(RealFLAC, "__init__", patched_init)

    with pytest.raises(ValueError, match="simulated corrupt FLAC"):
        source_tagger.write_tagged_copy(
            str(src), {"ARTIST": "Will See"}, None, expected_duration=None
        )

    leftovers = list((tmp_path / source_tagger.TEMP_DIRNAME).glob("*")) \
        if (tmp_path / source_tagger.TEMP_DIRNAME).exists() else []
    assert leftovers == [], "a corrupt write must not leave a temp file behind"
