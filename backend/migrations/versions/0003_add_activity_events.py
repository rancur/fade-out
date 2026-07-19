"""add activity_events table

Adds the persistent running activity/event log surfaced via GET /api/activity
and the UI sidebar. Append-only; one row per pipeline/system event.

Revision ID: 0003_add_activity_events
Revises: 0002_add_mixcloud_url
Create Date: 2026-07-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_add_activity_events"
down_revision: Union[str, None] = "0002_add_mixcloud_url"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "activity_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(), nullable=True),
        sa.Column("level", sa.String(), nullable=True),
        sa.Column("event", sa.String(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("mix_id", sa.String(), nullable=True),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("platform", sa.String(), nullable=True),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
    )
    op.create_index("ix_activity_events_ts", "activity_events", ["ts"])
    op.create_index("ix_activity_events_level", "activity_events", ["level"])
    op.create_index("ix_activity_events_event", "activity_events", ["event"])
    op.create_index("ix_activity_events_mix_id", "activity_events", ["mix_id"])


def downgrade() -> None:
    op.drop_index("ix_activity_events_mix_id", table_name="activity_events")
    op.drop_index("ix_activity_events_event", table_name="activity_events")
    op.drop_index("ix_activity_events_level", table_name="activity_events")
    op.drop_index("ix_activity_events_ts", table_name="activity_events")
    op.drop_table("activity_events")
