"""Tests for the Mixcloud pipeline handlers (gating + cross-link weaving)."""

from types import SimpleNamespace

import pytest

import app.services.handlers as handlers
from app.services.handlers import (
    handle_cross_link,
    handle_upload_mixcloud,
    handle_verify_mixcloud,
)


class FakeSession:
    def __init__(self, mix, app_settings=None):
        self._mix = mix
        self._app_settings = app_settings

    async def get(self, model, key):
        # AppSettings lookups use integer key 1; Mix lookups use the mix id.
        if getattr(model, "__name__", "") == "AppSettings":
            return self._app_settings
        return self._mix


def _mix(**kw):
    base = dict(
        id="m1",
        title="Night Mix",
        audio_file_path="/watch/audio/mix.mp3",
        mixcloud_url=None,
        genres=["house"],
        vibes=["groovy"],
        tags=["house", "dj mix"],
        tracklist=[],
        description_soundcloud="SC body",
        cover_art_path=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestUploadGating:
    async def test_skips_when_disabled(self, monkeypatch):
        monkeypatch.setattr(handlers.settings, "MIXCLOUD_ENABLED", False)
        out = await handle_upload_mixcloud("m1", FakeSession(_mix()))
        assert out["skipped"] is True

    async def test_enabled_but_no_token_skips(self, monkeypatch):
        monkeypatch.setattr(handlers.settings, "MIXCLOUD_ENABLED", True)
        monkeypatch.setattr(handlers.settings, "MIXCLOUD_ACCESS_TOKEN", "")
        out = await handle_upload_mixcloud("m1", FakeSession(_mix()))
        assert out["skipped"] is True
        assert "token" in out["reason"].lower()

    async def test_enabled_with_token_uploads(self, monkeypatch, tmp_path):
        f = tmp_path / "mix.mp3"
        f.write_bytes(b"audio")
        monkeypatch.setattr(handlers.settings, "MIXCLOUD_ENABLED", True)
        monkeypatch.setattr(handlers.settings, "MIXCLOUD_ACCESS_TOKEN", "tok")

        uploaded = {}

        class FakeUploader:
            def __init__(self, db_settings_json=None):
                pass

            async def upload(self, audio_path, title, description, tags, cover_art_path=None):
                uploaded["called"] = True
                return "https://www.mixcloud.com/willsee/night-mix/"

        import app.services.mixcloud_uploader as mc_mod
        monkeypatch.setattr(mc_mod, "MixcloudUploader", FakeUploader)

        mix = _mix(audio_file_path=str(f))
        out = await handle_upload_mixcloud("m1", FakeSession(mix))
        assert uploaded.get("called") is True
        assert out["mixcloud_url"] == "https://www.mixcloud.com/willsee/night-mix/"
        assert mix.mixcloud_url == "https://www.mixcloud.com/willsee/night-mix/"


class TestVerifyMixcloud:
    async def test_skips_without_url(self):
        out = await handle_verify_mixcloud("m1", FakeSession(_mix(mixcloud_url=None)))
        assert out["skipped"] is True


class TestCrossLinkWithMixcloud:
    async def test_mixcloud_url_woven_into_descriptions(self):
        mix = SimpleNamespace(
            soundcloud_url="https://soundcloud.com/thewillsee/mix",
            youtube_url="https://www.youtube.com/watch?v=abc",
            mixcloud_url="https://www.mixcloud.com/willsee/night-mix/",
            description_soundcloud="SC body",
            description_youtube="YT body",
        )
        out = await handle_cross_link("m1", FakeSession(mix))
        assert mix.mixcloud_url in mix.description_soundcloud
        assert mix.mixcloud_url in mix.description_youtube
        assert out["mixcloud_url"] == mix.mixcloud_url
        # Push stays off by default.
        assert out["pushed_platforms"] == []
