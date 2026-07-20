"""Tests for the YouTube Shorts pipeline (ffprobe/Shazam/LLM/YT all mocked)."""

import json
from datetime import date, datetime, time as dt_time, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import AppSettings, Mix, Short
from app.services import shorts_pipeline
from app.services.shorts_pipeline import (
    QUOTA_KEY,
    YT_SHORT_UPLOAD_COST,
    ShortsService,
    _enforce_hashtags,
    _enforce_title,
    generate_short_metadata,
    parse_recording_date,
    serialize_short,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGenerator:
    """Stands in for DescriptionGenerator: returns canned JSON metadata."""

    _model = "gpt-4o"

    def __init__(self, payload=None, raw=None):
        self.payload = payload or {
            "title": "Four decks, one drop 🔥 house heat from the desert",
            "description": (
                "Live four-deck moment from the desert.\n"
                "Track: Artist X - Track Y\n"
                "#shorts #dj #house #djset #electronicmusic"
            ),
            "tags": ["dj", "house", "dj set", "will see"],
        }
        self.raw = raw
        self.last_prompt = None

    async def _create_completion(self, prompt, max_tokens, temperature):
        self.last_prompt = prompt
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=60)
        )
        text = self.raw if self.raw is not None else json.dumps(self.payload)
        return response, text


class FakeUploader:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def upload_short(self, file_path, title, description, tags):
        self.calls.append({"file_path": file_path, "title": title, "tags": tags})
        if self.fail:
            raise RuntimeError("boom from YouTube")
        vid = f"vid{len(self.calls)}"
        return {"video_id": vid, "video_url": f"https://www.youtube.com/shorts/{vid}"}


def _patch_pipeline(
    monkeypatch,
    duration=45.0,
    width=1080,
    height=1920,
    track=("Artist X", "Track Y"),
    generator=None,
    uploader=None,
):
    generator = generator or FakeGenerator()
    uploader = uploader or FakeUploader()

    async def fake_probe(path):
        return {"duration": duration, "width": width, "height": height}

    async def fake_identify(path, dur):
        return track

    monkeypatch.setattr(shorts_pipeline, "ffprobe_clip", fake_probe)
    monkeypatch.setattr(shorts_pipeline, "identify_track", fake_identify)
    monkeypatch.setattr(
        shorts_pipeline, "get_description_generator", lambda sj: generator
    )
    monkeypatch.setattr(shorts_pipeline, "get_youtube_uploader", lambda sj: uploader)
    return generator, uploader


async def _make_short(path="/watch/shorts/Backtrack 2026-05-14 21-03-22.mp4", **kw):
    async with async_session_factory() as session:
        short = Short(file_path=path, file_hash=kw.pop("file_hash", None), **kw)
        session.add(short)
        await session.commit()
        return short.id


async def _get_short(short_id):
    async with async_session_factory() as session:
        return (
            await session.execute(select(Short).where(Short.id == short_id))
        ).scalar_one()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_parse_recording_date(self):
        dt = parse_recording_date("/watch/shorts/Backtrack 2026-05-14 21-03-22.mp4")
        assert dt == datetime(2026, 5, 14, 21, 3, 22)

    def test_parse_recording_date_none_for_other_names(self):
        assert parse_recording_date("clip.mp4") is None
        assert parse_recording_date("Backtrack 2026-13-99 25-00-00.mp4") is None

    def test_enforce_title_truncates_to_80(self):
        assert len(_enforce_title("x" * 200)) <= 80
        assert _enforce_title('"Hooked" ') == "Hooked"

    def test_enforce_hashtags_guarantees_shorts_tag(self):
        out = _enforce_hashtags("great clip, no tags")
        assert "#shorts" in out

    def test_enforce_hashtags_caps_at_15_keeping_shorts(self):
        # >15 hashtags makes YouTube ignore ALL of them — must be trimmed,
        # and #shorts must survive the trim.
        import re

        desc = "line\n" + " ".join(f"#tag{i}" for i in range(20)) + " #shorts"
        out = _enforce_hashtags(desc)
        found = re.findall(r"#\w+", out)
        assert len(found) <= 15
        assert "#shorts" in found


# ---------------------------------------------------------------------------
# Metadata generation
# ---------------------------------------------------------------------------


