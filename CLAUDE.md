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
The project has a substantial test suite. Keep it green and add to it.

```bash
# Backend — 993 tests, ~45s. Requires Python 3.12 (see below) and ffmpeg.
cd backend && python -m pytest -q

# Frontend — 30 tests, plus typecheck and build
cd frontend && npm test && npx tsc --noEmit && npm run build
```

- Backend tests live in `backend/tests/`, config in `backend/pytest.ini`
  (`asyncio_mode = auto`, so async tests need no decorator).
- Frontend tests live beside their components in `__tests__/` directories.
- Tests must never make live calls to SoundCloud, YouTube, or Mixcloud — mock
  the client, as the existing tests do. Uploads hit real accounts.
- All of the above runs in CI (`.github/workflows/ci.yml`).

## Python Version
Use **Python 3.12** — the version the Dockerfile ships. Python 3.13+ removed the
stdlib `audioop` module that `pydub` (via `shazamio`) imports at start-up, so
the backend does not run there.

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
All secrets are passed via environment variables (never committed). `.env.example` is the complete, documented list; the highlights:
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
