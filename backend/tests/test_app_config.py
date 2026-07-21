"""Tests for the app-config service (DB-over-env resolution) and the
introspected settings API (schema + write-only-secret values endpoint)."""

import pytest

from app.config import settings as env_settings
from app.services import app_config


# ---------------------------------------------------------------------------
# Pure resolution / validation helpers
# ---------------------------------------------------------------------------

class TestValidation:
    def test_bool_rejects_non_bool(self):
        d = app_config.SCHEMA_BY_KEY["draft_mode"]
        with pytest.raises(ValueError):
            app_config.validate_value(d, "true")
        assert app_config.validate_value(d, False) is False

    def test_int_coercion_and_range(self):
        d = app_config.SCHEMA_BY_KEY["youtube_daily_quota_budget"]
        assert app_config.validate_value(d, "5000") == 5000
        assert app_config.validate_value(d, 5000.0) == 5000
        with pytest.raises(ValueError):
            app_config.validate_value(d, 10_001)  # above max
        with pytest.raises(ValueError):
            app_config.validate_value(d, -1)  # below min
        with pytest.raises(ValueError):
            app_config.validate_value(d, True)  # bool is not an int here
        with pytest.raises(ValueError):
            app_config.validate_value(d, "lots")

    def test_float_range(self):
        d = app_config.SCHEMA_BY_KEY["detection_name_confidence_threshold"]
        assert app_config.validate_value(d, 0.6) == 0.6
        with pytest.raises(ValueError):
            app_config.validate_value(d, 1.5)

    def test_enum_choices(self):
        d = app_config.SCHEMA_BY_KEY["premiere_mode"]
        assert app_config.validate_value(d, "unlisted") == "unlisted"
        with pytest.raises(ValueError):
            app_config.validate_value(d, "whenever")

    def test_youtube_publish_mode_schema(self):
        # New publish-mode enum: non-secret, editable, default scheduled.
        d = app_config.SCHEMA_BY_KEY["youtube_publish_mode"]
        assert d.type == "enum"
        assert d.category == "YouTube"
        assert d.editable is True
        assert d.choices == ("immediate", "scheduled")
        assert d.key not in app_config.SECRET_JSON_KEYS
        assert app_config.env_default(d) == "scheduled"
        assert app_config.validate_value(d, "immediate") == "immediate"
        assert app_config.validate_value(d, "scheduled") == "scheduled"
        # "premiere" is no longer offered: the Data API cannot create
        # Premieres, so the schema must reject it outright.
        for bad in ("premiere", "instant", "unlisted", "now", 3):
            with pytest.raises(ValueError):
                app_config.validate_value(d, bad)

    def test_secret_must_be_string(self):
        d = app_config.SCHEMA_BY_KEY["openai_api_key"]
        assert app_config.validate_value(d, "sk-abc") == "sk-abc"
        with pytest.raises(ValueError):
            app_config.validate_value(d, 123)


class TestSchemaCatalog:
    def test_every_setting_has_valid_category_and_type(self):
        for d in app_config.SETTINGS_SCHEMA:
            assert d.category in app_config.CATEGORIES
            assert d.type in {"str", "int", "float", "bool", "enum", "path", "secret"}

    def test_paths_are_display_only(self):
        for d in app_config.SETTINGS_SCHEMA:
            if d.type == "path":
                assert d.editable is False

    def test_secret_json_keys_cover_all_secrets(self):
        for d in app_config.SETTINGS_SCHEMA:
            if d.type == "secret":
                assert d.key in app_config.SECRET_JSON_KEYS
        # The notification SMTP password is protected too.
        assert "notification_email_smtp_password" in app_config.SECRET_JSON_KEYS

    def test_redact_settings_json_strips_secrets(self):
        sj = {
            "openai_api_key": "sk-secret",
            "soundcloud_password": "hunter2",
            "notification_email_smtp_password": "smtp-pass",
            "youtube_daily_quota_budget": 4000,
        }
        redacted = app_config.redact_settings_json(sj)
        assert redacted == {"youtube_daily_quota_budget": 4000}
        assert app_config.redact_settings_json(None) is None


