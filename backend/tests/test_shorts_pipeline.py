"""Tests for the YouTube Shorts pipeline (ffprobe/Shazam/LLM/YT all mocked)."""

import json
import os
from datetime import date, datetime, timedelta, timezone
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
    durations=None,  # optional {basename: duration} for multi-clip tests
):
    generator = generator or FakeGenerator()
    uploader = uploader or FakeUploader()

    async def fake_probe(path):
        import os as _os

        d = (durations or {}).get(_os.path.basename(path), duration)
        return {"duration": d, "width": width, "height": height}

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


async def _block_quota():
    """Exhaust today's shared YouTube quota so unmatched clips park as queued."""
    async with async_session_factory() as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == 1))
        ).scalar_one_or_none()
        if row is None:
            row = AppSettings(id=1)
            session.add(row)
        sj = dict(row.settings_json or {})
        sj[QUOTA_KEY] = {
            "date": date.today().isoformat(),
            "used": settings.YOUTUBE_DAILY_QUOTA_BUDGET,
        }
        row.settings_json = sj
        await session.commit()


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
    async def test_no_session_held_across_ffprobe_and_shazam(
        self, prepared_db, monkeypatch
    ):
        """Write-lock regression (same class as the catalog backfill bug):
        process_short used to hold ONE session across the whole run, with the
        dirty probe-field writes autoflushed by the catalog-dedupe SELECT and
        then kept uncommitted across ffmpeg + Shazam — holding sqlite's single
        write lock for the whole track-ID await. Both long awaits must now run
        with ZERO connections checked out of the engine pool (no open
        session/transaction anywhere)."""
        from app.database import engine

        checked_out = {}

        async def probe_ffprobe(path):
            checked_out["ffprobe"] = engine.pool.checkedout()
            return {"duration": 45.0, "width": 1080, "height": 1920}

        async def probe_identify(path, dur):
            checked_out["identify"] = engine.pool.checkedout()
            return ("Artist X", "Track Y")

        monkeypatch.setattr(shorts_pipeline, "ffprobe_clip", probe_ffprobe)
        monkeypatch.setattr(shorts_pipeline, "identify_track", probe_identify)
        monkeypatch.setattr(
            shorts_pipeline, "get_description_generator", lambda sj: FakeGenerator()
        )

        short_id = await _make_short()
        status = await ShortsService().process_short(short_id, auto_upload=False)

        assert status == "ready"
        assert checked_out == {"ffprobe": 0, "identify": 0}
        short = await _get_short(short_id)
        assert short.track_artist == "Artist X"
        assert (short.width, short.height) == (1080, 1920)

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
                    duration_seconds=44.5,  # within the 2.5s tolerance
                    youtube_video_id="ytEXIST",
                    youtube_url="https://www.youtube.com/watch?v=ytEXIST",
                    # Published the day after the clip was recorded (2026-05-14)
                    # — both the duration AND date gates are now required.
                    metadata_json={
                        "catalog": {"youtube": {"published_at": "2026-05-15T00:00:00Z"}}
                    },
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

    async def test_undated_candidates_never_match(
        self, prepared_db, monkeypatch
    ):
        # The date gate is mandatory: candidates without a published date (or
        # a clip without a parseable recording date) never dedupe-match —
        # better to upload-review than silently drop the clip.
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
                    # Months outside the 5-day window — fails the date gate.
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


