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
    assert "0004_step_progress_and_dedupe" in rev_ids

    heads = script.get_heads()
    assert heads == ["0004_step_progress_and_dedupe"]

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

        step_cols = {c["name"] for c in insp.get_columns("pipeline_steps")}
        assert {"progress", "progress_detail"}.issubset(step_cols)  # added by 0004
        sync_engine.dispose()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_migration_0004_dedupes_duplicate_step_rows():
    """0004 keeps, per (mix_id, step_name), the row with the most recent
    non-null started_at (highest id as tiebreak) and deletes the rest —
    cleaning up the production duplicate-rows bug (initial pending rows +
    one row per orchestrator attempt)."""
    import sqlite3

    from alembic import command

    tmp = os.path.join(tempfile.gettempdir(), "fadeout_migration_dedupe_test.db")
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
