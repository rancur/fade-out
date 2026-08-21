"""The freshness check must not turn "I cannot see the repo" into "up to date".

GitHub answers **404** — not 403 — both for a repo with no releases and for a
repo the caller has no access to, so a 404 is only an answer once we know the
credential can actually see the repository. ``rancur/fade-out`` is public now,
so its own check needs no credential, but ``GITHUB_REPO`` is configurable and
the private case is the one that goes wrong quietly.

Three failures are covered here, in the order they were found:

1. No credential at all: the container was never handed ``GITHUB_TOKEN``, so
   the check could only ever say "cannot determine". It must keep saying that
   rather than guessing.
2. A credential that authenticates but cannot see *this* repo. Reading its 404
   as "no releases published" marks the deployment healthy on the strength of a
   token that is doing nothing — a false green wearing a credential.
3. A credential that is expired or rejected outright (401/403). Also unknown,
   never current.

Only case 4 — a credential that demonstrably reads the repo — is allowed to
produce a comparison, and it must produce the right one in both directions.
"""

import httpx
import pytest

from app.services import upgrade_service


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _client_factory(handler):
    """Hand ``refresh_deployment_status`` a client wired to ``handler``.

    Bound to the real class at import time — the factory replaces
    ``httpx.AsyncClient`` in the module under test, so constructing through the
    patched name would recurse into itself.
    """

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return _factory


@pytest.fixture(autouse=True)
def _fresh_status(monkeypatch):
    """Isolate the module-level cache and pin the running version."""
    monkeypatch.setattr(
        upgrade_service, "deployment_status", upgrade_service._DeploymentStatus()
    )
    monkeypatch.setattr(upgrade_service, "_get_current_version", lambda: "2.4.0")
    yield


def _set_token(monkeypatch, value):
    monkeypatch.setattr(upgrade_service.settings, "GITHUB_TOKEN", value)
    monkeypatch.setattr(upgrade_service.settings, "GITHUB_REPO", "rancur/fade-out")


class TestNoCredential:
    @pytest.mark.asyncio
    async def test_unauthenticated_404_is_unknown_not_current(self, monkeypatch):
        _set_token(monkeypatch, "")

        def handler(request):
            assert "Authorization" not in request.headers
            return httpx.Response(404, json={"message": "Not Found"})

        monkeypatch.setattr(
            upgrade_service.httpx, "AsyncClient", _client_factory(handler)
        )
        snap = await upgrade_service.refresh_deployment_status()

        assert snap["state"] == "error"
        assert snap["stale"] is None
        assert snap["healthy"] is False
        assert "GITHUB_TOKEN" in snap["error"]


class TestCredentialCannotSeeRepo:
    @pytest.mark.asyncio
    async def test_authenticated_404_on_an_invisible_repo_is_an_error(
        self, monkeypatch
    ):
        """The regression this file exists for.

        A valid token with no access to the repo 404s on both endpoints. Before
        the repo-visibility probe, the first 404 was recorded as "no releases
        published; nothing to compare against" — state ok, stale False,
        healthy True. A monitor reporting green because its credential is
        useless is worse than one reporting nothing.
        """
        _set_token(monkeypatch, "token-without-access")
        seen = []

        def handler(request):
            seen.append(request.url.path)
            assert request.headers["Authorization"] == "Bearer token-without-access"
            return httpx.Response(404, json={"message": "Not Found"})

        monkeypatch.setattr(
            upgrade_service.httpx, "AsyncClient", _client_factory(handler)
        )
        snap = await upgrade_service.refresh_deployment_status()

        assert seen == [
            "/repos/rancur/fade-out/releases/latest",
            "/repos/rancur/fade-out",
        ]
        assert snap["state"] == "error"
        assert snap["stale"] is None
        assert snap["healthy"] is False
        assert "cannot see rancur/fade-out" in snap["error"]

    @pytest.mark.asyncio
    async def test_visible_repo_with_no_releases_is_still_a_known_answer(
        self, monkeypatch
    ):
        """The probe must not break the legitimate "no releases yet" case."""
        _set_token(monkeypatch, "good-token")

        def handler(request):
            if request.url.path.endswith("/releases/latest"):
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"full_name": "rancur/fade-out"})

        monkeypatch.setattr(
            upgrade_service.httpx, "AsyncClient", _client_factory(handler)
        )
        snap = await upgrade_service.refresh_deployment_status()

        assert snap["state"] == "ok"
        assert snap["stale"] is False
        assert snap["healthy"] is True
        assert "no releases" in snap["comparison"]


