"""Catalog fetchers, normalization, LLM judge parsing, and the sync service.

All platform I/O is faked: googleapiclient via a stub service object on
YouTubeUploader._get_service, SoundCloud via a monkeypatched httpx.AsyncClient
(same pattern as test_soundcloud_uploader), and the LLM via a fake
DescriptionGenerator.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import app.services.catalog_sync as sync_mod
import app.services.soundcloud_uploader as sc_mod
from app.services.catalog_sync import (
    _parse_judge_response,
    fetch_soundcloud_catalog,
    fetch_youtube_catalog,
    normalize_soundcloud_item,
    normalize_youtube_item,
    parse_iso8601_duration,
    resolve_ambiguous_with_llm,
    run_catalog_sync,
)
from app.services.youtube_uploader import YouTubeUploader


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestParseDuration:
    def test_hours_minutes_seconds(self):
        assert parse_iso8601_duration("PT1H2M3S") == 3723.0

    def test_minutes_only(self):
        assert parse_iso8601_duration("PT45M") == 2700.0

    def test_seconds_only(self):
        assert parse_iso8601_duration("PT59S") == 59.0

    def test_days(self):
        assert parse_iso8601_duration("P1DT1H") == 90000.0

    def test_garbage_is_none(self):
        assert parse_iso8601_duration("not-a-duration") is None
        assert parse_iso8601_duration("") is None


class TestNormalizeYouTube:
    def test_full_resource(self):
        raw = {
            "id": "vid123XYZab",
            "snippet": {
                "title": "My Mix",
                "description": "desc here",
                "publishedAt": "2025-06-01T12:00:00Z",
                "thumbnails": {
                    "default": {"url": "http://t/default.jpg"},
                    "maxres": {"url": "http://t/maxres.jpg"},
                },
            },
            "contentDetails": {"duration": "PT2H"},
            "status": {"privacyStatus": "public"},
        }
        item = normalize_youtube_item(raw)
        assert item["platform"] == "youtube"
        assert item["id"] == "vid123XYZab"
        assert item["url"] == "https://www.youtube.com/watch?v=vid123XYZab"
        assert item["duration_seconds"] == 7200.0
        assert item["published_at"] == datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        assert item["thumbnail_url"] == "http://t/maxres.jpg"  # best quality wins
        assert item["privacy_status"] == "public"

    def test_minimal_resource(self):
        item = normalize_youtube_item({"id": "abc123def"})
        assert item["title"] == "" and item["description"] == ""
        assert item["duration_seconds"] is None
        assert item["published_at"] is None
        assert item["thumbnail_url"] is None


class TestNormalizeSoundCloud:
    def test_full_resource(self):
        raw = {
            "id": 987654,
            "title": "Desert Frequencies",
            "description": "notes",
            "permalink_url": "https://soundcloud.com/thewillsee/desert-frequencies",
            "duration": 3600000,
            "artwork_url": "https://i1.sndcdn.com/artworks-x-large.jpg",
            "created_at": "2025/06/01 12:00:00 +0000",
            "sharing": "public",
        }
        item = normalize_soundcloud_item(raw)
        assert item["platform"] == "soundcloud"
        assert item["id"] == "987654"  # stringified
        assert item["duration_seconds"] == 3600.0  # ms -> s
        assert item["published_at"] == datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        assert item["artwork_url"].endswith("x-large.jpg")
        assert item["sharing"] == "public"


# ---------------------------------------------------------------------------
# YouTube fetcher (stubbed googleapiclient service)
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeYouTubeService:
    """channels/playlistItems/videos stub with pagination + id-batch capture."""

    def __init__(self, uploads_playlist="UUxx", video_ids=None, page_size=50):
        self.video_ids = video_ids or []
        self.uploads_playlist = uploads_playlist
        self.page_size = page_size
        self.videos_list_calls = []

    def channels(self):
        svc = self

        class C:
            def list(self, **kw):
                return _Req(
                    {
                        "items": [
                            {
                                "contentDetails": {
                                    "relatedPlaylists": {"uploads": svc.uploads_playlist}
                                }
                            }
                        ]
                    }
                )

        return C()

    def playlistItems(self):
        svc = self

        class P:
            def list(self, **kw):
                token = kw.get("pageToken")
                start = int(token) if token else 0
                page = svc.video_ids[start : start + svc.page_size]
                resp = {
                    "items": [{"contentDetails": {"videoId": v}} for v in page]
                }
                if start + svc.page_size < len(svc.video_ids):
                    resp["nextPageToken"] = str(start + svc.page_size)
                return _Req(resp)

        return P()

    def videos(self):
        svc = self

        class V:
            def list(self, **kw):
                ids = kw["id"].split(",")
                svc.videos_list_calls.append(ids)
                return _Req(
                    {
                        "items": [
                            {
                                "id": vid,
                                "snippet": {"title": f"Video {vid}"},
                                "contentDetails": {"duration": "PT1H"},
                                "status": {"privacyStatus": "public"},
                            }
                            for vid in ids
                        ]
                    }
                )

        return V()


class TestFetchYouTubeCatalog:
    async def test_paginates_and_batches(self, monkeypatch):
        ids = [f"vid{i:04d}xyz" for i in range(120)]
        fake = FakeYouTubeService(video_ids=ids)
        monkeypatch.setattr(YouTubeUploader, "_get_service", lambda self: fake)

        items = await fetch_youtube_catalog({})
        assert len(items) == 120
        assert {i["id"] for i in items} == set(ids)
        assert all(i["duration_seconds"] == 3600.0 for i in items)
        # videos.list batched at <=50 ids per call
        assert [len(c) for c in fake.videos_list_calls] == [50, 50, 20]

    async def test_no_channel_returns_empty(self, monkeypatch):
        class Empty(FakeYouTubeService):
            def channels(self):
                class C:
                    def list(self, **kw):
                        return _Req({"items": []})

                return C()

        monkeypatch.setattr(YouTubeUploader, "_get_service", lambda self: Empty())
        assert await fetch_youtube_catalog({}) == []


# ---------------------------------------------------------------------------
# SoundCloud fetcher (monkeypatched httpx)
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class SeqClient:
    """Returns queued GET responses in order, recording the requests."""

    responses = []
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        SeqClient.requests.append({"url": url, **kwargs})
        return SeqClient.responses.pop(0)


class TestFetchSoundCloudCatalog:
    async def test_follows_next_href(self, monkeypatch):
        monkeypatch.setattr(sc_mod.httpx, "AsyncClient", SeqClient)
        monkeypatch.setattr(
            sc_mod.SoundCloudUploader,
            "_ensure_access_token",
            lambda self: _async_return("tok"),
        )
        SeqClient.requests = []
        SeqClient.responses = [
            FakeResp(
                200,
                {
                    "collection": [
                        {"id": 1, "title": "A", "duration": 1000,
                         "permalink_url": "https://soundcloud.com/u/a"},
                    ],
                    "next_href": "https://api.soundcloud.com/me/tracks?cursor=abc",
                },
            ),
            FakeResp(
                200,
                {
                    "collection": [
                        {"id": 2, "title": "B", "duration": 2000,
                         "permalink_url": "https://soundcloud.com/u/b"},
                    ],
                    "next_href": None,
                },
            ),
        ]

        items = await fetch_soundcloud_catalog({})
        assert [i["id"] for i in items] == ["1", "2"]
        # first request carries pagination params; the second follows next_href verbatim
        assert SeqClient.requests[0]["params"] == {"linked_partitioning": 1, "limit": 200}
        assert SeqClient.requests[1]["url"].endswith("cursor=abc")
        assert SeqClient.requests[1]["params"] is None

    async def test_error_raises(self, monkeypatch):
        monkeypatch.setattr(sc_mod.httpx, "AsyncClient", SeqClient)
        monkeypatch.setattr(
            sc_mod.SoundCloudUploader,
            "_ensure_access_token",
            lambda self: _async_return("tok"),
        )
        SeqClient.responses = [FakeResp(401, text="unauthorized")]
        with pytest.raises(RuntimeError, match="401"):
            await fetch_soundcloud_catalog({})


def _async_return(value):
    async def _coro():
        return value

    return _coro()


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------

class FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5


class FakeResponse:
    usage = FakeUsage()


class FakeGenerator:
    def __init__(self, text):
        self._text = text
        self.tracked = []

    async def _create_completion(self, prompt, max_tokens, temperature):
        return FakeResponse(), self._text

    async def _track_usage(self, session, mix_id, op, in_tok, out_tok):
        self.tracked.append(op)


class TestJudgeParsing:
    def test_parse_plain_json(self):
        text = '[{"pair": 1, "same": true}, {"pair": 2, "same": false}]'
        assert _parse_judge_response(text, 2) == [True, False]

    def test_parse_fenced_json(self):
        text = '```json\n[{"pair": 1, "same": false}]\n```'
        assert _parse_judge_response(text, 1) == [False]

    def test_out_of_range_pairs_ignored(self):
        text = '[{"pair": 5, "same": true}]'
        assert _parse_judge_response(text, 2) == [False, False]


class TestResolveAmbiguous:
    def _cands(self):
        yt = {"platform": "youtube", "id": "v1abcde", "title": "t1",
              "duration_seconds": 100, "published_at": None}
        sc = {"platform": "soundcloud", "id": "9", "title": "t2",
              "duration_seconds": 101, "published_at": None}
        return [(yt, sc)]

    async def test_same_verdict_pairs(self, monkeypatch):
        import app.services.description_generator as dg

        fake = FakeGenerator('[{"pair": 1, "same": true}]')
        monkeypatch.setattr(dg, "DescriptionGenerator", lambda sj: fake)
        pairs, singles = await resolve_ambiguous_with_llm(self._cands(), {}, session=object())
        assert len(pairs) == 1 and singles == []
        assert pairs[0][2:] == (0.6, "llm-judge")
        assert fake.tracked == ["catalog_match_judge"]

    async def test_not_same_verdict_splits_to_singles(self, monkeypatch):
        import app.services.description_generator as dg

        monkeypatch.setattr(
            dg, "DescriptionGenerator",
            lambda sj: FakeGenerator('[{"pair": 1, "same": false}]'),
        )
        pairs, singles = await resolve_ambiguous_with_llm(self._cands(), {})
        assert pairs == [] and len(singles) == 2

    async def test_llm_failure_falls_back_to_singles(self, monkeypatch):
        import app.services.description_generator as dg

        class Boom:
            def __init__(self, sj):
                raise RuntimeError("no api key")

        monkeypatch.setattr(dg, "DescriptionGenerator", Boom)
        pairs, singles = await resolve_ambiguous_with_llm(self._cands(), {})
        assert pairs == [] and len(singles) == 2

    async def test_empty_candidates_no_llm(self):
        assert await resolve_ambiguous_with_llm([], {}) == ([], [])


# ---------------------------------------------------------------------------
# run_catalog_sync end-to-end (fetchers + judge stubbed)
# ---------------------------------------------------------------------------

def _yt_item(id, title, dur=3600.0, desc=""):
    return {
        "platform": "youtube", "id": id, "title": title, "description": desc,
        "url": f"https://www.youtube.com/watch?v={id}",
        "duration_seconds": dur, "published_at": None,
        "thumbnail_url": f"http://t/{id}.jpg", "privacy_status": "public",
    }


def _sc_item(id, title, dur=3600.0, desc="", permalink=None):
    return {
        "platform": "soundcloud", "id": id, "title": title, "description": desc,
        "url": permalink or f"https://soundcloud.com/thewillsee/{id}",
        "duration_seconds": dur, "published_at": None,
        "artwork_url": f"http://a/{id}.jpg", "sharing": "public",
    }


@pytest.fixture
def stub_fetchers(monkeypatch):
    """Stub both fetchers + the judge; returns a dict to tweak per test."""
    data = {"yt": [], "sc": [], "judge_pairs": [], "judge_singles": []}

    async def fake_yt(sj):
        return data["yt"]

    async def fake_sc(sj, cb=None):
        return data["sc"]

    async def fake_judge(cands, sj, session=None):
        if data["judge_pairs"] or data["judge_singles"]:
            return data["judge_pairs"], data["judge_singles"]
        singles = [x for pair in cands for x in pair]
        return [], singles

    monkeypatch.setattr(sync_mod, "fetch_youtube_catalog", fake_yt)
    monkeypatch.setattr(sync_mod, "fetch_soundcloud_catalog", fake_sc)
    monkeypatch.setattr(sync_mod, "resolve_ambiguous_with_llm", fake_judge)
    return data


class TestRunCatalogSync:
    async def test_imports_pairs_and_singles(self, prepared_db, stub_fetchers):
        from app.database import async_session_factory
        from app.models import AppSettings, Mix

        stub_fetchers["yt"] = [
            _yt_item("vidPair0001", "Desert Frequencies"),
            _yt_item("vidLone0001", "YT Only Video", dur=100.0),
        ]
        stub_fetchers["sc"] = [
            _sc_item("500", "Desert Frequencies", dur=3620.0),
            _sc_item("501", "SC Only Track", dur=90000.0),
        ]

        summary = await run_catalog_sync()
        assert summary["status"] == "ok"
        assert summary["pairs_created"] == 1
        assert summary["singles_created"] == 2

        async with async_session_factory() as session:
            mixes = (await session.execute(select(Mix))).scalars().all()
            assert len(mixes) == 3
            pair = next(m for m in mixes if m.youtube_video_id == "vidPair0001")
            assert pair.soundcloud_track_id == "500"
            assert pair.source == "imported"
            assert pair.pipeline_status == "imported"
            assert pair.title == "Desert Frequencies"  # SC title preferred
            assert pair.duration_seconds == 3600.0
            meta = pair.metadata_json["catalog"]
            assert meta["youtube"]["thumbnail_url"] == "http://t/vidPair0001.jpg"
            assert meta["soundcloud"]["artwork_url"] == "http://a/500.jpg"

            # summary persisted in settings_json
            row = (
                await session.execute(select(AppSettings).where(AppSettings.id == 1))
            ).scalar_one()
            assert row.settings_json["catalog_last_sync"]["status"] == "ok"

    async def test_resync_is_idempotent(self, prepared_db, stub_fetchers):
        from app.database import async_session_factory
        from app.models import Mix

        stub_fetchers["yt"] = [_yt_item("vidStable01", "Stable Mix")]
        stub_fetchers["sc"] = [_sc_item("600", "Stable Mix", dur=3610.0)]

        first = await run_catalog_sync()
        second = await run_catalog_sync()
        assert first["pairs_created"] == 1
        assert second["pairs_created"] == 0
        assert second["singles_created"] == 0
        assert second["existing_updated"] == 1  # re-claimed + metadata refreshed

        async with async_session_factory() as session:
            mixes = (await session.execute(select(Mix))).scalars().all()
            assert len(mixes) == 1

    async def test_existing_mix_gets_ids_backfilled(self, prepared_db, stub_fetchers):
        from app.database import async_session_factory
        from app.models import Mix

        async with async_session_factory() as session:
            session.add(
                Mix(
                    id="pipe-1",
                    title="Pipeline Mix",
                    source="pipeline",
                    youtube_url="https://www.youtube.com/watch?v=vidPipe0001",
                    soundcloud_url="https://soundcloud.com/thewillsee/pipeline-mix",
                    pipeline_status="completed",
                )
            )
            await session.commit()

        stub_fetchers["yt"] = [_yt_item("vidPipe0001", "Pipeline Mix (YT)")]
        stub_fetchers["sc"] = [
            _sc_item("700", "Pipeline Mix", permalink="https://soundcloud.com/thewillsee/pipeline-mix")
        ]

        summary = await run_catalog_sync()
        assert summary["existing_updated"] == 1
        assert summary["pairs_created"] == 0 and summary["singles_created"] == 0

        async with async_session_factory() as session:
            mix = (
                await session.execute(select(Mix).where(Mix.id == "pipe-1"))
            ).scalar_one()
            assert mix.youtube_video_id == "vidPipe0001"
            assert mix.soundcloud_track_id == "700"
            assert mix.source == "pipeline"  # untouched
            assert mix.pipeline_status == "completed"  # untouched

    async def test_fetch_failure_is_partial_not_fatal(self, prepared_db, monkeypatch, stub_fetchers):
        async def boom(sj):
            raise RuntimeError("YOUTUBE_REFRESH_TOKEN not configured")

        monkeypatch.setattr(sync_mod, "fetch_youtube_catalog", boom)
        stub_fetchers["sc"] = [_sc_item("800", "SC Solo", dur=100.0)]

        summary = await run_catalog_sync()
        assert summary["status"] == "partial"
        assert any("youtube" in e for e in summary["errors"])
        assert summary["singles_created"] == 1

    async def test_activity_events_emitted(self, prepared_db, stub_fetchers):
        from app.services import activity_log

        stub_fetchers["yt"] = []
        stub_fetchers["sc"] = []
        await run_catalog_sync()
        items, _total = await activity_log.query(event="catalog_sync")
        assert len(items) >= 2  # started + finished at minimum


# ---------------------------------------------------------------------------
# Sync must not re-introduce per-platform title divergence
# ---------------------------------------------------------------------------

class TestSyncKeepsTitlesUnified:
    """Titles are unified (one string, both platforms). A sync backfilling
    ``title_youtube`` from whatever the video is currently called would undo
    that every time it ran, so the live value is only adopted when it agrees
    with ``mix.title``."""

    @staticmethod
    def _yt(title):
        return {
            "id": "vidUnify001",
            "platform": "youtube",
            "url": "https://youtu.be/vidUnify001",
            "title": title,
            "description": "yt desc",
            "duration_seconds": 3600.0,
            "published_at": None,
            "thumbnail_url": None,
            "privacy_status": "public",
        }

    def test_divergent_live_title_is_not_adopted(self):
        from app.models import Mix
        from app.services.catalog_sync import _attach_platform_data

        mix = Mix(title="Four Decks and a Prayer | House Mix")
        _attach_platform_data(mix, self._yt("Will See Wednesdays 2024-05-01"), None)

        assert mix.title_youtube is None
        assert mix.title == "Four Decks and a Prayer | House Mix"

    def test_agreeing_live_title_is_adopted(self):
        from app.models import Mix
        from app.services.catalog_sync import _attach_platform_data

        mix = Mix(title="Four Decks and a Prayer | House Mix")
        _attach_platform_data(mix, self._yt("Four Decks and a Prayer | House Mix"), None)

        assert mix.title_youtube == "Four Decks and a Prayer | House Mix"

    def test_live_title_is_still_recorded_in_catalog_metadata(self):
        from app.models import Mix
        from app.services.catalog_sync import _attach_platform_data

        mix = Mix(title="Four Decks and a Prayer | House Mix")
        _attach_platform_data(mix, self._yt("Will See Wednesdays 2024-05-01"), None)

        # Nothing is lost -- the operator can still see what YouTube shows.
        meta = mix.metadata_json["catalog"]["youtube"]
        assert meta["title"] == "Will See Wednesdays 2024-05-01"

    def test_existing_unified_title_is_never_clobbered(self):
        from app.models import Mix
        from app.services.catalog_sync import _attach_platform_data

        mix = Mix(
            title="Four Decks and a Prayer | House Mix",
            title_youtube="Four Decks and a Prayer | House Mix",
        )
        _attach_platform_data(mix, self._yt("Some Stale YouTube Name"), None)

        assert mix.title == mix.title_youtube == "Four Decks and a Prayer | House Mix"

    def test_imported_pair_with_different_platform_titles_does_not_diverge(self):
        from app.services.catalog_sync import _new_imported_mix

        sc = {
            "id": "9001",
            "platform": "soundcloud",
            "url": "https://soundcloud.com/x/y",
            "title": "Four Decks and a Prayer | House Mix",
            "description": "sc desc",
            "duration_seconds": 3600.0,
            "published_at": None,
            "artwork_url": None,
            "sharing": "public",
        }
        mix = _new_imported_mix(self._yt("DJ Will See Live 5/1"), sc)

        assert mix.title == "Four Decks and a Prayer | House Mix"
        assert mix.title_youtube is None  # no divergence created at import

    def test_youtube_only_import_still_gets_its_title(self):
        from app.services.catalog_sync import _new_imported_mix

        mix = _new_imported_mix(self._yt("Neon Cactus | Techno Mix"), None)

        assert mix.title == "Neon Cactus | Techno Mix"
        assert mix.title_youtube == "Neon Cactus | Techno Mix"
