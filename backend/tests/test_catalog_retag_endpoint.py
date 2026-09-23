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
    except ModuleNotFoundError as exc:
        # Only stub if shazamio ITSELF is missing. If shazamio is installed
        # but one of its own transitive deps is missing, that's a real
        # environment problem -- don't swallow it under this stub.
        if exc.name != "shazamio":
            raise
        _fake_shazamio = types.ModuleType("shazamio")

        class _FakeShazam:  # pragma: no cover - never exercised by these tests
            async def recognize(self, *args, **kwargs):
                return {}

        _fake_shazamio.Shazam = _FakeShazam
        sys.modules["shazamio"] = _fake_shazamio

from app.routers import catalog


async def _drain_pending_tasks(timeout: float = 5.0) -> None:
    """Wait for every OTHER pending asyncio task to finish.

    Used to settle background retag run(s) before asserting on their side
    effects. Unlike awaiting a single known task handle, this also catches
    an extra, un-referenced task -- which is exactly what a broken
    single-flight guard produces when two concurrent requests both spawn a
    run and the second assignment overwrites ``catalog._retag_task``,
    orphaning the first task's reference (it keeps running regardless; this
    still waits for it).
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    current = asyncio.current_task()
    while True:
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if not pending:
            return
        if loop.time() > deadline:
            raise TimeoutError(f"tasks still pending after {timeout}s: {pending}")
        await asyncio.sleep(0)


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
    async def test_second_run_while_one_is_running_returns_409(self, client, monkeypatch):
        """Steady-state branch: a second run started well AFTER the first is
        already known to be in progress must be rejected with 409, not
        queued or silently coalesced. The two POSTs here are sequential
        (the first fully resolves before the second is sent), so this only
        exercises the "already running" check once ``_retag_task`` is set --
        it can NOT catch a race in the check-then-set window itself. See
        ``test_truly_concurrent_requests_only_one_wins`` for that."""
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

    async def test_truly_concurrent_requests_only_one_wins(self, client, monkeypatch):
        """Genuine race: fire two POSTs concurrently via ``asyncio.gather``
        so they interleave on the shared event loop, exercising the actual
        check-then-set window between the single-flight guard and the
        ``_retag_task`` assignment (the bug this guards against: an ``await``
        between the check and the assignment lets both requests pass the
        guard before either sets the task). Exactly one request must win
        (202) and the other must be rejected (409), and exactly one
        background run may actually touch the seeded mix.

        The renamer is held open on an ``asyncio.Event`` for the duration of
        the gather. This matters: without it, the first (winning) run can
        finish so fast (nothing here does real I/O) that it's genuinely
        ``done()`` before the second request even reaches the guard -- which
        would make a *second, legitimate* run start and produce two 202s for
        a reason that has nothing to do with the check-then-set race. Holding
        the run open keeps it definitively in-progress for the whole window,
        so a second 202 can only mean the guard let two runs overlap.
        """
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        await _make_mix(id="retag-mix-concurrent")

        rename_calls = []
        hold = asyncio.Event()

        async def fake_rename(mix_id, *, reason, dry_run=False):
            rename_calls.append(mix_id)
            await hold.wait()
            return {"status": "dry_run", "renamed": [], "skipped": []}

        async def fake_tag(mix_id, *, reason, dry_run=False):
            return {"status": "dry_run", "actions": [], "reason": reason}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        try:
            first, second = await asyncio.gather(
                client.post("/api/catalog/retag"),
                client.post("/api/catalog/retag"),
            )

            statuses = sorted([first.status_code, second.status_code])
            assert statuses == [202, 409], (
                f"expected exactly one 202 and one 409, got {statuses}"
            )
        finally:
            hold.set()

        # Settle any/all background run(s) -- including an orphaned extra
        # one a broken guard would have spawned -- before checking side
        # effects.
        await _drain_pending_tasks()

        assert rename_calls == ["retag-mix-concurrent"], (
            "exactly one background run should have processed the mix; "
            f"got {rename_calls} (more than one entry means the "
            "single-flight guard let two concurrent runs through)"
        )


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


class TestRetagSummary:
    async def test_summary_counts_match_per_mix_results(self, prepared_db, monkeypatch):
        """_run_retag must aggregate a summary of every rename AND tag
        outcome so GET /api/catalog/retag/status cannot show a run that did
        nothing (e.g. every mix coming back "disabled" because the setting
        is off) as if it had succeeded -- processed == total alone can't
        tell the two apart."""
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        await _make_mix(id="retag-ok", pipeline_status="completed")
        await _make_mix(id="retag-disabled", pipeline_status="completed")
        await _make_mix(id="retag-failed", pipeline_status="completed")

        rename_outcomes = {
            "retag-ok": {"status": "ok", "renamed": [], "skipped": []},
            "retag-disabled": {"status": "disabled", "renamed": [], "skipped": []},
            "retag-failed": {"status": "failed", "renamed": [], "skipped": []},
        }
        tag_outcomes = {
            "retag-ok": {"status": "ok", "actions": [], "reason": "ok"},
            "retag-disabled": {"status": "disabled", "actions": [], "reason": "off"},
            "retag-failed": {"status": "skipped", "actions": [], "reason": "no space"},
        }

        async def fake_rename(mix_id, *, reason, dry_run=False):
            return rename_outcomes[mix_id]

        async def fake_tag(mix_id, *, reason, dry_run=False):
            return tag_outcomes[mix_id]

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        await catalog._run_retag(dry_run=False, limit=None)

        state = catalog._retag_state
        assert state["total"] == 3
        assert len(state["results"]) == 3

        # The summary must be derivable from -- and match -- the per-mix
        # results it was built alongside, not just plausible-looking counts.
        expected = {"ok": 0, "skipped": 0, "disabled": 0, "failed": 0, "dry_run": 0}
        for r in state["results"]:
            expected[r["rename"]["status"]] += 1
            expected[r["tag"]["status"]] += 1

        assert state["summary"] == expected
        assert state["summary"] == {
            "ok": 2, "skipped": 1, "disabled": 2, "failed": 1, "dry_run": 0,
        }
