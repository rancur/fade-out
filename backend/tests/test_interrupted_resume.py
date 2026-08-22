"""Boot recovery: a mix cut off mid-pipeline must come BACK, not die quietly.

Incident this covers (2026-08-21): the container was OOM-killed while
analyzing a long set. The boot sweep marked the mix "failed — interrupted by
restart" and nothing ever re-drove it, so the set simply never published and
the loss surfaced days later when a human went looking for it.

Every test here starts from the FAILING state — work already in flight when
the process dies — rather than a clean boot, which proves nothing.
"""

import pytest
from sqlalchemy import select

import app.services.pipeline as pipeline_mod
from app.models import Mix, PipelineStep
from app.services.pipeline import (
    MAX_INTERRUPT_RESUMES,
    RESUME_COUNT_KEY,
    resume_interrupted_at_boot,
    sweep_interrupted_at_boot,
)


class RecordingOrchestrator:
    """Stands in for PipelineOrchestrator, recording what got re-driven."""

    def __init__(self) -> None:
        self.started: list[str] = []

    async def start_pipeline(self, mix_id: str) -> None:
        self.started.append(mix_id)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Strip the settle/stagger sleeps so the tests are instant."""
    monkeypatch.setattr(pipeline_mod, "RESUME_STAGGER_SECONDS", 0)


async def _mix_cut_off_mid_pipeline(mix_id="m1", *, resumes=0, title="Cut off"):
    """Seed the DB exactly as an OOM kill leaves it: mix and step 'running'."""
    from app.database import async_session_factory

    async with async_session_factory() as session:
        session.add(
            Mix(
                id=mix_id,
                title=title,
                pipeline_status="running",
                metadata_json={RESUME_COUNT_KEY: resumes} if resumes else None,
            )
        )
        session.add(PipelineStep(mix_id=mix_id, step_name="detect", status="completed"))
        session.add(PipelineStep(mix_id=mix_id, step_name="analyze", status="running"))
        session.add(
            PipelineStep(mix_id=mix_id, step_name="upload_youtube", status="pending")
        )
        await session.commit()


class TestInterruptedMixResumes:
    async def test_interrupted_mix_is_re_driven_after_restart(self, prepared_db):
        """The whole incident, end to end: crash mid-analysis, then reboot."""
        from app.database import async_session_factory

        await _mix_cut_off_mid_pipeline()

        # --- the restart ---
        swept = await sweep_interrupted_at_boot()
        assert swept == {"steps": 1, "mixes": 1}

        # The sweep alone must never be terminal.
        async with async_session_factory() as session:
            mix = await session.get(Mix, "m1")
            assert mix.pipeline_status == "interrupted"

        orch = RecordingOrchestrator()
        result = await resume_interrupted_at_boot(orch, delay_seconds=0)

        assert result["resumed"] == 1
        assert result["exhausted"] == 0
        # The pipeline was actually handed back to the orchestrator.
        assert orch.started == ["m1"]

        async with async_session_factory() as session:
            mix = await session.get(Mix, "m1")
            assert mix.pipeline_status == "pending"
            assert mix.pipeline_error is None
            assert mix.metadata_json[RESUME_COUNT_KEY] == 1

            steps = {
                s.step_name: s
                for s in (await session.execute(select(PipelineStep))).scalars()
            }
            # The step the crash landed on is runnable again...
            assert steps["analyze"].status == "pending"
            assert steps["analyze"].retry_count == 1
            # ...and finished work is not redone.
            assert steps["detect"].status == "completed"

    async def test_resume_gives_up_after_the_bound(self, prepared_db):
        """A mix that keeps killing the process must not restart-loop."""
        from app.database import async_session_factory

        await _mix_cut_off_mid_pipeline(resumes=MAX_INTERRUPT_RESUMES)
        await sweep_interrupted_at_boot()

        orch = RecordingOrchestrator()
        result = await resume_interrupted_at_boot(orch, delay_seconds=0)

        assert result == {"resumed": 0, "exhausted": 1, "skipped": False}
        assert orch.started == []

        async with async_session_factory() as session:
            mix = await session.get(Mix, "m1")
            assert mix.pipeline_status == "failed"
            assert "limit" in mix.pipeline_error
            assert "retry manually" in mix.pipeline_error

    async def test_bound_is_reached_by_repeated_restarts(self, prepared_db):
        """Restart N+1 times: it resumes exactly MAX times, then stops."""
        from app.database import async_session_factory

        await _mix_cut_off_mid_pipeline()
        orch = RecordingOrchestrator()

        for _ in range(MAX_INTERRUPT_RESUMES + 2):
            # Each loop is one crash-and-reboot: put the mix back in flight.
            async with async_session_factory() as session:
                mix = await session.get(Mix, "m1")
                if mix.pipeline_status == "failed":
                    break
                mix.pipeline_status = "running"
                await session.commit()
            await sweep_interrupted_at_boot()
            await resume_interrupted_at_boot(orch, delay_seconds=0)

        assert len(orch.started) == MAX_INTERRUPT_RESUMES

        async with async_session_factory() as session:
            mix = await session.get(Mix, "m1")
            assert mix.pipeline_status == "failed"

    async def test_disabled_leaves_the_mix_interrupted_not_failed(
        self, prepared_db, monkeypatch
    ):
        """Off, the mix still has to be visible and hand-retryable."""
        from app.database import async_session_factory
        from app.services import app_config

        async def _off(key, *a, **kw):
            return False if key == "resume_interrupted_mixes" else None

        monkeypatch.setattr(app_config, "resolve", _off)

        await _mix_cut_off_mid_pipeline()
        await sweep_interrupted_at_boot()

        orch = RecordingOrchestrator()
        result = await resume_interrupted_at_boot(orch, delay_seconds=0)

        assert result["skipped"] is True
        assert orch.started == []

        async with async_session_factory() as session:
            mix = await session.get(Mix, "m1")
            # "interrupted" is what /mixes/{id}/retry accepts — not a dead end.
            assert mix.pipeline_status == "interrupted"

    async def test_completed_and_failed_mixes_are_left_alone(self, prepared_db):
        from app.database import async_session_factory

        async with async_session_factory() as session:
            session.add(Mix(id="done", title="Done", pipeline_status="completed"))
            session.add(Mix(id="dead", title="Dead", pipeline_status="failed"))
            await session.commit()

        await sweep_interrupted_at_boot()
        orch = RecordingOrchestrator()
        result = await resume_interrupted_at_boot(orch, delay_seconds=0)

        assert result["resumed"] == 0
        assert orch.started == []
