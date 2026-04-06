# CLAUDE.md — fade-out Project Instructions

## Project Overview
fade-out is an automated DJ mix upload pipeline for SoundCloud and YouTube. It watches NAS directories for new audio recordings, analyzes them, generates tracklists/thumbnails/cover art, uploads to platforms, and notifies via Discord. Built with Python (FastAPI) backend and React (Vite) frontend, deployed via Docker.

## Repository Structure
- `backend/` — Python FastAPI application (`backend/app/main.py` is the entrypoint)
- `frontend/` — React + TypeScript frontend (Vite, built to `frontend/dist/`)
- `scripts/` — Shell scripts for setup and backup
- `docker-compose.yml` — Production compose (maps NAS volumes, port 8500:8000)
- `docker-compose.dev.yml` — Development compose
- `Dockerfile` — Multi-stage build (Node frontend build + Python runtime with ffmpeg/Chromium)

## Running the App
```bash
# Production
docker compose up -d

# Development
docker compose -f docker-compose.dev.yml up

# Backend only (local)
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Testing
No test suite exists yet. When adding tests:
- Use `pytest` for backend tests
- Place tests in `backend/tests/` or `tests/`
- Run with: `python3 -m pytest tests/ -v`

## Coding Standards
- **Python**: Follow PEP 8. Use type hints. FastAPI dependency injection patterns.
- **TypeScript/React**: Functional components, TypeScript strict mode.
- **Commits**: Use conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`).
- **Secrets**: NEVER hardcode secrets. All credentials come from environment variables (see `docker-compose.yml` for the full list). The `.env` file is gitignored.
- **Configuration**: Use `pydantic-settings` for backend config (`backend/app/config.py`).

## Key Dependencies
- **Backend**: FastAPI, SQLAlchemy, Alembic, Playwright (browser automation for SoundCloud uploads), librosa, shazamio, OpenAI API, Google API client
- **Frontend**: React, Vite, TypeScript
- **System**: ffmpeg, Chromium (both installed in Docker image)

## Docker Setup
The Dockerfile is a two-stage build:
1. **Stage 1**: Node 20 Alpine — builds the Vite frontend
2. **Stage 2**: Python 3.12 slim — installs ffmpeg, Chromium, Python deps, copies frontend dist

Health check: `GET /api/health` on port 8000.

## Environment Variables
All secrets are passed via environment variables (never committed):
- `OPENAI_API_KEY` — AI-powered metadata generation
- `FAL_API_KEY` — Image generation for thumbnails
- `SOUNDCLOUD_*` — SoundCloud OAuth credentials
- `YOUTUBE_*` — YouTube Data API credentials
- `NOTIFICATION_DISCORD_WEBHOOK_URL` — Discord notifications
- `PUBLIC_URL` — Public-facing URL for the dashboard

## Review Standards
When reviewing PRs:
- Verify no secrets or credentials are exposed
- Check that new env vars are documented
- Ensure Docker build is not broken by changes
- Validate that API endpoints have proper error handling
- Confirm frontend changes build cleanly with `npm run build`
