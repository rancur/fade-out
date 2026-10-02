"""/api/health must assert real function, and go unhealthy when it isn't.

Before this, ``/api/health`` returned ``{"status":"ok"}`` no matter what — it
stayed green for the two days the SoundCloud grant was dead. A 200 that does
not assert the underlying condition is a false green.
"""

import time

import pytest

from app.services.platform_health import (
    DEAD,
    NOT_CONFIGURED,
    OK,
    UNKNOWN,
    CredentialState,
    PlatformHealth,
)


def _state(platform, state, **kw):
    return CredentialState(
        platform=platform, state=state, checked_monotonic=time.monotonic(), **kw
    )


@pytest.fixture
def healthy_service(monkeypatch):
    svc = PlatformHealth()
    svc._states = {
        "soundcloud": _state("soundcloud", OK, detail="token accepted"),
        "youtube": _state("youtube", OK, detail="refresh accepted"),
        "mixcloud": _state("mixcloud", NOT_CONFIGURED, detail="disabled"),
    }
    return svc


class TestOverallHealth:
    def test_all_good_is_ok(self, healthy_service):
        assert healthy_service.overall_ok() is True
        assert healthy_service.unhealthy() == {}

    def test_dead_credential_makes_it_unhealthy(self, healthy_service):
        healthy_service._states["soundcloud"] = _state(
            "soundcloud", DEAD,
            detail="refresh grant rejected: invalid_grant",
            credential="SOUNDCLOUD_REFRESH_TOKEN",
        )
        assert healthy_service.overall_ok() is False
        assert "soundcloud" in healthy_service.unhealthy()

    def test_unknown_never_renders_as_fine(self, healthy_service):
        healthy_service._states["youtube"] = _state(
            "youtube", UNKNOWN, detail="probe could not reach Google"
        )
        assert healthy_service.overall_ok() is False

    def test_stale_result_decays_to_unknown(self, healthy_service):
        """A probe that stopped running must not keep reporting its last OK."""
        healthy_service._states["youtube"].checked_monotonic = time.monotonic() - 99999
        snap = healthy_service.snapshot()
        assert snap["youtube"]["state"] == UNKNOWN
        assert snap["youtube"]["healthy"] is False
        assert healthy_service.overall_ok() is False


class TestProbes:
    async def test_soundcloud_probe_reports_dead_on_auth_error(
        self, prepared_db, monkeypatch
    ):
        from app.services import platform_health as ph
        import app.services.soundcloud_uploader as sc_mod

        async def boom(self):
            raise sc_mod.SoundCloudAuthError(["refresh grant rejected: invalid_grant"])

        monkeypatch.setattr(sc_mod.SoundCloudUploader, "_ensure_access_token", boom)
        svc = ph.PlatformHealth()
        state = await svc._probe_soundcloud({"soundcloud_access_token": "x"})

        assert state.state == DEAD
        assert "invalid_grant" in state.detail
        assert "SOUNDCLOUD" in state.credential

    async def test_soundcloud_probe_ok_when_token_accepted(self, prepared_db, monkeypatch):
        from app.services import platform_health as ph
        import app.services.soundcloud_uploader as sc_mod

        async def fine(self):
            return "token"

        monkeypatch.setattr(sc_mod.SoundCloudUploader, "_ensure_access_token", fine)
        svc = ph.PlatformHealth()
        state = await svc._probe_soundcloud({"soundcloud_access_token": "x"})
        assert state.state == OK

    async def test_unconfigured_platform_is_not_a_failure(self, prepared_db, monkeypatch):
        from app.config import settings
        from app.services import platform_health as ph

        monkeypatch.setattr(settings, "SOUNDCLOUD_ACCESS_TOKEN", "", raising=False)
        monkeypatch.setattr(settings, "SOUNDCLOUD_REFRESH_TOKEN", "", raising=False)
        monkeypatch.setattr(settings, "SOUNDCLOUD_EMAIL", "", raising=False)
        monkeypatch.setattr(settings, "SOUNDCLOUD_PASSWORD", "", raising=False)

        svc = ph.PlatformHealth()
        state = await svc._probe_soundcloud({})
        assert state.state == NOT_CONFIGURED

    async def test_youtube_probe_reports_dead_on_refresh_error(
        self, prepared_db, monkeypatch
    ):
        from app.config import settings
        from app.services import platform_health as ph
        import app.services.youtube_uploader as yt_mod

        monkeypatch.setattr(settings, "YOUTUBE_REFRESH_TOKEN", "rt", raising=False)

        def boom(self):
            raise yt_mod.YouTubeAuthError("refresh grant rejected: invalid_grant")

        monkeypatch.setattr(yt_mod.YouTubeUploader, "_get_credentials", boom)
        svc = ph.PlatformHealth()
        state = await svc._probe_youtube({})
        assert state.state == DEAD
        assert state.credential == "YOUTUBE_REFRESH_TOKEN"

    async def test_soundcloud_probe_reports_unknown_on_transient_failure(
        self, prepared_db, monkeypatch
    ):
        """2026-09-24 incident: a 504 from the token endpoint must be
        reported as unknown (does not page), not dead (pages a human)."""
        from app.services import platform_health as ph
        import app.services.soundcloud_uploader as sc_mod

        async def transient(self):
            raise sc_mod.SoundCloudTransientError(
                ["refresh_token grant rejected (504: Gateway Time-out)"]
            )

        monkeypatch.setattr(sc_mod.SoundCloudUploader, "_ensure_access_token", transient)
        svc = ph.PlatformHealth()
        state = await svc._probe_soundcloud({"soundcloud_access_token": "x"})

        assert state.state == UNKNOWN
        assert "inconclusive" in state.detail

    async def test_youtube_probe_reports_unknown_on_transient_refresh_error(
        self, prepared_db, monkeypatch
    ):
        from app.config import settings
        from app.services import platform_health as ph
        import app.services.youtube_uploader as yt_mod

        monkeypatch.setattr(settings, "YOUTUBE_REFRESH_TOKEN", "rt", raising=False)

        def boom(self):
            raise yt_mod.YouTubeTransientError("504: Gateway Time-out")

        monkeypatch.setattr(yt_mod.YouTubeUploader, "_get_credentials", boom)
        svc = ph.PlatformHealth()
        state = await svc._probe_youtube({})
        assert state.state == UNKNOWN
        assert "inconclusive" in state.detail


