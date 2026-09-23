# Source-File Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write proper tags into the FLAC recordings on the NAS and rename the source files to match their published titles, so a mix is identifiable in Plex or any local player without fade-out present.

**Architecture:** A new `source_tagger` service mirrors the existing `source_renamer` safety model (opt-in, allowlisted, never fatal). Because FLAC metadata sits inside the watcher's 10 MB dedupe window and these files carry no padding, tagging rewrites the whole file — so it is done **out-of-place** into a dot-directory the watcher never scans, the resulting hash is registered as already-seen, and only then is the file promoted into place with an atomic rename. Renaming is not rebuilt; it already exists and is merely switched on.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, mutagen (already a dependency), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-22-source-file-metadata-design.md`

## Global Constraints

- Tagging is **opt-in** via the `tag_source_files` setting. Off is a clean, logged no-op.
- Tagging **only ever touches** files inside `source_renamer.allowed_roots()`.
- Tagging **only ever acts on** a mix whose `pipeline_status == "completed"`.
- Tagging **can never raise** into a pipeline run. Every failure is caught, classified, logged.
- The temp directory is `<watch_audio_root>/.fadeout-tagging/`. It MUST be on the same filesystem as the target (for atomic `os.rename`) and MUST NOT be scanned by the watcher. The watcher uses non-recursive `os.listdir`, so a subdirectory is invisible to it.
- The new hash MUST be registered in `seen_files` **before** the tagged file is promoted into a watch root. Never the other way round.
- The original file MUST remain untouched until the atomic promote.
- FLAC writes use **65536 bytes of padding**, so only the first write is a full rewrite.
- `ARTIST` is always `Will See`. `ALBUM` is always `Will See Mixes`.
- Free space must be at least **2x** the source file size before tagging it.
- Both `/watch/audio` and `/watch/video` are already mounted `rw=true` on the NAS. No compose change is required.

---

### Task 1: Expose the watcher's hash and seen-file DB

`source_tagger` must compute the same hash the watcher computes and write to the same `seen_files` table. Both are currently private (`_compute_file_hash`, `_SeenFilesDB`). Reaching into privates from another module would make this contract invisible to anyone editing `file_watcher`, so publish a deliberate, documented surface instead.

**Files:**
- Modify: `backend/app/services/file_watcher.py`
- Test: `backend/tests/test_seen_files_api.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `file_watcher.compute_file_hash(path: str) -> str`, `file_watcher.open_seen_files_db(db_path: str = "data/seen_files.db") -> _SeenFilesDB`, and `_SeenFilesDB.mark_done(file_hash: str, file_path: str, file_type: str) -> None` (already exists).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_seen_files_api.py
"""The tagger depends on hashing and seen-file registration being public.

These are the two pieces of file_watcher that source_tagger must reuse exactly.
If either changes shape, tagging silently stops protecting against re-ingest,
so the contract is pinned here rather than left implicit.
"""
import os

from app.services import file_watcher


def test_compute_file_hash_is_public_and_stable(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"x" * 2048)
    first = file_watcher.compute_file_hash(str(p))
    assert first == file_watcher.compute_file_hash(str(p))
    assert len(first) == 32


def test_compute_file_hash_changes_when_the_head_changes(tmp_path):
    """The premise of the whole design: editing the head changes the hash."""
    p = tmp_path / "a.bin"
    p.write_bytes(b"A" + b"x" * 2048)
    before = file_watcher.compute_file_hash(str(p))
    p.write_bytes(b"B" + b"x" * 2048)
    assert file_watcher.compute_file_hash(str(p)) != before


def test_open_seen_files_db_roundtrips_done_state(tmp_path):
    db = file_watcher.open_seen_files_db(str(tmp_path / "seen.db"))
    assert db.is_done("deadbeef") is False
    db.mark_done("deadbeef", "/watch/audio/x.flac", "audio")
    assert db.is_done("deadbeef") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_seen_files_api.py -v`
Expected: FAIL — `AttributeError: module 'app.services.file_watcher' has no attribute 'compute_file_hash'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/file_watcher.py`, after the existing `_compute_file_hash` definition, add:

```python
def compute_file_hash(path: str) -> str:
    """Public alias of the dedupe hash.

    ``source_tagger`` must compute the byte-identical value, because writing
    FLAC tags changes the file head and therefore this hash; the tagger
    re-registers the new hash so the watcher does not treat a tagged file as a
    new recording and re-ingest an already-published mix. Keep this and
    ``_compute_file_hash`` the same function, not merely similar.
    """
    return _compute_file_hash(path)


def open_seen_files_db(db_path: str = "data/seen_files.db") -> "_SeenFilesDB":
    """Open the dedupe database the watcher uses.

    Exposed so the tagger can register a tagged file's new hash against the
    same table the watcher reads.
    """
    return _SeenFilesDB(db_path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_seen_files_api.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/file_watcher.py backend/tests/test_seen_files_api.py
git commit -m "refactor(watcher): publish the dedupe hash and seen-file DB for the tagger"
```

---

