# Changelog

## v2.0.0 — 2026-07-19

The observability + mix-management release. Consolidates PRs #13, #14, #15 and adds
five major feature sets on top.

### Mix management & back-catalog import
- **Catalog sync** (`POST /api/catalog/sync`): imports every YouTube upload and
  SoundCloud track, matching them into unified mixes via description cross-links,
  title similarity + duration, and an LLM judge for ambiguous pairs. Idempotent.
- **Mix editor**: per-platform title / description / tags editing for any mix —
  including imported back-catalog — with per-platform Apply (YouTube `videos.update`,
  SoundCloud `PUT /tracks/:id`). Unique titles can be locked ("keepers").
- **AI improve** (`POST /api/catalog/improve`): classifies titles as keeper vs generic
  (raid-train boilerplate etc.) and drafts click-optimized titles + descriptions.
- **Review queue**: nothing touches a published mix until approved — side-by-side
  diffs, edit-then-approve, bulk approve, applied/failed history.
- YouTube API quota budgeting: apply worker tracks daily units and pauses at the
  configured budget (`YOUTUBE_DAILY_QUOTA_BUDGET`, default 8000), resuming next day.

### Live progress & pipeline accuracy
- Per-step progress (percent + human detail: bytes uploaded, transcode %, tracks
  identified n/total) streamed over the existing WebSocket and persisted to
  `pipeline_steps.progress`.
- Pipeline step rows are upserted per (mix, step): no more duplicate phantom
  "pending" rows in the pipeline tab.
- Boot sweep marks steps orphaned by a restart as `interrupted` (retryable).
- Pipeline tab rehaul: attempts, durations, expandable errors, step output
  summaries, inline retry / re-read-tracklist actions, live updates.

### Activity
- Activity is a first-class page (`/activity`) with level/platform/event/mix/date
  filters, search, cursor-paginated infinite scroll, and live prepend.
- Far more verbose event stream: CUE match/reject reasoning, token refreshes,
  transcode lifecycle, upload milestones, platform description pushes,
  notification deliveries.
- Nightly retention pruning (`ACTIVITY_RETENTION_DAYS`, `ACTIVITY_MAX_ROWS`).

### Notifications (now real)
- NotificationService is wired to pipeline events at startup (it previously never
  fired) and reads its config from the database — editable in the UI by any user —
  with env vars as first-boot seed only.
- `/notifications` page: Discord webhook + SMTP email channel setup with per-channel
  test sends, per-event toggles, minimum severity, and a delivery history that
  matches the activity feed.

### Reliability (from the July 18 production incident)
- SoundCloud OAuth: rotated refresh tokens are persisted (fixes `invalid_grant`
  → Playwright-fallback failures).
- SoundCloud uploads > 450 MB are transcoded to 320 kbps MP3 (the API returns an
  empty 413 for large bodies; the documented 4 GB limit is web-uploader-only).
- CUE sheets: exact filename-date matching, multi-session parsing, and
  duration-fit validation — a copied master can no longer inherit an unrelated
  recent tracklist.
- Shazam fallback keeps both tracks of a transition and allows A→B→A returns.
- SQLite: WAL + busy timeout; retry-step endpoint no longer self-deadlocks and
  runs steps in the background.
- Cross-links are pushed to both platforms; the SoundCloud description always
  contains the tracklist; re-read preserves published titles.

### Upgrade notes
- Migrations `0004` (step progress + step-row dedupe) and `0005` (catalog columns
  + `mix_proposals`) run on startup.
- New settings keys live in `AppSettings.settings_json` (`notification_*`,
  `catalog_*`); env equivalents remain as seeds.
- ffmpeg is required for oversized SoundCloud uploads (already in the Docker image).

## v1.0.0 — 2026-06-02

Initial release: watch-folder ingest, audio analysis (Shazam + DJCTL CUE),
AI descriptions/art, SoundCloud + YouTube upload pipeline, draft mode, brand
settings, AI usage tracking.
