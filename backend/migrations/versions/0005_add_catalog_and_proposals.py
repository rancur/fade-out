"""add catalog columns to mixes + mix_proposals table

Stage C (mix management / back-catalog):

* ``mixes`` gains ``source`` ("pipeline" | "imported"), platform-native id
  columns ``youtube_video_id`` / ``soundcloud_track_id`` (indexed — these are
  the catalog-sync idempotency keys), and ``title_locked`` (keeper titles the
  AI improver must never propose changes for).
* New ``mix_proposals`` table holding per-field, per-platform proposed changes
  with a full draft -> approved -> applying -> applied/failed lifecycle.
* Backfills ``youtube_video_id`` for existing rows by parsing ``youtube_url``
  (watch?v= / youtu.be forms) and ``soundcloud_track_id`` from any
  ``soundcloud_url`` that carries a numeric track id (api.soundcloud.com
  /tracks/<id> form). Permalink-style SoundCloud URLs carry no track id; those
  rows are backfilled later by the first catalog sync (matched by permalink).

MERGE NOTE: this branch (v2-catalog) was developed in parallel with migration
0004 (pipeline_steps progress columns) which lives on another branch. Here
Follows 0004 (step progress + dedupe) in the linear chain.

Revision ID: 0005_add_catalog_and_proposals
Revises: 0003_add_activity_events
Create Date: 2026-07-19
"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_add_catalog_and_proposals"
down_revision: Union[str, None] = "0004_step_progress_and_dedupe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?(?:[^#\s]*&)?v=|youtu\.be/)([A-Za-z0-9_-]{6,})"
)
_SC_ID_RE = re.compile(r"soundcloud\.com/tracks/(\d+)")


def extract_youtube_video_id(url: str) -> Union[str, None]:
    """Parse a video id out of a watch?v=/youtu.be URL, or None."""
    if not url:
        return None
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


def extract_soundcloud_track_id(url: str) -> Union[str, None]:
    """Parse a numeric track id out of an api.soundcloud.com/tracks URL, or None.

    Public permalink URLs (soundcloud.com/user/slug) carry no numeric id — the
    catalog sync backfills those rows by permalink match instead.
    """
    if not url:
        return None
    m = _SC_ID_RE.search(url)
    return m.group(1) if m else None


def upgrade() -> None:
    op.add_column(
        "mixes",
        sa.Column("source", sa.String(), nullable=False, server_default="pipeline"),
    )
    op.add_column("mixes", sa.Column("youtube_video_id", sa.String(), nullable=True))
    op.add_column("mixes", sa.Column("soundcloud_track_id", sa.String(), nullable=True))
    op.add_column(
        "mixes",
        sa.Column(
            "title_locked", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_index("ix_mixes_youtube_video_id", "mixes", ["youtube_video_id"])
    op.create_index("ix_mixes_soundcloud_track_id", "mixes", ["soundcloud_track_id"])

    op.create_table(
        "mix_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "mix_id", sa.String(), sa.ForeignKey("mixes.id"), nullable=False
        ),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("current_value", sa.Text(), nullable=True),
        sa.Column("proposed_value", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.String(), nullable=False, server_default="ai"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_mix_proposals_mix_id", "mix_proposals", ["mix_id"])
    op.create_index("ix_mix_proposals_status", "mix_proposals", ["status"])

    # --- Backfill platform ids from stored URLs -----------------------------
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, youtube_url, soundcloud_url FROM mixes "
            "WHERE youtube_url IS NOT NULL OR soundcloud_url IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        yt_id = extract_youtube_video_id(row[1] or "")
        sc_id = extract_soundcloud_track_id(row[2] or "")
        if not yt_id and not sc_id:
            continue
        conn.execute(
            sa.text(
                "UPDATE mixes SET "
                "youtube_video_id = COALESCE(:yt, youtube_video_id), "
                "soundcloud_track_id = COALESCE(:sc, soundcloud_track_id) "
                "WHERE id = :id"
            ),
            {"yt": yt_id, "sc": sc_id, "id": row[0]},
        )


def downgrade() -> None:
    op.drop_index("ix_mix_proposals_status", table_name="mix_proposals")
    op.drop_index("ix_mix_proposals_mix_id", table_name="mix_proposals")
    op.drop_table("mix_proposals")
    op.drop_index("ix_mixes_soundcloud_track_id", table_name="mixes")
    op.drop_index("ix_mixes_youtube_video_id", table_name="mixes")
    op.drop_column("mixes", "title_locked")
    op.drop_column("mixes", "soundcloud_track_id")
    op.drop_column("mixes", "youtube_video_id")
    op.drop_column("mixes", "source")
