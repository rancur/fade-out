# Changelog

## v2.2.0 — 2026-07-19

### YouTube Shorts auto-uploader
- New Shorts tab: watches the Vertical Backtrack folder, verifies 9:16 ≤3-min
  clips, Shazams the drop, writes algorithm-optimized titles/descriptions/
  hashtags (hook-first titles, ≤15 hashtags incl. #shorts), and uploads via the
  API — quota-budgeted at 3/day with an oldest-first backlog queue and a
  channel-dedupe scan for clips already uploaded.

### Uniqueness engine
- No repeated creative anywhere: a registry (titles / thumbnail hooks / scene
  descriptors) with similarity guards backs every generator. Thumbnail hook
  text is now drafted per mix (never a shared genre label like "ALL VIBES"),
  scenes get deterministic per-mix variation, and titles are enforced unique
  across the pipeline, catalog improve, and apply.

### Settings overhaul
- Schema-driven Settings page: 34 settings across Paths/Pipeline/AI/YouTube/
  SoundCloud/Activity/Advanced editable in-app (DB-over-env with cached
  resolve), secrets strictly write-only, effective watch/output paths shown
  with deploy hints. Fixed a legacy endpoint that leaked stored secrets.

## v2.1.0 — 2026-07-19

The discoverability release.

### Branded thumbnail design system
- Will-approved template baked into the app: warm Sonoran-desert palette,
  hand-illustrated psychedelic focal subject with the Will See eye motif,
  Press Start 2P pixel-font hook text (≤3 words), accent bar, WILL SEE tag.
- 12 genre motifs (DnB cyber coyote, dubstep sandstone totem, house
  sunflower-eye, trance planet-eye oasis, EDM ember phoenix, trap, deep house,
  tech house, techno, garage, organic, open format) — every mix gets unique
  on-brand art; pixel font ships in the Docker image.
- `POST /api/catalog/regen-thumbnails` regenerates catalog art at scale.

### Cross-platform playlist grouping
- Series- and genre-aware buckets (Will See Wednesdays, Second Saturdays,
  DnB & Jungle, Dubstep & Bass, House, Trance, Techno, EDM & Big Room,
  Garage & Breaks, Melodic & Progressive, Open Format), fuzzy-matched against
  existing YouTube playlists before creating new "Will See | …" ones;
  SoundCloud playlist create/append support.
- `POST /api/catalog/organize-playlists`, quota-budgeted, idempotent,
  per-bucket error isolation, title-keyword genre fallback.

### Tracklist backfill
- `POST /api/catalog/backfill-tracklists`: matches local audio to imported
  back-catalog mixes (filename date/title/duration), runs full CUE+Shazam
  analysis, and queues platform description updates with real tracklists.

### Metadata quality
- Title diversity: anti-repetition prompt (used-title context, banned
  overused words, structure variation) + similarity guard with retry.
- Series-aware refresh keeps series identity in titles while adding
  discoverability hooks; per-platform search-tag drafting (YT ≤20, SC ≤30).
- Sorting on catalog + mixes lists (newest first by default).

### Fixes
- YouTube snippet updates batched per video (stale-snippet revert race).
- Malformed YouTube playlist ids skipped instead of aborting the run;
  SoundCloud playlist bodies sent as documented JSON (form-encoded retry).

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
