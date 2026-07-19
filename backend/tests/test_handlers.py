"""Tests for pure handler helpers (no DB / network)."""

from app.services.handlers import (
    _sc_token_persister,
    _title_from_filename,
    extract_youtube_video_id,
)


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


class _FakeSession:
    def __init__(self):
        self.flushed = False

    async def flush(self):
        self.flushed = True


class _FakeAppSettings:
    def __init__(self, settings_json=None):
        self.settings_json = settings_json


class TestScTokenPersister:
    async def test_persists_rotated_pair_and_reassigns_dict(self):
        original = {"soundcloud_refresh_token": "old-rt", "other_key": "keep"}
        app_settings = _FakeAppSettings(settings_json=original)
        session = _FakeSession()

        persist = _sc_token_persister(session, app_settings)
        await persist("new-at", "new-rt")

        assert app_settings.settings_json["soundcloud_access_token"] == "new-at"
        assert app_settings.settings_json["soundcloud_refresh_token"] == "new-rt"
        assert app_settings.settings_json["other_key"] == "keep"
        # The dict must be REASSIGNED (new object) so SQLAlchemy detects the
        # JSON column change; in-place mutation would be silently dropped.
        assert app_settings.settings_json is not original
        assert session.flushed is True

    async def test_missing_refresh_token_keeps_existing(self):
        app_settings = _FakeAppSettings(
            settings_json={"soundcloud_refresh_token": "old-rt"}
        )
        persist = _sc_token_persister(_FakeSession(), app_settings)
        await persist("new-at", None)

        assert app_settings.settings_json["soundcloud_access_token"] == "new-at"
        assert app_settings.settings_json["soundcloud_refresh_token"] == "old-rt"

    async def test_no_app_settings_is_noop(self):
        session = _FakeSession()
        persist = _sc_token_persister(session, None)
        await persist("new-at", "new-rt")  # must not raise
        assert session.flushed is False