class TestOneToOneDedupe:
    """Regression suite for the many-to-one dedupe bug: the per-clip
    duration±date match once linked 52 of 89 backlog clips onto just 3
    distinct video ids. Dedupe must be strictly one-to-one."""

    _MIX_META = {"catalog": {"youtube": {"published_at": "2026-06-01T20:00:00Z"}}}

    async def _seed_channel_video(self, video_id="ytONE", duration=45.0):
        async with async_session_factory() as session:
            mix = Mix(
                title="The real upload",
                duration_seconds=duration,
                youtube_video_id=video_id,
                youtube_url=f"https://www.youtube.com/watch?v={video_id}",
                metadata_json=self._MIX_META,
            )
            session.add(mix)
            await session.commit()
            return mix.id

    def _write_clips(self, tmp_path, durations):
        for name in durations:
            (tmp_path / name).write_bytes(name.encode() * 200)

    async def test_scan_links_at_most_one_clip_per_video(
        self, prepared_db, tmp_path, monkeypatch
    ):
        """N similar-duration clips + 1 channel video -> exactly ONE clip is
        skipped/linked (best score = duration_diff + 0.5*day_diff); the rest
        continue through the pipeline (queued here — quota blocked)."""
        durations = {
            "Backtrack 2026-06-01 12-00-00.mp4": 45.2,  # best score
            "Backtrack 2026-06-01 13-00-00.mp4": 45.6,
            "Backtrack 2026-06-02 12-00-00.mp4": 46.0,
        }
        self._write_clips(tmp_path, durations)
        _patch_pipeline(monkeypatch, durations=durations)
        await _block_quota()
        await self._seed_channel_video()

        summary = await ShortsService(watch_path=str(tmp_path)).scan()

        assert summary["ingested"] == 3
        assert summary["catalog_matched"] == 1

        async with async_session_factory() as session:
            rows = (await session.execute(select(Short))).scalars().all()
        linked = [r for r in rows if r.youtube_video_id == "ytONE"]
        assert len(linked) == 1  # the video id claims exactly ONE clip
        best = linked[0]
        assert os.path.basename(best.file_path) == "Backtrack 2026-06-01 12-00-00.mp4"
        assert best.status == "skipped"
        assert "already on channel" in best.error
        others = [r for r in rows if r.id != best.id]
        assert len(others) == 2
        assert all(r.status == "queued" for r in others)
        assert all(r.youtube_video_id is None for r in others)

    async def test_rescan_is_idempotent(self, prepared_db, tmp_path, monkeypatch):
        """A second scan preserves existing links: no re-claiming, no status
        churn, no duplicate rows."""
        durations = {
            "Backtrack 2026-06-01 12-00-00.mp4": 45.2,
            "Backtrack 2026-06-01 13-00-00.mp4": 45.6,
        }
        self._write_clips(tmp_path, durations)
        _patch_pipeline(monkeypatch, durations=durations)
        await _block_quota()
        await self._seed_channel_video()

        service = ShortsService(watch_path=str(tmp_path))
        await service.scan()

        async def snapshot():
            async with async_session_factory() as session:
                rows = (await session.execute(select(Short))).scalars().all()
            return {
                os.path.basename(r.file_path): (r.status, r.youtube_video_id)
                for r in rows
            }

        before = await snapshot()
        assert before["Backtrack 2026-06-01 12-00-00.mp4"] == ("skipped", "ytONE")
        assert before["Backtrack 2026-06-01 13-00-00.mp4"] == ("queued", None)

        second = await service.scan()
        assert second["already_known"] == 2
        assert second["ingested"] == 0
        assert second["catalog_matched"] == 1  # same link re-derived, not added
        assert second["requeued"] == 0
        assert await snapshot() == before

    async def test_better_new_clip_reclaims_link_and_requeues_loser(
        self, prepared_db, tmp_path, monkeypatch
    ):
        """A previously-skipped clip loses its link when a better-scoring new
        clip claims the same video id, and re-enters the pipeline."""
        durations = {"Backtrack 2026-06-01 12-00-00.mp4": 45.1}  # score ~0.27
        self._write_clips(tmp_path, durations)
        _patch_pipeline(monkeypatch, durations=durations)
        await _block_quota()
        mix_id = await self._seed_channel_video()

        # Weaker earlier match (duration diff 1.8) currently holds the link.
        loser_id = await _make_short(
            path="/watch/shorts/Backtrack 2026-06-01 10-00-00.mp4",
            status="skipped",
            duration_seconds=46.8,
            width=1080,
            height=1920,
            youtube_video_id="ytONE",
            youtube_url="https://www.youtube.com/watch?v=ytONE",
            error="already on channel (matched catalog mix 'The real upload')",
            metadata_json={
                "catalog_match": {"mix_id": mix_id, "mix_title": "The real upload"}
            },
        )

        summary = await ShortsService(watch_path=str(tmp_path)).scan()
        assert summary["catalog_matched"] == 1
        assert summary["requeued"] == 1

        async with async_session_factory() as session:
            rows = (await session.execute(select(Short))).scalars().all()
        winner = next(r for r in rows if r.id != loser_id)
        loser = await _get_short(loser_id)
        assert winner.status == "skipped"
        assert winner.youtube_video_id == "ytONE"
        # The loser went back through the pipeline (quota blocked -> queued)
        assert loser.youtube_video_id is None
        assert loser.status == "queued"
        assert not (loser.metadata_json or {}).get("catalog_match")

    async def test_watcher_path_never_double_claims(self, prepared_db, monkeypatch):
        """Single-clip path (watcher/un-skip): a video id already held by any
        other short row is off the table, so the clip proceeds to upload."""
        _patch_pipeline(monkeypatch, duration=45.2)
        async with async_session_factory() as session:
            session.add(
                Mix(
                    title="The real upload",
                    duration_seconds=45.0,
                    youtube_video_id="ytONE",
                    metadata_json={
                        "catalog": {"youtube": {"published_at": "2026-05-15T00:00:00Z"}}
                    },
                )
            )
            await session.commit()
        holder_id = await _make_short(
            path="/watch/shorts/Backtrack 2026-05-14 20-00-00.mp4",
            status="skipped",
            duration_seconds=45.4,
            youtube_video_id="ytONE",
            metadata_json={"catalog_match": {"mix_id": "x", "mix_title": "The real upload"}},
        )

        # New clip would match ytONE on duration+date, but it is claimed.
        short_id = await _make_short()  # Backtrack 2026-05-14 21-03-22.mp4
        status = await ShortsService().process_short(short_id)
        assert status == "uploaded"

        holder = await _get_short(holder_id)
        assert holder.status == "skipped"
        assert holder.youtube_video_id == "ytONE"

    async def test_dedupe_backlog_is_one_to_one_across_many_clips(
        self, prepared_db, monkeypatch
    ):
        """Direct dedupe_backlog: 5 similar clips vs 2 channel videos -> 2
        links max, best scores win, everyone else stays detected."""
        _patch_pipeline(monkeypatch)
        async with async_session_factory() as session:
            for vid, dur in (("ytA", 45.0), ("ytB", 52.0)):
                session.add(
                    Mix(
                        title=f"Upload {vid}",
                        duration_seconds=dur,
                        youtube_video_id=vid,
                        metadata_json=self._MIX_META,
                    )
                )
            await session.commit()

        clip_specs = [
            ("Backtrack 2026-06-01 12-00-00.mp4", 45.1),  # best for ytA
            ("Backtrack 2026-06-01 13-00-00.mp4", 45.4),
            ("Backtrack 2026-06-01 14-00-00.mp4", 46.1),
            ("Backtrack 2026-06-01 15-00-00.mp4", 52.3),  # best for ytB
            ("Backtrack 2026-06-01 16-00-00.mp4", 52.9),
        ]
        ids = {}
        for name, dur in clip_specs:
            ids[name] = await _make_short(
                path=f"/watch/shorts/{name}",
                status="detected",
                duration_seconds=dur,
                width=1080,
                height=1920,
            )

        result = await ShortsService().dedupe_backlog()
        assert result == {"linked": 2, "unlinked": 0}

        async with async_session_factory() as session:
            rows = (await session.execute(select(Short))).scalars().all()
        by_name = {os.path.basename(r.file_path): r for r in rows}
        assert by_name["Backtrack 2026-06-01 12-00-00.mp4"].youtube_video_id == "ytA"
        assert by_name["Backtrack 2026-06-01 15-00-00.mp4"].youtube_video_id == "ytB"
        claimed = [r.youtube_video_id for r in rows if r.youtube_video_id]
        assert sorted(claimed) == ["ytA", "ytB"]  # each video claimed once
        unmatched = [r for r in rows if r.youtube_video_id is None]
        assert len(unmatched) == 3
        assert all(r.status == "detected" for r in unmatched)


