"""Platform legs must fail independently.

The regression under test is the 2026-08-12 publish failure: SoundCloud's
OAuth grant was dead, the orchestrator walked the flat step list in order, and
``upload_youtube`` was never attempted even though YouTube was healthy. The
mix published nowhere and its YouTube step sat at "pending".

The load-bearing test here is
``test_soundcloud_auth_failure_still_publishes_youtube``: it simulates exactly
that auth failure and asserts YouTube still lands.
"""

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models import Mix, PipelineStep
from app.services.pipeline import (
    PIPELINE_STEPS,
    PLATFORM_LEGS,
    PipelineOrchestrator,
    StepStatus,
)
from app.services.soundcloud_uploader import SoundCloudAuthError


async def _make_mix(mix_id="m-decouple", status="running"):
    """A mix with the same all-pending step rows ingest creates."""
    async with async_session_factory() as session:
        session.add(Mix(id=mix_id, title="Sim Mix", pipeline_status=status))
        for name in PIPELINE_STEPS:
            session.add(PipelineStep(mix_id=mix_id, step_name=name, status="pending"))
        await session.commit()


async def _steps(mix_id):
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(PipelineStep).where(PipelineStep.mix_id == mix_id)
            )
        ).scalars().all()
        return {r.step_name: r for r in rows}


async def _mix(mix_id):
    async with async_session_factory() as session:
        return await session.get(Mix, mix_id)