class TestCredentialRejected:
    @pytest.mark.parametrize("code", [401, 403])
    @pytest.mark.asyncio
    async def test_rejected_credential_is_unknown_not_current(
        self, monkeypatch, code
    ):
        _set_token(monkeypatch, "expired-token")

        def handler(request):
            return httpx.Response(code, json={"message": "Bad credentials"})

        monkeypatch.setattr(
            upgrade_service.httpx, "AsyncClient", _client_factory(handler)
        )
        snap = await upgrade_service.refresh_deployment_status()

        assert snap["state"] == "error"
        assert snap["stale"] is None
        assert snap["healthy"] is False
        assert str(code) in snap["error"]

    @pytest.mark.asyncio
    async def test_transport_failure_never_leaks_the_token(self, monkeypatch):
        _set_token(monkeypatch, "sekrit-token-value")

        def handler(request):
            raise httpx.ConnectError("failed connecting with sekrit-token-value")

        monkeypatch.setattr(
            upgrade_service.httpx, "AsyncClient", _client_factory(handler)
        )
        snap = await upgrade_service.refresh_deployment_status()

        assert snap["state"] == "error"
        assert "sekrit-token-value" not in snap["error"]
        assert "***" in snap["error"]


class TestWorkingCredential:
    @pytest.mark.asyncio
    async def test_current_build_reports_current(self, monkeypatch):
        _set_token(monkeypatch, "good-token")

        def handler(request):
            assert request.headers["Authorization"] == "Bearer good-token"
            return httpx.Response(200, json={"tag_name": "v2.4.0"})

        monkeypatch.setattr(
            upgrade_service.httpx, "AsyncClient", _client_factory(handler)
        )
        snap = await upgrade_service.refresh_deployment_status()

        assert snap["state"] == "ok"
        assert snap["latest_version"] == "v2.4.0"
        assert snap["stale"] is False
        assert snap["healthy"] is True
        assert snap["comparison"] == "compared against the latest GitHub release"

    @pytest.mark.asyncio
    async def test_older_build_reports_stale(self, monkeypatch):
        _set_token(monkeypatch, "good-token")
        monkeypatch.setattr(upgrade_service, "_get_current_version", lambda: "2.3.0")

        def handler(request):
            return httpx.Response(200, json={"tag_name": "v2.4.0"})

        monkeypatch.setattr(
            upgrade_service.httpx, "AsyncClient", _client_factory(handler)
        )
        snap = await upgrade_service.refresh_deployment_status()

        assert snap["state"] == "ok"
        assert snap["stale"] is True
        assert snap["behind_by"] == "2.3.0 -> v2.4.0"
        assert snap["healthy"] is False


class TestUpgradeServiceUsesTheSameCredential:
    @pytest.mark.asyncio
    async def test_check_for_update_authenticates(self, monkeypatch):
        """Without this the auto-upgrade loop silently never upgrades.

        On a private repo an unauthenticated ``releases/latest`` is always 404,
        which ``check_for_update`` reads as "no update available" — forever.
        """
        _set_token(monkeypatch, "good-token")
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"tag_name": "v9.9.9", "name": "next"})

        monkeypatch.setattr(
            upgrade_service.httpx, "AsyncClient", _client_factory(handler)
        )
        info = await upgrade_service.UpgradeService().check_for_update()

        assert seen["auth"] == "Bearer good-token"
        assert info is not None
        assert info["latest_version"] == "v9.9.9"
