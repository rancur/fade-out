"""Tests for CUE parsing, audio<->CUE matching, and tracklist merging."""

import os
import time
from datetime import datetime

from app.services.djctl_integration import (
    CueTrack,
    find_cue_for_audio,
    merge_tracklists,
    parse_cue_file,
)

SAMPLE_CUE = """\
PERFORMER "Will See"
TITLE "Desert Session"
FILE "mix.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Opener"
    PERFORMER "Artist One"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Track"
    PERFORMER "Artist Two"
    INDEX 01 03:30:37
  TRACK 03 AUDIO
    INDEX 01 07:15:00
"""


class TestCueTrack:
    def test_timestamp_seconds_with_frames(self):
        # 3 min 30 sec + 37/75 frames
        t = CueTrack(number=2, title="x", artist="y", index_mm=3, index_ss=30, index_ff=37)
        assert abs(t.timestamp_seconds - (3 * 60 + 30 + 37 / 75.0)) < 1e-6

    def test_timestamp_formatted(self):
        t = CueTrack(number=1, title="x", artist="y", index_mm=65, index_ss=5, index_ff=0)
        assert t.timestamp_formatted == "1:05:05"


class TestParseCue:
    def test_parses_all_tracks(self, tmp_path):
        cue = tmp_path / "djctl-2025-12-05.cue"
        cue.write_text(SAMPLE_CUE)
        tracks = parse_cue_file(str(cue))
        assert len(tracks) == 3
        assert tracks[0].title == "Opener"
        assert tracks[0].artist == "Artist One"

    def test_track_performer_falls_back_to_global(self, tmp_path):
        cue = tmp_path / "djctl-2025-12-05.cue"
        cue.write_text(SAMPLE_CUE)
        tracks = parse_cue_file(str(cue))
        # Track 3 has no PERFORMER -> inherits global "Will See".
        assert tracks[2].artist == "Will See"
        assert tracks[2].title == "Track 3"  # no TITLE -> placeholder

    def test_index_timestamps(self, tmp_path):
        cue = tmp_path / "djctl-2025-12-05.cue"
        cue.write_text(SAMPLE_CUE)
        tracks = parse_cue_file(str(cue))
        assert tracks[0].timestamp_seconds == 0.0
        assert abs(tracks[1].timestamp_seconds - (210 + 37 / 75.0)) < 1e-6


class TestFindCueForAudio:
    def _make(self, tmp_path, cue_name, audio_date):
        cue_dir = tmp_path / "cues"
        cue_dir.mkdir()
        (cue_dir / cue_name).write_text(SAMPLE_CUE)
        audio = tmp_path / "mix.flac"
        audio.write_text("x")
        ts = time.mktime(datetime.strptime(audio_date, "%Y-%m-%d").timetuple())
        os.utime(str(audio), (ts, ts))
        return str(cue_dir), str(audio)

    def test_matches_same_day(self, tmp_path):
        cue_dir, audio = self._make(tmp_path, "djctl-2025-12-05.cue", "2025-12-05")
        assert find_cue_for_audio(audio, cue_directory=cue_dir) is not None

    def test_no_match_when_far(self, tmp_path):
        cue_dir, audio = self._make(tmp_path, "djctl-2025-12-05.cue", "2025-12-20")
        assert find_cue_for_audio(audio, cue_directory=cue_dir) is None

    def test_missing_dir(self, tmp_path):
        assert find_cue_for_audio(str(tmp_path / "mix.flac"),
                                  cue_directory=str(tmp_path / "nope")) is None


class TestMergeTracklists:
    def test_cue_only(self):
        cue = [CueTrack(1, "T", "A", 0, 0, 0)]
        result = merge_tracklists(cue_tracks=cue, shazam_tracks=None)
        assert result.source == "djctl"
        assert result.tracklist[0]["title"] == "T"

    def test_shazam_backfills_placeholder(self):
        cue = [CueTrack(1, "Track 1", "Unknown", 0, 0, 0)]
        shazam = [{"artist": "Real Artist", "title": "Real Title", "timestamp_seconds": 5}]
        result = merge_tracklists(cue_tracks=cue, shazam_tracks=shazam)
        assert result.source == "merged"
        assert result.tracklist[0]["title"] == "Real Title"
        assert result.tracklist[0]["artist"] == "Real Artist"

    def test_shazam_only(self):
        shazam = [{"artist": "A", "title": "T", "timestamp_seconds": 90}]
        result = merge_tracklists(cue_tracks=None, shazam_tracks=shazam)
        assert result.source == "shazam"
        assert result.tracklist[0]["timestamp_formatted"] == "1:30"

    def test_nothing(self):
        result = merge_tracklists(cue_tracks=None, shazam_tracks=None)
        assert result.source == "none"
        assert result.tracklist == []
