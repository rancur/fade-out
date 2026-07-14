"""initial schema baseline

Baseline capturing the schema as it existed when Alembic was introduced (the
tables previously produced by ``Base.metadata.create_all``). For a brand-new
deployment this creates everything from scratch. For an existing deployment
whose tables were already created via ``create_all``, stamp this revision
instead of running it::

    alembic stamp 0001_initial_schema
    alembic upgrade head   # applies later revisions only

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mixes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("audio_file_path", sa.String()),
        sa.Column("video_file_path", sa.String()),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("genres", sa.JSON()),
        sa.Column("vibes", sa.JSON()),
        sa.Column("energy_profile", sa.JSON()),
        sa.Column("tracklist", sa.JSON()),
        sa.Column("description_soundcloud", sa.Text()),
        sa.Column("description_youtube", sa.Text()),
        sa.Column("title_youtube", sa.String()),
        sa.Column("tags", sa.JSON()),
        sa.Column("cover_art_path", sa.String()),
        sa.Column("thumbnail_path", sa.String()),
        sa.Column("soundcloud_url", sa.String()),
        sa.Column("youtube_url", sa.String()),
        sa.Column("youtube_playlist_id", sa.String()),
        sa.Column("youtube_timestamp_offset", sa.Float()),
        sa.Column("pipeline_status", sa.String()),
        sa.Column("pipeline_step", sa.String()),
        sa.Column("pipeline_error", sa.Text()),
        sa.Column("pipeline_started_at", sa.DateTime()),
        sa.Column("pipeline_completed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("metadata_json", sa.JSON()),
    )

    op.create_table(
        "pipeline_steps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mix_id", sa.String(), sa.ForeignKey("mixes.id")),
        sa.Column("step_name", sa.String()),
        sa.Column("status", sa.String()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("error", sa.Text()),
        sa.Column("retry_count", sa.Integer()),
        sa.Column("output_json", sa.JSON()),
    )

    op.create_table(
        "brand_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brand_name", sa.String()),
        sa.Column("description_template", sa.Text()),
        sa.Column("color_palette", sa.JSON()),
        sa.Column("visual_style", sa.Text()),
        sa.Column("motifs", sa.JSON()),
        sa.Column("genre_visual_modifiers", sa.JSON()),
        sa.Column("title_format", sa.String()),
        sa.Column("youtube_playlists", sa.JSON()),
        sa.Column("soundcloud_links", sa.Text()),
        sa.Column("youtube_links", sa.Text()),
        sa.Column("updated_at", sa.DateTime()),
    )

    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mix_id", sa.String(), sa.ForeignKey("mixes.id"), nullable=True),
        sa.Column("provider", sa.String()),
        sa.Column("model", sa.String()),
        sa.Column("operation", sa.String()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mix_id", sa.String(), sa.ForeignKey("mixes.id"), nullable=True),
        sa.Column("type", sa.String()),
        sa.Column("channel", sa.String()),
        sa.Column("message", sa.Text()),
        sa.Column("sent", sa.Boolean()),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("image_gen_provider", sa.String()),
        sa.Column("image_gen_model", sa.String()),
        sa.Column("llm_provider", sa.String()),
        sa.Column("llm_model", sa.String()),
        sa.Column("premiere_mode", sa.String()),
        sa.Column("premiere_hour_utc", sa.Integer()),
        sa.Column("premiere_day", sa.String()),
        sa.Column("draft_mode", sa.Boolean()),
        sa.Column("auto_upgrade", sa.Boolean()),
        sa.Column("settings_json", sa.JSON()),
        sa.Column("updated_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_table("notifications")
    op.drop_table("ai_usage")
    op.drop_table("brand_settings")
    op.drop_table("pipeline_steps")
    op.drop_table("mixes")
