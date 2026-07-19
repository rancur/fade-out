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


# ---------------------------------------------------------------------------
# YouTube playlist APIs (Feature: playlist grouping)
# ---------------------------------------------------------------------------

class PlaylistService:
    """Fake youtube service covering playlists() + playlistItems()."""

    def __init__(self, item_pages=None):
        self.created = []
        self.inserted_items = []
        self.item_pages = item_pages or [{"items": []}]
        self._page = 0

    def playlists(self):
        svc = self

        class P:
            def insert(self, **kw):
                svc.created.append(kw)
                return _Req({"id": "PL-NEW"})

        return P()

    def playlistItems(self):
        svc = self

        class PI:
            def list(self, **kw):
                page = svc.item_pages[svc._page]
                svc._page = min(svc._page + 1, len(svc.item_pages) - 1)
                return _Req(page)

            def insert(self, **kw):
                svc.inserted_items.append(kw)
                return _Req({})

        return PI()


class TestYouTubePlaylistApis:
    async def test_create_playlist_returns_id(self, monkeypatch):
        svc = PlaylistService()
        monkeypatch.setattr(YouTubeUploader, "_get_service", lambda self: svc)
        up = YouTubeUploader()
        pid = await up.create_playlist("Will See | House", description="d")
        assert pid == "PL-NEW"
        body = svc.created[0]["body"]
        assert body["snippet"]["title"] == "Will See | House"
        assert body["status"]["privacyStatus"] == "public"

    async def test_list_playlist_video_ids_paginates(self, monkeypatch):
        svc = PlaylistService(
            item_pages=[
                {
                    "items": [{"contentDetails": {"videoId": "a"}}],
                    "nextPageToken": "t2",
                },
                {"items": [{"contentDetails": {"videoId": "b"}}]},
            ]
        )
        monkeypatch.setattr(YouTubeUploader, "_get_service", lambda self: svc)
        up = YouTubeUploader()
        assert await up.list_playlist_video_ids("PL") == ["a", "b"]


# ---------------------------------------------------------------------------
# SoundCloud playlist APIs (Feature: playlist grouping)
# ---------------------------------------------------------------------------

class PlaylistClient:
    """Fake httpx.AsyncClient recording GET/POST/PUT for playlist endpoints."""

    get_responses = []
    post_response = None
    put_response = None
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        PlaylistClient.calls.append(("get", url, kwargs))
        return PlaylistClient.get_responses.pop(0)

    async def post(self, url, **kwargs):
        PlaylistClient.calls.append(("post", url, kwargs))
        return PlaylistClient.post_response

    async def put(self, url, **kwargs):
        PlaylistClient.calls.append(("put", url, kwargs))
        return PlaylistClient.put_response


@pytest.fixture
def sc_playlist_uploader(monkeypatch):
    monkeypatch.setattr(sc_mod.httpx, "AsyncClient", PlaylistClient)

    async def fake_token(self):
        return "tok"

    monkeypatch.setattr(SoundCloudUploader, "_ensure_access_token", fake_token)
    PlaylistClient.get_responses = []
    PlaylistClient.post_response = None
    PlaylistClient.put_response = None
    PlaylistClient.calls = []
    return SoundCloudUploader()


class TestSoundCloudPlaylistApis:
    async def test_list_playlists_follows_next_href(self, sc_playlist_uploader):
        PlaylistClient.get_responses = [
            FakeResp(200, {"collection": [{"id": 1}], "next_href": "http://n/2"}),
            FakeResp(200, {"collection": [{"id": 2}]}),
        ]
        out = await sc_playlist_uploader.list_playlists()
        assert [p["id"] for p in out] == [1, 2]
        # second call hits next_href without re-sending params
        assert PlaylistClient.calls[1][1] == "http://n/2"
        assert PlaylistClient.calls[1][2].get("params") is None

    async def test_list_playlists_error_raises(self, sc_playlist_uploader):
        PlaylistClient.get_responses = [FakeResp(500, text="boom")]
        with pytest.raises(RuntimeError, match="500"):
            await sc_playlist_uploader.list_playlists()

    async def test_create_playlist_posts_tracks(self, sc_playlist_uploader):
        PlaylistClient.post_response = FakeResp(201, {"id": 42, "title": "T"})
        created = await sc_playlist_uploader.create_playlist("T", ["7", 8])
        assert created["id"] == 42
        method, url, kwargs = PlaylistClient.calls[0]
        assert method == "post" and url.endswith("/playlists")
        assert kwargs["json"] == {
            "playlist": {
                "title": "T",
                "sharing": "public",
                "tracks": [{"id": 7}, {"id": 8}],
            }
        }
        assert kwargs["headers"]["Authorization"] == "OAuth tok"

    async def test_add_track_fetches_then_puts_full_list(self, sc_playlist_uploader):
        # SC's PUT replaces the track array wholesale: existing order must be
        # preserved and the new track appended.
        PlaylistClient.get_responses = [
            FakeResp(200, {"tracks": [{"id": 5}, {"id": 6}]})
        ]
        PlaylistClient.put_response = FakeResp(200, {})
        await sc_playlist_uploader.add_track_to_playlist(42, "7")
        method, url, kwargs = PlaylistClient.calls[-1]
        assert method == "put" and url.endswith("/playlists/42")
        assert kwargs["json"] == {
            "playlist": {"tracks": [{"id": 5}, {"id": 6}, {"id": 7}]}
        }

    async def test_add_track_already_member_is_noop(self, sc_playlist_uploader):
        PlaylistClient.get_responses = [FakeResp(200, {"tracks": [{"id": 7}]})]
        await sc_playlist_uploader.add_track_to_playlist(42, 7)
        assert [c[0] for c in PlaylistClient.calls] == ["get"]  # no PUT

    async def test_add_track_put_failure_raises(self, sc_playlist_uploader):
        PlaylistClient.get_responses = [FakeResp(200, {"tracks": []})]
        PlaylistClient.put_response = FakeResp(403, text="no")
        with pytest.raises(RuntimeError, match="403"):
            await sc_playlist_uploader.add_track_to_playlist(42, 7)
