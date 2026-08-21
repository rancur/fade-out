"""Async HTTP-level tests for the real FastAPI endpoints.

These drive the actual ASGI app through httpx.AsyncClient (ASGITransport) with a
fresh SQLite schema per test (``client`` fixture in conftest). The orchestrator's
pipeline launch methods are stubbed, so these assert the HTTP + DB behavior of
the routers, not the background pipeline.
"""



class TestHealth:
    async def test_liveness_is_always_up_when_the_process_is(self, client):
        resp = await client.get("/api/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

    async def test_health_ok_when_nothing_is_broken(self, client):
        """/api/health asserts function, so the test has to give it a
        verifiable world: no platform configured (nothing to assert) and a
        deployment check that has actually run."""
        from app.services.platform_health import get_platform_health
        from app.services.upgrade_service import deployment_status

        deployment_status.record_success(None)
        for state in get_platform_health()._states.values():
            state.state = "not_configured"
            state.detail = "not configured in tests"

        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["problems"] == []
        assert "version" in body

    async def test_unverifiable_deployment_warns_but_does_not_503(self, client):
        """An unreachable GitHub is stated out loud, not treated as fine —
        and not treated as an outage of a service that can still publish."""
        from app.services.platform_health import get_platform_health
        from app.services.upgrade_service import deployment_status

        deployment_status.record_error("connection refused")
        for state in get_platform_health()._states.values():
            state.state = "not_configured"
            state.detail = "not configured in tests"

        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deployment"]["state"] == "error"
        assert body["deployment"]["stale"] is None
        assert any("deployment freshness" in w for w in body["warnings"])


class TestMixesCrud:
    async def test_create_and_get_mix(self, client):
        resp = await client.post("/api/mixes", json={"title": "Test Mix"})
        assert resp.status_code == 201
        created = resp.json()
        assert created["title"] == "Test Mix"
        assert created["pipeline_status"] == "pending"
        mix_id = created["id"]

        # mixcloud_url is part of the schema and serialized (default None)
        assert "mixcloud_url" in created
        assert created["mixcloud_url"] is None

        detail = await client.get(f"/api/mixes/{mix_id}")
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["id"] == mix_id
        # Detail view includes the initialized pipeline steps.
        step_names = {s["step_name"] for s in detail_body["steps"]}
        assert "upload_mixcloud" in step_names
        assert "verify_mixcloud" in step_names
        assert "cross_link" in step_names

    async def test_get_missing_mix_404(self, client):
        resp = await client.get("/api/mixes/does-not-exist")
        assert resp.status_code == 404

    async def test_list_mixes_pagination_and_filter(self, client):
        for i in range(3):
            await client.post("/api/mixes", json={"title": f"Mix {i}"})

        resp = await client.get("/api/mixes", params={"page": 1, "page_size": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert body["page_size"] == 2
        assert len(body["items"]) == 2

        # Filter by a status that no mix has.
        filtered = await client.get("/api/mixes", params={"status": "completed"})
        assert filtered.json()["total"] == 0

    async def test_update_mix(self, client):
        created = (await client.post("/api/mixes", json={"title": "Before"})).json()
        mix_id = created["id"]

        resp = await client.put(
            f"/api/mixes/{mix_id}",
            json={"title": "After", "genres": ["house", "techno"]},
        )
        assert resp.status_code == 200
        updated = resp.json()
        assert updated["title"] == "After"
        assert updated["genres"] == ["house", "techno"]

    async def test_update_missing_mix_404(self, client):
        resp = await client.put("/api/mixes/nope", json={"title": "X"})
        assert resp.status_code == 404

    async def test_delete_mix(self, client):
        created = (await client.post("/api/mixes", json={"title": "Doomed"})).json()
        mix_id = created["id"]

        resp = await client.delete(f"/api/mixes/{mix_id}")
        assert resp.status_code == 204

        # Now gone.
        assert (await client.get(f"/api/mixes/{mix_id}")).status_code == 404

    async def test_delete_missing_mix_404(self, client):
        resp = await client.delete("/api/mixes/nope")
        assert resp.status_code == 404


class TestMixStateTransitions:
    async def test_approve_requires_draft_review(self, client):
        created = (await client.post("/api/mixes", json={"title": "Fresh"})).json()
        mix_id = created["id"]
        # Newly created mix is "pending", not "draft_review".
        resp = await client.post(f"/api/mixes/{mix_id}/approve")
        assert resp.status_code == 400

    async def test_retry_requires_failed(self, client):
        created = (await client.post("/api/mixes", json={"title": "Fresh"})).json()
        mix_id = created["id"]
        resp = await client.post(f"/api/mixes/{mix_id}/retry")
        assert resp.status_code == 400

    async def test_retry_step_missing_step_404(self, client):
        created = (await client.post("/api/mixes", json={"title": "Fresh"})).json()
        mix_id = created["id"]
        resp = await client.post(f"/api/mixes/{mix_id}/retry-step/not_a_step")
        assert resp.status_code == 404

    async def test_retry_step_returns_before_step_completes(self, client, monkeypatch):
        # The endpoint must commit its own txn and run the step in the
        # background: awaiting the orchestrator inline self-deadlocked sqlite
        # (the request held an uncommitted write txn while _execute_step wrote
        # from its own session) and pinned multi-GB uploads inside the request.
        import asyncio

        from app.main import orchestrator

        started = asyncio.Event()
        release = asyncio.Event()
        calls = {}

        async def slow_retry(mix_id, step_name):
            calls["args"] = (mix_id, step_name)
            started.set()
            await release.wait()

        monkeypatch.setattr(orchestrator, "retry_step", slow_retry)

        created = (await client.post("/api/mixes", json={"title": "Fresh"})).json()
        mix_id = created["id"]

        resp = await client.post(f"/api/mixes/{mix_id}/retry-step/analyze")
        # Responds while the (still-running) step is blocked on `release`.
        assert resp.status_code == 200
        assert resp.json()["id"] == mix_id

        await asyncio.wait_for(started.wait(), timeout=5)
        assert calls["args"] == (mix_id, "analyze")
        release.set()


class TestMixesSorting:
    async def _seed(self):
        from datetime import datetime

        from app.database import async_session_factory
        from app.models import Mix

        rows = [
            ("m-old", "Zulu Set", datetime(2025, 1, 1, 12, 0, 0)),
            ("m-mid", "alpha set", datetime(2025, 2, 1, 12, 0, 0)),
            ("m-new", "Mango Set", datetime(2025, 3, 1, 12, 0, 0)),
        ]
        async with async_session_factory() as session:
            for mix_id, title, created in rows:
                session.add(
                    Mix(id=mix_id, title=title, created_at=created,
                        pipeline_status="uploaded")
                )
            await session.commit()

    async def _ids(self, client, **params):
        resp = await client.get("/api/mixes", params=params)
        assert resp.status_code == 200
        return [i["id"] for i in resp.json()["items"]]

    async def test_default_is_newest_created_first(self, client):
        await self._seed()
        assert await self._ids(client) == ["m-new", "m-mid", "m-old"]
        assert await self._ids(client, sort="newest") == ["m-new", "m-mid", "m-old"]

    async def test_oldest_and_title(self, client):
        await self._seed()
        assert await self._ids(client, sort="oldest") == ["m-old", "m-mid", "m-new"]
        # case-insensitive title sort: alpha < Mango < Zulu
        assert await self._ids(client, sort="title") == ["m-mid", "m-new", "m-old"]

    async def test_sort_applies_before_pagination(self, client):
        await self._seed()
        page1 = await self._ids(client, sort="newest", page=1, page_size=2)
        page2 = await self._ids(client, sort="newest", page=2, page_size=2)
        assert page1 == ["m-new", "m-mid"]
        assert page2 == ["m-old"]

    async def test_invalid_sort_is_422(self, client):
        resp = await client.get("/api/mixes", params={"sort": "sideways"})
        assert resp.status_code == 422


class TestRereadTracklist:
    async def test_404_unknown_mix(self, client):
        resp = await client.post("/api/mixes/does-not-exist/reread-tracklist")
        assert resp.status_code == 404

    async def test_400_when_audio_missing(self, client):
        created = (await client.post("/api/mixes", json={"title": "No Audio"})).json()
        resp = await client.post(f"/api/mixes/{created['id']}/reread-tracklist")
        assert resp.status_code == 400

    async def test_202_runs_handler_in_background(self, client, tmp_path, monkeypatch):
        import asyncio

        import app.services.handlers as handlers_mod

        audio = tmp_path / "mix.flac"
        audio.write_bytes(b"x")

        done = asyncio.Event()
        calls = {}

        async def fake_reread(mix_id, session):
            calls["mix_id"] = mix_id
            done.set()
            return {"tracks_found": 0}

        # The router resolves the handler at request time from the module, so
        # patching the module attribute intercepts the background task.
        monkeypatch.setattr(handlers_mod, "handle_reread_tracklist", fake_reread)

        created = (
            await client.post(
                "/api/mixes",
                json={"title": "Reread Me", "audio_file_path": str(audio)},
            )
        ).json()

        resp = await client.post(f"/api/mixes/{created['id']}/reread-tracklist")
        assert resp.status_code == 202
        assert resp.json()["id"] == created["id"]

        await asyncio.wait_for(done.wait(), timeout=5)
        assert calls["mix_id"] == created["id"]


class TestPipelineEndpoints:
    async def test_status_counts(self, client):
        # Two pending mixes -> queued == 2.
        await client.post("/api/mixes", json={"title": "A"})
        await client.post("/api/mixes", json={"title": "B"})

        resp = await client.get("/api/pipeline/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["queued"] == 2
        assert body["completed"] == 0
        assert body["failed"] == 0
        assert body["paused"] is False

    async def test_pause_and_resume(self, client):
        paused = await client.post("/api/pipeline/pause")
        assert paused.status_code == 200
        assert paused.json()["paused"] is True

        resumed = await client.post("/api/pipeline/resume")
        assert resumed.status_code == 200
        assert resumed.json()["paused"] is False

    async def test_queue_lists_pending(self, client):
        created = (await client.post("/api/mixes", json={"title": "Queued"})).json()
        resp = await client.get("/api/pipeline/queue")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["mix_id"] == created["id"]


class TestNotificationSettings:
    async def test_get_and_update_settings_roundtrip(self, client):
        # Defaults come back empty.
        got = await client.get("/api/notifications/settings")
        assert got.status_code == 200
        assert got.json()["discord_webhook_url"] is None

        updated = await client.put(
            "/api/notifications/settings",
            json={"discord_webhook_url": "https://discord.example/webhook"},
        )
        assert updated.status_code == 200
        assert updated.json()["discord_webhook_url"] == "https://discord.example/webhook"

        # Persisted.
        got2 = await client.get("/api/notifications/settings")
        assert got2.json()["discord_webhook_url"] == "https://discord.example/webhook"

    async def test_test_notification_invalid_channel_400(self, client):
        resp = await client.post(
            "/api/notifications/test", json={"channel": "carrier-pigeon"}
        )
        assert resp.status_code == 400

    async def test_list_notifications_empty(self, client):
        resp = await client.get("/api/notifications")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
