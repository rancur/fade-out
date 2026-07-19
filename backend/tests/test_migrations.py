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


def test_revision_chain_is_linear():
    script = ScriptDirectory.from_config(_config())
    revs = list(script.walk_revisions())
    rev_ids = {r.revision for r in revs}
    assert "0001_initial_schema" in rev_ids
    assert "0002_add_mixcloud_url" in rev_ids
    assert "0003_add_activity_events" in rev_ids
    assert "0005_add_catalog_and_proposals" in rev_ids

    heads = script.get_heads()
    assert heads == ["0005_add_catalog_and_proposals"]

    base = script.get_base()
    assert base == "0001_initial_schema"


def test_upgrade_head_builds_schema_with_mixcloud_url():
    from alembic import command
    from sqlalchemy import create_engine, inspect

    tmp = os.path.join(tempfile.gettempdir(), "fadeout_migration_test.db")
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
        # added by 0005
        assert {"source", "youtube_video_id", "soundcloud_track_id", "title_locked"}.issubset(mix_cols)
        assert "mix_proposals" in tables
        proposal_cols = {c["name"] for c in insp.get_columns("mix_proposals")}
        assert {
            "id", "mix_id", "platform", "field", "current_value", "proposed_value",
            "status", "created_by", "error", "created_at", "updated_at", "applied_at",
        }.issubset(proposal_cols)
        sync_engine.dispose()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_0005_backfills_platform_ids_from_urls():
    """Rows that predate 0005 get youtube_video_id/soundcloud_track_id parsed
    from their stored URLs during the upgrade."""
    from alembic import command
    from sqlalchemy import create_engine, text

    tmp = os.path.join(tempfile.gettempdir(), "fadeout_migration_backfill_test.db")
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