# ---------------------------------------------------------------------------
# Cached resolve() against the real (test) DB
# ---------------------------------------------------------------------------

class TestResolve:
    async def test_env_fallback_without_row(self, prepared_db):
        value = await app_config.resolve("youtube_daily_quota_budget")
        assert value == env_settings.YOUTUBE_DAILY_QUOTA_BUDGET

    async def test_db_value_wins_and_cache_invalidation(self, prepared_db):
        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(
                AppSettings(id=1, settings_json={"youtube_daily_quota_budget": 1234})
            )
            await session.commit()

        app_config.invalidate_cache()
        assert await app_config.resolve("youtube_daily_quota_budget") == 1234

        # Update behind the cache: stale until invalidated.
        async with async_session_factory() as session:
            row = await session.get(AppSettings, 1)
            row.settings_json = {"youtube_daily_quota_budget": 777}
            await session.commit()

        assert await app_config.resolve("youtube_daily_quota_budget") == 1234
        app_config.invalidate_cache()
        assert await app_config.resolve("youtube_daily_quota_budget") == 777

    async def test_shorts_daily_upload_cap_in_schema_and_roundtrips(
        self, prepared_db
    ):
        # Runtime-editable (env-only before): int, non-secret, env fallback 3.
        d = app_config.SCHEMA_BY_KEY["shorts_daily_upload_cap"]
        assert d.type == "int"
        assert d.env_attr == "SHORTS_DAILY_UPLOAD_CAP"
        assert app_config.validate_value(d, "5") == 5

        assert (
            await app_config.resolve("shorts_daily_upload_cap")
            == env_settings.SHORTS_DAILY_UPLOAD_CAP
        )

        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(
                AppSettings(id=1, settings_json={"shorts_daily_upload_cap": 7})
            )
            await session.commit()

        app_config.invalidate_cache()
        assert await app_config.resolve("shorts_daily_upload_cap") == 7

    async def test_youtube_publish_mode_defaults_and_roundtrips(
        self, prepared_db
    ):
        # No row: schema default (scheduled — current behavior).
        assert await app_config.resolve("youtube_publish_mode") == "scheduled"

        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(
                AppSettings(id=1, settings_json={"youtube_publish_mode": "immediate"})
            )
            await session.commit()

        app_config.invalidate_cache()
        assert await app_config.resolve("youtube_publish_mode") == "immediate"

    async def test_backfill_auto_resume_in_schema_and_roundtrips(
        self, prepared_db
    ):
        # Boot auto-resume gate for the catalog tracklist backfill: bool,
        # non-secret, default ON (no env attr — static fallback).
        d = app_config.SCHEMA_BY_KEY["backfill_auto_resume"]
        assert d.type == "bool"
        assert d.category == "Advanced"
        assert d.editable is True
        assert d.key not in app_config.SECRET_JSON_KEYS
        assert app_config.env_default(d) is True
        assert app_config.validate_value(d, False) is False
        with pytest.raises(ValueError):
            app_config.validate_value(d, "false")

        # Default resolves True without any DB row...
        assert await app_config.resolve("backfill_auto_resume") is True

        # ...and a DB-set False wins.
        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(
                AppSettings(id=1, settings_json={"backfill_auto_resume": False})
            )
            await session.commit()

        app_config.invalidate_cache()
        assert await app_config.resolve("backfill_auto_resume") is False

    async def test_video_wait_max_checks_in_schema_and_roundtrips(
        self, prepared_db
    ):
        # Wait ceiling for the upload step's video-sync polling: int,
        # non-secret, default 96 (8 hours of 5-minute checks).
        d = app_config.SCHEMA_BY_KEY["video_wait_max_checks"]
        assert d.type == "int"
        assert d.category == "Pipeline"
        assert d.key not in app_config.SECRET_JSON_KEYS
        assert app_config.env_default(d) == 96
        assert app_config.validate_value(d, "12") == 12
        with pytest.raises(ValueError):
            app_config.validate_value(d, 0)  # below min

        assert await app_config.resolve("video_wait_max_checks") == 96

        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(
                AppSettings(id=1, settings_json={"video_wait_max_checks": 288})
            )
            await session.commit()

        app_config.invalidate_cache()
        assert await app_config.resolve("video_wait_max_checks") == 288

    async def test_column_backed_resolution(self, prepared_db):
        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(AppSettings(id=1, draft_mode=False, premiere_mode="unlisted"))
            await session.commit()

        app_config.invalidate_cache()
        assert await app_config.resolve("draft_mode") is False
        assert await app_config.resolve("premiere_mode") == "unlisted"

    async def test_get_all_returns_every_key(self, prepared_db):
        values = await app_config.get_all()
        assert set(values) == set(app_config.SCHEMA_BY_KEY)


