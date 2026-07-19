"""Playlist grouping: classification, fuzzy matching, and the organize run."""

import pytest
from sqlalchemy import select

import app.services.catalog_playlists as cp
from app.models import Mix
from app.services.catalog_apply import QUOTA_KEY
from app.services.catalog_playlists import (
    classify_playlist,
    find_matching_playlist,
    playlist_title_for_bucket,
    record_placement,
    run_organize_playlists,
)


YT_EXISTING_ID = "PLexisting000000000000000000000001"
YT_TECHNO_ID = "PLtechno00000000000000000000000001"


def _mix(title="Some Mix", genres=None, **kwargs):
    return Mix(title=title, genres=genres or [], **kwargs)


# ---------------------------------------------------------------------------
# classify_playlist (pure)
# ---------------------------------------------------------------------------

class TestClassifyPlaylist:
    @pytest.mark.parametrize(
        "title",
        [
            "Will See Wednesdays #14",
            "will see wednesday open decks",
            "WILL SEE WEDNESDAYS - house edition",
        ],
    )
    def test_wednesdays_series(self, title):
        assert classify_playlist(_mix(title)) == "Will See Wednesdays"

    @pytest.mark.parametrize(
        "title",
        ["2nd Saturdays vol 3", "Second Saturdays at the park", "second saturday sessions"],
    )
    def test_second_saturdays_series(self, title):
        assert classify_playlist(_mix(title)) == "Second Saturdays"

    def test_series_wins_over_raid_train(self):
        mix = _mix("Will See Wednesdays raid train", genres=["dubstep"])
        assert classify_playlist(mix) == "Will See Wednesdays"

    @pytest.mark.parametrize(
        ("title", "genres", "expected"),
        [
            ("Raid Train DnB Takeover", [], "DnB & Jungle"),
            ("raidtrain 2024-05-01", ["drum and bass"], "DnB & Jungle"),
            ("Raid Train", ["dubstep"], "Dubstep & Bass"),
            ("Raid Train", ["trap"], "Dubstep & Bass"),
            ("Raid Train house hour", [], "House"),
            ("Raid Train", ["deep house"], "House"),
            ("Raid Train", ["trance"], "Trance"),
            ("Raid Train", ["techno"], "Techno"),
            ("Big Room Raid Train", [], "EDM & Big Room"),
            ("Raid Train", ["garage"], "Garage & Breaks"),
            ("Raid Train", ["breakbeat"], "Garage & Breaks"),
            ("Raid Train", ["progressive"], "Melodic & Progressive"),
            ("Raid Train", [], "Open Format"),
            ("Raid Train", ["electronic"], "Open Format"),
        ],
    )
    def test_raid_trains_get_genre_buckets(self, title, genres, expected):
        assert classify_playlist(_mix(title, genres)) == expected

    def test_dnb_phrase_beats_lone_bass_keyword(self):
        assert (
            classify_playlist(_mix("Raid Train", ["drum and bass"]))
            == "DnB & Jungle"
        )

    @pytest.mark.parametrize(
        ("genres", "expected"),
        [
            (["house"], "House"),
            (["techno"], "Techno"),
            (["drum and bass", "house"], "DnB & Jungle"),
            (["ambient"], "Melodic & Progressive"),
            (["electronic"], "DJ Sets"),
            ([], "DJ Sets"),
        ],
    )
    def test_plain_mixes_fall_back_to_genres_then_dj_sets(self, genres, expected):
        assert classify_playlist(_mix("A Night to Remember", genres)) == expected


# ---------------------------------------------------------------------------
# Fuzzy playlist-name matching
# ---------------------------------------------------------------------------

