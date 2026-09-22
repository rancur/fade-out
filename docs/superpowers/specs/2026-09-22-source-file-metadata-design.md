# Source-file metadata and renaming

**Status:** approved 2026-09-22
**Goal:** the recordings on the NAS should be identifiable on their own — correct
filenames and embedded tags — so they are usable in Plex or any local player
without fade-out present. Publishing behaviour must not change.

## Problem

fade-out generates a title at `generate_description` and publishes it
everywhere, but the files on disk keep whatever OBS called them
(`2026-08-11 14-45-55.flac`). Opened directly, a mix has no artist, no title,
no date and no artwork.

Renaming is already solved: `source_renamer` writes
`YYYY-MM-DD <title>.<ext>` for audio and video, is opt-in, allowlisted to the
watch roots, refuses to clobber, and can never fail a run. It is simply
switched off (`rename_source_files` unset). **This design does not rebuild it.**

What is missing is tag writing. `mutagen` is already a dependency but is used
strictly read-only, for durations.

## The hazard that shapes the design

The watcher dedupes on `md5(first 10 MB)`. Measured on two real library files:

```
STREAMINFO      offset=4    size=34
VORBIS_COMMENT  offset=42   size=40   <-- LAST block
audio frames begin at byte 86
```

Two consequences, both load-bearing:

1. **Metadata lives inside the dedupe window.** Writing tags changes the hash,
   so the watcher sees a new file and re-ingests it — a fresh pipeline run that
   re-uploads an already-published mix. `source_renamer` was explicitly
   designed around renames being hash-neutral; tagging breaks that assumption.
2. **There is no PADDING**, and audio starts at byte 86. No realistic tag set
   fits in place, so every first write is a **full file rewrite**. With cover
   art embedded, unavoidably so.

Library: 80 FLACs, 498 GB, on a volume with 11 TB free. Space is not a
constraint; I/O time is (~1 TB read+write).

## Design

### Tag out-of-place, then promote

A full rewrite is required anyway, so writing to a temp location costs the same
I/O and eliminates both the race and the risk to the original:

```
1. copy+tag  ->  /volume1/music/.fadeout-tagging/<name>.flac
                 (same filesystem, OUTSIDE the watch roots)
2. verify    ->  re-read with mutagen; duration must match the DB within 1s
3. register  ->  md5(first 10 MB of temp) -> seen_db.mark_done(new_hash)
4. promote   ->  atomic os.rename into the watch folder
```

The new hash is registered *before* the file is visible to the watcher, so
there is no window in which an unregistered file exists in a watch root. The
original is untouched until the atomic rename, so a crash or power loss cannot
destroy an irreplaceable recording. In-place tagging provides neither
guarantee. The old hash row is kept, so a restored backup still dedupes.

### `source_tagger.py`

Mirrors `source_renamer`'s safety model deliberately:

- **Opt-in** — `tag_source_files`; off is a clean, logged no-op.
- **Allowlisted** — reuses `source_renamer.is_within_allowed_roots`.
- **Published only** — acts solely on `pipeline_status == "completed"`, never
  on a mix mid-flight.
- **Never fatal** — every failure is caught, classified and logged. Tagging
  cannot fail a pipeline run.
- **Idempotent** — a file already carrying the target tags is skipped.

### Tags

| Tag | Source |
|---|---|
| `ARTIST` | `Will See` |
| `TITLE` | `Mix.title` |
| `DATE` | date token (filename, else `created_at`) |
| `GENRE` | `Mix.genres` |
| `ALBUM` | `Will See Mixes` |
| `DESCRIPTION` | `Mix.tracklist`, formatted |
| `URL` | `soundcloud_url` or `youtube_url` |
| `PICTURE` | `Mix.cover_art_path` |

Written with **64 KB of PADDING**, so this is the only full rewrite these files
ever need; later edits land in place.

### Ordering

Rename first (it updates `audio_file_path`), then tag the resulting path. Both
strictly after publish.

### Retroactive backfill

`POST /api/catalog/retag`, matching the `catalog_sync` pattern.

- `dry_run=true` reports every planned rename and tag, plus collisions and
  missing sources, touching nothing.
- Real runs are batched and resumable; progress via activity events.
- Free-space check before each file: refuse if free space < 2x file size.

### Video

Renamed only. MKV tagging needs `mkvpropedit` or an ffmpeg remux — a new
dependency and a rewrite risk on multi-GB files, for little Plex benefit.

## Testing

The re-ingest guarantee is the point of the feature and gets a real test, not a
mocked one: tag a file, run the watcher's own detection over the directory,
assert no new pipeline run is produced.

Also covered: hash changes on tagging (the premise — asserted, not assumed);
the temp file never lands inside a watch root; promotion is atomic; a failed
verify leaves the original untouched; collisions; missing source; read-only
mount; corrupt file; idempotent re-tagging; dry-run mutates nothing.

## Out of scope

MKV tags; changing the dedupe hash algorithm; re-encoding; touching published
platform metadata.
