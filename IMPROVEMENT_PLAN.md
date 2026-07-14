# fade-out — Improvement Plan

Prioritized backlog produced from an end-to-end audit of the pipeline (file
watcher → analyze → describe → art → upload → verify → cross-link). Items marked
**[DONE]** were implemented in the `feat/fade-out-improvements` branch. Items
marked **[NEEDS WILL]** require a paid service, a credential, or a taste/branding
call and were intentionally left for Will to decide.

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
- **[LATER] Genre keyword over-matching** (L/S) — `"bass"` matches "bassline",
  "bass house", etc. and over-credits drum & bass. Use word-boundary matching.

## B. Reliability / error-handling

- **[DONE] Import-time filesystem side-effect made non-fatal** (M/S) —
  `database.py` did a bare `os.makedirs("/data")` at import; on a read-only or
  sandboxed host this crashed the whole process (and blocked all unit testing).
  Now wrapped so a failure logs and defers to engine connect time.
- **[DONE] Robust YouTube video-ID extraction** (M/S) — `handle_verify_youtube`
  used `url.split("v=")[-1]`, which silently breaks on `youtu.be/<id>` and
  `/shorts/<id>` forms and mangles extra query params. Replaced with a tolerant
  `extract_youtube_video_id()` helper (covers watch?v=, youtu.be, embed, shorts).
- **[LATER] Guard OpenAI empty responses** (M/S) — `response.choices[0].message.content`
  can be `None` (content filter / length) → `.strip()` raises. Add a null guard +
  one retry. (Left out of this PR because `description_generator.py` is also
  touched by PR #8 — avoid a merge conflict; do it once #8 lands.)
- **[LATER] Model-aware token pricing** (L/S) — `_track_usage` hard-codes gpt-4o
  prices; wrong if `OPENAI_MODEL` changes. Same file as PR #8, deferred.
- **[LATER] Persist file-watcher "seen" state atomically** (L/M) — a crash between
  `mark_seen` and callback success can drop a file. Consider marking seen only
  after successful hand-off, or a processing/done state.

## C. Output quality (descriptions / metadata / tags)

- **[DONE] Enforce YouTube's 500-character aggregate tag limit** (H/S) — YouTube
  rejects the *entire* `videos.insert` call if the combined length of all tags
  (quoted when they contain spaces/commas, plus separators) exceeds 500 chars.
  The generator produced up to 30 tags with no char budget, risking hard upload
  failures. `format_for_youtube()` now trims to fit the real API budget.
- **[LATER] YouTube chapter guarantees** (H/M) — YouTube only renders chapters if
  the first timestamp is exactly `0:00`, there are ≥3 of them, and each is ≥10s
  apart. `build_youtube_chapters()` (added, tested, not yet wired) enforces this;
  wiring it means editing the branded description body, which overlaps PR #8 —
  do it once #8 merges.
- **[LATER] Render unidentified tracks as "ID - ID"** (M/S) — DJ convention.
  Slight taste call on wording; flag for Will.
- **[NEEDS WILL] Thumbnail/cover art review** (M/L) — visual/branding output; out
  of scope for autonomous change.

## D. Publishing / platform integration

- **[LATER] Push cross-links to the platforms** (H/M) — `handle_cross_link` only
  mutates the local DB copy; it never calls SoundCloud `PUT /tracks/:id` or
  YouTube `videos.update`, so the published descriptions never actually get the
  reciprocal link. Real API writes needed.
- **[LATER] Mixcloud upload support** (M/L) — roadmap item; new integration.
- **[NEEDS WILL] Scheduled release queue** (M/M) — premiere timing is Will's call.

## E. Code quality / tests

- **[DONE] First test suite** (H/M) — `backend/tests/` with pytest coverage for
  the pure logic most likely to regress: tracklist cleaning + timestamp
  formatting, tag generation + the new YouTube tag budget, CUE parsing +
  date-proximity matching + tracklist merge, YouTube premiere-time selection,
  filename→title derivation, and video-ID extraction. `pytest.ini` +
  `requirements-dev.txt` added.
- **[LATER] API endpoint tests** (M/M) — needs an async test client + DB fixtures
  (roadmap item). Foundation (conftest, env isolation) is now in place.

## F. Developer / UX ergonomics

- **[LATER] Structured logging** (M/M) — roadmap item; consistent JSON logs.
- **[LATER] Alembic migrations** (M/M) — schema changes are currently implicit.
- **[LATER] WebSocket live pipeline status** (M/M) — roadmap item.
