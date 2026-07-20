"""Sanity checks for the Alembic migration setup.

Skipped when alembic isn't installed (e.g. a minimal dev env). When present,
verifies the script directory loads, the revision chain is linear head->base,
and that upgrading a scratch SQLite DB to head produces the expected schema
(including the mixcloud_url column added by 0002).
"""

import os
import tempfile
from typing import Optional

import pytest

pytest.importorskip("alembic")

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config(db_url: Optional[str] = None) -> Config:
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "migrations"))
    if db_url:
        cfg.cmd_opts = type("O", (), {"x": [f"db_url={db_url}"]})()
    return cfg


# 0007_add_shorts revises "0006_add_used_creative", which lives in the
# parallel uniqueness-registry PR and is not present on this branch. Until
# that PR merges (it merges FIRST by agreement) the chain is intentionally
# dangling, so chain-walking tests skip with this reason instead of failing.
# 0007 itself is still covered standalone below.
_CHAIN_SKIP_REASON = (
    "migration chain incomplete on this branch: 0007_add_shorts revises "
    "0006_add_used_creative from the parallel uniqueness PR "
    "(merge order: 0006 first)"
)


def _skip_unless_full_chain() -> None:
    script = ScriptDirectory.from_config(_config())
    try:
        list(script.walk_revisions())
    except Exception:
        pytest.skip(_CHAIN_SKIP_REASON)


def test_revision_chain_is_linear():
    _skip_unless_full_chain()
    script = ScriptDirectory.from_config(_config())
    revs = list(script.walk_revisions())
    rev_ids = {r.revision for r in revs}
    assert "0001_initial_schema" in rev_ids
    assert "0002_add_mixcloud_url" in rev_ids
    assert "0003_add_activity_events" in rev_ids
    assert "0004_step_progress_and_dedupe" in rev_ids
    assert "0005_add_catalog_and_proposals" in rev_ids
    assert "0006_add_used_creative" in rev_ids
    assert "0007_add_shorts" in rev_ids

    heads = script.get_heads()
    assert heads == ["0007_add_shorts"]

    base = script.get_base()
    assert base == "0001_initial_schema"


