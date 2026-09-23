# Changelog

## v2.6.1 — 2026-09-23

### Retag backfill: safe to run unattended

v2.6.0 made the retag backfill resumable, paginated and auditable, but a
whole-branch review found it could still be **silently incomplete** when
run unattended — never unsafe to the files themselves, but a run that
looks successful while having done little or nothing is worse for an
operation nobody is watching. This release closes those gaps.

- The resume cursor now only advances when **both** legs of a mix reached a
  terminal outcome — the rename as well as the tag. A rename that failed on
  `permission_denied`, a read-only mount or a cross-device link previously
  advanced the cursor and was never retried, which is precisely the class of
  transient, fixable condition resume exists for.
- **`disabled` is no longer treated as terminal success on a real run.** It
  used to be, on the reasoning that a switched-off setting means "nothing to
  do" — true at the time, false the moment the setting is switched on. A real
  run with `tag_source_files` off would advance the cursor across the whole
  backlog and record it as a real run, so enabling the flag and resuming
  afterwards tagged nothing and reported success. Dry runs are unaffected.
- `retag_run_started` now records `resume_cursor_ignored`, so the durable log
  alone shows when a cursor was refused for belonging to the opposite mode —
  previously that appeared only in the HTTP response, which is gone by the
  time anyone reads the log.
- `tag_sources_for_mix` now marks its already-tagged outcome with a structural
  `"terminal": True` at the one site that knows the answer, replacing a match
  on the human-readable reason string. The string match remains as a fallback
  for older builds.

- **A dry run's cursor can no longer poison a real run.** The persisted
  resume cursor records the `dry_run` it was written under; `resume=true`
  against a cursor from the other mode is now treated as no cursor at all
  (starts from the beginning) instead of being resolved into an offset.
  Previously, the README's own documented flow — a bare (dry-run) POST over
  the whole backlog, then a real run — left the real run resuming past a
  cursor the dry run wrote, so it touched nothing and reported
  `{"started": true}` with no file ever tagged. The POST response now
  includes `resume_cursor_ignored` so this is visible when it happens.
- **The resume cursor now only advances past terminal-success outcomes.**
  A failed tag write, or a skip for a condition that can resolve itself
  (missing source, outside the allowed roots, insufficient free space), no
  longer advances the cursor — the mix is retried on the next
  `resume=true` instead of being skipped forever. The run still moves on to
  the next mix immediately either way; not advancing the cursor never
  stalls it.
- **The durable audit trail now carries `reason`, not just `status`.**
  `retag_mix_processed` events include both the rename and tag reason
  alongside their status, and `retag_run_completed` is now logged at `warn`
  when any tag skip's reason was not "already tagged" (previously only
  failures triggered `warn`) — this is what actually distinguishes "every
  file was already tagged" from "every file was skipped for want of disk",
  which otherwise both show up identically as `{skipped: N}`. A run that
  crashes outright now emits a `retag_run_failed` event before the
  exception propagates, so a crashed run is distinguishable in the durable
  log from one still in progress.
- **The orphan-reclaiming sweep can no longer delete an in-flight staging
  copy.** `write_tagged_copy` stages via `shutil.copy2`, whose `copystat`
  carries the SOURCE file's mtime onto the staged copy — for these
  months-old archival recordings, a freshly staged copy reported an age of
  thousands of hours immediately, so a 6-hour sweeper could delete a copy
  while it was still being written. `write_tagged_copy` now stamps the
  staged copy with the current time right after copying, and
  `sweep_staging` now ages off the more recent of `st_mtime`/`st_ctime`
  (ctime cannot be backdated by `copystat`) rather than `st_mtime` alone —
  belt and braces on a delete primitive.
- **A bare, report-only POST can no longer perform real deletions.** The
  staging sweep's own `dry_run` was being dropped — passed positionally
  where the parameter went unused — so it always deleted for real
  regardless of what the retag call's `dry_run` was. It is now plumbed
  through properly, and the would-remove set is included in the
  `retag_staging_swept` activity event under a dry run just as it would be
  under a real one.
