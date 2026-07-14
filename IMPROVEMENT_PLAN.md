# fade-out — Improvement Plan

Prioritized backlog produced from an end-to-end audit of the pipeline (file
watcher → analyze → describe → art → upload → verify → cross-link). Items marked
**[DONE]** were implemented in the `feat/fade-out-improvements` branch. Items
marked **[NEEDS WILL]** require a paid service, a credential, or a taste/branding
call and were intentionally left for Will to decide.

Items marked **[DONE 07-13]** shipped in the follow-up
`feat/fade-out-id-labels-and-guards` branch (ID labels, OpenAI guards, chapter
wiring, gated cross-link push, structured logging, more tests).

Items marked **[DONE 07-13b]** shipped in the third follow-up
`feat/fade-out-endpoint-tests-migrations` branch (async HTTP endpoint tests,
Alembic migrations, gated Mixcloud upload path, WebSocket live status, genre
word-boundary matching).

Legend: impact (H/M/L) · effort (S/M/L).

---

## A. Track-detection accuracy

- **[DONE] Clean the final tracklist before it is stored** (H/S) — new
  `tracklist_utils.clean_tracklist()` sorts by timestamp, drops "phantom"
  entries where both artist and title are unknown/empty (Shazam/AudD noise), and
  collapses consecutive duplicate detections. Wired into `handle_analyze` so both
  SoundCloud and YouTube descriptions get a cleaner list, and video-offset
  detection runs against real tracks only.
- **[DONE] Single canonical timestamp formatter** (M/S) — `format_timestamp()`
  replaces four near-identical `h:mm:ss / m:ss` implementations scattered across
  `djctl_integration`, `description_generator`, and `audio_analyzer`. Removes
  drift risk (e.g. one place formatting 3661s differently from another).
- **[NEEDS WILL] Confirm AudD as the paid fingerprint fallback** (H/M) — PR #8
  already added the optional `AUDD_API_TOKEN` hook. Enabling it (paid) is the
  single biggest recall win on layered/underground DJ audio. Decision + token
  needed.
- **[LATER] Weight CUE/DJCTL timestamps over Shazam when both exist** (M/M) —
  `merge_tracklists` currently only backfills CUE `Track N` placeholders from
  Shazam by coarse 60s buckets. A confidence-scored merge (CUE time authoritative,
  Shazam/AudD for names) would be more robust.
- **[DONE 07-13b] Genre keyword over-matching** (L/S) — extracted the keyword
  boost into a pure, tested `genre_utils` module and switched substring matching
  to word-boundary matching (`keyword_matches`). "bassline" / "embassy" no longer
  credit drum & bass; only whole-word hits boost a genre. Unit-tested.

## B. Reliability / error-handling

- **[DONE] Import-time filesystem side-effect made non-fatal** (M/S) —
  `database.py` did a bare `os.makedirs("/data")` at import; on a read-only or
  sandboxed host this crashed the whole process (and blocked all unit testing).
  Now wrapped so a failure logs and defers to engine connect time.
- **[DONE] Robust YouTube video-ID extraction** (M/S) — `handle_verify_youtube`
  used `url.split("v=")[-1]`, which silently breaks on `youtu.be/<id>` and
  `/shorts/<id>` forms and mangles extra query params. Replaced with a tolerant
  `extract_youtube_video_id()` helper (covers watch?v=, youtu.be, embed, shorts).
- **[DONE 07-13] Guard OpenAI empty responses** (M/S) —
  `_create_completion()` centralizes the three chat calls, guards `None`/empty
  content (content filter / length cutoff), retries once, and raises a clear
  `RuntimeError` instead of letting `.strip()` blow up. `_response_text()` is a
  pure, tested extractor.
- **[DONE 07-13] Model-aware token pricing** (L/S) — `price_for_model()` replaces
  the hard-coded gpt-4o rate with a per-model table (longest-prefix match for
  dated snapshots, gpt-4o fallback for unknown models). Wired into
  `_track_usage`; unit-tested.
- **[LATER] Persist file-watcher "seen" state atomically** (L/M) — a crash between
  `mark_seen` and callback success can drop a file. Consider marking seen only
  after successful hand-off, or a processing/done state.

## C. Output quality (descriptions / metadata / tags)

- **[DONE] Enforce YouTube's 500-character aggregate tag limit** (H/S) — YouTube
  rejects the *entire* `videos.insert` call if the combined length of all tags
  (quoted when they contain spaces/commas, plus separators) exceeds 500 chars.
  The generator produced up to 30 tags with no char budget, risking hard upload
  failures. `format_for_youtube()` now trims to fit the real API budget.
- **[DONE 07-13] YouTube chapter guarantees** (H/M) — `build_youtube_chapters()`
  is now wired into `generate_youtube_description`: a deterministic, validated
  chapter block (first stamp `0:00`, ≥3 chapters, ≥10s apart) is appended to the
  YouTube description, and the model is told not to write its own tracklist. Falls
  back to the model tracklist when there are too few tracks for real chapters.