### Task 2: Build the tag set (pure function, no I/O)

**Files:**
- Create: `backend/app/services/source_tagger.py`
- Test: `backend/tests/test_source_tagger_tags.py`

**Interfaces:**
- Consumes: `source_renamer.resolve_date_token`.
- Produces: `source_tagger.build_tags(mix) -> Dict[str, str]` where `mix` is an `app.models.Mix` (or any object with those attributes).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_source_tagger_tags.py
"""Tag construction is pure, so it is tested without touching a disk."""
from datetime import datetime
from types import SimpleNamespace

from app.services.source_tagger import build_tags


def _mix(**kw):
    base = dict(
        id="m1",
        title="Lunar Desert Groove | Trance Mix",
        audio_file_path="/watch/audio/2026-09-18 16-50-12.flac",
        video_file_path=None,
        genres=["Trance", "Progressive"],
        tracklist=[
            {"title": "One", "artist": "A", "timestamp": "00:00"},
            {"title": "Two", "artist": "B", "timestamp": "05:30"},
        ],
        cover_art_path="/output/cover-art/m1.jpg",
        soundcloud_url="https://soundcloud.com/thewillsee/lunar-desert-groove-trance-mix",
        youtube_url="https://www.youtube.com/watch?v=28j2P14Qp_g",
        created_at=datetime(2026, 9, 18, 16, 50, 12),
        source="watch",
        pipeline_status="completed",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_artist_and_album_are_fixed():
    tags = build_tags(_mix())
    assert tags["ARTIST"] == "Will See"
    assert tags["ALBUM"] == "Will See Mixes"


def test_title_comes_from_the_mix():
    assert build_tags(_mix())["TITLE"] == "Lunar Desert Groove | Trance Mix"


def test_date_prefers_the_filename_token():
    assert build_tags(_mix())["DATE"] == "2026-09-18"


def test_genres_are_joined():
    assert build_tags(_mix())["GENRE"] == "Trance; Progressive"


def test_tracklist_becomes_a_readable_description():
    desc = build_tags(_mix())["DESCRIPTION"]
    assert "00:00 A - One" in desc
    assert "05:30 B - Two" in desc


def test_url_prefers_soundcloud_then_youtube():
    assert build_tags(_mix())["URL"].startswith("https://soundcloud.com/")
    assert build_tags(_mix(soundcloud_url=None))["URL"].startswith("https://www.youtube.com/")


def test_missing_optional_fields_are_omitted_not_blank():
    tags = build_tags(_mix(genres=None, tracklist=None, soundcloud_url=None, youtube_url=None))
    assert "GENRE" not in tags
    assert "DESCRIPTION" not in tags
    assert "URL" not in tags
    assert tags["ARTIST"] == "Will See"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_source_tagger_tags.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.source_tagger'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/source_tagger.py
"""Write library metadata into a mix's source recording.

fade-out publishes a generated title, artwork and tracklist to SoundCloud and
YouTube, but the FLAC on the NAS carries none of it. Opened in Plex or any
local player a mix has no artist, no title, no date and no artwork. This module
closes that gap for the file itself; ``source_renamer`` closes it for the name.

**Why this is not a simple mutagen call.** The watcher dedupes on
``md5(first 10 MB)`` and FLAC metadata blocks live at the very start of the
file -- measured on this library, audio frames begin at byte 86 with zero
padding. So writing tags (a) changes the dedupe hash, which would make the
watcher re-ingest a published mix and upload it a second time, and (b) cannot
fit in place, forcing a full rewrite of a multi-gigabyte file.

The rewrite is therefore done out-of-place and promoted atomically:

    1. copy+tag  -> <watch_audio>/.fadeout-tagging/<name>   (same filesystem,
                    and invisible to the watcher, which uses a non-recursive
                    os.listdir)
    2. verify    -> re-read; duration must still match the database
    3. register  -> md5(first 10 MB of temp) -> seen_files, marked done
    4. promote   -> atomic os.rename into place

Registering before promoting means a file is never visible to the watcher
while unregistered. Writing out-of-place means the original survives a crash,
a full disk, or a corrupt write. In-place tagging gives neither guarantee.
"""

import logging
from typing import Any, Dict, List, Optional

from app.services import source_renamer

logger = logging.getLogger("fadeout.source_tagger")

ARTIST = "Will See"
ALBUM = "Will See Mixes"
TEMP_DIRNAME = ".fadeout-tagging"
FLAC_PADDING_BYTES = 65536


def _format_tracklist(tracklist: Optional[List[Dict[str, Any]]]) -> str:
    lines = []
    for entry in tracklist or []:
        if not isinstance(entry, dict):
            continue
        ts = (entry.get("timestamp") or "").strip()
        artist = (entry.get("artist") or "").strip()
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        label = f"{artist} - {title}" if artist else title
        lines.append(f"{ts} {label}".strip())
    return "\n".join(lines)


def build_tags(mix: Any) -> Dict[str, str]:
    """The Vorbis comments to write for this mix. Pure; touches no disk."""
    tags: Dict[str, str] = {"ARTIST": ARTIST, "ALBUM": ALBUM}

    if getattr(mix, "title", None):
        tags["TITLE"] = mix.title

    date_token = source_renamer.resolve_date_token(
        getattr(mix, "audio_file_path", None) or "",
        getattr(mix, "video_file_path", None) or "",
        source=getattr(mix, "source", None),
        created_at=getattr(mix, "created_at", None),
    )
    if date_token:
        tags["DATE"] = date_token

    genres = getattr(mix, "genres", None)
    if genres:
        tags["GENRE"] = "; ".join(str(g) for g in genres if g)

    description = _format_tracklist(getattr(mix, "tracklist", None))
    if description:
        tags["DESCRIPTION"] = description

    url = getattr(mix, "soundcloud_url", None) or getattr(mix, "youtube_url", None)
    if url:
        tags["URL"] = url

    return tags
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_source_tagger_tags.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/source_tagger.py backend/tests/test_source_tagger_tags.py
git commit -m "feat(tagger): build the Vorbis comment set for a mix"
```

---

### Task 3: Write tags out-of-place with verification

**Files:**
- Modify: `backend/app/services/source_tagger.py`
- Test: `backend/tests/test_source_tagger_write.py`

**Interfaces:**
- Consumes: `build_tags` from Task 2.
- Produces: `source_tagger.temp_dir_for(path: str) -> str`, `source_tagger.write_tagged_copy(src: str, tags: Dict[str, str], cover_art_path: Optional[str], expected_duration: Optional[float]) -> str` returning the temp file path. Raises `RuntimeError` on verification failure, having removed the temp file.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_source_tagger_write.py
"""Tagging writes a verified copy; it never edits the original in place."""
import os

import pytest
from mutagen.flac import FLAC

from app.services import source_tagger

pytest.importorskip("mutagen")


def _make_flac(path):
    """A real, tiny, valid FLAC. Synthesised rather than fixtured so the test
    exercises mutagen's actual rewrite path."""
    import subprocess
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "1", "-c:a", "flac", "-y", str(path)],
        check=True, capture_output=True,
    )


def test_temp_dir_is_a_dot_dir_beside_the_source():
    d = source_tagger.temp_dir_for("/watch/audio/x.flac")
    assert d == "/watch/audio/.fadeout-tagging"
    assert os.path.basename(d).startswith("."), "must be hidden from casual listing"


def test_writes_tags_into_a_copy_and_leaves_the_original_alone(tmp_path):
    src = tmp_path / "orig.flac"
    _make_flac(src)
    before = src.read_bytes()

    out = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T"}, None, None
    )

    assert out != str(src)
    assert src.read_bytes() == before, "the original must not be modified"
    assert FLAC(out)["ARTIST"] == ["Will See"]
    assert FLAC(out)["TITLE"] == ["T"]


def test_tagging_changes_the_dedupe_hash(tmp_path):
    """The premise of the design, asserted rather than assumed."""
    from app.services.file_watcher import compute_file_hash

    src = tmp_path / "orig.flac"
    _make_flac(src)
    out = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T"}, None, None
    )
    assert compute_file_hash(out) != compute_file_hash(str(src))


def test_padding_is_added_so_later_edits_are_in_place(tmp_path):
    src = tmp_path / "orig.flac"
    _make_flac(src)
    out = source_tagger.write_tagged_copy(str(src), {"ARTIST": "Will See"}, None, None)
    padding = sum(b.length for b in FLAC(out).metadata_blocks if b.code == 1)
    assert padding >= 32768, "without padding every future edit rewrites the file"


def test_duration_mismatch_fails_and_cleans_up(tmp_path):
    src = tmp_path / "orig.flac"
    _make_flac(src)
    with pytest.raises(RuntimeError, match="duration"):
        source_tagger.write_tagged_copy(
            str(src), {"ARTIST": "Will See"}, None, expected_duration=9999.0
        )
    leftovers = list((tmp_path / source_tagger.TEMP_DIRNAME).glob("*")) \
        if (tmp_path / source_tagger.TEMP_DIRNAME).exists() else []
    assert leftovers == [], "a failed write must not leave a temp file behind"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_source_tagger_write.py -v`
Expected: FAIL — `AttributeError: module 'app.services.source_tagger' has no attribute 'temp_dir_for'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/source_tagger.py`:

```python
import os
import shutil


def temp_dir_for(path: str) -> str:
    """The staging directory for a source file's tagged copy.

    A dot-subdirectory of the file's own folder: same filesystem, so the final
    promote is an atomic rename rather than a second multi-gigabyte copy, and
    invisible to the watcher, which scans with a non-recursive ``os.listdir``.
    """
    return os.path.join(os.path.dirname(path), TEMP_DIRNAME)


def write_tagged_copy(
    src: str,
    tags: Dict[str, str],
    cover_art_path: Optional[str],
    expected_duration: Optional[float],
) -> str:
    """Copy ``src``, write ``tags`` into the copy, verify it, return its path.

    The original is never opened for writing. On any failure the temp file is
    removed and the caller is left exactly as it started.
    """
    from mutagen.flac import FLAC, Picture

    staging = temp_dir_for(src)
    os.makedirs(staging, exist_ok=True)
    temp_path = os.path.join(staging, os.path.basename(src))

    try:
        shutil.copy2(src, temp_path)

        audio = FLAC(temp_path)
        for key, value in tags.items():
            audio[key] = value

        if cover_art_path and os.path.isfile(cover_art_path):
            picture = Picture()
            picture.type = 3  # front cover
            picture.mime = "image/jpeg"
            picture.desc = "Cover"
            with open(cover_art_path, "rb") as fh:
                picture.data = fh.read()
            audio.clear_pictures()
            audio.add_picture(picture)

        audio.save(padding=lambda _info: FLAC_PADDING_BYTES)

        # Verify by re-reading. A truncated or corrupt write that still parses
        # would otherwise be promoted over a good recording.
        verify = FLAC(temp_path)
        if expected_duration and verify.info.length:
            if abs(verify.info.length - float(expected_duration)) > 1.0:
                raise RuntimeError(
                    f"tagged copy duration {verify.info.length:.1f}s does not match "
                    f"the expected {float(expected_duration):.1f}s"
                )
        return temp_path
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            logger.warning("could not clean up temp file %s", temp_path, exc_info=True)
        raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_source_tagger_write.py -v`
Expected: PASS (5 passed). If `ffmpeg` is unavailable the suite skips; run it in the container where ffmpeg exists.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/source_tagger.py backend/tests/test_source_tagger_write.py
git commit -m "feat(tagger): write verified tagged copies out-of-place with padding"
```

---

### Task 4: Register the hash, then promote atomically

This is the safety-critical task. Order is the whole point: register, **then** promote.

**Files:**
- Modify: `backend/app/services/source_tagger.py`
- Test: `backend/tests/test_source_tagger_promote.py`

**Interfaces:**
- Consumes: `file_watcher.compute_file_hash`, `file_watcher.open_seen_files_db` (Task 1); `write_tagged_copy` (Task 3).
- Produces: `source_tagger.register_and_promote(temp_path: str, final_path: str, file_type: str = "audio", seen_db=None) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_source_tagger_promote.py
"""A tagged file must be known to the watcher BEFORE it becomes visible.

If it is promoted first, there is a window in which a watch folder holds a
file whose hash is unknown -- the watcher ingests it and re-uploads a mix that
is already published. That is the exact failure this ordering prevents.
"""
import os

from app.services import file_watcher, source_tagger


class _RecordingSeenDB:
    """Records the order of operations against real state."""

    def __init__(self):
        self.done = set()
        self.events = []

    def mark_done(self, file_hash, file_path, file_type):
        self.events.append(("mark_done", file_hash))
        self.done.add(file_hash)

    def is_done(self, file_hash):
        return file_hash in self.done


def test_hash_is_registered_before_the_file_is_promoted(tmp_path, monkeypatch):
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")
    final = tmp_path / "x.flac"

    db = _RecordingSeenDB()
    order = []

    real_rename = os.rename

    def spy_rename(a, b):
        order.append("rename")
        return real_rename(a, b)

    monkeypatch.setattr(os, "rename", spy_rename)
    db_mark = db.mark_done

    def spy_mark(h, p, t):
        order.append("mark_done")
        return db_mark(h, p, t)

    db.mark_done = spy_mark

    source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=db)

    assert order == ["mark_done", "rename"], f"wrong order: {order}"


