"""Catalog router: unified list, proposal lifecycle, editor, sync/apply triggers."""

import json

import pytest


@pytest.fixture(autouse=True)
def _no_real_workers(monkeypatch):
    """approve-bulk / apply / editor triggers spawn the apply worker; stub it."""
    import app.services.catalog_apply as apply_mod

    calls = {"apply": 0}

    async def fake_run_apply():
        calls["apply"] += 1
        return {"applied": 0, "failed": 0, "queued": 0, "paused": False}

    monkeypatch.setattr(apply_mod, "run_apply", fake_run_apply)
    return calls


async def _make_mix(client=None, **kwargs):
    from app.database import async_session_factory
    from app.models import Mix

    defaults = dict(
        id="cat-mix-1",
        title="Catalog Mix",
        source="imported",
        pipeline_status="imported",
        youtube_video_id="vidCat00001",
        youtube_url="https://www.youtube.com/watch?v=vidCat00001",
        soundcloud_track_id="1200",
        soundcloud_url="https://soundcloud.com/thewillsee/catalog-mix",
        duration_seconds=3600.0,
        metadata_json={
            "catalog": {
                "youtube": {
                    "thumbnail_url": "http://t/cat.jpg",
                    "published_at": "2025-06-01T12:00:00+00:00",
                },
                "soundcloud": {
                    "artwork_url": "http://a/cat.jpg",
                    "published_at": "2025-06-02T12:00:00+00:00",
                },
            }
        },
    )
    defaults.update(kwargs)
    async with async_session_factory() as session:
        session.add(Mix(**defaults))
        await session.commit()
    return defaults["id"]


