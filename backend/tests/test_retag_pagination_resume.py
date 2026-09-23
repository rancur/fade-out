"""Ordered, offset-based pagination and a resume cursor for the retag backfill.

``POST /api/catalog/retag`` backfills ~80 irreplaceable multi-gigabyte FLAC
recordings (~498 GB). Before this change, ``_run_retag`` and the candidate
count both used ``.limit(n)`` with no ``ORDER BY`` and no offset, so a
second batch re-selected the same rows the first batch already touched --
the documented "run it with `limit` under supervision" workflow didn't
actually paginate. These tests exercise the real selection query (via
``_run_retag``) and the real HTTP response, not just source text, since a
pagination bug here means silently reprocessing (or never reaching) real
files across a run an operator believes is advancing.
"""

import asyncio
import sys
import types
from datetime import datetime, timezone

# Sandbox-only workaround: this environment doesn't have the `shazamio`
# dependency installed (a real dependency of app/services/audio_analyzer.py
# and app/services/shorts_pipeline.py, unrelated to this task). The `client`
# fixture imports `app.main`, which eagerly imports every router including
# `shorts.py`, so without this the HTTP-level tests here can't even collect.
# This stubs the module in THIS process's sys.modules only (no pip install,
# nothing touches the real environment) and is a no-op if shazamio is
# actually installed. Mirrors test_catalog_retag_endpoint.py.
if "shazamio" not in sys.modules:
    try:
        import shazamio  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "shazamio":
            raise
        _fake_shazamio = types.ModuleType("shazamio")

        class _FakeShazam:  # pragma: no cover - never exercised by these tests
            async def recognize(self, *args, **kwargs):
                return {}

        _fake_shazamio.Shazam = _FakeShazam
        sys.modules["shazamio"] = _fake_shazamio

from sqlalchemy import select

from app.routers import catalog


def _dt(day: int) -> datetime:
    """A naive UTC-ish datetime for deterministic ``created_at`` ordering."""
    return datetime(2024, 1, day, 12, 0, 0)


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


async def _delete_mix(mix_id: str) -> None:
    from app.database import async_session_factory
    from app.models import Mix

    async with async_session_factory() as session:
        result = await session.execute(select(Mix).where(Mix.id == mix_id))
        row = result.scalar_one_or_none()
        if row is not None:
            await session.delete(row)
            await session.commit()


async def _get_progress():
    from app.database import async_session_factory
    from app.models import AppSettings

    async with async_session_factory() as session:
        result = await session.execute(
            select(AppSettings).where(AppSettings.id == 1)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        return (row.settings_json or {}).get(catalog.RETAG_PROGRESS_KEY)


def _noop_fakes(monkeypatch, seen=None):
    """Patch renamer/tagger with cheap fakes that record the mix_id seen and
    return the REAL shapes each function returns (see FIXTURE WARNING in the
    task brief: ``rename_sources_for_mix`` has no top-level ``status`` key)."""
    import app.services.source_renamer as renamer_mod
    import app.services.source_tagger as tagger_mod

    async def fake_rename(mix_id, *, reason, dry_run=False):
        if seen is not None:
            seen.append(mix_id)
        return {
            "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
            "renamed": [], "skipped": [], "errors": [],
        }

    async def fake_tag(mix_id, *, reason, dry_run=False):
        return {"status": "dry_run" if dry_run else "ok", "actions": [], "reason": reason}

    monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
    monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)


class TestDeterministicOrder:
    async def test_order_is_stable_across_repeated_calls_even_with_ties(
        self, prepared_db, monkeypatch
    ):
        """Three mixes share the SAME ``created_at`` (a real scenario: mixes
        inserted in the same tick by the back-catalog sync). Run the
        selection, then delete and re-insert all three rows in the OPPOSITE
        order (same ids, same created_at) -- which perturbs whatever
        insertion-order/rowid artifact SQLite would otherwise fall back on --
        and run the selection again. With ``Mix.id`` as an explicit tiebreak,
        the result must be identical both times."""
        _noop_fakes(monkeypatch)

        tie = _dt(1)
        await _make_mix(id="mix-tie-1", created_at=tie)
        await _make_mix(id="mix-tie-2", created_at=tie)
        await _make_mix(id="mix-tie-3", created_at=tie)

        await catalog._run_retag(dry_run=True, limit=None)
        first_order = [r["mix_id"] for r in catalog._retag_state["results"]]

        # Perturb physical/insertion order without changing any created_at
        # or id value.
        for mix_id in ("mix-tie-1", "mix-tie-2", "mix-tie-3"):
            await _delete_mix(mix_id)
        for mix_id in ("mix-tie-3", "mix-tie-2", "mix-tie-1"):
            await _make_mix(id=mix_id, created_at=tie)

        await catalog._run_retag(dry_run=True, limit=None)
        second_order = [r["mix_id"] for r in catalog._retag_state["results"]]

        assert first_order == ["mix-tie-1", "mix-tie-2", "mix-tie-3"]
        assert second_order == first_order