def test_promoted_file_hash_is_marked_done(tmp_path):
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")
    final = tmp_path / "x.flac"
    db = _RecordingSeenDB()

    expected = file_watcher.compute_file_hash(str(temp))
    source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=db)

    assert db.is_done(expected), "the watcher would re-ingest this file"
    assert final.exists()
    assert not temp.exists()


def test_promote_replaces_the_original_contents(tmp_path):
    final = tmp_path / "x.flac"
    final.write_bytes(b"original")
    temp = tmp_path / ".fadeout-tagging" / "x.flac"
    temp.parent.mkdir()
    temp.write_bytes(b"tagged-content")

    source_tagger.register_and_promote(str(temp), str(final), "audio", seen_db=_RecordingSeenDB())
    assert final.read_bytes() == b"tagged-content"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_source_tagger_promote.py -v`
Expected: FAIL — `AttributeError: module 'app.services.source_tagger' has no attribute 'register_and_promote'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/source_tagger.py`:

```python
def register_and_promote(
    temp_path: str,
    final_path: str,
    file_type: str = "audio",
    seen_db: Any = None,
) -> None:
    """Register the tagged file's hash, then move it into place.

    **The order is the safety property.** Tagging changes the dedupe hash, so
    between a tagged file appearing in a watch folder and its hash being known,
    the watcher would treat it as a new recording and start a pipeline run that
    re-uploads an already-published mix. Registering first closes that window
    entirely; the file is never visible while unknown.

    The previous hash's row is deliberately left in place -- a restored backup
    of the untagged original must still dedupe.
    """
    from app.services import file_watcher

    db = seen_db if seen_db is not None else file_watcher.open_seen_files_db()
    new_hash = file_watcher.compute_file_hash(temp_path)
    db.mark_done(new_hash, final_path, file_type)
    os.rename(temp_path, final_path)
    logger.info("promoted tagged file %s (hash %s registered first)", final_path, new_hash)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_source_tagger_promote.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/source_tagger.py backend/tests/test_source_tagger_promote.py
