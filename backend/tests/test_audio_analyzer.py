"""Tests for Shazam track identification logic (no audio decode / network).

``_recognize_segment`` is stubbed with canned hits keyed by clip offset so
``_identify_tracks`` can be exercised deterministically.
"""

from typing import Dict, Optional

from app.services.audio_analyzer import (
    RETRY_CLIP_OFFSET,
    SECONDARY_CLIP_OFFSET,
    AudioAnalyzer,
    TrackHit,
)
from app.services.confidence_merge import (
    SHAZAM_CONFIRMED_CONFIDENCE,
    SHAZAM_SINGLE_CONFIDENCE,
)


def _analyzer_with_hits(hits_by_offset: Dict[float, tuple]) -> AudioAnalyzer:
    """Build an analyzer whose _recognize_segment returns canned hits."""
    analyzer = AudioAnalyzer(sample_interval=120)

    async def fake_recognize(path: str, offset: float, sr_native: int) -> Optional[TrackHit]:
        entry = hits_by_offset.get(offset)
        if entry is None:
            return None
        title, artist = entry
        return TrackHit(title=title, artist=artist, timestamp_seconds=offset)

    analyzer._recognize_segment = fake_recognize  # type: ignore[method-assign]
    return analyzer


class TestIdentifyTracks:
    async def test_track_reappearing_later_is_kept(self):
        # A -> B -> A: the comeback of A must NOT be dropped by a global
        # seen-titles set. Dedup is consecutive-only.
        analyzer = _analyzer_with_hits({
            30.0: ("Track A", "Artist A"),
            150.0: ("Track B", "Artist B"),
            270.0: ("Track A", "Artist A"),
        })
        result = await analyzer._identify_tracks("mix.flac", [30.0, 150.0, 270.0], 44100)
        assert [t.title for t in result] == ["Track A", "Track B", "Track A"]

    async def test_consecutive_duplicates_collapsed(self):
        # Same track recognized at consecutive sample points (and by the
        # secondary clip) collapses to one entry at the earliest timestamp.
        analyzer = _analyzer_with_hits({
            30.0: ("Track A", "Artist A"),
            30.0 + SECONDARY_CLIP_OFFSET: ("Track A", "Artist A"),
            150.0: ("Track A", "Artist A"),
        })
        result = await analyzer._identify_tracks("mix.flac", [30.0, 150.0], 44100)
        assert len(result) == 1
        assert result[0].timestamp_seconds == 30.0

    async def test_secondary_clip_runs_even_when_primary_hits(self):
        # A transition window: primary clip hears the outgoing track, the +30s
        # secondary clip hears the incoming one. BOTH belong in the tracklist.
        analyzer = _analyzer_with_hits({
            30.0: ("Track A", "Artist A"),
            30.0 + SECONDARY_CLIP_OFFSET: ("Track B", "Artist B"),
        })
        result = await analyzer._identify_tracks("mix.flac", [30.0], 44100)
        assert [t.title for t in result] == ["Track A", "Track B"]

    async def test_retry_offset_used_when_primary_misses(self):
        analyzer = _analyzer_with_hits({
            30.0 + RETRY_CLIP_OFFSET: ("Track A", "Artist A"),
        })
        result = await analyzer._identify_tracks("mix.flac", [30.0], 44100)
        assert [t.title for t in result] == ["Track A"]

    async def test_single_hit_sandwiched_track_is_kept(self):
        # The old "sandwich" filter dropped a single-hit track squeezed between
        # close neighbors — but in a fast blend that IS the real middle track.
        analyzer = _analyzer_with_hits({
            30.0: ("Track A", "Artist A"),
            30.0 + SECONDARY_CLIP_OFFSET: ("Track B", "Artist B"),
            90.0: ("Track C", "Artist C"),
            150.0: ("Track C", "Artist C"),
            270.0: ("Track D", "Artist D"),
        })
        result = await analyzer._identify_tracks(
            "mix.flac", [30.0, 90.0, 150.0, 270.0], 44100
        )
        assert "Track B" in [t.title for t in result]

    async def test_confidence_tagging(self):
        # A recognized twice -> confirmed confidence; B once -> single-hit.
        analyzer = _analyzer_with_hits({
            30.0: ("Track A", "Artist A"),
            30.0 + SECONDARY_CLIP_OFFSET: ("Track A", "Artist A"),
            150.0: ("Track B", "Artist B"),
        })
        result = await analyzer._identify_tracks("mix.flac", [30.0, 150.0], 44100)
        by_title = {t.title: t.confidence for t in result}
        assert by_title["Track A"] == SHAZAM_CONFIRMED_CONFIDENCE
        assert by_title["Track B"] == SHAZAM_SINGLE_CONFIDENCE

    async def test_no_hits_returns_empty(self):
        analyzer = _analyzer_with_hits({})
        result = await analyzer._identify_tracks("mix.flac", [30.0, 150.0], 44100)
        assert result == []
