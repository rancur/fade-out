"""Application configuration using pydantic-settings."""

from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configurable values for the Fade-Out application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- File Watching ---
    WATCH_AUDIO_PATH: str = "/watch/audio"
    WATCH_VIDEO_PATH: str = "/watch/video"
    OUTPUT_THUMBNAILS_PATH: str = "/output/thumbnails"
    OUTPUT_COVER_ART_PATH: str = "/output/cover-art"
    DJCTL_CUE_PATH: str = "/watch/djctl-cue"
    DJCTL_WS_URL: str = "ws://192.168.1.221:8081/ws"

    # --- Database ---
    DATABASE_URL: str = "sqlite:///./data/fadeout.db"

    # --- OpenAI ---
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"

    # --- Fal (Image Generation) ---
    FAL_API_KEY: str = ""
    FAL_MODEL: str = "fal-ai/flux-pro/v1.1"

    # --- SoundCloud ---
    SOUNDCLOUD_CLIENT_ID: str = ""
    SOUNDCLOUD_CLIENT_SECRET: str = ""
    SOUNDCLOUD_ACCESS_TOKEN: str = ""  # OAuth access token (obtained via auth flow)
    SOUNDCLOUD_REFRESH_TOKEN: str = ""  # OAuth refresh token
    SOUNDCLOUD_EMAIL: str = ""  # Fallback for password grant or browser auth
    SOUNDCLOUD_PASSWORD: str = ""  # Fallback for password grant or browser auth

    # --- YouTube ---
    YOUTUBE_API_KEY: str = ""  # Data API key for reads (playlists, verification)
    YOUTUBE_CLIENT_ID: str = ""  # OAuth client ID (required for uploads)
    YOUTUBE_CLIENT_SECRET: str = ""  # OAuth client secret (required for uploads)
    YOUTUBE_REFRESH_TOKEN: str = ""  # OAuth refresh token (required for uploads)
    YOUTUBE_CHANNEL_ID: str = ""
    YOUTUBE_DEFAULT_PLAYLIST_PREFIX: str = "Will See"

    # --- Premiere ---
    PREMIERE_MODE: str = "scheduled"  # instant, scheduled, unlisted
    PREMIERE_SCHEDULE_STRATEGY: str = "optimal_next_24h"
    PREMIERE_DEFAULT_HOUR: int = 19  # 7 PM EST / 4 PM Phoenix
    PREMIERE_DEFAULT_DAY: str = "friday"

    # --- Notifications ---
    NOTIFICATION_DISCORD_WEBHOOK_URL: str = ""
    NOTIFICATION_EMAIL_SMTP_HOST: str = ""
    NOTIFICATION_EMAIL_SMTP_PORT: int = 587
    NOTIFICATION_EMAIL_SMTP_USER: str = ""
    NOTIFICATION_EMAIL_SMTP_PASSWORD: str = ""
    NOTIFICATION_EMAIL_TO: str = ""
    NOTIFICATION_WEBHOOK_URLS: str = ""  # comma-separated

    # --- Auto-Upgrade ---
    GITHUB_REPO: str = "rancur/fade-out"
    AUTO_UPGRADE_ENABLED: bool = True
    AUTO_UPGRADE_CHECK_INTERVAL_HOURS: int = 6

    # --- Settings Backup ---
    SETTINGS_BACKUP_ENABLED: bool = True
    SETTINGS_BACKUP_INTERVAL_HOURS: int = 24

    # --- AI Budget ---
    AI_MONTHLY_BUDGET_USD: float = 50.0

    # --- Brand ---
    BRAND_NAME: str = "Will See"
    SOUNDCLOUD_LINKS: str = (
        "Twitch: https://www.twitch.tv/thewillsee\n"
        "YouTube: https://www.youtube.com/@willseetv\n"
        "Website: https://www.thewillsee.com\n"
        "Shop: https://shop.thewillsee.com"
    )
    YOUTUBE_LINKS: str = (
        "Twitch: https://www.twitch.tv/thewillsee\n"
        "SoundCloud: https://www.soundcloud.com/thewillsee\n"
        "Website: https://www.thewillsee.com\n"
        "Shop: https://shop.thewillsee.com"
    )

    # --- Public URL (required for OAuth callbacks with Google/SoundCloud) ---
    PUBLIC_URL: str = ""  # e.g. https://fadeout.example.com — must be a real domain for Google OAuth

    # --- Logging ---
    LOG_LEVEL: str = "INFO"

    # --- Pipeline ---
    DRAFT_MODE: bool = True
    FILE_STABLE_SECONDS: int = 120
    AUDIO_SAMPLE_INTERVAL_SECONDS: int = 300
    MAX_CONCURRENT_PIPELINES: int = 2

    @field_validator("NOTIFICATION_WEBHOOK_URLS", mode="before")
    @classmethod
    def _parse_webhook_urls(cls, v: str) -> str:
        return v

    def get_webhook_urls(self) -> List[str]:
        """Return NOTIFICATION_WEBHOOK_URLS split into a list."""
        if not self.NOTIFICATION_WEBHOOK_URLS:
            return []
        return [u.strip() for u in self.NOTIFICATION_WEBHOOK_URLS.split(",") if u.strip()]


settings = Settings()
