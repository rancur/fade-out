# fade-out

[![Docker](https://img.shields.io/badge/docker-ready-blue?logo=docker)](https://ghcr.io/rancur/fade-out)
[![CI](https://github.com/rancur/fade-out/actions/workflows/build.yml/badge.svg)](https://github.com/rancur/fade-out/actions/workflows/build.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Automated DJ mix upload pipeline for SoundCloud & YouTube.**

Drop a recorded mix into a watch folder. fade-out analyzes the audio, generates tracklists, creates branded thumbnails and cover art, uploads to SoundCloud and YouTube, verifies each upload, and notifies you on Discord. Zero manual steps after the initial recording.

---

## Features

| Feature | Description |
|---|---|
| **File Watcher** | Monitors NAS directories for new audio recordings, video captures, and cue sheets |
| **Audio Analysis** | Extracts duration, BPM, key, waveform data via ffmpeg |
| **Tracklist Extraction** | Parses cue sheets or uses AI to identify tracks from mix recordings |
| **Title & Description Generation** | AI-powered metadata: mix titles, descriptions, tags, and timestamps |
| **Thumbnail Generation** | Branded YouTube thumbnails with AI-generated backgrounds via fal.ai |
| **Cover Art Generation** | SoundCloud cover art with consistent DJ branding |
| **SoundCloud Upload** | Automated upload with metadata, cover art, and tracklist via browser automation |
| **YouTube Upload** | Automated upload via YouTube Data API with thumbnails and descriptions |
| **Upload Verification** | Post-upload checks to confirm successful publishing on both platforms |
| **Discord Notifications** | Webhook notifications on upload success/failure with direct links |
| **Web Dashboard** | React-based UI for monitoring pipeline status, managing mixes, and brand settings |
| **Brand Customization** | Configurable colors, fonts, logos, and templates for generated assets |

## Architecture

```
                           fade-out pipeline
  ┌──────────────────────────────────────────────────────────────────┐
  │                                                                  │
  │  /watch/audio ──┐                                               │
  │  /watch/video ──┼──▶ File Watcher ──▶ Analyzer ──┐             │
  │  /watch/djctl ──┘         │              │        │             │
  │                           │         BPM, key,     │             │
  │                      new file       duration   tracklist        │
  │                      detected        extracted  parsed          │
  │                                                   │             │
  │                         ┌─────────────────────────┘             │
  │                         ▼                                       │
  │                    Generator                                    │
  │               ┌──────┬──────┐                                   │
  │               │      │      │                                   │
  │            title  thumbnail cover                               │
  │            desc   (fal.ai)  art                                 │
  │               │      │      │                                   │
  │               └──────┴──────┘                                   │
  │                      │                                          │
  │                      ▼                                          │
  │                   Uploader                                      │
  │              ┌───────┴────────┐                                 │
  │              │                │                                 │
  │         SoundCloud       YouTube                                │
  │         (Playwright)     (Data API)                             │
  │              │                │                                 │
  │              └───────┬────────┘                                 │
  │                      ▼                                          │
  │                   Verifier ──▶ Discord Notification             │
  │                                                                 │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │  Web Dashboard (React + FastAPI)  :8000                 │   │
  │  │  Pipeline status │ Mix manager │ Brand settings         │   │
  │  └─────────────────────────────────────────────────────────┘   │
  └──────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Docker (recommended)

```bash
# Clone the repository
git clone https://github.com/rancur/fade-out.git
cd fade-out

# Configure environment
cp .env.example .env
# Edit .env with your API keys and credentials

# Build and start
docker compose up -d

# Access the dashboard
open http://localhost:8500
```

### NAS Deployment

```bash
# Run the setup script (creates directories, configures .env)
bash scripts/setup.sh

# Or manually:
docker compose up -d --build
```

The web dashboard will be available at `http://<nas-ip>:8500`.

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI API key for tracklist analysis and metadata generation |
| `FAL_API_KEY` | Yes | fal.ai API key for AI thumbnail/cover art generation |
| `SOUNDCLOUD_EMAIL` | Yes | SoundCloud account email |
| `SOUNDCLOUD_PASSWORD` | Yes | SoundCloud account password |
| `YOUTUBE_CLIENT_ID` | Yes | YouTube Data API OAuth2 client ID |
| `YOUTUBE_CLIENT_SECRET` | Yes | YouTube Data API OAuth2 client secret |
| `YOUTUBE_REFRESH_TOKEN` | Yes | YouTube Data API OAuth2 refresh token |
| `NOTIFICATION_DISCORD_WEBHOOK_URL` | No | Discord webhook for upload notifications |
| `TZ` | No | Timezone (default: `America/Phoenix`) |

### Watch Directories

| Path | Purpose |
|---|---|
| `/watch/audio` | Recorded DJ mix audio files (WAV, FLAC, MP3) |
| `/watch/video` | Twitch/OBS video recordings for YouTube upload |
| `/watch/djctl-cue` | Cue sheets exported from DJ software (via djctl) |

### Output Directories

| Path | Purpose |
|---|---|
| `/output/thumbnails` | Generated YouTube thumbnails |
| `/output/cover-art` | Generated SoundCloud cover art |

## Web Dashboard

The web UI provides:

- **Pipeline Monitor** -- Real-time status of each mix through the pipeline stages
- **Mix Manager** -- Browse, edit metadata, retry failed uploads
- **Brand Settings** -- Customize colors, fonts, logo, and template layouts
- **Upload History** -- Links to published mixes on SoundCloud and YouTube

<!-- Screenshots: TODO -->

## Brand Customization

Customize generated thumbnails and cover art through the web dashboard or by editing brand settings in the database:

- **Primary/secondary colors** -- Used in text overlays and borders
- **Font family** -- Applied to mix titles and tracklist text
- **Logo** -- Overlaid on thumbnails and cover art
- **Template layout** -- Choose from preset arrangements or create custom layouts
- **AI prompt modifiers** -- Steer the fal.ai background generation style

## API Reference

The backend exposes a REST API at `/api`:

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | GET | Health check |
| `/api/mixes` | GET | List all mixes |
| `/api/mixes/{id}` | GET | Get mix details |
| `/api/mixes/{id}` | PATCH | Update mix metadata |
| `/api/mixes/{id}/retry` | POST | Retry failed pipeline stages |
| `/api/mixes/{id}/upload` | POST | Manually trigger upload |
| `/api/pipeline/status` | GET | Current pipeline status |
| `/api/brand` | GET | Get brand settings |
| `/api/brand` | PUT | Update brand settings |
| `/api/settings` | GET | Get application settings |
| `/api/settings` | PUT | Update application settings |

## Development

### Local Setup

```bash
# Backend
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

### Docker Development

```bash
# Start with live reload (mounts source code)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up

# Backend at :8500, Frontend dev server at :5173
```

### Linting

```bash
# Python
ruff check backend/
ruff format backend/

# TypeScript
cd frontend && npm run typecheck
```

## Roadmap

- [ ] Mixcloud upload support
- [ ] Automatic chapter markers from tracklist timestamps
- [ ] Shazam-based track identification fallback
- [ ] Scheduled uploads (release queue with configurable timing)
- [ ] Analytics dashboard (play counts, listener stats across platforms)
- [ ] Multi-brand support (different visual presets per genre/series)
- [ ] RSS podcast feed generation
- [ ] Waveform video generation for YouTube (audio-reactive visuals)

## License

[MIT](LICENSE) -- Copyright 2026 Will Curran
