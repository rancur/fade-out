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

    heads = script.get_heads()
    assert heads == ["0002_add_mixcloud_url"]

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
        sync_engine.dispose()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