class TestOffsetPaginationDisjoint:
    async def test_limit_offset_batches_are_disjoint_and_cover_all_candidates(
        self, prepared_db, monkeypatch
    ):
        """The core bug: batch one (``limit=2, offset=0``) and batch two
        (``limit=2, offset=2``) must select DISJOINT sets, and their union
        must be every candidate -- not the same rows re-selected twice.

        Insertion order is deliberately the REVERSE of ``created_at`` order
        (mix-d inserted first, mix-a inserted last) so that this can't pass
        by accident because SQLite happens to return rows in insertion
        order -- only an explicit ``ORDER BY created_at, id`` produces the
        expected ascending slices.
        """
        _noop_fakes(monkeypatch)

        await _make_mix(id="mix-d", created_at=_dt(4))
        await _make_mix(id="mix-c", created_at=_dt(3))
        await _make_mix(id="mix-b", created_at=_dt(2))
        await _make_mix(id="mix-a", created_at=_dt(1))

        await catalog._run_retag(dry_run=True, limit=2, offset=0)
        batch1 = [r["mix_id"] for r in catalog._retag_state["results"]]

        await catalog._run_retag(dry_run=True, limit=2, offset=2)
        batch2 = [r["mix_id"] for r in catalog._retag_state["results"]]

        assert batch1 == ["mix-a", "mix-b"]
        assert batch2 == ["mix-c", "mix-d"]
        assert set(batch1).isdisjoint(set(batch2))
        assert set(batch1) | set(batch2) == {"mix-a", "mix-b", "mix-c", "mix-d"}


