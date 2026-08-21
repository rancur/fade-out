"""Out-of-order audio/video pairing tests for the IngestCoordinator.

Covers both arrival orders, expiry (audio-only proceeds, video-only is dropped),
and duplicate-drop protection.
"""


import pytest

from app.config import settings
from app.database import async_session_factory
from app.models import Mix
from app.services.ingest import IngestCoordinator


class _FakeOrch:
    def __init__(self):
        self.started = []

    async def start_pipeline(self, mix_id):
        self.started.append(mix_id)


@pytest.fixture
def watch_dirs(tmp_path, monkeypatch):
    audio = tmp_path / "audio"
    video = tmp_path / "video"
    audio.mkdir()
    video.mkdir()
    monkeypatch.setattr(settings, "WATCH_AUDIO_PATH", str(audio))
    monkeypatch.setattr(settings, "WATCH_VIDEO_PATH", str(video))
    # Keep the disk guard from interfering in CI.
    monkeypatch.setattr(IngestCoordinator, "_disk_ok", staticmethod(lambda: True))
    return audio, video


def _touch(path):
    with open(path, "wb") as f:
        f.write(b"x" * 2_000_000)  # 2 MB, above the min-size floor
    return str(path)


async def _mix_count():
    from sqlalchemy import func, select

    async with async_session_factory() as s:
        return (await s.execute(select(func.count()).select_from(Mix))).scalar()


@pytest.mark.asyncio
async def test_audio_first_then_video(prepared_db, watch_dirs):
    audio_dir, video_dir = watch_dirs
    orch = _FakeOrch()
    coord = IngestCoordinator(orch)

    a = _touch(audio_dir / "2026-07-15 set.flac")
    await coord.ingest_audio(a)
    # No sibling yet -> pending, no mix started.
    assert orch.started == []
    assert await _mix_count() == 0
    snap = coord.pending_snapshot()
    assert snap and snap[0]["waiting_for"] == "video"

    v = _touch(video_dir / "2026-07-15 set.mkv")
    await coord.ingest_video(v)
    # Sibling arrived -> paired and started exactly once.
    assert len(orch.started) == 1
    assert await _mix_count() == 1
    assert coord.pending_snapshot() == []


@pytest.mark.asyncio
async def test_video_first_then_audio(prepared_db, watch_dirs):
    audio_dir, video_dir = watch_dirs
    orch = _FakeOrch()
    coord = IngestCoordinator(orch)

    v = _touch(video_dir / "2026-07-15 set.mkv")
    await coord.ingest_video(v)
    assert orch.started == []
    snap = coord.pending_snapshot()
    assert snap and snap[0]["waiting_for"] == "audio"

    a = _touch(audio_dir / "2026-07-15 set.flac")
    await coord.ingest_audio(a)
    assert len(orch.started) == 1
    assert await _mix_count() == 1


@pytest.mark.asyncio
async def test_date_fallback_pairs_differently_named(prepared_db, watch_dirs):
    audio_dir, video_dir = watch_dirs
    orch = _FakeOrch()
    coord = IngestCoordinator(orch)

    a = _touch(audio_dir / "Twitch DJs Mix (2026-07-15).flac")
    v = _touch(video_dir / "will-see-stream-2026-07-15.mkv")
    await coord.ingest_audio(a)
    await coord.ingest_video(v)
    assert len(orch.started) == 1


@pytest.mark.asyncio
async def test_audio_only_expiry_proceeds(prepared_db, watch_dirs, monkeypatch):
    audio_dir, _ = watch_dirs
    monkeypatch.setattr(settings, "PAIRING_WAIT_SECONDS", 0)
    orch = _FakeOrch()
    coord = IngestCoordinator(orch)

    a = _touch(audio_dir / "2026-07-15 set.flac")
    await coord.ingest_audio(a)
    assert orch.started == []  # still pending immediately

    await coord._sweep_once()  # window (0s) has passed -> audio-only run
    assert len(orch.started) == 1
    assert await _mix_count() == 1


@pytest.mark.asyncio
async def test_video_only_expiry_dropped(prepared_db, watch_dirs, monkeypatch):
    _, video_dir = watch_dirs
    monkeypatch.setattr(settings, "PAIRING_WAIT_SECONDS", 0)
    orch = _FakeOrch()
    coord = IngestCoordinator(orch)

    v = _touch(video_dir / "2026-07-15 set.mkv")
    await coord.ingest_video(v)
    await coord._sweep_once()  # expires; audio mandatory -> not run
    assert orch.started == []
    assert await _mix_count() == 0
    assert coord.pending_snapshot() == []


@pytest.mark.asyncio
async def test_duplicate_drop_no_second_run(prepared_db, watch_dirs):
    audio_dir, video_dir = watch_dirs
    orch = _FakeOrch()
    coord = IngestCoordinator(orch)

    a = _touch(audio_dir / "2026-07-15 set.flac")
    v = _touch(video_dir / "2026-07-15 set.mkv")
    await coord.ingest_audio(a)
    await coord.ingest_video(v)
    assert len(orch.started) == 1
    # Re-dropping the same audio must not create a second mix.
    await coord.ingest_audio(a)
    assert len(orch.started) == 1
    assert await _mix_count() == 1
