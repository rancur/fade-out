"""Source-file renaming: sanitizer, safety rails, and the pipeline hook.

This is the only feature in the backend that writes to the watch folders, so
the tests here are weighted toward what must NOT happen: no file outside the two
watch roots is ever touched, no existing file is ever overwritten, and no
failure mode — a read-only mount, a missing file, a raising ``os.rename`` — can
propagate out and fail a pipeline run.

The date-prefix tests are not cosmetic. ``ingest._find_sibling`` and
``catalog_backfill.match_local_audio`` both key off a date token in the
filename, so a rename that dropped it would silently break audio/video pairing
and local-file matching for every mix from then on.
"""

import os
import unicodedata
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.database import async_session_factory
from app.models import ActivityEvent, AppSettings, Mix
from app.services import app_config, source_renamer

TITLE = "Neon Drift | House Mix"
EXPECTED_STEM = "2026-07-15 Neon Drift - House Mix"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def watch_dirs(tmp_path, monkeypatch):
    audio = tmp_path / "audio"
    video = tmp_path / "video"
    audio.mkdir()
    video.mkdir()
    monkeypatch.setattr(settings, "WATCH_AUDIO_PATH", str(audio))
    monkeypatch.setattr(settings, "WATCH_VIDEO_PATH", str(video))
    return audio, video


@pytest.fixture
async def enabled(prepared_db):
    """Turn the feature on for this test (it ships OFF)."""
    async with async_session_factory() as session:
        session.add(AppSettings(id=1, settings_json={"rename_source_files": True}))
        await session.commit()
    app_config.invalidate_cache()
    yield


def _touch(path, size=2048):
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return str(path)


async def _make_mix(
    audio=None,
    video=None,
    title=TITLE,
    source="pipeline",
    mix_id="mix-1",
    created_at=None,
):
    async with async_session_factory() as session:
        session.add(
            Mix(
                id=mix_id,
                title=title,
                source=source,
                audio_file_path=audio,
                video_file_path=video,
                created_at=created_at or datetime(2026, 7, 15, tzinfo=timezone.utc),
            )
        )
        await session.commit()
    return mix_id


async def _get_mix(mix_id="mix-1"):
    async with async_session_factory() as session:
        return await session.get(Mix, mix_id)


# ---------------------------------------------------------------------------
# Sanitizer — pure, no fixtures
# ---------------------------------------------------------------------------

def test_sanitize_maps_windows_illegal_chars():
    out = source_renamer.sanitize_title_for_filename('A:B/C\\D|E"F<G>H?I*J')
    for ch in ':/\\|"<>?*':
        assert ch not in out
    # ":" becomes " -" so the title still reads as prose, not a run-on.
    assert source_renamer.sanitize_title_for_filename("Deep: House") == "Deep - House"
    assert source_renamer.sanitize_title_for_filename(TITLE) == "Neon Drift - House Mix"


def test_sanitize_collapses_whitespace():
    assert source_renamer.sanitize_title_for_filename("A   B\t\tC") == "A B C"
    # The ":" -> " -" mapping would otherwise leave a double space.
    assert source_renamer.sanitize_title_for_filename("A : B") == "A - B"


def test_sanitize_strips_trailing_dots_and_spaces():
    # SMB and Windows physically cannot store a name ending in "." or " ".
    for raw in ("Mix...", "Mix   ", "Mix. . ."):
        assert not source_renamer.sanitize_title_for_filename(raw).endswith((".", " "))


def test_sanitize_strips_leading_dots_and_dashes():
    assert not source_renamer.sanitize_title_for_filename(".hidden").startswith(".")
    assert not source_renamer.sanitize_title_for_filename("--flag").startswith("-")


def test_sanitize_strips_control_chars():
    out = source_renamer.sanitize_title_for_filename("A\x00B\nC\x7fD")
    assert out == "A B C D" or "\x00" not in out and "\n" not in out


