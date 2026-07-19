"""Tests for the persistent activity log service + API endpoint."""

import pytest

from app.services import activity_log


@pytest.mark.asyncio
async def test_log_persists_and_queries(prepared_db):
    await activity_log.info("file_detected", "audio detected", filename="set.flac")
    await activity_log.warn("file_skipped", "undersized", filename="stray.flac")
    await activity_log.error("pipeline_error", "boom", mix_id="m1", stage="upload_youtube")

    items, total = await activity_log.query(limit=50)
    assert total == 3
    # Newest first
    assert items[0]["event"] == "pipeline_error"
    assert items[0]["level"] == "error"
    assert items[0]["mix_id"] == "m1"


@pytest.mark.asyncio
async def test_query_filters(prepared_db):
    await activity_log.info("a", "one", mix_id="mix-A")
    await activity_log.error("b", "two", mix_id="mix-B")
    await activity_log.error("c", "three", mix_id="mix-A")

    err, err_total = await activity_log.query(level="error")
    assert err_total == 2
    assert all(i["level"] == "error" for i in err)

    a_items, a_total = await activity_log.query(mix_id="mix-A")
    assert a_total == 2
    assert all(i["mix_id"] == "mix-A" for i in a_items)


@pytest.mark.asyncio
async def test_invalid_level_coerced(prepared_db):
    await activity_log.log("bogus", "e", "msg")
    items, _ = await activity_log.query()
    assert items[0]["level"] == "info"


@pytest.mark.asyncio
async def test_activity_endpoint(client):
    await activity_log.info("file_detected", "hello world", filename="x.flac")

    resp = await client.get("/api/activity?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert body["items"][0]["message"] == "hello world"
    assert body["items"][0]["filename"] == "x.flac"

    resp2 = await client.get("/api/activity?level=error")
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 0
