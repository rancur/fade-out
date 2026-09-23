"""Durable per-mix outcomes for the retag backfill in the activity log.

``POST /api/catalog/retag`` rewrites ~80 irreplaceable multi-gigabyte FLAC
recordings (~498 GB). Before this change its results lived ONLY in the
module-level ``_retag_state`` dict, wiped on process restart -- after an
unattended run (or any run followed by a container restart) there was no way
to answer "what did it actually do to my files?" These tests exercise the
real activity log (``app.services.activity_log.query``), not just source
text, since a bug here means the durable record silently doesn't match what
actually happened.

FIXTURE WARNING (see task brief): ``source_renamer.rename_sources_for_mix``
returns ``{mix_id, reason, dry_run, renamed, skipped, errors}`` with NO
top-level ``status`` key; ``source_tagger.tag_sources_for_mix`` DOES return
``{"status", "actions", "reason"}``. The fakes below use exactly these real
shapes.
"""

import sys
import types

# Sandbox-only workaround: this environment doesn't have the `shazamio`
# dependency installed (a real dependency of app/services/audio_analyzer.py
# and app/services/shorts_pipeline.py, unrelated to this task). The `client`
# fixture imports `app.main`, which eagerly imports every router including
# `shorts.py`, so without this the HTTP-level tests here can't even collect.
# This stubs the module in THIS process's sys.modules only (no pip install,
# nothing touches the real environment) and is a no-op if shazamio is
# actually installed. Mirrors test_retag_pagination_resume.py.
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

from datetime import datetime

from app.routers import catalog
from app.services import activity_log


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


def _ok_rename(mix_id: str, *, dry_run: bool = False):
    """Real shape from source_renamer.rename_sources_for_mix on a clean run --
    NO top-level ``status`` key."""
    return {
        "mix_id": mix_id, "reason": "retag_backfill", "dry_run": dry_run,
        "renamed": [{"from": "old.flac", "to": "new.flac"}], "skipped": [], "errors": [],
    }


def _failed_rename(mix_id: str, *, dry_run: bool = False):
    return {
        "mix_id": mix_id, "reason": "retag_backfill", "dry_run": dry_run,
        "renamed": [], "skipped": [],
        "errors": [{"path": "/x/old.flac", "error": "disk full"}],
    }


def _ok_tag(dry_run: bool = False):
    """Real shape from source_tagger.tag_sources_for_mix on a clean run."""
    return {"status": "dry_run" if dry_run else "ok", "actions": [], "reason": "no changes made"}


def _failed_tag(dry_run: bool = False):
    return {"status": "failed", "actions": [], "reason": "disk full"}


def _wire_fakes(monkeypatch, rename_by_mix=None, tag_by_mix=None):
    """Patch renamer/tagger to return per-mix canned outcomes (defaulting to
    a clean OK result), using the real return shapes each function has."""
    import app.services.source_renamer as renamer_mod
    import app.services.source_tagger as tagger_mod

    rename_by_mix = rename_by_mix or {}
    tag_by_mix = tag_by_mix or {}

    async def fake_rename(mix_id, *, reason, dry_run=False):
        maker = rename_by_mix.get(mix_id, _ok_rename)
        return maker(mix_id, dry_run=dry_run)

    async def fake_tag(mix_id, *, reason, dry_run=False):
        maker = tag_by_mix.get(mix_id, _ok_tag)
        return maker(dry_run=dry_run)

    monkeypatch.setattr(renamer_mod, "rename_sources_for_mix", fake_rename)
    monkeypatch.setattr(tagger_mod, "tag_sources_for_mix", fake_tag)


