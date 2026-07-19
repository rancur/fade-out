"""System health + readiness endpoint for the record -> drop -> automate flow.

Surfaces the operational signals that matter for an unattended pipeline: is the
file watcher alive, how many drops are waiting for a sibling, is there enough
free disk to ingest a multi-GB set, is the DB reachable, and are the publish
safety flags (draft mode / unlisted premieres) still in force.
"""

import logging
import os
import shutil
from typing import Any, Dict

from fastapi import APIRouter
from sqlalchemy import text

from app.config import settings
from app.database import async_session_factory
from app.services import ingest as ingest_module

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


def _disk_free_gb(path: str) -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(path if os.path.isdir(path) else "/")
        return {
            "path": path,
            "free_gb": round(usage.free / (1024**3), 2),
            "total_gb": round(usage.total / (1024**3), 2),
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {"path": path, "error": str(exc)}


@router.get("/health")
async def system_health() -> Dict[str, Any]:
    """Rich health/readiness snapshot (DB, watcher, pending pairs, disk, safety)."""
    # DB check
    db_ok = True
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - defensive
        db_ok = False
        logger.warning("health: DB check failed: %s", exc)

    coordinator = ingest_module.get_active_coordinator()
    pending = coordinator.pending_snapshot() if coordinator else []

    output_disk = _disk_free_gb(settings.OUTPUT_COVER_ART_PATH)
    free_gb = output_disk.get("free_gb")
    disk_ok = free_gb is None or free_gb >= settings.MIN_FREE_DISK_GB

    ok = db_ok and disk_ok

    return {
        "status": "ok" if ok else "degraded",
        "version": "0.1.0",
        "db_ok": db_ok,
        "watcher_running": coordinator is not None,
        "pending_pairs": pending,
        "pending_count": len(pending),
        "disk": {
            "output": output_disk,
            "min_free_gb": settings.MIN_FREE_DISK_GB,
            "ok": disk_ok,
        },
        "safety": {
            "draft_mode": settings.DRAFT_MODE,
            "premiere_mode": settings.PREMIERE_MODE,
        },
    }
