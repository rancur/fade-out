"""Fade-Out — DJ mix upload automation API."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers import ai_usage, brand, mixes, notifications, pipeline, settings as settings_router, upgrade

logger = logging.getLogger("fadeout")

FRONTEND_DIR = Path("/app/frontend/dist")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle handler."""
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database ready.")
    # Future: start file watcher and scheduler here
    logger.info("Fade-Out is running.")
    yield
    logger.info("Fade-Out shutting down.")


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
app.include_router(mixes.router)
app.include_router(pipeline.router)
app.include_router(settings_router.router)
app.include_router(brand.router)
app.include_router(ai_usage.router)
app.include_router(notifications.router)
app.include_router(upgrade.router)


# --- Health Check ---
@app.get("/api/health")
async def health_check():
    """Return service health status."""
    return {"status": "ok", "version": "0.1.0"}


# --- Static Files & SPA Fallback ---
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
