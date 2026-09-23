# backend/tests/test_source_tagger_rails.py
"""The rails, not the mutagen call, are the substance of this module."""
import pytest

from app.services import source_tagger


@pytest.mark.asyncio
async def test_disabled_setting_is_a_clean_no_op(monkeypatch):
    async def _off():
        return False
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _off)
    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "disabled"
    assert result["actions"] == []


@pytest.mark.asyncio
async def test_refuses_a_mix_that_is_not_completed(monkeypatch):
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="running", audio_file_path="/watch/audio/a.flac"),
    )
    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "skipped"
    assert "completed" in result["reason"]


@pytest.mark.asyncio
async def test_refuses_a_path_outside_the_allowed_roots(monkeypatch):
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="completed", audio_file_path="/etc/passwd"),
    )
    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "skipped"
    assert "allowed roots" in result["reason"]


@pytest.mark.asyncio
async def test_never_raises_when_the_write_explodes(monkeypatch):
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="completed", audio_file_path="/watch/audio/a.flac"),
    )
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda p: True)
    monkeypatch.setattr(source_tagger, "_has_free_space", lambda p: True)
    monkeypatch.setattr(source_tagger.os.path, "isfile", lambda p: True)

    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(source_tagger, "write_tagged_copy", boom)
    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "failed"
    assert "disk on fire" in result["reason"]


@pytest.mark.asyncio
async def test_missing_source_file_is_skipped_not_failed(monkeypatch):
    """A source that has vanished is a skip, not an error: nothing is wrong
    with the system, the file is simply not there to tag."""
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="completed", audio_file_path="/watch/audio/gone.flac"),
    )
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda p: True)

    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "skipped"
    assert "does not exist" in result["reason"]


@pytest.mark.asyncio
async def test_dry_run_touches_nothing(monkeypatch):
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="completed", audio_file_path="/watch/audio/a.flac"),
    )
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda p: True)

    def boom(*a, **k):
        raise AssertionError("dry run must not write")

    monkeypatch.setattr(source_tagger, "write_tagged_copy", boom)
    result = await source_tagger.tag_sources_for_mix("m1", reason="test", dry_run=True)
    assert result["status"] == "dry_run"
    assert result["actions"], "a dry run should still report what it would do"


@pytest.mark.asyncio
async def test_dry_run_reports_a_missing_source_instead_of_a_false_green(monkeypatch):
    """Regression test for the review-gate false green: a dry run over a
    vanished source must say so, not silently report "would tag"."""
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="completed", audio_file_path="/watch/audio/gone.flac"),
    )
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda p: True)

    def boom(*a, **k):
        raise AssertionError("dry run must not write")

    monkeypatch.setattr(source_tagger, "write_tagged_copy", boom)
    result = await source_tagger.tag_sources_for_mix("m1", reason="test", dry_run=True)
    assert result["status"] == "dry_run"
    assert "missing" in result["reason"]
    assert result["actions"][0]["source_exists"] is False


@pytest.mark.asyncio
async def test_dry_run_reports_ok_when_source_exists_with_space(monkeypatch):
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="completed", audio_file_path="/watch/audio/a.flac"),
    )
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda p: True)
    monkeypatch.setattr(source_tagger.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(source_tagger, "_has_free_space", lambda p: True)

    def boom(*a, **k):
        raise AssertionError("dry run must not write")

    monkeypatch.setattr(source_tagger, "write_tagged_copy", boom)
    result = await source_tagger.tag_sources_for_mix("m1", reason="test", dry_run=True)
    assert result["status"] == "dry_run"
    assert result["reason"] == "no changes made"
    assert result["actions"][0]["source_exists"] is True


@pytest.mark.asyncio
async def test_enabled_check_failure_does_not_propagate(monkeypatch):
    """Regression test for the never-fatal hole: a failure inside
    _tagging_enabled (e.g. a bad settings row) must come back as a failed
    status, not an exception escaping into the pipeline run that already
    published the mix."""
    async def _boom():
        raise RuntimeError("settings row is malformed")

    monkeypatch.setattr(source_tagger, "_tagging_enabled", _boom)
    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "failed"
    assert "settings row is malformed" in result["reason"]


def _fake_loader(**attrs):
    from datetime import datetime
    from types import SimpleNamespace

    async def _load(mix_id):
        base = dict(
            id=mix_id, title="T", video_file_path=None, genres=None, tracklist=None,
            cover_art_path=None, soundcloud_url=None, youtube_url=None,
            created_at=datetime(2026, 9, 18), source="watch", duration_seconds=60.0,
        )
        base.update(attrs)
        return SimpleNamespace(**base)

    return _load