class TestMetadataGeneration:
    async def test_generates_title_description_tags(self, prepared_db):
        short = Short(
            file_path="/watch/shorts/Backtrack 2026-05-14 21-03-22.mp4",
            duration_seconds=42.0,
            track_artist="Artist X",
            track_title="Track Y",
        )
        gen = FakeGenerator()
        # generate_short_metadata resolves the generator via the factory hook.
        import unittest.mock as um

        with um.patch.object(
            shorts_pipeline, "get_description_generator", lambda sj: gen
        ):
            meta = await generate_short_metadata(
                short, ["Old title one", "Old title two"], None, {}
            )

        assert len(meta["title"]) <= 80
        assert "#shorts" in meta["description"]
        # Brand links appended deterministically after the model output
        assert "twitch.tv/thewillsee" in meta["description"]
        assert meta["tags"]
        # Track credit + uniqueness feed reach the prompt
        assert "Artist X - Track Y" in gen.last_prompt
        assert "Old title one" in gen.last_prompt

    async def test_non_json_output_raises(self, prepared_db):
        short = Short(file_path="/x.mp4", duration_seconds=30.0)
        gen = FakeGenerator(raw="not json at all")
        import unittest.mock as um

        with um.patch.object(
            shorts_pipeline, "get_description_generator", lambda sj: gen
        ):
            with pytest.raises(RuntimeError, match="non-JSON"):
                await generate_short_metadata(short, [], None, {})

    async def test_code_fenced_json_is_accepted(self, prepared_db):
        short = Short(file_path="/x.mp4", duration_seconds=30.0)
        payload = {
            "title": "Hooky title 🔥",
            "description": "line one\n#shorts #dj #house",
            "tags": ["dj"],
        }
        gen = FakeGenerator(raw=f"```json\n{json.dumps(payload)}\n```")
        import unittest.mock as um

        with um.patch.object(
            shorts_pipeline, "get_description_generator", lambda sj: gen
        ):
            meta = await generate_short_metadata(short, [], None, {})
        assert meta["title"] == "Hooky title 🔥"


# ---------------------------------------------------------------------------
# process_short
# ---------------------------------------------------------------------------