- **The already-tagged skip now compares cover art by content, not just
  presence.** A mix whose artwork was regenerated at the same
  `cover_art_path` — same file, different bytes — previously kept its stale
  embedded art forever, since "some picture is embedded" and "a cover art
  file exists" both stayed true. `already_tagged()` now compares the
  embedded picture's size and a SHA-256 digest against the file on disk.
- **The orphan sweep now visits nested source directories, not just flat
  watch roots.** A mix stored in a subdirectory of a watch root (e.g.
  `<watch_audio>/2023/foo.flac`) stages its own `.fadeout-tagging/` copy
  alongside itself, not at the watch root — which a sweep of only the flat
  roots never visited. The sweep's targets are now the union of the flat
  watch-root directories and `dirname(<candidate's source>)/.fadeout-tagging/`
  for every mix the run actually selected as a candidate.
- `GET /retag/status` no longer reports the previous run's numbers while a
  new run's staging sweep is still in progress — the `running` flag is now
  set before the sweep starts, not after.
- Corrected documentation: the bare-POST "without touching anything" claim
  now genuinely holds for the staging sweep too (previously only for the
  per-mix dry-run report), the "never removes a file younger than the
  threshold" claim now genuinely holds under the copy2-mtime scenario
  above, and the cover-art "matches" claim now means content, not presence.

## v2.6.0 — 2026-09-23

### Retag backfill: idempotent, resumable, auditable

v2.5.0 shipped the retag backfill over roughly 80 irreplaceable multi-
gigabyte FLAC recordings (~498 GB) with known gaps: a re-run redid the full
rewrite even on files already tagged, `limit` capped a single call but did
not paginate (no `ORDER BY`, no offset, so a second capped call could
re-select the same rows), nothing durable survived a process restart, and
orphaned staging copies from an interrupted run were never reclaimed. This
release closes all four.

- **Re-tagging skips files that already carry the target tags.**
  `source_tagger.already_tagged()` reads a FLAC's existing Vorbis comments
  (a header read only — never a write, never a copy) and compares every
  target tag value plus embedded cover art presence; only when everything
  matches does the mix come back `{"status": "skipped", "reason": "already
  tagged"}` untouched. A retitled mix has a different `TITLE`, so it is
  still re-tagged. Dry runs report `"already tagged -- would be skipped"`
  for the same mixes instead of overstating the work.
- **`POST /api/catalog/retag` gained `offset` and `resume`.** Candidate
  selection is now ordered deterministically (`Mix.created_at`, `Mix.id`),
  identically in the pre-flight `candidates` count and in the run itself, so
  `limit`+`offset` select genuinely disjoint batches. After every mix, a
  cursor is persisted to `AppSettings.settings_json["retag_progress"]`;
  `resume=true` continues from that cursor (ignoring any `offset` passed
  alongside it) instead of restarting, and falls back to the beginning when
  no cursor is saved.
- **Durable per-mix outcomes in the activity log.** Each run emits one
  `retag_run_started` (parameters + candidate count), one
  `retag_mix_processed` per mix (both the rename and tag outcome, at
  `error` level when either failed), and a closing `retag_run_completed`
  with the summary, logged at `warn` when the summary contains any
  failures. All emits are best-effort and can never abort a run.
- **Orphaned staging copies are reclaimed automatically.** Every backfill
  run now opens by sweeping each watch root's `.fadeout-tagging/` directory
  (`source_tagger.sweep_staging`, `older_than_hours=6.0` by default) and
  reports the result as a `retag_staging_swept` activity event. The sweep
  refuses to touch anything whose basename isn't `.fadeout-tagging` or that
  falls outside the allowed watch roots, and never removes a file younger
  than the age threshold, since it may belong to a run still in flight.
- Documentation updated to match: the README no longer tells operators to
  run the backfill "under supervision" in hand-sized batches — that
  instruction existed only because of the four gaps above.

## v2.5.0 — 2026-09-22

