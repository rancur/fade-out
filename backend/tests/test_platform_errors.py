"""The failure classifier must name the real cause, not the last symptom."""

import pytest

from app.services.platform_errors import (
    PlatformAuthError,
    classify_failure,
    platform_for_step,
)
from app.services.soundcloud_uploader import SoundCloudAuthError


class TestAuthClassification:
    def test_chained_auth_error_beats_the_playwright_symptom(self):
        """Exactly the 08-12 misdiagnosis, in one assertion.

        The browser fallback's selector timeout is the OUTER exception; the
        dead OAuth grant is chained beneath it. The alert must name the grant.
        """
        auth = SoundCloudAuthError(["refresh grant rejected: invalid_grant"])
        try:
            try:
                raise auth
            except SoundCloudAuthError as exc:
                raise RuntimeError(
                    "SoundCloud upload failed. API: authorization failed. "
                    "Browser: Page.wait_for_selector: Timeout 10000ms exceeded"
                ) from exc
        except RuntimeError as outer:
            cause = classify_failure(outer, "upload_soundcloud")

        assert cause.kind == "auth"
        assert cause.platform == "soundcloud"
        assert "invalid_grant" in cause.summary
        assert "SOUNDCLOUD" in cause.credential
        assert cause.retryable is False
        assert "re-auth" in (cause.remediation or "").lower()

    def test_bare_invalid_grant_text_is_still_auth(self):
        cause = classify_failure(
            RuntimeError("('invalid_grant: Token has been expired or revoked.')"),
            "upload_youtube",
        )
        assert cause.kind == "auth"
        assert cause.platform == "youtube"
        assert cause.credential == "YOUTUBE_REFRESH_TOKEN"
        assert cause.retryable is False

    def test_typed_error_carries_its_own_credential_name(self):
        exc = PlatformAuthError(
            "nope", platform="mixcloud", credential="MIXCLOUD_ACCESS_TOKEN"
        )
        cause = classify_failure(exc, "upload_mixcloud")
        assert cause.credential == "MIXCLOUD_ACCESS_TOKEN"

    def test_no_secret_value_leaks_into_the_classification(self):
        exc = SoundCloudAuthError(["refresh grant rejected: invalid_grant"])
        blob = str(cause_dict := classify_failure(exc, "upload_soundcloud").to_dict())
        assert "invalid_grant" in blob
        # Credential is named, never valued.
        assert cause_dict["credential"].isupper() or "/" in cause_dict["credential"]


class TestOtherKinds:
    def test_plain_timeout_stays_a_network_failure(self):
        cause = classify_failure(
            TimeoutError("Page.wait_for_selector: Timeout 10000ms exceeded"),
            "upload_soundcloud",
        )
        assert cause.kind == "network"
        assert cause.retryable is True

    def test_missing_file_is_not_retryable(self):
        cause = classify_failure(FileNotFoundError("/watch/audio/x.flac"), "upload_youtube")
        assert cause.kind == "missing_file"
        assert cause.retryable is False

    def test_quota_is_retryable(self):
        cause = classify_failure(RuntimeError("quotaExceeded"), "upload_youtube")
        assert cause.kind == "quota"
        assert cause.retryable is True

    def test_unknown_is_honest_not_optimistic(self):
        cause = classify_failure(ValueError("something odd"), "analyze")
        assert cause.kind == "unknown"
        assert "something odd" in cause.summary

    @pytest.mark.parametrize(
        "step,expected",
        [
            ("upload_soundcloud", "soundcloud"),
            ("verify_youtube", "youtube"),
            ("upload_mixcloud", "mixcloud"),
            ("analyze", None),
        ],
    )
    def test_platform_for_step(self, step, expected):
        assert platform_for_step(step) == expected


class TestEmailBody:
    def test_failure_email_names_cause_platform_and_credential(self):
        from app.services.notification_service import NotificationService

        cause = classify_failure(
            SoundCloudAuthError(["refresh grant rejected: invalid_grant"]),
            "upload_soundcloud",
        )
        payload = {
            "type": "platform_failed",
            "title": NotificationService._failure_title(
                {"platform": "soundcloud"}, cause.to_dict()
            ),
            "message": "soundcloud did not publish",
            "data": {"step": "upload_soundcloud", "cause": cause.to_dict()},
            "timestamp": "now",
        }
        html = NotificationService()._build_email_html(payload)

        assert "Authorization failed — soundcloud" in payload["title"]
        assert "invalid_grant" in html
        assert "SOUNDCLOUD_REFRESH_TOKEN" in html
        assert "operator action required" in html
        # The old misdiagnosis must not be what the reader sees.
        assert "wait_for_selector" not in html