class TestProcessShort:
    async def test_vertical_clip_auto_uploads(self, prepared_db, monkeypatch):
        _, uploader = _patch_pipeline(monkeypatch)
        short_id = await _make_short()

        service = ShortsService()
        status = await service.process_short(short_id)

        assert status == "uploaded"
        short = await _get_short(short_id)
        assert short.track_artist == "Artist X"
        assert short.track_title == "Track Y"
        assert short.duration_seconds == 45.0
        assert (short.width, short.height) == (1080, 1920)
        assert short.youtube_video_id == "vid1"
        assert short.youtube_url == "https://www.youtube.com/shorts/vid1"
        assert short.uploaded_at is not None
        assert uploader.calls[0]["file_path"] == short.file_path

        # videos.insert (1600 units) charged to the shared daily ledger
        async with async_session_factory() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.id == 1))
            ).scalar_one()
            quota = (row.settings_json or {})[QUOTA_KEY]
            assert quota["date"] == date.today().isoformat()
            assert quota["used"] == YT_SHORT_UPLOAD_COST

    async def test_landscape_clip_is_skipped(self, prepared_db, monkeypatch):
        _patch_pipeline(monkeypatch, width=1920, height=1080)
        short_id = await _make_short()
        status = await ShortsService().process_short(short_id)
        assert status == "skipped"
        short = await _get_short(short_id)
        assert "not vertical" in short.error

    async def test_too_long_clip_is_skipped(self, prepared_db, monkeypatch):
        # Shorts ceiling is 180s (3 min since Oct 2024)
        _patch_pipeline(monkeypatch, duration=200.0)
        short_id = await _make_short()
        status = await ShortsService().process_short(short_id)
        assert status == "skipped"
        short = await _get_short(short_id)
        assert "Shorts limit" in short.error

    async def test_catalog_duration_match_skips_and_links(
        self, prepared_db, monkeypatch
    ):
        _patch_pipeline(monkeypatch, duration=45.0)
        async with async_session_factory() as session:
            session.add(
                Mix(
                    title="Existing short on channel",
                    duration_seconds=44.5,  # within the 2s tolerance
                    youtube_video_id="ytEXIST",
                    youtube_url="https://www.youtube.com/watch?v=ytEXIST",
                )
            )
            await session.commit()

        short_id = await _make_short()
        status = await ShortsService().process_short(short_id)
        assert status == "skipped"
        short = await _get_short(short_id)
        assert short.youtube_video_id == "ytEXIST"
        assert "already on channel" in short.error
        assert short.metadata_json["catalog_match"]["mix_title"] == (
            "Existing short on channel"
        )

    async def test_ambiguous_catalog_match_does_not_skip(
        self, prepared_db, monkeypatch
    ):
        # Two same-duration candidates and no usable published dates: better
        # to upload-review than silently drop the clip.
        _patch_pipeline(monkeypatch, duration=45.0)
        async with async_session_factory() as session:
            for i in range(2):
                session.add(
                    Mix(
                        title=f"Candidate {i}",
                        duration_seconds=45.0,
                        youtube_video_id=f"yt{i}",
                    )
                )
            await session.commit()
        short_id = await _make_short(path="/watch/shorts/no-date-name.mp4")
        status = await ShortsService().process_short(short_id)
        assert status == "uploaded"

    async def test_ambiguous_match_resolved_by_recording_date(
        self, prepared_db, monkeypatch
    ):
        _patch_pipeline(monkeypatch, duration=45.0)
        async with async_session_factory() as session:
            session.add(
                Mix(
                    title="Published before recording",
                    duration_seconds=45.0,
                    youtube_video_id="ytOLD",
                    metadata_json={
                        "catalog": {"youtube": {"published_at": "2026-01-01T00:00:00Z"}}
                    },
                )
            )
            session.add(
                Mix(
                    title="Published after recording",
                    duration_seconds=45.0,
                    youtube_video_id="ytNEW",
                    metadata_json={
                        "catalog": {"youtube": {"published_at": "2026-05-15T00:00:00Z"}}
                    },
                )
            )
            await session.commit()
        short_id = await _make_short()  # recorded 2026-05-14
        status = await ShortsService().process_short(short_id)
        assert status == "skipped"
        short = await _get_short(short_id)
        assert short.youtube_video_id == "ytNEW"

    async def test_daily_cap_queues_instead_of_uploading(
        self, prepared_db, monkeypatch
    ):
        _patch_pipeline(monkeypatch)
        # Cap (3) already consumed today
        now = datetime.now(timezone.utc)
        for i in range(settings.SHORTS_DAILY_UPLOAD_CAP):
            await _make_short(
                path=f"/watch/shorts/done{i}.mp4", status="uploaded", uploaded_at=now
            )
        short_id = await _make_short()
        status = await ShortsService().process_short(short_id)
        assert status == "queued"
        short = await _get_short(short_id)
        assert short.title  # metadata still generated
        assert short.status == "queued"

    async def test_quota_budget_queues_instead_of_uploading(
        self, prepared_db, monkeypatch
    ):
        _patch_pipeline(monkeypatch)
        async with async_session_factory() as session:
            session.add(
                AppSettings(
                    id=1,
                    settings_json={
                        QUOTA_KEY: {
                            "date": date.today().isoformat(),
                            "used": settings.YOUTUBE_DAILY_QUOTA_BUDGET - 100,
                        }
                    },
                )
            )
            await session.commit()
        short_id = await _make_short()
        status = await ShortsService().process_short(short_id)
        assert status == "queued"

    async def test_upload_failure_marks_failed(self, prepared_db, monkeypatch):
        _patch_pipeline(monkeypatch, uploader=FakeUploader(fail=True))
        short_id = await _make_short()
        status = await ShortsService().process_short(short_id)
        assert status == "failed"
        short = await _get_short(short_id)
        assert "boom from YouTube" in short.error

    async def test_ffprobe_failure_marks_failed(self, prepared_db, monkeypatch):
        _patch_pipeline(monkeypatch)

        async def broken_probe(path):
            raise RuntimeError("no such file")

        monkeypatch.setattr(shorts_pipeline, "ffprobe_clip", broken_probe)
        short_id = await _make_short()
        status = await ShortsService().process_short(short_id)
        assert status == "failed"
        short = await _get_short(short_id)
        assert "ffprobe failed" in short.error


# ---------------------------------------------------------------------------
# Ingest + scan
# ---------------------------------------------------------------------------


class TestIngestAndScan:
    async def test_ingest_dedupes_by_hash(self, prepared_db, tmp_path):
        clip = tmp_path / "Backtrack 2026-06-01 12-00-00.mp4"
        clip.write_bytes(b"same bytes" * 1000)
        service = ShortsService(watch_path=str(tmp_path))

        first = await service.ingest_file(str(clip))
        assert first is not None
        second = await service.ingest_file(str(clip))
        assert second is None  # hash already in the shorts table

    async def test_scan_ingests_mp4_only_and_processes(
        self, prepared_db, tmp_path, monkeypatch
    ):
        _patch_pipeline(monkeypatch)
        (tmp_path / "Backtrack 2026-06-01 12-00-00.mp4").write_bytes(b"a" * 2048)
        (tmp_path / "Backtrack 2026-06-02 12-00-00.mp4").write_bytes(b"b" * 2048)
        # .mkv sibling of the same recording must be ignored
        (tmp_path / "Backtrack 2026-06-01 12-00-00.mkv").write_bytes(b"c" * 2048)

        service = ShortsService(watch_path=str(tmp_path))
        summary = await service.scan()

        assert summary["files_seen"] == 2
        assert summary["ingested"] == 2
        assert summary["status"] == "ok"

        async with async_session_factory() as session:
            rows = (await session.execute(select(Short))).scalars().all()
        assert len(rows) == 2
        assert all(r.file_path.endswith(".mp4") for r in rows)
        # First upload of the day succeeds; scan summary persisted
        async with async_session_factory() as session:
            app_row = (
                await session.execute(select(AppSettings).where(AppSettings.id == 1))
            ).scalar_one()
            assert app_row.settings_json[shorts_pipeline.LAST_SCAN_KEY]["status"] == "ok"

    async def test_rescan_skips_known_files(self, prepared_db, tmp_path, monkeypatch):
        _patch_pipeline(monkeypatch)
        (tmp_path / "Backtrack 2026-06-01 12-00-00.mp4").write_bytes(b"a" * 2048)
        service = ShortsService(watch_path=str(tmp_path))
        await service.scan()
        summary = await service.scan()
        assert summary["already_known"] == 1
        assert summary["ingested"] == 0


