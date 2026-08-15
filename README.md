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
| `AUDD_API_TOKEN` | No | [AudD](https://dashboard.audd.io) fingerprint token. When set, used as a fallback for mix segments the Shazam identifier misses (better recall on layered/underground DJ audio). |
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

### Source File Renaming

fade-out invents a title for every mix, but the recordings on disk keep whatever
OBS or your DJ software called them. Turning on **Rename source files to match
titles** (Settings → Advanced) closes that gap: when a run finishes — and
whenever a new title is applied from the catalog — the source audio and video
are renamed in place and the stored paths follow.

```
before  /watch/audio/Twitch DJs Vol 4 (2026-07-15).flac
        /watch/video/will-see-live-2026-07-15.mkv
after   /watch/audio/2026-07-15 Neon Drift - House Mix.flac
        /watch/video/2026-07-15 Neon Drift - House Mix.mkv
```

It takes **two keys to fire**, and either one missing is a safe no-op:

1. the setting is on, and
2. `/watch/audio` and `/watch/video` are mounted `:rw` in `docker-compose.yml`.

With a read-only mount every rename is skipped and logged to the Activity page,
and nothing else about the pipeline changes.

The date prefix comes from the **original** filename, so audio/video pairing and
catalog matching keep working; both files get the same token, which means the
pair ends up with identical stems. Characters that SMB and Windows cannot store
are replaced (`|` and `/` become `-`, `:` becomes ` -`).

Safety rails worth knowing about:

- Only files inside `/watch/audio` and `/watch/video` are ever touched.
  `/watch/shorts`, `/watch/djctl-cue`, and anything in
  `CATALOG_EXTRA_AUDIO_PATHS` are excluded.
- An existing file is **never** overwritten — an occupied name gets a ` (2)`
  suffix, and after `(9)` the rename is refused.
- Symlinks are skipped, and a rename can never fail a pipeline run.
- On a Synology, the share's ACLs and the container's UID govern whether the
  write actually succeeds; a permission failure shows up as a warning in the
  Activity page.

To preview or trigger a rename for one mix — including for back-catalog mixes
that predate the feature:

```bash
curl -X POST "$FADEOUT/api/catalog/mixes/<mix_id>/rename-source?dry_run=true"
```

`dry_run=true` reports the plan without touching anything, and works even while
the setting is off.

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
| `/api/health` | GET | Functional health — 503 when a credential is dead or the build is stale |
| `/api/health/live` | GET | Process liveness only (what the container healthcheck uses) |
| `/api/mixes` | GET | List all mixes |
| `/api/mixes/{id}` | GET | Get mix details |
| `/api/mixes/{id}` | PATCH | Update mix metadata |
| `/api/mixes/{id}/retry` | POST | Retry failed pipeline stages |
| `/api/mixes/{id}/retry-platform/{platform}` | POST | Re-run ONE platform leg (soundcloud/youtube/mixcloud) |
| `/api/mixes/{id}/publish-state` | GET | Per-platform publish state for a mix |
| `/api/mixes/{id}/upload` | POST | Manually trigger upload |
| `/api/system/unpublished` | GET | Mixes ingested but not published inside their window |
| `/api/pipeline/status` | GET | Current pipeline status |
| `/api/brand` | GET | Get brand settings |
| `/api/brand` | PUT | Update brand settings |
| `/api/settings` | GET | Get application settings |
| `/api/settings` | PUT | Update application settings |

### Health, and what "healthy" means here

`/api/health` asserts real function, not process liveness. It exercises the
same auth path an upload uses for every configured platform and returns **503**
when a credential is dead, unknown, or its last probe went stale — a publish
pipeline that cannot authenticate is not healthy, whatever the process is
doing. It also reports the running build against the latest release, so a
container that never got redeployed is visible rather than silently old.

Container orchestration should watch `/api/health/live` instead: the
Dockerfile healthcheck does, so a dead OAuth grant pages a human rather than
cycling a process that is working fine.

Build identity comes from `app/version.py` plus `BUILD_COMMIT` / `BUILD_TIME`,
baked in at image build time:

```bash
BUILD_COMMIT=$(git rev-parse --short HEAD) \
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
docker compose build && docker compose up -d
curl -s localhost:8500/api/health | jq .build   # must match what you built
```

### When one platform fails

Publish targets are independent legs. If SoundCloud's grant is dead, YouTube
is still attempted; the mix ends up `partial` with `pipeline_error` naming
which targets are live, and the failed leg's remaining steps are recorded
`blocked` rather than left at `pending`. Fix the credential and re-run just
that leg:

```bash
curl -X POST localhost:8500/api/mixes/<id>/retry-platform/soundcloud
```

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
