"""Catalog tracklist backfill: matching rules, description rebuild, run + API."""

from datetime import date

import pytest

from app.services.catalog_backfill import (
    DURATION_TOLERANCE_SECONDS,
    LAST_BACKFILL_KEY,
    build_tracklist_block,
    extract_filename_date,
    filename_title_similarity,
    match_local_audio,
    rebuild_description_with_tracklist,
    run_backfill,
    scan_local_audio,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _file(path="/watch/audio/a.flac", filename=None, d=None, duration=None):
    return {
        "path": path,
        "filename": filename or path.rsplit("/", 1)[-1],
        "date": d,
        "duration_seconds": duration,
    }


def _mix(mix_id="m1", title="Untitled", dates=None, duration=None):
    return {
        "id": mix_id,
        "title": title,
        "published_dates": dates or [],
        "duration_seconds": duration,
    }


TRACKLIST = [
    {"title": "One", "artist": "A", "timestamp_seconds": 0.0,
     "timestamp_formatted": "0:00"},
    {"title": "Two", "artist": "B", "timestamp_seconds": 245.0,
     "timestamp_formatted": "4:05"},
    {"title": "Three", "artist": "C", "timestamp_seconds": 3725.0,
     "timestamp_formatted": "1:02:05"},
]


# ---------------------------------------------------------------------------
# Filename date extraction
# ---------------------------------------------------------------------------


class TestExtractFilenameDate:
    def test_iso_date(self):
        assert extract_filename_date("will-see-set-2025-06-14.flac") == date(2025, 6, 14)

    def test_us_date(self):
        assert extract_filename_date("recording 06-14-2025.mp3") == date(2025, 6, 14)

    def test_iso_wins_over_us(self):
        assert extract_filename_date("2025-06-14 vs 01-02-2020.wav") == date(2025, 6, 14)

    def test_invalid_date_rejected(self):
        assert extract_filename_date("set-2025-13-45.flac") is None

    def test_no_date(self):
        assert extract_filename_date("warehouse-closing-set.flac") is None


# ---------------------------------------------------------------------------
# Filename/title similarity
# ---------------------------------------------------------------------------


class TestFilenameTitleSimilarity:
    def test_close_match(self):
        sim = filename_title_similarity(
            "deep-house-warehouse-session.flac", "Deep House Warehouse Session"
        )
        assert sim > 0.9

    def test_dates_do_not_dominate(self):
        # Same date but totally different words must not score as a title match
        sim = filename_title_similarity(
            "2025-06-14-techno-bunker.flac", "2025-06-14 ambient morning"
        )
        assert sim < 0.6

    def test_single_shared_token_insufficient_for_containment(self):
        sim = filename_title_similarity("live-thing.flac", "live other words here")
        assert sim < 0.6

    def test_empty(self):
        assert filename_title_similarity("2025-01-01.flac", "2025-01-01") == 0.0


# ---------------------------------------------------------------------------
# match_local_audio
# ---------------------------------------------------------------------------


class TestMatchLocalAudio:
    def test_date_match_within_window(self):
        files = [_file("/w/a-2025-06-14.flac", d=date(2025, 6, 14))]
        mixes = [_mix("m1", "Raid Train VOD", dates=[date(2025, 6, 16)])]
        plan = match_local_audio(files, mixes)
        assert [(m, f["path"]) for m, f, _r in plan["matches"]] == [("m1", "/w/a-2025-06-14.flac")]
        assert plan["unmatched_mixes"] == []
        assert plan["unmatched_files"] == []

    def test_date_outside_window_no_match(self):
        files = [_file("/w/a-2025-06-14.flac", d=date(2025, 6, 14))]
        mixes = [_mix("m1", "Raid Train VOD", dates=[date(2025, 6, 20)])]
        plan = match_local_audio(files, mixes)
        assert plan["matches"] == []
        assert [m["id"] for m in plan["unmatched_mixes"]] == ["m1"]
        assert len(plan["unmatched_files"]) == 1

    def test_title_match_without_date(self):
        files = [_file("/w/deep-house-warehouse-session.flac")]
        mixes = [_mix("m1", "Deep House Warehouse Session")]
        plan = match_local_audio(files, mixes)
        assert len(plan["matches"]) == 1
        assert plan["matches"][0][0] == "m1"
        assert "title" in plan["matches"][0][2]

    def test_duration_mismatch_disqualifies_even_with_date(self):
        files = [_file("/w/a-2025-06-14.flac", d=date(2025, 6, 14), duration=7200.0)]
        mixes = [
            _mix("m1", "Set", dates=[date(2025, 6, 14)],
                 duration=7200.0 + DURATION_TOLERANCE_SECONDS + 1)
        ]
        plan = match_local_audio(files, mixes)
        assert plan["matches"] == []

    def test_duration_confirmation_within_tolerance(self):
        files = [_file("/w/a-2025-06-14.flac", d=date(2025, 6, 14), duration=7200.0)]
        mixes = [_mix("m1", "Set", dates=[date(2025, 6, 14)], duration=7290.0)]
        plan = match_local_audio(files, mixes)
        assert len(plan["matches"]) == 1
        assert "duration" in plan["matches"][0][2]

    def test_missing_duration_skips_confirmation(self):
        files = [_file("/w/a-2025-06-14.flac", d=date(2025, 6, 14), duration=None)]
        mixes = [_mix("m1", "Set", dates=[date(2025, 6, 14)], duration=7290.0)]
        plan = match_local_audio(files, mixes)
        assert len(plan["matches"]) == 1

    def test_one_file_one_mix_best_score_first(self):
        # Both mixes published same week; exact-date + duration must claim the
        # file, the other mix stays unmatched.
        files = [_file("/w/a-2025-06-14.flac", d=date(2025, 6, 14), duration=3600.0)]
        mixes = [
            _mix("near", "Stream VOD", dates=[date(2025, 6, 16)]),
            _mix("exact", "Stream VOD", dates=[date(2025, 6, 14)], duration=3620.0),
        ]
        plan = match_local_audio(files, mixes)
        assert len(plan["matches"]) == 1
        assert plan["matches"][0][0] == "exact"
        assert [m["id"] for m in plan["unmatched_mixes"]] == ["near"]

    def test_each_file_used_once(self):
        files = [
            _file("/w/a-2025-06-14.flac", d=date(2025, 6, 14)),
            _file("/w/b-2025-06-14.flac", d=date(2025, 6, 14)),
        ]
        mixes = [_mix("m1", "Set", dates=[date(2025, 6, 14)])]
        plan = match_local_audio(files, mixes)
        assert len(plan["matches"]) == 1
        assert len(plan["unmatched_files"]) == 1


# ---------------------------------------------------------------------------
# scan_local_audio
# ---------------------------------------------------------------------------


class TestScanLocalAudio:
    def test_scans_audio_extensions_only(self, tmp_path):
        (tmp_path / "set-2025-06-14.flac").write_bytes(b"x")
        (tmp_path / "video.mkv").write_bytes(b"x")
        (tmp_path / "notes.txt").write_bytes(b"x")
        files = scan_local_audio([str(tmp_path)])
        assert [f["filename"] for f in files] == ["set-2025-06-14.flac"]
        assert files[0]["date"] == date(2025, 6, 14)
        # Junk bytes -> mutagen cannot read a duration
        assert files[0]["duration_seconds"] is None

    def test_missing_directory_is_empty(self, tmp_path):
        assert scan_local_audio([str(tmp_path / "nope")]) == []


# ---------------------------------------------------------------------------
# Description rebuild
# ---------------------------------------------------------------------------


class TestRebuildDescription:
    def test_injects_before_brand_links(self):
        desc = (
            "A journey through deep house.\n\n"
            "Twitch: https://www.twitch.tv/thewillsee\n"
            "YouTube: https://www.youtube.com/@willseetv"
        )
        out = rebuild_description_with_tracklist(desc, TRACKLIST)
        block = build_tracklist_block(TRACKLIST)
        assert block in out
        assert out.index("journey") < out.index("Tracklist:") < out.index("Twitch:")

    def test_strips_stale_tracklist_block(self):
        desc = (
            "Prose intro.\n\n"
            "Tracklist:\n0:00 Old Artist - Old Track\n12:00 Stale - Entry\n\n"
            "Twitch: https://www.twitch.tv/thewillsee"
        )
        out = rebuild_description_with_tracklist(desc, TRACKLIST)
        assert "Old Artist" not in out
        assert "Stale" not in out
        assert out.count("Tracklist:") == 1
        assert "4:05 B - Two" in out
        assert out.index("Tracklist:") < out.index("Twitch:")

    def test_appends_when_no_links_marker(self):
        out = rebuild_description_with_tracklist("Just prose.", TRACKLIST)
        assert out.startswith("Just prose.")
        assert out.endswith("1:02:05 C - Three")

    def test_empty_description_becomes_block_only(self):
        out = rebuild_description_with_tracklist(None, TRACKLIST)
        assert out == build_tracklist_block(TRACKLIST)

    def test_empty_tracklist_leaves_description_untouched(self):
        assert rebuild_description_with_tracklist("Keep me.", []) == "Keep me."

    def test_block_formats_timestamp_when_missing_formatted(self):
        tracks = [{"title": "T", "artist": "A", "timestamp_seconds": 65.0}]
        assert build_tracklist_block(tracks) == "Tracklist:\n1:05 A - T"


# ---------------------------------------------------------------------------
# run_backfill (integration, analyzer mocked)
# ---------------------------------------------------------------------------


async def _make_imported_mix(mix_id, title, published="2025-06-14T20:00:00+00:00",
                             duration=None, tracklist=None, **kwargs):
    from app.database import async_session_factory
    from app.models import Mix

    async with async_session_factory() as session:
        session.add(
            Mix(
                id=mix_id,
                title=title,
                source="imported",
                pipeline_status="imported",
                duration_seconds=duration,
                tracklist=tracklist,
                metadata_json={
                    "catalog": {"youtube": {"published_at": published}}
                },
                **kwargs,
            )
        )
        await session.commit()


class FakeResult:
    genres = ["house"]
    vibes = ["groovy"]
    energy_profile = [{"timestamp_seconds": 30, "rms": 0.1, "bpm": 124.0}]
    duration_seconds = 7200.0


@pytest.fixture
def fake_analyze(monkeypatch):
    """Mock the shared analyze helper (no Shazam / librosa in tests)."""
    import app.services.handlers as handlers_mod

    calls = []

    async def _fake(audio_path, mix_id=None, progress_cb=None, analyzer=None):
        calls.append({"audio_path": audio_path, "mix_id": mix_id})
        return FakeResult(), list(TRACKLIST), "merged"

    monkeypatch.setattr(handlers_mod, "analyze_audio_with_cue", _fake)
    return calls


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "WATCH_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(app_settings, "CATALOG_EXTRA_AUDIO_PATHS", "")
    return tmp_path


class TestRunBackfill:
    async def test_backfills_matched_mix_and_creates_approved_proposals(
        self, prepared_db, fake_analyze, audio_dir
    ):
        from sqlalchemy import select
        from app.database import async_session_factory
        from app.models import ActivityEvent, Mix, MixProposal

        (audio_dir / "set-2025-06-14.flac").write_bytes(b"x")
        await _make_imported_mix(
            "m1", "Friday Night Set",
            youtube_video_id="vid1",
            youtube_url="https://www.youtube.com/watch?v=vid1",
            soundcloud_track_id="sc1",
            soundcloud_url="https://soundcloud.com/x/y",
            description_youtube=(
                "YT prose.\n\nTwitch: https://www.twitch.tv/thewillsee"
            ),
            description_soundcloud="SC prose.",
        )

        summary = await run_backfill()

        assert summary["status"] == "ok"
        assert summary["eligible_mixes"] == 1
        assert summary["audio_files"] == 1
        assert summary["matched"] == 1
        assert summary["processed"] == 1
        assert summary["proposals_created"] == 2
        assert summary["unmatched_mixes"] == []
        assert fake_analyze[0]["mix_id"] == "m1"

        async with async_session_factory() as session:
            mix = await session.get(Mix, "m1")
            assert mix.audio_file_path == str(audio_dir / "set-2025-06-14.flac")
            assert mix.tracklist == TRACKLIST
            assert mix.genres == ["house"]
            assert mix.duration_seconds == 7200.0

            proposals = (
                (await session.execute(select(MixProposal))).scalars().all()
            )
            assert sorted(p.platform for p in proposals) == ["soundcloud", "youtube"]
            for p in proposals:
                assert p.field == "description"
                assert p.status == "approved"
                assert p.created_by == "ai"
                assert "Tracklist:" in p.proposed_value
                assert "4:05 B - Two" in p.proposed_value
            yt = next(p for p in proposals if p.platform == "youtube")
            assert yt.proposed_value.index("Tracklist:") < yt.proposed_value.index("Twitch:")

            events = (
                (await session.execute(
                    select(ActivityEvent).where(ActivityEvent.event == "catalog_backfill")
                )).scalars().all()
            )
            assert any(e.mix_id == "m1" and "3 tracks" in e.message for e in events)

    async def test_unmatched_mixes_reported_and_persisted(
        self, prepared_db, fake_analyze, audio_dir
    ):
        from sqlalchemy import select
        from app.database import async_session_factory
        from app.models import AppSettings

        await _make_imported_mix("lonely", "No Audio Anywhere",
                                 published="2020-01-01T00:00:00+00:00")

        summary = await run_backfill()

        assert summary["matched"] == 0
        assert summary["unmatched_mixes"] == [
            {"mix_id": "lonely", "title": "No Audio Anywhere"}
        ]
        assert fake_analyze == []

        async with async_session_factory() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.id == 1))
            ).scalar_one()
            assert row.settings_json[LAST_BACKFILL_KEY]["status"] == "ok"
            assert row.settings_json[LAST_BACKFILL_KEY]["unmatched_mixes"] == [
                {"mix_id": "lonely", "title": "No Audio Anywhere"}
            ]

    async def test_mixes_with_tracklists_or_pipeline_source_excluded(
        self, prepared_db, fake_analyze, audio_dir
    ):
        (audio_dir / "set-2025-06-14.flac").write_bytes(b"x")
        await _make_imported_mix("done", "Already Done",
                                 tracklist=[{"title": "X", "artist": "Y"}])

        summary = await run_backfill()
        assert summary["eligible_mixes"] == 0
        assert summary["matched"] == 0
        assert fake_analyze == []

    async def test_mix_ids_filter(self, prepared_db, fake_analyze, audio_dir):
        (audio_dir / "a-2025-06-14.flac").write_bytes(b"x")
        (audio_dir / "b-2025-06-14.flac").write_bytes(b"x")
        await _make_imported_mix("m1", "Set One")
        await _make_imported_mix("m2", "Set Two")

        summary = await run_backfill(mix_ids=["m2"])
        assert summary["eligible_mixes"] == 1
        assert summary["processed"] == 1
        assert fake_analyze[0]["mix_id"] == "m2"

    async def test_cancel_flag_stops_between_mixes(
        self, prepared_db, audio_dir, monkeypatch
    ):
        import app.services.handlers as handlers_mod
        from app.services.catalog_backfill import request_cancel

        (audio_dir / "a-2025-06-14.flac").write_bytes(b"x")
        (audio_dir / "b-2025-06-15.flac").write_bytes(b"x")
        await _make_imported_mix("m1", "Set One", published="2025-06-14T00:00:00+00:00")
        await _make_imported_mix("m2", "Set Two", published="2025-06-15T00:00:00+00:00")

        analyzed = []

        async def _fake(audio_path, mix_id=None, progress_cb=None, analyzer=None):
            analyzed.append(mix_id)
            await request_cancel()  # cancel arrives mid-first-mix
            return FakeResult(), list(TRACKLIST), "shazam"

        monkeypatch.setattr(handlers_mod, "analyze_audio_with_cue", _fake)

        summary = await run_backfill()
        assert summary["status"] == "cancelled"
        assert summary["cancelled"] is True
        assert summary["matched"] == 2
        assert summary["processed"] == 1
        assert len(analyzed) == 1

    async def test_analyze_failure_is_isolated(
        self, prepared_db, audio_dir, monkeypatch
    ):
        import app.services.handlers as handlers_mod

        (audio_dir / "a-2025-06-14.flac").write_bytes(b"x")
        (audio_dir / "b-2025-06-15.flac").write_bytes(b"x")
        await _make_imported_mix("m1", "Set One", published="2025-06-14T00:00:00+00:00")
        await _make_imported_mix("m2", "Set Two", published="2025-06-15T00:00:00+00:00")

        async def _fake(audio_path, mix_id=None, progress_cb=None, analyzer=None):
            if mix_id == "m1":
                raise RuntimeError("decode blew up")
            return FakeResult(), list(TRACKLIST), "shazam"

        monkeypatch.setattr(handlers_mod, "analyze_audio_with_cue", _fake)

        summary = await run_backfill()
        assert summary["status"] == "partial"
        assert summary["processed"] == 1
        assert summary["failed"] == 1
        assert any("decode blew up" in e for e in summary["errors"])

    async def test_no_proposals_when_no_tracks_found(
        self, prepared_db, audio_dir, monkeypatch
    ):
        from sqlalchemy import select
        from app.database import async_session_factory
        from app.models import MixProposal

        import app.services.handlers as handlers_mod

        (audio_dir / "a-2025-06-14.flac").write_bytes(b"x")
        await _make_imported_mix(
            "m1", "Set One",
            youtube_video_id="vid1",
            youtube_url="https://www.youtube.com/watch?v=vid1",
        )

        async def _fake(audio_path, mix_id=None, progress_cb=None, analyzer=None):
            return FakeResult(), [], "none"

        monkeypatch.setattr(handlers_mod, "analyze_audio_with_cue", _fake)

        summary = await run_backfill()
        assert summary["processed"] == 1
        assert summary["proposals_created"] == 0
        async with async_session_factory() as session:
            assert (await session.execute(select(MixProposal))).scalars().all() == []


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