# ---------------------------------------------------------------------------
# Queue drain + manual upload
# ---------------------------------------------------------------------------


class TestDrainAndManualUpload:
    async def test_drain_uploads_oldest_first_up_to_cap(
        self, prepared_db, monkeypatch
    ):
        _, uploader = _patch_pipeline(monkeypatch)
        old = datetime.now(timezone.utc) - timedelta(days=3)
        ids = []
        for i in range(5):
            ids.append(
                await _make_short(
                    path=f"/watch/shorts/q{i}.mp4",
                    status="queued",
                    title=f"Queued {i}",
                    description=f"desc {i} #shorts",
                    tags=["dj"],
                    detected_at=old + timedelta(hours=i),
                )
            )

        summary = await ShortsService().drain_queue()

        assert summary["uploaded"] == settings.SHORTS_DAILY_UPLOAD_CAP  # 3
        assert summary["remaining"] == 2
        # Oldest first
        uploaded_titles = [c["title"] for c in uploader.calls]
        assert uploaded_titles == ["Queued 0", "Queued 1", "Queued 2"]

    async def test_manual_upload_bypasses_cap_but_not_budget(
        self, prepared_db, monkeypatch
    ):
        _, uploader = _patch_pipeline(monkeypatch)
        now = datetime.now(timezone.utc)
        for i in range(settings.SHORTS_DAILY_UPLOAD_CAP):
            await _make_short(
                path=f"/watch/shorts/done{i}.mp4", status="uploaded", uploaded_at=now
            )
        short_id = await _make_short(
            status="queued", title="Manual", description="d #shorts", tags=[]
        )

        service = ShortsService()
        # Cap reached -> automatic path parks it...
        assert await service.upload_short_by_id(short_id, manual=False) == "queued"
        # ...manual bypasses the cap
        assert await service.upload_short_by_id(short_id, manual=True) == "uploaded"
        assert len(uploader.calls) == 1

        # But the shared quota budget is never bypassed
        async with async_session_factory() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.id == 1))
            ).scalar_one()
            sj = dict(row.settings_json or {})
            sj[QUOTA_KEY] = {
                "date": date.today().isoformat(),
                "used": settings.YOUTUBE_DAILY_QUOTA_BUDGET,
            }
            row.settings_json = sj
            await session.commit()
        short2 = await _make_short(
            path="/watch/shorts/q2.mp4",
            status="queued", title="Manual2", description="d #shorts", tags=[],
        )
        assert await service.upload_short_by_id(short2, manual=True) == "queued"

    async def test_stats_reports_cap_and_queue(self, prepared_db, monkeypatch):
        _patch_pipeline(monkeypatch)
        now = datetime.now(timezone.utc)
        await _make_short(path="/w/a.mp4", status="uploaded", uploaded_at=now)
        await _make_short(path="/w/b.mp4", status="queued")
        await _make_short(path="/w/c.mp4", status="queued")

        stats = await ShortsService().stats()
        assert stats["uploaded_today"] == 1
        assert stats["daily_cap"] == settings.SHORTS_DAILY_UPLOAD_CAP
        assert stats["queued"] == 2
        assert stats["cap_reached"] is False
        assert stats["quota_budget"] == settings.YOUTUBE_DAILY_QUOTA_BUDGET


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_serialize_short_includes_filename():
    short = Short(
        id="s1",
        file_path="/watch/shorts/Backtrack 2026-05-14 21-03-22.mp4",
        status="ready",
        detected_at=datetime(2026, 5, 14, 21, 5, 0),
    )
    data = serialize_short(short)
    assert data["filename"] == "Backtrack 2026-05-14 21-03-22.mp4"
    assert data["status"] == "ready"
    assert data["detected_at"] == "2026-05-14T21:05:00"
