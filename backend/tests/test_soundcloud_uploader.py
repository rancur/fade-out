"""Tests for SoundCloud OAuth token rotation persistence (no live network).

httpx.AsyncClient is swapped for a fake so the refresh / password-grant paths
can be exercised with canned token responses. SoundCloud ROTATES the refresh
token on every refresh — the uploader must hand the new pair to the
``on_tokens_refreshed`` callback or the stored refresh token goes stale and the
next refresh fails with invalid_grant.
"""

import os

import pytest

import app.services.soundcloud_uploader as sc_mod
from app.services.soundcloud_uploader import SoundCloudUploader


class FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class FakeAsyncClient:
    """Records the last request and returns pre-set responses."""

    post_response = FakeResp()
    get_response = FakeResp()
    last_post = None
    last_get = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        FakeAsyncClient.last_post = {"url": url, **kwargs}
        return FakeAsyncClient.post_response

    async def get(self, url, **kwargs):
        FakeAsyncClient.last_get = {"url": url, **kwargs}
        return FakeAsyncClient.get_response


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    monkeypatch.setattr(sc_mod.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.last_post = None
    FakeAsyncClient.last_get = None
    FakeAsyncClient.post_response = FakeResp()
    FakeAsyncClient.get_response = FakeResp()
    yield


def _uploader(**kwargs) -> SoundCloudUploader:
    return SoundCloudUploader(
        db_settings_json={
            "soundcloud_client_id": "cid",
            "soundcloud_client_secret": "csecret",
            "soundcloud_refresh_token": "old-refresh",
        },
        **kwargs,
    )


class TestTokenRotationPersistence:
    async def test_refresh_invokes_persist_callback_with_rotated_pair(self):
        FakeAsyncClient.post_response = FakeResp(
            200, json_data={"access_token": "new-access", "refresh_token": "new-refresh"}
        )
        recorded = {}

        async def persist(access_token, refresh_token):
            recorded["access"] = access_token
            recorded["refresh"] = refresh_token

        up = _uploader(on_tokens_refreshed=persist)
        token = await up._refresh_access_token()

        assert token == "new-access"
        assert recorded == {"access": "new-access", "refresh": "new-refresh"}
        # In-memory state carries the rotated pair too.
        assert up._refresh_token == "new-refresh"
        assert FakeAsyncClient.last_post["data"]["grant_type"] == "refresh_token"

    async def test_password_grant_invokes_persist_callback(self, monkeypatch):
        FakeAsyncClient.post_response = FakeResp(
            200, json_data={"access_token": "pw-access", "refresh_token": "pw-refresh"}
        )
        recorded = {}

        async def persist(access_token, refresh_token):
            recorded["access"] = access_token
            recorded["refresh"] = refresh_token

        up = _uploader(on_tokens_refreshed=persist)
        up._email = "dj@example.com"
        up._password = "hunter2"
        token = await up._password_grant()

        assert token == "pw-access"
        assert recorded == {"access": "pw-access", "refresh": "pw-refresh"}

    async def test_failed_refresh_does_not_invoke_callback(self):
        FakeAsyncClient.post_response = FakeResp(400, text="invalid_grant")
        calls = []

        async def persist(access_token, refresh_token):
            calls.append((access_token, refresh_token))

        up = _uploader(on_tokens_refreshed=persist)
        assert await up._refresh_access_token() is None
        assert calls == []

    async def test_persist_callback_errors_are_swallowed(self):
        FakeAsyncClient.post_response = FakeResp(
            200, json_data={"access_token": "new-access", "refresh_token": "new-refresh"}
        )

        async def persist(access_token, refresh_token):
            raise RuntimeError("db down")

        up = _uploader(on_tokens_refreshed=persist)
        # Refresh still succeeds; persistence is best-effort.
        assert await up._refresh_access_token() == "new-access"

    async def test_refresh_without_callback_still_works(self):
        FakeAsyncClient.post_response = FakeResp(
            200, json_data={"access_token": "new-access", "refresh_token": "new-refresh"}
        )
        up = _uploader()
        assert await up._refresh_access_token() == "new-access"
        assert up._refresh_token == "new-refresh"


class TestOversizedUploadTranscode:
    """api.soundcloud.com/tracks 413s on large bodies (observed live at 2.9GB
    despite the web uploader's documented 4GB cap). Masters over the API cap
    are transcoded to 320kbps MP3 (ffmpeg) before the API upload."""

    def _uploader_with_token(self):
        return SoundCloudUploader(
            db_settings_json={
                "soundcloud_client_id": "cid",
                "soundcloud_client_secret": "csecret",
                "soundcloud_access_token": "at",
            }
        )

    def _patch_getsize(self, monkeypatch, flac_size):
        # Oversized source; anything else (the transcoded .mp3) reads small.
        monkeypatch.setattr(
            sc_mod.os.path,
            "getsize",
            lambda p: flac_size if str(p).endswith(".flac") else 1234,
        )

    async def test_oversized_flac_is_transcoded_uploaded_and_cleaned_up(
        self, tmp_path, monkeypatch
    ):
        f = tmp_path / "big-mix.flac"
        f.write_bytes(b"x")
        self._patch_getsize(monkeypatch, int(2.9 * 1024 ** 3))

        captured = {}

        async def fake_exec(*args, **kwargs):
            out_path = args[-1]
            with open(out_path, "wb") as fh:
                fh.write(b"mp3data")
            captured["cmd"] = args

            class Proc:
                returncode = 0

                async def communicate(self):
                    return (b"", b"")

            return Proc()

        monkeypatch.setattr(sc_mod.asyncio, "create_subprocess_exec", fake_exec)
        FakeAsyncClient.get_response = FakeResp(200)  # /me token check
        FakeAsyncClient.post_response = FakeResp(
            201,
            json_data={"permalink_url": "https://soundcloud.com/willsee/big-mix", "id": 1},
        )

        up = self._uploader_with_token()
        url = await up._api_upload(str(f), "Big Mix", "desc", "House", ["house"], None)

        assert url == "https://soundcloud.com/willsee/big-mix"
        # ffmpeg invoked with the 320k libmp3lame + metadata-preserving args.
        cmd = captured["cmd"]
        assert cmd[0] == "ffmpeg"
        assert "libmp3lame" in cmd
        assert sc_mod.TRANSCODE_BITRATE in cmd
        assert "-map_metadata" in cmd
        # The MP3 (not the FLAC) was uploaded, with the right content type...
        name, _fh, content_type = FakeAsyncClient.last_post["files"]["track[asset_data]"]
        assert name.endswith(".sc-upload.mp3")
        assert content_type == "audio/mpeg"
        # ...and the temp file was unlinked afterwards.
        assert not os.path.exists(cmd[-1])

    async def test_transcode_failure_raises_with_stderr(self, tmp_path, monkeypatch):
        f = tmp_path / "big-mix.flac"
        f.write_bytes(b"x")
        self._patch_getsize(monkeypatch, int(2.9 * 1024 ** 3))

        async def fake_exec(*args, **kwargs):
            class Proc:
                returncode = 1

                async def communicate(self):
                    return (b"", b"boom: no such codec")

            return Proc()

        monkeypatch.setattr(sc_mod.asyncio, "create_subprocess_exec", fake_exec)
        FakeAsyncClient.get_response = FakeResp(200)

        up = self._uploader_with_token()
        with pytest.raises(RuntimeError) as exc_info:
            await up._api_upload(str(f), "Big Mix", "desc", "House", ["house"], None)
        assert "no such codec" in str(exc_info.value)

    async def test_small_file_skips_transcode(self, tmp_path, monkeypatch):
        f = tmp_path / "small-mix.flac"
        f.write_bytes(b"x")
        monkeypatch.setattr(sc_mod.os.path, "getsize", lambda p: 1000)

        async def fail_exec(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("transcode must not run for files under the cap")

        monkeypatch.setattr(sc_mod.asyncio, "create_subprocess_exec", fail_exec)
        FakeAsyncClient.get_response = FakeResp(200)
        FakeAsyncClient.post_response = FakeResp(
            201,
            json_data={"permalink_url": "https://soundcloud.com/willsee/small-mix", "id": 2},
        )

        up = self._uploader_with_token()
        url = await up._api_upload(str(f), "Small Mix", "desc", "House", ["house"], None)

        assert url == "https://soundcloud.com/willsee/small-mix"
        name, _fh, content_type = FakeAsyncClient.last_post["files"]["track[asset_data]"]
        assert name.endswith(".flac")
        assert content_type == "audio/flac"

    def test_upload_constants(self):
        # The public API's earned-knowledge cap (413 above this), not the web
        # uploader's 4GB. Timeout stays 1h for large uploads on home upstream.
        assert sc_mod.API_MAX_UPLOAD_BYTES == 450 * 1024 * 1024
        assert sc_mod.UPLOAD_TIMEOUT == 3600
