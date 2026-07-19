"""Tests for GET /api/activity filtering, search, and cursor pagination (A3),
plus the retention prune."""

from datetime import datetime, timedelta, timezone

from app.services import activity_log


async def _seed(client=None):
    """Insert a small fixed history (oldest → newest)."""
    await activity_log.info(
        "upload_attempt", "Uploading to SoundCloud: Sunset Mix",
        mix_id="m1", platform="soundcloud",
    )
    await activity_log.warn(
        "step_retry", "Step upload_youtube failed (attempt 1/3)",
        mix_id="m1", platform="youtube", stage="upload_youtube",
    )
    await activity_log.error(
        "pipeline_error", "Pipeline error at step upload_youtube: quota",
        mix_id="m2", stage="upload_youtube",
    )
    await activity_log.info(
        "upload_result", "SoundCloud upload succeeded: https://sc/x",
        mix_id="m2", platform="soundcloud",
    )


class TestActivityQueryParams:
    async def test_q_searches_message_case_insensitively(self, client):
        await _seed()
        resp = await client.get("/api/activity", params={"q": "SOUNDCLOUD UPLOAD"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert "succeeded" in body["items"][0]["message"]

    async def test_platform_filter(self, client):
        await _seed()
        resp = await client.get("/api/activity", params={"platform": "youtube"})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["event"] == "step_retry"

    async def test_level_event_and_mix_filters_still_work(self, client):
        await _seed()
        assert (await client.get("/api/activity", params={"level": "error"})).json()["total"] == 1
        assert (await client.get("/api/activity", params={"event": "upload_attempt"})).json()["total"] == 1
        assert (await client.get("/api/activity", params={"mix_id": "m1"})).json()["total"] == 2

    async def test_since_and_until_bound_the_range(self, client):
        await _seed()
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).isoformat()
        future = (now + timedelta(hours=1)).isoformat()

        all_resp = (await client.get("/api/activity", params={"since": past, "until": future})).json()
        assert all_resp["total"] == 4

        none_resp = (await client.get("/api/activity", params={"since": future})).json()
        assert none_resp["total"] == 0

        none_resp2 = (await client.get("/api/activity", params={"until": past})).json()
        assert none_resp2["total"] == 0

    async def test_since_between_rows_splits_history(self, client):
        await activity_log.info("first", "first entry")
        cutoff = datetime.now(timezone.utc).isoformat()
        await activity_log.info("second", "second entry")

        resp = (await client.get("/api/activity", params={"since": cutoff})).json()
        assert [i["event"] for i in resp["items"]] == ["second"]

    async def test_invalid_since_is_422(self, client):
        resp = await client.get("/api/activity", params={"since": "not-a-date"})
        assert resp.status_code == 422

    async def test_before_id_cursor_pagination(self, client):
        await _seed()
        page1 = (await client.get("/api/activity", params={"limit": 2})).json()
        assert len(page1["items"]) == 2
        assert page1["total"] == 4
        cursor = page1["items"][-1]["id"]

        page2 = (
            await client.get("/api/activity", params={"limit": 2, "before_id": cursor})
        ).json()
        assert len(page2["items"]) == 2
        # total stays the full filtered count while scrolling
        assert page2["total"] == 4
        # strictly older rows, no overlap
        assert all(i["id"] < cursor for i in page2["items"])
        seen = {i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]}
        assert seen == set()

    async def test_limit_offset_paging_keeps_working(self, client):
        await _seed()
        resp = (await client.get("/api/activity", params={"limit": 2, "offset": 2})).json()
        assert len(resp["items"]) == 2
        assert resp["limit"] == 2
        assert resp["offset"] == 2


class TestActivityRetention:
    async def test_prune_deletes_rows_older_than_retention(self, prepared_db):
        from app.database import async_session_factory
        from app.models import ActivityEvent

        old_ts = datetime.now(timezone.utc) - timedelta(days=120)
        async with async_session_factory() as session:
            session.add(ActivityEvent(ts=old_ts, level="info", event="old", message="old"))
            session.add(ActivityEvent(
                ts=datetime.now(timezone.utc), level="info", event="new", message="new",
            ))
            await session.commit()

        result = await activity_log.prune(retention_days=90, max_rows=1000)
        assert result["deleted_by_age"] == 1

        items, total = await activity_log.query()
        assert total == 1
        assert items[0]["event"] == "new"

    async def test_prune_enforces_row_cap_keeping_newest(self, prepared_db):
        for i in range(10):
            await activity_log.info(f"e{i}", f"entry {i}")

        result = await activity_log.prune(retention_days=90, max_rows=4)
        assert result["deleted_by_cap"] == 6

        items, total = await activity_log.query()
        assert total == 4
        assert [i["event"] for i in items] == ["e9", "e8", "e7", "e6"]

    async def test_prune_noop_under_limits(self, prepared_db):
        await activity_log.info("keep", "keep me")
        result = await activity_log.prune(retention_days=90, max_rows=1000)
        assert result == {"deleted_by_age": 0, "deleted_by_cap": 0}
        _, total = await activity_log.query()
        assert total == 1
