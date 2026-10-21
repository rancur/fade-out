"""verify_youtube records a home-region block instead of passing it silently."""

from types import SimpleNamespace

import app.services.handlers as handlers
from app.services.handlers import handle_verify_youtube


class FakeSession:
    def __init__(self, mix):
        self._mix = mix

    async def get(self, model, key):
        if getattr(model, "__name__", "") == "AppSettings":
            return None
        return self._mix


def _patch(monkeypatch, verify_result):
    events = []

    async def emit(level, event, message, **kwargs):
        events.append((level, event, message))

    class FakeUploader:
        def __init__(self, *a, **k):
            pass

        async def verify_upload(self, video_id):
            return {**verify_result, "video_id": video_id}

    import app.services.youtube_uploader as yu

    monkeypatch.setattr(handlers, "_emit_activity", emit)
    monkeypatch.setattr(yu, "YouTubeUploader", FakeUploader)
    return events


MIX = SimpleNamespace(id="m1", youtube_url="https://www.youtube.com/watch?v=blockedvid1")


async def test_home_region_block_is_recorded_not_swallowed(monkeypatch):
    events = _patch(monkeypatch, {
        "status": "uploaded", "processing": "processing",
        "region_restriction": {"blocked": ["US"]},
        "home_region_block": "blocked in US (regionRestriction.blocked=['US'])",
    })
    out = await handle_verify_youtube("m1", FakeSession(MIX))

    assert out["verified"] is True  # the upload itself worked
    assert "blocked in US" in out["home_region_block"]
    assert [e[:2] for e in events] == [("warn", "youtube_region_blocked")]


async def test_unblocked_video_emits_nothing(monkeypatch):
    events = _patch(monkeypatch, {
        "status": "processed", "processing": "succeeded",
        "region_restriction": None, "home_region_block": None,
    })
    out = await handle_verify_youtube("m1", FakeSession(MIX))

    assert out["home_region_block"] is None
    assert events == []
