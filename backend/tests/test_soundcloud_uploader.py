"""Tests for SoundCloud OAuth token rotation persistence (no live network).

httpx.AsyncClient is swapped for a fake so the refresh / password-grant paths
can be exercised with canned token responses. SoundCloud ROTATES the refresh
token on every refresh — the uploader must hand the new pair to the
``on_tokens_refreshed`` callback or the stored refresh token goes stale and the
next refresh fails with invalid_grant.
"""

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
