# fade-out v2 — Design Spec

Date: 2026-07-18 · Author: Barry (Claude Code) with Will Curran · Status: approved via goal directive

## Goal

Ship fade-out v2: a single major release that consolidates the three open PRs, makes the
pipeline fully observable (activity page, verbose events, live per-step progress), fixes the
pipeline tab's accuracy, makes notifications configurable and real, and adds cross-platform
mix management with a back-catalog import of Will's entire YouTube + SoundCloud history.

## Decisions (from Will)

- **One v2 mega-branch**: `v2` off `main`; merge PR #13 → #14 → #15 into it, build features on
  top, merge to main once at the end. Tag `v2.0.0`, rebuild the NAS container from git.
- **Review queue**: no published mix is modified until its proposal is approved in the UI.
  Unique titles are preserved as "keepers"; generic ones (raid trains) get AI-drafted upgrades.
- **Notifications**: user-configurable channels — Discord webhook and/or SMTP email — editable
  by any user in the UI, stored in the DB. Will's SMTP seeded from his SEER container settings;
  Discord webhook added by him later.

## 1. Consolidation (stage 0)

Merge order: `feat/activity-log-and-robustness` (#13), `feat/fade-out-metadata-thumbnail`
(#14), `fix/cue-guards-shazam-sc-refresh` (#15). Known collision hotspots: `handlers.py`
(all three), `audio_analyzer.py` (#14/#15 both rework it), `file_watcher.py` (#13/#14),
`config.py` (#13/#15). Resolution principle: #15's logic is authoritative for CUE/SC/analyzer
dedup (it is what runs verified in production); #14's genre classifier and thumbnail logic is
authoritative for art; #13's activity/ingest is authoritative for ingest/watcher. Full backend
test suite (215+) green at the end of stage 0.

## 2. Activity page + verbose logging

- `/activity` becomes a routed page in the sidebar nav; the right-rail `ActivityPanel` becomes
  a compact Dashboard widget ("recent activity", links to /activity).
- Page: filters (level, event type, mix, platform, date range), text search, cursor-paginated
  infinite scroll, expandable `context` JSON per row, per-mix filter deep-link
  (`/activity?mix_id=…`) used by the pipeline tab.
- Backend: `GET /api/activity` gains `q` (search), `platform`, `since`/`until`, cursor
  pagination. New emissions so the feed alone explains a run: CUE match/reject + session
  selection reasons, SC token refresh/persist, transcode start/done (with sizes), upload
  progress milestones (25/50/75%), platform description pushes, notification deliveries,
  watcher pairing decisions. Retention: nightly prune > 90 days / > 50k rows (config).

## 3. Live step progress

- Model: `PipelineStep.progress` (int 0–100, nullable) + `progress_detail` (str, nullable).
  Migration 0004.
- Emission: orchestrator gains `report_progress(mix_id, step, pct, detail)` throttled to ≥1s
  intervals; handlers pass a callback into uploaders/analyzer/transcoder:
  - SoundCloud: counting-file wrapper (bytes sent / total).
  - YouTube: existing resumable-chunk percent (currently a discarded log line).
  - Transcode: ffmpeg `-progress pipe:1` parsing (out_time vs duration).
  - Analyze: segment counter (n / total segments).
- Transport: existing `/api/ws/status` broadcasts `step_progress` events; DB row updated at
  most every 5s (WS is the live path, DB is the recovery path).
- Frontend: `useWebSocket` client (auto-reconnect, exponential backoff, poll fallback via
  react-query refetch), progress bars in PipelineProgress nodes + mix list rows + Dashboard.

## 4. Pipeline tab rehaul

- Fix accuracy: steps deduped per run (latest attempt wins; run separator for prior attempts),
  orphaned `running` steps marked `interrupted` by a startup sweep, unconfigured platform steps
  (e.g. Mixcloud) hidden with a "not configured" note instead of rendering as pending forever.
- Per-step: duration, attempts with per-attempt errors (expandable full traceback), output
  summary rendered from `output_json` (tracks found, URLs produced, artwork paths), live
  progress bar (stage 3), actions: retry step, reread tracklist (analyze/desc steps), link to
  step-filtered activity.

## 5. Notifications

- Single config source: `AppSettings.settings_json` keys (`notification_discord_webhook_url`,
  `notification_email_*`, `notification_events`, `notification_min_level`). Env vars remain as
  first-boot seed only. NotificationService reads DB config at send time (cached 60s).
- Wire-up: `main.py` startup registers the service as an orchestrator listener and `.start()`s
  the queue worker. Event → type mapping already 1:1.
- UI `/notifications`: two tabs — **Settings** (channel cards: Discord webhook URL w/ test
  button; SMTP host/port/user/password/from/to w/ test button; per-event-type toggles;
  min-severity) and **History** (existing list, now actually populated, filterable, each row
  linking to its activity event).
- Seed Will's SMTP from the SEER container's settings on the NAS at deploy time.

## 6. Mix management + back-catalog import

**Model.** `Mix` gains: `source` (`pipeline` | `imported`), `youtube_video_id`,
`soundcloud_track_id` (backfilled for existing rows from URLs), `title_locked` (bool —
keeper titles). New table `MixProposal`: id, mix_id, platform (`youtube` | `soundcloud` |
`both`), field (`title` | `description` | `thumbnail` | `playlist` | `tags`), current_value,
proposed_value (JSON), status (`draft` | `approved` | `rejected` | `applying` | `applied` |
`failed`), created_by (`ai` | `user`), error, timestamps. Migration 0005.

**Import.** `POST /api/catalog/sync` (background job + progress via WS):
1. Fetch all YouTube uploads (channel uploads playlist → playlistItems, then videos.list
   batches of 50: snippet, contentDetails.duration, status) and all SoundCloud tracks
   (`GET /me/tracks`, paginated).
2. Match into unified mixes: (a) URL cross-links in descriptions (fade-out publishes them),
   (b) existing Mix rows by URL, (c) title similarity (normalized Levenshtein ≥ 0.8) +
   duration within ±90s, (d) upload date within 14 days as tiebreak, (e) LLM judge for the
   remaining ambiguous set (batched, cheap model). Unmatched platform items become
   single-platform mixes.
3. Idempotent: re-sync updates platform metadata, never duplicates (keyed on platform ids).
- Import review UI: matched pairs / YT-only / SC-only / uncertain (with confirm, merge,
  split actions).

**Editor.** Mix editor panel (within MixDetail, also reachable from the list): title,
description, tags/genre, thumbnail/artwork (upload file or AI-regenerate via existing
ArtGenerator), YouTube playlist membership (list playlists, add/remove). Saving creates
user-authored proposals; **Apply** pushes approved proposals per platform:
YT `videos.update` / `thumbnails.set` / `playlistItems.*`; SC `PUT /tracks/:id`
(title, description, artwork via `track[artwork_data]`, tags). Applied state + history kept
on the proposal rows.

**AI improve.** `POST /api/catalog/improve` (scoped: selected mixes or all-generic):
classifier marks each mix title `keeper` (unique/creative — locked, never proposed) vs
`generic` (raid-train patterns, date-only, "DJ set" boilerplate); for generics, drafts
click-optimized titles + full descriptions (retaining/back-filling tracklists where known)
as `draft` proposals. Review queue UI: diff view (current vs proposed per field), approve /
edit-then-approve / reject, bulk approve. Quota-aware apply worker: YT budget tracked
(update=50 units, thumbnail=50, playlistItem=50, 10k/day default), queue pauses at budget
and resumes next day; SC has no meaningful quota.

## 7. Release + hardening

- Version 2.0.0: `frontend/package.json`, FastAPI app version + `/health`, Layout badge
  (read from build-time env, not hardcoded), `CHANGELOG.md`, git tag `v2.0.0`, GitHub release.
- Hardening riders: startup interrupted-step sweep (stage 4), activity retention (stage 2),
  orphaned `running` pipeline recovery, mixcloud steps hidden when unconfigured.
- Deploy: rebuild NAS image from `v2`, migrate DB (0004/0005), verify health + a re-run of
  the live verification script, then merge PR to main, close #13/#14/#15 as superseded.

## Non-goals

- Mixcloud back-catalog import (Mixcloud uploader stays as-is, hidden when unconfigured).
- Frontend e2e test suite (backend suite + targeted component tests only).
- Multi-user auth (fade-out remains single-tenant behind Tailscale).

## Testing

Backend: full suite green after every stage; new tests for progress emission, notification
config/dispatch, catalog matcher (fixture-driven pairs incl. ambiguous cases), proposal
lifecycle, quota budgeting. Frontend: component tests for ActivityPage filters, NotificationSettings
forms, ReviewQueue diff/approve; typecheck + build as CI gate.