# ---------------------------------------------------------------------------
# HTTP API: /api/settings/schema + /api/settings/values
# ---------------------------------------------------------------------------

class TestSettingsSchemaEndpoint:
    async def test_schema_lists_catalog_with_categories(self, client):
        resp = await client.get("/api/settings/schema")
        assert resp.status_code == 200
        body = resp.json()
        assert body["categories"] == list(app_config.CATEGORIES)
        keys = {s["key"] for s in body["settings"]}
        assert "draft_mode" in keys
        assert "watch_audio_path" in keys
        assert "openai_api_key" in keys

    async def test_paths_are_effective_and_not_editable(self, client):
        resp = await client.get("/api/settings/schema")
        by_key = {s["key"]: s for s in resp.json()["settings"]}
        audio = by_key["watch_audio_path"]
        assert audio["editable"] is False
        assert audio["value"] == env_settings.WATCH_AUDIO_PATH
        assert "mount" in audio["help"].lower()

    async def test_secrets_expose_only_has_value(self, client, monkeypatch):
        monkeypatch.setattr(env_settings, "OPENAI_API_KEY", "sk-env-secret")
        resp = await client.get("/api/settings/schema")
        by_key = {s["key"]: s for s in resp.json()["settings"]}
        secret = by_key["openai_api_key"]
        assert secret["type"] == "secret"
        assert secret["value"] is None
        assert secret["default"] is None
        assert secret["has_value"] is True
        assert "sk-env-secret" not in resp.text


