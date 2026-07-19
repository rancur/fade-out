"""Fade-Out — DJ mix upload automation API."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.logging_config import configure_logging
from app.routers import (
    activity,
    ai_usage,
    auth,
    brand,
    mixes,
    notifications,
    pipeline,
    settings as settings_router,
    system,
    upgrade,
    ws,
)
from app.services import activity_log
from app.services.file_watcher import FileWatcherService
from app.services.handlers import register_all_handlers
from app.services.ingest import IngestCoordinator
from app.services.notification_service import get_notification_service
from app.services.pipeline import PipelineOrchestrator, sweep_interrupted_at_boot

logger = logging.getLogger("fadeout")

FRONTEND_DIR = Path("/app/frontend/dist")

# Global orchestrator instance -- importable by routers
orchestrator = PipelineOrchestrator()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle handler."""
    configure_logging(settings.LOG_LEVEL, settings.LOG_JSON)
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database ready.")

    # Startup sweep: anything still marked "running" was cut off by the last
    # shutdown (no orchestrator task can exist at boot). Steps become
    # "interrupted", their mixes "failed" with a retryable error.
    await sweep_interrupted_at_boot()

    # Register all pipeline step handlers
    register_all_handlers(orchestrator)
    logger.info(
        "Pipeline orchestrator ready (%d handlers registered).",
        len(orchestrator._handlers),
    )

    # Stream live pipeline events to any connected WebSocket clients.
    orchestrator.on_event(ws.manager.broadcast_event)

    # Fan out every activity-log entry to connected WebSocket clients too, so
    # the UI sidebar updates live in addition to its polling fallback.
    def _broadcast_activity(payload: dict) -> Any:
        return ws.manager.broadcast({"event": "activity", "data": payload})

    activity_log.add_listener(_broadcast_activity)

    # Notifications: the singleton NotificationService reads its channel
    # config from AppSettings.settings_json (env as first-boot fallback) and
    # listens to orchestrator events (pipeline_started, step_completed,
    # upload_complete, error, draft_ready), honoring the per-event toggles and
    # min-level saved via /api/notifications/settings.
    notifier = get_notification_service()
    await notifier.start()
    orchestrator.on_event(notifier.handle_orchestrator_event)

    # Nightly activity-log retention: prune rows older than
    # ACTIVITY_RETENTION_DAYS or beyond ACTIVITY_MAX_ROWS.
    async def _activity_retention_loop() -> None:
        while True:
            try:
                await activity_log.prune()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Activity retention prune failed")
            await asyncio.sleep(24 * 3600)

    retention_task = asyncio.create_task(_activity_retention_loop())

    # Start the file watcher so dropped audio/video files auto-ingest into the
    # pipeline. Without this the watcher service is never instantiated and
    # nothing is ever picked up from the watch folders.
    coordinator = IngestCoordinator(orchestrator)
    coordinator.start()  # background pairing / expiry sweeper for out-of-order drops
    file_watcher = FileWatcherService(
        on_audio_file=coordinator.ingest_audio,
        on_video_file=coordinator.ingest_video,
    )
    await file_watcher.start()
    logger.info(
        "File watcher started (audio=%s video=%s).",
        settings.WATCH_AUDIO_PATH,
        settings.WATCH_VIDEO_PATH,
    )
    await activity_log.info("service_started", "fade-out started; watching for drops.")

    logger.info("Fade-Out is running.")
    yield
    logger.info("Fade-Out shutting down.")
    retention_task.cancel()
    try:
        await retention_task
    except asyncio.CancelledError:
        pass
    await file_watcher.stop()
    await coordinator.stop()
    await notifier.stop()


app = FastAPI(
    title="Fade-Out",
    description="DJ mix upload automation tool",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow all for local use
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Routers ---
app.include_router(auth.router)
app.include_router(mixes.router)
app.include_router(pipeline.router)
app.include_router(activity.router)
app.include_router(system.router)
app.include_router(settings_router.router)
app.include_router(brand.router)
app.include_router(ai_usage.router)
app.include_router(notifications.router)
app.include_router(upgrade.router)
app.include_router(ws.router)


# --- Health Check ---
@app.get("/api/health")
async def health_check():
    """Return service health status."""
    return {"status": "ok", "version": "0.1.0"}


# --- Static Files & SPA Fallback ---

# Serve cover art and thumbnails from the output directories
COVER_ART_DIR = Path(settings.OUTPUT_COVER_ART_PATH)
THUMBNAILS_DIR = Path(settings.OUTPUT_THUMBNAILS_PATH)

if COVER_ART_DIR.exists():
    app.mount("/output/cover-art", StaticFiles(directory=COVER_ART_DIR), name="cover-art")
if THUMBNAILS_DIR.exists():
    app.mount("/output/thumbnails", StaticFiles(directory=THUMBNAILS_DIR), name="thumbnails")

if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")


@app.get("/{full_path:path}")
async def spa_fallback(request: Request, full_path: str):
    """Serve the React SPA for any non-API route."""
    # Don't intercept API routes
    if full_path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    # Try to serve the exact static file first
    static_file = FRONTEND_DIR / full_path
    if static_file.is_file():
        return FileResponse(static_file)

    # Fall back to index.html for SPA routing
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(index)

    return JSONResponse(
        status_code=200,
        content={"message": "Fade-Out API is running. Frontend not built yet."},
    )