class TestFindMatchingPlaylist:
    def test_exact_name_matches(self):
        pls = [{"id": "a", "title": "Will See | House"}, {"id": "b", "title": "House"}]
        assert find_matching_playlist("House", pls)["id"] == "a"

    def test_existing_playlist_with_filler_words_matches(self):
        pls = [{"id": "x", "title": "Will See | House Mixes"}]
        assert find_matching_playlist("House", pls)["id"] == "x"

    def test_partial_token_playlist_matches_wider_bucket(self):
        # Will's existing "EDM Mixes" is respected for the EDM & Big Room bucket
        pls = [{"id": "e", "title": "EDM Mixes"}]
        assert find_matching_playlist("EDM & Big Room", pls)["id"] == "e"

    def test_bucket_tokens_contained_in_playlist_title(self):
        pls = [{"id": "d", "title": "Will See DnB & Jungle Raid Trains"}]
        assert find_matching_playlist("DnB & Jungle", pls)["id"] == "d"

    def test_series_playlist_matches(self):
        pls = [{"id": "w", "title": "Will See Wednesdays"}]
        assert find_matching_playlist("Will See Wednesdays", pls)["id"] == "w"

    def test_unrelated_playlists_do_not_match(self):
        pls = [
            {"id": "1", "title": "Will See Wednesdays"},
            {"id": "2", "title": "Trance Mixes"},
        ]
        assert find_matching_playlist("Techno", pls) is None

    def test_playlist_owned_by_more_specific_bucket_is_not_stolen(self):
        # "Dubstep & Bass Mixes" belongs to the Dubstep bucket; the DnB lookup
        # must not claim it even though "bass" overlaps.
        pls = [{"id": "d", "title": "Dubstep & Bass Mixes"}]
        assert find_matching_playlist("DnB & Jungle", pls) is None
        assert find_matching_playlist("Dubstep & Bass", pls)["id"] == "d"

    def test_no_playlists(self):
        assert find_matching_playlist("House", []) is None


class TestPlaylistTitleForBucket:
    def test_prefixes_genre_buckets(self):
        assert playlist_title_for_bucket("House") == "Will See | House"

    def test_series_already_branded(self):
        assert playlist_title_for_bucket("Will See Wednesdays") == "Will See Wednesdays"


class TestRecordPlacement:
    def test_appends_and_dedupes(self):
        mix = _mix(metadata_json={"catalog": {"youtube": {"published_at": "x"}}})
        record_placement(mix, "youtube", "PL1", "House")
        record_placement(mix, "youtube", "PL1", "House")  # dedupe
        record_placement(mix, "soundcloud", 9, "House")
        playlists = mix.metadata_json["catalog"]["playlists"]
        assert playlists["youtube"] == [{"id": "PL1", "title": "House"}]
        assert playlists["soundcloud"] == [{"id": 9, "title": "House"}]
        # pre-existing catalog metadata survives
        assert mix.metadata_json["catalog"]["youtube"]["published_at"] == "x"


# ---------------------------------------------------------------------------
# run_organize_playlists (uploaders faked)
# ---------------------------------------------------------------------------

class FakeYT:
    def __init__(self, playlists=None, members=None):
        # playlists: list of {"id", "snippet": {"title"}}
        self.playlists = playlists or []
        self.members = members or {}  # playlist_id -> [video ids]
        self.created = []
        self.added = []  # (playlist_id, video_id)

    async def list_playlists(self):
        return self.playlists

    async def create_playlist(self, title, description="", privacy="public"):
        # Realistic 34-char playlist id ("PL" + 32), like the live API returns.
        pid = f"PLcreated{len(self.created) + 1:025d}"
        self.created.append(title)
        self.playlists.append({"id": pid, "snippet": {"title": title}})
        self.members[pid] = []
        return pid

    async def list_playlist_video_ids(self, playlist_id):
        return list(self.members.get(playlist_id, []))

    async def add_video_to_playlist(self, playlist_id, video_id):
        self.added.append((playlist_id, video_id))
        self.members.setdefault(playlist_id, []).append(video_id)


class FakeSC:
    def __init__(self, playlists=None):
        # playlists: list of {"id", "title", "tracks": [{"id": n}]}
        self.playlists = playlists or []
        self.created = []  # (title, track_ids)
        self.added = []  # (playlist_id, track_id)

    async def list_playlists(self):
        return self.playlists

    async def create_playlist(self, title, track_ids, sharing="public"):
        pid = 9000 + len(self.created)
        self.created.append((title, list(track_ids)))
        self.playlists.append(
            {"id": pid, "title": title, "tracks": [{"id": int(t)} for t in track_ids]}
        )
        return {"id": pid, "title": title}

    async def add_track_to_playlist(self, playlist_id, track_id):
        self.added.append((playlist_id, track_id))


@pytest.fixture
def fake_uploaders(monkeypatch):
    yt = FakeYT()
    sc = FakeSC()
    monkeypatch.setattr(cp, "get_youtube_uploader", lambda sj: yt)
    monkeypatch.setattr(cp, "get_soundcloud_uploader", lambda sj, cb=None: sc)
    return yt, sc


