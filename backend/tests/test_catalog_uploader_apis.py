"""New catalog editing APIs on the uploaders (no live network).

YouTube: update_video_fields must fetch the current snippet first and mutate
only the requested fields (videos.update replaces the whole part). SoundCloud:
update_track_fields must PUT only the requested track[...] fields with
upload-style tag formatting.
"""

import json

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

# A realistic modern playlist id: "PL" + 32 chars = 34 total. The live 404
# incident involved a 13-char id, so fakes use full-length ids to prove the
# round-trip never truncates.
FULL_PLAYLIST_ID = "PL" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
assert len(FULL_PLAYLIST_ID) == 34


class PlaylistService:
    """Fake youtube service covering playlists() + playlistItems()."""

    def __init__(self, item_pages=None, created_id=FULL_PLAYLIST_ID):
        self.created = []
        self.inserted_items = []
        self.listed_item_calls = []
        self.item_pages = item_pages or [{"items": []}]
        self._page = 0
        self.created_id = created_id

    def playlists(self):
        svc = self

        class P:
            def insert(self, **kw):
                svc.created.append(kw)
                return _Req({"id": svc.created_id})

        return P()

    def playlistItems(self):
        svc = self

        class PI:
            def list(self, **kw):
                svc.listed_item_calls.append(kw)
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
        assert pid == FULL_PLAYLIST_ID
        body = svc.created[0]["body"]
        assert body["snippet"]["title"] == "Will See | House"
        assert body["status"]["privacyStatus"] == "public"

    async def test_full_id_survives_create_then_listing_round_trip(self, monkeypatch):
        # Regression for the live 404: a realistic 34-char playlist id must
        # reach playlistItems.list exactly as playlists.insert returned it.
        svc = PlaylistService()
        monkeypatch.setattr(YouTubeUploader, "_get_service", lambda self: svc)
        up = YouTubeUploader()
        pid = await up.create_playlist("Will See | Techno")
        assert pid == FULL_PLAYLIST_ID and len(pid) == 34
        await up.list_playlist_video_ids(pid)
        assert svc.listed_item_calls[0]["playlistId"] == FULL_PLAYLIST_ID

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
    """Fake httpx.AsyncClient recording GET + generic request() calls."""

    get_responses = []
    post_responses = []
    put_responses = []
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

    async def request(self, method, url, **kwargs):
        PlaylistClient.calls.append((method.lower(), url, kwargs))
        if method.upper() == "POST":
            return PlaylistClient.post_responses.pop(0)
        return PlaylistClient.put_responses.pop(0)


@pytest.fixture
def sc_playlist_uploader(monkeypatch):
    monkeypatch.setattr(sc_mod.httpx, "AsyncClient", PlaylistClient)

    async def fake_token(self):
        return "tok"

    monkeypatch.setattr(SoundCloudUploader, "_ensure_access_token", fake_token)
    PlaylistClient.get_responses = []
    PlaylistClient.post_responses = []
    PlaylistClient.put_responses = []
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

    async def test_create_playlist_posts_exact_json_body(self, sc_playlist_uploader):
        PlaylistClient.post_responses = [FakeResp(201, {"id": 42, "title": "T"})]
        created = await sc_playlist_uploader.create_playlist("T", ["7", 8])
        assert created["id"] == 42
        method, url, kwargs = PlaylistClient.calls[0]
        assert method == "post" and url.endswith("/playlists")
        # Exact documented body: {"playlist": {..., "tracks": [{"id": n}]}}
        # — tracks as an OBJECT LIST with id keys, never bare ints — sent as
        # explicitly serialized JSON with an explicit Content-Type.
        assert json.loads(kwargs["content"]) == {
            "playlist": {
                "title": "T",
                "sharing": "public",
                "tracks": [{"id": 7}, {"id": 8}],
            }
        }
        assert kwargs["headers"]["Content-Type"] == "application/json; charset=utf-8"
        assert kwargs["headers"]["Authorization"] == "OAuth tok"
        assert "json" not in kwargs and "data" not in kwargs

    async def test_create_playlist_422_parse_error_falls_back_to_form(
        self, sc_playlist_uploader
    ):
        # Live incident: 422 "Could not parse JSON request body". The retry
        # uses the same Rails-style form fields the track endpoints use.
        PlaylistClient.post_responses = [
            FakeResp(422, text='{"error": "Could not parse JSON request body"}'),
            FakeResp(201, {"id": 43, "title": "T"}),
        ]
        created = await sc_playlist_uploader.create_playlist("T", [7])
        assert created["id"] == 43
        assert [c[0] for c in PlaylistClient.calls] == ["post", "post"]
        retry_kwargs = PlaylistClient.calls[1][2]
        assert retry_kwargs["data"] == [
            ("playlist[title]", "T"),
            ("playlist[sharing]", "public"),
            ("playlist[tracks][][id]", "7"),
        ]
        assert "content" not in retry_kwargs

    async def test_create_playlist_non_parse_422_raises(self, sc_playlist_uploader):
        PlaylistClient.post_responses = [FakeResp(422, text="title too long")]
        with pytest.raises(RuntimeError, match="422"):
            await sc_playlist_uploader.create_playlist("T", [7])
        assert len(PlaylistClient.calls) == 1  # no blind form retry

    async def test_add_track_fetches_then_puts_full_list(self, sc_playlist_uploader):
        # SC's PUT replaces the track array wholesale: existing order must be
        # preserved and the new track appended.
        PlaylistClient.get_responses = [
            FakeResp(200, {"tracks": [{"id": 5}, {"id": 6}]})
        ]
        PlaylistClient.put_responses = [FakeResp(200, {})]
        await sc_playlist_uploader.add_track_to_playlist(42, "7")
        method, url, kwargs = PlaylistClient.calls[-1]
        assert method == "put" and url.endswith("/playlists/42")
        assert json.loads(kwargs["content"]) == {
            "playlist": {"tracks": [{"id": 5}, {"id": 6}, {"id": 7}]}
        }
        assert kwargs["headers"]["Content-Type"] == "application/json; charset=utf-8"

    async def test_add_track_put_422_parse_error_falls_back_to_form(
        self, sc_playlist_uploader
    ):
        PlaylistClient.get_responses = [FakeResp(200, {"tracks": [{"id": 5}]})]
        PlaylistClient.put_responses = [
            FakeResp(422, text="Could not parse JSON request body"),
            FakeResp(200, {}),
        ]
        await sc_playlist_uploader.add_track_to_playlist(42, 7)
        retry_kwargs = PlaylistClient.calls[-1][2]
        assert retry_kwargs["data"] == [
            ("playlist[tracks][][id]", "5"),
            ("playlist[tracks][][id]", "7"),
        ]

    async def test_add_track_already_member_is_noop(self, sc_playlist_uploader):
        PlaylistClient.get_responses = [FakeResp(200, {"tracks": [{"id": 7}]})]
        await sc_playlist_uploader.add_track_to_playlist(42, 7)
        assert [c[0] for c in PlaylistClient.calls] == ["get"]  # no PUT

    async def test_add_track_put_failure_raises(self, sc_playlist_uploader):
        PlaylistClient.get_responses = [FakeResp(200, {"tracks": []})]
        PlaylistClient.put_responses = [FakeResp(403, text="no")]
        with pytest.raises(RuntimeError, match="403"):
            await sc_playlist_uploader.add_track_to_playlist(42, 7)