- **[DONE 07-13] Render unidentified tracks as "ID - ID"** (M/S) — Will approved
  the wording. `label_or_id()` in `tracklist_utils` maps unknown/blank/"Track N"
  fields to the canonical `ID` label; `clean_tracklist` now normalizes (and
  collapses consecutive) unidentified tracks to `ID - ID` at storage time, and
  descriptions, YouTube chapters, cross-links, and the recognition source
  (Shazam/AudD/CUE/WS defaults) all inherit it. Tag generation skips `ID` as a
  discovery artist.
- **[NEEDS WILL] Thumbnail/cover art review** (M/L) — visual/branding output; out
  of scope for autonomous change.

## D. Publishing / platform integration

- **[DONE 07-13] Push cross-links to the platforms** (H/M) — implemented against
  existing OAuth tokens (no new credentials): `YouTubeUploader.update_description`
  (fetch snippet → `videos.update`) and `SoundCloudUploader.update_description`
  (`/resolve` → `PUT /tracks/:id`). `handle_cross_link` calls them, each push
  best-effort (a failure logs but never fails the pipeline). Gated behind
  `CROSS_LINK_PUSH_ENABLED` (default **OFF**) so a published description is never
  mutated without an explicit opt-in — Will flips the flag when ready.
- **[DONE 07-13b] Mixcloud upload support** (M/L) — new `MixcloudUploader`
  mirrors the SoundCloud/YouTube uploader shape (`upload` / `verify_upload` /
  `update_description`) against the official Mixcloud API. Added `upload_mixcloud`
  + `verify_mixcloud` pipeline steps, a `mixes.mixcloud_url` column, and wove the
  Mixcloud URL into the cross-link descriptions. The **entire path is gated**
  behind `MIXCLOUD_ENABLED` (default **OFF**) AND requires an access token, so
  the steps no-op (skip) until Will completes the Mixcloud OAuth flow and flips
  the flag — no new live credentials required to ship. Unit-tested (uploader +
  handler gating + cross-link weave).
- **[NEEDS WILL] Scheduled release queue** (M/M) — premiere timing is Will's call.

## E. Code quality / tests

- **[DONE] First test suite** (H/M) — `backend/tests/` with pytest coverage for
  the pure logic most likely to regress: tracklist cleaning + timestamp
  formatting, tag generation + the new YouTube tag budget, CUE parsing +
  date-proximity matching + tracklist merge, YouTube premiere-time selection,
  filename→title derivation, and video-ID extraction. `pytest.ini` +
  `requirements-dev.txt` added.
- **[DONE 07-13] More pure-logic + handler tests** (M/M) — added
  `test_description_generator.py` (model pricing, empty-response guard, chapter
  block), `test_cross_link.py` (handler description-linking, idempotent, skip),
  `test_logging_config.py` (JSON formatter + handler setup), and expanded
  `test_tracklist_utils.py` for the ID-label behavior. 92 tests pass.
- **[DONE 07-13b] Full async API endpoint tests** (M/M) — `test_api_endpoints.py`
  drives the real ASGI app through `httpx.AsyncClient` + `ASGITransport` with a
  fresh SQLite schema per test (`client`/`prepared_db` fixtures in `conftest.py`;
  orchestrator launch methods stubbed). Covers health, mixes CRUD + 404s, draft/
  retry state guards, pipeline status/pause/resume/queue, and notification
  settings round-trip.
- **[DONE 07-13] Structured logging** (M/M) — `app/logging_config.py` adds a JSON
  formatter + single-handler `configure_logging()`, wired into `main.lifespan`.
  Opt-in via `LOG_JSON` (default False → unchanged plain-text format).

## F. Developer / UX ergonomics

- **[DONE 07-13b] Alembic migrations** (M/M) — `backend/migrations/` (dir named
  `migrations/` to avoid shadowing the installed `alembic` package on `sys.path`)
  with `alembic.ini` + an async `env.py` that pulls `DATABASE_URL` from app
  settings. `0001_initial_schema` is the create-from-scratch baseline;
  `0002_add_mixcloud_url` adds the new column via a SQLite-safe batch ALTER.
  `create_all` stays for the zero-config first-run path; existing DBs adopt
  Alembic by stamping the baseline (see `migrations/README.md`). Verified:
  `alembic upgrade head` builds the schema and `--autogenerate` reports **no
  drift** vs the ORM models. Tested (`test_migrations.py`).
- **[DONE 07-13b] WebSocket live pipeline status** (M/M) — `app/routers/ws.py`
  adds a `ConnectionManager` registered as an orchestrator event listener
  (`on_event`), so every `pipeline_started`/`step_completed`/`upload_complete`/
  `error`/`draft_ready` event is fanned out to clients on `GET /api/ws/status`.
  Clients get a one-shot queue-count `snapshot` on connect. Dead sockets are
  pruned on broadcast. Unit-tested (connect/broadcast/prune/event-shape).