async def _add_mix(**kwargs):
    from app.database import async_session_factory

    defaults = dict(title="Mix", source="imported", pipeline_status="imported")
    defaults.update(kwargs)
    async with async_session_factory() as session:
        mix = Mix(**defaults)
        session.add(mix)
        await session.commit()
        return mix.id


async def _get_mix(mix_id):
    from app.database import async_session_factory

    async with async_session_factory() as session:
        return (
            await session.execute(select(Mix).where(Mix.id == mix_id))
        ).scalar_one()


async def _settings_json():
    from app.database import async_session_factory
    from app.models import AppSettings

    async with async_session_factory() as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == 1))
        ).scalar_one_or_none()
        return dict(row.settings_json or {}) if row else {}


class TestRunOrganizePlaylists:
    async def test_creates_playlists_and_places_on_both_platforms(
        self, prepared_db, fake_uploaders
    ):
        yt, sc = fake_uploaders
        mid = await _add_mix(
            title="Raid Train DnB hour",
            genres=["drum and bass"],
            youtube_video_id="vid1",
            soundcloud_track_id="111",
        )
        summary = await run_organize_playlists("all")

        assert summary["status"] == "ok"
        assert summary["targeted"] == 1
        assert summary["buckets"] == {"DnB & Jungle": 1}
        assert yt.created == ["Will See | DnB & Jungle"]
        assert yt.added == [(f"PLcreated{1:025d}", "vid1")]
        assert sc.created == [("Will See | DnB & Jungle", ["111"])]
        assert summary["youtube"]["placed"] == 1
        assert summary["soundcloud"]["placed"] == 1
        assert summary["youtube"]["created_playlists"] == 1
        assert summary["soundcloud"]["created_playlists"] == 1

        mix = await _get_mix(mid)
        assert mix.youtube_playlist_id == f"PLcreated{1:025d}"
        placements = mix.metadata_json["catalog"]["playlists"]
        assert placements["youtube"][0]["title"] == "DnB & Jungle"
        assert placements["soundcloud"][0]["title"] == "DnB & Jungle"

        # summary persisted
        sj = await _settings_json()
        assert sj["catalog_last_playlists"]["status"] == "ok"

    async def test_respects_existing_fuzzy_matched_playlists(
        self, prepared_db, fake_uploaders
    ):
        yt, sc = fake_uploaders
        yt.playlists = [{"id": YT_EXISTING_ID, "snippet": {"title": "Will See | House Mixes"}}]
        yt.members = {YT_EXISTING_ID: []}
        sc.playlists = [{"id": 77, "title": "House Mixes", "tracks": []}]
        await _add_mix(
            title="Warm Nights", genres=["house"],
            youtube_video_id="vidH", soundcloud_track_id="222",
        )
        summary = await run_organize_playlists(None)

        assert yt.created == [] and sc.created == []
        assert yt.added == [(YT_EXISTING_ID, "vidH")]
        assert sc.added == [(77, "222")]
        assert summary["youtube"]["created_playlists"] == 0

    async def test_idempotent_skips_existing_members(
        self, prepared_db, fake_uploaders
    ):
        yt, sc = fake_uploaders
        yt.playlists = [{"id": YT_TECHNO_ID, "snippet": {"title": "Will See | Techno"}}]
        yt.members = {YT_TECHNO_ID: ["vidT"]}
        sc.playlists = [{"id": 5, "title": "Will See | Techno", "tracks": [{"id": 333}]}]
        mid = await _add_mix(
            title="Concrete Basement", genres=["techno"],
            youtube_video_id="vidT", soundcloud_track_id="333",
        )
        summary = await run_organize_playlists(None)

        assert yt.added == [] and sc.added == []
        assert summary["youtube"]["already_member"] == 1
        assert summary["soundcloud"]["already_member"] == 1
        # placement still recorded on the mix
        mix = await _get_mix(mid)
        assert mix.youtube_playlist_id == YT_TECHNO_ID
        assert mix.metadata_json["catalog"]["playlists"]["soundcloud"][0]["id"] == 5

    async def test_quota_budget_pauses_youtube_but_not_soundcloud(
        self, prepared_db, fake_uploaders, monkeypatch
    ):
        from app.database import async_session_factory
        from app.models import AppSettings
        from datetime import date

        yt, sc = fake_uploaders
        # Budget allows exactly one 50-unit write.
        monkeypatch.setattr(
            cp.settings, "YOUTUBE_DAILY_QUOTA_BUDGET", 100, raising=False
        )
        async with async_session_factory() as session:
            session.add(
                AppSettings(
                    id=1,
                    settings_json={
                        QUOTA_KEY: {"date": date.today().isoformat(), "used": 50}
                    },
                )
            )
            await session.commit()

        await _add_mix(
            title="A", genres=["techno"],
            youtube_video_id="v1", soundcloud_track_id="1",
        )
        await _add_mix(
            title="B", genres=["techno"],
            youtube_video_id="v2", soundcloud_track_id="2",
        )
        yt.playlists = [{"id": YT_TECHNO_ID, "snippet": {"title": "Will See | Techno"}}]
        yt.members = {YT_TECHNO_ID: []}

        summary = await run_organize_playlists(None)

        # only one YT insert fit the budget; the second is queued
        assert len(yt.added) == 1
        assert summary["youtube"]["placed"] == 1
        assert summary["youtube"]["queued"] == 1
        assert summary["youtube"]["paused"] is True
        # SoundCloud phase unaffected (both tracks placed via create)
        assert len(sc.created) == 1
        assert summary["soundcloud"]["placed"] == 2

        # quota usage persisted
        sj = await _settings_json()
        assert sj[QUOTA_KEY]["used"] == 100

    async def test_mix_id_subset_targets_only_those(self, prepared_db, fake_uploaders):
        yt, sc = fake_uploaders
        target = await _add_mix(
            title="Chosen", genres=["trance"], youtube_video_id="vidC"
        )
        await _add_mix(title="Other", genres=["trance"], youtube_video_id="vidO")
        summary = await run_organize_playlists([target])

        assert summary["targeted"] == 1
        assert [v for _, v in yt.added] == ["vidC"]

    async def test_platform_failure_recorded_as_partial(
        self, prepared_db, fake_uploaders
    ):
        yt, sc = fake_uploaders

        async def boom():
            raise RuntimeError("yt down")

        yt.list_playlists = boom
        await _add_mix(
            title="X", genres=["house"],
            youtube_video_id="v", soundcloud_track_id="9",
        )
        summary = await run_organize_playlists(None)

        assert summary["status"] == "partial"
        assert any("yt down" in e for e in summary["errors"])
        # SoundCloud still ran
        assert len(sc.created) == 1


