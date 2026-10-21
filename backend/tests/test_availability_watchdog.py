"""A mix that published but cannot be watched has to surface on its own."""

from datetime import datetime, timedelta, timezone

from app.database import async_session_factory
from app.models import Mix
from app.services.availability_watchdog import AvailabilityWatchdog

SANDWAVE = {"blocked": ["AS", "GU", "MP", "PR", "UM", "US", "VI"]}


async def _add_mix(mix_id, video_id, *, days_old=1):
    when = datetime.now(timezone.utc) - timedelta(days=days_old)
    async with async_session_factory() as session:
        session.add(Mix(
            id=mix_id, title=f"Mix {mix_id}", source="pipeline",
            pipeline_status="completed", created_at=when, pipeline_completed_at=when,
            youtube_video_id=video_id,
            youtube_url=f"https://www.youtube.com/watch?v={video_id}",
        ))
        await session.commit()


def _fake_youtube(monkeypatch, items=None, raises=None):
    calls = []

    class FakeUploader:
        def __init__(self, *a, **k):
            pass

        async def get_videos(self, ids):
            calls.append(list(ids))
            if raises:
                raise raises
            return {k: v for k, v in (items or {}).items() if k in ids}

    monkeypatch.setattr("app.services.youtube_uploader.YouTubeUploader", FakeUploader)
    return calls


def _video(restriction=None, upload_status="processed"):
    return {
        "status": {"uploadStatus": upload_status, "privacyStatus": "public"},
        "contentDetails": {"regionRestriction": restriction} if restriction else {},
    }


class TestDetection:
    async def test_home_region_block_is_reported(self, prepared_db, monkeypatch):
        await _add_mix("sandwave", "Xu_Dq2d7biQ")
        await _add_mix("dune", "J2SVLsf790s")
        _fake_youtube(monkeypatch, {
            "Xu_Dq2d7biQ": _video(SANDWAVE),
            "J2SVLsf790s": _video({"blocked": ["BY", "RU"]}),  # not home region
        })

        result = await AvailabilityWatchdog().check(alert=False)

        assert result["state"] == "ok"
        assert result["checked_count"] == 2
        assert [e["id"] for e in result["unavailable"]] == ["sandwave"]
        assert "blocked in US" in result["unavailable"][0]["reason"]

    async def test_removed_video_is_reported(self, prepared_db, monkeypatch):
        await _add_mix("gone", "deadbeef")
        _fake_youtube(monkeypatch, {})
        result = await AvailabilityWatchdog().check(alert=False)
        assert "not returned" in result["unavailable"][0]["reason"]

    async def test_rejected_upload_is_reported(self, prepared_db, monkeypatch):
        await _add_mix("rej", "rej1")
        _fake_youtube(monkeypatch, {"rej1": _video(upload_status="rejected")})
        result = await AvailabilityWatchdog().check(alert=False)
        assert "uploadStatus=rejected" in result["unavailable"][0]["reason"]

    async def test_old_mixes_are_outside_the_window(self, prepared_db, monkeypatch):
        await _add_mix("ancient", "old1", days_old=90)
        calls = _fake_youtube(monkeypatch, {})
        result = await AvailabilityWatchdog().check(alert=False)
        assert result["checked_count"] == 0
        assert calls == [[]]

    async def test_lookup_failure_is_unknown_not_all_clear(self, prepared_db, monkeypatch):
        await _add_mix("sandwave", "Xu_Dq2d7biQ")
        _fake_youtube(monkeypatch, raises=RuntimeError("quotaExceeded"))
        result = await AvailabilityWatchdog().check(alert=False)
        assert result["state"] == "unknown"
        assert "quotaExceeded" in result["detail"]

    async def test_never_run_reports_unknown(self):
        assert AvailabilityWatchdog().last_result["state"] == "unknown"


class TestReporting:
    async def test_alert_is_written_once_per_window(self, prepared_db, monkeypatch):
        from app.services import activity_log

        sent = []

        class _FakeNotifier:
            async def notify(self, ntype, **kw):
                sent.append((ntype, kw.get("title")))

        monkeypatch.setattr(
            "app.services.notification_service.get_notification_service",
            lambda: _FakeNotifier(),
        )
        await _add_mix("sandwave", "Xu_Dq2d7biQ")
        _fake_youtube(monkeypatch, {"Xu_Dq2d7biQ": _video(SANDWAVE)})

        watchdog = AvailabilityWatchdog()
        await watchdog.check(alert=True)
        await watchdog.check(alert=True)

        _items, total = await activity_log.query(
            limit=10, mix_id="sandwave", event="mix_unavailable"
        )
        assert total == 1, "a blocked video must not become an alert storm"
        assert sent == [("mix_unavailable", "Mix not watchable on YouTube: Mix sandwave")]


def test_mix_unavailable_is_a_known_enabled_error_notification():
    from app.services import notification_service as ns

    assert "mix_unavailable" in ns.NOTIFICATION_TYPES
    assert ns.DEFAULT_EVENT_TOGGLES["mix_unavailable"] is True
    assert ns.TYPE_LEVELS["mix_unavailable"] == "error"


class TestApi:
    async def test_unavailable_endpoint_refresh(self, client, monkeypatch):
        await _add_mix("sandwave", "Xu_Dq2d7biQ")
        _fake_youtube(monkeypatch, {"Xu_Dq2d7biQ": _video(SANDWAVE)})

        resp = await client.get("/api/system/unavailable", params={"refresh": "true"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "ok"
        assert body["unavailable"][0]["id"] == "sandwave"

    async def test_system_health_reports_unavailable_mixes(self, client, monkeypatch):
        await _add_mix("sandwave", "Xu_Dq2d7biQ")
        _fake_youtube(monkeypatch, {"Xu_Dq2d7biQ": _video(SANDWAVE)})
        from app.services.availability_watchdog import get_availability_watchdog

        await get_availability_watchdog().check(alert=False)
        body = (await client.get("/api/system/health")).json()

        section = body["unavailable_mixes"]
        assert section["state"] == "ok" and section["count"] == 1
        assert section["items"][0]["id"] == "sandwave"
