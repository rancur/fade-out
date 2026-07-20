"""Effective application configuration: DB (AppSettings) over env defaults.

Generalizes the notification_service pattern to the whole app config. Every
setting in ``SETTINGS_SCHEMA`` resolves in this order:

1. ``AppSettings.settings_json[key]`` (an explicit in-app override),
2. the matching ``AppSettings`` column for column-backed settings
   (draft_mode, premiere_*, llm_*, image_gen_*, auto_upgrade),
3. the env default from ``app.config.settings`` (pydantic-settings).

``resolve()`` / ``get_all()`` read the AppSettings singleton row with their
own session and cache the snapshot for 60s — services running outside any
request scope (pipeline, catalog workers, pruning tasks) get DB-configured
values without a query per read. ``invalidate_cache()`` is called by the
settings API on every write so changes apply immediately.

Secrets (``type == "secret"``) are stored like everything else but are never
serialized back out: ``describe()`` reports only ``has_value``.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings as env_settings
from app.database import async_session_factory
from app.models import AppSettings

logger = logging.getLogger(__name__)

CONFIG_CACHE_SECONDS = 60

# Category display order (drives the Settings page tabs).
CATEGORIES: Tuple[str, ...] = (
    "Paths",
    "Pipeline",
    "AI",
    "YouTube",
    "SoundCloud",
    "Activity",
    "Advanced",
)

MOUNT_HINT = (
    "Bound to a container mount at deploy time (docker-compose volumes) — "
    "change the mount, not this value."
)


@dataclass(frozen=True)
class SettingDef:
    """One entry in the settings catalog."""

    key: str            # canonical lowercase key (settings_json key)
    label: str
    help: str
    type: str           # str | int | float | bool | enum | path | secret
    category: str
    env_attr: Optional[str] = None   # attribute on app.config.settings
    column: Optional[str] = None     # AppSettings column name, if column-backed
    editable: bool = True            # False = display-only (deploy/startup-bound)
    choices: Optional[Tuple[str, ...]] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    fallback: Any = None             # static default when no env_attr


SETTINGS_SCHEMA: Tuple[SettingDef, ...] = (
    # --- Paths (mount-bound, display-only) --------------------------------
    SettingDef(
        key="watch_audio_path",
        label="Audio watch folder",
        help=f"Folder watched for new audio drops. {MOUNT_HINT}",
        type="path", category="Paths", env_attr="WATCH_AUDIO_PATH", editable=False,
    ),
    SettingDef(
        key="watch_video_path",
        label="Video watch folder",
        help=f"Folder watched for new video drops. {MOUNT_HINT}",
        type="path", category="Paths", env_attr="WATCH_VIDEO_PATH", editable=False,
    ),
    SettingDef(
        key="djctl_cue_path",
        label="djctl CUE folder",
        help=f"Folder scanned for djctl CUE sheets used as authoritative tracklists. {MOUNT_HINT}",
        type="path", category="Paths", env_attr="DJCTL_CUE_PATH", editable=False,
    ),
    SettingDef(
        key="output_thumbnails_path",
        label="Thumbnails output folder",
        help=f"Where generated YouTube thumbnails are written. {MOUNT_HINT}",
        type="path", category="Paths", env_attr="OUTPUT_THUMBNAILS_PATH", editable=False,
    ),
    SettingDef(
        key="output_cover_art_path",
        label="Cover art output folder",
        help=f"Where generated SoundCloud cover art is written. {MOUNT_HINT}",
        type="path", category="Paths", env_attr="OUTPUT_COVER_ART_PATH", editable=False,
    ),
    # --- Pipeline ----------------------------------------------------------
    SettingDef(
        key="draft_mode",
        label="Draft mode",
        help="Pause every pipeline for manual review after artwork is generated. "
             "Uploads only run after you approve the draft.",
        type="bool", category="Pipeline", env_attr="DRAFT_MODE", column="draft_mode",
    ),
    SettingDef(
        key="premiere_mode",
        label="Premiere mode",
        help="How YouTube uploads go live: instant (public immediately), "
             "scheduled (premiere at the configured day/hour), or unlisted.",
        type="enum", category="Pipeline", env_attr="PREMIERE_MODE",
        column="premiere_mode", choices=("instant", "scheduled", "unlisted"),
    ),
    SettingDef(
        key="premiere_day",
        label="Premiere day",
        help="Preferred weekday for scheduled YouTube premieres.",
        type="enum", category="Pipeline", env_attr="PREMIERE_DEFAULT_DAY",
        column="premiere_day",
        choices=("monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday"),
    ),
    SettingDef(
        key="premiere_hour_utc",
        label="Premiere hour (UTC)",
        help="Hour of day (UTC, 0-23) for scheduled YouTube premieres.",
        type="int", category="Pipeline", column="premiere_hour_utc",
        min_value=0, max_value=23, fallback=0,
    ),
    SettingDef(
        key="detection_name_confidence_threshold",
        label="Track-name confidence threshold",
        help="Gap-filling detections (Shazam/AudD) scoring below this keep their "
             "timestamp but render as the 'ID - ID' placeholder. Raise toward 0.6 "
             "to be stricter; CUE/djctl entries always win regardless.",
        type="float", category="Pipeline",
        env_attr="DETECTION_NAME_CONFIDENCE_THRESHOLD",
        min_value=0.0, max_value=1.0,
    ),
    SettingDef(
        key="file_stable_seconds",
        label="File stability window (s)",
        help="A dropped file must stop changing size for this long before ingest. "
             "Read when the file watcher starts — set via env and restart to change.",
        type="int", category="Pipeline", env_attr="FILE_STABLE_SECONDS", editable=False,
    ),
    SettingDef(
        key="pairing_wait_seconds",
        label="Audio/video pairing wait (s)",
        help="How long a lone audio-only or video-only drop waits for its "
             "date-matched sibling. Read at startup — set via env.",
        type="int", category="Pipeline", env_attr="PAIRING_WAIT_SECONDS", editable=False,
    ),
    SettingDef(
        key="max_concurrent_pipelines",
        label="Max concurrent pipelines",
        help="Upper bound on pipelines processing at once. Read at startup — "
             "set via env and restart to change.",
        type="int", category="Pipeline", env_attr="MAX_CONCURRENT_PIPELINES",
        editable=False,
    ),
    # --- AI ----------------------------------------------------------------
    SettingDef(
        key="llm_provider",
        label="LLM provider",
        help="Provider used for descriptions, titles, and tags.",
        type="enum", category="AI", column="llm_provider",
        choices=("openai",), fallback="openai",
    ),
    SettingDef(
        key="llm_model",
        label="LLM model",
        help="Model used for descriptions, titles, and tags (e.g. gpt-4o).",
        type="str", category="AI", env_attr="OPENAI_MODEL", column="llm_model",
    ),
    SettingDef(
        key="image_gen_provider",
        label="Image provider",
        help="Provider used for cover art and thumbnail scene generation.",
        type="enum", category="AI", column="image_gen_provider",
        choices=("fal", "openai"), fallback="fal",
    ),
    SettingDef(
        key="image_gen_model",
        label="Image model",
        help="Image generation model (e.g. fal-ai/flux-pro/v1.1).",
        type="str", category="AI", env_attr="FAL_MODEL", column="image_gen_model",
    ),
    SettingDef(
        key="ai_monthly_budget_usd",
        label="Monthly AI budget (USD)",
        help="Soft budget for AI spend shown on the AI Usage page.",
        type="float", category="AI", env_attr="AI_MONTHLY_BUDGET_USD", min_value=0,
    ),
    SettingDef(
        key="openai_api_key",
        label="OpenAI API key",
        help="Used for descriptions/titles/tags. Write-only: the saved value is "
             "never shown.",
        type="secret", category="AI", env_attr="OPENAI_API_KEY",
    ),
    SettingDef(
        key="fal_api_key",
        label="Fal API key",
        help="Used for image generation. Write-only.",
        type="secret", category="AI", env_attr="FAL_API_KEY",
    ),
    SettingDef(
        key="audd_api_token",
        label="AudD API token",
        help="Optional paid fingerprint fallback for track detection "
             "(dashboard.audd.io). Write-only.",
        type="secret", category="AI", env_attr="AUDD_API_TOKEN",
    ),
    # --- YouTube -------------------------------------------------------------
    SettingDef(
        key="youtube_daily_quota_budget",
        label="Daily quota budget (units)",
        help="YouTube Data API quota reserved per day for catalog writes. Each "
             "write costs ~50 units; the true daily cap is 10,000.",
        type="int", category="YouTube", env_attr="YOUTUBE_DAILY_QUOTA_BUDGET",
        min_value=0, max_value=10_000,
    ),
    SettingDef(
        key="youtube_default_playlist_prefix",
        label="Playlist prefix",
        help="Prefix for auto-created genre playlists (e.g. 'Will See | House Mixes').",
        type="str", category="YouTube", env_attr="YOUTUBE_DEFAULT_PLAYLIST_PREFIX",
    ),
    SettingDef(
        key="youtube_channel_id",
        label="Channel ID",
        help="Used for read-only channel lookups when only an API key is configured.",
        type="str", category="YouTube", env_attr="YOUTUBE_CHANNEL_ID",
    ),
    SettingDef(
        key="youtube_api_key",
        label="API key",
        help="Data API key for reads (playlists, verification). Write-only.",
        type="secret", category="YouTube", env_attr="YOUTUBE_API_KEY",
    ),
    SettingDef(
        key="youtube_client_id",
        label="OAuth client ID",
        help="Google OAuth client ID (required for uploads). Write-only.",
        type="secret", category="YouTube", env_attr="YOUTUBE_CLIENT_ID",
    ),
    SettingDef(
        key="youtube_client_secret",
        label="OAuth client secret",
        help="Google OAuth client secret (required for uploads). Write-only.",
        type="secret", category="YouTube", env_attr="YOUTUBE_CLIENT_SECRET",
    ),
    SettingDef(
        key="youtube_refresh_token",
        label="OAuth refresh token",
        help="Obtained via the Connect flow on this page. Write-only.",
        type="secret", category="YouTube", env_attr="YOUTUBE_REFRESH_TOKEN",
    ),
    # --- SoundCloud ----------------------------------------------------------
    SettingDef(
        key="soundcloud_email",
        label="Account email",
        help="Fallback identity for browser-based auth.",
        type="str", category="SoundCloud", env_attr="SOUNDCLOUD_EMAIL",
    ),
    SettingDef(
        key="soundcloud_client_id",
        label="Client ID",
        help="SoundCloud app client ID. Write-only.",
        type="secret", category="SoundCloud", env_attr="SOUNDCLOUD_CLIENT_ID",
    ),
    SettingDef(
        key="soundcloud_client_secret",
        label="Client secret",
        help="SoundCloud app client secret. Write-only.",
        type="secret", category="SoundCloud", env_attr="SOUNDCLOUD_CLIENT_SECRET",
    ),
    SettingDef(
        key="soundcloud_access_token",
        label="Access token",
        help="OAuth access token — normally set by the Connect flow. Write-only.",
        type="secret", category="SoundCloud", env_attr="SOUNDCLOUD_ACCESS_TOKEN",
    ),
    SettingDef(
        key="soundcloud_refresh_token",
        label="Refresh token",
        help="OAuth refresh token — normally set by the Connect flow. Write-only.",
        type="secret", category="SoundCloud", env_attr="SOUNDCLOUD_REFRESH_TOKEN",
    ),
    SettingDef(
        key="soundcloud_password",
        label="Account password",
        help="Fallback for password-grant/browser auth. Write-only.",
        type="secret", category="SoundCloud", env_attr="SOUNDCLOUD_PASSWORD",
    ),
    # --- Activity ------------------------------------------------------------
    SettingDef(
        key="activity_retention_days",
        label="Retention (days)",
        help="Activity events older than this are pruned nightly.",
        type="int", category="Activity", env_attr="ACTIVITY_RETENTION_DAYS",
        min_value=1, max_value=3650,
    ),
    SettingDef(
        key="activity_max_rows",
        label="Max rows",
        help="Hard cap on activity rows; the oldest beyond this are pruned nightly.",
        type="int", category="Activity", env_attr="ACTIVITY_MAX_ROWS",
        min_value=1_000, max_value=10_000_000,
    ),
    # --- Advanced --------------------------------------------------------------
    SettingDef(
        key="auto_upgrade",
        label="Auto upgrade",
        help="Automatically apply fade-out releases when available.",
        type="bool", category="Advanced", env_attr="AUTO_UPGRADE_ENABLED",
        column="auto_upgrade",
    ),
    SettingDef(
        key="cross_link_push_enabled",
        label="Push cross-links live",
        help="Push reciprocal YouTube/SoundCloud links into the LIVE published "
             "descriptions at the cross_link step (best-effort, never fails a run).",
        type="bool", category="Advanced", env_attr="CROSS_LINK_PUSH_ENABLED",
    ),
    SettingDef(
        key="mixcloud_enabled",
        label="Mixcloud uploads",
        help="Enable the optional Mixcloud publishing target (requires an access "
             "token; steps skip until both are set).",
        type="bool", category="Advanced", env_attr="MIXCLOUD_ENABLED",
    ),
    SettingDef(
        key="min_free_disk_gb",
        label="Min free disk (GB)",
        help="Ingest is refused when free space on the output volume drops below "
             "this. Set via env.",
        type="float", category="Advanced", env_attr="MIN_FREE_DISK_GB", editable=False,
    ),
    SettingDef(
        key="public_url",
        label="Public URL",
        help="External base URL used for OAuth callbacks (Google requires a real "
             "domain). Set via PUBLIC_URL env at deploy.",
        type="str", category="Advanced", env_attr="PUBLIC_URL", editable=False,
    ),
)

SCHEMA_BY_KEY: Dict[str, SettingDef] = {d.key: d for d in SETTINGS_SCHEMA}

# Column-backed keys mirrored into settings_json on write so services that
# only receive the settings_json dict (DescriptionGenerator / ArtGenerator)
# see the DB-configured value without loading the whole row.
MIRRORED_COLUMN_KEYS = frozenset(
    {"llm_provider", "llm_model", "image_gen_provider", "image_gen_model"}
)

# settings_json keys that must never be serialized out of the API.
SECRET_JSON_KEYS = frozenset(
    {d.key for d in SETTINGS_SCHEMA if d.type == "secret"}
    | {"notification_email_smtp_password"}
)


def env_default(d: SettingDef) -> Any:
    """The setting's env-configured default (or static fallback)."""
    if d.env_attr is not None:
        return getattr(env_settings, d.env_attr)
    return d.fallback


