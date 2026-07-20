"""Application settings endpoints.

Two generations of API live here:

* ``GET /api/settings`` + ``PUT /api/settings`` — the original column-based
  singleton (kept for compatibility). ``settings_json`` is redacted on the way
  out (secrets are never serialized) and secret keys already stored are
  preserved when a client round-trips the redacted dict back.
* ``GET /api/settings/schema`` + ``PUT /api/settings/values`` — the
  introspected catalog (see ``services/app_config.py``): every setting with
  label/help/type/category, DB-over-env resolution, per-type validation, and
  write-only secrets (only ``has_value`` is ever returned).
"""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AppSettings
from app.services import app_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

BACKUP_DIR = Path("data/backups")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


class AppSettingsOut(BaseModel):
    id: int = 1
    image_gen_provider: Optional[str] = "fal"
    image_gen_model: Optional[str] = "fal-ai/flux-pro/v1.1"
    llm_provider: Optional[str] = "openai"
    llm_model: Optional[str] = "gpt-4o"
    premiere_mode: Optional[str] = "scheduled"
    premiere_hour_utc: Optional[int] = 0
    premiere_day: Optional[str] = "friday"
    draft_mode: Optional[bool] = True
    auto_upgrade: Optional[bool] = True
    settings_json: Optional[dict] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AppSettingsUpdate(BaseModel):
    image_gen_provider: Optional[str] = None
    image_gen_model: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    premiere_mode: Optional[str] = None
    premiere_hour_utc: Optional[int] = None
    premiere_day: Optional[str] = None
    draft_mode: Optional[bool] = None
    auto_upgrade: Optional[bool] = None
    settings_json: Optional[dict] = None


class SettingOut(BaseModel):
    key: str
    label: str
    help: str
    type: str  # str | int | float | bool | enum | path | secret
    category: str
    value: Any = None  # always None for secrets
    has_value: bool = False
    default: Any = None  # always None for secrets
    editable: bool = True
    source: str = "default"  # db | env | default
    choices: Optional[List[str]] = None
    min: Optional[float] = None
    max: Optional[float] = None


class SettingsSchemaResponse(BaseModel):
    categories: List[str]
    settings: List[SettingOut]


class SettingsValuesUpdate(BaseModel):
    """Partial update: only the keys present are written.

    ``null`` clears the DB override (the setting falls back to its env
    default). Secrets are write-only — send a new value to overwrite, ``null``
    to clear; they are never echoed back.
    """

    values: Dict[str, Any]


class BackupInfo(BaseModel):
    backup_id: str
    created_at: str
    size_bytes: int


class BackupListResponse(BaseModel):
    backups: List[BackupInfo]


async def _get_or_create_settings(db: AsyncSession) -> AppSettings:
    """Get or create the singleton settings row."""
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    if not row:
        row = AppSettings(id=1)
        db.add(row)
        await db.flush()
    return row


def _legacy_out(row: AppSettings) -> AppSettingsOut:
    """Serialize the singleton row with secrets stripped from settings_json."""
    out = AppSettingsOut.model_validate(row)
    out.settings_json = app_config.redact_settings_json(row.settings_json)
    return out


@router.get("", response_model=AppSettingsOut)
async def get_settings(db: AsyncSession = Depends(get_db)):
    """Get all application settings (settings_json secrets are redacted)."""
    row = await _get_or_create_settings(db)
    return _legacy_out(row)


@router.put("", response_model=AppSettingsOut)
async def update_settings(body: AppSettingsUpdate, db: AsyncSession = Depends(get_db)):
    """Update application settings (legacy column-based API).

    Secret keys already stored in ``settings_json`` survive a client
    round-tripping the redacted GET payload back: any secret key absent from
    the provided dict keeps its stored value.
    """
    row = await _get_or_create_settings(db)
    update_data = body.model_dump(exclude_unset=True)

    if "settings_json" in update_data:
        provided = update_data.pop("settings_json") or {}
        stored = dict(row.settings_json or {})
        for key in app_config.SECRET_JSON_KEYS:
            if key not in provided and key in stored:
                provided[key] = stored[key]
        row.settings_json = provided

    for key, value in update_data.items():
        setattr(row, key, value)

    # Keep the settings_json mirror of column-backed keys in sync so services
    # reading only settings_json (generators) see column edits from this API.
    sj = dict(row.settings_json or {})
    mirrored = False
    for key in app_config.MIRRORED_COLUMN_KEYS:
        d = app_config.SCHEMA_BY_KEY[key]
        col_val = getattr(row, d.column, None)
        if col_val is not None and sj.get(key) != col_val:
            sj[key] = col_val
            mirrored = True
    if mirrored:
        row.settings_json = sj

    await db.flush()
    await db.refresh(row)
    app_config.invalidate_cache()
    return _legacy_out(row)


