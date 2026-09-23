"""Retroactive backfill endpoint: rename + tag source files of published mixes.

The real (non-dry) run rewrites roughly 498 GB of irreplaceable multi-gigabyte
FLAC recordings in place. The owner runs the dry run first and treats its
report as a go/no-go decision before that rewrite, so these tests exercise
the HTTP layer and the actual background coroutine rather than just asserting
on source text -- a dry run that silently touched files, or a "safe by
default" claim that only held at the Python-signature level and not over the
wire, would defeat the entire point of the control.
"""

import asyncio
import inspect
import sys
import types

# Sandbox-only workaround: this environment doesn't have the `shazamio`
# dependency installed (a real dependency of app/services/audio_analyzer.py
# and app/services/shorts_pipeline.py, unrelated to this task -- see the
# task instructions). The `client` fixture below imports `app.main`, which
# eagerly imports every router including `shorts.py`, so without this the
# HTTP-level tests here can't even collect. This stubs the module in THIS
# process's sys.modules only (no pip install, nothing touches the real
# environment) and is a no-op if shazamio is actually installed.
if "shazamio" not in sys.modules:
    try:
        import shazamio  # noqa: F401
    except ModuleNotFoundError:
        _fake_shazamio = types.ModuleType("shazamio")

        class _FakeShazam:  # pragma: no cover - never exercised by these tests
            async def recognize(self, *args, **kwargs):
                return {}

        _fake_shazamio.Shazam = _FakeShazam
        sys.modules["shazamio"] = _fake_shazamio

from app.routers import catalog


async def _make_mix(**kwargs):
    from app.database import async_session_factory
    from app.models import Mix

    defaults = dict(
        id="retag-mix-1",
        title="Retag Mix",
        source="pipeline",
        pipeline_status="completed",
    )
    defaults.update(kwargs)
    async with async_session_factory() as session:
        session.add(Mix(**defaults))
        await session.commit()
    return defaults["id"]


def test_retag_defaults_to_dry_run_signature():
    """Cheap structural check ONLY: the declared ``dry_run`` parameter's
    default value is True. This proves nothing about runtime behaviour --
    not that a bare POST reaches this handler, not that the default is
    actually honoured end to end. See
    ``test_bare_post_reports_dry_run_true_over_http`` for the real proof."""
    sig = inspect.signature(catalog.catalog_retag)
    default = sig.parameters["dry_run"].default
    # FastAPI's Query(...) wraps the real default; unwrap it if present so
    # this stays a check of the declared default, not of FastAPI internals.
    actual_default = getattr(default, "default", default)
    assert actual_default is True


class TestBareRequestIsSafe:
    async def test_bare_post_reports_dry_run_true_over_http(self, client):
        """POST with no body/params at all must report dry_run: true in the
        actual HTTP response -- the thing the owner reads as the go/no-go
        signal, not just the Python default."""
        resp = await client.post("/api/catalog/retag")
        assert resp.status_code == 202
        body = resp.json()
        assert body["dry_run"] is True
        assert body["started"] is True
        assert "candidates" in body


class TestDryRunPerformsNoWrites:
    async def test_dry_run_calls_both_helpers_with_dry_run_true(self, prepared_db, monkeypatch):
        """Drive the actual background coroutine (not just the route) and
        prove BOTH the renamer and tagger are invoked with dry_run=True, so a
        dry run can never fall through to the destructive path."""
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        await _make_mix(id="retag-mix-dry")

        rename_calls = []
        tag_calls = []

        async def fake_rename(mix_id, *, reason, dry_run=False):
            rename_calls.append({"mix_id": mix_id, "reason": reason, "dry_run": dry_run})
            return {"status": "dry_run", "renamed": [], "skipped": []}

        async def fake_tag(mix_id, *, reason, dry_run=False):
            tag_calls.append({"mix_id": mix_id, "reason": reason, "dry_run": dry_run})
            return {"status": "dry_run", "actions": [], "reason": reason}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        # Call the underlying coroutine directly rather than going through
        # BackgroundTasks/asyncio.create_task: the route just schedules this
        # same function, and awaiting it directly is deterministic (no
        # dependence on the event loop yielding at the right moment).
        await catalog._run_retag(dry_run=True, limit=None)

        assert len(rename_calls) == 1
        assert len(tag_calls) == 1
        assert rename_calls[0]["mix_id"] == "retag-mix-dry"
        assert rename_calls[0]["dry_run"] is True
        assert tag_calls[0]["mix_id"] == "retag-mix-dry"
        assert tag_calls[0]["dry_run"] is True


class TestSingleFlight:
    async def test_concurrent_run_returns_409(self, client, monkeypatch):
        """A second run started while one is still in progress must be
        rejected with 409, not queued or silently coalesced -- two concurrent
        passes over the same FLACs is exactly the hazard this guards."""
        import app.services.source_renamer as renamer_mod

        await _make_mix(id="retag-mix-slow")

        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow_rename(mix_id, *, reason, dry_run=False):
            entered.set()
            await release.wait()
            return {"status": "dry_run", "renamed": [], "skipped": []}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", slow_rename)

        first = await client.post("/api/catalog/retag")
        assert first.status_code == 202
        assert first.json()["started"] is True

        # Let the spawned background task actually start and block inside
        # the patched renamer before we fire the second request.
        await asyncio.wait_for(entered.wait(), timeout=2)

        second = await client.post("/api/catalog/retag")
        assert second.status_code == 409

        status = await client.get("/api/catalog/retag/status")
        assert status.json()["running"] is True

        release.set()
        # Await the actual first-run task directly so it can't leak into a
        # later test -- polling on the "running" flag is racy (it can read
        # the module default before the task has even started).
        await asyncio.wait_for(catalog._retag_task, timeout=5)


class TestOnlyCompletedMixesAreCandidates:
    async def test_selection_excludes_non_completed_mixes(self, client, monkeypatch):
        """Seed mixes across several pipeline statuses and assert only the
        'completed' one is selected -- both in the synchronous candidate
        count and in the actual run's processed results."""
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        await _make_mix(id="retag-completed", pipeline_status="completed")
        await _make_mix(id="retag-pending", pipeline_status="pending")
        await _make_mix(id="retag-failed", pipeline_status="failed")
        await _make_mix(id="retag-recording", pipeline_status="recording")

        seen_mix_ids = []

        async def fake_rename(mix_id, *, reason, dry_run=False):
            seen_mix_ids.append(mix_id)
            return {"status": "dry_run", "renamed": [], "skipped": []}

        async def fake_tag(mix_id, *, reason, dry_run=False):
            return {"status": "dry_run", "actions": [], "reason": reason}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        resp = await client.post("/api/catalog/retag")
        assert resp.status_code == 202
        assert resp.json()["candidates"] == 1

        # Await the actual background task directly (deterministic) rather
        # than polling the "running" flag, which can read the pre-start
        # default before the task has been scheduled at all.
        await asyncio.wait_for(catalog._retag_task, timeout=5)

        status = await client.get("/api/catalog/retag/status")
        body = status.json()
        assert body["total"] == 1
        assert [r["mix_id"] for r in body["results"]] == ["retag-completed"]
        assert seen_mix_ids == ["retag-completed"]