class TestRunStartedEvent:
    async def test_start_event_emitted_with_run_parameters(
        self, prepared_db, monkeypatch
    ):
        _wire_fakes(monkeypatch)
        await _make_mix(id="mix-a", created_at=_dt(1))
        await _make_mix(id="mix-b", created_at=_dt(2))
        await _make_mix(id="mix-c", created_at=_dt(3))

        await catalog._run_retag(
            dry_run=True, limit=2, offset=1, resume=True
        )

        items, total = await activity_log.query(event="retag_run_started")
        assert total == 1
        assert len(items) == 1
        ctx = items[0]["context"]
        assert ctx["dry_run"] is True
        assert ctx["limit"] == 2
        assert ctx["offset"] == 1
        assert ctx["resume"] is True
        # offset=1, limit=2 over 3 candidates (mix-a, mix-b, mix-c) -> 2 left.
        assert ctx["candidates"] == 2
        assert items[0]["level"] == "info"

    async def test_start_event_records_a_refused_resume_cursor(
        self, prepared_db, monkeypatch
    ):
        """Gate 3: the HTTP response surfaces a mode-mismatched resume
        cursor as ``resume_cursor_ignored``, but that only exists for the
        life of the request/response cycle. An operator reading the
        DURABLE log after the process is gone must be able to tell a
        refused resume from a fresh start -- the ``retag_run_started``
        event's context must carry it too, not just
        ``dry_run/limit/offset/resume/candidates``."""
        _wire_fakes(monkeypatch)
        await _make_mix(id="mix-a", created_at=_dt(1))

        await catalog._run_retag(
            dry_run=False, limit=None, offset=0, resume=True,
            resume_cursor_ignored=True,
        )

        items, total = await activity_log.query(event="retag_run_started")
        assert total == 1
        assert items[0]["context"]["resume_cursor_ignored"] is True

    async def test_start_event_records_a_non_refused_resume_as_false(
        self, prepared_db, monkeypatch
    ):
        """The other side of the same field: a resume that WAS honoured
        must record ``resume_cursor_ignored: False``, not just omit the
        key -- an absent key and an explicit False look identical to a
        naive reader unless the field is always present."""
        _wire_fakes(monkeypatch)
        await _make_mix(id="mix-a", created_at=_dt(1))

        await catalog._run_retag(
            dry_run=False, limit=None, offset=0, resume=True,
            resume_cursor_ignored=False,
        )

        items, total = await activity_log.query(event="retag_run_started")
        assert total == 1
        assert items[0]["context"]["resume_cursor_ignored"] is False


