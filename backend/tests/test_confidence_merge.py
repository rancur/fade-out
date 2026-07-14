"""Tests for the confidence-scored multi-source detection merge."""

from app.services.confidence_merge import (
    DEFAULT_NAME_CONFIDENCE_THRESHOLD,
    DetectionSource,
    SHAZAM_CONFIRMED_CONFIDENCE,
    SHAZAM_SINGLE_CONFIDENCE,
    merge_detections,
)


def _cue(tracks):
    return DetectionSource("cue", tracks)


def _shazam(tracks):
    return DetectionSource("shazam", tracks)


def _t(artist, title, ts, **extra):
    d = {"artist": artist, "title": title, "timestamp_seconds": ts}
    d.update(extra)
    return d


class TestEmptyAndSingleSource:
    def test_no_sources(self):
        result = merge_detections([])
        assert result.source == "none"
        assert result.tracklist == []

    def test_all_empty_sources(self):
        result = merge_detections([_cue([]), _shazam([])])
        assert result.source == "none"
        assert result.tracklist == []

    def test_cue_only(self):
        result = merge_detections([_cue([_t("A", "T", 0)])])
        assert result.source == "djctl"
        assert result.tracklist[0]["artist"] == "A"
        assert result.tracklist[0]["title"] == "T"

    def test_shazam_only(self):
        result = merge_detections(
            [_shazam([_t("A", "T", 90, confidence=SHAZAM_CONFIRMED_CONFIDENCE)])]
        )
        assert result.source == "shazam"
        assert result.tracklist[0]["timestamp_formatted"] == "1:30"


class TestAuthoritativeWins:
    def test_cue_wins_over_shazam_on_overlap(self):
        # Same moment: CUE says the real track, Shazam disagrees -> CUE wins and
        # keeps its own timestamp.
        cue = _cue([_t("Real Artist", "Real Title", 100)])
        shazam = _shazam(
            [_t("Wrong Artist", "Wrong Title", 110, confidence=SHAZAM_CONFIRMED_CONFIDENCE)]
        )
        result = merge_detections([cue, shazam])
        assert len(result.tracklist) == 1
        assert result.tracklist[0]["artist"] == "Real Artist"
        assert result.tracklist[0]["title"] == "Real Title"
        assert result.tracklist[0]["timestamp_seconds"] == 100
        assert result.source == "djctl"  # only CUE ultimately contributed a name

    def test_shazam_fills_gap_not_covered_by_cue(self):
        cue = _cue([_t("A", "T1", 0)])
        shazam = _shazam(
            [_t("B", "T2", 300, confidence=SHAZAM_CONFIRMED_CONFIDENCE)]
        )
        result = merge_detections([cue, shazam])
        titles = [t["title"] for t in result.tracklist]
        assert titles == ["T1", "T2"]
        assert result.source == "merged"


class TestPlaceholderFill:
    def test_shazam_fills_cue_track_placeholder_keeps_cue_timestamp(self):
        # CUE "Track 3" placeholder normalizes to ID; a confident Shazam hit
        # overlapping it lends its name, but the CUE timestamp is preserved.
        cue = _cue([_t("ID", "Track 3", 200)])
        shazam = _shazam(
            [_t("Filled Artist", "Filled Title", 210, confidence=SHAZAM_CONFIRMED_CONFIDENCE)]
        )
        result = merge_detections([cue, shazam])
        assert len(result.tracklist) == 1
        entry = result.tracklist[0]
        assert entry["artist"] == "Filled Artist"
        assert entry["title"] == "Filled Title"
        assert entry["timestamp_seconds"] == 200  # CUE timestamp, not Shazam's

    def test_weak_shazam_does_not_fill_placeholder(self):
        # Below-threshold Shazam is not trusted enough to fill a placeholder;
        # the CUE entry stays ID - ID.
        cue = _cue([_t("ID", "Track 3", 200)])
        shazam = _shazam([_t("Maybe", "Guess", 205, confidence=0.2)])
        result = merge_detections([cue, shazam])
        assert len(result.tracklist) == 1
        assert result.tracklist[0]["artist"] == "ID"
        assert result.tracklist[0]["title"] == "ID"


