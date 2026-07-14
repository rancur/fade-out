"""Tests for the Mixcloud uploader (no live network).

httpx.AsyncClient is swapped for a fake so upload/verify/update paths are
exercised against recorded requests and canned responses.
"""

import pytest

import app.services.mixcloud_uploader as mc_mod
from app.services.mixcloud_uploader import MixcloudUploader


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
    monkeypatch.setattr(mc_mod.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.last_post = None
    FakeAsyncClient.last_get = None
    FakeAsyncClient.post_response = FakeResp()
    FakeAsyncClient.get_response = FakeResp()
    yield


class TestPureHelpers:
    def test_key_from_url_public(self):
        assert (
            MixcloudUploader._key_from_url("https://www.mixcloud.com/willsee/night-mix/")
            == "/willsee/night-mix/"
        )

    def test_key_from_url_adds_trailing_slash(self):
        assert (
            MixcloudUploader._key_from_url("https://www.mixcloud.com/willsee/night-mix")
            == "/willsee/night-mix/"
        )

    def test_key_from_url_api_base(self):
        assert (
            MixcloudUploader._key_from_url("https://api.mixcloud.com/willsee/night-mix/")
            == "/willsee/night-mix/"
        )

    def test_key_from_url_empty(self):
        assert MixcloudUploader._key_from_url("") is None

    def test_tag_fields_caps_at_five_and_skips_blank(self):
        fields = MixcloudUploader._tag_fields(
            ["house", "", "techno", "a", "b", "c", "d"]
        )
        names = [f[0] for f in fields]
        # blanks dropped, capped at MAX_TAGS (5 input slots consumed incl blank)
        assert "tags-0-tag" in names
        assert all(v[1][1] for v in fields)  # no empty tag values
        assert len(fields) <= 5

    def test_key_from_result_variants(self):
        assert MixcloudUploader._key_from_result({"result": {"key": "/u/s/"}}) == "/u/s/"
        assert MixcloudUploader._key_from_result({"key": "/u/s/"}) == "/u/s/"
        assert MixcloudUploader._key_from_result({}) is None


class TestTokenGuard:
    def test_require_token_raises_without_token(self):
        up = MixcloudUploader(db_settings_json={})
        with pytest.raises(RuntimeError):
            up._require_token()

    def test_token_from_db_settings(self):
        up = MixcloudUploader(db_settings_json={"mixcloud_access_token": "tok"})
        assert up._require_token() == "tok"


class TestUpload:
    async def test_upload_rejects_flac(self, tmp_path):
        f = tmp_path / "mix.flac"
        f.write_bytes(b"x")
        up = MixcloudUploader(db_settings_json={"mixcloud_access_token": "tok"})
        with pytest.raises(ValueError):
            await up.upload(str(f), "Title", "Desc", ["house"])

    async def test_upload_missing_file(self):
        up = MixcloudUploader(db_settings_json={"mixcloud_access_token": "tok"})
        with pytest.raises(FileNotFoundError):
            await up.upload("/nope/mix.mp3", "Title", "Desc", ["house"])

    async def test_upload_success_returns_url(self, tmp_path):
        f = tmp_path / "mix.mp3"
        f.write_bytes(b"audio")
        FakeAsyncClient.post_response = FakeResp(
            200, json_data={"result": {"key": "/willsee/night-mix/", "success": True}}
        )
        up = MixcloudUploader(db_settings_json={"mixcloud_access_token": "tok"})
        url = await up.upload(str(f), "Night Mix", "desc", ["house", "techno"])
        assert url == "https://www.mixcloud.com/willsee/night-mix/"
        # Token passed as query param, file part present.
        assert FakeAsyncClient.last_post["params"]["access_token"] == "tok"

    async def test_upload_api_error_raises(self, tmp_path):
        f = tmp_path / "mix.mp3"
        f.write_bytes(b"audio")
        FakeAsyncClient.post_response = FakeResp(403, text="forbidden")
        up = MixcloudUploader(db_settings_json={"mixcloud_access_token": "tok"})
        with pytest.raises(RuntimeError):
            await up.upload(str(f), "Night Mix", "desc", ["house"])


class TestVerifyAndUpdate:
    async def test_verify_ok(self):
        FakeAsyncClient.get_response = FakeResp(200, json_data={"name": "Night Mix"})
        up = MixcloudUploader(db_settings_json={"mixcloud_access_token": "tok"})
        assert await up.verify_upload("https://www.mixcloud.com/willsee/night-mix/") is True

    async def test_verify_bad_url_false(self):
        up = MixcloudUploader(db_settings_json={"mixcloud_access_token": "tok"})
        assert await up.verify_upload("") is False

    async def test_update_description_posts_to_edit(self):
        FakeAsyncClient.post_response = FakeResp(200, json_data={"result": {"success": True}})
        up = MixcloudUploader(db_settings_json={"mixcloud_access_token": "tok"})
        ok = await up.update_description(
            "https://www.mixcloud.com/willsee/night-mix/", "new desc"
        )
        assert ok is True
        assert FakeAsyncClient.last_post["url"].endswith("/willsee/night-mix/edit/")
        assert FakeAsyncClient.last_post["data"]["description"] == "new desc"