class TestPerMixEvent:
    async def test_each_processed_mix_emits_exactly_one_event_with_both_statuses(
        self, prepared_db, monkeypatch
    ):
        _wire_fakes(monkeypatch)
        mix_ids = ["mix-a", "mix-b", "mix-c"]
        for day, mix_id in enumerate(mix_ids, start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        await catalog._run_retag(dry_run=False, limit=None)

        items, total = await activity_log.query(
            event="retag_mix_processed", limit=100
        )
        # Exactly one event per mix -- not "at least one".
        assert total == len(mix_ids)
        assert len(items) == len(mix_ids)

        seen_mix_ids = {item["mix_id"] for item in items}
        assert seen_mix_ids == set(mix_ids)

        for item in items:
            ctx = item["context"]
            assert "rename_status" in ctx
            assert "tag_status" in ctx
            assert ctx["rename_status"] == "ok"
            assert ctx["tag_status"] == "ok"

    async def test_event_context_carries_both_reason_fields(
        self, prepared_db, monkeypatch
    ):
        """Blocker 3: the status alone doesn't say WHY -- two mixes both
        showing ``tag_status: "skipped"`` could be one already-tagged (fine)
        and one out of disk space (alarming), indistinguishable without the
        reason string. ``retag_mix_processed`` must carry
        ``rename_reason``/``tag_reason`` alongside the statuses, not just
        the statuses on their own."""
        await _make_mix(id="mix-a", created_at=_dt(1))

        def _rename_with_reason(mix_id, *, dry_run=False):
            return {
                "mix_id": mix_id, "reason": "retag_backfill", "dry_run": dry_run,
                "renamed": [], "skipped": [{"path": "/x/a.flac", "reason": "no_title"}],
                "errors": [],
            }

        def _tag_out_of_space(dry_run=False):
            return {
                "status": "skipped", "actions": [],
                "reason": "insufficient free space to rewrite /x/a.flac safely",
            }

        _wire_fakes(
            monkeypatch,
            rename_by_mix={"mix-a": _rename_with_reason},
            tag_by_mix={"mix-a": _tag_out_of_space},
        )

        await catalog._run_retag(dry_run=False, limit=None)

        items, total = await activity_log.query(event="retag_mix_processed")
        assert total == 1
        ctx = items[0]["context"]
        assert ctx["rename_status"] == "skipped"
        assert ctx["rename_reason"] == "no_title"
        assert ctx["tag_status"] == "skipped"
        assert ctx["tag_reason"] == (
            "insufficient free space to rewrite /x/a.flac safely"
        )


class TestLevelByOutcome:
    async def test_failed_leg_emits_at_error_level_clean_mix_emits_at_info(
        self, prepared_db, monkeypatch
    ):
        await _make_mix(id="mix-clean", created_at=_dt(1))
        await _make_mix(id="mix-rename-failed", created_at=_dt(2))
        await _make_mix(id="mix-tag-failed", created_at=_dt(3))

        _wire_fakes(
            monkeypatch,
            rename_by_mix={"mix-rename-failed": _failed_rename},
            tag_by_mix={"mix-tag-failed": _failed_tag},
        )

        await catalog._run_retag(dry_run=False, limit=None)

        items, _ = await activity_log.query(event="retag_mix_processed", limit=100)
        by_mix = {item["mix_id"]: item for item in items}

        assert by_mix["mix-clean"]["level"] == "info"
        assert by_mix["mix-rename-failed"]["level"] == "error"
        assert by_mix["mix-rename-failed"]["context"]["rename_status"] == "failed"
        assert by_mix["mix-tag-failed"]["level"] == "error"
        assert by_mix["mix-tag-failed"]["context"]["tag_status"] == "failed"


class TestCompletionEvent:
    async def test_completion_event_carries_final_summary(
        self, prepared_db, monkeypatch
    ):
        await _make_mix(id="mix-clean", created_at=_dt(1))
        await _make_mix(id="mix-rename-failed", created_at=_dt(2))

        _wire_fakes(
            monkeypatch, rename_by_mix={"mix-rename-failed": _failed_rename}
        )

        await catalog._run_retag(dry_run=False, limit=None)

        items, total = await activity_log.query(event="retag_run_completed")
        assert total == 1
        item = items[0]
        assert item["context"]["summary"] == catalog._retag_state["summary"]
        assert item["context"]["processed"] == 2
        assert item["context"]["total"] == 2
        # At least one leg failed (the rename on mix-rename-failed) -> not a
        # silent "info" completion for a run that actually lost work.
        assert item["level"] == "warn"

    async def test_alarming_skip_with_no_failures_still_warns(
        self, prepared_db, monkeypatch
    ):
        """Blocker 3, the other half: NOTHING failed here -- every rename
        and every tag call returns cleanly -- but one tag was skipped for
        insufficient free space, not because it was already tagged. That
        must still warn: ``{skipped: N}`` alone can't tell "every file was
        already tagged" (fine) from "every file was skipped for want of
        disk" (alarming) apart, which is the entire reason
        ``any_alarming_skip`` exists. A completion event that only checked
        ``summary.get("failed")`` would silently report "info" here."""
        await _make_mix(id="mix-a", created_at=_dt(1))

        def _tag_out_of_space(dry_run=False):
            return {
                "status": "skipped", "actions": [],
                "reason": "insufficient free space to rewrite /x/a.flac safely",
            }

        _wire_fakes(monkeypatch, tag_by_mix={"mix-a": _tag_out_of_space})

        await catalog._run_retag(dry_run=False, limit=None)

        items, total = await activity_log.query(event="retag_run_completed")
        assert total == 1
        item = items[0]
        assert item["context"]["summary"].get("failed", 0) == 0
        assert item["context"]["any_alarming_skip"] is True
        assert item["level"] == "warn"


class TestActivityLogFailureDoesNotAbortRun:
    async def test_activity_log_raising_does_not_abort_the_run(
        self, prepared_db, monkeypatch
    ):
        """An activity-log failure is a worse-but-tolerable outcome next to
        losing a 498 GB unattended run. Patch ``activity_log.log`` itself (the
        function every info/warn/error wrapper and ``_emit_retag_activity``
        ultimately calls) to raise on every call, and confirm the backfill
        still runs every mix to completion with a fully-populated
        ``_retag_state`` -- the log failure must be swallowed, not merely
        logged past."""
        _wire_fakes(monkeypatch)
        mix_ids = ["mix-a", "mix-b", "mix-c"]
        for day, mix_id in enumerate(mix_ids, start=1):
            await _make_mix(id=mix_id, created_at=_dt(day))

        async def _raise(*args, **kwargs):
            raise RuntimeError("activity log is down")

        monkeypatch.setattr(activity_log, "log", _raise)

        await catalog._run_retag(dry_run=False, limit=None)

        assert catalog._retag_state["running"] is False
        assert catalog._retag_state["processed"] == len(mix_ids)
        assert catalog._retag_state["total"] == len(mix_ids)
        assert len(catalog._retag_state["results"]) == len(mix_ids)
        assert {r["mix_id"] for r in catalog._retag_state["results"]} == set(mix_ids)
        # The summary still reconciles even though every log call blew up.
        assert sum(catalog._retag_state["summary"].values()) == 2 * len(mix_ids)

        # And, naturally, nothing landed in the activity log at all.
        items, total = await activity_log.query(event="retag_run_completed")
        assert total == 0
