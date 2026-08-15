"""A mix that never published has to surface on its own."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sa_text

from app.database import async_session_factory
from app.models import Mix
from app.services.stuck_mix_watchdog import StuckMixWatchdog


async def _add_mix(mix_id, *, hours_old, status="failed", **fields):
    async with async_session_factory() as session:
        session.add(Mix(
            id=mix_id,
            title=f"Mix {mix_id}",
            source="pipeline",
            pipeline_status=status,
            created_at=datetime.now(timezone.utc) - timedelta(hours=hours_old),
            **fields,
        ))
        await session.commit()


class TestDetection:
    async def test_old_unpublished_mix_is_reported(self, prepared_db):
        await _add_mix("stuck-1", hours_old=30)
        result = await StuckMixWatchdog().check(alert=False)

        assert result["stuck_count"] == 1
        entry = result["stuck"][0]
        assert entry["id"] == "stuck-1"
        assert entry["kind"] == "unpublished"
        assert "never published" in entry["reason"]

    async def test_fresh_mix_is_left_alone(self, prepared_db):
        await _add_mix("fresh-1", hours_old=1, status="running")
        result = await StuckMixWatchdog().check(alert=False)
        assert result["stuck_count"] == 0

    async def test_partially_published_mix_is_reported(self, prepared_db):
        await _add_mix(
            "partial-1", hours_old=12, status="partial",
            youtube_url="https://youtube.com/watch?v=x",
        )
        result = await StuckMixWatchdog().check(alert=False)
        assert result["stuck_count"] == 1
        assert result["stuck"][0]["reason"] == "published to some targets but not all"

    async def test_completed_and_published_is_not_stuck(self, prepared_db):
        await _add_mix(
            "done-1", hours_old=200, status="completed",
            youtube_url="https://youtube.com/watch?v=x",
            soundcloud_url="https://soundcloud.com/x",
        )
        result = await StuckMixWatchdog().check(alert=False)
        assert result["stuck_count"] == 0

    async def test_completed_with_one_platform_unconfigured_is_not_stuck(
        self, prepared_db
    ):
        """SoundCloud-only setups must not be nagged about a missing YouTube URL."""
        await _add_mix(
            "sc-only", hours_old=200, status="completed",
            soundcloud_url="https://soundcloud.com/x",
        )
        result = await StuckMixWatchdog().check(alert=False)
        assert result["stuck_count"] == 0

    async def test_draft_gets_a_longer_but_finite_window(self, prepared_db):
        await _add_mix("draft-young", hours_old=10, status="draft_review")
        await _add_mix("draft-old", hours_old=100, status="draft_review")

        result = await StuckMixWatchdog().check(alert=False)
        ids = {e["id"] for e in result["stuck"]}
        assert ids == {"draft-old"}
        assert result["stuck"][0]["kind"] == "draft_unreviewed"

    async def test_missing_created_at_is_not_assumed_fresh(self, prepared_db):
        await _add_mix("no-ts", hours_old=1)
        # created_at carries a server default, so null it explicitly — the
        # case under test is a row whose age we genuinely cannot determine.
        async with async_session_factory() as session:
            await session.execute(
                sa_text("UPDATE mixes SET created_at = NULL WHERE id = 'no-ts'")
            )
            await session.commit()

        result = await StuckMixWatchdog().check(alert=False)
        assert {e["id"] for e in result["stuck"]} == {"no-ts"}
        assert result["stuck"][0]["age_hours"] is None


class TestReporting:
    async def test_never_run_reports_unknown_not_all_clear(self):
        watchdog = StuckMixWatchdog()
        assert watchdog.last_result["state"] == "unknown"
        assert watchdog.last_result["stuck"] == []

    async def test_alert_is_written_once_per_window(self, prepared_db, monkeypatch):
        from app.services import activity_log

        sent = []

        class _FakeNotifier:
            async def notify(self, *a, **kw):
                sent.append(kw.get("title"))

        monkeypatch.setattr(
            "app.services.notification_service.get_notification_service",
            lambda: _FakeNotifier(),
        )

        await _add_mix("stuck-2", hours_old=48)
        watchdog = StuckMixWatchdog()
        await watchdog.check(alert=True)
        await watchdog.check(alert=True)  # second sweep, same mix

        items, total = await activity_log.query(
            limit=10, mix_id="stuck-2", event="stuck_mix"
        )
        assert total == 1, "a stuck mix must not become an alert storm"
        assert len(sent) == 1
