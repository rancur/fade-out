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
        not repeat mix-a/mix-b."""
        seen = []
        _noop_fakes(monkeypatch, seen=seen)

        for day, mix_id in enumerate(["mix-a", "mix-b", "mix-c", "mix-d"], start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        first = await client.post("/api/catalog/retag", params={"limit": 2})
        assert first.status_code == 202
        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-a", "mix-b"]

        progress = await _get_progress()
        assert progress["last_mix_id"] == "mix-b"

        seen.clear()
        second = await client.post("/api/catalog/retag", params={"resume": "true"})
        assert second.status_code == 202
        body = second.json()
        assert body["offset"] == 2
        assert body["candidates"] == 2

        await asyncio.wait_for(catalog._retag_task, timeout=5)
        assert seen == ["mix-c", "mix-d"]

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
