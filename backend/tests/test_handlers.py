"""Tests for handler helpers and pipeline-step lock hygiene (no network)."""

from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

import app.services.handlers as handlers_mod
from app.database import async_session_factory
from app.models import AppSettings, Mix, UsedCreative
from app.services.handlers import (
    YT_QUOTA_KEY,
    YT_UPLOAD_QUOTA_COST,
    _ensure_tracklist_section,
    _sc_token_persister,
    _title_from_filename,
    extract_youtube_video_id,
)


class TestExtractYoutubeVideoId:
    def test_watch_url(self):
        assert extract_youtube_video_id(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_watch_url_extra_params(self):
        assert extract_youtube_video_id(
            "https://www.youtube.com/watch?list=abc&v=dQw4w9WgXcQ&t=30s"
        ) == "dQw4w9WgXcQ"

    def test_short_url(self):
        assert extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url_with_query(self):
        assert extract_youtube_video_id(
            "https://youtu.be/dQw4w9WgXcQ?t=42"
        ) == "dQw4w9WgXcQ"

    def test_shorts(self):
        assert extract_youtube_video_id(
            "https://www.youtube.com/shorts/dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_embed(self):
        assert extract_youtube_video_id(
            "https://www.youtube.com/embed/dQw4w9WgXcQ"
        ) == "dQw4w9WgXcQ"

    def test_bare_id(self):
        assert extract_youtube_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_empty(self):
        assert extract_youtube_video_id("") == ""


class TestTitleFromFilename:
    def test_separators_to_spaces_and_title_case(self):
        assert _title_from_filename("/watch/audio/desert_session-live.flac") == "Desert Session Live"

    def test_plain(self):
        assert _title_from_filename("midnight groove.flac") == "Midnight Groove"


class _FakeAppSettings:
    def __init__(self, settings_json=None):
        self.settings_json = settings_json


class _FakeSettingsRow:
    def __init__(self, settings_json=None, premiere_mode=None):
        self.settings_json = settings_json or {}
        self.premiere_mode = premiere_mode


class TestResolveYoutubePublishMode:
    """youtube_publish_mode resolution with legacy premiere_mode compat."""

    @staticmethod
    def _patch_resolve(monkeypatch, value="scheduled"):
        async def fake_resolve(key, force_refresh=False):
            assert key == "youtube_publish_mode"
            return value

        monkeypatch.setattr(handlers_mod.app_config, "resolve", fake_resolve)

    async def test_explicit_new_key_wins_over_legacy(self, monkeypatch):
        self._patch_resolve(monkeypatch, "immediate")
        row = _FakeSettingsRow(
            settings_json={"youtube_publish_mode": "immediate"},
            premiere_mode="unlisted",
        )
        assert await handlers_mod._resolve_youtube_publish_mode(row) == "immediate"

    async def test_stored_legacy_premiere_coerces_to_scheduled(self, monkeypatch):
        # The mode was removed (the Data API cannot create Premieres); a value
        # left in an existing DB must degrade to scheduled, not blow up.
        self._patch_resolve(monkeypatch, "premiere")
        row = _FakeSettingsRow(
            settings_json={"youtube_publish_mode": "premiere"},
            premiere_mode="unlisted",
        )
        assert await handlers_mod._resolve_youtube_publish_mode(row) == "scheduled"

    async def test_immediate_mode_resolves(self, monkeypatch):
        self._patch_resolve(monkeypatch, "immediate")
        row = _FakeSettingsRow(
            settings_json={"youtube_publish_mode": "immediate"},
            premiere_mode="scheduled",
        )
        assert await handlers_mod._resolve_youtube_publish_mode(row) == "immediate"

    async def test_legacy_unlisted_safety_flag_honored_when_key_unset(
        self, monkeypatch
    ):
        self._patch_resolve(monkeypatch)  # schema default
        row = _FakeSettingsRow(premiere_mode="unlisted")
        assert await handlers_mod._resolve_youtube_publish_mode(row) == "unlisted"

    async def test_legacy_instant_honored_when_key_unset(self, monkeypatch):
        self._patch_resolve(monkeypatch)
        row = _FakeSettingsRow(premiere_mode="instant")
        assert await handlers_mod._resolve_youtube_publish_mode(row) == "instant"

    async def test_default_is_scheduled(self, monkeypatch):
        self._patch_resolve(monkeypatch)
        row = _FakeSettingsRow(premiere_mode="scheduled")
        assert await handlers_mod._resolve_youtube_publish_mode(row) == "scheduled"

    async def test_no_settings_row_uses_resolved_default(self, monkeypatch):
        self._patch_resolve(monkeypatch)
        assert (
            await handlers_mod._resolve_youtube_publish_mode(None) == "scheduled"
        )


async def _seed_app_settings(settings_json):
    async with async_session_factory() as session:
        session.add(AppSettings(id=1, settings_json=settings_json))
        await session.commit()


async def _load_settings_json():
    async with async_session_factory() as session:
        row = await session.get(AppSettings, 1)
        return dict(row.settings_json or {}) if row else None


class TestScTokenPersister:
    """The persister must commit in its OWN session — flushing rotated tokens
    on the pipeline session held the sqlite write lock for the entire
    multi-GB upload when a refresh fired at upload start."""

    async def test_persists_rotated_pair_via_own_session(self, prepared_db):
        await _seed_app_settings(
            {"soundcloud_refresh_token": "old-rt", "other_key": "keep"}
        )

        # Only the None-check reads the caller's snapshot; the write goes
        # through a fresh session so no caller session/flush is involved.
        persist = _sc_token_persister(_FakeAppSettings(settings_json={}))
        await persist("new-at", "new-rt")

        # Visible from a brand-new session with no commit by the caller —
        # i.e. the persister committed durably on its own.
        sj = await _load_settings_json()
        assert sj["soundcloud_access_token"] == "new-at"
        assert sj["soundcloud_refresh_token"] == "new-rt"
        assert sj["other_key"] == "keep"

    async def test_missing_refresh_token_keeps_existing(self, prepared_db):
        await _seed_app_settings({"soundcloud_refresh_token": "old-rt"})

        persist = _sc_token_persister(_FakeAppSettings(settings_json={}))
        await persist("new-at", None)

        sj = await _load_settings_json()
        assert sj["soundcloud_access_token"] == "new-at"
        assert sj["soundcloud_refresh_token"] == "old-rt"

    async def test_no_app_settings_is_noop(self, prepared_db):
        persist = _sc_token_persister(None)
        await persist("new-at", "new-rt")  # must not raise
        assert await _load_settings_json() is None


class TestEnsureTracklistSection:
    """The LLM sometimes weaves track names into prose and omits the actual
    tracklist section (observed live). The tracklist is the product."""

    TRACKS = [
        {"timestamp_formatted": "0:00", "artist": "A1", "title": "T1"},
        {"timestamp_formatted": "3:30", "artist": "A2", "title": "T2"},
    ]

    def test_injects_before_brand_links_marker(self):
        desc = "Great mix prose.\nTwitch: https://twitch.tv/willsee\nIG: x"
        out = _ensure_tracklist_section(desc, self.TRACKS)
        assert "Tracklist:\n0:00 A1 - T1\n3:30 A2 - T2" in out
        assert out.index("Tracklist:") < out.index("Twitch: https://")

    def test_appends_at_end_when_marker_absent(self):
        out = _ensure_tracklist_section("Prose only.", self.TRACKS)
        assert out.endswith("Tracklist:\n0:00 A1 - T1\n3:30 A2 - T2")

    def test_skipped_when_section_already_present(self):
        desc = "Prose.\n\nTracklist:\n0:00 A1 - T1"
        assert _ensure_tracklist_section(desc, self.TRACKS) == desc

    def test_skipped_when_no_tracklist(self):
        assert _ensure_tracklist_section("Prose.", []) == "Prose."


class _RereadSession:
    """Returns the mix for Mix lookups, None otherwise (no AppSettings row)."""

    def __init__(self, mix):
        self._mix = mix

    async def get(self, model, key):
        if model is Mix:
            return self._mix
        return None


class TestRereadTitleConsistency:
    async def test_published_title_restored_in_descriptions(self, tmp_path, monkeypatch):
        # The regenerated description prose references the transient creative
        # title the generator just invented; the published title is restored
        # afterwards — without the replace, live SC/YT descriptions opened
        # with the wrong mix name.
        audio = tmp_path / "mix.flac"
        audio.write_bytes(b"x")
        mix = SimpleNamespace(
            title="Published Title",
            title_youtube="Published YT Title",
            audio_file_path=str(audio),
            description_soundcloud=None,
            description_youtube=None,
            youtube_url=None,
            soundcloud_url=None,
        )

        async def fake_analyze(mix_id, session):
            return {"tracks_found": 3, "tracklist_source": "cue"}

        async def fake_generate_description(mix_id, session):
            mix.title = "Transient Creative Title"
            mix.description_soundcloud = "Transient Creative Title opens with heat."
            mix.description_youtube = "Enjoy Transient Creative Title tonight."
            return {}

        monkeypatch.setattr(handlers_mod, "handle_analyze", fake_analyze)
        monkeypatch.setattr(
            handlers_mod, "handle_generate_description", fake_generate_description
        )

        out = await handlers_mod.handle_reread_tracklist("m1", _RereadSession(mix))

        assert mix.title == "Published Title"
        assert mix.title_youtube == "Published YT Title"
        assert "Transient Creative Title" not in mix.description_soundcloud
        assert mix.description_soundcloud == "Published Title opens with heat."
        assert mix.description_youtube == "Enjoy Published Title tonight."
        assert out["tracks_found"] == 3
        assert out["descriptions_updated"] == {"youtube": False, "soundcloud": False}


async def _add_mix(**kwargs):
    defaults = dict(title="Mix")
    defaults.update(kwargs)
    async with async_session_factory() as session:
        mix = Mix(**defaults)
        session.add(mix)
        await session.commit()
        return mix.id


class TestGenerateArtLockHygiene:
    """The uniqueness design phase must be COMMITTED before any art
    generation starts — holding the pipeline session's write txn across the
    minutes-long fal polls starved every other writer past the 30s busy
    timeout."""

    async def test_uniqueness_claims_committed_before_generation(
        self, prepared_db, monkeypatch, tmp_path
    ):
        from app.config import settings as app_settings_cfg
        from app.services.art_generator import ArtGenerator

        monkeypatch.setattr(
            app_settings_cfg, "OUTPUT_COVER_ART_PATH", str(tmp_path / "covers")
        )
        monkeypatch.setattr(
            app_settings_cfg, "OUTPUT_THUMBNAILS_PATH", str(tmp_path / "thumbs")
        )
        # No LLM: the hook falls back to the genre motif (no OpenAI call).
        monkeypatch.setattr(app_settings_cfg, "OPENAI_API_KEY", "")

        mix_id = await _add_mix(title="Desert Heat", genres=["house"])

        seen = {}

        async def _claims_from_fresh_session():
            # A brand-new session sees only COMMITTED rows — pending/flushed
            # writes on the handler's sessions are invisible here.
            async with async_session_factory() as check:
                rows = (
                    await check.execute(
                        select(UsedCreative).where(UsedCreative.mix_id == mix_id)
                    )
                ).scalars().all()
                return {r.kind for r in rows}

        async def fake_cover(self, **kwargs):
            seen["claims_at_cover_start"] = await _claims_from_fresh_session()
            return kwargs["output_path"]

        async def fake_thumb(self, **kwargs):
            seen["claims_at_thumb_start"] = await _claims_from_fresh_session()
            return kwargs["output_path"]

        monkeypatch.setattr(ArtGenerator, "generate_cover_art", fake_cover)
        monkeypatch.setattr(ArtGenerator, "generate_youtube_thumbnail", fake_thumb)

        async with async_session_factory() as session:
            out = await handlers_mod.handle_generate_art(mix_id, session)
            await session.commit()

        # Hook + scene claims were durable before the first generation began.
        assert seen["claims_at_cover_start"] == {"hook", "scene"}
        assert seen["claims_at_thumb_start"] == {"hook", "scene"}
        assert out["cover_art_path"].endswith(f"{mix_id}.jpg")
        assert out["thumbnail_path"].endswith(f"{mix_id}.jpg")

        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            assert mix.cover_art_path == out["cover_art_path"]
            assert mix.thumbnail_path == out["thumbnail_path"]


class TestUploadYoutubeQuotaLedger:
    """The main pipeline's videos.insert (1600 units) must be charged to the
    shared catalog_yt_quota ledger — only shorts and catalog-apply charged it
    before, so the day's spend was undercounted."""

    async def _run_upload(self, tmp_path, monkeypatch):
        from app.services.youtube_uploader import YouTubeUploader

        video = tmp_path / "mix.mp4"
        video.write_bytes(b"video")
        mix_id = await _add_mix(
            title="Quota Mix", video_file_path=str(video), genres=["house"]
        )

        async def fake_upload(self, **kwargs):
            return {
                "video_url": "https://www.youtube.com/watch?v=abc123def45",
                "video_id": "abc123def45",
                "playlist_id": None,
            }

        monkeypatch.setattr(YouTubeUploader, "upload", fake_upload)

        # The completeness gate would reject this fake video file (no real
        # container, fresh mtime) — these tests exercise the quota ledger.
        async def _always_complete(path, audio_duration):
            return True, "ok"

        monkeypatch.setattr(handlers_mod, "check_video_complete", _always_complete)

        async with async_session_factory() as session:
            out = await handlers_mod.handle_upload_youtube(mix_id, session)
            await session.commit()
        return out

    async def test_successful_upload_charges_1600_units(
        self, prepared_db, monkeypatch, tmp_path
    ):
        await _seed_app_settings({"youtube_refresh_token": "tok"})

        out = await self._run_upload(tmp_path, monkeypatch)

        assert out["video_id"] == "abc123def45"
        sj = await _load_settings_json()
        assert sj[YT_QUOTA_KEY] == {
            "date": date.today().isoformat(),
            "used": YT_UPLOAD_QUOTA_COST,
        }

    async def test_charge_accumulates_on_todays_ledger(
        self, prepared_db, monkeypatch, tmp_path
    ):
        await _seed_app_settings(
            {
                "youtube_refresh_token": "tok",
                YT_QUOTA_KEY: {"date": date.today().isoformat(), "used": 3200},
            }
        )

        await self._run_upload(tmp_path, monkeypatch)

        sj = await _load_settings_json()
        assert sj[YT_QUOTA_KEY]["used"] == 3200 + YT_UPLOAD_QUOTA_COST

    async def test_skipped_upload_charges_nothing(
        self, prepared_db, monkeypatch, tmp_path
    ):
        await _seed_app_settings({"youtube_refresh_token": "tok"})
        mix_id = await _add_mix(
            title="Already Up", youtube_url="https://youtu.be/abc123def45"
        )

        async with async_session_factory() as session:
            out = await handlers_mod.handle_upload_youtube(mix_id, session)
            await session.commit()

        assert out["skipped"] is True
        sj = await _load_settings_json()
        assert YT_QUOTA_KEY not in sj


class TestVideoCompletenessGate:
    """Regression suite for the partial-sync incident: the NAS sync from the
    OBS machine stalled mid-copy, leaving an MKV with 10m40s of a ~115-minute
    set (ffprobe duration=N/A, decode ending "File ended prematurely") — and
    upload_youtube would have published it. The handler must verify the video
    is complete and otherwise raise VideoNotReady so the orchestrator's
    5-minute poll loop waits out the sync."""

    async def _make_video_mix(self, tmp_path, audio_duration=6900.0, mtime_age=600):
        import os
        import time

        video = tmp_path / "mix.mkv"
        video.write_bytes(b"video")
        old = time.time() - mtime_age
        os.utime(video, (old, old))
        mix_id = await _add_mix(
            title="Partial Sync Mix",
            video_file_path=str(video),
            duration_seconds=audio_duration,
            genres=["house"],
        )
        return mix_id

    def _fake_probe(self, monkeypatch, duration):
        import app.services.pipeline as pipeline_mod

        async def fake(path):
            return duration

        monkeypatch.setattr(pipeline_mod, "_ffprobe_container_duration", fake)

    def _fake_uploader(self, monkeypatch):
        from app.services.youtube_uploader import YouTubeUploader

        calls = []

        async def fake_upload(self, **kwargs):
            calls.append(kwargs)
            return {
                "video_url": "https://www.youtube.com/watch?v=abc123def45",
                "video_id": "abc123def45",
                "playlist_id": None,
            }

        monkeypatch.setattr(YouTubeUploader, "upload", fake_upload)
        return calls

    async def test_unfinalized_video_raises_video_not_ready(
        self, prepared_db, monkeypatch, tmp_path
    ):
        import pytest
        from app.models import ActivityEvent
        from app.services.pipeline import VideoNotReady

        await _seed_app_settings({"youtube_refresh_token": "tok"})
        mix_id = await self._make_video_mix(tmp_path)
        self._fake_probe(monkeypatch, None)  # duration=N/A — truncated MKV
        upload_calls = self._fake_uploader(monkeypatch)

        async with async_session_factory() as session:
            with pytest.raises(VideoNotReady, match="no container duration"):
                await handlers_mod.handle_upload_youtube(mix_id, session)

        assert upload_calls == []  # the partial video never reached YouTube
        # The rejection reason lands in the activity log so the UI shows
        # why the step is waiting.
        async with async_session_factory() as session:
            events = (
                (
                    await session.execute(
                        select(ActivityEvent).where(
                            ActivityEvent.event == "video_incomplete"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert any("no container duration" in e.message for e in events)

    async def test_partial_duration_raises_video_not_ready(
        self, prepared_db, monkeypatch, tmp_path
    ):
        import pytest
        from app.services.pipeline import VideoNotReady

        await _seed_app_settings({"youtube_refresh_token": "tok"})
        # 640s of video against a 6900s set — the live incident's shape.
        mix_id = await self._make_video_mix(tmp_path, audio_duration=6900.0)
        self._fake_probe(monkeypatch, 640.0)
        upload_calls = self._fake_uploader(monkeypatch)

        async with async_session_factory() as session:
            with pytest.raises(VideoNotReady, match="likely partial sync"):
                await handlers_mod.handle_upload_youtube(mix_id, session)
        assert upload_calls == []

    async def test_still_growing_file_raises_video_not_ready(
        self, prepared_db, monkeypatch, tmp_path
    ):
        import pytest
        from app.services.pipeline import VideoNotReady

        await _seed_app_settings({"youtube_refresh_token": "tok"})
        # Full duration, but the file was touched seconds ago — still syncing.
        mix_id = await self._make_video_mix(tmp_path, mtime_age=5)
        self._fake_probe(monkeypatch, 6900.0)
        upload_calls = self._fake_uploader(monkeypatch)

        async with async_session_factory() as session:
            with pytest.raises(VideoNotReady, match="still syncing"):
                await handlers_mod.handle_upload_youtube(mix_id, session)
        assert upload_calls == []

    async def test_complete_video_uploads(self, prepared_db, monkeypatch, tmp_path):
        await _seed_app_settings({"youtube_refresh_token": "tok"})
        mix_id = await self._make_video_mix(tmp_path, audio_duration=6900.0)
        self._fake_probe(monkeypatch, 6890.0)  # >= 90% of the audio
        upload_calls = self._fake_uploader(monkeypatch)

        async with async_session_factory() as session:
            out = await handlers_mod.handle_upload_youtube(mix_id, session)
            await session.commit()

        assert out["video_id"] == "abc123def45"
        assert len(upload_calls) == 1

    async def test_unknown_audio_duration_skips_ratio_gate(
        self, prepared_db, monkeypatch, tmp_path
    ):
        # An imported/odd mix may not know its audio duration — the ratio
        # gate cannot apply, but the parse + stability gates still do.
        await _seed_app_settings({"youtube_refresh_token": "tok"})
        mix_id = await self._make_video_mix(tmp_path, audio_duration=None)
        self._fake_probe(monkeypatch, 640.0)
        self._fake_uploader(monkeypatch)

        async with async_session_factory() as session:
            out = await handlers_mod.handle_upload_youtube(mix_id, session)
            await session.commit()
        assert out["video_id"] == "abc123def45"


class TestWaitForVideoCeiling:
    """_wait_for_video re-checks the completeness gate every poll and takes
    its ceiling from the video_wait_max_checks setting (default 96 = 8h) —
    a stalled NAS sync can take hours to recover."""

    async def test_ceiling_resolves_from_settings_and_fails_with_reason(
        self, prepared_db, monkeypatch, tmp_path
    ):
        import app.services.pipeline as pipeline_mod
        from app.services import app_config
        from app.services.pipeline import PipelineOrchestrator

        await _seed_app_settings({"video_wait_max_checks": 2})
        app_config.invalidate_cache()

        video = tmp_path / "mix.mkv"
        video.write_bytes(b"v")
        mix_id = await _add_mix(
            title="Waiting Mix", video_file_path=str(video), duration_seconds=100.0
        )

        monkeypatch.setattr(pipeline_mod, "VIDEO_POLL_INTERVAL", 0)
        checks = []

        async def fake_check(path, audio_duration):
            checks.append(path)
            return False, "video 640s < 90% of audio 6900s — likely partial sync"

        monkeypatch.setattr(pipeline_mod, "check_video_complete", fake_check)

        resolved, reason = await PipelineOrchestrator()._wait_for_video(mix_id)
        assert resolved is False
        assert len(checks) == 2  # ceiling came from the DB setting, not 48
        assert "likely partial sync" in reason  # the step fails with WHY

    async def test_resolves_once_video_completes(
        self, prepared_db, monkeypatch, tmp_path
    ):
        import app.services.pipeline as pipeline_mod
        from app.services.pipeline import PipelineOrchestrator

        video = tmp_path / "mix.mkv"
        video.write_bytes(b"v")
        mix_id = await _add_mix(
            title="Recovering Mix", video_file_path=str(video), duration_seconds=100.0
        )

        monkeypatch.setattr(pipeline_mod, "VIDEO_POLL_INTERVAL", 0)
        results = iter(
            [
                (False, "video file modified 5s ago — still syncing"),
                (True, "ok"),
            ]
        )

        async def fake_check(path, audio_duration):
            return next(results)

        monkeypatch.setattr(pipeline_mod, "check_video_complete", fake_check)

        resolved, reason = await PipelineOrchestrator()._wait_for_video(
            mix_id, max_checks=5
        )
        assert resolved is True
        assert reason == "ok"