class TestSettingsValuesEndpoint:
    async def test_partial_update_and_resolution(self, client):
        resp = await client.put(
            "/api/settings/values",
            json={"values": {"draft_mode": False, "youtube_daily_quota_budget": 4000}},
        )
        assert resp.status_code == 200
        by_key = {s["key"]: s for s in resp.json()["settings"]}
        assert by_key["draft_mode"]["value"] is False
        assert by_key["youtube_daily_quota_budget"]["value"] == 4000
        assert by_key["youtube_daily_quota_budget"]["source"] == "db"

        # The cached service-side resolver sees it immediately.
        assert await app_config.resolve("draft_mode") is False
        assert await app_config.resolve("youtube_daily_quota_budget") == 4000

    async def test_unknown_key_rejected(self, client):
        resp = await client.put(
            "/api/settings/values", json={"values": {"nope": 1}}
        )
        assert resp.status_code == 400

    async def test_non_editable_rejected(self, client):
        resp = await client.put(
            "/api/settings/values", json={"values": {"watch_audio_path": "/x"}}
        )
        assert resp.status_code == 400
        assert "not editable" in resp.json()["detail"]

    async def test_type_validation_errors(self, client):
        resp = await client.put(
            "/api/settings/values", json={"values": {"premiere_mode": "whenever"}}
        )
        assert resp.status_code == 422

        resp = await client.put(
            "/api/settings/values", json={"values": {"premiere_hour_utc": 99}}
        )
        assert resp.status_code == 422

        resp = await client.put(
            "/api/settings/values", json={"values": {"draft_mode": "yes"}}
        )
        assert resp.status_code == 422

    async def test_empty_body_rejected(self, client):
        resp = await client.put("/api/settings/values", json={"values": {}})
        assert resp.status_code == 422

    async def test_secret_write_only_roundtrip(self, client):
        # Write a secret...
        resp = await client.put(
            "/api/settings/values",
            json={"values": {"openai_api_key": "sk-super-secret"}},
        )
        assert resp.status_code == 200
        assert "sk-super-secret" not in resp.text
        by_key = {s["key"]: s for s in resp.json()["settings"]}
        assert by_key["openai_api_key"]["has_value"] is True
        assert by_key["openai_api_key"]["value"] is None

        # ...it is stored in the DB...
        assert await app_config.resolve("openai_api_key", force_refresh=True) == (
            "sk-super-secret"
        )

        # ...and NO settings endpoint ever returns it.
        for url in ("/api/settings", "/api/settings/schema"):
            got = await client.get(url)
            assert got.status_code == 200
            assert "sk-super-secret" not in got.text

    async def test_secret_cleared_with_empty_string(self, client, monkeypatch):
        monkeypatch.setattr(env_settings, "FAL_API_KEY", "")
        await client.put(
            "/api/settings/values", json={"values": {"fal_api_key": "fal-123"}}
        )
        resp = await client.put(
            "/api/settings/values", json={"values": {"fal_api_key": ""}}
        )
        by_key = {s["key"]: s for s in resp.json()["settings"]}
        assert by_key["fal_api_key"]["has_value"] is False

    async def test_null_clears_db_override(self, client):
        await client.put(
            "/api/settings/values",
            json={"values": {"youtube_daily_quota_budget": 111}},
        )
        resp = await client.put(
            "/api/settings/values",
            json={"values": {"youtube_daily_quota_budget": None}},
        )
        by_key = {s["key"]: s for s in resp.json()["settings"]}
        assert (
            by_key["youtube_daily_quota_budget"]["value"]
            == env_settings.YOUTUBE_DAILY_QUOTA_BUDGET
        )

    async def test_model_settings_mirrored_for_generators(self, client):
        """llm/image model writes land in settings_json so the generators
        (which only receive settings_json) pick them up."""
        resp = await client.put(
            "/api/settings/values",
            json={"values": {"llm_model": "gpt-5.2", "image_gen_model": "fal-ai/foo"}},
        )
        assert resp.status_code == 200

        legacy = (await client.get("/api/settings")).json()
        assert legacy["llm_model"] == "gpt-5.2"
        assert legacy["settings_json"]["llm_model"] == "gpt-5.2"
        assert legacy["settings_json"]["image_gen_model"] == "fal-ai/foo"


class TestLegacySettingsEndpointSecrecy:
    async def test_legacy_get_redacts_settings_json_secrets(self, client):
        await client.put(
            "/api/settings/values",
            json={"values": {"soundcloud_client_secret": "sc-shhh"}},
        )
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        assert "sc-shhh" not in resp.text
        sj = resp.json()["settings_json"]
        assert "soundcloud_client_secret" not in (sj or {})

    async def test_legacy_put_roundtrip_preserves_stored_secrets(self, client):
        """A client saving back the redacted settings_json must not wipe
        stored secrets (the old Settings page did exactly this)."""
        await client.put(
            "/api/settings/values",
            json={"values": {"openai_api_key": "sk-keepme"}},
        )
        got = (await client.get("/api/settings")).json()

        # Round-trip the redacted payload through the legacy PUT.
        resp = await client.put(
            "/api/settings",
            json={"draft_mode": True, "settings_json": got["settings_json"] or {}},
        )
        assert resp.status_code == 200

        assert await app_config.resolve("openai_api_key", force_refresh=True) == (
            "sk-keepme"
        )

    async def test_auth_credentials_endpoint_masks_values(self, client):
        await client.put(
            "/api/auth/credentials",
            json={"credentials": {"openai_api_key": "sk-proj-abcdefghijklmnop"}},
        )
        resp = await client.get("/api/auth/credentials")
        assert resp.status_code == 200
        assert "sk-proj-abcdefghijklmnop" not in resp.text
        cred = next(
            c for c in resp.json()["credentials"] if c["name"] == "openai_api_key"
        )
        assert cred["is_set"] is True
        assert cred["source"] == "db"
