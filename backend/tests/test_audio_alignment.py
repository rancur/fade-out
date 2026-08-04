"""Alignment measurement + per-destination chapter timestamps.

Builds real audio files with ffmpeg and aligns them, so these exercise the
actual measurement path rather than a mock of it.
"""

import asyncio
import shutil
import subprocess

import pytest

from app.services import audio_alignment
from app.services.description_generator import _format_chapter_block
from app.services.tracklist_utils import build_youtube_chapters

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required for alignment tests",
)

LEAD_IN = 37.0
BODY = 240.0


def _make_pair(tmp_path):
    """A 'FLAC' and a 'video' render of the same audio, LEAD_IN seconds apart.

    The source is noise gated into varying-length bursts, which gives the
    envelope the irregular structure a real DJ set has — a constant tone would
    correlate equally well at every lag.
    """
    body = tmp_path / "body.wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"anoisesrc=d={BODY}:c=pink:r=16000:a=0.6",
        "-af", "tremolo=f=0.37:d=0.9,tremolo=f=0.11:d=0.8",
        "-ac", "1", str(body),
    ], check=True)

    reference = tmp_path / "reference.wav"
    shutil.copy(body, reference)

    target = tmp_path / "target.wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"anoisesrc=d={LEAD_IN}:c=brown:r=16000:a=0.2",
        "-i", str(body),
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
        "-ac", "1", str(target),
    ], check=True)
    return reference, target


def test_measures_the_lead_in_between_two_renders(tmp_path):
    reference, target = _make_pair(tmp_path)
    alignment = asyncio.run(
        audio_alignment.measure_offset(str(reference), str(target), window=30.0)
    )
    assert alignment.offset_seconds == pytest.approx(LEAD_IN, abs=0.5)
    # The whole premise of a single scalar per mix: no accumulating drift.
    assert alignment.spread_seconds < audio_alignment.MAX_PROBE_SPREAD_SECONDS
    assert alignment.probes >= audio_alignment.MIN_AGREEING_PROBES


def test_identical_files_align_at_zero(tmp_path):
    reference, _ = _make_pair(tmp_path)
    alignment = asyncio.run(
        audio_alignment.measure_offset(str(reference), str(reference), window=30.0)
    )
    assert alignment.offset_seconds == pytest.approx(0.0, abs=0.1)


def test_unrelated_audio_refuses_to_produce_an_offset(tmp_path):
    """A mismatched source must raise, never return a fabricated number.

    This is also how a mix with the wrong video attached is caught.
    """
    reference, _ = _make_pair(tmp_path)
    other = tmp_path / "other.wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"anoisesrc=d={BODY}:c=white:r=16000:a=0.5:seed=99",
        "-af", "tremolo=f=1.7:d=0.9",
        "-ac", "1", str(other),
    ], check=True)

    with pytest.raises(audio_alignment.AlignmentError):
        asyncio.run(
            audio_alignment.measure_offset(str(reference), str(other), window=30.0)
        )


def test_shifted_chapters_still_satisfy_youtube_rules():
    """Applying an offset must not break chapter rendering.

    Shifting every track forward leaves nothing at 0:00, which is precisely
    when YouTube silently renders no chapters at all. The block must still
    open at 0:00.
    """
    offset = 613.0
    tracks = [
        {"artist": f"A{i}", "title": f"T{i}",
         "timestamp_seconds": float(i * 180), "timestamp_formatted": ""}
        for i in range(6)
    ]
    shifted = [dict(t, timestamp_seconds=t["timestamp_seconds"] + offset) for t in tracks]

    chapters = build_youtube_chapters(shifted)
    assert chapters, "shifted tracklist produced no chapters"
    assert chapters[0]["timestamp_seconds"] == 0.0
    assert len(chapters) >= 3
    stamps = [c["timestamp_seconds"] for c in chapters]
    assert stamps == sorted(stamps)
    assert all(b - a >= 10 for a, b in zip(stamps, stamps[1:]))

    block = _format_chapter_block(shifted)
    assert block.splitlines()[1].startswith("0:00")
    # The first real track lands at the offset, not at 0:00 as SoundCloud has it.
    assert "10:13" in block
