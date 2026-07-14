"""Tests for pure handler helpers (no DB / network)."""

from app.services.handlers import _title_from_filename, extract_youtube_video_id


class TestExtractYoutubeVideoId:
    def test_watch_url(self):
        assert extract_youtube_video_id(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_watch_url_extra_params(self):
        assert extract_youtube_video_id(
            "https://www.youtube.com/watch?list=abc&v=dQw4w9WgXcQ&t=30s"
        ) == "dQw4w9WgXcQ"

    def test_short_url(self):
        assert extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url_with_query(self):
        assert extract_youtube_video_id(
            "https://youtu.be/dQw4w9WgXcQ?t=42"
        ) == "dQw4w9WgXcQ"

    def test_shorts(self):
        assert extract_youtube_video_id(
            "https://www.youtube.com/shorts/dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_embed(self):
        assert extract_youtube_video_id(
            "https://www.youtube.com/embed/dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_bare_id(self):
        assert extract_youtube_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_empty(self):
        assert extract_youtube_video_id("") == ""


class TestTitleFromFilename:
    def test_separators_to_spaces_and_title_case(self):
        assert _title_from_filename("/watch/audio/desert_session-live.flac") == "Desert Session Live"

    def test_plain(self):
        assert _title_from_filename("midnight groove.flac") == "Midnight Groove"