class TestDrainAndManualUpload:
    async def test_drain_uploads_newest_first_up_to_cap(
        self, prepared_db, monkeypatch
    ):
        """With a 60+ clip backlog, oldest-first made yesterday's clip wait
        weeks — the drain must upload the freshest recordings first."""
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
        # Newest first (detected_at fallback — no date token in these names)
        uploaded_titles = [c["title"] for c in uploader.calls]
        assert uploaded_titles == ["Queued 4", "Queued 3", "Queued 2"]

    async def test_drain_orders_by_recording_date_over_detected_at(
        self, prepared_db, monkeypatch
    ):
        """The Backtrack filename timestamp is the recording date the code
        tracks best; ingest order (detected_at) must not override it."""
        _, uploader = _patch_pipeline(monkeypatch)
        now = datetime.now(timezone.utc)
        # Recorded LATEST but ingested a month ago...
        await _make_short(
            path="/watch/shorts/Backtrack 2026-07-19 21-00-00.mp4",
            status="queued", title="Fresh recording",
            description="d #shorts", tags=[],
            detected_at=now - timedelta(days=30),
        )
        # ...vs recorded in May but ingested just now.
        await _make_short(
            path="/watch/shorts/Backtrack 2026-05-01 21-00-00.mp4",
            status="queued", title="Archive recording",
            description="d #shorts", tags=[],
            detected_at=now,
        )

        await ShortsService().drain_queue()

        titles = [c["title"] for c in uploader.calls]
        assert titles == ["Fresh recording", "Archive recording"]

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

    async def test_quota_gate_honors_db_budget_override(self, prepared_db):
        # The env budget (8000) would allow one more upload; a DB-set budget
        # must win (same resolution as catalog_apply), so 1000 blocks it.
        async with async_session_factory() as session:
            session.add(
                AppSettings(
                    id=1, settings_json={"youtube_daily_quota_budget": 1000}
                )
            )
            await session.commit()

        service = ShortsService()
        async with async_session_factory() as session:
            ok, reason = await service._can_upload(session)
        assert ok is False
        assert "quota budget reached (0/1000 units)" in reason

        stats = await service.stats()
        assert stats["quota_budget"] == 1000

    async def test_daily_cap_honors_db_override(self, prepared_db):
        # The env cap (3) would allow an upload with 0 uploaded today; a
        # DB-set cap of 0 must win.
        async with async_session_factory() as session:
            session.add(
                AppSettings(id=1, settings_json={"shorts_daily_upload_cap": 0})
            )
            await session.commit()

        service = ShortsService()
        async with async_session_factory() as session:
            ok, reason = await service._can_upload(session)
        assert ok is False
        assert "daily cap reached (0/0)" in reason

        stats = await service.stats()
        assert stats["daily_cap"] == 0
        assert stats["cap_reached"] is True


