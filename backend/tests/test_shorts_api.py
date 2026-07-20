"""Tests for the /api/shorts endpoints (service work mocked out)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models import Short
from app.routers import shorts as shorts_router


class FakeService:
    """Records calls; the router only wires HTTP to the service."""

    def __init__(self):
        self.scan_calls = 0
        self.upload_calls = []
        self.process_calls = []
        self.regen_calls = []

    async def scan(self):
        self.scan_calls += 1
        return {"status": "ok"}

    async def upload_short_by_id(self, short_id, manual=False):
        self.upload_calls.append((short_id, manual))
        return "uploaded"

    async def process_short(self, short_id, auto_upload=True):
        self.process_calls.append(short_id)
        return "ready"

    async def regenerate_metadata(self, short_id):
        self.regen_calls.append(short_id)
        return {}

    async def stats(self):
        return {"uploaded_today": 1, "daily_cap": 3, "queued": 2}


@pytest.fixture
def fake_service(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(shorts_router, "get_shorts_service", lambda: service)
    monkeypatch.setattr(shorts_router, "_scan_task", None)
    return service


async def _seed_short(**kw):
    defaults = dict(
        file_path="/watch/shorts/Backtrack 2026-05-14 21-03-22.mp4",
        status="ready",
        title="A hooky title 🔥",
        description="desc\n#shorts #dj",
        tags=["dj"],
        detected_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    async with async_session_factory() as session:
        short = Short(**defaults)
        session.add(short)
        await session.commit()
        return short.id


class TestListAndStats:
    async def test_empty_list(self, client):
        resp = await client.get("/api/shorts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0

    async def test_list_with_filters_and_pagination(self, client):
        base = datetime.now(timezone.utc)
        for i in range(3):
            await _seed_short(
                file_path=f"/watch/shorts/clip{i}.mp4",
                status="queued" if i < 2 else "uploaded",
                detected_at=base + timedelta(minutes=i),
            )

        resp = await client.get("/api/shorts", params={"status": "queued"})
        body = resp.json()
        assert body["total"] == 2
        assert all(item["status"] == "queued" for item in body["items"])
        # newest first by default
        assert body["items"][0]["filename"] == "clip1.mp4"

        resp = await client.get(
            "/api/shorts", params={"sort": "oldest", "page_size": 1, "page": 1}
        )
        body = resp.json()
        assert body["items"][0]["filename"] == "clip0.mp4"
        assert body["total"] == 3

        resp = await client.get("/api/shorts", params={"q": "clip2"})
        assert resp.json()["total"] == 1

    async def test_list_rejects_unknown_status(self, client):
        resp = await client.get("/api/shorts", params={"status": "bogus"})
        assert resp.status_code == 422

    async def test_stats_endpoint_uses_service(self, client, fake_service):
        resp = await client.get("/api/shorts/stats")
        assert resp.status_code == 200
        assert resp.json()["daily_cap"] == 3


class TestScan:
    async def test_scan_starts_and_reports_single_flight(self, client, fake_service):
        resp = await client.post("/api/shorts/scan")
        assert resp.status_code == 202
        assert resp.json()["status"] == "started"

    async def test_scan_status_reports_last_summary(self, client):
        from app.models import AppSettings
        from app.services.shorts_pipeline import LAST_SCAN_KEY

        async with async_session_factory() as session:
            session.add(
                AppSettings(id=1, settings_json={LAST_SCAN_KEY: {"status": "ok"}})
            )
            await session.commit()
        resp = await client.get("/api/shorts/scan/status")
        assert resp.status_code == 200
        assert resp.json()["last_scan"] == {"status": "ok"}


class TestActions:
    async def test_manual_upload_triggers_service(self, client, fake_service):
        short_id = await _seed_short()
        resp = await client.post(f"/api/shorts/{short_id}/upload")
        assert resp.status_code == 202
        await asyncio.sleep(0)  # let the spawned task run
        assert fake_service.upload_calls == [(short_id, True)]

    async def test_upload_conflicts_when_already_uploaded(self, client, fake_service):
        short_id = await _seed_short(status="uploaded")
        resp = await client.post(f"/api/shorts/{short_id}/upload")
        assert resp.status_code == 409

    async def test_upload_conflicts_without_metadata(self, client, fake_service):
        short_id = await _seed_short(title=None, description=None, status="detected")
        resp = await client.post(f"/api/shorts/{short_id}/upload")
        assert resp.status_code == 409

    async def test_skip_and_unskip_requeues_with_metadata(self, client, fake_service):
        short_id = await _seed_short(status="queued")

        resp = await client.post(f"/api/shorts/{short_id}/skip")
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

        resp = await client.post(f"/api/shorts/{short_id}/unskip")
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"  # metadata present -> queue
        assert fake_service.process_calls == []

    async def test_unskip_without_metadata_reprocesses(self, client, fake_service):
        short_id = await _seed_short(status="skipped", title=None, description=None)
        resp = await client.post(f"/api/shorts/{short_id}/unskip")
        assert resp.status_code == 200
        assert resp.json()["status"] == "detected"
        await asyncio.sleep(0)  # let the spawned task run
        assert fake_service.process_calls == [short_id]

    async def test_unskip_conflicts_when_not_skipped(self, client, fake_service):
        short_id = await _seed_short(status="ready")
        resp = await client.post(f"/api/shorts/{short_id}/unskip")
        assert resp.status_code == 409

    async def test_skip_conflicts_when_uploaded(self, client, fake_service):
        short_id = await _seed_short(status="uploaded")
        resp = await client.post(f"/api/shorts/{short_id}/skip")
        assert resp.status_code == 409

    async def test_edit_title_description_tags(self, client):
        short_id = await _seed_short()
        resp = await client.put(
            f"/api/shorts/{short_id}",
            json={"title": "Edited", "description": "new desc #shorts", "tags": ["x"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Edited"
        assert body["tags"] == ["x"]

        async with async_session_factory() as session:
            row = (
                await session.execute(select(Short).where(Short.id == short_id))
            ).scalar_one()
            assert row.description == "new desc #shorts"

    async def test_edit_rejects_empty_body_and_uploaded(self, client):
        short_id = await _seed_short()
        resp = await client.put(f"/api/shorts/{short_id}", json={})
        assert resp.status_code == 400

        uploaded_id = await _seed_short(status="uploaded", file_path="/w/u.mp4")
        resp = await client.put(f"/api/shorts/{uploaded_id}", json={"title": "x"})
        assert resp.status_code == 409

    async def test_regenerate_metadata_triggers_service(self, client, fake_service):
        short_id = await _seed_short(status="failed")
        resp = await client.post(f"/api/shorts/{short_id}/regenerate-metadata")
        assert resp.status_code == 202
        await asyncio.sleep(0)  # let the spawned task run
        assert fake_service.regen_calls == [short_id]

    async def test_404s(self, client, fake_service):
        for method, path in [
            ("post", "/api/shorts/nope/upload"),
            ("post", "/api/shorts/nope/skip"),
            ("post", "/api/shorts/nope/unskip"),
            ("post", "/api/shorts/nope/regenerate-metadata"),
            ("put", "/api/shorts/nope"),
        ]:
            resp = await getattr(client, method)(
                path, **({"json": {"title": "x"}} if method == "put" else {})
            )
            assert resp.status_code == 404, path