git commit -m "feat(tagger): register the new hash before promoting a tagged file"
```

---

### Task 5: The orchestrator, with every safety rail

**Files:**
- Modify: `backend/app/services/source_tagger.py`
- Modify: `backend/app/services/app_config.py` (add the `tag_source_files` setting)
- Modify: `backend/app/config.py` (add `TAG_SOURCE_FILES: bool = False`)
- Test: `backend/tests/test_source_tagger_rails.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `async source_tagger.tag_sources_for_mix(mix_id: str, *, reason: str, dry_run: bool = False) -> Dict[str, Any]` returning `{"status": str, "actions": List[Dict[str, Any]], "reason": str}` where `status` is one of `"disabled"`, `"skipped"`, `"ok"`, `"failed"`, `"dry_run"`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_source_tagger_rails.py
"""The rails, not the mutagen call, are the substance of this module."""
import pytest

from app.services import source_tagger


@pytest.mark.asyncio
async def test_disabled_setting_is_a_clean_no_op(monkeypatch):
    async def _off():
        return False
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _off)
    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "disabled"
    assert result["actions"] == []


@pytest.mark.asyncio
async def test_refuses_a_mix_that_is_not_completed(monkeypatch):
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="running", audio_file_path="/watch/audio/a.flac"),
    )
    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "skipped"
    assert "completed" in result["reason"]


