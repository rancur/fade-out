# Making the retag backfill safe to run unattended

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** remove the four reasons the 498 GB retag backfill currently needs a human watching it.

**Why:** v2.5.0 shipped the backfill with known gaps, stated in its release notes — not idempotent, not batched, not resumable, no durable audit trail, and orphaned staging files nothing reclaims. The operator instruction today is "run it with `limit` under supervision", and `limit` doesn't even paginate: `_run_retag` uses `.limit(n)` with no `ORDER BY` and no offset, so successive batches re-select the same rows.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, mutagen, pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-source-file-metadata-design.md` (the gaps closed here are its unfulfilled promises at lines 105-106).

## Global Constraints

- Nothing may weaken the existing safety properties: register-before-promote ordering, the audio-payload truncation check, fsync-before-rename, never-fatal contracts, `dry_run` defaulting to True, the 409 single-flight guard.
- `source_renamer` is ALREADY idempotent (`source_renamer.py:352` → `{"status": "unchanged"}` when the basename already matches). Do not add a second mechanism; only the tagger needs one.
- Persisted job state follows the existing precedent: a key in `AppSettings.settings_json`, as `catalog_sync` does with `catalog_last_sync` (`catalog_sync.py:36`).
- Durable per-mix outcomes go through `app.services.activity_log` (`log`/`info`/`warn`/`error`), which is what every other background job in this codebase uses.
- Every new test must fail when the behaviour it guards is reverted. State the mutation check in your report. This branch's predecessor shipped three tests that asserted something other than their name claimed; do not add a fourth.

---

### Task 1: Skip files that already carry the target tags

Today every backfill run rewrites every file in full, even one tagged an hour ago. That makes a re-run cost the entire 498 GB, which is the single biggest reason the operation can't simply be repeated after an interruption.

**Files:**
- Modify: `backend/app/services/source_tagger.py`
- Test: `backend/tests/test_source_tagger_idempotent.py`

**Interfaces:**
- Produces: `source_tagger.already_tagged(path: str, tags: Dict[str, str], cover_art_path: Optional[str]) -> bool`
- `tag_sources_for_mix` gains a `"skipped"` outcome with reason `"already tagged"`.

- [ ] **Step 1: Write the failing test.** Cover: a freshly tagged file reports True; an untagged file reports False; a file whose TITLE differs reports False (so a retitled mix IS re-tagged); a file tagged without cover art reports False when cover art is now available; the check reads only the header and never rewrites (assert mtime and size unchanged).
- [ ] **Step 2: Run it, confirm it fails** (`AttributeError: already_tagged`).
- [ ] **Step 3: Implement.** Read existing Vorbis comments with mutagen and compare against the target tag set; compare picture presence against whether `cover_art_path` exists. Header read only — no copy, no save. Then, in `tag_sources_for_mix`, after `build_tags` and the existence/space checks, return `{"status": "skipped", "reason": "already tagged", ...}` when it reports True. **Dry run must report this too** — a dry run over an already-tagged library should say "would skip, already tagged", not "would tag".
- [ ] **Step 4: Run, confirm pass.** Verify by mutation: make `already_tagged` always return False and confirm the skip tests fail.
- [ ] **Step 5: Commit** `feat(tagger): skip files that already carry the target tags`.

---

### Task 2: Ordered, offset-based pagination and a resume cursor

**Files:**
- Modify: `backend/app/routers/catalog.py`
- Test: `backend/tests/test_retag_pagination_resume.py`

**Interfaces:**
- `POST /api/catalog/retag` gains `offset: int = Query(default=0)` and `resume: bool = Query(default=False)`.
- Persisted state lives at `AppSettings.settings_json["retag_progress"]`.

