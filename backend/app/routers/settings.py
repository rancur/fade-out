"""Application settings endpoints."""

import json
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


@router.get("", response_model=AppSettingsOut)
async def get_settings(db: AsyncSession = Depends(get_db)):
    """Get all application settings."""
    row = await _get_or_create_settings(db)
    return AppSettingsOut.model_validate(row)


@router.put("", response_model=AppSettingsOut)
async def update_settings(body: AppSettingsUpdate, db: AsyncSession = Depends(get_db)):
    """Update application settings."""
    row = await _get_or_create_settings(db)
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return AppSettingsOut.model_validate(row)


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
    return AppSettingsOut.model_validate(row)