def _register_publish_handlers(orch, *, soundcloud_fails=True, calls=None):
    """Register upload/verify handlers for all three legs.

    SoundCloud raises the real ``SoundCloudAuthError`` — the same exception the
    live uploader raises on ``invalid_grant`` — so this is the production
    failure path, not a stand-in exception.
    """
    calls = calls if calls is not None else []

    async def sc_upload(mix_id, session, **_kw):
        calls.append("upload_soundcloud")
        if soundcloud_fails:
            raise SoundCloudAuthError([
                "stored access token rejected by /me",
                "refresh grant rejected: invalid_grant",
            ])
        mix = await session.get(Mix, mix_id)
        mix.soundcloud_url = "https://soundcloud.com/x/sim"
        return {"soundcloud_url": mix.soundcloud_url}

    async def sc_verify(mix_id, session, **_kw):
        calls.append("verify_soundcloud")
        return {"verified": True}

    async def yt_upload(mix_id, session, **_kw):
        calls.append("upload_youtube")
        mix = await session.get(Mix, mix_id)
        mix.youtube_url = "https://youtube.com/watch?v=SIMULATED"
        mix.youtube_video_id = "SIMULATED"
        return {"youtube_url": mix.youtube_url, "video_id": "SIMULATED"}

    async def yt_verify(mix_id, session, **_kw):
        calls.append("verify_youtube")
        return {"verified": True}

    async def mc_upload(mix_id, session, **_kw):
        calls.append("upload_mixcloud")
        return {"skipped": True, "reason": "Mixcloud disabled"}

    async def mc_verify(mix_id, session, **_kw):
        return {"skipped": True, "reason": "No Mixcloud URL"}

    async def cross_link(mix_id, session, **_kw):
        calls.append("cross_link")
        return {"updated": []}

    orch.register_handler("upload_soundcloud", sc_upload)
    orch.register_handler("verify_soundcloud", sc_verify)
    orch.register_handler("upload_youtube", yt_upload)
    orch.register_handler("verify_youtube", yt_verify)
    orch.register_handler("upload_mixcloud", mc_upload)
    orch.register_handler("verify_mixcloud", mc_verify)
    orch.register_handler("cross_link", cross_link)
    return calls


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Auth failures are non-retryable, but keep any backoff instant anyway."""
    import app.services.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "BASE_BACKOFF_SECONDS", 0)


class TestLegIsolation:
    async def test_soundcloud_auth_failure_still_publishes_youtube(self, prepared_db):
        """THE regression test for 2026-08-12."""
        orch = PipelineOrchestrator()
        calls = _register_publish_handlers(orch, soundcloud_fails=True)
        await _make_mix("m1")

        await orch._run_publish_phase("m1")

        # YouTube was attempted and published despite SoundCloud dying first.
        assert "upload_youtube" in calls
        mix = await _mix("m1")
        assert mix.youtube_url == "https://youtube.com/watch?v=SIMULATED"
        assert mix.soundcloud_url is None

        steps = await _steps("m1")
        assert steps["upload_soundcloud"].status == StepStatus.FAILED.value
        assert steps["upload_youtube"].status == StepStatus.COMPLETED.value
        assert steps["verify_youtube"].status == StepStatus.COMPLETED.value

    async def test_failed_leg_blocks_only_its_own_verify(self, prepared_db):
        orch = PipelineOrchestrator()
        _register_publish_handlers(orch, soundcloud_fails=True)
        await _make_mix("m2")

        await orch._run_publish_phase("m2")

        steps = await _steps("m2")
        # The failed leg's verify is BLOCKED — never "pending", which is how
        # the stranded YouTube step hid in plain sight.
        assert steps["verify_soundcloud"].status == StepStatus.BLOCKED.value
        assert steps["verify_soundcloud"].error
        assert steps["upload_youtube"].status == StepStatus.COMPLETED.value

    async def test_partial_publish_is_an_explicit_state(self, prepared_db):
        orch = PipelineOrchestrator()
        _register_publish_handlers(orch, soundcloud_fails=True)
        await _make_mix("m3")

        await orch._run_publish_phase("m3")

        mix = await _mix("m3")
        assert mix.pipeline_status == "partial"
        assert "soundcloud" in (mix.pipeline_error or "")
        assert "youtube" in (mix.pipeline_error or "")

        publish = (mix.metadata_json or {}).get("publish", {})
        assert publish["published"] == ["youtube"]
        assert publish["failed"] == ["soundcloud"]
        assert publish["legs"]["youtube"]["status"] == "published"
        assert publish["legs"]["soundcloud"]["status"] == "failed"

    async def test_all_legs_ok_completes(self, prepared_db):
        orch = PipelineOrchestrator()
        _register_publish_handlers(orch, soundcloud_fails=False)
        await _make_mix("m4")

        await orch._run_publish_phase("m4")

        mix = await _mix("m4")
        assert mix.pipeline_status == "completed"
        assert mix.pipeline_step == "complete"

    async def test_auth_failure_is_not_retried(self, prepared_db):
        """A dead credential must not burn the retry budget."""
        orch = PipelineOrchestrator()
        calls = _register_publish_handlers(orch, soundcloud_fails=True)
        await _make_mix("m5")

        await orch._run_publish_phase("m5")

        assert calls.count("upload_soundcloud") == 1

    async def test_failure_event_names_platform_and_credential(self, prepared_db):
        orch = PipelineOrchestrator()
        events = []
        orch.on_event(lambda e, m, d: events.append((e, d)))
        _register_publish_handlers(orch, soundcloud_fails=True)
        await _make_mix("m6")

        await orch._run_publish_phase("m6")

        failures = [d for e, d in events if e == "platform_failed"]
        assert failures, "a failed leg must emit platform_failed"
        cause = failures[0]["cause"]
        assert failures[0]["platform"] == "soundcloud"
        assert cause["kind"] == "auth"
        assert "invalid_grant" in cause["summary"]
        assert "SOUNDCLOUD" in (cause["credential"] or "")
        assert cause["retryable"] is False
        # And no token value can ride along in the alert payload.
        assert "Authorization:" not in str(failures[0])


class TestFullPipelinePath:
    async def test_end_to_end_run_reaches_youtube_after_soundcloud_dies(
        self, prepared_db, monkeypatch
    ):
        """The whole ``start_pipeline`` path, not just the publish phase.

        This is the shape of the real 08-12 run: prep succeeds, SoundCloud's
        grant is dead, and the run must still reach YouTube.
        """
        from app.services import app_config

        async def _no_draft(key, *a, **kw):
            return False if key == "draft_mode" else None

        monkeypatch.setattr(app_config, "resolve", _no_draft)

        orch = PipelineOrchestrator()
        calls = _register_publish_handlers(orch, soundcloud_fails=True)

        async def prep(mix_id, session, **_kw):
            return {"ok": True}

        for name in ("detect", "analyze", "generate_description", "generate_art"):
            orch.register_handler(name, prep)

        await _make_mix("m-e2e")
        await orch._run_pipeline("m-e2e")

        assert "upload_youtube" in calls
        mix = await _mix("m-e2e")
        assert mix.youtube_url
        assert mix.pipeline_status == "partial"


class TestTargetedRetry:
    async def test_retry_platform_republishes_only_that_leg(self, prepared_db):
        orch = PipelineOrchestrator()
        calls = _register_publish_handlers(orch, soundcloud_fails=True)
        await _make_mix("m7")
        await orch._run_publish_phase("m7")
        assert (await _mix("m7")).pipeline_status == "partial"

        # Credentials are fixed; retry ONLY the SoundCloud leg.
        calls.clear()
        _register_publish_handlers(orch, soundcloud_fails=False, calls=calls)
        result = await orch.retry_platform("m7", "soundcloud")

        assert result["status"] == "published"
        assert "upload_soundcloud" in calls
        # The healthy leg is not re-uploaded — idempotency plus isolation.
        assert "upload_youtube" not in calls

        mix = await _mix("m7")
        assert mix.pipeline_status == "completed"
        assert mix.soundcloud_url and mix.youtube_url

    async def test_retry_unknown_platform_rejected(self, prepared_db):
        orch = PipelineOrchestrator()
        with pytest.raises(ValueError):
            await orch.retry_platform("m8", "bandcamp")


class TestLegStates:
    async def test_pending_leg_is_never_reported_as_fine(self, prepared_db):
        """Unknown/pending must not finalize a mix as completed."""
        orch = PipelineOrchestrator()
        await _make_mix("m9")

        # No handlers registered at all: nothing ran, nothing published.
        states = await orch.leg_states("m9")
        assert set(states) == set(PLATFORM_LEGS)
        assert all(s["status"] == "pending" for s in states.values())

        status = await orch._finalize("m9")
        assert status == "failed"
        mix = await _mix("m9")
        assert mix.pipeline_status == "failed"
