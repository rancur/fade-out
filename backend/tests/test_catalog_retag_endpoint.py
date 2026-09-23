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
import os
import sys
import time
import types
from types import SimpleNamespace

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
    async def test_bare_post_reports_dry_run_true_over_http(self, client, monkeypatch):
        """POST with no body/params at all must report dry_run: true in the
        actual HTTP response -- the thing the owner reads as the go/no-go
        signal, not just the Python default."""
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        # The route spawns a real background _run_retag against the real DB.
        # Patch the helpers (as every other test in this file does) so that
        # run touches nothing, and await the task before returning so it
        # can't leak into the next test.
        async def fake_rename(mix_id, *, reason, dry_run=False):
            # Real shape (source_renamer.rename_sources_for_mix): no
            # top-level "status" key -- see _rename_outcome_status.
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

        async def fake_tag(mix_id, *, reason, dry_run=False):
            return {"status": "dry_run", "actions": [], "reason": reason}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        resp = await client.post("/api/catalog/retag")
        assert resp.status_code == 202
        body = resp.json()
        assert body["dry_run"] is True
        assert body["started"] is True
        assert "candidates" in body

        await asyncio.wait_for(catalog._retag_task, timeout=5)


class TestDryRunCallsHelpersWithDryRunTrue:
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
            # Real shape (source_renamer.rename_sources_for_mix): no
            # top-level "status" key -- see _rename_outcome_status.
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

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
            # Real shape (source_renamer.rename_sources_for_mix): no
            # top-level "status" key -- see _rename_outcome_status.
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

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
            # Real shape (source_renamer.rename_sources_for_mix): no
            # top-level "status" key -- see _rename_outcome_status.
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

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
            # Real shape (source_renamer.rename_sources_for_mix): no
            # top-level "status" key -- see _rename_outcome_status.
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

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
        tell the two apart.

        The renamer stubs below use ``rename_sources_for_mix``'s REAL return
        shape (``{mix_id, reason, dry_run, renamed, skipped, errors}``, per
        ``source_renamer.py`` -- there is no top-level "status" key at all).
        Earlier versions of this test used ``{"status": "ok", ...}`` stubs,
        a shape the real function never returns, which made this test green
        against fiction: it could never have caught the bug where the
        summary silently dropped every renamer outcome because
        ``outcome.get("status")`` was always None.
        """
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        await _make_mix(id="retag-ok", pipeline_status="completed")
        await _make_mix(id="retag-disabled", pipeline_status="completed")
        await _make_mix(id="retag-failed", pipeline_status="completed")

        def _rename_result(mix_id, **overrides):
            base = {
                "mix_id": mix_id, "reason": "retag_backfill", "dry_run": False,
                "renamed": [], "skipped": [], "errors": [],
            }
            base.update(overrides)
            return base

        rename_outcomes = {
            # renamed non-empty -> derived status "ok"
            "retag-ok": _rename_result(
                "retag-ok",
                renamed=[{"kind": "audio", "from": "/a.flac", "to": "/b.flac"}],
            ),
            # a skipped entry with reason "disabled" -> derived status "disabled"
            "retag-disabled": _rename_result(
                "retag-disabled", skipped=[{"reason": "disabled"}],
            ),
            # errors non-empty -> derived status "failed"
            "retag-failed": _rename_result(
                "retag-failed",
                errors=[{"kind": "audio", "reason": "os_error", "path": "/c.flac", "error": "boom"}],
            ),
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
        # The renamer outcome has no "status" key of its own, so its status
        # must go through the same derivation _run_retag itself uses.
        expected = {status: 0 for status in catalog._RETAG_SUMMARY_STATUSES}
        for r in state["results"]:
            expected[catalog._rename_outcome_status(r["rename"])] += 1
            tag_status = r["tag"]["status"]
            expected[tag_status if tag_status in expected else "unknown"] += 1

        assert state["summary"] == expected
        assert state["summary"] == {
            "ok": 2, "skipped": 1, "disabled": 2, "failed": 1, "dry_run": 0,
            "unknown": 0,
        }

        # The totals must always reconcile: every mix contributes exactly
        # one rename outcome and one tag outcome, and an unrecognised status
        # must be bucketed into "unknown" rather than silently dropped, so
        # this sum can never silently fall short of 2 * processed.
        assert sum(state["summary"].values()) == 2 * state["processed"]

    async def test_unrecognised_status_is_bucketed_as_unknown(self, prepared_db, monkeypatch):
        """An outcome shape this summary doesn't recognise must be counted,
        not dropped -- the exact failure mode this whole item exists to
        close off. Regression guard: with a naive ``outcome.get("status")``
        read directly against the renamer's real return shape (no top-level
        "status" key), this would previously vanish without incrementing
        anything at all."""
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        await _make_mix(id="retag-weird", pipeline_status="completed")

        async def fake_rename(mix_id, *, reason, dry_run=False):
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

        async def fake_tag(mix_id, *, reason, dry_run=False):
            # A status this summary was never taught about.
            return {"status": "quarantined", "actions": [], "reason": "??"}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        await catalog._run_retag(dry_run=False, limit=None)

        state = catalog._retag_state
        # rename -> "skipped" (nothing renamed, nothing errored, nothing
        # disabled); tag -> "unknown" (unrecognised status).
        assert state["summary"]["skipped"] == 1
        assert state["summary"]["unknown"] == 1
        assert sum(state["summary"].values()) == 2 * state["processed"]


class TestSweepPassesDryRunThrough:
    """Blocker 5 -- the delete-primitive bug. ``_sweep_retag_staging`` used
    to call ``source_tagger.sweep_staging(staging_root)`` POSITIONALLY, which
    silently dropped ``dry_run`` to that function's own default of
    ``False`` regardless of what the retag caller asked for -- so a bare,
    "report-only" POST deleted real orphaned staging files. These tests
    exercise the real ``catalog._sweep_retag_staging`` ->
    ``source_tagger.sweep_staging`` call boundary (not a mock of either
    side), since the bug lived exactly at that boundary, and use a REAL
    file on disk so a regression actually deletes something a test can see.
    """

    async def test_dry_run_sweep_reports_but_does_not_delete(self, tmp_path, monkeypatch):
        import app.services.source_tagger as tagger_mod
        from app.config import settings

        audio = tmp_path / "audio"
        video = tmp_path / "video"
        audio.mkdir()
        video.mkdir()
        monkeypatch.setattr(settings, "WATCH_AUDIO_PATH", str(audio))
        monkeypatch.setattr(settings, "WATCH_VIDEO_PATH", str(video))

        staging = audio / tagger_mod.TEMP_DIRNAME
        staging.mkdir()
        orphan = staging / "orphan.flac"
        orphan.write_bytes(b"x" * 1024)

        # The default age threshold is 6 hours; sweep_staging ages off the
        # MORE RECENT of st_mtime/st_ctime, and real ctime cannot be
        # backdated through any public API. Patch os.stat to report a
        # controlled, genuinely-old ctime for this one path -- same
        # technique as test_staging_sweeper.py's ``fake_stat`` fixture.
        real_stat = os.stat
        old_ts = time.time() - 7 * 3600
        target = os.path.normpath(str(orphan))

        def _stat(path, *args, **kwargs):
            real = real_stat(path, *args, **kwargs)
            if os.path.normpath(os.fspath(path)) == target:
                return SimpleNamespace(
                    st_mtime=old_ts, st_ctime=old_ts, st_size=real.st_size,
                )
            return real

        monkeypatch.setattr(os, "stat", _stat)

        result = await catalog._sweep_retag_staging(dry_run=True)
        assert orphan.exists(), "a dry-run sweep must not delete anything, for real"
        assert result["removed_count"] == 1  # reports what WOULD be removed

        result = await catalog._sweep_retag_staging(dry_run=False)
        assert not orphan.exists(), "sanity: the age guard genuinely caught it"
        assert result["removed_count"] == 1


class TestSweepTargetsIncludeNestedCandidateDirs:
    """Finding 7 -- sweep targets must be the UNION of the flat watch roots
    AND ``dirname(candidate_source)/.fadeout-tagging`` for every candidate
    this run selected, not the flat roots alone. A source living in a
    subdirectory of a watch root (e.g. ``<watch_audio>/2023/foo.flac``)
    stages its orphaned copy under ``<watch_audio>/2023/.fadeout-tagging``,
    which sweeping only the flat root's own ``.fadeout-tagging`` would never
    visit."""

    async def test_orphan_under_a_nested_candidate_dir_is_swept(self, tmp_path, monkeypatch):
        import app.services.source_tagger as tagger_mod
        from app.config import settings

        audio = tmp_path / "audio"
        video = tmp_path / "video"
        audio.mkdir()
        video.mkdir()
        monkeypatch.setattr(settings, "WATCH_AUDIO_PATH", str(audio))
        monkeypatch.setattr(settings, "WATCH_VIDEO_PATH", str(video))

        nested = audio / "2023"
        nested.mkdir()
        nested_staging = nested / tagger_mod.TEMP_DIRNAME
        nested_staging.mkdir()
        orphan = nested_staging / "orphan.flac"
        orphan.write_bytes(b"x" * 1024)

        real_stat = os.stat
        old_ts = time.time() - 7 * 3600
        target = os.path.normpath(str(orphan))

        def _stat(path, *args, **kwargs):
            real = real_stat(path, *args, **kwargs)
            if os.path.normpath(os.fspath(path)) == target:
                return SimpleNamespace(
                    st_mtime=old_ts, st_ctime=old_ts, st_size=real.st_size,
                )
            return real

        monkeypatch.setattr(os, "stat", _stat)

        # No candidate paths -- the flat-roots-only sweep must NOT reach
        # this nested orphan.
        result = await catalog._sweep_retag_staging(dry_run=False, candidate_audio_paths=[])
        assert orphan.exists(), (
            "sanity: an orphan nested under a candidate's own directory "
            "must not be reachable from the flat watch roots alone"
        )
        assert result["removed_count"] == 0

        # Now pass the actual candidate source living in that nested
        # directory -- its own staging dir must be added as a sweep target.
        candidate_source = str(nested / "foo.flac")
        result = await catalog._sweep_retag_staging(
            dry_run=False, candidate_audio_paths=[candidate_source]
        )
        assert not orphan.exists(), (
            "the nested candidate's own .fadeout-tagging dir must be swept"
        )
        assert result["removed_count"] == 1
