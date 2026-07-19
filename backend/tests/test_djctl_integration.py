"""Tests for CUE parsing, audio<->CUE matching, and tracklist merging."""

import os
import time
from datetime import datetime

from app.services.djctl_integration import (
    CueTrack,
    find_cue_for_audio,
    merge_tracklists,
    parse_cue_file,
    parse_cue_sessions,
    select_cue_tracks,
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

# DJCTL appends a fresh PERFORMER/TITLE/FILE header block per recorder restart:
# one .cue, two sessions whose timestamps each restart at 00:00.
MULTI_SESSION_CUE = """\
PERFORMER "Will See"
TITLE "Desert Session"
FILE "mix.flac" WAVE
  TRACK 01 AUDIO
    TITLE "A1"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "A2"
    INDEX 01 30:00:00
  TRACK 03 AUDIO
    TITLE "A3"
    INDEX 01 60:00:00
PERFORMER "Will See"
TITLE "Desert Session"
FILE "mix_2.flac" WAVE
  TRACK 01 AUDIO
    TITLE "B1"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "B2"
    INDEX 01 05:00:00
"""

# Timestamp rewind WITHOUT a new FILE header is also a session restart.
REWIND_CUE = """\
PERFORMER "Will See"
FILE "mix.flac" WAVE
  TRACK 01 AUDIO
    TITLE "A1"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "A2"
    INDEX 01 30:00:00
  TRACK 03 AUDIO
    TITLE "B1"
    INDEX 01 01:00:00
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


class TestParseCueSessions:
    def test_single_session(self, tmp_path):
        cue = tmp_path / "djctl-2025-12-05.cue"
        cue.write_text(SAMPLE_CUE)
        sessions = parse_cue_sessions(str(cue))
        assert len(sessions) == 1
        assert len(sessions[0]) == 3

    def test_splits_on_new_file_header(self, tmp_path):
        cue = tmp_path / "djctl-2025-12-05.cue"
        cue.write_text(MULTI_SESSION_CUE)
        sessions = parse_cue_sessions(str(cue))
        assert len(sessions) == 2
        assert [t.title for t in sessions[0]] == ["A1", "A2", "A3"]
        assert [t.title for t in sessions[1]] == ["B1", "B2"]
        # Each session's timestamps restart at 00:00
        assert sessions[1][0].timestamp_seconds == 0.0

    def test_splits_on_timestamp_rewind(self, tmp_path):
        cue = tmp_path / "djctl-2025-12-05.cue"
        cue.write_text(REWIND_CUE)
        sessions = parse_cue_sessions(str(cue))
        assert len(sessions) == 2
        assert [t.title for t in sessions[0]] == ["A1", "A2"]
        assert [t.title for t in sessions[1]] == ["B1"]

    def test_parse_cue_file_flattens_sessions(self, tmp_path):
        cue = tmp_path / "djctl-2025-12-05.cue"
        cue.write_text(MULTI_SESSION_CUE)
        tracks = parse_cue_file(str(cue))
        assert [t.title for t in tracks] == ["A1", "A2", "A3", "B1", "B2"]


class TestSelectCueTracks:
    def _write(self, tmp_path, content=MULTI_SESSION_CUE):
        cue = tmp_path / "djctl-2025-12-05.cue"
        cue.write_text(content)
        return str(cue)

    def test_picks_session_that_fits_duration(self, tmp_path):
        # Session A spans 3600s; a ~62 min mix fits it.
        cue = self._write(tmp_path)
        tracks = select_cue_tracks(cue, duration_seconds=3700.0)
        assert tracks is not None
        assert [t.title for t in tracks] == ["A1", "A2", "A3"]

    def test_rejects_session_longer_than_mix(self, tmp_path):
        # A 6-minute mix: session A (3600s) exceeds duration+slack, session B
        # (300s) fits and covers >= 50%.
        cue = self._write(tmp_path)
        tracks = select_cue_tracks(cue, duration_seconds=360.0)
        assert tracks is not None
        assert [t.title for t in tracks] == ["B1", "B2"]

    def test_returns_none_when_no_session_qualifies(self, tmp_path):
        # A ~2.8h mix: session A covers only 3600/10000 < 50%, session B even
        # less -> fingerprint-only fallback.
        cue = self._write(tmp_path)
        assert select_cue_tracks(cue, duration_seconds=10000.0) is None

    def test_no_duration_uses_longest_session(self, tmp_path):
        cue = self._write(tmp_path)
        tracks = select_cue_tracks(cue, duration_seconds=None)
        assert tracks is not None
        assert [t.title for t in tracks] == ["A1", "A2", "A3"]

    def test_single_session_fits(self, tmp_path):
        cue = self._write(tmp_path, content=SAMPLE_CUE)
        tracks = select_cue_tracks(cue, duration_seconds=500.0)
        assert tracks is not None
        assert len(tracks) == 3


class TestFindCueForAudio:
    def _make(self, tmp_path, cue_name, audio_date, audio_name="mix.flac"):
        cue_dir = tmp_path / "cues"
        cue_dir.mkdir()
        (cue_dir / cue_name).write_text(SAMPLE_CUE)
        audio = tmp_path / audio_name
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

    def test_filename_date_exact_match(self, tmp_path):
        # Filename date wins even when mtime is weeks later (fresh copy).
        cue_dir, audio = self._make(
            tmp_path, "djctl-2025-12-05.cue", "2025-12-20",
            audio_name="will-see-2025-12-05.flac",
        )
        assert find_cue_for_audio(audio, cue_directory=cue_dir) is not None

    def test_filename_date_no_slack(self, tmp_path):
        # One day off used to match via the +/-2 day slack; now it must not:
        # a freshly-copied master matched a CUE from an unrelated recent
        # session and published the wrong tracklist.
        cue_dir, audio = self._make(
            tmp_path, "djctl-2025-12-06.cue", "2025-12-06",
            audio_name="will-see-2025-12-05.flac",
        )
        assert find_cue_for_audio(audio, cue_directory=cue_dir) is None

    def test_mtime_fallback_requires_exact_day(self, tmp_path):
        # No date in the audio filename -> mtime fallback, but only an exact
        # same-day CUE is accepted (no more nearest-within-2-days).
        cue_dir, audio = self._make(tmp_path, "djctl-2025-12-05.cue", "2025-12-06")
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