def test_sanitize_normalizes_to_nfc():
    # macOS returns decomposed strings over SMB; ingest._find_sibling compares
    # stems with ==, so a stray NFD name would stop pairing.
    nfd = unicodedata.normalize("NFD", "Café Déep")
    out = source_renamer.sanitize_title_for_filename(nfd)
    assert unicodedata.is_normalized("NFC", out)
    assert out == "Café Déep"


def test_sanitize_drops_emoji_but_keeps_letters():
    out = source_renamer.sanitize_title_for_filename("Fire 🔥 Mix")
    assert "🔥" not in out
    assert "Fire" in out and "Mix" in out


def test_sanitize_truncates_on_utf8_boundary():
    out = source_renamer.sanitize_title_for_filename("é" * 400, max_bytes=100)
    assert len(out.encode("utf-8")) <= 100
    assert out.encode("utf-8").decode("utf-8") == out  # no partial codepoint


def test_sanitize_reserved_windows_names():
    assert source_renamer.sanitize_title_for_filename("CON") == "_CON"
    assert source_renamer.sanitize_title_for_filename("nul") == "_nul"


def test_sanitize_empty_falls_back_to_mix():
    for raw in ("", "   ", "...", "///", "***"):
        assert source_renamer.sanitize_title_for_filename(raw) == "mix"


def test_build_target_name_with_and_without_date():
    assert source_renamer.build_target_name(TITLE, "2026-07-15", ".flac") == (
        f"{EXPECTED_STEM}.flac"
    )
    assert source_renamer.build_target_name(TITLE, None, ".MKV") == (
        "Neon Drift - House Mix.mkv"
    )


def test_build_target_name_stays_under_the_byte_ceiling():
    name = source_renamer.build_target_name("é" * 400, "2026-07-15", ".flac")
    assert len(name.encode("utf-8")) <= source_renamer.MAX_NAME_BYTES


def test_resolve_date_token_prefers_filename_and_normalizes_mdy():
    assert source_renamer.resolve_date_token("set 2026-07-15.flac") == "2026-07-15"
    # MM-DD-YYYY is upgraded to ISO, which ingest._extract_date can read.
    assert source_renamer.resolve_date_token("set 07-15-2026.flac") == "2026-07-15"


def test_resolve_date_token_falls_back_to_created_at_for_pipeline_mixes():
    got = source_renamer.resolve_date_token(
        "untitled.flac",
        source="pipeline",
        created_at=datetime(2026, 3, 4, tzinfo=timezone.utc),
    )
    assert got == "2026-03-04"


def test_resolve_date_token_never_invents_a_date_for_imported_mixes():
    # An imported mix's created_at is the catalog-sync run, not the recording.
    # Stamping it on would inject a false date that match_local_audio scores
    # against real publish dates.
    got = source_renamer.resolve_date_token(
        "untitled.flac",
        source="imported",
        created_at=datetime(2026, 3, 4, tzinfo=timezone.utc),
    )
    assert got is None


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def test_allowlist_accepts_watch_roots(watch_dirs):
    audio, video = watch_dirs
    assert source_renamer.is_within_allowed_roots(str(audio / "a.flac"))
    assert source_renamer.is_within_allowed_roots(str(video / "a.mkv"))


def test_allowlist_refuses_lookalike_sibling_root(watch_dirs, tmp_path):
    # "startswith" containment would accept this; commonpath does not.
    sibling = tmp_path / "audio-archive"
    sibling.mkdir()
    assert not source_renamer.is_within_allowed_roots(str(sibling / "a.flac"))


def test_allowlist_refuses_arbitrary_paths(watch_dirs, tmp_path):
    assert not source_renamer.is_within_allowed_roots(str(tmp_path / "loose.flac"))
    assert not source_renamer.is_within_allowed_roots("/etc/passwd")
    assert not source_renamer.is_within_allowed_roots("")


# ---------------------------------------------------------------------------
# Filesystem + DB
# ---------------------------------------------------------------------------

