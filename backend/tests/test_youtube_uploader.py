"""Tests for YouTube premiere-time selection, privacy resolution, and Shorts."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.services import app_config
from app.services.youtube_uploader import (
    PREMIERE_FALLBACK_MESSAGE,
    YouTubeUploader,
)


def _fake_resolve(values):
    """A stand-in for app_config.resolve backed by a plain dict."""

    async def resolve(key, force_refresh=False):
        return values[key]

    return resolve


class TestResolvePrivacy:
    def setup_method(self):
        self.up = YouTubeUploader()

    def test_instant_is_public_now(self):
        privacy, when = self.up._resolve_privacy("instant")
        assert privacy == "public"
        assert when is None

    def test_unlisted(self):
        privacy, when = self.up._resolve_privacy("unlisted")
        assert privacy == "unlisted"
        assert when is None

    def test_scheduled_is_private_future(self):
        privacy, when = self.up._resolve_privacy("scheduled")
        assert privacy == "private"
        assert when is not None
        assert when > datetime.now(timezone.utc)

    def test_unknown_mode_private_no_time(self):
        privacy, when = self.up._resolve_privacy("nonsense")
        assert privacy == "private"
        assert when is None


class TestCalculateOptimalTime:
    def setup_method(self):
        self.up = YouTubeUploader()

    def test_returns_future_tz_aware_utc(self):
        result = self.up._calculate_optimal_time()
        assert result.tzinfo is not None
        assert result > datetime.now(timezone.utc)

    def test_lands_on_a_premiere_slot_hour(self):
        # Converted back to Phoenix local, the hour must be one of the slots.
        from app.services.youtube_uploader import PHOENIX_UTC_OFFSET, PREMIERE_SLOTS

        result = self.up._calculate_optimal_time()
        phoenix = result.replace(tzinfo=None) + PHOENIX_UTC_OFFSET
        assert phoenix.hour in {hour for _, hour in PREMIERE_SLOTS}


class TestResolvePublish:
    """Async publish-mode resolution used by upload()."""

    def setup_method(self):
        self.up = YouTubeUploader()

    async def test_immediate_is_public_now(self):
        assert await self.up._resolve_publish("immediate") == ("public", None)

    async def test_legacy_instant_is_public_now(self):
        assert await self.up._resolve_publish("instant") == ("public", None)

    async def test_legacy_unlisted(self):
        assert await self.up._resolve_publish("unlisted") == ("unlisted", None)

    async def test_scheduled_uses_configured_day_hour(self, monkeypatch):
        monkeypatch.setattr(
            app_config, "resolve",
            _fake_resolve({"premiere_day": "friday", "premiere_hour_utc": 3}),
        )
        privacy, when = await self.up._resolve_publish("scheduled")
        assert privacy == "private"
        assert when.tzinfo is not None
        assert when > datetime.now(timezone.utc)
        assert when.weekday() == 4  # friday
        assert (when.hour, when.minute) == (3, 0)

    async def test_premiere_falls_back_to_scheduled_slot(self, monkeypatch):
        monkeypatch.setattr(
            app_config, "resolve",
            _fake_resolve({"premiere_day": "sunday", "premiere_hour_utc": 0}),
        )
        privacy, when = await self.up._resolve_publish("premiere")
        assert privacy == "private"
        assert when.weekday() == 6  # sunday
        assert when.hour == 0
        assert when > datetime.now(timezone.utc)

    async def test_unknown_mode_private_no_time(self):
        assert await self.up._resolve_publish("nonsense") == ("private", None)


class TestScheduledPublishTime:
    def setup_method(self):
        self.up = YouTubeUploader()

    async def test_unresolvable_config_falls_back_to_optimal_slots(
        self, monkeypatch
    ):
        async def boom(key, force_refresh=False):
            raise RuntimeError("no db")

        monkeypatch.setattr(app_config, "resolve", boom)
        sentinel = datetime(2030, 1, 4, 23, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(self.up, "_calculate_optimal_time", lambda: sentinel)
        assert await self.up._scheduled_publish_time() == sentinel

    async def test_bad_day_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            app_config, "resolve",
            _fake_resolve({"premiere_day": "someday", "premiere_hour_utc": 0}),
        )
        sentinel = datetime(2030, 1, 4, 23, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(self.up, "_calculate_optimal_time", lambda: sentinel)
        assert await self.up._scheduled_publish_time() == sentinel

    async def test_never_in_the_past(self, monkeypatch):
        # Whatever day it is now, the slot lands strictly in the future.
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(
            app_config, "resolve",
            _fake_resolve({
                "premiere_day": now.strftime("%A").lower(),
                "premiere_hour_utc": now.hour,
            }),
        )
        when = await self.up._scheduled_publish_time()
        assert when > now
        assert when.weekday() == now.weekday()


class TestUploadPublishModes:
    """Full upload() body per publish mode (API mocked)."""

    async def _upload(self, tmp_path, monkeypatch, mode):
        video = tmp_path / "mix 2026-07-18.mkv"
        video.write_bytes(b"fake mkv bytes")

        up = YouTubeUploader()
        fake_service = MagicMock()
        monkeypatch.setattr(up, "_get_service", lambda: fake_service)
        monkeypatch.setattr(
            up, "_resumable_upload", lambda request, *a, **k: {"id": "vid1"}
        )
        events = []

        async def record_activity(level, event, message, **kwargs):
            events.append((level, event, message))

        monkeypatch.setattr(up, "_activity", record_activity)
        monkeypatch.setattr(
            app_config, "resolve",
            _fake_resolve({"premiere_day": "thursday", "premiere_hour_utc": 0}),
        )

        result = await up.upload(
            video_path=str(video),
            title="Test Mix",
            description="desc",
            tags=["dj"],
            premiere_mode=mode,
        )
        body = fake_service.videos.return_value.insert.call_args.kwargs["body"]
        return body, events, result

    async def test_immediate_public_on_insert(self, tmp_path, monkeypatch):
        body, events, result = await self._upload(tmp_path, monkeypatch, "immediate")
        assert body["status"]["privacyStatus"] == "public"
        assert "publishAt" not in body["status"]
        assert not [e for e in events if e[1] == "premiere_fallback"]
        assert result["video_id"] == "vid1"

    async def test_scheduled_private_with_publish_at(self, tmp_path, monkeypatch):
        body, events, _ = await self._upload(tmp_path, monkeypatch, "scheduled")
        assert body["status"]["privacyStatus"] == "private"
        publish_at = datetime.fromisoformat(body["status"]["publishAt"])
        assert publish_at > datetime.now(timezone.utc)
        assert publish_at.weekday() == 3  # thursday, per configured day
        assert not [e for e in events if e[1] == "premiere_fallback"]

    async def test_premiere_falls_back_to_scheduled_with_warning(
        self, tmp_path, monkeypatch
    ):
        body, events, _ = await self._upload(tmp_path, monkeypatch, "premiere")
        assert body["status"]["privacyStatus"] == "private"
        assert "publishAt" in body["status"]
        warns = [e for e in events if e[1] == "premiere_fallback"]
        assert warns == [("warn", "premiere_fallback", PREMIERE_FALLBACK_MESSAGE)]


class TestUploadShort:
    async def test_upload_short_inserts_public_music_video(self, tmp_path, monkeypatch):
        clip = tmp_path / "Backtrack 2026-05-14 21-03-22.mp4"
        clip.write_bytes(b"fake mp4 bytes")

        up = YouTubeUploader()
        fake_service = MagicMock()
        insert_call = fake_service.videos.return_value.insert
        monkeypatch.setattr(up, "_get_service", lambda: fake_service)
        monkeypatch.setattr(
            up, "_resumable_upload", lambda request, *a, **k: {"id": "vid123"}
        )

        result = await up.upload_short(
            str(clip),
            title="T" * 120,  # gets clamped to YouTube's 100-char max
            description="desc #shorts",
            tags=["dj", "house"],
        )

        assert result == {
            "video_id": "vid123",
            "video_url": "https://www.youtube.com/shorts/vid123",
        }
        body = insert_call.call_args.kwargs["body"]
        assert body["snippet"]["categoryId"] == "10"  # Music
        assert len(body["snippet"]["title"]) == 100
        assert body["snippet"]["tags"] == ["dj", "house"]
        assert body["status"]["privacyStatus"] == "public"
        assert body["status"]["selfDeclaredMadeForKids"] is False

    async def test_upload_short_missing_file_raises(self):
        up = YouTubeUploader()
        with pytest.raises(FileNotFoundError):
            await up.upload_short("/nope/missing.mp4", "t", "d", [])
