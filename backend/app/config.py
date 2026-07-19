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
    # When False (default) the watcher ignores files already present at startup
    # and only ingests files created/modified while it is running. The watch
    # folders permanently hold a large back-catalog (existing sets + every raw
    # Twitch recording); sweeping them on every start would stampede the
    # pipeline and re-hash hundreds of GB. Set True only for an empty/dedicated
    # inbox folder where re-scanning offline arrivals is actually wanted.
    WATCH_INGEST_EXISTING_ON_START: bool = False
    OUTPUT_THUMBNAILS_PATH: str = "/output/thumbnails"
    OUTPUT_COVER_ART_PATH: str = "/output/cover-art"
    DJCTL_CUE_PATH: str = "/watch/djctl-cue"
    DJCTL_WS_URL: str = "ws://192.168.1.221:8081/ws"

    # --- Database ---
    DATABASE_URL: str = "sqlite:////data/fadeout.db"

    # --- OpenAI ---
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"

    # --- Fal (Image Generation) ---
    FAL_API_KEY: str = ""
    FAL_MODEL: str = "fal-ai/flux-pro/v1.1"

    # --- Track Recognition (fallback provider) ---
    # AudD (https://audd.io) is a paid fingerprint API that is markedly more
    # accurate than the unofficial Shazam client on layered/transitioning DJ
    # audio. Leave blank to disable; when set, it is used ONLY as a fallback for
    # segments Shazam fails to identify. Get a token at https://dashboard.audd.io
    AUDD_API_TOKEN: str = ""

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
    # Daily YouTube Data API quota budget reserved for catalog apply writes.
    # Each write (videos.update / thumbnails.set / playlistItems.*) costs ~50
    # units; the true daily cap is 10k, so 8k leaves headroom for uploads.
    YOUTUBE_DAILY_QUOTA_BUDGET: int = 8000

    # --- Mixcloud ---
    # Mixcloud is an optional third publishing target that mirrors the
    # SoundCloud/YouTube uploader pattern. It needs an OAuth access token Will
    # has not set up yet, so the whole path is gated behind MIXCLOUD_ENABLED and
    # ships OFF by default -- the upload/verify steps no-op (skip) until the flag
    # is flipped AND a token is present. Get a token via the OAuth flow at
    # https://www.mixcloud.com/developers/ (client id/secret -> access token).
    MIXCLOUD_ENABLED: bool = False
    MIXCLOUD_ACCESS_TOKEN: str = ""  # OAuth access token (required for uploads)
    MIXCLOUD_CLIENT_ID: str = ""  # OAuth client id (for future token refresh)
    MIXCLOUD_CLIENT_SECRET: str = ""  # OAuth client secret (for future token refresh)

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

    # --- Activity log retention ---
    # A nightly task prunes activity_events older than the retention window or
    # beyond the row cap (whichever bites first), keeping the feed queryable
    # without letting the table grow without bound.
    ACTIVITY_RETENTION_DAYS: int = 90
    ACTIVITY_MAX_ROWS: int = 50_000

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
    LOG_JSON: bool = False  # emit structured JSON logs instead of plain text

    # --- Cross-linking ---
    # When True, the cross_link step pushes reciprocal links to the LIVE
    # platform descriptions (YouTube videos.update + SoundCloud PUT /tracks/:id)
    # using the existing OAuth tokens -- no new credentials required. ON by
    # default: with it off the cross-links only ever landed in the DB and the
    # published descriptions never carried them (observed live). Every push is
    # best-effort and can never fail the pipeline's final step; set to False to
    # opt out of mutating published descriptions.
    CROSS_LINK_PUSH_ENABLED: bool = True

    # --- Track-detection merge ---
    # Gap-filling detections (Shazam/AudD) scoring strictly below this confidence
    # keep their timestamp but render as the "ID - ID" placeholder instead of
    # asserting a probably-wrong name. The default preserves current recall
    # (single-hit Shazam sits exactly at this level); raise toward 0.6 to be
    # stricter and turn weak guesses into honest "ID" markers. CUE/DJCTL entries
    # are authoritative and always win an overlap regardless of this value.
    DETECTION_NAME_CONFIDENCE_THRESHOLD: float = 0.50

    # --- Pipeline ---
    DRAFT_MODE: bool = True
    FILE_STABLE_SECONDS: int = 120
    AUDIO_SAMPLE_INTERVAL_SECONDS: int = 75  # sample every 75s (denser = better track recall)
    MAX_CONCURRENT_PIPELINES: int = 2

    # --- Partial-file safety ---
    # A file is only ingested once its size has stopped changing (see
    # FILE_STABLE_SECONDS) AND it clears this floor. The floor rejects the
    # 0-byte / stray-file class outright: a real recorded set is always well
    # over a megabyte, so anything smaller is a half-written or junk drop.
    MIN_FILE_SIZE_BYTES: int = 1_048_576  # 1 MB
    # Number of consecutive size-stable polls required before ingest. Combined
    # with the time window this guards against a slow SMB copy that briefly
    # pauses mid-write from being mistaken for a finished file.
    STABILITY_CONFIRMATIONS: int = 2

    # --- Out-of-order pairing ---
    # A lone audio-only or video-only drop waits in a PENDING state for its
    # date-matched sibling this long before the coordinator gives up waiting.
    # Video for a 3-hour set can take far longer to copy over SMB than the
    # audio, so this defaults generous. On expiry an audio-only drop still runs
    # (SoundCloud-only is a valid outcome); a video-only drop is logged as
    # unpaired/expired and never run (audio is mandatory).
    PAIRING_WAIT_SECONDS: int = 7200  # 2 hours
    PAIRING_SWEEP_INTERVAL_SECONDS: int = 30

    # --- Catalog tracklist backfill ---
    # Extra directories (comma-separated) scanned for back-catalog audio in
    # addition to WATCH_AUDIO_PATH when matching local files to imported mixes
    # that have no tracklist. Leave blank to scan the watch folder only.
    CATALOG_EXTRA_AUDIO_PATHS: str = ""

    # --- Disk safety ---
    # Ingest is refused (and surfaced in the activity log + health endpoint)
    # when free space on the output volume drops below this, so a multi-GB set
    # never half-processes into a full disk.
    MIN_FREE_DISK_GB: float = 5.0

    @field_validator("NOTIFICATION_WEBHOOK_URLS", mode="before")
    @classmethod
    def _parse_webhook_urls(cls, v: str) -> str:
        return v

    def get_catalog_extra_audio_paths(self) -> List[str]:
        """Return CATALOG_EXTRA_AUDIO_PATHS split into a list of directories."""
        if not self.CATALOG_EXTRA_AUDIO_PATHS:
            return []
        return [
            p.strip()
            for p in self.CATALOG_EXTRA_AUDIO_PATHS.split(",")
            if p.strip()
        ]

    def get_webhook_urls(self) -> List[str]:
        """Return NOTIFICATION_WEBHOOK_URLS split into a list."""
        if not self.NOTIFICATION_WEBHOOK_URLS:
            return []
        return [u.strip() for u in self.NOTIFICATION_WEBHOOK_URLS.split(",") if u.strip()]


settings = Settings()