async def test_renames_audio_and_video_and_updates_db(enabled, watch_dirs):
    audio_dir, video_dir = watch_dirs
    audio = _touch(audio_dir / "Twitch DJs Vol 4 (2026-07-15).flac")
    video = _touch(video_dir / "will-see-live-2026-07-15.mkv")
    await _make_mix(audio=audio, video=video)

    result = await source_renamer.rename_sources_for_mix(
        "mix-1", reason="pipeline_complete"
    )

    assert len(result["renamed"]) == 2
    assert not os.path.exists(audio) and not os.path.exists(video)
    assert (audio_dir / f"{EXPECTED_STEM}.flac").exists()
    assert (video_dir / f"{EXPECTED_STEM}.mkv").exists()

    mix = await _get_mix()
    assert mix.audio_file_path == str(audio_dir / f"{EXPECTED_STEM}.flac")
    assert mix.video_file_path == str(video_dir / f"{EXPECTED_STEM}.mkv")


async def test_renamed_pair_still_pairs_and_still_matches(enabled, watch_dirs):
    """The whole premise: renaming must not break the existing matchers."""
    from app.services.catalog_backfill import extract_filename_date
    from app.services.ingest import _extract_date

    audio_dir, video_dir = watch_dirs
    audio = _touch(audio_dir / "Twitch DJs Vol 4 (2026-07-15).flac")
    video = _touch(video_dir / "totally-different-name-2026-07-15.mkv")
    await _make_mix(audio=audio, video=video)

    await source_renamer.rename_sources_for_mix("mix-1", reason="pipeline_complete")
    mix = await _get_mix()
    new_audio = os.path.basename(mix.audio_file_path)
    new_video = os.path.basename(mix.video_file_path)

    # Date token survives, so both matchers keep working.
    assert _extract_date(new_audio) == "2026-07-15"
    assert extract_filename_date(new_audio).isoformat() == "2026-07-15"
    # And the pair now shares an identical stem, which upgrades _find_sibling
    # from its fuzzy same-date branch to its exact-stem branch.
    assert os.path.splitext(new_audio)[0] == os.path.splitext(new_video)[0]


async def test_audio_only_mix_when_video_path_is_none(enabled, watch_dirs):
    audio_dir, _ = watch_dirs
    audio = _touch(audio_dir / "set 2026-07-15.flac")
    await _make_mix(audio=audio, video=None)

    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert [r["kind"] for r in result["renamed"]] == ["audio"]
    mix = await _get_mix()
    assert mix.video_file_path is None


async def test_disabled_setting_is_a_noop(prepared_db, watch_dirs):
    audio_dir, _ = watch_dirs
    audio = _touch(audio_dir / "set 2026-07-15.flac")
    await _make_mix(audio=audio)

    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert result["skipped"] == [{"reason": "disabled"}]
    assert os.path.exists(audio)
    assert (await _get_mix()).audio_file_path == audio


async def test_noop_when_the_name_already_matches(enabled, watch_dirs):
    audio_dir, _ = watch_dirs
    audio = _touch(audio_dir / f"{EXPECTED_STEM}.flac")
    await _make_mix(audio=audio)

    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert result["renamed"] == []
    assert {s["reason"] for s in result["skipped"]} == {"unchanged"}
    assert os.path.exists(audio)


async def test_missing_title_is_skipped(enabled, watch_dirs):
    audio_dir, _ = watch_dirs
    audio = _touch(audio_dir / "set 2026-07-15.flac")
    await _make_mix(audio=audio, title="Untitled")

    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert result["skipped"] == [{"reason": "no_title"}]
    assert os.path.exists(audio)


async def test_collision_suffixes_and_never_overwrites(enabled, watch_dirs):
    audio_dir, _ = watch_dirs
    occupied = audio_dir / f"{EXPECTED_STEM}.flac"
    _touch(occupied, size=999)  # a distinct, precious file
    audio = _touch(audio_dir / "original 2026-07-15.flac", size=2048)
    await _make_mix(audio=audio)

    await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert occupied.stat().st_size == 999  # untouched
    assert (audio_dir / f"{EXPECTED_STEM} (2).flac").exists()
    assert (await _get_mix()).audio_file_path.endswith(f"{EXPECTED_STEM} (2).flac")


