"""add shorts table (vertical clip auto-uploader)

New ``shorts`` table: one row per vertical OBS Backtrack clip discovered in
``SHORTS_WATCH_PATH``, carrying the ffprobe results (duration/resolution),
the Shazam track ID, the LLM-generated title/description/tags, the YouTube
upload result, and a status lifecycle
(detected|analyzing|ready|queued|uploading|uploaded|failed|skipped).

MERGE-ORDER NOTE: ``down_revision`` points at ``0006_add_uniqueness_registry``
which lives in the parallel uniqueness-registry PR and is NOT present on this
branch. That PR must merge FIRST — until it does, the alembic chain on this
branch alone is intentionally dangling (the migration tests skip with a clear
reason instead of failing).

Revision ID: 0007_add_shorts
Revises: 0006_add_uniqueness_registry
Create Date: 2026-07-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_add_shorts"
down_revision: Union[str, None] = "0006_add_uniqueness_registry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shorts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("file_hash", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("track_artist", sa.String(), nullable=True),
        sa.Column("track_title", sa.String(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("youtube_video_id", sa.String(), nullable=True),
        sa.Column("youtube_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="detected"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("detected_at", sa.DateTime(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_shorts_file_hash", "shorts", ["file_hash"])
    op.create_index("ix_shorts_status", "shorts", ["status"])
    op.create_index("ix_shorts_youtube_video_id", "shorts", ["youtube_video_id"])


def downgrade() -> None:
    op.drop_index("ix_shorts_youtube_video_id", table_name="shorts")
    op.drop_index("ix_shorts_status", table_name="shorts")
    op.drop_index("ix_shorts_file_hash", table_name="shorts")
    op.drop_table("shorts")
