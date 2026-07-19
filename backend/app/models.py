"""SQLAlchemy ORM models for Fade-Out."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid4())


class Mix(Base):
    __tablename__ = "mixes"

    id = Column(String, primary_key=True, default=_uuid)
    title = Column(String, nullable=False)
    audio_file_path = Column(String)
    video_file_path = Column(String)
    duration_seconds = Column(Float)
    genres = Column(JSON)  # list of detected genres
    vibes = Column(JSON)  # list of detected vibes/moods
    energy_profile = Column(JSON)  # energy over time
    tracklist = Column(JSON)  # list of {title, artist, timestamp}
    description_soundcloud = Column(Text)
    description_youtube = Column(Text)
    title_youtube = Column(String)
    tags = Column(JSON)  # list of tags
    cover_art_path = Column(String)  # square SoundCloud art
    thumbnail_path = Column(String)  # 16:9 YouTube thumbnail
    soundcloud_url = Column(String)
    youtube_url = Column(String)
    mixcloud_url = Column(String)
    youtube_playlist_id = Column(String)
    youtube_timestamp_offset = Column(Float, default=0.0)  # seconds to add to FLAC timestamps for YT chapters
    pipeline_status = Column(String, default="pending")
    pipeline_step = Column(String)
    pipeline_error = Column(Text)
    pipeline_started_at = Column(DateTime)
    pipeline_completed_at = Column(DateTime)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    metadata_json = Column(JSON)

    steps = relationship("PipelineStep", back_populates="mix", cascade="all, delete-orphan")
    ai_usages = relationship("AIUsage", back_populates="mix", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="mix", cascade="all, delete-orphan")


class PipelineStep(Base):
    __tablename__ = "pipeline_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mix_id = Column(String, ForeignKey("mixes.id"))
    step_name = Column(String)  # detect, analyze, generate_description, generate_art, upload_soundcloud, upload_youtube, verify_soundcloud, verify_youtube, cross_link
    status = Column(String)  # pending, running, completed, failed, skipped, waiting, interrupted
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    error = Column(Text)
    retry_count = Column(Integer, default=0)
    output_json = Column(JSON)
    progress = Column(Integer, nullable=True)  # 0-100 live step progress
    progress_detail = Column(String, nullable=True)  # e.g. "1.2 GB / 2.9 GB"

    mix = relationship("Mix", back_populates="steps")


class BrandSettings(Base):
    __tablename__ = "brand_settings"

    id = Column(Integer, primary_key=True, default=1)
    brand_name = Column(String, default="Will See")
    description_template = Column(Text)
    color_palette = Column(JSON)  # list of hex colors
    visual_style = Column(Text)  # image gen style prompt
    motifs = Column(JSON)  # brand motifs list
    genre_visual_modifiers = Column(JSON)  # genre-specific visual tweaks
    title_format = Column(String)  # YouTube title template
    youtube_playlists = Column(JSON)  # genre -> playlist mapping
    soundcloud_links = Column(Text)
    youtube_links = Column(Text)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class AIUsage(Base):
    __tablename__ = "ai_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mix_id = Column(String, ForeignKey("mixes.id"), nullable=True)
    provider = Column(String)  # openai, fal
    model = Column(String)
    operation = Column(String)  # description, analysis, image_gen
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, default=func.now())

    mix = relationship("Mix", back_populates="ai_usages")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mix_id = Column(String, ForeignKey("mixes.id"), nullable=True)
    type = Column(String)  # info, success, warning, error
    channel = Column(String)  # discord, email, webhook
    message = Column(Text)
    sent = Column(Boolean, default=False)
    sent_at = Column(DateTime)
    created_at = Column(DateTime, default=func.now())

    mix = relationship("Mix", back_populates="notifications")


class ActivityEvent(Base):
    """Persistent, append-only running log of pipeline + system events.

    Every meaningful thing the app does (a file detected or skipped, a mix
    created, each pipeline stage starting/finishing, upload attempts and their
    result per platform, auth/connect events, and every error) is written here
    so the history survives restarts and can be surfaced live in the UI sidebar
    and via GET /api/activity.
    """

    __tablename__ = "activity_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    level = Column(String, default="info", index=True)  # info, warn, error
    event = Column(String, index=True)  # short machine key, e.g. file_detected
    message = Column(Text)  # short human-readable message
    mix_id = Column(String, ForeignKey("mixes.id", ondelete="SET NULL"), nullable=True, index=True)
    filename = Column(String, nullable=True)
    platform = Column(String, nullable=True)  # soundcloud, youtube, mixcloud
    stage = Column(String, nullable=True)  # pipeline step name
    context = Column(JSON, nullable=True)  # arbitrary structured detail


class AppSettings(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, default=1)
    image_gen_provider = Column(String, default="fal")
    image_gen_model = Column(String, default="fal-ai/flux-pro/v1.1")
    llm_provider = Column(String, default="openai")
    llm_model = Column(String, default="gpt-4o")
    premiere_mode = Column(String, default="scheduled")
    premiere_hour_utc = Column(Integer, default=0)  # midnight UTC = 5 PM Phoenix
    premiere_day = Column(String, default="friday")
    draft_mode = Column(Boolean, default=True)
    auto_upgrade = Column(Boolean, default=True)
    settings_json = Column(JSON)  # catch-all for additional settings
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