async def test_collision_gives_up_rather_than_clobbering(enabled, watch_dirs):
    audio_dir, _ = watch_dirs
    _touch(audio_dir / f"{EXPECTED_STEM}.flac")
    for n in range(2, source_renamer.MAX_COLLISION_ATTEMPTS + 1):
        _touch(audio_dir / f"{EXPECTED_STEM} ({n}).flac")
    audio = _touch(audio_dir / "original 2026-07-15.flac")
    await _make_mix(audio=audio)

    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert {s["reason"] for s in result["skipped"]} == {"collision"}
    assert os.path.exists(audio)
    assert (await _get_mix()).audio_file_path == audio


async def test_missing_source_is_skipped(enabled, watch_dirs):
    audio_dir, _ = watch_dirs
    await _make_mix(audio=str(audio_dir / "vanished 2026-07-15.flac"))

    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert {s["reason"] for s in result["skipped"]} == {"not_found"}
    assert result["errors"] == []


async def test_adopts_an_already_renamed_target(enabled, watch_dirs):
    """Self-heal after a crash between the rename and the commit."""
    audio_dir, _ = watch_dirs
    stale = str(audio_dir / "original 2026-07-15.flac")
    _touch(audio_dir / f"{EXPECTED_STEM}.flac")
    await _make_mix(audio=stale)

    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert {s["reason"] for s in result["skipped"]} == {"adopted"}
    assert (await _get_mix()).audio_file_path == str(
        audio_dir / f"{EXPECTED_STEM}.flac"
    )


