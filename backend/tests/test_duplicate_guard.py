"""The same recording must never be published twice.

Regression for 2026-11-04: the original video files of two already-published
mixes reappeared in the watch directory, ingest paired them with the existing
FLACs, and the pipeline published both mixes again under new titles. These
tests replay that shape and also prove the guard does NOT stop a genuinely new
mix (the negative control).
"""

import shutil
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models import ActivityEvent, Mix, PipelineStep
from app.services import duplicate_guard
from app.services.duplicate_guard import (
    Fingerprint,
    STATUS_DUPLICATE,
    compare,
    find_duplicate,
)
from app.services.pipeline import PIPELINE_STEPS, PipelineOrchestrator
from app.services.stuck_mix_watchdog import StuckMixWatchdog

HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    import app.services.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "BASE_BACKOFF_SECONDS", 0)


def _write(path, payload: bytes):
    path.write_bytes(payload)
    return str(path)


async def _add_mix(mix_id, *, status="running", steps=True, hours_old=0, **fields):
    async with async_session_factory() as session:
        session.add(Mix(
            id=mix_id,
            title=fields.pop("title", f"Mix {mix_id}"),
            source=fields.pop("source", "pipeline"),
            pipeline_status=status,
            created_at=datetime.now(timezone.utc) - timedelta(hours=hours_old),
            **fields,
        ))
        if steps:
            for name in PIPELINE_STEPS:
                session.add(PipelineStep(mix_id=mix_id, step_name=name, status="pending"))
        await session.commit()


async def _mix(mix_id):
    async with async_session_factory() as session:
        return await session.get(Mix, mix_id)


def _register_publish_handlers(orch, calls):
    async def sc_upload(mix_id, session, **_kw):
        calls.append(("upload_soundcloud", mix_id))
        mix = await session.get(Mix, mix_id)
        mix.soundcloud_url = f"https://soundcloud.com/x/{mix_id}"
        return {"soundcloud_url": mix.soundcloud_url}

    async def yt_upload(mix_id, session, **_kw):
        calls.append(("upload_youtube", mix_id))
        mix = await session.get(Mix, mix_id)
        mix.youtube_url = f"https://youtube.com/watch?v={mix_id}"
        mix.youtube_video_id = mix_id
        return {"youtube_url": mix.youtube_url, "video_id": mix_id}

    async def ok(mix_id, session, **_kw):
        return {"verified": True}

    async def mc_upload(mix_id, session, **_kw):
        return {"skipped": True, "reason": "Mixcloud disabled"}

    async def mc_verify(mix_id, session, **_kw):
        return {"skipped": True, "reason": "No Mixcloud URL"}

    orch.register_handler("upload_soundcloud", sc_upload)
    orch.register_handler("verify_soundcloud", ok)
    orch.register_handler("upload_youtube", yt_upload)
    orch.register_handler("verify_youtube", ok)
    orch.register_handler("upload_mixcloud", mc_upload)
    orch.register_handler("verify_mixcloud", mc_verify)
    orch.register_handler("cross_link", ok)


# ---------------------------------------------------------------------------
# Pure comparison rules
# ---------------------------------------------------------------------------

class TestCompare:
    def test_duration_alone_is_never_a_match(self):
        """Two different two-hour mixes 3 s apart (seen in production)."""
        a = Fingerprint(duration=7142.4, video_head="aaa:1", audio_pcm="p1")
        b = Fingerprint(duration=7145.6, video_head="bbb:1", audio_pcm="p2")
        assert compare(a, "New Mix A | House Mix", b, "Old Mix B | Trance Mix") == []

    def test_same_bytes_but_far_apart_durations_is_not_a_match(self):
        a = Fingerprint(duration=3600.0, video_head="same:1")
        b = Fingerprint(duration=7200.0, video_head="same:1")
        assert compare(a, "A", b, "B") == []

    def test_identical_decoded_audio_matches_despite_retitle(self):
        a = Fingerprint(duration=6920.8, audio_pcm="pcm", audio_head="tagged:1")
        b = Fingerprint(duration=6921.0, audio_pcm="pcm", audio_head="untagged:1")
        assert compare(a, "Fresh AI Title", b, "Original Title") == [
            "identical decoded audio"
        ]

    def test_unknown_duration_still_matches_on_same_file(self):
        """A failed analyze leaves duration 0.0; the shared file still counts."""
        a = Fingerprint(duration=None, video_path="/watch/video/x.mkv")
        b = Fingerprint(duration=8057.4, video_path="/watch/video/x.mkv")
        assert "same video file" in compare(a, "2026-01-01 10-00-00", b, "Original Title")

    def test_title_stem_matches_only_file_less_catalog_rows(self):
        new = Fingerprint(duration=7332.1, audio_path="/watch/audio/a.flac")
        imported = Fingerprint(duration=7332.0)
        assert compare(
            new, "Stem Name: Draft Subtitle",
            imported, "Stem Name | Techno Mix",
        ) == ["same title and duration as a catalog upload"]
        # Same stem but a different duration is a different mix.
        assert compare(
            new, "Stem Name: Draft Subtitle",
            Fingerprint(duration=7826.0), "Stem Name | Techno Mix",
        ) == []


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------