def test_upgrade_head_builds_schema_with_mixcloud_url():
    _skip_unless_full_chain()
    from alembic import command
    from sqlalchemy import create_engine, inspect

    # PID-suffixed so concurrent pytest runs never share a migration scratch DB.
    tmp = os.path.join(tempfile.gettempdir(), f"fadeout_migration_test_{os.getpid()}.db")
    if os.path.exists(tmp):
        os.remove(tmp)

    url = f"sqlite+aiosqlite:///{tmp}"
    cfg = _config(db_url=url)
    try:
        command.upgrade(cfg, "head")

        sync_engine = create_engine(f"sqlite:///{tmp}")
        insp = inspect(sync_engine)
        tables = set(insp.get_table_names())
        assert {"mixes", "pipeline_steps", "app_settings"}.issubset(tables)

        mix_cols = {c["name"] for c in insp.get_columns("mixes")}
        assert "mixcloud_url" in mix_cols  # added by 0002

        step_cols = {c["name"] for c in insp.get_columns("pipeline_steps")}
        assert {"progress", "progress_detail"}.issubset(step_cols)  # added by 0004

        # added by 0005
        assert {"source", "youtube_video_id", "soundcloud_track_id", "title_locked"}.issubset(mix_cols)
        assert "mix_proposals" in tables
        proposal_cols = {c["name"] for c in insp.get_columns("mix_proposals")}
        assert {
            "id", "mix_id", "platform", "field", "current_value", "proposed_value",
            "status", "created_by", "error", "created_at", "updated_at", "applied_at",
        }.issubset(proposal_cols)

        # added by 0006
        assert "used_creative" in tables
        uc_cols = {c["name"] for c in insp.get_columns("used_creative")}
        assert {
            "id", "kind", "value_normalized", "value_raw", "mix_id", "created_at",
        }.issubset(uc_cols)
        uc_indexes = {i["name"] for i in insp.get_indexes("used_creative")}
        assert "ix_used_creative_value_normalized" in uc_indexes
        assert "ix_used_creative_kind" in uc_indexes

        # added by 0007
        assert "shorts" in tables
        short_cols = {c["name"] for c in insp.get_columns("shorts")}
        assert {
            "id", "file_path", "file_hash", "title", "description", "tags",
            "track_artist", "track_title", "duration_seconds", "width", "height",
            "youtube_video_id", "youtube_url", "status", "error", "detected_at",
            "uploaded_at", "metadata_json",
        }.issubset(short_cols)
        sync_engine.dispose()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_0006_seeds_registry_from_titles_and_proposals():
    """0006 backfills used_creative with existing mix titles plus
    approved/applying/applied title proposals — normalized, series prefix
    stripped, deduped — and never seeds hooks or scenes."""
    from alembic import command
    from sqlalchemy import create_engine, text

    tmp = os.path.join(
        tempfile.gettempdir(), f"fadeout_migration_uniq_seed_test_{os.getpid()}.db"
    )
    if os.path.exists(tmp):
        os.remove(tmp)

    url = f"sqlite+aiosqlite:///{tmp}"
    cfg = _config(db_url=url)
    try:
        command.upgrade(cfg, "0005_add_catalog_and_proposals")

        sync_engine = create_engine(f"sqlite:///{tmp}")
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO mixes (id, title, source, title_locked) VALUES "
                    "('m1', 'Desert Frequencies Vol. III', 'pipeline', 0), "
                    "('m2', 'Will See Wednesdays: Golden Hour', 'imported', 0), "
                    "('m3', 'desert frequencies vol iii', 'imported', 0), "  # dupe of m1 after normalization
                    "('m4', '', 'imported', 0)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO mix_proposals "
                    "(id, mix_id, platform, field, proposed_value, status, created_by) VALUES "
                    "('p1', 'm1', 'both', 'title', 'Freight Train Techno', 'applied', 'ai'), "
                    "('p2', 'm2', 'both', 'title', 'Neon Cactus After Dark', 'approved', 'ai'), "
                    "('p3', 'm3', 'both', 'title', 'Rejected Junk Title', 'rejected', 'ai'), "
                    "('p4', 'm3', 'both', 'title', 'Draft Only Title', 'draft', 'ai'), "
                    "('p5', 'm1', 'both', 'description', 'Not A Title', 'applied', 'ai')"
                )
            )

        command.upgrade(cfg, "head")

        with sync_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT kind, value_normalized, value_raw, mix_id FROM used_creative")
            ).fetchall()
        sync_engine.dispose()

        assert all(r[0] == "title" for r in rows)  # no hooks/scenes seeded
        normalized = {r[1] for r in rows}
        assert "desert frequencies vol iii" in normalized
        # series prefix stripped before comparison
        assert "golden hour" in normalized
        assert "freight train techno" in normalized
        assert "neon cactus after dark" in normalized
        # rejected/draft proposals, non-title fields, empty titles: not seeded
        assert "rejected junk title" not in normalized
        assert "draft only title" not in normalized
        assert "not a title" not in normalized
        # dedup: m1's title and m3's normalized twin land as ONE row
        assert len([r for r in rows if r[1] == "desert frequencies vol iii"]) == 1
        assert len(rows) == 4
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_0005_backfills_platform_ids_from_urls():
    """Rows that predate 0005 get youtube_video_id/soundcloud_track_id parsed
    from their stored URLs during the upgrade."""
    _skip_unless_full_chain()
    from alembic import command
    from sqlalchemy import create_engine, text

    tmp = os.path.join(tempfile.gettempdir(), f"fadeout_migration_backfill_test_{os.getpid()}.db")
    if os.path.exists(tmp):
        os.remove(tmp)

    url = f"sqlite+aiosqlite:///{tmp}"
    cfg = _config(db_url=url)
    try:
        command.upgrade(cfg, "0003_add_activity_events")

        sync_engine = create_engine(f"sqlite:///{tmp}")
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO mixes (id, title, youtube_url, soundcloud_url) VALUES "
                    "('m1', 'Both URLs', 'https://www.youtube.com/watch?v=abc123DEF45', "
                    " 'https://api.soundcloud.com/tracks/987654321'), "
                    "('m2', 'Short YT + permalink SC', 'https://youtu.be/xyz789ABC12', "
                    " 'https://soundcloud.com/thewillsee/some-mix'), "
                    "('m3', 'No URLs', NULL, NULL)"
                )
            )

        command.upgrade(cfg, "head")

        with sync_engine.connect() as conn:
            rows = {
                r[0]: (r[1], r[2], r[3], r[4])
                for r in conn.execute(
                    text(
                        "SELECT id, youtube_video_id, soundcloud_track_id, source, title_locked "
                        "FROM mixes"
                    )
                )
            }
        assert rows["m1"][0] == "abc123DEF45"
        assert rows["m1"][1] == "987654321"
        assert rows["m2"][0] == "xyz789ABC12"
        assert rows["m2"][1] is None  # permalink carries no numeric id
        assert rows["m3"][0] is None and rows["m3"][1] is None
        # server defaults applied to pre-existing rows
        assert all(v[2] == "pipeline" for v in rows.values())
        assert all(not v[3] for v in rows.values())
        sync_engine.dispose()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_migration_0004_dedupes_duplicate_step_rows():
    """0004 keeps, per (mix_id, step_name), the row with the most recent
    non-null started_at (highest id as tiebreak) and deletes the rest —
    cleaning up the production duplicate-rows bug (initial pending rows +
    one row per orchestrator attempt)."""
    _skip_unless_full_chain()
    import sqlite3

    from alembic import command

    # PID-suffixed so concurrent pytest runs never share a migration scratch DB.
    tmp = os.path.join(tempfile.gettempdir(), f"fadeout_migration_dedupe_test_{os.getpid()}.db")
    if os.path.exists(tmp):
        os.remove(tmp)

    url = f"sqlite+aiosqlite:///{tmp}"
    cfg = _config(db_url=url)
    try:
        command.upgrade(cfg, "0003_add_activity_events")

        conn = sqlite3.connect(tmp)
        conn.executemany(
            "INSERT INTO pipeline_steps (mix_id, step_name, status, started_at, retry_count)"
            " VALUES (?, ?, ?, ?, 0)",
            [
                # analyze: eternally-pending initial row + two real attempts
                ("m1", "analyze", "pending", None),
                ("m1", "analyze", "failed", "2026-07-17 09:00:00"),
                ("m1", "analyze", "completed", "2026-07-18 10:00:00"),
                # detect: pending row + one attempt
                ("m1", "detect", "pending", None),
                ("m1", "detect", "completed", "2026-07-18 09:55:00"),
                # upload: two pending rows, no attempt yet (highest id wins)
                ("m1", "upload_soundcloud", "pending", None),
                ("m1", "upload_soundcloud", "pending", None),
                # other mix untouched
                ("m2", "analyze", "completed", "2026-07-16 12:00:00"),
            ],
        )
        conn.commit()
        keeper_upload_id = conn.execute(
            "SELECT MAX(id) FROM pipeline_steps"
            " WHERE mix_id='m1' AND step_name='upload_soundcloud'"
        ).fetchone()[0]
        conn.close()

        command.upgrade(cfg, "head")

        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT mix_id, step_name, status, id FROM pipeline_steps"
            " ORDER BY mix_id, step_name"
        ).fetchall()
        conn.close()

        by_key = {(r[0], r[1]): r for r in rows}
        assert len(rows) == 4  # one per (mix_id, step_name)
        assert by_key[("m1", "analyze")][2] == "completed"  # latest started_at wins
        assert by_key[("m1", "detect")][2] == "completed"
        assert by_key[("m1", "upload_soundcloud")][3] == keeper_upload_id
        assert by_key[("m2", "analyze")][2] == "completed"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_0007_add_shorts_schema_standalone():
    """0007 cannot run through the chain pre-merge (its down_revision,
    0006_add_used_creative, lives in the parallel uniqueness PR), so
    exercise its upgrade()/downgrade() directly against a scratch DB via an
    Operations context. This test keeps working after the chains merge."""
    import importlib.util

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    path = os.path.join(BACKEND_DIR, "migrations", "versions", "0007_add_shorts.py")
    spec = importlib.util.spec_from_file_location("mig_0007_add_shorts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0007_add_shorts"
    # String constant by agreement with the uniqueness PR (merge order: 0006 first).
    assert module.down_revision == "0006_add_used_creative"

    tmp = os.path.join(tempfile.gettempdir(), f"fadeout_shorts_migration_test_{os.getpid()}.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    engine = create_engine(f"sqlite:///{tmp}")
    try:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(connection=conn)
            with Operations.context(ctx):
                module.upgrade()

        insp = inspect(engine)
        assert "shorts" in insp.get_table_names()
        cols = {c["name"] for c in insp.get_columns("shorts")}
        assert {
            "id", "file_path", "file_hash", "title", "description", "tags",
            "track_artist", "track_title", "duration_seconds", "width", "height",
            "youtube_video_id", "youtube_url", "status", "error", "detected_at",
            "uploaded_at", "metadata_json",
        }.issubset(cols)
        index_names = {i["name"] for i in insp.get_indexes("shorts")}
        assert {
            "ix_shorts_file_hash", "ix_shorts_status", "ix_shorts_youtube_video_id",
        }.issubset(index_names)

        with engine.begin() as conn:
            ctx = MigrationContext.configure(connection=conn)
            with Operations.context(ctx):
                module.downgrade()
        insp = inspect(engine)
        assert "shorts" not in insp.get_table_names()
    finally:
        engine.dispose()
        if os.path.exists(tmp):
            os.remove(tmp)
