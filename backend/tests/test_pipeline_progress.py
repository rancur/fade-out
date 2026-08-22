"""Tests for live step progress (A1) and pipeline accuracy (A2).

Covers: report_progress throttling + WS event shape, the milestone activity
entries, per-step progress_cb handler wiring, _record_step upsert semantics,
and the boot-time interrupted sweep.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import app.services.pipeline as pipeline_mod
from app.models import Mix, PipelineStep
from app.services.pipeline import (
    PipelineOrchestrator,
    StepStatus,
    sweep_interrupted_at_boot,
)


class FakeTime:
    """Deterministic stand-in for the pipeline module's ``time``."""

    def __init__(self, now: float = 1000.0):
        self._now = now

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def fake_time(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(pipeline_mod, "time", ft)
    return ft


def _collect_events(orch):
    events = []

    def listener(event_type, mix_id, data):
        events.append({"event": event_type, "mix_id": mix_id, "data": data})

    orch.on_event(listener)
    return events


def _progress_events(events):
    return [e for e in events if e["event"] == "step_progress"]


async def _make_mix_with_step(mix_id="m1", step_name="upload_soundcloud"):
    from app.database import async_session_factory

    async with async_session_factory() as session:
        session.add(Mix(id=mix_id, title="Test", pipeline_status="running"))
        session.add(PipelineStep(mix_id=mix_id, step_name=step_name, status="pending"))
        await session.commit()


async def _get_step(mix_id, step_name):
    from app.database import async_session_factory

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(PipelineStep).where(
                    PipelineStep.mix_id == mix_id,
                    PipelineStep.step_name == step_name,
                )
            )
        ).scalars().all()
        return rows


class TestReportProgressThrottle:
    async def test_first_report_emits_ws_event_with_contract_shape(
        self, prepared_db, fake_time
    ):
        orch = PipelineOrchestrator()
        events = _collect_events(orch)

        await orch.report_progress("m1", "upload_soundcloud", 10, "1.2 GB / 2.9 GB")

        progress = _progress_events(events)
        assert progress == [
            {
                "event": "step_progress",
                "mix_id": "m1",
                "data": {
                    "step": "upload_soundcloud",
                    "progress": 10,
                    "detail": "1.2 GB / 2.9 GB",
                },
            }
        ]

    async def test_emits_at_most_once_per_second_per_step(self, prepared_db, fake_time):
        orch = PipelineOrchestrator()
        events = _collect_events(orch)

        await orch.report_progress("m1", "upload_soundcloud", 1)
        await orch.report_progress("m1", "upload_soundcloud", 2)
        await orch.report_progress("m1", "upload_soundcloud", 3)
        assert len(_progress_events(events)) == 1

        fake_time.advance(1.1)
        await orch.report_progress("m1", "upload_soundcloud", 4)
        assert len(_progress_events(events)) == 2

    async def test_throttle_is_per_mix_step_pair(self, prepared_db, fake_time):
        orch = PipelineOrchestrator()
        events = _collect_events(orch)

        await orch.report_progress("m1", "upload_soundcloud", 1)
        await orch.report_progress("m1", "upload_youtube", 1)
        await orch.report_progress("m2", "upload_soundcloud", 1)
        assert len(_progress_events(events)) == 3

    async def test_final_100_percent_always_emits(self, prepared_db, fake_time):
        orch = PipelineOrchestrator()
        events = _collect_events(orch)

        await orch.report_progress("m1", "upload_soundcloud", 98)
        await orch.report_progress("m1", "upload_soundcloud", 100, "done")
        progress = _progress_events(events)
        assert len(progress) == 2
        assert progress[-1]["data"]["progress"] == 100

    async def test_db_row_updated_at_most_every_5s(self, prepared_db, fake_time):
        await _make_mix_with_step()
        orch = PipelineOrchestrator()

        await orch.report_progress("m1", "upload_soundcloud", 10, "d10")
        (row,) = await _get_step("m1", "upload_soundcloud")
        assert row.progress == 10
        assert row.progress_detail == "d10"

        # Within the 5s window: WS may emit, DB must not churn.
        fake_time.advance(1.5)
        await orch.report_progress("m1", "upload_soundcloud", 20, "d20")
        (row,) = await _get_step("m1", "upload_soundcloud")
        assert row.progress == 10

        fake_time.advance(5.0)
        await orch.report_progress("m1", "upload_soundcloud", 30, "d30")
        (row,) = await _get_step("m1", "upload_soundcloud")
        assert row.progress == 30
        assert row.progress_detail == "d30"

    async def test_never_raises_without_db_row(self, prepared_db, fake_time):
        orch = PipelineOrchestrator()
        # No mix / step rows exist — must be silently tolerated.
        await orch.report_progress("ghost", "upload_soundcloud", 50, "x")

    async def test_milestone_crossings_write_activity_entries(
        self, prepared_db, fake_time
    ):
        from app.services import activity_log

        orch = PipelineOrchestrator()
        await orch.report_progress("m1", "upload_soundcloud", 10)
        fake_time.advance(2)
        await orch.report_progress("m1", "upload_soundcloud", 26, "700 MB / 2.9 GB")
        fake_time.advance(2)
        await orch.report_progress("m1", "upload_soundcloud", 30)  # still 25-band

        items, total = await activity_log.query(event="progress_milestone")
        assert total == 1
        assert "25%" in items[0]["message"]
        assert "700 MB / 2.9 GB" in items[0]["message"]