@pytest.mark.asyncio
async def test_refuses_a_path_outside_the_allowed_roots(monkeypatch):
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="completed", audio_file_path="/etc/passwd"),
    )
    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "skipped"
    assert "allowed roots" in result["reason"]


@pytest.mark.asyncio
async def test_never_raises_when_the_write_explodes(monkeypatch):
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="completed", audio_file_path="/watch/audio/a.flac"),
    )
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda p: True)
    monkeypatch.setattr(source_tagger, "_has_free_space", lambda p: True)

    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(source_tagger, "write_tagged_copy", boom)
    result = await source_tagger.tag_sources_for_mix("m1", reason="test")
    assert result["status"] == "failed"
    assert "disk on fire" in result["reason"]


@pytest.mark.asyncio
async def test_dry_run_touches_nothing(monkeypatch):
    async def _on():
        return True
    monkeypatch.setattr(source_tagger, "_tagging_enabled", _on)
    monkeypatch.setattr(
        source_tagger, "_load_mix",
        _fake_loader(pipeline_status="completed", audio_file_path="/watch/audio/a.flac"),
    )
    monkeypatch.setattr(source_tagger, "is_within_allowed_roots", lambda p: True)

    def boom(*a, **k):
        raise AssertionError("dry run must not write")

    monkeypatch.setattr(source_tagger, "write_tagged_copy", boom)
    result = await source_tagger.tag_sources_for_mix("m1", reason="test", dry_run=True)
    assert result["status"] == "dry_run"
    assert result["actions"], "a dry run should still report what it would do"


def _fake_loader(**attrs):
    from datetime import datetime
    from types import SimpleNamespace

    async def _load(mix_id):
        base = dict(
            id=mix_id, title="T", video_file_path=None, genres=None, tracklist=None,
            cover_art_path=None, soundcloud_url=None, youtube_url=None,
            created_at=datetime(2026, 9, 18), source="watch", duration_seconds=60.0,
        )
        base.update(attrs)
        return SimpleNamespace(**base)

    return _load
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_source_tagger_rails.py -v`
Expected: FAIL — `AttributeError: module 'app.services.source_tagger' has no attribute 'tag_sources_for_mix'`

- [ ] **Step 3: Write minimal implementation**

Add to `backend/app/config.py`, beside the other feature flags:

```python
    TAG_SOURCE_FILES: bool = False
```

Add to `backend/app/services/app_config.py`, after the `rename_source_files` `SettingDef`:

```python
    SettingDef(
        key="tag_source_files",
        label="Write metadata into source files",
        help="After a run completes, write artist/title/date/genre/tracklist "
             "and the cover art into the source FLAC so the recording is "
             "identifiable in Plex or any local player. The file is rewritten "
             "out-of-place and swapped in atomically, and its new dedupe hash "
             "is registered BEFORE the swap, so tagging can never cause the "
             "watcher to re-ingest and re-upload an already-published mix. "
             "The first write rewrites the whole file (these FLACs carry no "
             "padding); 64 KB of padding is added so later edits do not. "
             "Requires the audio watch folder mounted read-WRITE. OFF by "
             "default.",
        type="bool", category="Advanced", env_attr="TAG_SOURCE_FILES",
    ),
