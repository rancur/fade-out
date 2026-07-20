"""Tests for YouTube premiere-time selection, privacy resolution, and Shorts."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.services.youtube_uploader import YouTubeUploader


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