class TestProgressCallbackWiring:
    async def test_handler_accepting_progress_cb_receives_it(self, prepared_db, fake_time):
        orch = PipelineOrchestrator()
        events = _collect_events(orch)
        await _make_mix_with_step("m1", "detect")

        async def handler(mix_id, session, progress_cb=None):
            assert progress_cb is not None
            await progress_cb(50, "halfway")
            return {"ok": True}

        orch.register_handler("detect", handler)
        ok = await orch._execute_step("m1", "detect")
        assert ok.ok is True

        progress = _progress_events(events)
        assert progress and progress[0]["data"] == {
            "step": "detect", "progress": 50, "detail": "halfway",
        }

    async def test_legacy_two_arg_handler_still_works(self, prepared_db, fake_time):
        orch = PipelineOrchestrator()
        await _make_mix_with_step("m1", "detect")

        async def legacy_handler(mix_id, session):
            return {"legacy": True}

        orch.register_handler("detect", legacy_handler)
        ok = await orch._execute_step("m1", "detect")
        assert ok.ok is True
        (row,) = await _get_step("m1", "detect")
        assert row.status == "completed"
        assert row.output_json == {"legacy": True}


class TestRecordStepUpsert:
    async def test_initial_pending_row_is_updated_not_duplicated(self, prepared_db):
        await _make_mix_with_step("m1", "analyze")
        orch = PipelineOrchestrator()

        started = datetime.now(timezone.utc)
        await orch._record_step("m1", "analyze", StepStatus.RUNNING, started_at=started)
        rows = await _get_step("m1", "analyze")
        assert len(rows) == 1
        assert rows[0].status == "running"

        await orch._record_step(
            "m1", "analyze", StepStatus.COMPLETED,
            started_at=started, output={"tracks_found": 12},
        )
        rows = await _get_step("m1", "analyze")
        assert len(rows) == 1
        assert rows[0].status == "completed"
        assert rows[0].output_json == {"tracks_found": 12}
        assert rows[0].progress == 100
        assert rows[0].completed_at is not None

    async def test_upsert_without_preexisting_row_inserts_once(self, prepared_db):
        orch = PipelineOrchestrator()
        await orch._record_step("m2", "detect", StepStatus.COMPLETED)
        await orch._record_step("m2", "detect", StepStatus.FAILED, error="boom")
        rows = await _get_step("m2", "detect")
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].error == "boom"


class TestInterruptedSweep:
    async def test_running_steps_and_mixes_are_swept(self, prepared_db):
        from app.database import async_session_factory

        async with async_session_factory() as session:
            session.add(Mix(id="m1", title="Cut off", pipeline_status="running"))
            session.add(Mix(id="m2", title="Fine", pipeline_status="completed"))
            session.add(PipelineStep(mix_id="m1", step_name="upload_soundcloud", status="running"))
            session.add(PipelineStep(mix_id="m1", step_name="analyze", status="completed"))
            session.add(PipelineStep(mix_id="m2", step_name="analyze", status="completed"))
            await session.commit()

        swept = await sweep_interrupted_at_boot()
        assert swept == {"steps": 1, "mixes": 1}

        async with async_session_factory() as session:
            m1 = await session.get(Mix, "m1")
            m2 = await session.get(Mix, "m2")
            # NOT "failed": a restart says nothing about the mix. The boot
            # resume re-drives it from here (test_interrupted_resume.py).
            assert m1.pipeline_status == "interrupted"
            assert m1.pipeline_error == "interrupted by restart"
            assert m2.pipeline_status == "completed"

            steps = (
                await session.execute(
                    select(PipelineStep).where(PipelineStep.mix_id == "m1")
                )
            ).scalars().all()
            by_name = {s.step_name: s.status for s in steps}
            assert by_name["upload_soundcloud"] == "interrupted"
            assert by_name["analyze"] == "completed"

    async def test_sweep_noop_when_nothing_running(self, prepared_db):
        swept = await sweep_interrupted_at_boot()
        assert swept == {"steps": 0, "mixes": 0}


class TestRetryFromInterrupted:
    async def test_retry_endpoint_accepts_interrupted_steps(self, client):
        from app.database import async_session_factory

        created = (await client.post("/api/mixes", json={"title": "Interrupted"})).json()
        mix_id = created["id"]

        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            mix.pipeline_status = "failed"
            mix.pipeline_error = "interrupted by restart"
            step = (
                await session.execute(
                    select(PipelineStep).where(
                        PipelineStep.mix_id == mix_id,
                        PipelineStep.step_name == "upload_soundcloud",
                    )
                )
            ).scalars().first()
            step.status = "interrupted"
            await session.commit()

        resp = await client.post(f"/api/mixes/{mix_id}/retry")
        assert resp.status_code == 200
        assert resp.json()["pipeline_status"] == "pending"

        async with async_session_factory() as session:
            step = (
                await session.execute(
                    select(PipelineStep).where(
                        PipelineStep.mix_id == mix_id,
                        PipelineStep.step_name == "upload_soundcloud",
                    )
                )
            ).scalars().first()
            assert step.status == "pending"

    async def test_step_serialization_includes_progress_fields(self, client):
        created = (await client.post("/api/mixes", json={"title": "Progress"})).json()
        detail = (await client.get(f"/api/mixes/{created['id']}")).json()
        step = detail["steps"][0]
        assert "progress" in step
        assert "progress_detail" in step
