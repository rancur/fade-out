"""Tests for the cross_link handler: local description updates + live pushes.

CROSS_LINK_PUSH_ENABLED now defaults to True (with it off, cross-links only
ever landed in the DB and the published descriptions never carried them). The
local-update tests force the gate off; the push tests stub the uploaders.
"""

from types import SimpleNamespace

import pytest

from app.config import settings as app_config_settings
from app.models import Mix
from app.services.handlers import handle_cross_link


class FakeSession:
    """Minimal stand-in for AsyncSession.get().

    Returns the mix for Mix lookups and None otherwise (no AppSettings row).
    """

    def __init__(self, mix):
        self._mix = mix

    async def get(self, model, key):
        if model is Mix:
            return self._mix
        return None


def _mix(**kwargs):
    base = dict(
        soundcloud_url="https://soundcloud.com/thewillsee/mix",
        youtube_url="https://www.youtube.com/watch?v=abc123",
        description_soundcloud="SC body",
        description_youtube="YT body",
        mixcloud_url=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestHandleCrossLink:
    @pytest.fixture(autouse=True)
    def _disable_push(self, monkeypatch):
        # These tests exercise the local description mutation only.
        monkeypatch.setattr(app_config_settings, "CROSS_LINK_PUSH_ENABLED", False)
    async def test_links_both_descriptions_locally(self):
        mix = _mix()
        out = await handle_cross_link("m1", FakeSession(mix))
        assert set(out["updated_descriptions"]) == {
            "soundcloud_description",
            "youtube_description",
        }
        assert out["pushed_platforms"] == []
        assert mix.youtube_url in mix.description_soundcloud
        assert mix.soundcloud_url in mix.description_youtube

    async def test_skips_when_one_url_missing(self):
        mix = _mix(youtube_url=None)
        out = await handle_cross_link("m1", FakeSession(mix))
        assert out["updated_descriptions"] == []
        assert out["pushed_platforms"] == []

    async def test_idempotent_no_double_link(self):
        mix = _mix()
        await handle_cross_link("m1", FakeSession(mix))
        # Second run should not append the links again.
        out = await handle_cross_link("m1", FakeSession(mix))
        assert out["updated_descriptions"] == []
        assert mix.description_youtube.count(mix.soundcloud_url) == 1
        assert mix.description_soundcloud.count(mix.youtube_url) == 1


class _FakeYouTubeUploader:
    calls = []
    fail = False

    def __init__(self, db_settings_json=None, mix_id=None):
        pass

    async def update_description(self, video_id, description):
        if _FakeYouTubeUploader.fail:
            raise RuntimeError("yt api down")
        _FakeYouTubeUploader.calls.append((video_id, description))
        return True


class _FakeSoundCloudUploader:
    calls = []
    fail = False

    def __init__(self, db_settings_json=None, on_tokens_refreshed=None, mix_id=None):
        pass

    async def update_description(self, track_url, description):
        if _FakeSoundCloudUploader.fail:
            raise RuntimeError("sc api down")
        _FakeSoundCloudUploader.calls.append((track_url, description))
        return True


class TestCrossLinkPush:
    """With the (now default-on) push, cross-links reach the LIVE platforms."""

    @pytest.fixture(autouse=True)
    def _wire_push(self, monkeypatch):
        import app.services.soundcloud_uploader as sc_mod
        import app.services.youtube_uploader as yt_mod

        monkeypatch.setattr(app_config_settings, "CROSS_LINK_PUSH_ENABLED", True)
        monkeypatch.setattr(yt_mod, "YouTubeUploader", _FakeYouTubeUploader)
        monkeypatch.setattr(sc_mod, "SoundCloudUploader", _FakeSoundCloudUploader)
        _FakeYouTubeUploader.calls = []
        _FakeYouTubeUploader.fail = False
        _FakeSoundCloudUploader.calls = []
        _FakeSoundCloudUploader.fail = False
        yield

    async def test_pushes_cross_linked_descriptions_to_both_platforms(self):
        mix = _mix()
        out = await handle_cross_link("m1", FakeSession(mix))

        assert out["pushed_platforms"] == ["youtube", "soundcloud"]
        # YouTube got the extracted video id + the SC-linked description.
        (video_id, yt_desc), = _FakeYouTubeUploader.calls
        assert video_id == "abc123"
        assert mix.soundcloud_url in yt_desc
        # SoundCloud got the track url + the YT-linked description.
        (track_url, sc_desc), = _FakeSoundCloudUploader.calls
        assert track_url == mix.soundcloud_url
        assert mix.youtube_url in sc_desc

    async def test_push_failure_never_raises_and_other_platform_still_pushes(self):
        _FakeYouTubeUploader.fail = True
        mix = _mix()
        # Must not raise — cross_link is the pipeline's final step.
        out = await handle_cross_link("m1", FakeSession(mix))

        assert out["pushed_platforms"] == ["soundcloud"]
        assert len(_FakeSoundCloudUploader.calls) == 1
        # The local descriptions were still cross-linked.
        assert mix.youtube_url in mix.description_soundcloud

    async def test_no_push_when_urls_missing(self):
        mix = _mix(youtube_url=None)
        out = await handle_cross_link("m1", FakeSession(mix))
        assert out["pushed_platforms"] == []
        assert _FakeYouTubeUploader.calls == []
        assert _FakeSoundCloudUploader.calls == []