class TestCandidateCountReflectsPagination:
    async def test_post_response_candidates_reflects_limit_and_offset(
        self, client, monkeypatch
    ):
        """The pre-flight ``candidates`` count in the POST response must
        reflect ``limit``/``offset``, not the unfiltered total -- otherwise
        the operator's go/no-go number lies about what the run will touch."""
        _noop_fakes(monkeypatch)

        for day, mix_id in enumerate(["mix-1", "mix-2", "mix-3", "mix-4", "mix-5"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        resp = await client.post("/api/catalog/retag", params={"limit": 2, "offset": 3})
        assert resp.status_code == 202
        body = resp.json()
        # 5 total candidates, offset 3 leaves 2 (mix-4, mix-5), limit 2 keeps both.
        assert body["candidates"] == 2
        assert body["offset"] == 3

        await asyncio.wait_for(catalog._retag_task, timeout=5)

    async def test_post_response_candidates_smaller_than_unfiltered_total(
        self, client, monkeypatch
    ):
        """A plain COUNT(*) ignoring limit/offset would over-report here."""
        _noop_fakes(monkeypatch)

        for day, mix_id in enumerate(["mix-1", "mix-2", "mix-3"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        resp = await client.post("/api/catalog/retag", params={"limit": 1, "offset": 0})
        assert resp.status_code == 202
        assert resp.json()["candidates"] == 1  # not 3

        await asyncio.wait_for(catalog._retag_task, timeout=5)


class TestCursorPersistedPerMix:
    async def test_cursor_persisted_after_each_mix_not_only_at_end(
        self, prepared_db, monkeypatch
    ):
        """After the FIRST mix (before the run has touched the second), the
        persisted cursor must already reflect it -- persisting only at the
        end would defeat the entire point of a resumable backfill (a
        ``kill -9`` mid-run would leave nothing usable)."""
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        await _make_mix(id="mix-a", created_at=_dt(1))
        await _make_mix(id="mix-b", created_at=_dt(2))

        mid_run_progress = {}

        async def fake_rename(mix_id, *, reason, dry_run=False):
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

        async def fake_tag(mix_id, *, reason, dry_run=False):
            if mix_id == "mix-b":
                # By the time the SECOND mix starts tagging, the cursor from
                # the FIRST mix must already be committed and readable.
                mid_run_progress.update(await _get_progress() or {})
            return {"status": "ok", "actions": [], "reason": reason}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        await catalog._run_retag(dry_run=False, limit=None)

        assert mid_run_progress.get("last_mix_id") == "mix-a"
        assert mid_run_progress.get("processed") == 1
        assert mid_run_progress.get("total") == 2
        assert mid_run_progress.get("dry_run") is False
        assert "updated_at" in mid_run_progress

        final_progress = await _get_progress()
        assert final_progress["last_mix_id"] == "mix-b"
        assert final_progress["processed"] == 2


class TestResume:
    async def test_resume_true_continues_after_last_mix_id(self, client, monkeypatch):
        """A run stopped early (``limit=2``) leaves a cursor at the second
        mix. A follow-up call with ``resume=true`` (no limit/offset) must
        continue with the mixes AFTER that cursor, not restart from zero and
        not repeat mix-a/mix-b.

        Both calls are explicit REAL runs (``dry_run=False``) -- see
        Blocker 1: a dry run's cursor can never resume a real run (and vice
        versa), so the setup call here must actually be the same mode as the
        resuming call, or the resume below would hit that mode-mismatch
        guard instead of exercising cursor continuation at all. The original
        version of this test passed only ``limit=2`` to the first call,
        which defaults to ``dry_run=True`` -- so what it actually proved was
        that a DRY run advances a REAL run's cursor, not what its name
        claims.
        """
        seen = []
        _noop_fakes(monkeypatch, seen=seen)

        for day, mix_id in enumerate(["mix-a", "mix-b", "mix-c", "mix-d"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        first = await client.post(
            "/api/catalog/retag", params={"dry_run": "false", "limit": 2}
        )
        assert first.status_code == 202
        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-a", "mix-b"]

        progress = await _get_progress()
        assert progress["last_mix_id"] == "mix-b"
        assert progress["dry_run"] is False

        seen.clear()
        second = await client.post(
            "/api/catalog/retag", params={"dry_run": "false", "resume": "true"}
        )
        assert second.status_code == 202
        body = second.json()
        assert body["offset"] == 2
        assert body["candidates"] == 2
        assert body["resume_cursor_ignored"] is False

        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-c", "mix-d"]

    async def test_resume_true_ignores_a_cursor_from_the_opposite_mode(
        self, client, monkeypatch
    ):
        """Blocker 1: the README's own documented flow is a bare (dry-run)
        POST over the whole backlog, followed by a real run. A REAL run's
        ``resume=true`` must NOT resolve a cursor a DRY run wrote -- a dry
        run never touches a single file, so resuming past its cursor would
        make the real run believe everything up to it was already done and
        touch nothing at all, while still reporting ``{"started": true}``.
        """
        seen = []
        _noop_fakes(monkeypatch, seen=seen)

        for day, mix_id in enumerate(["mix-a", "mix-b", "mix-c"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        # Bare POST -- dry_run defaults to True -- over the whole backlog.
        dry = await client.post("/api/catalog/retag")
        assert dry.status_code == 202
        assert dry.json()["dry_run"] is True
        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-a", "mix-b", "mix-c"]

        progress = await _get_progress()
        assert progress["last_mix_id"] == "mix-c"
        assert progress["dry_run"] is True

        # Now the real run, resuming. If the dry run's cursor governed it,
        # this would compute offset=3 (past every mix) and touch nothing.
        seen.clear()
        real = await client.post(
            "/api/catalog/retag", params={"dry_run": "false", "resume": "true"}
        )
        assert real.status_code == 202
        body = real.json()
        assert body["resume_cursor_ignored"] is True
        assert body["offset"] == 0
        assert body["candidates"] == 3

        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-a", "mix-b", "mix-c"], (
            "the real run must actually touch every mix -- a dry run's "
            "cursor must never make a real run silently do nothing"
        )

        # Gate 3: the refusal must also be durable, not just in the HTTP
        # response -- an operator reading the activity log after the
        # process is gone must be able to tell a refused resume from a
        # fresh start. Two runs happened (the bare dry run, then the real
        # resume) -- ``query`` returns newest-first, so the real run's
        # start event is items[0].
        from app.services import activity_log

        items, total = await activity_log.query(event="retag_run_started")
        assert total == 2
        assert items[0]["context"]["dry_run"] is False
        assert items[0]["context"]["resume_cursor_ignored"] is True

    async def test_resume_true_with_no_saved_cursor_starts_at_beginning(
        self, client, monkeypatch
    ):
        """No prior run means no cursor in AppSettings at all.
        ``resume=true`` on a clean slate must start from the beginning and
        must NOT raise."""
        seen = []
        _noop_fakes(monkeypatch, seen=seen)

        for day, mix_id in enumerate(["mix-x", "mix-y"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        resp = await client.post("/api/catalog/retag", params={"resume": "true"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["offset"] == 0
        assert body["candidates"] == 2

        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-x", "mix-y"]

    async def test_resume_true_with_stale_cursor_mix_deleted_starts_at_beginning(
        self, client, monkeypatch
    ):
        """The cursor points at a mix that no longer exists in the candidate
        set (e.g. deleted between runs). Resolving the cursor to an offset
        must not raise -- it must fall back to the beginning."""
        seen = []
        _noop_fakes(monkeypatch, seen=seen)

        for day, mix_id in enumerate(["mix-p", "mix-q"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        from app.database import async_session_factory
        from app.models import AppSettings

        async with async_session_factory() as session:
            row = AppSettings(
                id=1,
                settings_json={
                    catalog.RETAG_PROGRESS_KEY: {
                        "last_mix_id": "mix-does-not-exist",
                        "processed": 1,
                        "total": 5,
                        "dry_run": True,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )
            session.add(row)
            await session.commit()

        resp = await client.post("/api/catalog/retag", params={"resume": "true"})
        assert resp.status_code == 202
        assert resp.json()["offset"] == 0

        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-p", "mix-q"]


class TestCursorSkipsNonTerminalOutcomes:
    async def test_failed_tag_does_not_advance_cursor_and_is_retried_on_resume(
        self, client, monkeypatch
    ):
        """Blocker 2: a mix whose tag write comes back ``{"status": "failed"}``
        must NOT have the resume cursor advanced past it -- otherwise a
        later ``resume=true`` skips it forever instead of retrying the
        failed write. The run itself must still continue past it to the
        next mix in the same pass (not advancing the cursor must never
        stall the run)."""
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        for day, mix_id in enumerate(["mix-a", "mix-b", "mix-c"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        seen = []

        async def fake_rename(mix_id, *, reason, dry_run=False):
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

        async def fake_tag(mix_id, *, reason, dry_run=False):
            seen.append(mix_id)
            if mix_id == "mix-b":
                return {"status": "failed", "actions": [], "reason": "disk full"}
            return {"status": "ok", "actions": [], "reason": reason}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        first = await client.post("/api/catalog/retag", params={"dry_run": "false"})
        assert first.status_code == 202
        await asyncio.wait_for(catalog._retag_task, timeout=5)
        # The run itself must not stall on the failure -- all three are seen.
        assert seen == ["mix-a", "mix-b", "mix-c"]

        # mix-b failed -- the persisted cursor must be stuck at mix-a, the
        # last TERMINAL-success mix, not mix-c.
        progress = await _get_progress()
        assert progress["last_mix_id"] == "mix-a"

        seen.clear()
        second = await client.post(
            "/api/catalog/retag", params={"dry_run": "false", "resume": "true"}
        )
        assert second.status_code == 202
        body = second.json()
        assert body["offset"] == 1
        assert body["candidates"] == 2

        await asyncio.wait_for(catalog._retag_task, timeout=5)
        # mix-b (the previously-failed one) is retried, not skipped forever.
        assert seen == ["mix-b", "mix-c"]

    async def test_skip_for_insufficient_space_freezes_the_cursor_at_the_prior_mix(
        self, client, monkeypatch
    ):
        """Same guarantee as above, for the other reproduced case: a mix
        skipped for a condition that can resolve itself (here, insufficient
        free space) must also not advance the cursor -- ``skipped`` is NOT
        automatically terminal-success, only ``skipped`` with reason
        ``"already tagged"`` is.

        Three mixes: mix-a genuinely succeeds (cursor should reach it),
        mix-b is skipped for space (the cursor must freeze here), mix-c
        then also succeeds -- but the cursor must NOT advance to mix-c
        either. The cursor is an OFFSET into the ordered candidate list, so
        if mix-c's success pushed it forward, a future resume would resolve
        past mix-b's position and silently skip it anyway, even though
        nothing was ever technically "persisted for mix-b" -- this is the
        subtler version of the same bug the mutation check below exists to
        catch.
        """
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        for day, mix_id in enumerate(["mix-a", "mix-b", "mix-c"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        async def fake_rename(mix_id, *, reason, dry_run=False):
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

        async def fake_tag(mix_id, *, reason, dry_run=False):
            if mix_id == "mix-b":
                return {
                    "status": "skipped", "actions": [],
                    "reason": "insufficient free space to rewrite /x/b.flac safely",
                }
            return {"status": "ok", "actions": [], "reason": reason}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        resp = await client.post("/api/catalog/retag", params={"dry_run": "false"})
        assert resp.status_code == 202
        await asyncio.wait_for(catalog._retag_task, timeout=5)

        progress = await _get_progress()
        assert progress["last_mix_id"] == "mix-a"

        # And a subsequent resume must actually revisit mix-b (and, as a
        # cheap side effect of the offset-based cursor, mix-c too) rather
        # than resolving straight past both of them.
        second = await client.post(
            "/api/catalog/retag", params={"dry_run": "false", "resume": "true"}
        )
        assert second.status_code == 202
        body = second.json()
        assert body["offset"] == 1
        assert body["candidates"] == 2

    async def test_disabled_tag_on_a_real_run_does_not_advance_cursor(
        self, client, monkeypatch
    ):
        """Gate 1 / Blocker 1 restated: a real run whose tag leg comes back
        ``"disabled"`` (``tag_source_files`` is off) must NOT have its
        cursor advanced past that mix. Before this fix, ``"disabled"`` was
        treated as terminal-success for a real run -- an operator running
        the real backfill with the flag off would see the cursor race to
        the end of the backlog (recorded under ``dry_run=False``, so the
        mode guard doesn't catch it), then enable the flag and
        ``resume=true`` into ``{"candidates": 0}`` with zero files ever
        touched.
        """
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        for day, mix_id in enumerate(["mix-a", "mix-b", "mix-c"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        seen = []

        async def fake_rename(mix_id, *, reason, dry_run=False):
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [], "skipped": [], "errors": [],
            }

        async def fake_tag(mix_id, *, reason, dry_run=False):
            seen.append(mix_id)
            if mix_id == "mix-b":
                return {
                    "status": "disabled", "actions": [],
                    "reason": "tag_source_files is off",
                }
            return {"status": "ok", "actions": [], "reason": reason}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        first = await client.post("/api/catalog/retag", params={"dry_run": "false"})
        assert first.status_code == 202
        await asyncio.wait_for(catalog._retag_task, timeout=5)
        # The run itself still covers every candidate in this same pass.
        assert seen == ["mix-a", "mix-b", "mix-c"]

        # mix-b came back disabled -- the persisted cursor must be stuck at
        # mix-a, the last TERMINAL-success mix, not mix-c.
        progress = await _get_progress()
        assert progress["last_mix_id"] == "mix-a"

        # The setting is now enabled -- resume must revisit mix-b, not
        # believe it was already handled.
        seen.clear()

        async def fake_tag_enabled(mix_id, *, reason, dry_run=False):
            seen.append(mix_id)
            return {"status": "ok", "actions": [], "reason": reason}

        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag_enabled)

        second = await client.post(
            "/api/catalog/retag", params={"dry_run": "false", "resume": "true"}
        )
        assert second.status_code == 202
        body = second.json()
        assert body["offset"] == 1
        assert body["candidates"] == 2

        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-b", "mix-c"], (
            "the previously-disabled mix must be retried once the setting "
            "is enabled, not skipped forever"
        )

    async def test_failed_rename_does_not_advance_cursor_even_when_tag_succeeds(
        self, client, monkeypatch
    ):
        """Gate 2: the rename leg was absent from the OLD cursor decision --
        it inspected only the tag outcome. A mix whose rename comes back
        with ``errors`` (e.g. ``permission_denied``) but whose tag succeeds
        must NOT advance the cursor, or a later ``resume=true`` never
        retries the failed rename."""
        import app.services.source_renamer as renamer_mod
        import app.services.source_tagger as tagger_mod

        for day, mix_id in enumerate(["mix-a", "mix-b", "mix-c"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        seen = []

        async def fake_rename(mix_id, *, reason, dry_run=False):
            seen.append(mix_id)
            if mix_id == "mix-b":
                return {
                    "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                    "renamed": [], "skipped": [],
                    "errors": [{"path": "/x/b.flac", "reason": "permission_denied"}],
                }
            return {
                "mix_id": mix_id, "reason": reason, "dry_run": dry_run,
                "renamed": [{"from": "old.flac", "to": "new.flac"}],
                "skipped": [], "errors": [],
            }

        async def fake_tag(mix_id, *, reason, dry_run=False):
            # Tag succeeds for EVERY mix, including mix-b -- the rename
            # failure is the only thing wrong with it.
            return {"status": "ok", "actions": [], "reason": reason}

        monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
        monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)

        first = await client.post("/api/catalog/retag", params={"dry_run": "false"})
        assert first.status_code == 202
        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-a", "mix-b", "mix-c"]

        progress = await _get_progress()
        assert progress["last_mix_id"] == "mix-a"

        seen.clear()
        second = await client.post(
            "/api/catalog/retag", params={"dry_run": "false", "resume": "true"}
        )
        assert second.status_code == 202
        body = second.json()
        assert body["offset"] == 1
        assert body["candidates"] == 2

        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-b", "mix-c"], (
            "mix-b's failed rename must be retried on resume even though "
            "its tag leg already succeeded"
        )
