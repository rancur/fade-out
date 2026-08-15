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
from app.services import app_config
from app.services import ingest as ingest_module
from app.services.platform_health import get_platform_health
from app.services.stuck_mix_watchdog import get_stuck_mix_watchdog
from app.services.upgrade_service import deployment_status
from app.version import __version__, build_info

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

    # Credential + deployment truth, same source as /api/health.
    platforms = get_platform_health().snapshot()
    deployment = deployment_status.as_dict()
    stuck = get_stuck_mix_watchdog().last_result

    platforms_ok = all(p["healthy"] for p in platforms.values())
    ok = db_ok and disk_ok and platforms_ok and deployment["healthy"]

    return {
        "status": "ok" if ok else "degraded",
        "version": __version__,
        "build": build_info(),
        "platforms": platforms,
        "deployment": deployment,
        "unpublished_mixes": {
            "state": stuck.get("state", "unknown"),
            "count": stuck.get("stuck_count"),
            "checked_at": stuck.get("checked_at"),
        },
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
            "draft_mode": bool(await app_config.resolve("draft_mode")),
            "premiere_mode": await app_config.resolve("premiere_mode"),
        },
    }


@router.get("/unpublished")
async def unpublished_mixes(refresh: bool = False) -> Dict[str, Any]:
    """Mixes ingested but not published inside their window.

    ``refresh=true`` runs the sweep now instead of returning the watchdog's
    last result. The result always carries its own ``state`` — a check that
    has never run reports ``unknown``, never an empty "all clear".
    """
    watchdog = get_stuck_mix_watchdog()
    if refresh:
        return await watchdog.check(alert=False)
    return watchdog.last_result