### Source file tagging
- New opt-in **Write metadata into source files** setting (`tag_source_files`,
  Settings → Advanced, OFF by default): when a run completes, the source FLAC
  gets `ARTIST` ("Will See"), `TITLE`, `DATE`, `GENRE`, `ALBUM` ("Will See
  Mixes"), `DESCRIPTION` (the tracklist), `URL` (SoundCloud or YouTube), and
  the generated cover art embedded as a picture block — so the recording is
  identifiable in Plex or any local player. `rename_source_files` is
  unchanged and stays a separate setting; renaming was already shipped
  before this work.
- The file watcher dedupes on `md5(first 10 MB)`, and FLAC metadata blocks
  live at the start of the file, so tagging changes a file's dedupe hash. The
  write is therefore done out-of-place: the file is copied into a hidden
  `.fadeout-tagging/` staging directory inside the watch folder, tagged
  there, verified, and only THEN is its new hash registered with the file
  watcher — before the tagged copy is fsynced and moved into place with an
  atomic rename. Registering before promoting is the safety property: the
  file is never visible to the watcher while its hash is unknown, so a
  tagged file can never be re-ingested and re-uploaded. Do not reorder this.
- Verification compares the AUDIO PAYLOAD size, not the total file size and
  not the reported duration. A FLAC's duration comes from its `STREAMINFO`
  header, which a copy carries over verbatim — a file with half its audio
  deleted still parses cleanly and still reports the full original duration,
  so a parse-and-duration check would promote a truncated write over an
  irreplaceable master. The staged copy's payload must be at least as large
  as the source's. Total file size does not work either: re-tagging a file
  that already has padding writes into that padding and leaves the size
  byte-identical, which a naive size check would flag as corruption on a
  perfectly healthy file.
- The staged file is fsynced before the rename and its directory fsynced
  after, so a power loss cannot leave the directory entry pointing at
  unwritten blocks with the original already gone.
- These FLACs carry no padding (audio frames measured to begin at byte 86),
  so the first tag write on a file is always a full rewrite; 64 KB of
  padding is added so later edits happen in place.
- Refuses to rewrite a file when free space on its volume is under 2x the
  file's size, rather than risking a truncated multi-gigabyte recording.
- Tagging can never fail a pipeline run — every failure returns a status
  rather than raising, matching `source_renamer`'s contract.

### Retag backfill
- `POST /api/catalog/retag` backfills renames and tags across already-
  published (completed) mixes, running rename before tag for each one, same
  as the pipeline does at completion. `dry_run` defaults to **true** — a bare
  POST is the safe, report-only form; pass `dry_run=false` to actually
  rewrite files. The dry run reports, per mix, whether the source file exists
  and whether there is sufficient free space, so it is a genuine pre-flight
  check rather than a guess.
- `GET /api/catalog/retag/status` reports live progress (`running`,
  `processed`, `total`, `results`). A second run started while one is
  already in progress gets **409** instead of starting a concurrent pass over
  the same files.

## v2.4.1 — 2026-08-22

### An interrupted mix is resumed, not quietly buried

On 2026-08-21 the container was OOM-killed at 4.19 GB while analyzing a long
set. Two failures compounded: the memory ceiling was too low for the work, and
the boot sweep then marked the in-flight mix **failed** and never touched it
again. The set simply never published, and nobody found out for days.

- **`mem_limit` raised 4g → 12g.** Analysis of a multi-hour set is the memory
  high-water mark and 4g did not cover it. An OOM kill mid-pipeline is data
  loss, not a slowdown, so the ceiling now carries real headroom.
- **The boot sweep no longer fails an interrupted mix.** A restart says
  nothing about the mix, only about the process. Mixes cut off mid-flight are
  parked in `interrupted` (already a retryable, UI-visible status) instead of
  `failed`.
- **`resume_interrupted_at_boot` re-drives them.** Shortly after startup, every
  interrupted mix has its interrupted/failed/blocked steps reset and its
  pipeline restarted — completed steps are not redone. Gated by the new
  **Resume interrupted mixes** setting (Advanced, on by default).
- **Bounded, so a poisonous mix cannot loop.** Each automatic resume is counted
  in `metadata_json.interrupt_resumes`; after 3 the mix is left `failed` with
  an explicit reason and an error-level activity entry. Manual retries are not
  spent against the bound.
- Interrupted mixes stay counted in the dashboard's failed bucket
  (`/api/pipeline/status` and the WS snapshot) rather than vanishing from every
  bucket while they wait, and the stuck-mix watchdog still reports them.

### Prepared for public release

fade-out is now a public repository. Nothing about the pipeline changed; this
is packaging, hardening, and honesty about what the project is.

**Security**

- **`claude.yml` is gated on `author_association`.** On a public repo the old
  trigger let any stranger start a Claude run — with write access to the
  repository and the owner's OAuth token — by opening an issue or commenting
  "@claude". Only the owner, org members, and invited collaborators can now.
- **CORS is an explicit allowlist instead of `"*"`.** The API has no
  authentication, so a credentialed wildcard let any website the operator
  visited read their stored credential metadata and drive their pipeline.
  Configurable via `CORS_ALLOW_ORIGINS`; `PUBLIC_URL` is folded in
  automatically, and setting `"*"` now disables credentialed CORS.
- **Removed the unused `python-jose` dependency**, the only source of the sole
  known Python vulnerability (`ecdsa`, PYSEC-2026-1325, no fix available).
  `pip-audit` is now clean.
- Merged the outstanding dependency bumps for `undici`, `postcss`,
  `react-router`, and `react-router-dom`. `npm audit` is now clean.
- The roadmap workflow passes its input through the environment rather than
  interpolating it into the prompt.
- Added `SECURITY.md`, including the deployment guidance this project has
  always needed: **fade-out has no authentication and must not be exposed
  directly to the internet.**

**Fixed**

- **`scripts/backup.sh` never backed up the database.** It looked for
  `data/fade-out.db`; the application writes `fadeout.db`. The database was
  silently skipped and the script still printed "Backup complete!" over an
  archive that could not restore. It now uses the correct name and fails loudly
  when the database is missing.
- The YouTube OAuth code-exchange error path parsed Google's
  `error_description` and then logged the raw response body instead, so the
  useful reason never reached the log.
- The DJCTL WebSocket listener no longer starts a reconnect loop when no URL is
  configured. `DJCTL_WS_URL` now defaults to empty (opt-in) rather than to a
  hardcoded LAN address.

**Configuration**

- Host paths are read from `.env` (`WATCH_AUDIO_DIR`, `WATCH_VIDEO_DIR`,
  `WATCH_CUE_DIR`, `WATCH_SHORTS_DIR`, `OUTPUT_THUMBNAILS_DIR`,
  `OUTPUT_COVER_ART_DIR`), so `docker-compose.yml` works unmodified anywhere.
  They default to `./media/*` beside the compose file.
- Back-catalog title cleanup is configurable via `CATALOG_CHANNEL_NAME` and
  `CATALOG_SERIES_NAMES` instead of hardcoding one channel's retired show
  names. Defaults preserve existing behaviour; series matching now also treats
  singular/plural and written/numeric ordinals as equivalent.
- `TZ` defaults to `UTC`, and the SoundCloud automation browser follows it
  rather than a fixed timezone.
- `AUDD_API_TOKEN`, `PUBLIC_URL`, and `CORS_ALLOW_ORIGINS` are documented in
  `.env.example`, which had drifted from the README.

**Project**

- **Added CI** (`.github/workflows/ci.yml`): backend tests, lint, frontend
  typecheck/test/build, a Docker build, and a dependency audit. The README's CI
  badge pointed at a `build.yml` that never existed.
- Added `CONTRIBUTING.md` and a `ruff.toml` with a deliberately conservative
  rule set; fixed the 39 findings it reported.
- `CLAUDE.md` claimed no test suite existed. There are now 1015 backend tests
  and 30 frontend tests, and the documented commands are the real ones.
- `ROADMAP.md` listed a dozen already-shipped features as pending work — which
  the autonomous roadmap driver would have re-implemented on top of working
  code. Rewritten against what is actually in the tree.
- Documented that **Python 3.12 is required**: 3.13 removed the stdlib
  `audioop` module that `pydub` (via `shazamio`) imports at start-up.
- Removed `IMPROVEMENT_PLAN.md` and `docs/superpowers/`, internal planning
  documents that referenced personal infrastructure.
- Scrubbed personal email addresses, NAS paths, and a LAN IP from the tree.

### The deployment freshness check can actually reach a private repo
- `GITHUB_TOKEN` is now **passed through to the container** in
  `docker-compose.yml` / `docker-compose.dev.yml` and documented in
  `.env.example`. The setting has existed since v2.4.0, but nothing ever handed
  it to the running process, so on a private repo the check had no way to
  answer anything but "cannot determine".
- An **authenticated 404 is no longer treated as "no releases published"**.
  GitHub answers 404 — not 403 — for a private repo the caller cannot read, so
  "this repo has no releases" and "this credential cannot see this repo" arrive
  as the same response. The first reading marked the deployment healthy on the
  strength of a token that was doing nothing. The check now confirms the
  credential can actually see the repository (`GET /repos/{repo}`) before
  reading a 404 as an answer, and reports an error naming the reason when it
  cannot.
- 401/403 from the releases API is reported as a named error (expired, revoked,
  missing SSO authorization, or rate limited) instead of a raw exception string.
- `UpgradeService.check_for_update()` sends the same credential. Without it the
  auto-upgrade loop saw a permanent 404 on a private repo and silently never
  had an upgrade to do.
- Error strings are scrubbed of the token before they reach a log line, the
  activity log, or `/api/health`.

Unchanged, and deliberately: with no credential the check still reports
`state: error`, `stale: null`, `healthy: false` and says a token is needed. An
honest unknown is not replaced with a confident answer.

## v2.4.0 — 2026-08-14

Fixes the structural defects behind the 2026-08-12 publish failure, where one
mix reached no platform at all and nothing said so for two days.

### Platform legs are independent
- The orchestrator no longer walks one flat step list and stops at the first
  failure. Prep (detect → analyze → description → art) is still shared and
  sequential, but each publish target is its own leg: **one platform's failure
  can no longer prevent another platform from being attempted.** On 08-12 a dead
  SoundCloud grant left `upload_youtube` at "pending" while YouTube was healthy
  the whole time.
- A partial publish is a first-class state. A mix that reached some targets and
  not others is `partial`, with `pipeline_error` naming which is which and
  `metadata_json.publish` recording every leg's outcome.
- Steps behind a failed step in the SAME leg are recorded `blocked` with a
  reason, never left at "pending" — that resting state is what hid the stranded
  YouTube step.
- `POST /api/mixes/{id}/retry-platform/{platform}` re-runs ONE leg and
  re-finalizes the mix; `GET /api/mixes/{id}/publish-state` reports per-leg
  status. Recovering a half-published mix no longer needs a human driving the
  pipeline by hand.
- Finalization fails closed: only `published` and `skipped` (nothing
  configured) count as done. Pending/blocked/unknown legs cannot produce a
  "completed" mix.

### Failures name their real cause
- New failure classifier attributes each failure to a kind (`auth`, `quota`,
  `network`, `missing_file`, `video_not_ready`, `unknown`), a platform, and the
  **name** of the credential at fault — never its value. It walks the
  `__cause__` chain, so a chained auth failure beats the browser fallback's
  selector timeout that used to be reported instead.
- Auth failures are non-retryable and no longer burn the retry budget.
- The failure email and Discord embed lead with the cause, platform, credential
  and whether a retry can help. The subject line states the kind of failure.
- `YouTubeAuthError` joins `SoundCloudAuthError` under a shared
  `PlatformAuthError`, so a rejected Google refresh grant is reported as an auth
  failure too.

### Two false greens killed
- `GET /api/health` now asserts real function: it exercises the same auth path
  an upload uses for every configured platform, and returns **503** when a
  credential is dead, unknown, or stale. It previously returned `{"status":
  "ok"}` unconditionally — including throughout the 08-12 outage. Container
  liveness moved to the new `GET /api/health/live`, so a credential outage pages
  rather than cycling the process.
- Version detection no longer reads a VERSION file the image never contained.
  The version lives in `app/version.py`, the image bakes `BUILD_COMMIT` /
  `BUILD_TIME`, and `/api/upgrade/status` reports a tri-state check
  (`ok` / `error` / `unknown`) instead of a comfortable `update_available:
  false`. A deployment behind the latest release is reported as stale by
  `/api/health` and written to the activity log.
- The release check distinguishes "no releases" from "cannot see the
  releases": on a PRIVATE repo the unauthenticated API answers 404, which is
  indistinguishable from an empty release list, so that case is reported as
  `error` (with the reason) rather than silently "up to date". Set
  `GITHUB_TOKEN` to make the comparison work on a private repo.
- `/api/health` separates `problems` (503 — the service cannot be trusted to
  publish: dead/unverifiable credentials, DB down, a KNOWN-stale build) from
  `warnings` (200 — something is genuinely unknown and is said out loud, e.g.
  GitHub was unreachable). An unreachable third party does not take down the
  health of a service that can still publish, and unknown is never rendered
  as fine.

### Unpublished-mix watchdog
- A new watchdog sweeps every 30 minutes for mixes ingested but not published
  inside their window (default 6h; 72h for drafts awaiting review, both
  configurable in Settings → Pipeline). It alerts once per mix per 24h,
  deduplicated against the activity log so a restart cannot cause a storm.
- `GET /api/system/unpublished` exposes the result, and reports `unknown` when
  the sweep has never run rather than an empty all-clear.

## v2.3.0 — 2026-08-08

### Source file renaming
- New opt-in **Rename source files to match titles** setting (Settings →
  Advanced, OFF by default): when a run completes — and whenever a new title is
  applied from the catalog — the source audio and video in the watch folders are
  renamed to `YYYY-MM-DD <title>.<ext>` and the stored paths follow, so the
  archive on disk finally matches the published catalog.
- The date prefix comes from the ORIGINAL filename and the same token is applied
  to both files, so audio/video pairing and catalog local-file matching keep
  working — and the pair now shares an identical stem, which upgrades
  `_find_sibling` from its fuzzy same-date branch to its exact-stem branch. An
  imported back-catalog mix with no date in its filename gets no prefix rather
  than a fabricated one from its catalog-sync timestamp.
- Titles are sanitized to the strictest common set across ext4/NTFS/SMB (`|` and
  `/` become `-`, `:` becomes ` -`), NFC-normalized so macOS's decomposed SMB
  strings still compare equal, and truncated on a UTF-8 boundary against a
  255-byte ceiling.
- `POST /api/catalog/mixes/{id}/rename-source?dry_run=true` previews or triggers
  a rename for a single mix, and works while the setting is off — the only path
  the existing back-catalog has to the new naming.
- **Requires the audio and video bind mounts to be read-write.**
  `docker-compose.yml` now sets `/watch/audio` and `/watch/video` to `:rw`
  (`/watch/shorts` and `/watch/djctl-cue` stay `:ro`). Until that lands on the
  NAS the feature is a clean no-op: every rename is skipped and logged.

### Safety rails
This is the first code in the backend that writes to the watch folders, so the
guarantees are worth stating plainly:
- Only files inside `/watch/audio` and `/watch/video` are eligible — containment
  is checked with `commonpath`, not `startswith`, so a lookalike sibling like
  `/watch/audio-archive` is refused. `CATALOG_EXTRA_AUDIO_PATHS`,
  `/watch/shorts` and `/watch/djctl-cue` are excluded outright. `POST /api/mixes`
  accepts a caller-supplied `audio_file_path`, which is exactly what this rail
  defends against.
- An existing file is never overwritten: an occupied target name gets a ` (2)`
  through ` (9)` suffix and is then refused. Symlinks are skipped rather than
  followed.
- A rename can never fail a pipeline run, and can never cause a re-ingest —
  dedupe is keyed on the content hash, which a rename does not change.
- No DB session is held across the rename, so sqlite's write lock is never held
  while a NAS call blocks. A partial outcome (audio renamed, video not) is
  committed as-is, and a crash between the rename and the commit self-heals on
  the next run.

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