async def test_refuses_paths_outside_the_watch_roots(enabled, watch_dirs, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    audio = _touch(outside / "precious 2026-07-15.flac")
    await _make_mix(audio=audio)

    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert {s["reason"] for s in result["skipped"]} == {"outside_allowed_roots"}
    assert os.path.exists(audio)
    assert (await _get_mix()).audio_file_path == audio


async def test_symlinks_are_skipped(enabled, watch_dirs, tmp_path):
    audio_dir, _ = watch_dirs
    real = _touch(tmp_path / "real.flac")
    link = audio_dir / "link 2026-07-15.flac"
    os.symlink(real, link)
    await _make_mix(audio=str(link))

    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert {s["reason"] for s in result["skipped"]} == {"symlink"}
    assert os.path.islink(link)
    assert os.path.exists(real)


async def test_read_only_directory_never_raises(enabled, watch_dirs):
    audio_dir, _ = watch_dirs
    audio = _touch(audio_dir / "set 2026-07-15.flac")
    await _make_mix(audio=audio)

    os.chmod(audio_dir, 0o555)
    try:
        result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")
    finally:
        os.chmod(audio_dir, 0o755)

    assert result["renamed"] == []
    assert [e["reason"] for e in result["errors"]] == ["permission_denied"]
    assert os.path.exists(audio)
    assert (await _get_mix()).audio_file_path == audio


async def test_partial_failure_commits_the_half_that_worked(
    enabled, watch_dirs, monkeypatch
):
    """No rollback: each column independently tracks what is on disk."""
    audio_dir, video_dir = watch_dirs
    audio = _touch(audio_dir / "set 2026-07-15.flac")
    video = _touch(video_dir / "set 2026-07-15.mkv")
    await _make_mix(audio=audio, video=video)

    real_rename = os.rename

    def flaky(src, dst):
        if str(src).endswith(".mkv"):
            raise OSError(13, "Permission denied")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", flaky)
    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert [r["kind"] for r in result["renamed"]] == ["audio"]
    assert [e["kind"] for e in result["errors"]] == ["video"]

    mix = await _get_mix()
    assert mix.audio_file_path == str(audio_dir / f"{EXPECTED_STEM}.flac")
    assert mix.video_file_path == video  # unchanged, matching disk


async def test_never_raises_on_an_unexpected_error(enabled, watch_dirs, monkeypatch):
    audio_dir, _ = watch_dirs
    audio = _touch(audio_dir / "set 2026-07-15.flac")
    await _make_mix(audio=audio)

    def boom(src, dst):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(os, "rename", boom)
    result = await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    assert [e["reason"] for e in result["errors"]] == ["os_error"]
    assert os.path.exists(audio)


async def test_missing_mix_is_skipped(enabled, watch_dirs):
    result = await source_renamer.rename_sources_for_mix("nope", reason="manual")
    assert result["skipped"] == [{"reason": "mix_missing"}]


async def test_dry_run_touches_nothing_and_ignores_the_setting(
    prepared_db, watch_dirs
):
    audio_dir, _ = watch_dirs
    audio = _touch(audio_dir / "original 2026-07-15.flac")
    await _make_mix(audio=audio)

    # Note: no `enabled` fixture — dry run works as a preview while off.
    result = await source_renamer.rename_sources_for_mix(
        "mix-1", reason="manual", dry_run=True
    )

    assert result["planned"] == [
        {
            "kind": "audio",
            "from": audio,
            "to": str(audio_dir / f"{EXPECTED_STEM}.flac"),
        }
    ]
    assert os.path.exists(audio)
    assert (await _get_mix()).audio_file_path == audio


async def test_emits_an_activity_event(enabled, watch_dirs):
    from sqlalchemy import select

    audio_dir, _ = watch_dirs
    await _make_mix(audio=_touch(audio_dir / "original 2026-07-15.flac"))

    await source_renamer.rename_sources_for_mix("mix-1", reason="pipeline_complete")

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(ActivityEvent).where(
                    ActivityEvent.event == "source_files_renamed"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].mix_id == "mix-1"
    assert rows[0].filename == f"{EXPECTED_STEM}.flac"


async def test_skips_are_warned_when_notable(enabled, watch_dirs, tmp_path):
    from sqlalchemy import select

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    await _make_mix(audio=_touch(outside / "precious 2026-07-15.flac"))

    await source_renamer.rename_sources_for_mix("mix-1", reason="manual")

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(ActivityEvent).where(
                    ActivityEvent.event == "source_rename_skipped"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].level == "warn"


# ---------------------------------------------------------------------------
# Pipeline hook
# ---------------------------------------------------------------------------

async def test_mark_complete_invokes_the_renamer(prepared_db, monkeypatch):
    from app.services.pipeline import PipelineOrchestrator

    await _make_mix(audio=None, video=None)
    calls = []

    async def spy(mix_id, *, reason, dry_run=False):
        calls.append((mix_id, reason))
        return {}

    monkeypatch.setattr(source_renamer, "rename_sources_for_mix", spy)
    await PipelineOrchestrator()._mark_complete("mix-1")

    assert calls == [("mix-1", "pipeline_complete")]
    assert (await _get_mix()).pipeline_status == "completed"


async def test_mark_complete_survives_a_raising_renamer(prepared_db, monkeypatch):
    """A rename must never be able to un-complete a finished run."""
    from app.services.pipeline import PipelineOrchestrator

    await _make_mix(audio=None, video=None)

    async def boom(mix_id, *, reason, dry_run=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(source_renamer, "rename_sources_for_mix", boom)
    await PipelineOrchestrator()._mark_complete("mix-1")

    assert (await _get_mix()).pipeline_status == "completed"


# ---------------------------------------------------------------------------
# Manual endpoint
# ---------------------------------------------------------------------------

async def test_rename_source_endpoint_dry_run(client, watch_dirs):
    audio_dir, _ = watch_dirs
    audio = _touch(audio_dir / "original 2026-07-15.flac")
    await _make_mix(audio=audio)

    resp = await client.post("/api/catalog/mixes/mix-1/rename-source?dry_run=true")

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["planned"][0]["to"].endswith(f"{EXPECTED_STEM}.flac")
    assert os.path.exists(audio)


async def test_rename_source_endpoint_404s_for_unknown_mix(client):
    resp = await client.post("/api/catalog/mixes/nope/rename-source")
    assert resp.status_code == 404