class TestLowConfidenceBecomesID:
    def test_below_threshold_gap_fill_becomes_id(self):
        # A lone weak Shazam guess in a gap keeps its timestamp but renders as
        # ID - ID rather than a probably-wrong name.
        shazam = _shazam([_t("Sketchy", "Guess", 500, confidence=0.3)])
        result = merge_detections([shazam])
        assert len(result.tracklist) == 1
        assert result.tracklist[0]["artist"] == "ID"
        assert result.tracklist[0]["title"] == "ID"
        assert result.tracklist[0]["timestamp_seconds"] == 500

    def test_at_threshold_gap_fill_keeps_name(self):
        # Exactly at the threshold is trusted (default preserves single-hit recall).
        shazam = _shazam(
            [_t("Kept", "Name", 500, confidence=DEFAULT_NAME_CONFIDENCE_THRESHOLD)]
        )
        result = merge_detections([shazam])
        assert result.tracklist[0]["artist"] == "Kept"
        assert result.tracklist[0]["title"] == "Name"

    def test_single_hit_shazam_default_kept(self):
        shazam = _shazam(
            [_t("Kept", "Name", 500, confidence=SHAZAM_SINGLE_CONFIDENCE)]
        )
        result = merge_detections([shazam])
        assert result.tracklist[0]["title"] == "Name"

    def test_strict_threshold_turns_single_hit_into_id(self):
        # Raising the threshold makes single-hit Shazam collapse to ID.
        shazam = _shazam(
            [_t("Kept", "Name", 500, confidence=SHAZAM_SINGLE_CONFIDENCE)]
        )
        result = merge_detections([shazam], name_confidence_threshold=0.6)
        assert result.tracklist[0]["title"] == "ID"


class TestDuplicateResolution:
    def test_stronger_shazam_duplicate_wins(self):
        # Two overlapping Shazam hits: the higher-confidence name is kept.
        shazam = _shazam(
            [
                _t("Weak", "WeakTitle", 300, confidence=0.5),
                _t("Strong", "StrongTitle", 320, confidence=0.7),
            ]
        )
        result = merge_detections([shazam])
        assert len(result.tracklist) == 1
        assert result.tracklist[0]["title"] == "StrongTitle"

    def test_overlapping_authoritative_prefers_identified(self):
        # Two CUE-ish authoritative entries overlap; the identified one wins over
        # a placeholder at the same moment.
        cue = _cue([_t("ID", "Track 1", 100), _t("Named", "RealTrack", 110)])
        result = merge_detections([cue])
        assert len(result.tracklist) == 1
        assert result.tracklist[0]["title"] == "RealTrack"


class TestOrderingAndFormatting:
    def test_output_sorted_and_formatted(self):
        cue = _cue([_t("A", "Late", 3661), _t("B", "Early", 0)])
        result = merge_detections([cue])
        assert [t["title"] for t in result.tracklist] == ["Early", "Late"]
        assert result.tracklist[0]["timestamp_formatted"] == "0:00"
        assert result.tracklist[1]["timestamp_formatted"] == "1:01:01"

    def test_does_not_mutate_input(self):
        track = _t("A", "T", 0)
        src = _cue([track])
        merge_detections([src])
        assert track == {"artist": "A", "title": "T", "timestamp_seconds": 0}


class TestDjctlSource:
    def test_djctl_authoritative_over_shazam(self):
        djctl = DetectionSource("djctl", [_t("Deck", "NowPlaying", 50)])
        shazam = _shazam([_t("Guess", "Wrong", 55, confidence=0.7)])
        result = merge_detections([djctl, shazam])
        assert len(result.tracklist) == 1
        assert result.tracklist[0]["title"] == "NowPlaying"
