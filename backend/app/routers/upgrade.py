"""Upgrade management endpoints: version check, apply, backup."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.upgrade_service import UpgradeService, _get_current_version, BACKUP_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upgrade", tags=["upgrade"])

# Module-level state for last check result
_last_check: Optional[dict] = None
_last_checked_at: Optional[str] = None


# --- Schemas ---

class UpgradeStatusResponse(BaseModel):
    current_version: str
    latest_version: Optional[str] = None
    update_available: bool = False
    last_checked: Optional[str] = None


class UpgradeCheckResponse(BaseModel):
    current_version: str
    latest_version: Optional[str] = None
    update_available: bool = False
    release_name: Optional[str] = None
    release_notes: Optional[str] = None
    html_url: Optional[str] = None
    checked_at: str


class UpgradeApplyResponse(BaseModel):
    success: bool
    message: str
    from_version: str
    to_version: Optional[str] = None


class BackupItem(BaseModel):
    filename: str
    path: str
    size_bytes: int
    created_at: str


class BackupListResponse(BaseModel):
    backups: List[BackupItem]
    total: int


class BackupCreateResponse(BaseModel):
    filename: str
    path: str
    size_bytes: int
    created_at: str


# --- Endpoints ---

@router.get("/status", response_model=UpgradeStatusResponse)
async def upgrade_status():
    """Return current version info and whether an update is available."""
    global _last_check, _last_checked_at

    current = _get_current_version()
    latest = _last_check.get("latest_version") if _last_check else None
    update_available = _last_check is not None and _last_check.get("latest_version") is not None

    return UpgradeStatusResponse(
        current_version=current,
        latest_version=latest,
        update_available=update_available,
        last_checked=_last_checked_at,
    )


@router.post("/check", response_model=UpgradeCheckResponse)
async def check_for_updates():
    """Trigger an update check against GitHub releases."""
    global _last_check, _last_checked_at

    service = UpgradeService()
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        update_info = await service.check_for_update()
    except Exception as exc:
        logger.exception("Update check failed")
        raise HTTPException(status_code=502, detail=f"Failed to check for updates: {exc}")

    _last_checked_at = checked_at

    if update_info:
        _last_check = update_info
        return UpgradeCheckResponse(
            current_version=update_info["current_version"],
            latest_version=update_info["latest_version"],
            update_available=True,
            release_name=update_info.get("release_name"),
            release_notes=update_info.get("release_notes"),
            html_url=update_info.get("html_url"),
            checked_at=checked_at,
        )

    _last_check = None
    return UpgradeCheckResponse(
        current_version=_get_current_version(),
        latest_version=None,
        update_available=False,
        checked_at=checked_at,
    )


@router.post("/apply", response_model=UpgradeApplyResponse)
async def apply_upgrade():
    """Apply a pending update (backup first, then upgrade)."""
    global _last_check

    if not _last_check or not _last_check.get("latest_version"):
        raise HTTPException(
            status_code=400,
            detail="No pending update. Run POST /api/upgrade/check first.",
        )

    target_version = _last_check["latest_version"]
    current_version = _get_current_version()
    service = UpgradeService()

    # Backup before upgrading
    try:
        backup_path = await service.backup_settings()
        logger.info("Pre-upgrade backup saved to %s", backup_path)
    except Exception as exc:
        logger.exception("Pre-upgrade backup failed")
        raise HTTPException(status_code=500, detail=f"Backup failed, aborting upgrade: {exc}")

    # Perform upgrade
    try:
        success = await service.check_and_upgrade()
    except Exception as exc:
        logger.exception("Upgrade failed")
        raise HTTPException(status_code=500, detail=f"Upgrade failed: {exc}")

    if success:
        _last_check = None
        return UpgradeApplyResponse(
            success=True,
            message=f"Upgraded from {current_version} to {target_version}. Restart may be required.",
            from_version=current_version,
            to_version=target_version,
        )

    return UpgradeApplyResponse(
        success=False,
        message="Upgrade process failed. Check logs for details. Backup was created before attempt.",
        from_version=current_version,
        to_version=target_version,
    )


@router.get("/backups", response_model=BackupListResponse)
async def list_backups():
    """List available backup files."""
    service = UpgradeService()
    backups_raw = service.list_backups()

    backups = [
        BackupItem(
            filename=b["filename"],
            path=b["path"],
            size_bytes=b["size_bytes"],
            created_at=b["created_at"],
        )
        for b in backups_raw
    ]

    return BackupListResponse(backups=backups, total=len(backups))


@router.post("/backup", response_model=BackupCreateResponse)
async def trigger_backup():
    """Trigger a manual full backup (mixes, settings, brand)."""
    service = UpgradeService()

    try:
        backup_path = await service.backup_settings()
    except Exception as exc:
        logger.exception("Manual backup failed")
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}")

    stat = os.stat(backup_path)
    filename = os.path.basename(backup_path)

    return BackupCreateResponse(
        filename=filename,
        path=backup_path,
        size_bytes=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )
