"""Tests for tracklist cleaning + timestamp formatting helpers."""

from app.services.tracklist_utils import (
    ID_LABEL,
    build_youtube_chapters,
    clean_tracklist,
    format_timestamp,
    label_or_id,
)


class TestFormatTimestamp:
    def test_sub_minute(self):
        assert format_timestamp(0) == "0:00"
        assert format_timestamp(5) == "0:05"
        assert format_timestamp(59) == "0:59"

    def test_minutes(self):
        assert format_timestamp(65) == "1:05"
        assert format_timestamp(600) == "10:00"

    def test_hours(self):
        assert format_timestamp(3600) == "1:00:00"
        assert format_timestamp(3661) == "1:01:01"

    def test_negative_clamped(self):
        assert format_timestamp(-5) == "0:00"

    def test_float_truncates(self):
        assert format_timestamp(59.9) == "0:59"


class TestCleanTracklist:
    def test_empty(self):
        assert clean_tracklist([]) == []

    def test_labels_unknown_as_id(self):
        # Unidentified tracks are labelled "ID - ID" (DJ convention), not dropped.
        tracks = [
            {"artist": "Unknown", "title": "Unknown", "timestamp_seconds": 30},
            {"artist": "Aphex Twin", "title": "Xtal", "timestamp_seconds": 60},
        ]
        out = clean_tracklist(tracks)
        assert len(out) == 2
        assert out[0]["artist"] == "ID"
        assert out[0]["title"] == "ID"
        assert out[1]["title"] == "Xtal"

    def test_collapses_consecutive_unknown_runs(self):
        # A run of unidentified segments collapses to a single "ID - ID".
        tracks = [
            {"artist": "", "title": "", "timestamp_seconds": 10},
            {"artist": "Unknown", "title": "", "timestamp_seconds": 40},
            {"artist": "Boards of Canada", "title": "Roygbiv", "timestamp_seconds": 70},
        ]
        out = clean_tracklist(tracks)
        assert [(t["artist"], t["title"]) for t in out] == [
            ("ID", "ID"),
            ("Boards of Canada", "Roygbiv"),
        ]

    def test_track_placeholder_becomes_id(self):
        # CUE "Track N" placeholders are unidentified -> ID.
        tracks = [{"artist": "ID", "title": "Track 3", "timestamp_seconds": 10}]
        out = clean_tracklist(tracks)
        assert out[0]["title"] == "ID"

    def test_keeps_partial_identity(self):
        # Title known, artist unknown -> keep, artist becomes "ID".
        tracks = [{"artist": "Unknown", "title": "Some Track", "timestamp_seconds": 10}]
        out = clean_tracklist(tracks)
        assert len(out) == 1
        assert out[0]["artist"] == "ID"
        assert out[0]["title"] == "Some Track"

    def test_collapses_consecutive_duplicates(self):
        tracks = [
            {"artist": "A", "title": "T", "timestamp_seconds": 10},
            {"artist": "a", "title": "t", "timestamp_seconds": 40},  # dup (case-insensitive)
            {"artist": "B", "title": "U", "timestamp_seconds": 70},
        ]
        out = clean_tracklist(tracks)
        assert [t["title"] for t in out] == ["T", "U"]
        assert out[0]["timestamp_seconds"] == 10  # earliest kept

    def test_non_consecutive_repeat_preserved(self):
        # A legitimate replay separated by another track is kept.
        tracks = [
            {"artist": "A", "title": "T", "timestamp_seconds": 10},
            {"artist": "B", "title": "U", "timestamp_seconds": 40},
            {"artist": "A", "title": "T", "timestamp_seconds": 70},
        ]
        out = clean_tracklist(tracks)
        assert len(out) == 3

    def test_sorts_and_reformats(self):
        tracks = [
            {"artist": "B", "title": "U", "timestamp_seconds": 125},
            {"artist": "A", "title": "T", "timestamp_seconds": 5},
        ]
        out = clean_tracklist(tracks)
        assert [t["timestamp_seconds"] for t in out] == [5, 125]
        assert out[0]["timestamp_formatted"] == "0:05"
        assert out[1]["timestamp_formatted"] == "2:05"

    def test_does_not_mutate_input(self):
        tracks = [{"artist": "A", "title": "T", "timestamp_seconds": 5}]
        clean_tracklist(tracks)
        assert "timestamp_formatted" not in tracks[0]

    def test_bad_timestamp_defaults_zero(self):
        tracks = [{"artist": "A", "title": "T", "timestamp_seconds": None}]
        out = clean_tracklist(tracks)
        assert out[0]["timestamp_seconds"] == 0.0


class TestLabelOrId:
    def test_known_value_passthrough(self):
        assert label_or_id("Deadmau5") == "Deadmau5"

    def test_blank_and_unknown_become_id(self):
        assert label_or_id("") == ID_LABEL
        assert label_or_id("   ") == ID_LABEL
        assert label_or_id("Unknown") == ID_LABEL
        assert label_or_id("unknown artist") == ID_LABEL
        assert label_or_id(None) == ID_LABEL

    def test_track_placeholder_becomes_id(self):
        assert label_or_id("Track 1") == ID_LABEL
        assert label_or_id("track 12") == ID_LABEL

    def test_existing_id_stays_id(self):
        assert label_or_id("ID") == ID_LABEL

    def test_strips_whitespace(self):
        assert label_or_id("  Aphex Twin  ") == "Aphex Twin"


class TestBuildYoutubeChapters:
    def test_returns_empty_when_too_few(self):
        tracks = [
            {"artist": "A", "title": "T", "timestamp_seconds": 0},
            {"artist": "B", "title": "U", "timestamp_seconds": 60},
        ]
        assert build_youtube_chapters(tracks) == []

    def test_prepends_intro_when_first_not_zero(self):
        tracks = [
            {"artist": "A", "title": "T", "timestamp_seconds": 30},
            {"artist": "B", "title": "U", "timestamp_seconds": 90},
            {"artist": "C", "title": "V", "timestamp_seconds": 150},
        ]
        chapters = build_youtube_chapters(tracks)
        assert chapters[0]["timestamp_seconds"] == 0.0
        assert chapters[0]["title"] == "Intro"
        assert len(chapters) == 4

    def test_first_at_zero_no_intro(self):
        tracks = [
            {"artist": "A", "title": "T", "timestamp_seconds": 0},
            {"artist": "B", "title": "U", "timestamp_seconds": 60},
            {"artist": "C", "title": "V", "timestamp_seconds": 120},
        ]
        chapters = build_youtube_chapters(tracks)
        assert chapters[0]["timestamp_seconds"] == 0.0
        assert chapters[0]["title"] == "T"

    def test_drops_too_close_chapters(self):
        tracks = [
            {"artist": "A", "title": "T", "timestamp_seconds": 0},
            {"artist": "B", "title": "U", "timestamp_seconds": 3},  # <10s gap, dropped
            {"artist": "C", "title": "V", "timestamp_seconds": 60},
            {"artist": "D", "title": "W", "timestamp_seconds": 120},
        ]
        chapters = build_youtube_chapters(tracks)
        titles = [c["title"] for c in chapters]
        assert "U" not in titles
        assert titles == ["T", "V", "W"]
