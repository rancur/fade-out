"""Tests for notification config unification, event dispatch, and settings API (A4).

Covers: DB-over-env config precedence, the 60s config cache + invalidation,
orchestrator event → notification translation with per-event toggles and
min-level, the write-only SMTP password on the settings API, and the
per-channel test endpoint.
"""

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import AppSettings, Notification
from app.services.notification_service import (
    DEFAULT_EVENT_TOGGLES,
    NotificationService,
    resolve_config,
)


class TestResolveConfigPrecedence:
    def test_db_key_wins_over_env(self, monkeypatch):
        monkeypatch.setattr(
            settings, "NOTIFICATION_DISCORD_WEBHOOK_URL", "https://env.example/hook"
        )
        cfg = resolve_config({"notification_discord_webhook_url": "https://db.example/hook"})
        assert cfg["discord_webhook_url"] == "https://db.example/hook"

    def test_env_used_only_when_db_key_absent(self, monkeypatch):
        monkeypatch.setattr(
            settings, "NOTIFICATION_DISCORD_WEBHOOK_URL", "https://env.example/hook"
        )
        cfg = resolve_config({})
        assert cfg["discord_webhook_url"] == "https://env.example/hook"

    def test_explicit_db_none_disables_channel_despite_env(self, monkeypatch):
        monkeypatch.setattr(
            settings, "NOTIFICATION_DISCORD_WEBHOOK_URL", "https://env.example/hook"
        )
        cfg = resolve_config({"notification_discord_webhook_url": None})
        assert cfg["discord_webhook_url"] is None

    def test_empty_env_resolves_to_none(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFICATION_EMAIL_SMTP_HOST", "")
        cfg = resolve_config({})
        assert cfg["email_smtp_host"] is None

    def test_email_from_falls_back_to_smtp_user(self):
        cfg = resolve_config({"notification_email_smtp_user": "barry@x.com"})
        assert cfg["email_from"] == "barry@x.com"
        cfg2 = resolve_config({
            "notification_email_smtp_user": "barry@x.com",
            "notification_email_from": "noreply@x.com",
        })
        assert cfg2["email_from"] == "noreply@x.com"

    def test_webhook_urls_parsed_from_csv(self):
        cfg = resolve_config({"notification_webhook_urls": "https://a/1, https://b/2 ,"})
        assert cfg["webhook_urls"] == ["https://a/1", "https://b/2"]

    def test_event_toggles_merge_over_defaults(self):
        cfg = resolve_config({"notification_events": {"step_completed": True, "error": False}})
        assert cfg["events"]["step_completed"] is True
        assert cfg["events"]["error"] is False
        # untouched defaults preserved
        assert cfg["events"]["upload_complete"] is DEFAULT_EVENT_TOGGLES["upload_complete"]

    def test_invalid_min_level_falls_back_to_info(self):
        assert resolve_config({"notification_min_level": "verbose"})["min_level"] == "info"
        assert resolve_config({"notification_min_level": "error"})["min_level"] == "error"


class TestConfigCache:
    async def test_reads_db_and_caches_for_60s(self, prepared_db):
        from app.database import async_session_factory

        async with async_session_factory() as session:
            session.add(AppSettings(id=1, settings_json={
                "notification_discord_webhook_url": "https://db.example/hook-v1",
            }))
            await session.commit()

        svc = NotificationService()
        cfg = await svc.get_config()
        assert cfg["discord_webhook_url"] == "https://db.example/hook-v1"

        # Change the DB; the cached value must persist until invalidated.
        async with async_session_factory() as session:
            row = await session.get(AppSettings, 1)
            row.settings_json = {
                "notification_discord_webhook_url": "https://db.example/hook-v2",
            }
            await session.commit()

        assert (await svc.get_config())["discord_webhook_url"] == "https://db.example/hook-v1"

        svc.invalidate_config_cache()
        assert (await svc.get_config())["discord_webhook_url"] == "https://db.example/hook-v2"


def _svc_with_captured_sends(monkeypatch, cfg_overrides=None):
    """A NotificationService with a fixed config and captured Discord sends."""
    svc = NotificationService()
    cfg = resolve_config({})
    cfg["discord_webhook_url"] = "https://discord.example/hook"
    cfg.update(cfg_overrides or {})

    async def fake_get_config(force_refresh=False):
        return cfg

    sent = []

    async def fake_send_discord(payload, config):
        sent.append(payload)
        return True

    monkeypatch.setattr(svc, "get_config", fake_get_config)
    monkeypatch.setattr(svc, "_send_discord", fake_send_discord)
    return svc, sent


class TestEventDispatch:
    async def test_orchestrator_events_translate_to_notifications(self, monkeypatch):
        svc = NotificationService()
        queued = []

        async def fake_notify(ntype, mix_id=None, title="", message="", data=None):
            queued.append((ntype, mix_id, message))

        monkeypatch.setattr(svc, "notify", fake_notify)

        await svc.handle_orchestrator_event("pipeline_started", "m1", {})
        await svc.handle_orchestrator_event(
            "step_completed", "m1", {"step": "analyze", "elapsed_seconds": 4.2}
        )
        await svc.handle_orchestrator_event("upload_complete", "m1", {})
        await svc.handle_orchestrator_event("error", "m1", {"step": "upload_youtube"})
        await svc.handle_orchestrator_event("draft_ready", "m1", {})
        await svc.handle_orchestrator_event("step_progress", "m1", {"step": "x", "progress": 5})

        types = [q[0] for q in queued]
        assert types == [
            "pipeline_started", "step_completed", "upload_complete", "error", "draft_ready",
        ]
        assert "analyze" in queued[1][2] and "4.2s" in queued[1][2]
        assert "upload_youtube" in queued[3][2]

    async def test_default_toggles_mute_per_step_chatter(self, monkeypatch):
        svc, sent = _svc_with_captured_sends(monkeypatch)

        await svc._send_all_channels({"type": "step_completed", "mix_id": "m1",
                                      "title": "x", "message": "x", "data": {}})
        await svc._send_all_channels({"type": "pipeline_started", "mix_id": "m1",
                                      "title": "x", "message": "x", "data": {}})
        assert sent == []

        await svc._send_all_channels({"type": "upload_complete", "mix_id": "m1",
                                      "title": "x", "message": "x", "data": {}})
        assert len(sent) == 1

    async def test_explicit_toggle_overrides_default(self, monkeypatch):
        events = dict(DEFAULT_EVENT_TOGGLES)
        events["step_completed"] = True
        events["error"] = False
        svc, sent = _svc_with_captured_sends(monkeypatch, {"events": events})

        await svc._send_all_channels({"type": "step_completed", "mix_id": "m1",
                                      "title": "x", "message": "x", "data": {}})
        assert len(sent) == 1

        await svc._send_all_channels({"type": "error", "mix_id": "m1",
                                      "title": "x", "message": "x", "data": {}})
        assert len(sent) == 1  # error muted

    async def test_min_level_error_suppresses_info_notifications(self, monkeypatch):
        svc, sent = _svc_with_captured_sends(monkeypatch, {"min_level": "error"})

        await svc._send_all_channels({"type": "upload_complete", "mix_id": "m1",
                                      "title": "x", "message": "x", "data": {}})
        assert sent == []

        await svc._send_all_channels({"type": "error", "mix_id": "m1",
                                      "title": "x", "message": "x", "data": {}})
        assert len(sent) == 1


class TestDeliveryRecording:
    async def test_delivery_attempt_recorded_in_history_and_activity(self, prepared_db):
        from app.database import async_session_factory
        from app.services import activity_log

        svc = NotificationService()
        await svc._record("m1", "upload_complete", "discord", "Mix done", sent=True)
        await svc._record("m1", "error", "email", "Mix failed", sent=False, detail="boom")

        async with async_session_factory() as session:
            rows = (await session.execute(select(Notification))).scalars().all()
        assert len(rows) == 2
        ok = next(r for r in rows if r.channel == "discord")
        failed = next(r for r in rows if r.channel == "email")
        assert ok.sent is True and ok.sent_at is not None
        assert failed.sent is False and failed.sent_at is None

        sent_items, sent_total = await activity_log.query(event="notification_sent")
        failed_items, failed_total = await activity_log.query(event="notification_failed")
        assert sent_total == 1 and failed_total == 1
        assert "discord" in sent_items[0]["message"]
        assert "boom" in failed_items[0]["message"]


class TestSettingsAPI:
    async def test_roundtrip_with_write_only_password(self, client):
        put = await client.put("/api/notifications/settings", json={
            "discord_webhook_url": "https://discord.example/hook",
            "email_smtp_host": "smtp.seer.example",
            "email_smtp_port": 587,
            "email_smtp_user": "will@seer.example",
            "email_smtp_password": "hunter2",
            "email_from": "fadeout@seer.example",
            "email_to": "flash@willcurran.com",
            "email_smtp_secure": True,
            "webhook_urls": "https://hooks.example/a",
            "events": {"step_completed": True},
            "min_level": "info",
        })
        assert put.status_code == 200
        body = put.json()
        assert body["discord_webhook_url"] == "https://discord.example/hook"
        assert body["email_smtp_host"] == "smtp.seer.example"
        assert body["email_from"] == "fadeout@seer.example"
        assert body["has_password"] is True
        assert "hunter2" not in put.text
        assert "email_smtp_password" not in body
        assert body["events"]["step_completed"] is True
        assert body["events"]["upload_complete"] is True  # default preserved
        assert body["min_level"] == "info"

        got = await client.get("/api/notifications/settings")
        gbody = got.json()
        assert gbody["has_password"] is True
        assert "hunter2" not in got.text

    async def test_partial_update_keeps_stored_password(self, client):
        await client.put("/api/notifications/settings", json={
            "email_smtp_password": "hunter2",
        })
        # Update an unrelated field without resending the password.
        resp = await client.put("/api/notifications/settings", json={
            "email_to": "flash@willcurran.com",
        })
        assert resp.json()["has_password"] is True
        assert resp.json()["email_to"] == "flash@willcurran.com"

    async def test_invalid_min_level_rejected(self, client):
        resp = await client.put("/api/notifications/settings", json={"min_level": "loud"})
        assert resp.status_code == 422

    async def test_settings_persist_into_service_config(self, client):
        from app.services.notification_service import get_notification_service

        await client.put("/api/notifications/settings", json={
            "discord_webhook_url": "https://discord.example/service-hook",
        })
        svc = get_notification_service()
        cfg = await svc.get_config(force_refresh=True)
        assert cfg["discord_webhook_url"] == "https://discord.example/service-hook"


class TestTestEndpoint:
    async def test_per_channel_endpoint_reports_unconfigured(self, client):
        resp = await client.post("/api/notifications/test/discord")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["channel"] == "discord"
        assert "not configured" in body["detail"].lower()

    async def test_per_channel_endpoint_invalid_channel_400(self, client):
        resp = await client.post("/api/notifications/test/carrier-pigeon")
        assert resp.status_code == 400

    async def test_per_channel_endpoint_uses_saved_settings(self, client, monkeypatch):
        from app.services.notification_service import get_notification_service

        await client.put("/api/notifications/settings", json={
            "discord_webhook_url": "https://discord.example/hook",
        })

        svc = get_notification_service()
        sent = []

        async def fake_send_discord(payload, cfg):
            sent.append((payload, cfg["discord_webhook_url"]))
            return True

        monkeypatch.setattr(svc, "_send_discord", fake_send_discord)

        resp = await client.post("/api/notifications/test/discord")
        assert resp.json()["success"] is True
        assert sent[0][1] == "https://discord.example/hook"
        assert sent[0][0]["type"] == "test"

    async def test_legacy_body_endpoint_still_works(self, client):
        resp = await client.post(
            "/api/notifications/test", json={"channel": "webhook"}
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is False  # nothing configured
