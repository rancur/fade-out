"""Version detection must make a stale deployment loud.

The old behaviour: ``_get_current_version()`` read a VERSION file the image
never contained and returned ``0.0.0``, while ``/api/upgrade/status`` reported
``update_available: false`` because no check had run. A container built in
July went unnoticed into the middle of August.
"""


from app.services import upgrade_service
from app.version import __version__, build_info


class TestCurrentVersion:
    def test_version_comes_from_the_code_not_a_missing_file(self, monkeypatch):
        monkeypatch.setattr(upgrade_service.os.path, "exists", lambda p: False)
        assert upgrade_service._get_current_version() == __version__
        assert upgrade_service._get_current_version() != "0.0.0"

    def test_build_info_reports_identity(self, monkeypatch):
        build_info.cache_clear()
        monkeypatch.setenv("BUILD_COMMIT", "abc1234")
        monkeypatch.setenv("BUILD_TIME", "2026-08-14T12:00:00Z")
        info = build_info()
        build_info.cache_clear()
        assert info["version"] == __version__
        assert info["commit"] == "abc1234"
        assert info["built_at"] == "2026-08-14T12:00:00Z"


class TestDeploymentStatus:
    def test_never_checked_is_unknown_not_up_to_date(self):
        status = upgrade_service._DeploymentStatus()
        snap = status.as_dict()
        assert snap["state"] == "unknown"
        assert snap["stale"] is None
        assert snap["healthy"] is False

    def test_failed_check_is_error_not_up_to_date(self):
        status = upgrade_service._DeploymentStatus()
        status.record_error("connection refused")
        snap = status.as_dict()
        assert snap["state"] == "error"
        assert snap["stale"] is None
        assert snap["healthy"] is False

    def test_behind_a_release_is_stale(self, monkeypatch):
        monkeypatch.setattr(upgrade_service, "_get_current_version", lambda: "2.2.0")
        status = upgrade_service._DeploymentStatus()
        status.record_success("v2.4.0")
        snap = status.as_dict()
        assert snap["stale"] is True
        assert snap["behind_by"] == "2.2.0 -> v2.4.0"
        assert snap["healthy"] is False

    def test_no_releases_is_a_known_answer_not_unknown(self, monkeypatch):
        monkeypatch.setattr(upgrade_service, "_get_current_version", lambda: "2.4.0")
        status = upgrade_service._DeploymentStatus()
        status.record_success(None)
        snap = status.as_dict()
        assert snap["state"] == "ok"
        assert snap["stale"] is False
        assert snap["healthy"] is True
        assert "no releases" in snap["comparison"]

    def test_current_is_healthy(self, monkeypatch):
        monkeypatch.setattr(upgrade_service, "_get_current_version", lambda: "2.4.0")
        status = upgrade_service._DeploymentStatus()
        status.record_success("v2.4.0")
        snap = status.as_dict()
        assert snap["stale"] is False
        assert snap["healthy"] is True

    def test_a_long_stale_success_decays_to_unknown(self, monkeypatch):
        monkeypatch.setattr(upgrade_service, "_get_current_version", lambda: "2.4.0")
        status = upgrade_service._DeploymentStatus()
        status.record_success("v2.4.0")
        status.checked_monotonic -= upgrade_service.DEPLOYMENT_STALE_CHECK_SECONDS + 60
        snap = status.as_dict()
        assert snap["state"] == "unknown"
        assert snap["healthy"] is False
