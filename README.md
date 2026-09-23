# fade-out

[![Docker](https://img.shields.io/badge/docker-ready-blue?logo=docker)](https://ghcr.io/rancur/fade-out)
[![CI](https://github.com/rancur/fade-out/actions/workflows/ci.yml/badge.svg)](https://github.com/rancur/fade-out/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Automated DJ mix upload pipeline for SoundCloud & YouTube.**

Drop a recorded mix into a watch folder. fade-out analyzes the audio, generates tracklists, creates branded thumbnails and cover art, uploads to SoundCloud and YouTube, verifies each upload, and notifies you on Discord. Zero manual steps after the initial recording.

> [!IMPORTANT]
> **fade-out has no authentication. Do not expose it directly to the internet.**
>
> Anyone who can reach the port can read your configured credentials and publish
> to your accounts. It is built as a single-user tool for a trusted home
> network — run it on your LAN, behind an authenticating reverse proxy, or on a
> private overlay network like Tailscale. See [SECURITY.md](SECURITY.md).

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

## Requirements

- **Docker** and Docker Compose (the recommended path — everything else is baked in), or
- **Python 3.12**, Node 20, and `ffmpeg` for a local install.

> Python 3.13+ is not supported. It removed the stdlib `audioop` module that
> `pydub` — pulled in by `shazamio` — imports at start-up. The Docker image
> pins 3.12 for this reason.

You will also need API credentials for the services you intend to use; see
[Configuration](#configuration).

## Quick Start

### Docker (recommended)

```bash
# Clone the repository
git clone https://github.com/rancur/fade-out.git
cd fade-out

# Configure environment
cp .env.example .env
# Edit .env with your API keys, credentials, and media paths.
# By default fade-out watches ./media/* next to the compose file; point
# WATCH_AUDIO_DIR and friends at your own folders (a NAS share, for example).

# Build and start
docker compose up -d

# Access the dashboard
open http://localhost:8500
```

### NAS / server deployment

```bash
# Interactive setup: creates the media directories and fills in .env
bash scripts/setup.sh

# Or manually:
docker compose up -d --build
```

Set the `WATCH_*` and `OUTPUT_*` variables in `.env` to your shares — for
example `WATCH_AUDIO_DIR=/volume1/music/mixes` on a Synology. The dashboard is
then available at `http://<host-ip>:8500`; keep it on the LAN, or put an
authenticating proxy in front of it.

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
| `GITHUB_TOKEN` | Only for a private repo | Read-only credential for the deployment freshness check (see below) |
| `TZ` | No | IANA timezone (default: `UTC`) |
| `PUBLIC_URL` | For YouTube OAuth | Publicly resolvable URL used for OAuth callbacks |
| `CORS_ALLOW_ORIGINS` | No | Comma-separated browser origins allowed to call the API. Defaults to the Vite dev-server origins; `PUBLIC_URL` is added automatically. **Do not set to `*`** — see [SECURITY.md](SECURITY.md) |
| `DJCTL_WS_URL` | No | Live DJCTL WebSocket feed. Empty (default) disables it |
| `CATALOG_CHANNEL_NAME` | No | Your channel name, stripped when used as a title prefix during back-catalog import |
| `CATALOG_SERIES_NAMES` | No | Comma-separated recurring show names to strip from imported titles |

> [!NOTE]
> `CATALOG_CHANNEL_NAME` and `CATALOG_SERIES_NAMES` default to the original
> author's own Twitch-era show names, so that existing deployments keep working.
> **Set them to your own** (or to empty) — a phrase that never matches simply
> does nothing. The universal markers (`Raid Train`, `Twitch`, `untitled`, bare
> dates) always apply and need no configuration.

### Host paths

`docker-compose.yml` reads these from `.env`, so the compose file works
unmodified on any machine. Each defaults to a folder under `./media/` next to
the compose file — point them at your own shares instead.

| Variable | Mount | Mode |
|---|---|---|
| `WATCH_AUDIO_DIR` | `/watch/audio` | read-write |
| `WATCH_VIDEO_DIR` | `/watch/video` | read-write |
| `WATCH_CUE_DIR` | `/watch/djctl-cue` | read-only |
| `WATCH_SHORTS_DIR` | `/watch/shorts` | read-only |
| `OUTPUT_THUMBNAILS_DIR` | `/output/thumbnails` | read-write |
| `OUTPUT_COVER_ART_DIR` | `/output/cover-art` | read-write |

Audio and video are mounted read-write only so the optional source-file renaming
feature can work; it is off by default. Set them to `:ro` in `docker-compose.yml`
if you never want fade-out to touch your source recordings.

### Deployment freshness

`GET /api/health` and `GET /api/upgrade/status` compare the running build
against the newest GitHub release of `GITHUB_REPO`.

If that repo is **private**, the comparison needs `GITHUB_TOKEN`. GitHub
answers `404` — not `403` — for a private repo the caller cannot read, so
without a credential "no releases published" and "you cannot see this repo"
are literally the same response. The check refuses to guess between them:
it reports `state: error`, `stale: null`, `healthy: false`, and names the
missing credential. It never degrades to "up to date".

Give it the least privilege that works: a **fine-grained** personal access
token scoped to this one repository, with *Repository permissions → Contents:
Read-only*. Release and tag metadata is all this ever reads. A token with write
scopes would hand a monitoring loop the ability to modify the repo it is
watching, and a classic PAT cannot be scoped to a single repository at all.

A credential that authenticates but has no access to the repo is reported as
an error, not as a clean bill of health — the check verifies the repository is
visible before it accepts a 404 as "no releases". Inject the value from your
secret manager at deploy time; never commit it or bake it into the image.

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

### Source File Tagging

Turning on **Write metadata into source files** (Settings → Advanced,
`tag_source_files`, OFF by default) writes library metadata directly into a
completed mix's source FLAC, so the recording is identifiable in Plex or any
local player — not just on SoundCloud/YouTube. It writes:

| Tag | Value |
|---|---|
| `ARTIST` | `Will See` |
| `TITLE` | the mix title |
| `DATE` | the same `YYYY-MM-DD` token used for renaming (see above) |
| `GENRE` | the mix's genres, `; `-separated |
| `ALBUM` | `Will See Mixes` |
| `DESCRIPTION` | the tracklist |
| `URL` | the SoundCloud or YouTube URL |
| cover picture block | the generated cover art |

This is a separate setting from **Rename source files to match titles**
(`rename_source_files`, above) and does not change it — renaming moves/renames
the file, tagging rewrites its metadata, and either can be on, off, or both.

**Why this can't just be a mutagen call in place.** The file watcher dedupes
on `md5(first 10 MB)`, and FLAC metadata blocks live at the very start of the
file, so writing tags changes a file's dedupe hash. If a freshly tagged file
appeared in the watch folder before the watcher knew its new hash, it would be
re-ingested as a new recording and the mix re-uploaded a second time. Tagging
is therefore done out-of-place and promoted in a fixed order:

1. copy the source into a hidden `.fadeout-tagging/` folder inside the watch
   directory (same filesystem, and invisible to the watcher's non-recursive
   directory listing),
2. write the tags and cover art into the copy,
3. re-read the copy to confirm it still parses as valid FLAC, that its audio
   payload (everything past the last metadata block) is at least as large as
   the source's (a truncated write — e.g. the disk filling mid-copy — still
   parses and still reports the original's full duration, since that
   duration comes from a header field a short write leaves untouched; only a
   size check catches it — and it has to be a payload-size check, not a
   total-file-size check, because a healthy re-tag of an already-tagged file
   writes its new metadata into the existing padding block and doesn't grow
   the file at all), and, when a duration is known for the mix, that it
   still matches,
4. register the copy's new hash with the file watcher,
5. only then move it into place with an atomic rename, followed by an fsync
   of the containing directory so a crash right after promotion can't lose
   the rename.

**Registering the hash before promoting the file is the safety property, not
an implementation detail — do not reorder steps 4 and 5.** A tagged file
registered too late is a tagged file the watcher can pick up as new.

These FLACs carry no padding (audio frames measured to begin at byte 86), so
the first tag write on a given file is always a full rewrite; 64 KB of padding
is added on write so later edits can happen in place. The rewrite is skipped —
logged, never fatal to the pipeline run — when free space on the volume is
under 2x the file's size, since the out-of-place copy needs room to exist
alongside the original while it is written.

`.fadeout-tagging/` staging copies from a write that never finished (a killed
process, an OS-level crash between writing the copy and promoting it) are no
longer left for an operator to find by hand: every retag backfill run sweeps
each watch root's `.fadeout-tagging/` directory before it starts, removing
anything older than the sweep's age threshold. See "Orphaned staging
cleanup" below for the guardrails and the default threshold. Tagging
triggered by ordinary pipeline completion (a single mix finishing, not the
backfill) does not run that sweep, so an orphan from a single-mix failure
outside a backfill run still needs manual cleanup.

**Re-tagging skips files that already carry the target tags.** Before
writing anything, `already_tagged()` reads the FLAC's existing Vorbis
comments — a header read only, it never opens the file for writing or
copies it — and compares every target tag's value plus whether embedded
cover art matches what the mix now has. Only when everything already
matches does the mix come back `{"status": "skipped", "reason": "already
tagged"}` without touching the file at all; a retitled mix has a different
`TITLE`, so it is still re-tagged, same as any other real change. A dry run
over an already-tagged library reports `"already tagged -- would be
skipped"` for the same mixes rather than overstating the work a real run
would do. This is what makes the retag backfill (below) cheap to re-run
after an interruption instead of redoing a full multi-gigabyte rewrite of
every file it already touched.

To backfill renames and tags across already-published mixes:

```bash
curl -X POST "$FADEOUT/api/catalog/retag"
```

**Both `tag_source_files` and `rename_source_files` must be turned on** for a
real (`dry_run=false`) backfill run to actually do anything. When a flag is
off, that half comes back as a no-op, but the two report it in different
shapes: the tagger's result carries `"status": "disabled"` directly; the
renamer's result has no top-level `status` key at all (its shape is
`{mix_id, reason, dry_run, enabled, renamed, skipped, errors}`) and instead
carries `"enabled": false` plus `"skipped": [{"reason": "disabled"}]`.
`GET /retag/status`'s `summary` (below) normalizes both into the same
`disabled` bucket so you don't need to know the shape difference yourself —
check it before trusting a run. Since both settings default to OFF, it's
easy to run a green dry-run pre-flight and then have the real run silently
do nothing.

`dry_run` **defaults to true**, so a bare POST is the safe, report-only form —
per mix, it reports whether the source file exists, whether there is
sufficient free space, and whether each of the two settings is actually on,
via an `enabled` field in each half's result, without touching anything. The
two halves differ here too: the tagger's dry run short-circuits with an
explicit "`tag_source_files is off`" reason when its own flag is disabled;
the renamer's dry run does **not** short-circuit — it always reports the
rename plan it would execute (`planned`) regardless of the flag, and relies
on its `enabled` field to tell you separately whether a real run would
actually apply that plan. Pass `dry_run=false` to actually rewrite files, and
poll progress while it runs:

```bash
curl -X POST "$FADEOUT/api/catalog/retag?dry_run=false"
curl "$FADEOUT/api/catalog/retag/status"
```

**The backfill is resumable and genuinely paginated.** `limit` and `offset`
select a deterministically ordered slice of completed mixes (`ORDER BY
created_at, id`, applied identically to the pre-flight `candidates` count
and to the run itself), so `offset=0&limit=20` followed by
`offset=20&limit=20` touches two disjoint batches rather than risking the
same rows twice. After every mix, a resume cursor is written to
`AppSettings.settings_json["retag_progress"]`; passing `resume=true` (which
ignores any `offset` you also pass) continues from that cursor instead of
restarting from the beginning, and with no saved cursor it just starts at
the beginning like a fresh run. Combined with the already-tagged skip
above, an interrupted run can simply be re-issued — as a fresh call
(previously-touched files are skipped cheaply) or with `resume=true` to
pick up exactly where the cursor left off — rather than watched through
hand-sized batches:

```bash
curl -X POST "$FADEOUT/api/catalog/retag?dry_run=false&limit=20"
curl -X POST "$FADEOUT/api/catalog/retag?dry_run=false&resume=true&limit=20"
```

The `candidates` count returned by the POST reflects `limit`/`offset` (or
the resolved resume cursor when `resume=true`) — it's the number of mixes
this call is actually about to touch, not the size of the whole backlog.

**Orphaned staging cleanup.** Before selecting any candidates, every retag
run sweeps each watch root's `.fadeout-tagging/` directory
(`source_tagger.sweep_staging`, `older_than_hours=6.0` by default) and
removes anything older than that threshold; a file younger than the
threshold is left alone because it may belong to a run currently in
flight. The sweep only ever acts on a directory whose basename is exactly
`.fadeout-tagging` and that lives inside an allowed watch root — anything
else is refused outright rather than swept. What it reclaimed is logged as
a `retag_staging_swept` activity event before the run's candidates are
even selected.

**Every backfill run is auditable after the fact**, independent of the
in-memory `GET /retag/status` state a process restart wipes. Durable events
go through the activity log: `retag_staging_swept` before the run starts,
one `retag_run_started` with the run's parameters and candidate count, one
`retag_mix_processed` per mix carrying both the rename and tag outcome
(logged at `error` when either failed, `info` otherwise), and a closing
`retag_run_completed` with the run's summary (logged at `warn` when the
summary contains any failures, `info` otherwise). Activity-log emits are
best-effort and layered on top — a logging failure can never abort or slow
the backfill itself.

`GET /retag/status` includes a `summary` — `{ok, skipped, disabled, failed,
dry_run, unknown}` counts across every rename *and* tag outcome of the run.
Any outcome shape the summary doesn't recognise lands in `unknown` rather
than being silently dropped, so `sum(summary.values())` always equals
`2 * processed` — a run where everything came back `disabled` can't be
mistaken for one that actually did the work; `processed == total` alone
can't tell those apart.

A retag run started while one is already in progress gets `409` rather than
starting a second concurrent pass over the same files.

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

Mixcloud upload, YouTube chapter markers, Shazam/AudD track identification, and
scheduled publishing have all shipped. See [ROADMAP.md](ROADMAP.md) for what is
done and what is next.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md)
for setup, the test commands, and the couple of things worth being careful with.

## Security

fade-out ships no authentication of its own; the deployment model is the
security boundary. Please read [SECURITY.md](SECURITY.md) before exposing it
anywhere, and report vulnerabilities privately rather than in a public issue.

## License

[MIT](LICENSE) -- Copyright 2026 Will Curran