class TestBackfillApi:
    @pytest.fixture(autouse=True)
    def _fresh_task_state(self, monkeypatch):
        import app.routers.catalog as catalog_router

        monkeypatch.setattr(catalog_router, "_backfill_task", None)

    @pytest.fixture
    def stub_run(self, monkeypatch):
        import app.services.catalog_backfill as backfill_mod

        calls = []

        async def fake_run(mix_ids=None):
            calls.append(mix_ids)
            return {"status": "ok"}

        monkeypatch.setattr(backfill_mod, "run_backfill", fake_run)
        return calls

    async def test_trigger_returns_202_and_runs(self, client, stub_run):
        resp = await client.post("/api/catalog/backfill-tracklists", json={})
        assert resp.status_code == 202
        assert resp.json() == {"status": "started"}

        import asyncio
        import app.routers.catalog as catalog_router

        await asyncio.wait_for(catalog_router._backfill_task, timeout=5)
        assert stub_run == [None]

    async def test_trigger_accepts_mix_ids(self, client, stub_run):
        resp = await client.post(
            "/api/catalog/backfill-tracklists", json={"mix_ids": ["m1", "m2"]}
        )
        assert resp.status_code == 202

        import asyncio
        import app.routers.catalog as catalog_router

        await asyncio.wait_for(catalog_router._backfill_task, timeout=5)
        assert stub_run == [["m1", "m2"]]

    async def test_single_flight(self, client, monkeypatch):
        import asyncio
        import app.services.catalog_backfill as backfill_mod

        release = asyncio.Event()

        async def slow_run(mix_ids=None):
            await release.wait()
            return {"status": "ok"}

        monkeypatch.setattr(backfill_mod, "run_backfill", slow_run)

        first = await client.post("/api/catalog/backfill-tracklists", json={})
        assert first.json() == {"status": "started"}
        second = await client.post("/api/catalog/backfill-tracklists", json={})
        assert second.json() == {"status": "already_running"}

        status = await client.get("/api/catalog/backfill-status")
        assert status.json()["running"] is True

        release.set()
        import app.routers.catalog as catalog_router

        await asyncio.wait_for(catalog_router._backfill_task, timeout=5)

    async def test_status_reports_last_summary(self, client):
        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(
                AppSettings(
                    id=1,
                    settings_json={LAST_BACKFILL_KEY: {"status": "ok", "matched": 3}},
                )
            )
            await session.commit()

        resp = await client.get("/api/catalog/backfill-status")
        body = resp.json()
        assert body["running"] is False
        assert body["last_backfill"] == {"status": "ok", "matched": 3}

    async def test_cancel_when_not_running(self, client):
        resp = await client.post("/api/catalog/backfill-cancel")
        assert resp.json() == {"status": "not_running"}

    async def test_cancel_sets_flag_while_running(self, client, monkeypatch):
        import asyncio
        import app.services.catalog_backfill as backfill_mod
        from app.services.catalog_backfill import BACKFILL_CANCEL_KEY

        release = asyncio.Event()

        async def slow_run(mix_ids=None):
            await release.wait()
            return {"status": "ok"}

        monkeypatch.setattr(backfill_mod, "run_backfill", slow_run)
        await client.post("/api/catalog/backfill-tracklists", json={})

        resp = await client.post("/api/catalog/backfill-cancel")
        assert resp.json() == {"status": "cancelling"}

        from sqlalchemy import select
        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.id == 1))
            ).scalar_one()
            assert row.settings_json[BACKFILL_CANCEL_KEY] is True

        release.set()
        import app.routers.catalog as catalog_router

        await asyncio.wait_for(catalog_router._backfill_task, timeout=5)