class TestHealthEndpoint:
    async def test_health_503s_when_a_credential_is_dead(self, client, monkeypatch):
        from app.services.platform_health import get_platform_health
        from app.services.upgrade_service import deployment_status

        deployment_status.record_success(None)  # deployment freshness: known-good

        svc = get_platform_health()
        svc._states = {
            "soundcloud": _state(
                "soundcloud", DEAD,
                detail="refresh grant rejected: invalid_grant",
                credential="SOUNDCLOUD_REFRESH_TOKEN",
            ),
            "youtube": _state("youtube", OK, detail="ok"),
            "mixcloud": _state("mixcloud", NOT_CONFIGURED, detail="disabled"),
        }

        resp = await client.get("/api/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["platforms"]["soundcloud"]["state"] == DEAD
        assert any("soundcloud" in p for p in body["problems"])

        # Liveness stays green: the process is fine, publishing is not.
        live = await client.get("/api/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "alive"

    async def test_unknown_deployment_is_a_warning_not_an_outage(self, client):
        from app.services import upgrade_service
        from app.services.platform_health import get_platform_health

        svc = get_platform_health()
        svc._states = {
            p: _state(p, OK, detail="ok") for p in ("soundcloud", "youtube", "mixcloud")
        }
        upgrade_service.deployment_status.record_error(
            "releases API returned 404 unauthenticated"
        )
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        # Reported honestly as unverified — never rewritten as "up to date".
        assert body["deployment"]["state"] == "error"
        assert body["deployment"]["stale"] is None
        assert body["deployment"]["latest_version"] is None
        assert body["warnings"]

    async def test_health_200s_when_everything_asserts(self, client):
        from app.services.platform_health import get_platform_health
        from app.services.upgrade_service import deployment_status

        deployment_status.record_success(None)
        svc = get_platform_health()
        svc._states = {
            "soundcloud": _state("soundcloud", OK, detail="ok"),
            "youtube": _state("youtube", OK, detail="ok"),
            "mixcloud": _state("mixcloud", NOT_CONFIGURED, detail="disabled"),
        }
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_health_reports_a_stale_deployment(self, client, monkeypatch):
        from app.services import upgrade_service
        from app.services.platform_health import get_platform_health

        svc = get_platform_health()
        svc._states = {
            p: _state(p, OK, detail="ok") for p in ("soundcloud", "youtube", "mixcloud")
        }
        monkeypatch.setattr(upgrade_service, "_get_current_version", lambda: "2.2.0")
        upgrade_service.deployment_status.record_success("v2.4.0")

        resp = await client.get("/api/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["deployment"]["stale"] is True
        assert any("deployment stale" in p for p in body["problems"])
