"""Tests for pure handler helpers (no DB / network)."""

from types import SimpleNamespace

import app.services.handlers as handlers_mod
from app.models import Mix
from app.services.handlers import (
    _ensure_tracklist_section,
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


class TestEnsureTracklistSection:
    """The LLM sometimes weaves track names into prose and omits the actual
    tracklist section (observed live). The tracklist is the product."""

    TRACKS = [
        {"timestamp_formatted": "0:00", "artist": "A1", "title": "T1"},
        {"timestamp_formatted": "3:30", "artist": "A2", "title": "T2"},
    ]

    def test_injects_before_brand_links_marker(self):
        desc = "Great mix prose.\nTwitch: https://twitch.tv/willsee\nIG: x"
        out = _ensure_tracklist_section(desc, self.TRACKS)
        assert "Tracklist:\n0:00 A1 - T1\n3:30 A2 - T2" in out
        assert out.index("Tracklist:") < out.index("Twitch: https://")

    def test_appends_at_end_when_marker_absent(self):
        out = _ensure_tracklist_section("Prose only.", self.TRACKS)
        assert out.endswith("Tracklist:\n0:00 A1 - T1\n3:30 A2 - T2")

    def test_skipped_when_section_already_present(self):
        desc = "Prose.\n\nTracklist:\n0:00 A1 - T1"
        assert _ensure_tracklist_section(desc, self.TRACKS) == desc

    def test_skipped_when_no_tracklist(self):
        assert _ensure_tracklist_section("Prose.", []) == "Prose."


class _RereadSession:
    """Returns the mix for Mix lookups, None otherwise (no AppSettings row)."""

    def __init__(self, mix):
        self._mix = mix

    async def get(self, model, key):
        if model is Mix:
            return self._mix
        return None


class TestRereadTitleConsistency:
    async def test_published_title_restored_in_descriptions(self, tmp_path, monkeypatch):
        # The regenerated description prose references the transient creative
        # title the generator just invented; the published title is restored
        # afterwards — without the replace, live SC/YT descriptions opened
        # with the wrong mix name.
        audio = tmp_path / "mix.flac"
        audio.write_bytes(b"x")
        mix = SimpleNamespace(
            title="Published Title",
            title_youtube="Published YT Title",
            audio_file_path=str(audio),
            description_soundcloud=None,
            description_youtube=None,
            youtube_url=None,
            soundcloud_url=None,
        )

        async def fake_analyze(mix_id, session):
            return {"tracks_found": 3, "tracklist_source": "cue"}

        async def fake_generate_description(mix_id, session):
            mix.title = "Transient Creative Title"
            mix.description_soundcloud = "Transient Creative Title opens with heat."
            mix.description_youtube = "Enjoy Transient Creative Title tonight."
            return {}

        monkeypatch.setattr(handlers_mod, "handle_analyze", fake_analyze)
        monkeypatch.setattr(
            handlers_mod, "handle_generate_description", fake_generate_description
        )

        out = await handlers_mod.handle_reread_tracklist("m1", _RereadSession(mix))

        assert mix.title == "Published Title"
        assert mix.title_youtube == "Published YT Title"
        assert "Transient Creative Title" not in mix.description_soundcloud
        assert mix.description_soundcloud == "Published Title opens with heat."
        assert mix.description_youtube == "Enjoy Published Title tonight."
        assert out["tracks_found"] == 3
        assert out["descriptions_updated"] == {"youtube": False, "soundcloud": False}