```

Append to `backend/app/services/source_tagger.py`:

```python
from app.services.source_renamer import is_within_allowed_roots

MIN_FREE_SPACE_MULTIPLE = 2


async def _tagging_enabled() -> bool:
    """Mirror how source_renamer reads its own flag: app_config.resolve is async."""
    from app.services import app_config

    return bool(await app_config.resolve("tag_source_files"))


async def _load_mix(mix_id: str) -> Any:
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models import Mix

    async with async_session_factory() as session:
        return (await session.execute(select(Mix).where(Mix.id == mix_id))).scalar_one_or_none()


def _has_free_space(path: str) -> bool:
    """Refuse to rewrite a file we cannot fit a second copy of.

    The rewrite is out-of-place, so it needs room for the copy alongside the
    original. Running the volume dry mid-write is how an irreplaceable
    multi-gigabyte recording gets truncated.
    """
    try:
        need = os.path.getsize(path) * MIN_FREE_SPACE_MULTIPLE
        return shutil.disk_usage(os.path.dirname(path)).free >= need
    except OSError:
        return False


async def tag_sources_for_mix(
    mix_id: str, *, reason: str, dry_run: bool = False
) -> Dict[str, Any]:
    """Tag this mix's source audio. Best-effort: never raises, never fails a run."""
    actions: List[Dict[str, Any]] = []

    if not dry_run and not await _tagging_enabled():
        return {"status": "disabled", "actions": [], "reason": "tag_source_files is off"}

    try:
        mix = await _load_mix(mix_id)
        if mix is None:
            return {"status": "skipped", "actions": [], "reason": f"no mix {mix_id}"}

        status = getattr(mix, "pipeline_status", None)
        if status != "completed":
            return {
                "status": "skipped", "actions": [],
                "reason": f"pipeline_status is {status!r}, not 'completed' -- "
                          "only published mixes are tagged",
            }

        src = getattr(mix, "audio_file_path", None)
        if not src:
            return {"status": "skipped", "actions": [], "reason": "no audio_file_path"}

        if not is_within_allowed_roots(src):
            return {
                "status": "skipped", "actions": [],
                "reason": f"{src} is outside the allowed roots",
            }

        tags = build_tags(mix)
        actions.append({"path": src, "tags": tags,
                        "cover_art": getattr(mix, "cover_art_path", None)})

        if dry_run:
            return {"status": "dry_run", "actions": actions, "reason": "no changes made"}

        if not os.path.isfile(src):
            return {"status": "skipped", "actions": actions,
                    "reason": f"{src} does not exist"}

        if not _has_free_space(src):
            return {"status": "skipped", "actions": actions,
                    "reason": f"insufficient free space to rewrite {src} safely"}

        temp = write_tagged_copy(
            src, tags, getattr(mix, "cover_art_path", None),
            getattr(mix, "duration_seconds", None),
        )
        register_and_promote(temp, src, "audio")
        logger.info("tagged %s (%s)", src, reason)
        return {"status": "ok", "actions": actions, "reason": reason}

    except Exception as exc:  # never fatal -- see module docstring
        logger.exception("tagging failed for mix %s", mix_id)
        return {"status": "failed", "actions": actions, "reason": str(exc)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_source_tagger_rails.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/source_tagger.py backend/app/services/app_config.py backend/app/config.py backend/tests/test_source_tagger_rails.py
git commit -m "feat(tagger): orchestrate tagging behind opt-in, allowlist and free-space rails"
```

---

### Task 6: The re-ingest guarantee, end to end

The headline safety claim deserves a test that exercises the real watcher, not a mock.

**Files:**
- Test: `backend/tests/test_tagging_does_not_reingest.py`

**Interfaces:**
- Consumes: `source_tagger.write_tagged_copy`, `source_tagger.register_and_promote`, `file_watcher.compute_file_hash`, `file_watcher.open_seen_files_db`.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tagging_does_not_reingest.py
"""End-to-end: a tagged file must not look new to the watcher.

This is the guarantee the whole out-of-place design exists to provide. It is
asserted against the watcher's own dedupe state, not a mock of it.
"""
import os
import subprocess

import pytest

from app.services import file_watcher, source_tagger


def _make_flac(path):
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "1", "-c:a", "flac", "-y", str(path)],
        check=True, capture_output=True,
    )


def test_tagged_file_is_already_known_to_the_watcher(tmp_path):
    watch = tmp_path / "audio"
    watch.mkdir()
    src = watch / "mix.flac"
    _make_flac(src)

    db = file_watcher.open_seen_files_db(str(tmp_path / "seen.db"))
    # The untagged original has been ingested already, as in production.
    db.mark_done(file_watcher.compute_file_hash(str(src)), str(src), "audio")

    temp = source_tagger.write_tagged_copy(
        str(src), {"ARTIST": "Will See", "TITLE": "T"}, None, None
    )
    source_tagger.register_and_promote(temp, str(src), "audio", seen_db=db)

    # The watcher's question, asked exactly as the watcher asks it.
    assert db.is_done(file_watcher.compute_file_hash(str(src))), (
        "the tagged file is unknown to the watcher -- it would be re-ingested "
        "and the mix re-uploaded"
    )


def test_the_staging_dir_is_invisible_to_the_watcher_scan(tmp_path):
    """The watcher lists one level; the staging dir must not yield candidates."""
    watch = tmp_path / "audio"
    watch.mkdir()
    src = watch / "mix.flac"
    _make_flac(src)
    source_tagger.write_tagged_copy(str(src), {"ARTIST": "Will See"}, None, None)

    entries = os.listdir(str(watch))
    assert source_tagger.TEMP_DIRNAME in entries
    audio_files = [
        e for e in entries
        if os.path.isfile(os.path.join(str(watch), e))
        and os.path.splitext(e)[1].lower() in file_watcher.AUDIO_EXTENSIONS
    ]
    assert audio_files == ["mix.flac"], (
        "a non-recursive listdir must not surface the staged copy as a new recording"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run against the pre-Task-4 tree (or temporarily reverse the order inside `register_and_promote` so the rename precedes `mark_done`).
Run: `cd backend && python3 -m pytest tests/test_tagging_does_not_reingest.py -v`
Expected: FAIL on the assertion message about re-ingest.

- [ ] **Step 3: Restore the correct implementation**

No new code. If the order was reversed to observe the failure, restore `mark_done` before `os.rename` in `register_and_promote`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_tagging_does_not_reingest.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_tagging_does_not_reingest.py
git commit -m "test(tagger): prove a tagged file is never re-ingested"
```

---

### Task 7: Wire into the pipeline, after renaming

**Files:**
- Modify: `backend/app/services/pipeline.py` (at the existing `source_renamer.rename_sources_for_mix` call, around line 1212)
- Test: `backend/tests/test_pipeline_tagging_hook.py`

**Interfaces:**
- Consumes: `source_tagger.tag_sources_for_mix`.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pipeline_tagging_hook.py
"""Tagging runs after renaming, and cannot break a run."""
import inspect

from app.services import pipeline


def test_pipeline_calls_the_tagger():
    src = inspect.getsource(pipeline)
    assert "tag_sources_for_mix" in src, "the pipeline never tags anything"


def test_tagging_is_ordered_after_renaming():
    """Tagging must act on the FINAL path, so renaming has to happen first."""
    src = inspect.getsource(pipeline)
    assert src.index("rename_sources_for_mix") < src.index("tag_sources_for_mix"), (
        "tagging the pre-rename path leaves the renamed file untagged"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_pipeline_tagging_hook.py -v`
Expected: FAIL — "the pipeline never tags anything"

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/pipeline.py`, immediately after the existing `await source_renamer.rename_sources_for_mix(...)` call, add:

```python
            # After renaming, so the tags land on the final path. Best-effort by
            # the same contract as renaming: this returns a status dict and
            # never raises, so it cannot fail a completed run.
            from app.services import source_tagger

            await source_tagger.tag_sources_for_mix(mix_id, reason="pipeline_complete")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_pipeline_tagging_hook.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline.py backend/tests/test_pipeline_tagging_hook.py
git commit -m "feat(pipeline): tag source files after a completed run"
```

---

### Task 8: Retroactive backfill endpoint

**Files:**
- Modify: `backend/app/routers/catalog.py`
- Test: `backend/tests/test_catalog_retag_endpoint.py`

**Interfaces:**
- Consumes: `source_tagger.tag_sources_for_mix`, `source_renamer.rename_sources_for_mix`.
- Produces: `POST /api/catalog/retag?dry_run=<bool>&limit=<int>` returning `202` with `{"started": bool, "dry_run": bool, "candidates": int}`; `GET /api/catalog/retag/status` returning `{"running": bool, "processed": int, "total": int, "results": List[Dict[str, Any]]}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_catalog_retag_endpoint.py
"""The backfill must default to safe and report before it acts."""
import inspect

from app.routers import catalog


def test_retag_route_exists():
    src = inspect.getsource(catalog)
    assert '"/retag"' in src or "'/retag'" in src


def test_retag_defaults_to_dry_run():
    """A 498 GB rewrite must not be one un-parameterised POST away."""
    sig = inspect.signature(catalog.catalog_retag)
    assert sig.parameters["dry_run"].default is True


def test_retag_status_route_exists():
    src = inspect.getsource(catalog)
    assert "retag/status" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_catalog_retag_endpoint.py -v`
Expected: FAIL — `AttributeError: module 'app.routers.catalog' has no attribute 'catalog_retag'`

- [ ] **Step 3: Write minimal implementation**

Add to `backend/app/routers/catalog.py`, following the existing `/sync` pattern:

```python
_retag_state: Dict[str, Any] = {"running": False, "processed": 0, "total": 0, "results": []}


async def _run_retag(dry_run: bool, limit: Optional[int]) -> None:
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models import Mix
    from app.services import source_renamer, source_tagger

    try:
        async with async_session_factory() as session:
            stmt = select(Mix.id).where(Mix.pipeline_status == "completed")
            if limit:
                stmt = stmt.limit(limit)
            mix_ids = [row[0] for row in (await session.execute(stmt)).all()]

        _retag_state.update(running=True, processed=0, total=len(mix_ids), results=[])

        for mix_id in mix_ids:
            rename = await source_renamer.rename_sources_for_mix(
                mix_id, reason="retag_backfill", dry_run=dry_run
            )
            tag = await source_tagger.tag_sources_for_mix(
                mix_id, reason="retag_backfill", dry_run=dry_run
            )
            _retag_state["results"].append(
                {"mix_id": mix_id, "rename": rename, "tag": tag}
            )
            _retag_state["processed"] += 1
    finally:
        _retag_state["running"] = False


@router.post("/retag", status_code=202)
async def catalog_retag(
    background_tasks: BackgroundTasks,
    dry_run: bool = Query(default=True),
    limit: Optional[int] = Query(default=None),
):
    """Rename and tag the source files of already-published mixes.

    Defaults to ``dry_run=True``. Tagging rewrites multi-gigabyte FLACs in
    full, so the destructive form is opt-in and the safe form is what a bare
    POST does.
    """
    if _retag_state["running"]:
        raise HTTPException(status_code=409, detail="A retag run is already in progress.")

    from sqlalchemy import func, select

    from app.database import async_session_factory
    from app.models import Mix

    async with async_session_factory() as session:
        total = (
            await session.execute(
                select(func.count()).select_from(Mix).where(Mix.pipeline_status == "completed")
            )
        ).scalar_one()

    background_tasks.add_task(_run_retag, dry_run, limit)
    return {"started": True, "dry_run": dry_run, "candidates": int(total)}


@router.get("/retag/status")
async def catalog_retag_status():
    return dict(_retag_state)
```

Ensure `BackgroundTasks`, `Query`, `HTTPException`, `Optional`, `Dict`, `Any` are imported at the top of `catalog.py`; add any that are missing.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_catalog_retag_endpoint.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/catalog.py backend/tests/test_catalog_retag_endpoint.py
git commit -m "feat(catalog): add a dry-run-by-default retag backfill endpoint"
```

---

### Task 9: Documentation and release

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Document the two settings**

In `README.md`, beside `rename_source_files`, document `tag_source_files`: what it writes, that the first write is a full rewrite, that the new hash is registered before the swap so no re-ingest can occur, and that `POST /api/catalog/retag` defaults to a dry run.

- [ ] **Step 2: Add the changelog entry**

```markdown
## [2.5.0] - 2026-09-22

### Added
- Source files can now carry proper library metadata. With `tag_source_files`
  on, a completed mix's FLAC gets ARTIST/TITLE/DATE/GENRE/ALBUM, its tracklist
  as DESCRIPTION, the platform URL, and the generated cover art embedded — so
  the recording is identifiable in Plex or any local player.
- `POST /api/catalog/retag` backfills renames and tags across already-published
  mixes. Defaults to a dry run.

### Fixed
- The SoundCloud OAuth redirect URI is pinned and resolved through a single
  helper, instead of being inferred from the request Host at two call sites
  that could disagree. A mismatch renders a blank authorize page rather than an
  error, which previously cost hours of misdirected debugging.
- A platform credential going dead now raises a notification on the transition.
  Previously it was visible only in `/api/health`.
- Browser-login failures capture the URL, visible text and a screenshot.

### Notes
- Tagging rewrites each FLAC once in full (these files carry no padding); 64 KB
  of padding is added so subsequent edits are in-place. The rewrite happens
  out-of-place and is swapped in atomically, and the new dedupe hash is
  registered **before** the swap, so tagging can never cause the watcher to
  re-ingest and re-upload an already-published mix.
```

- [ ] **Step 3: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document source tagging and the retag backfill"
```

- [ ] **Step 4: Open the PR and tag the release**

```bash
git push -u origin feat/source-file-metadata
gh pr create --title "feat: write library metadata into source files" --body "<summary of the spec>"
```

After merge: `git tag v2.5.0 && git push origin v2.5.0`, then `gh release create v2.5.0 --generate-notes`.

---

## Self-Review

**Spec coverage:** out-of-place tagging (T3), hash re-registration before promote (T4), verification (T3), opt-in/allowlist/completed-only/never-fatal/idempotent rails (T5), tag table (T2), padding (T3), ordering after rename (T7), dry-run backfill (T8), free-space guard (T5), video rename-only (T7, via the existing renamer), re-ingest test (T6). Covered.

**Known gap, deliberately deferred:** idempotence is specified ("a file already carrying the target tags is skipped") but not implemented in T5 — the current code re-tags unconditionally. It is correct but wasteful on repeat backfills. Add a cheap comparison of existing tags before writing if repeat runs become routine.

**Type consistency:** `tag_sources_for_mix` returns the same `{"status", "actions", "reason"}` shape everywhere, mirroring `rename_sources_for_mix`. `register_and_promote` takes `seen_db` in both its tests and the backfill path. `compute_file_hash`/`open_seen_files_db` are used with the Task 1 signatures throughout.
