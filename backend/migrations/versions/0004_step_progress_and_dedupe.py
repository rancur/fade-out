"""add pipeline_steps progress columns + dedupe duplicate step rows

Adds ``progress`` (0-100) and ``progress_detail`` to pipeline_steps for live
per-step progress reporting, and dedupes historical duplicate step rows.

Production DBs accumulated DUPLICATE step rows per (mix_id, step_name): an
initial set of eternally-"pending" rows created at mix creation plus the
orchestrator's real result rows (18 rows for a 9-step pipeline). The
orchestrator now upserts by (mix_id, step_name); this migration cleans up the
existing data by keeping, per (mix_id, step_name), the row with the most
recent non-null started_at (highest id as tiebreak) and deleting the rest.

Revision ID: 0004_step_progress_and_dedupe
Revises: 0003_add_activity_events
Create Date: 2026-07-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_step_progress_and_dedupe"
down_revision: Union[str, None] = "0003_add_activity_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pipeline_steps", sa.Column("progress", sa.Integer(), nullable=True))
    op.add_column("pipeline_steps", sa.Column("progress_detail", sa.String(), nullable=True))

    # Dedupe: keep one row per (mix_id, step_name) — the one with the most
    # recent non-null started_at, falling back to the highest id.
    op.execute(
        """
        DELETE FROM pipeline_steps
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY mix_id, step_name
                           ORDER BY (started_at IS NULL) ASC,
                                    started_at DESC,
                                    id DESC
                       ) AS rn
                FROM pipeline_steps
            ) ranked
            WHERE rn = 1
        )
        """
    )


def downgrade() -> None:
    op.drop_column("pipeline_steps", "progress_detail")
    op.drop_column("pipeline_steps", "progress")