class TestCatalogMixList:
    async def test_unified_listing_shape(self, client):
        await _make_mix()
        resp = await client.get("/api/catalog/mixes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["id"] == "cat-mix-1"
        assert item["source"] == "imported"
        assert item["platforms"] == ["youtube", "soundcloud"]
        assert item["youtube_video_id"] == "vidCat00001"
        assert item["soundcloud_track_id"] == "1200"
        assert item["thumbnail_url"] == "http://t/cat.jpg"
        assert item["artwork_url"] == "http://a/cat.jpg"
        assert item["youtube_published_at"] == "2025-06-01T12:00:00+00:00"
        assert item["soundcloud_published_at"] == "2025-06-02T12:00:00+00:00"
        assert item["duration_seconds"] == 3600.0
        assert item["title_locked"] is False
        assert item["open_proposals"] == 0

    async def test_platform_filters(self, client):
        await _make_mix()
        await _make_mix(
            id="yt-only", title="YT Only",
            soundcloud_track_id=None, soundcloud_url=None, metadata_json=None,
        )
        await _make_mix(
            id="sc-only", title="SC Only",
            youtube_video_id=None, youtube_url=None, metadata_json=None,
        )

        both = (await client.get("/api/catalog/mixes", params={"platform": "both"})).json()
        assert [i["id"] for i in both["items"]] == ["cat-mix-1"]

        yt = (await client.get("/api/catalog/mixes", params={"platform": "yt-only"})).json()
        assert [i["id"] for i in yt["items"]] == ["yt-only"]

        sc = (await client.get("/api/catalog/mixes", params={"platform": "sc-only"})).json()
        assert [i["id"] for i in sc["items"]] == ["sc-only"]

    async def test_source_and_q_filters(self, client):
        await _make_mix()
        await _make_mix(id="pipe", title="Studio Session", source="pipeline", metadata_json=None)

        by_source = (
            await client.get("/api/catalog/mixes", params={"source": "pipeline"})
        ).json()
        assert [i["id"] for i in by_source["items"]] == ["pipe"]

        by_q = (await client.get("/api/catalog/mixes", params={"q": "studio"})).json()
        assert [i["id"] for i in by_q["items"]] == ["pipe"]

    async def test_open_proposal_counts(self, client):
        mix_id = await _make_mix()
        for status in ("draft", "approved", "rejected", "applied"):
            await client.post(
                f"/api/catalog/mixes/{mix_id}/proposals",
                json={"platform": "youtube", "field": "title", "proposed_value": "x"},
            )
        # Flip two of the four created drafts to closed statuses directly.
        from app.database import async_session_factory
        from app.models import MixProposal
        from sqlalchemy import select

        async with async_session_factory() as session:
            rows = (await session.execute(select(MixProposal))).scalars().all()
            rows[0].status = "rejected"
            rows[1].status = "applied"
            await session.commit()

        listing = (await client.get("/api/catalog/mixes")).json()
        assert listing["items"][0]["open_proposals"] == 2  # 2 drafts remain open


class TestCatalogSorting:
    async def _seed(self):
        """Three mixes whose platform-publish order differs from insert order.

        latest publish dates: mid-2025-05 / early-2025-03 (created_at fallback,
        no metadata) / new-2025-07.
        """
        from datetime import datetime

        await _make_mix(
            id="mid", title="Bravo Mix", duration_seconds=1800.0,
            created_at=datetime(2025, 6, 20, 12, 0, 0),
            metadata_json={
                "catalog": {
                    "youtube": {"published_at": "2025-01-01T00:00:00+00:00"},
                    "soundcloud": {"published_at": "2025-05-01T00:00:00+00:00"},
                }
            },
        )
        await _make_mix(
            id="early", title="alpha mix", duration_seconds=7200.0,
            created_at=datetime(2025, 3, 1, 12, 0, 0),
            metadata_json=None,  # no platform dates -> falls back to created_at
        )
        await _make_mix(
            id="new", title="Charlie Mix", duration_seconds=None,
            created_at=datetime(2025, 1, 5, 12, 0, 0),  # created early, published late
            metadata_json={
                "catalog": {"youtube": {"published_at": "2025-07-01T00:00:00+00:00"}}
            },
        )

    async def _ids(self, client, **params):
        resp = await client.get("/api/catalog/mixes", params=params)
        assert resp.status_code == 200
        return [i["id"] for i in resp.json()["items"]]

    async def test_default_is_newest_by_platform_publish_date(self, client):
        await self._seed()
        assert await self._ids(client) == ["new", "mid", "early"]
        assert await self._ids(client, sort="newest") == ["new", "mid", "early"]

    async def test_oldest(self, client):
        await self._seed()
        assert await self._ids(client, sort="oldest") == ["early", "mid", "new"]

    async def test_title_is_case_insensitive(self, client):
        await self._seed()
        assert await self._ids(client, sort="title") == ["early", "mid", "new"]

    async def test_duration_longest_first_nulls_last(self, client):
        await self._seed()
        assert await self._ids(client, sort="duration") == ["early", "mid", "new"]

    async def test_sort_applies_before_pagination(self, client):
        await self._seed()
        page1 = await self._ids(client, sort="newest", page=1, page_size=1)
        page2 = await self._ids(client, sort="newest", page=2, page_size=1)
        assert page1 == ["new"]
        assert page2 == ["mid"]
        assert page1 != page2

    async def test_invalid_sort_is_422(self, client):
        resp = await client.get("/api/catalog/mixes", params={"sort": "sideways"})
        assert resp.status_code == 422


class TestProposalLifecycle:
    async def test_create_defaults_to_draft(self, client):
        mix_id = await _make_mix()
        resp = await client.post(
            f"/api/catalog/mixes/{mix_id}/proposals",
            json={
                "platform": "both", "field": "title",
                "proposed_value": "Better Title", "current_value": "Catalog Mix",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "draft"
        assert body["created_by"] == "user"
        assert body["mix_title"] == "Catalog Mix"

    async def test_create_rejects_sc_playlist(self, client):
        mix_id = await _make_mix()
        resp = await client.post(
            f"/api/catalog/mixes/{mix_id}/proposals",
            json={"platform": "soundcloud", "field": "playlist", "proposed_value": "{}"},
        )
        assert resp.status_code == 400

    async def test_create_404_for_missing_mix(self, client):
        resp = await client.post(
            "/api/catalog/mixes/ghost/proposals",
            json={"platform": "youtube", "field": "title", "proposed_value": "x"},
        )
        assert resp.status_code == 404

    async def test_approve_then_reject_transitions(self, client):
        mix_id = await _make_mix()
        pid = (
            await client.post(
                f"/api/catalog/mixes/{mix_id}/proposals",
                json={"platform": "youtube", "field": "title", "proposed_value": "x"},
            )
        ).json()["id"]

        approved = await client.post(f"/api/catalog/proposals/{pid}/approve")
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        # approving an approved proposal is a 400
        again = await client.post(f"/api/catalog/proposals/{pid}/approve")
        assert again.status_code == 400

        rejected = await client.post(f"/api/catalog/proposals/{pid}/reject")
        assert rejected.json()["status"] == "rejected"

        # rejected is terminal
        assert (await client.post(f"/api/catalog/proposals/{pid}/reject")).status_code == 400
        assert (await client.post(f"/api/catalog/proposals/{pid}/approve")).status_code == 400

    async def test_list_filters_by_status(self, client):
        mix_id = await _make_mix()
        p1 = (
            await client.post(
                f"/api/catalog/mixes/{mix_id}/proposals",
                json={"platform": "youtube", "field": "title", "proposed_value": "a"},
            )
        ).json()["id"]
        await client.post(
            f"/api/catalog/mixes/{mix_id}/proposals",
            json={"platform": "soundcloud", "field": "description", "proposed_value": "b"},
        )
        await client.post(f"/api/catalog/proposals/{p1}/approve")

        drafts = (
            await client.get("/api/catalog/proposals", params={"status": "draft"})
        ).json()
        assert drafts["total"] == 1
        assert drafts["items"][0]["field"] == "description"

        approved = (
            await client.get("/api/catalog/proposals", params={"status": "approved"})
        ).json()
        assert approved["total"] == 1 and approved["items"][0]["id"] == p1

    async def test_approve_bulk_triggers_apply(self, client, _no_real_workers):
        mix_id = await _make_mix()
        ids = []
        for i in range(3):
            ids.append(
                (
                    await client.post(
                        f"/api/catalog/mixes/{mix_id}/proposals",
                        json={"platform": "youtube", "field": "title",
                              "proposed_value": f"t{i}"},
                    )
                ).json()["id"]
            )
        resp = await client.post(
            "/api/catalog/proposals/approve-bulk", json={"ids": ids + ["ghost"]}
        )
        assert resp.status_code == 200
        assert resp.json() == {"approved": 3, "applying": True}
        assert _no_real_workers["apply"] == 1


class TestLockTitle:
    async def test_lock_and_unlock(self, client):
        mix_id = await _make_mix()
        resp = await client.post(
            f"/api/catalog/mixes/{mix_id}/lock-title", json={"locked": True}
        )
        assert resp.json() == {"id": mix_id, "title_locked": True}
        resp = await client.post(
            f"/api/catalog/mixes/{mix_id}/lock-title", json={"locked": False}
        )
        assert resp.json()["title_locked"] is False


class TestMixEditor:
    async def test_edits_become_approved_user_proposals(self, client, _no_real_workers):
        mix_id = await _make_mix()
        resp = await client.put(
            f"/api/catalog/mixes/{mix_id}",
            json={
                "youtube": {"title": "New YT", "description": "New YT desc"},
                "soundcloud": {"tags": ["house", "dj set"]},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applying"] is False
        assert _no_real_workers["apply"] == 0
        by_field = {(p["platform"], p["field"]): p for p in body["proposals"]}
        assert set(by_field) == {
            ("youtube", "title"), ("youtube", "description"), ("soundcloud", "tags"),
        }
        assert all(p["status"] == "approved" for p in body["proposals"])
        assert all(p["created_by"] == "user" for p in body["proposals"])
        assert json.loads(by_field[("soundcloud", "tags")]["proposed_value"]) == [
            "house", "dj set",
        ]

    async def test_apply_true_triggers_worker(self, client, _no_real_workers):
        mix_id = await _make_mix()
        resp = await client.put(
            f"/api/catalog/mixes/{mix_id}",
            json={"youtube": {"title": "Go"}, "apply": True},
        )
        assert resp.json()["applying"] is True
        assert _no_real_workers["apply"] == 1

    async def test_empty_body_is_400(self, client):
        mix_id = await _make_mix()
        assert (await client.put(f"/api/catalog/mixes/{mix_id}", json={})).status_code == 400


class TestSyncEndpoints:
    async def test_sync_status_empty(self, client):
        resp = await client.get("/api/catalog/sync/status")
        assert resp.json() == {"running": False, "last_sync": None}

    async def test_trigger_sync_runs_background_job(self, client, monkeypatch):
        import asyncio

        import app.services.catalog_sync as sync_mod

        ran = asyncio.Event()

        async def fake_sync():
            ran.set()
            return {"status": "ok"}

        monkeypatch.setattr(sync_mod, "run_catalog_sync", fake_sync)
        resp = await client.post("/api/catalog/sync")
        assert resp.status_code == 202
        assert resp.json()["status"] == "started"
        await asyncio.wait_for(ran.wait(), timeout=2)

    async def test_sync_status_reports_last_run(self, client):
        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(
                AppSettings(
                    id=1,
                    settings_json={"catalog_last_sync": {"status": "ok", "pairs_created": 4}},
                )
            )
            await session.commit()
        resp = await client.get("/api/catalog/sync/status")
        assert resp.json()["last_sync"] == {"status": "ok", "pairs_created": 4}


class TestApplyImproveTriggers:
    async def test_trigger_apply(self, client, _no_real_workers):
        import asyncio

        resp = await client.post("/api/catalog/apply")
        assert resp.status_code == 202
        await asyncio.sleep(0)  # let the task run
        assert _no_real_workers["apply"] == 1

    async def test_trigger_improve(self, client, monkeypatch):
        import asyncio

        import app.services.catalog_improve as improve_mod

        received = {}

        async def fake_improve(mix_ids=None):
            received["mix_ids"] = mix_ids
            return {}

        monkeypatch.setattr(improve_mod, "run_improve", fake_improve)
        resp = await client.post("/api/catalog/improve", json={"mix_ids": ["a", "b"]})
        assert resp.status_code == 202
        await asyncio.sleep(0)
        assert received["mix_ids"] == ["a", "b"]

    async def test_improve_rejects_bad_scope_string(self, client):
        resp = await client.post("/api/catalog/improve", json={"mix_ids": "everything"})
        assert resp.status_code == 422


class TestRegenThumbnailsEndpoint:
    async def test_trigger_and_status(self, client, monkeypatch):
        import asyncio

        import app.services.catalog_thumbnails as thumbs_mod

        received = {}

        async def fake_run(mix_ids=None):
            received["mix_ids"] = mix_ids
            return {}

        monkeypatch.setattr(thumbs_mod, "run_regen_thumbnails", fake_run)
        resp = await client.post(
            "/api/catalog/regen-thumbnails", json={"mix_ids": ["a"]}
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "started"
        await asyncio.sleep(0)
        assert received["mix_ids"] == ["a"]

        status = await client.get("/api/catalog/regen-thumbnails/status")
        assert status.status_code == 200
        body = status.json()
        assert set(body) == {"running", "last_regen"}

    @pytest.mark.parametrize("scope", ["all", "raid-trains"])
    async def test_string_scopes_accepted(self, client, monkeypatch, scope):
        import asyncio

        import app.services.catalog_thumbnails as thumbs_mod

        received = {}

        async def fake_run(mix_ids=None):
            received["mix_ids"] = mix_ids
            return {}

        monkeypatch.setattr(thumbs_mod, "run_regen_thumbnails", fake_run)
        resp = await client.post(
            "/api/catalog/regen-thumbnails", json={"mix_ids": scope}
        )
        assert resp.status_code == 202
        await asyncio.sleep(0)
        assert received["mix_ids"] == scope

    async def test_bad_scope_string_rejected(self, client):
        resp = await client.post(
            "/api/catalog/regen-thumbnails", json={"mix_ids": "everything"}
        )
        assert resp.status_code == 422

    async def test_single_flight(self, client, monkeypatch):
        import asyncio

        import app.services.catalog_thumbnails as thumbs_mod

        release = asyncio.Event()

        async def slow_run(mix_ids=None):
            await release.wait()
            return {}

        monkeypatch.setattr(thumbs_mod, "run_regen_thumbnails", slow_run)
        first = await client.post("/api/catalog/regen-thumbnails", json={"mix_ids": "all"})
        assert first.json()["status"] == "started"
        second = await client.post("/api/catalog/regen-thumbnails", json={"mix_ids": "all"})
        assert second.json()["status"] == "already_running"
        status = await client.get("/api/catalog/regen-thumbnails/status")
        assert status.json()["running"] is True
        release.set()
        await asyncio.sleep(0)

    async def test_status_reads_persisted_summary(self, client):
        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(
                AppSettings(
                    id=1,
                    settings_json={
                        "catalog_last_regen_thumbs": {"status": "ok", "targeted": 3}
                    },
                )
            )
            await session.commit()
        resp = await client.get("/api/catalog/regen-thumbnails/status")
        assert resp.json()["last_regen"] == {"status": "ok", "targeted": 3}


class TestOrganizePlaylistsEndpoint:
    async def test_trigger_and_status(self, client, monkeypatch):
        import asyncio

        import app.services.catalog_playlists as pl_mod

        received = {}

        async def fake_run(mix_ids=None):
            received["mix_ids"] = mix_ids
            return {}

        monkeypatch.setattr(pl_mod, "run_organize_playlists", fake_run)
        resp = await client.post(
            "/api/catalog/organize-playlists", json={"mix_ids": ["m1", "m2"]}
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "started"
        await asyncio.sleep(0)
        assert received["mix_ids"] == ["m1", "m2"]

        status = await client.get("/api/catalog/organize-playlists/status")
        assert status.status_code == 200
        assert set(status.json()) == {"running", "last_playlists"}

    async def test_no_body_defaults_to_all(self, client, monkeypatch):
        import asyncio

        import app.services.catalog_playlists as pl_mod

        received = {"mix_ids": "unset"}

        async def fake_run(mix_ids=None):
            received["mix_ids"] = mix_ids
            return {}

        monkeypatch.setattr(pl_mod, "run_organize_playlists", fake_run)
        resp = await client.post("/api/catalog/organize-playlists")
        assert resp.status_code == 202
        await asyncio.sleep(0)
        assert received["mix_ids"] is None

    async def test_bad_scope_string_rejected(self, client):
        resp = await client.post(
            "/api/catalog/organize-playlists", json={"mix_ids": "some"}
        )
        assert resp.status_code == 422

    async def test_single_flight(self, client, monkeypatch):
        import asyncio

        import app.services.catalog_playlists as pl_mod

        release = asyncio.Event()

        async def slow_run(mix_ids=None):
            await release.wait()
            return {}

        monkeypatch.setattr(pl_mod, "run_organize_playlists", slow_run)
        first = await client.post("/api/catalog/organize-playlists", json={"mix_ids": "all"})
        assert first.json()["status"] == "started"
        second = await client.post("/api/catalog/organize-playlists", json={"mix_ids": "all"})
        assert second.json()["status"] == "already_running"
        status = await client.get("/api/catalog/organize-playlists/status")
        assert status.json()["running"] is True
        release.set()
        await asyncio.sleep(0)

    async def test_status_reads_persisted_summary(self, client):
        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            session.add(
                AppSettings(
                    id=1,
                    settings_json={
                        "catalog_last_playlists": {"status": "ok", "targeted": 7}
                    },
                )
            )
            await session.commit()
        resp = await client.get("/api/catalog/organize-playlists/status")
        assert resp.json()["last_playlists"] == {"status": "ok", "targeted": 7}