class TestTitleKeywordFallback:
    """Imported mixes routinely have empty genres — the title must classify."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("DnB Tuesday", "DnB & Jungle"),
            ("Drum & Bass All Night", "DnB & Jungle"),
            ("Jungle Fever Session", "DnB & Jungle"),
            ("Dubstep Face Melter", "Dubstep & Bass"),
            ("Pure Bass Therapy", "Dubstep & Bass"),
            ("House Party Warmup", "House"),
            ("Trance Mission", "Trance"),
            ("Techno Bunker Vol 2", "Techno"),
            ("UK Garage Classics", "Garage & Breaks"),
            ("Breaks and Beats", "Garage & Breaks"),
            ("UKG Throwbacks", "Garage & Breaks"),
            ("Melodic Sunrise", "Melodic & Progressive"),
            ("Progressive Journey", "Melodic & Progressive"),
            ("Organic Grooves", "Melodic & Progressive"),
            ("EDM Bangers Only", "EDM & Big Room"),
            ("Big Room Energy", "EDM & Big Room"),
            ("Festival Warmup Mix", "EDM & Big Room"),
        ],
    )
    def test_title_keywords_classify_empty_genre_mixes(self, title, expected):
        assert classify_playlist(_mix(title, genres=[])) == expected

    def test_festival_is_the_weakest_signal(self):
        # A stronger genre word in the title outranks "festival".
        assert classify_playlist(_mix("Trance Festival 2024", genres=[])) == "Trance"

    def test_genres_still_outrank_title_keywords(self):
        assert classify_playlist(_mix("DnB Special", genres=["house"])) == "House"

    def test_no_keyword_still_falls_to_dj_sets(self):
        assert classify_playlist(_mix("An Evening To Remember", genres=[])) == "DJ Sets"
        # "bassline" must not word-boundary-match "bass"
        assert classify_playlist(_mix("Bassline?? Nope-line", genres=[])) == "DJ Sets"


class TestRunResilience:
    async def test_malformed_matched_playlist_id_skips_listing_not_platform(
        self, prepared_db, fake_uploaders
    ):
        """Live incident: a fuzzy-matched playlist with a 13-char id 404'd on
        playlistItems.list and zeroed the whole run. The guard must skip the
        listing (treat as no members) and keep placing."""
        yt, sc = fake_uploaders
        bad_id = "PLJPcS6-qELD0"  # the exact live shape: "PL" + 11 chars
        yt.playlists = [{"id": bad_id, "snippet": {"title": "Will See | Techno"}}]

        async def explode(pid):
            raise RuntimeError("playlistNotFound 404")

        yt.list_playlist_video_ids = explode  # would 404 like live

        mid = await _add_mix(
            title="Concrete", genres=["techno"], youtube_video_id="vidT"
        )
        summary = await run_organize_playlists(None)

        # Listing was skipped (no 404 raised), the insert still happened.
        assert yt.added == [(bad_id, "vidT")]
        assert summary["youtube"]["placed"] == 1
        assert summary["status"] == "ok"
        mix = await _get_mix(mid)
        assert mix.youtube_playlist_id == bad_id

    async def test_one_bucket_failure_does_not_abort_the_platform(
        self, prepared_db, fake_uploaders
    ):
        yt, sc = fake_uploaders
        yt.playlists = [
            {"id": YT_TECHNO_ID, "snippet": {"title": "Will See | Techno"}},
            {"id": YT_EXISTING_ID, "snippet": {"title": "Will See | House Mixes"}},
        ]
        yt.members = {YT_TECHNO_ID: [], YT_EXISTING_ID: []}

        real_add = yt.add_video_to_playlist

        async def flaky_add(pid, vid):
            if pid == YT_TECHNO_ID:
                raise RuntimeError("insert exploded")
            await real_add(pid, vid)

        yt.add_video_to_playlist = flaky_add

        await _add_mix(title="T", genres=["techno"], youtube_video_id="v1")
        await _add_mix(title="H", genres=["house"], youtube_video_id="v2")
        summary = await run_organize_playlists(None)

        # The failing bucket is recorded; the other bucket still placed.
        assert summary["status"] == "partial"
        assert any(e.startswith("youtube/Techno:") for e in summary["errors"])
        assert summary["youtube"]["placed"] == 1
        assert (YT_EXISTING_ID, "v2") in yt.added

    async def test_one_sc_bucket_failure_does_not_abort_the_platform(
        self, prepared_db, fake_uploaders
    ):
        yt, sc = fake_uploaders

        real_create = sc.create_playlist

        async def flaky_create(title, track_ids, sharing="public"):
            if "Techno" in title:
                raise RuntimeError('422 Could not parse JSON request body')
            return await real_create(title, track_ids, sharing)

        sc.create_playlist = flaky_create

        await _add_mix(title="T", genres=["techno"], soundcloud_track_id="1")
        await _add_mix(title="H", genres=["house"], soundcloud_track_id="2")
        summary = await run_organize_playlists(None)

        assert summary["status"] == "partial"
        assert any(e.startswith("soundcloud/Techno:") for e in summary["errors"])
        assert summary["soundcloud"]["placed"] == 1
        assert [t for t, _ in sc.created] == ["Will See | House"]

    async def test_yt_bucket_failures_do_not_zero_soundcloud(
        self, prepared_db, fake_uploaders
    ):
        """Live incident: yt placed=0 AND sc placed=0. Even with every YT
        bucket failing, the SoundCloud phase must complete its placements."""
        yt, sc = fake_uploaders

        async def always_fail(pid, vid):
            raise RuntimeError("404")

        yt.playlists = [{"id": YT_TECHNO_ID, "snippet": {"title": "Will See | Techno"}}]
        yt.members = {YT_TECHNO_ID: []}
        yt.add_video_to_playlist = always_fail

        await _add_mix(
            title="T", genres=["techno"],
            youtube_video_id="v1", soundcloud_track_id="9",
        )
        summary = await run_organize_playlists(None)

        assert summary["youtube"]["placed"] == 0
        assert summary["soundcloud"]["placed"] == 1
        assert len(sc.created) == 1
