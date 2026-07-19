"""New catalog editing APIs on the uploaders (no live network).

YouTube: update_video_fields must fetch the current snippet first and mutate
only the requested fields (videos.update replaces the whole part). SoundCloud:
update_track_fields must PUT only the requested track[...] fields with
upload-style tag formatting.
"""

import pytest

import app.services.soundcloud_uploader as sc_mod
from app.services.soundcloud_uploader import SoundCloudUploader
from app.services.youtube_uploader import YouTubeUploader


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result=None):
        self._result = result or {}

    def execute(self):
        return self._result


class SnippetService:
    def __init__(self, snippet=None, exists=True):
        self.snippet = snippet if snippet is not None else {
            "title": "Old", "description": "Old desc", "categoryId": "10",
            "tags": ["old"],
        }
        self.exists = exists
        self.updates = []

    def videos(self):
        svc = self

        class V:
            def list(self, **kw):
                if not svc.exists:
                    return _Req({"items": []})
                return _Req({"items": [{"snippet": dict(svc.snippet)}]})

            def update(self, **kw):
                svc.updates.append(kw)
                return _Req({})

        return V()


class TestUpdateVideoFields:
    async def test_mutates_only_target_field(self, monkeypatch):
        svc = SnippetService()
        monkeypatch.setattr(YouTubeUploader, "_get_service", lambda self: svc)
        up = YouTubeUploader()
        await up.update_video_fields("vid1", description="New desc")

        assert len(svc.updates) == 1
        body = svc.updates[0]["body"]
        assert body["id"] == "vid1"
        assert body["snippet"]["description"] == "New desc"
        # untouched fields survive the whole-part replacement
        assert body["snippet"]["title"] == "Old"
        assert body["snippet"]["categoryId"] == "10"
        assert body["snippet"]["tags"] == ["old"]

    async def test_limits_enforced(self, monkeypatch):
        svc = SnippetService()
        monkeypatch.setattr(YouTubeUploader, "_get_service", lambda self: svc)
        up = YouTubeUploader()
        await up.update_video_fields("vid1", title="T" * 200, description="D" * 6000)
        body = svc.updates[0]["body"]
        assert len(body["snippet"]["title"]) == 100
        assert len(body["snippet"]["description"]) == 5000

    async def test_missing_video_raises(self, monkeypatch):
        svc = SnippetService(exists=False)
        monkeypatch.setattr(YouTubeUploader, "_get_service", lambda self: svc)
        up = YouTubeUploader()
        with pytest.raises(RuntimeError, match="not found"):
            await up.update_video_fields("ghost", title="x")
        assert svc.updates == []


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class PutClient:
    response = FakeResp()
    last_put = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def put(self, url, **kwargs):
        PutClient.last_put = {"url": url, **kwargs}
        return PutClient.response


@pytest.fixture
def sc_uploader(monkeypatch):
    monkeypatch.setattr(sc_mod.httpx, "AsyncClient", PutClient)

    async def fake_token(self):
        return "tok"

    monkeypatch.setattr(SoundCloudUploader, "_ensure_access_token", fake_token)
    PutClient.response = FakeResp()
    PutClient.last_put = None
    return SoundCloudUploader()


class TestUpdateTrackFields:
    async def test_puts_only_requested_fields(self, sc_uploader):
        await sc_uploader.update_track_fields("42", description="New desc")
        put = PutClient.last_put
        assert put["url"].endswith("/tracks/42")
        assert put["data"] == {"track[description]": "New desc"}
        assert put["headers"]["Authorization"] == "OAuth tok"

    async def test_tags_formatted_like_upload(self, sc_uploader):
        await sc_uploader.update_track_fields("42", tags=["house", "dj mix"])
        assert PutClient.last_put["data"]["track[tag_list]"] == 'house "dj mix"'

    async def test_error_status_raises(self, sc_uploader):
        PutClient.response = FakeResp(403, text="forbidden")
        with pytest.raises(RuntimeError, match="403"):
            await sc_uploader.update_track_fields("42", title="x")

    async def test_noop_without_fields(self, sc_uploader):
        await sc_uploader.update_track_fields("42")
        assert PutClient.last_put is None

    async def test_missing_artwork_raises(self, sc_uploader):
        with pytest.raises(FileNotFoundError):
            await sc_uploader.update_track_fields("42", artwork_path="/nope/x.jpg")
