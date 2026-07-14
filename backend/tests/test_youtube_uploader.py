"""Tests for YouTube premiere-time selection and privacy resolution."""

from datetime import datetime, timezone

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
