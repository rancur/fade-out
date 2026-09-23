"""Tagging runs after renaming, and cannot break a run.

Two flavours of test here:

- A cheap structural smoke test (`test_tagging_is_ordered_after_renaming_in_source`)
  that only proves the two calls appear in that textual order in
  ``pipeline.py``. It would pass even if the tagging call were unreachable
  dead code, so it is not proof of anything at runtime -- see its docstring.
- Real behavioural tests that monkeypatch the actual functions, drive
  ``PipelineOrchestrator._mark_complete`` (the method that contains both
  calls, formerly around line 1212 pre-change), and assert on what actually
  happened: call order, and that a raising tagger does not stop the run from
  completing.
- A regression test that does NOT monkeypatch ``tag_sources_for_mix`` and
  instead runs the real function against the real DB state ``_mark_complete``
  produces, to prove the completed-status write actually happens before the
  tag call (see ``test_mark_complete_lets_the_real_tagger_pass_its_status_gate``).
"""

import inspect
from datetime import datetime, timezone

from app.database import async_session_factory
from app.models import Mix
from app.services import pipeline, source_renamer, source_tagger


# ---------------------------------------------------------------------------
# Structural smoke test — textual order only, NOT runtime order
# ---------------------------------------------------------------------------

def test_tagging_is_ordered_after_renaming_in_source():
    """Smoke test only: proves TEXTUAL order in pipeline.py's source, nothing
    about runtime behaviour.

    This would still pass if the tagging call were unreachable, commented out
    and reintroduced elsewhere, buried in a dead ``if False:`` branch, or
    called from an unrelated function entirely. It does not prove the tagger
    actually runs, or that it runs after the renamer at runtime. That proof
    is the job of the behavioural tests below
    (``test_mark_complete_calls_rename_then_tag`` and friends) -- keep this
    test only as a fast, cheap first signal, never as a substitute for them.
    """
    src = inspect.getsource(pipeline)
    assert "tag_sources_for_mix" in src, "the pipeline never tags anything"
    assert src.index("rename_sources_for_mix") < src.index("tag_sources_for_mix"), (
        "tagging the pre-rename path leaves the renamed file untagged"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

async def _make_mix(mix_id="mix-1"):
    async with async_session_factory() as session:
        session.add(
            Mix(
                id=mix_id,
                title="Neon Drift",
                source="pipeline",
                audio_file_path=None,
                video_file_path=None,
                created_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            )
        )
        await session.commit()
    return mix_id


async def _get_mix(mix_id="mix-1"):
    async with async_session_factory() as session:
        return await session.get(Mix, mix_id)


# ---------------------------------------------------------------------------
# Behavioural: runtime call order
# ---------------------------------------------------------------------------

async def test_mark_complete_calls_rename_then_tag(prepared_db, monkeypatch):
    """The thing that actually matters: at runtime, rename happens before tag.

    Monkeypatches BOTH real functions with async fakes that record their own
    name into a shared list, drives ``_mark_complete`` (the pipeline method
    that contains the real call site), and asserts on the recorded order --
    not on where any text sits in a source file.
    """
    from app.services.pipeline import PipelineOrchestrator

    await _make_mix()
    calls = []

    async def fake_rename(mix_id, *, reason, dry_run=False):
        calls.append("rename")
        return {}

    async def fake_tag(mix_id, *, reason, dry_run=False):
        calls.append("tag")
        return {}

    monkeypatch.setattr(source_renamer, "rename_sources_for_mix", fake_rename)
    monkeypatch.setattr(source_tagger, "tag_sources_for_mix", fake_tag)

    await PipelineOrchestrator()._mark_complete("mix-1")

    assert calls == ["rename", "tag"]
    assert (await _get_mix()).pipeline_status == "completed"


# ---------------------------------------------------------------------------
# Behavioural: a raising tagger cannot fail the run
# ---------------------------------------------------------------------------

async def test_mark_complete_survives_a_raising_tagger(prepared_db, monkeypatch):
    """Tagging is cosmetic. The mix is already published to SoundCloud and
    YouTube by the time this runs, so an exception escaping the tagger must
    not stop the run from being marked complete.

    This patches ``tag_sources_for_mix`` itself to raise, bypassing its own
    internal try/except entirely, so it exercises the pipeline's own
    never-fail guarantee at the call site rather than relying on the
    tagger's. If the tagger's internal guarantee were the only thing
    protecting this path, patching the callable out from under it (as done
    here) would still be a fair, external test of the pipeline's contract --
    it does not depend on the tagger's own body running at all.
    """
    from app.services.pipeline import PipelineOrchestrator

    await _make_mix()

    async def boom(mix_id, *, reason, dry_run=False):
        raise RuntimeError("tagging kaboom")

    monkeypatch.setattr(source_tagger, "tag_sources_for_mix", boom)

    await PipelineOrchestrator()._mark_complete("mix-1")

    mix = await _get_mix()
    assert mix.pipeline_status == "completed"
    assert mix.pipeline_step == "complete"


# ---------------------------------------------------------------------------
# Regression: the tagger's own completed-status gate must actually pass
# ---------------------------------------------------------------------------

async def test_mark_complete_lets_the_real_tagger_pass_its_status_gate(
    prepared_db, tmp_path, monkeypatch
):
    """Regression test for Finding 1: ``tag_sources_for_mix`` hard-gates on
    ``mix.pipeline_status == "completed"`` (source_tagger.py), but the
    original wiring fired the tag call BEFORE ``_mark_complete`` ever wrote
    that status to the DB. Every real invocation therefore hit the gate and
    the tagger silently returned ``{"status": "skipped", ...}`` without ever
    tagging anything -- with no exception and no distinguishing log line, so
    nothing else caught it.

    This does NOT monkeypatch ``tag_sources_for_mix`` itself. It lets the
    real function run against the actual DB state ``_mark_complete``
    produces, via a thin spy that delegates to the real function and just
    records its return value. Only the leaves that would otherwise touch the
    filesystem/watcher (``write_tagged_copy``, ``register_and_promote``) and
    the two gates unrelated to this bug (``_tagging_enabled``,
    ``is_within_allowed_roots``) are stubbed, so the status gate itself is
    genuinely exercised.
    """
    from app.services.pipeline import PipelineOrchestrator

    audio = tmp_path / "mix.flac"
    audio.write_bytes(b"x" * 4096)

    await _make_mix()
    async with async_session_factory() as session:
        mix = await session.get(Mix, "mix-1")
        mix.audio_file_path = str(audio)
        await session.commit()

    async def fake_rename(mix_id, *, reason, dry_run=False):
        return {}

    monkeypatch.setattr(source_renamer, "rename_sources_for_mix", fake_rename)

    async def always_enabled():
        return True

    monkeypatch.setattr(source_tagger, "_tagging_enabled", always_enabled)
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda _path: True)
    monkeypatch.setattr(
        source_tagger,
        "write_tagged_copy",
        lambda src, tags, cover_art_path, expected_duration: src,
    )
    monkeypatch.setattr(
        source_tagger,
        "register_and_promote",
        lambda temp_path, final_path, file_type="audio", seen_db=None: None,
    )

    real_tag = source_tagger.tag_sources_for_mix
    results = []

    async def spy(mix_id, *, reason, dry_run=False):
        result = await real_tag(mix_id, reason=reason, dry_run=dry_run)
        results.append(result)
        return result

    monkeypatch.setattr(source_tagger, "tag_sources_for_mix", spy)

    await PipelineOrchestrator()._mark_complete("mix-1")

    assert results, "the real tag_sources_for_mix was never invoked"
    result = results[0]
    gated_on_status = (
        result.get("status") == "skipped"
        and "pipeline_status" in (result.get("reason") or "")
    )
    assert not gated_on_status, (
        f"tag_sources_for_mix was gated on a stale pipeline_status -- the "
        f"completion write is not happening before the tag call: {result}"
    )
    assert result.get("status") == "ok", f"expected a real tag, got: {result}"