# ---------------------------------------------------------------------------
# Introspected settings catalog (schema + values)
# ---------------------------------------------------------------------------

@router.get("/schema", response_model=SettingsSchemaResponse)
async def get_settings_schema(db: AsyncSession = Depends(get_db)):
    """Full settings catalog with resolved values (secrets masked)."""
    row = await _get_or_create_settings(db)
    return SettingsSchemaResponse(
        categories=list(app_config.CATEGORIES),
        settings=[SettingOut(**item) for item in app_config.describe(row)],
    )


@router.put("/values", response_model=SettingsSchemaResponse)
async def update_settings_values(
    body: SettingsValuesUpdate, db: AsyncSession = Depends(get_db)
):
    """Partial settings update with per-type validation.

    * unknown keys and non-editable settings are rejected,
    * secrets are write-only (stored, never echoed),
    * ``null`` clears the DB override so the env default applies again,
    * the app-config cache is invalidated so changes apply immediately.
    """
    if not body.values:
        raise HTTPException(status_code=422, detail="No values provided")

    row = await _get_or_create_settings(db)
    sj = dict(row.settings_json or {})

    for key, value in body.values.items():
        d = app_config.SCHEMA_BY_KEY.get(key)
        if d is None:
            raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")
        if not d.editable:
            raise HTTPException(
                status_code=400,
                detail=f"'{key}' is not editable at runtime (configured at deploy)",
            )

        clearing = value is None or (d.type == "secret" and value == "")
        if clearing:
            sj.pop(key, None)
            if d.column is not None:
                setattr(row, d.column, app_config.env_default(d))
            continue

        try:
            validated = app_config.validate_value(d, value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        if d.column is not None:
            setattr(row, d.column, validated)
            if key in app_config.MIRRORED_COLUMN_KEYS:
                sj[key] = validated
        else:
            sj[key] = validated

    row.settings_json = sj
    await db.flush()
    await db.commit()
    await db.refresh(row)

    app_config.invalidate_cache()

    return SettingsSchemaResponse(
        categories=list(app_config.CATEGORIES),
        settings=[SettingOut(**item) for item in app_config.describe(row)],
    )


@router.post("/backup", response_model=BackupInfo)
async def trigger_backup(db: AsyncSession = Depends(get_db)):
    """Trigger a manual settings backup."""
    row = await _get_or_create_settings(db)

    backup_data = {
        "image_gen_provider": row.image_gen_provider,
        "image_gen_model": row.image_gen_model,
        "llm_provider": row.llm_provider,
        "llm_model": row.llm_model,
        "premiere_mode": row.premiere_mode,
        "premiere_hour_utc": row.premiere_hour_utc,
        "premiere_day": row.premiere_day,
        "draft_mode": row.draft_mode,
        "auto_upgrade": row.auto_upgrade,
        "settings_json": row.settings_json,
    }

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_id = f"settings_{timestamp}"
    backup_path = BACKUP_DIR / f"{backup_id}.json"
    backup_path.write_text(json.dumps(backup_data, indent=2, default=str))

    return BackupInfo(
        backup_id=backup_id,
        created_at=timestamp,
        size_bytes=backup_path.stat().st_size,
    )


@router.get("/backups", response_model=BackupListResponse)
async def list_backups():
    """List all available settings backups."""
    backups = []
    for f in sorted(BACKUP_DIR.glob("settings_*.json"), reverse=True):
        ts_part = f.stem.replace("settings_", "")
        backups.append(
            BackupInfo(
                backup_id=f.stem,
                created_at=ts_part,
                size_bytes=f.stat().st_size,
            )
        )
    return BackupListResponse(backups=backups)


@router.post("/restore/{backup_id}", response_model=AppSettingsOut)
async def restore_backup(backup_id: str, db: AsyncSession = Depends(get_db)):
    """Restore settings from a backup."""
    backup_path = BACKUP_DIR / f"{backup_id}.json"
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail=f"Backup '{backup_id}' not found")

    backup_data = json.loads(backup_path.read_text())
    row = await _get_or_create_settings(db)

    for key, value in backup_data.items():
        if hasattr(row, key):
            setattr(row, key, value)

    await db.flush()
    app_config.invalidate_cache()
    return _legacy_out(row)