class TestPipelineGuard:
    async def test_replayed_2026_11_04_duplicate_is_not_uploaded(self, prepared_db, tmp_path):
        """Same video bytes as a live mix: no upload, parked quietly."""
        video_bytes = b"OBS-RECORDING" * 4096
        original_video = _write(tmp_path / "original.mkv", video_bytes)
        reappeared_video = _write(tmp_path / "reappeared-obs-name.mkv", video_bytes)

        await _add_mix(
            "orig", status="completed", steps=False, hours_old=2500,
            title="Original Title", duration_seconds=6920.8,
            video_file_path=original_video,
            youtube_url="https://youtube.com/watch?v=orig", youtube_video_id="orig",
            soundcloud_url="https://soundcloud.com/x/orig",
        )
        await _add_mix(
            "dupe", title="Fresh AI Title", duration_seconds=6920.8,
            video_file_path=reappeared_video,
        )

        orch = PipelineOrchestrator()
        calls = []
        _register_publish_handlers(orch, calls)
        await orch._run_publish_phase("dupe")

        assert calls == [], "a duplicate must never reach an upload handler"
        mix = await _mix("dupe")
        assert mix.pipeline_status == STATUS_DUPLICATE
        assert mix.metadata_json["duplicate_of"] == "orig"
        assert "identical video bytes" in mix.metadata_json["duplicate_evidence"]["reasons"]
        assert mix.youtube_url is None and mix.soundcloud_url is None
        assert mix.pipeline_error is None

        async with async_session_factory() as session:
            steps = (
                await session.execute(select(PipelineStep).where(PipelineStep.mix_id == "dupe"))
            ).scalars().all()
            assert {s.status for s in steps} == {"skipped"}
            events = (
                await session.execute(
                    select(ActivityEvent).where(ActivityEvent.mix_id == "dupe")
                )
            ).scalars().all()
        levels = {(e.event, e.level) for e in events}
        assert ("duplicate_skipped", "info") in levels
        assert not any(level in ("warn", "error") for _e, level in levels)

    async def test_negative_control_new_mix_still_uploads(self, prepared_db, tmp_path):
        """A genuinely new recording beside a live one publishes normally."""
        await _add_mix(
            "live", status="completed", steps=False, hours_old=500,
            title="Live Mix", duration_seconds=7145.6,
            video_file_path=_write(tmp_path / "live.mkv", b"LIVE" * 4096),
            youtube_url="https://youtube.com/watch?v=live", youtube_video_id="live",
            soundcloud_url="https://soundcloud.com/x/live",
        )
        await _add_mix(
            "fresh", title="Brand New Mix", duration_seconds=7142.4,
            video_file_path=_write(tmp_path / "fresh.mkv", b"FRESH" * 4096),
        )

        orch = PipelineOrchestrator()
        calls = []
        _register_publish_handlers(orch, calls)
        await orch._run_publish_phase("fresh")

        assert ("upload_soundcloud", "fresh") in calls
        assert ("upload_youtube", "fresh") in calls
        mix = await _mix("fresh")
        assert mix.pipeline_status == "completed"
        assert "duplicate_of" not in (mix.metadata_json or {})
        # Its fingerprint is stored so a later copy can be caught after renames.
        assert mix.metadata_json["fingerprint"]["video_head"]

    async def test_stored_fingerprint_catches_copy_after_files_were_renamed(
        self, prepared_db, tmp_path
    ):
        payload = b"RECORDING" * 4096
        await _add_mix(
            "first", steps=False, title="First Title", duration_seconds=3600.0,
            video_file_path=_write(tmp_path / "first.mkv", payload),
        )
        assert await find_duplicate("first") is None  # stores its fingerprint
        async with async_session_factory() as session:
            m = await session.get(Mix, "first")
            m.video_file_path = str(tmp_path / "renamed-away.mkv")
            m.pipeline_status = "completed"
            m.youtube_url = "https://youtube.com/watch?v=first"
            await session.commit()

        await _add_mix(
            "second", title="Second Title", duration_seconds=3601.0,
            video_file_path=_write(tmp_path / "again.mkv", payload),
        )
        match = await find_duplicate("second")
        assert match is not None and match.duplicate_of == "first"

    async def test_allow_duplicate_override_uploads(self, prepared_db, tmp_path):
        payload = b"SAME" * 4096
        await _add_mix(
            "orig2", status="completed", steps=False, duration_seconds=100.0,
            video_file_path=_write(tmp_path / "o.mkv", payload),
            youtube_url="https://youtube.com/watch?v=o2",
        )
        await _add_mix(
            "wanted", duration_seconds=100.0,
            video_file_path=_write(tmp_path / "w.mkv", payload),
            metadata_json={"allow_duplicate": True},
        )
        orch = PipelineOrchestrator()
        calls = []
        _register_publish_handlers(orch, calls)
        await orch._run_publish_phase("wanted")
        assert ("upload_youtube", "wanted") in calls

    async def test_failed_never_published_mix_is_not_a_canonical(self, prepared_db, tmp_path):
        """Re-dropping a recording whose first run failed must still publish."""
        payload = b"RETRY" * 4096
        await _add_mix(
            "failed-run", status="failed", steps=False, duration_seconds=0.0,
            video_file_path=_write(tmp_path / "f.mkv", payload),
        )
        await _add_mix(
            "redo", duration_seconds=5000.0,
            video_file_path=_write(tmp_path / "r.mkv", payload),
        )
        assert await find_duplicate("redo") is None

    async def test_own_catalog_record_is_not_a_duplicate(self, prepared_db, tmp_path):
        """Retrying a partly published mix must not match its own catalog row."""
        payload = b"PARTIAL" * 4096
        await _add_mix(
            "partial", status="partial", duration_seconds=7142.4,
            video_file_path=_write(tmp_path / "p.mkv", payload),
            soundcloud_url="https://soundcloud.com/x/partial", soundcloud_track_id="42",
        )
        await _add_mix(
            "catalog-row", status="imported", source="imported", steps=False,
            duration_seconds=7142.4, video_file_path=_write(tmp_path / "copy.mkv", payload),
            soundcloud_track_id="42",
        )
        assert await find_duplicate("partial") is None

    async def test_guard_crash_fails_open(self, prepared_db, monkeypatch):
        async def boom(_mix_id):
            raise RuntimeError("guard bug")

        monkeypatch.setattr(duplicate_guard, "find_duplicate", boom)
        await _add_mix("open", duration_seconds=100.0)
        orch = PipelineOrchestrator()
        calls = []
        _register_publish_handlers(orch, calls)
        await orch._run_publish_phase("open")
        assert ("upload_youtube", "open") in calls

    async def test_retry_of_upload_step_is_guarded(self, prepared_db, tmp_path):
        payload = b"RETRYSTEP" * 4096
        await _add_mix(
            "canon", status="completed", steps=False, duration_seconds=200.0,
            video_file_path=_write(tmp_path / "c.mkv", payload),
            soundcloud_url="https://soundcloud.com/x/canon",
        )
        await _add_mix(
            "copy", status="failed", duration_seconds=200.0,
            video_file_path=_write(tmp_path / "copy.mkv", payload),
        )
        orch = PipelineOrchestrator()
        calls = []
        _register_publish_handlers(orch, calls)
        assert await orch.retry_step("copy", "upload_soundcloud") is False
        assert calls == []
        assert (await _mix("copy")).pipeline_status == STATUS_DUPLICATE


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
class TestDecodedAudioHash:
    async def test_retagged_flac_has_the_same_audio_hash(self, tmp_path):
        """Tag rewrites change the bytes, not the audio."""
        import subprocess

        src = tmp_path / "a.flac"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
             "-metadata", "title=one", str(src)],
            check=True,
        )
        retagged = tmp_path / "b.flac"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(src), "-c", "copy",
             "-metadata", "title=a much longer different title", str(retagged)],
            check=True,
        )
        other = tmp_path / "c.flac"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=8",
             str(other)],
            check=True,
        )
        h1 = await duplicate_guard.audio_pcm_hash(str(src), 8.0)
        h2 = await duplicate_guard.audio_pcm_hash(str(retagged), 8.0)
        h3 = await duplicate_guard.audio_pcm_hash(str(other), 8.0)
        assert h1 and h1 == h2
        assert h3 and h3 != h1
        assert duplicate_guard.file_head_hash(str(src)) != duplicate_guard.file_head_hash(
            str(retagged)
        )


class TestStuckWatchdogSuppression:
    async def test_duplicate_is_not_reported_as_stuck(self, prepared_db):
        await _add_mix("dup-old", status=STATUS_DUPLICATE, steps=False, hours_old=2000,
                       metadata_json={"duplicate_of": "orig"})
        await _add_mix("draft-dup", status="draft_review", steps=False, hours_old=2000,
                       metadata_json={"duplicate_of": "orig"})
        await _add_mix("really-stuck", status="failed", steps=False, hours_old=2000)

        result = await StuckMixWatchdog().check(alert=False)
        assert {e["id"] for e in result["stuck"]} == {"really-stuck"}