- [ ] **Step 1: Write the failing test.** Cover: selection is ordered deterministically; `limit`+`offset` select disjoint sets (batch 1 and batch 2 share no mix); the candidate count in the POST response reflects limit/offset; a completed run persists a cursor; `resume=true` continues after the last processed mix rather than restarting; `resume=true` with no saved cursor starts at the beginning rather than erroring.
- [ ] **Step 2: Run it, confirm it fails.**
- [ ] **Step 3: Implement.** Add `.order_by(Mix.created_at, Mix.id)` to BOTH the count query and `_run_retag`'s selection — they must stay identical or the pre-flight number lies. Apply `.offset(offset)`. After each mix, write `{"last_mix_id", "processed", "total", "dry_run", "updated_at"}` to `AppSettings.settings_json["retag_progress"]`, committing as you go so a kill -9 leaves a usable cursor. On `resume=true`, load it and skip past `last_mix_id`.
- [ ] **Step 4: Run, confirm pass.** Mutation-check: remove the `order_by` and confirm the disjoint-batches test fails.
- [ ] **Step 5: Commit** `feat(catalog): ordered pagination and a resume cursor for the retag backfill`.

---

### Task 3: Durable per-mix outcomes in the activity log

Today a run's results live only in the module-level `_retag_state` dict, wiped on restart. After an unattended run you cannot answer "what did it actually do?"

**Files:**
- Modify: `backend/app/routers/catalog.py`
- Test: `backend/tests/test_retag_activity_events.py`

- [ ] **Step 1: Write the failing test.** Cover: each processed mix emits exactly one activity event carrying `mix_id` and both statuses; a failure emits at `error` level; a skip emits at `info`; the run emits a start event and a completion event carrying the summary; an activity-log failure does NOT abort the run (it is best-effort, like every other call here).
- [ ] **Step 2: Run it, confirm it fails.**
- [ ] **Step 3: Implement** using `app.services.activity_log`, matching the event-name style already used (`sc_transcode_started`, `catalog_sync`, etc.) — use a `retag_` prefix. Wrap every emit so logging can never take down the backfill.
- [ ] **Step 4: Run, confirm pass.** Mutation-check: make the emit raise and confirm the run still completes.
- [ ] **Step 5: Commit** `feat(catalog): emit durable activity events for retag outcomes`.

---

### Task 4: Reclaim orphaned staging files

`.fadeout-tagging/` accumulates multi-gigabyte temps when a run dies between staging and promotion. Nothing reclaims them, the directory is hidden from the watcher, and `_has_free_space` reports the resulting shortage as a routine `"skipped"` — silent degradation in both directions.

**Files:**
- Modify: `backend/app/services/source_tagger.py`
- Modify: `backend/app/routers/catalog.py`
- Test: `backend/tests/test_staging_sweeper.py`

**Interfaces:**
- Produces: `source_tagger.sweep_staging(root: str, older_than_hours: float = 6.0, dry_run: bool = False) -> Dict[str, Any]`
- `POST /api/catalog/retag` sweeps before starting.

- [ ] **Step 1: Write the failing test.** Cover: a file older than the threshold is removed and its size reported; a file NEWER than the threshold is left alone (it may belong to a run in flight — removing it mid-write is the one thing this must never do); a non-staging directory is refused; `dry_run=True` removes nothing but reports what it would; a permission error on one file does not abort the sweep.
- [ ] **Step 2: Run it, confirm it fails.**
- [ ] **Step 3: Implement.** Only ever operate on a path whose basename is `TEMP_DIRNAME`, and only inside `source_renamer.allowed_roots()` — reuse the existing allowlist rather than inventing a second one. Age off mtime. Call it at the start of `_run_retag`, log what it reclaimed.
- [ ] **Step 4: Run, confirm pass.** Mutation-check: remove the age guard and confirm the "leaves a fresh file alone" test fails.
- [ ] **Step 5: Commit** `feat(tagger): reclaim orphaned staging files before a backfill run`.

---

### Task 5: Documentation

- [ ] Update `README.md`: re-tagging now skips already-tagged files; `limit`+`offset` genuinely paginate; `resume=true`; outcomes land in the activity log; staging is swept automatically. **Remove the "run under supervision" instruction** — it exists because of the gaps this branch closes — and replace it with what is now true.
- [ ] Add a `CHANGELOG.md` entry under a new version heading following the file's existing style.
- [ ] Commit `docs: the backfill is now resumable, idempotent and auditable`.

## Self-Review

Spec lines 105-106 promised batched + resumable + activity events: Tasks 2 and 3. The plan's own acknowledged idempotence gap: Task 1. The final review's orphan finding: Task 4. Nothing else from the v2.5.0 known-limitations list is left open.
