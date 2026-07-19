"""Tests for the auto-ingest coordinator that pairs watched files into runs."""

import pytest

from app.services.ingest import IngestCoordinator


class _FakeOrchestrator:
    def __init__(self):
        self.started: list[str] = []

    async def start_pipeline(self, mix_id: str) -> None:
        self.started.append(mix_id)


@pytest.fixture
def watch_dirs(tmp_path, monkeypatch):
    audio = tmp_path / "audio"
    video = tmp_path / "video"
    audio.mkdir()
    video.mkdir()
    monkeypatch.setattr("app.services.ingest.settings.WATCH_AUDIO_PATH", str(audio))
    monkeypatch.setattr("app.services.ingest.settings.WATCH_VIDEO_PATH", str(video))
    return audio, video


async def _fake_create_and_start(coord, orch):
    """Patch DB write out; just record the start call."""
    async def _impl(stem, audio, video):
        coord._last = (stem, audio, video)
        await orch.start_pipeline(f"mix-{stem}")

    coord._create_and_start = _impl


class TestIngestCoordinator:
    async def test_video_without_audio_waits(self, watch_dirs):
        _, video = watch_dirs
        orch = _FakeOrchestrator()
        coord = IngestCoordinator(orch)
        await _fake_create_and_start(coord, orch)

        vfile = video / "set.mkv"
        vfile.write_bytes(b"v")
        await coord.ingest_video(str(vfile))

        assert orch.started == []  # no audio sibling yet

    async def test_audio_then_video_pairs_once(self, watch_dirs):
        audio, video = watch_dirs
        orch = _FakeOrchestrator()
        coord = IngestCoordinator(orch)
        await _fake_create_and_start(coord, orch)

        afile = audio / "set.flac"
        vfile = video / "set.mkv"
        afile.write_bytes(b"a")
        vfile.write_bytes(b"v")

        # Audio arrives first, finds the sibling video already on disk.
        await coord.ingest_audio(str(afile))
        # Video callback fires next -- must NOT create a second run.
        await coord.ingest_video(str(vfile))

        assert orch.started == ["mix-set"]
        assert coord._last == ("set", str(afile), str(vfile))

    async def test_audio_only_starts_with_no_video(self, watch_dirs):
        audio, _ = watch_dirs
        orch = _FakeOrchestrator()
        coord = IngestCoordinator(orch)
        await _fake_create_and_start(coord, orch)

        afile = audio / "solo.flac"
        afile.write_bytes(b"a")
        await coord.ingest_audio(str(afile))

        assert orch.started == ["mix-solo"]
        assert coord._last == ("solo", str(afile), None)