class TestDrainMetadataGate:
    """Regression suite for the metadata-less overnight uploads: repair-
    requeued rows (status='queued', no title/description) were uploaded raw
    by the drain. The drain must generate metadata first and never upload
    without it."""

    async def test_drain_generates_missing_metadata_before_upload(
        self, prepared_db, monkeypatch
    ):
        gen, uploader = _patch_pipeline(monkeypatch)
        # A repair-requeued row: queued, but analysis/metadata never ran.
        short_id = await _make_short(status="queued")

        summary = await ShortsService().drain_queue()

        assert summary["uploaded"] == 1
        short = await _get_short(short_id)
        assert short.status == "uploaded"
        # The missing phases ran before the upload...
        assert short.duration_seconds == 45.0
        assert short.track_artist == "Artist X"
        assert short.title and short.description
        assert gen.last_prompt is not None
        # ...and the upload carried the generated metadata, not the raw file.
        assert uploader.calls[0]["title"] == short.title
        assert "#shorts" in short.description

    async def test_generation_failure_leaves_clip_queued_and_drain_continues(
        self, prepared_db, monkeypatch
    ):
        # The LLM breaks -> the (newer) metadata-less clip must NOT upload
        # raw and must NOT wedge the drain for the (older) ready clip.
        _, uploader = _patch_pipeline(
            monkeypatch, generator=FakeGenerator(raw="not json at all")
        )
        bad_id = await _make_short(
            path="/watch/shorts/Backtrack 2026-07-18 21-00-00.mp4",
            status="queued",
            duration_seconds=45.0, width=1080, height=1920,
        )
        good_id = await _make_short(
            path="/watch/shorts/Backtrack 2026-07-01 21-00-00.mp4",
            status="queued", title="Good clip",
            description="d #shorts", tags=["dj"],
            duration_seconds=44.0, width=1080, height=1920,
        )

        summary = await ShortsService().drain_queue()

        bad = await _get_short(bad_id)
        assert bad.status == "queued"  # still queued — never uploaded raw
        assert "metadata generation failed" in bad.error
        good = await _get_short(good_id)
        assert good.status == "uploaded"
        assert [c["title"] for c in uploader.calls] == ["Good clip"]
        assert summary["uploaded"] == 1
        assert summary["remaining"] == 1


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