def redact_settings_json(sj: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Copy of settings_json with every secret key removed."""
    if sj is None:
        return None
    return {k: v for k, v in sj.items() if k not in SECRET_JSON_KEYS}


# ---------------------------------------------------------------------------
# Validation / coercion
# ---------------------------------------------------------------------------

def validate_value(d: SettingDef, value: Any) -> Any:
    """Validate + coerce ``value`` for setting ``d``. Raises ValueError."""
    if d.type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"'{d.key}' must be a boolean")
        return value

    if d.type == "int":
        if isinstance(value, bool):
            raise ValueError(f"'{d.key}' must be an integer")
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{d.key}' must be an integer")
        if isinstance(value, float) and not float(value).is_integer():
            raise ValueError(f"'{d.key}' must be an integer")
        _check_range(d, coerced)
        return coerced

    if d.type == "float":
        if isinstance(value, bool):
            raise ValueError(f"'{d.key}' must be a number")
        try:
            coerced_f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{d.key}' must be a number")
        _check_range(d, coerced_f)
        return coerced_f

    if d.type == "enum":
        if not isinstance(value, str) or (d.choices and value not in d.choices):
            raise ValueError(
                f"'{d.key}' must be one of: {', '.join(d.choices or ())}"
            )
        return value

    # str | path | secret
    if not isinstance(value, str):
        raise ValueError(f"'{d.key}' must be a string")
    return value


def _check_range(d: SettingDef, value: float) -> None:
    if d.min_value is not None and value < d.min_value:
        raise ValueError(f"'{d.key}' must be >= {d.min_value:g}")
    if d.max_value is not None and value > d.max_value:
        raise ValueError(f"'{d.key}' must be <= {d.max_value:g}")


def _coerce(d: SettingDef, value: Any) -> Any:
    """Best-effort typed coercion for values read back from the DB."""
    try:
        if d.type == "bool":
            return bool(value)
        if d.type == "int":
            return int(value)
        if d.type == "float":
            return float(value)
    except (TypeError, ValueError):
        return env_default(d)
    return value


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_from_snapshot(
    d: SettingDef, sj: Dict[str, Any], columns: Dict[str, Any]
) -> Tuple[Any, str]:
    """Resolve one setting from a row snapshot. Returns (value, source)."""
    if d.key in sj and sj[d.key] is not None:
        return _coerce(d, sj[d.key]), "db"
    if d.column is not None and columns.get(d.column) is not None:
        return _coerce(d, columns[d.column]), "db"
    value = env_default(d)
    source = "env" if (d.env_attr is not None and value not in ("", None)) else "default"
    return value, source


def snapshot_from_row(row: Optional[AppSettings]) -> Dict[str, Any]:
    """Build the {sj, columns} snapshot used by resolution."""
    sj: Dict[str, Any] = {}
    columns: Dict[str, Any] = {}
    if row is not None:
        sj = dict(row.settings_json or {})
        for d in SETTINGS_SCHEMA:
            if d.column is not None:
                columns[d.column] = getattr(row, d.column, None)
    return {"sj": sj, "columns": columns}


def describe(row: Optional[AppSettings]) -> List[Dict[str, Any]]:
    """The introspected settings catalog with resolved values.

    Secrets never include a value — only ``has_value``.
    """
    snap = snapshot_from_row(row)
    out: List[Dict[str, Any]] = []
    for d in SETTINGS_SCHEMA:
        value, source = resolve_from_snapshot(d, snap["sj"], snap["columns"])
        item: Dict[str, Any] = {
            "key": d.key,
            "label": d.label,
            "help": d.help,
            "type": d.type,
            "category": d.category,
            "editable": d.editable,
            "source": source,
            "choices": list(d.choices) if d.choices else None,
            "min": d.min_value,
            "max": d.max_value,
        }
        if d.type == "secret":
            item["value"] = None
            item["default"] = None
            item["has_value"] = bool(value)
        else:
            item["value"] = value
            item["default"] = env_default(d)
            item["has_value"] = value not in ("", None)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Cached service-side access (60s TTL, own session — like notification_service)
# ---------------------------------------------------------------------------

_cache: Optional[Dict[str, Any]] = None
_cached_at: float = 0.0


def invalidate_cache() -> None:
    """Drop the cached snapshot so the next resolve re-reads the DB."""
    global _cache, _cached_at
    _cache = None
    _cached_at = 0.0


async def _get_snapshot(force_refresh: bool = False) -> Dict[str, Any]:
    global _cache, _cached_at
    now = time.monotonic()
    if (
        not force_refresh
        and _cache is not None
        and (now - _cached_at) < CONFIG_CACHE_SECONDS
    ):
        return _cache

    row: Optional[AppSettings] = None
    try:
        async with async_session_factory() as session:
            row = await session.get(AppSettings, 1)
    except Exception:
        logger.exception("Failed to read app config from DB; using env fallback")

    _cache = snapshot_from_row(row)
    _cached_at = now
    return _cache


async def resolve(key: str, force_refresh: bool = False) -> Any:
    """Effective value for ``key``: DB (settings_json/column) else env default."""
    d = SCHEMA_BY_KEY[key]
    snap = await _get_snapshot(force_refresh=force_refresh)
    value, _source = resolve_from_snapshot(d, snap["sj"], snap["columns"])
    return value


async def get_all(force_refresh: bool = False) -> Dict[str, Any]:
    """Effective values for every cataloged setting."""
    snap = await _get_snapshot(force_refresh=force_refresh)
    return {
        d.key: resolve_from_snapshot(d, snap["sj"], snap["columns"])[0]
        for d in SETTINGS_SCHEMA
    }
