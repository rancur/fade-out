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
import os
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional

from app.services import source_renamer
from app.services.source_renamer import is_within_allowed_roots
from app.services.tracklist_utils import format_timestamp

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
        artist = (entry.get("artist") or "").strip()
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        # Prefer pre-formatted timestamp; fall back to formatting from seconds
        ts = (entry.get("timestamp_formatted") or "").strip()
        if not ts:
            seconds = entry.get("timestamp_seconds")
            if seconds is not None:
                ts = format_timestamp(float(seconds or 0))
        label = f"{artist} - {title}" if artist else title
        line = f"{ts} {label}".strip() if ts else label
        lines.append(line)
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

    genre_value = "; ".join(str(g) for g in (getattr(mix, "genres", None) or []) if g)
    if genre_value:
        tags["GENRE"] = genre_value

    description = _format_tracklist(getattr(mix, "tracklist", None))
    if description:
        tags["DESCRIPTION"] = description

    url = getattr(mix, "soundcloud_url", None) or getattr(mix, "youtube_url", None)
    if url:
        tags["URL"] = url

    return tags


def already_tagged(path: str, tags: Dict[str, str], cover_art_path: Optional[str]) -> bool:
    """True if ``path`` already carries ``tags`` and its embedded cover art
    already matches the CONTENT of ``cover_art_path``, byte for byte. A
    HEADER READ ONLY -- opening a FLAC for reading never rewrites it, and
    this function never calls ``.save()``, never copies, never touches the
    original in any way. Reading ``cover_art_path`` (to compare against) is
    a small local PNG/JPEG, not the multi-gigabyte source, so this stays
    cheap.

    Every key in ``tags`` must be present on the file with an identical
    value (a retitled mix has a different TITLE, so it reports False and
    gets re-tagged -- exactly the case this exists to still catch).

    The cover art check compares actual CONTENT, not just presence: it was
    previously presence-only (embedded picture exists vs. ``cover_art_path``
    exists), which meant a mix whose artwork was regenerated -- same file
    path, different bytes -- kept its STALE embedded art forever, since
    "some picture is embedded" and "a cover art file exists" both stayed
    true. The embedded picture's data is already in memory from the header
    read above (FLAC PICTURE blocks are metadata, not audio payload, so
    this is not an extra disk read of the source); a size check plus a
    SHA-256 digest of both sides is compared instead of a full byte-for-byte
    diff, which is equivalent in practice and cheaper for a large image.

    Any failure to open or parse the file (missing, corrupt, not a FLAC)
    is treated as "not already tagged" -- the safe default, since it falls
    through to the normal tag-and-verify path rather than silently skipping
    a file that might need repair.
    """
    import hashlib

    from mutagen.flac import FLAC

    try:
        audio = FLAC(path)
    except Exception:
        return False

    for key, value in tags.items():
        if audio.get(key) != [value]:
            return False

    wants_art = bool(cover_art_path and os.path.isfile(cover_art_path))
    has_art = bool(audio.pictures)
    if has_art != wants_art:
        return False

    if wants_art and has_art:
        try:
            with open(cover_art_path, "rb") as fh:
                on_disk = fh.read()
        except OSError:
            # Cover art existed a moment ago (``os.path.isfile`` above) but
            # is now unreadable -- treat like any other failure to read the
            # comparison source: not already tagged, fall through to a real
            # (re-)tag rather than silently skipping.
            return False
        embedded = audio.pictures[0].data
        if len(embedded) != len(on_disk):
            return False
        if hashlib.sha256(embedded).digest() != hashlib.sha256(on_disk).digest():
            return False

    return True


def temp_dir_for(path: str) -> str:
    """The staging directory for a source file's tagged copy.

    A dot-subdirectory of the file's own folder: same filesystem, so the final
    promote is an atomic rename rather than a second multi-gigabyte copy, and
    invisible to the watcher, which scans with a non-recursive ``os.listdir``.
    """
    return os.path.join(os.path.dirname(path), TEMP_DIRNAME)


def sweep_staging(
    root: str, older_than_hours: float = 6.0, dry_run: bool = False
) -> Dict[str, Any]:
    """Reclaim orphaned copies left in a ``TEMP_DIRNAME`` staging directory.

    A run that dies between ``write_tagged_copy`` staging a multi-gigabyte
    copy and ``register_and_promote`` moving it into place leaves that copy
    behind. Nothing else reclaims it: the directory is invisible to the
    watcher by design (see the module docstring), so across ~80 files of
    ~498 GB a few failed runs can quietly consume tens of gigabytes, and the
    resulting shortage then surfaces only as a routine "skipped" from
    ``_has_free_space`` -- silent degradation in both directions.

    THIS FUNCTION DELETES FILES, so the guards are the substance of it, not
    the deletion:

    * ``root`` must be a real, existing ``TEMP_DIRNAME`` directory (checked
      by basename, not by content) inside one of
      ``source_renamer.allowed_roots()``. That allowlist already exists
      because an unguarded path-deletion primitive is dangerous; this reuses
      it rather than inventing a second one. Neither check is negotiable --
      a caller passing a normal directory (or one outside the watch roots)
      gets a refusal back, never a wipe.
    * Only entries strictly OLDER than ``older_than_hours`` (by the MORE
      RECENT of ``st_mtime``/``st_ctime``, i.e. whichever timestamp says the
      entry was touched most recently) are touched. ``st_mtime`` alone is
      not trustworthy here: ``write_tagged_copy`` stages via
      ``shutil.copy2``, whose ``copystat`` carries the SOURCE's mtime onto
      the staged copy, so a copy of a months-old archival recording reports
      an age of months the instant it is staged -- ``st_mtime`` alone would
      let this guard delete a copy still being written. ``st_ctime`` is not
      settable by ``copystat`` (or by any unprivileged caller) and always
      reflects when the entry was actually created/last changed on this
      filesystem, so taking the max of the two is the honest staging time
      even if a future caller of ``write_tagged_copy`` forgets to also stamp
      the mtime. A fresh temp file may belong to a run currently in flight;
      deleting one mid-write is the one outcome this must never produce.
    * The directory is scanned non-recursively (mirroring the watcher's own
      non-recursive ``os.listdir``, and how ``write_tagged_copy`` always
      places temp files directly inside ``root``, never in a subdirectory)
      and symlinks are never followed or removed -- an entry that is a
      symlink is left alone and reported as skipped, since a plain file
      written by ``shutil.copy2`` is the only thing this module itself would
      ever have staged there.
    * A failure to stat or remove one entry (permission error, file vanished
      under us, ...) is logged and skipped; it never aborts the sweep.

    Never raises. Returns a dict of:

    * ``refused`` / ``reason``: set when ``root`` fails a guard; when
      ``refused`` is True nothing was touched, including on a real
      basename-matching directory that happened to be outside the allowed
      roots.
    * ``removed``: list of ``{"path", "bytes"}`` for every entry removed (or,
      under ``dry_run``, every entry that WOULD have been removed -- the
      same set, nothing actually deleted).
    * ``removed_count`` / ``reclaimed_bytes``: totals over ``removed``.
    * ``skipped``: list of ``{"path", "reason"}`` for every entry left alone
      (too young, a symlink, or an error acting on it).
    """
    result: Dict[str, Any] = {
        "root": root,
        "dry_run": dry_run,
        "refused": False,
        "reason": None,
        "removed": [],
        "removed_count": 0,
        "reclaimed_bytes": 0,
        "skipped": [],
    }

    if os.path.basename(os.path.normpath(root)) != TEMP_DIRNAME:
        result["refused"] = True
        result["reason"] = (
            f"refusing to sweep {root!r}: basename is not {TEMP_DIRNAME!r}"
        )
        return result

    if not is_within_allowed_roots(root):
        result["refused"] = True
        result["reason"] = f"refusing to sweep {root!r}: outside the allowed roots"
        return result

    if os.path.islink(root) or not os.path.isdir(root):
        # Never created, already cleaned up, or -- out of caution -- a
        # symlink where a real staging directory should be. Either way there
        # is nothing safe to sweep; this is not a refusal.
        return result

    cutoff = time.time() - (older_than_hours * 3600.0)

    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        logger.warning("could not list staging directory %s: %s", root, exc)
        result["skipped"].append({"path": root, "reason": str(exc)})
        return result

    for entry in entries:
        path = entry.path
        try:
            if entry.is_symlink():
                result["skipped"].append({"path": path, "reason": "symlink"})
                continue
            if not entry.is_file(follow_symlinks=False):
                result["skipped"].append(
                    {"path": path, "reason": "not a regular file"}
                )
                continue
            # ``os.stat`` rather than ``entry.stat()`` -- functionally
            # identical for a confirmed regular, non-symlink file, but a
            # plain module-level call rather than a method bound to an
            # opaque ``os.DirEntry``, which is what lets tests substitute a
            # controlled ``st_ctime`` (real ctime cannot be backdated by any
            # public API, so that is the only way to unit test the ctime
            # guard below at all).
            st = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            logger.warning("could not stat staging entry %s: %s", path, exc)
            result["skipped"].append({"path": path, "reason": str(exc)})
            continue

        # See the docstring above: take the MORE RECENT of mtime/ctime, not
        # mtime alone, since ``copystat`` (via ``shutil.copy2``) carries the
        # source's mtime onto a freshly staged copy of a months-old
        # recording.
        age_basis = max(st.st_mtime, st.st_ctime)
        if age_basis > cutoff:
            result["skipped"].append({"path": path, "reason": "younger than threshold"})
            continue

        if dry_run:
            result["removed"].append({"path": path, "bytes": st.st_size})
            continue

        try:
            os.remove(path)
        except OSError as exc:
            logger.warning(
                "could not remove orphaned staging file %s: %s", path, exc
            )
            result["skipped"].append({"path": path, "reason": str(exc)})
            continue

        result["removed"].append({"path": path, "bytes": st.st_size})

    result["removed_count"] = len(result["removed"])
    result["reclaimed_bytes"] = sum(item["bytes"] for item in result["removed"])
    return result


def _audio_payload_offset(path: str) -> int:
    """Byte offset where FLAC audio frames begin, walking the metadata block
    chain the same way a real FLAC decoder would.

    Total file size is not a reliable stand-in for "did the audio payload
    survive the write": once a file already carries the 64 KB padding block
    this module adds, re-tagging writes new metadata INTO that padding, so
    the total file size does not change even though the write is perfectly
    healthy -- a byte-identical re-tag of an already-tagged file is the
    normal, expected case, not corruption. The only size comparison that
    actually means anything is over the audio payload itself: everything
    from this offset to the end of the file.
    """
    with open(path, "rb") as fh:
        magic = fh.read(4)
        if magic != b"fLaC":
            raise RuntimeError(f"{path} is not a FLAC file (bad magic {magic!r})")
        offset = 4
        while True:
            block_header = fh.read(4)
            if len(block_header) < 4:
                raise RuntimeError(f"{path}: truncated metadata block header")
            is_last = bool(block_header[0] & 0x80)
            block_len = int.from_bytes(block_header[1:4], "big")
            fh.seek(block_len, 1)
            offset = fh.tell()
            if is_last:
                break
        return offset


def write_tagged_copy(
    src: str,
    tags: Dict[str, str],
    cover_art_path: Optional[str],
    expected_duration: Optional[float],
) -> str:
    """Copy ``src``, write ``tags`` into the copy, verify it, return its path.

    The original is never opened for writing. On any failure the temp file is
    removed and the caller is left exactly as it started.

    The file is always re-read after writing to prove it still parses as valid
    FLAC, and its size is always compared against the source, which are the
    two checks that do not depend on the caller passing anything -- the size
    check exists because the STREAMINFO duration a parse gives back is a
    header read, not a measurement, and survives truncation unchanged. If
    ``expected_duration`` is provided, the duration is also compared; if
    None, only the parse and size checks are performed.
    """
    from mutagen.flac import FLAC, Picture

    staging = temp_dir_for(src)
    os.makedirs(staging, exist_ok=True)
    temp_path = os.path.join(
        staging, f"{os.getpid()}-{uuid.uuid4().hex[:8]}-{os.path.basename(src)}"
    )

    try:
        shutil.copy2(src, temp_path)

        # ``copy2``'s ``copystat`` carries the SOURCE's mtime onto the copy
        # (that's the whole point of "2" over plain ``copy``), which for
        # these archival recordings is months old. Left alone, a copy of a
        # 200-day-old FLAC reports an age of ~4800 hours the instant it is
        # staged -- ``sweep_staging``'s "never remove a file younger than
        # the threshold" guard would not protect it at all; a 6-hour sweeper
        # could delete this copy while this very function is still writing
        # it. Stamp the copy with the current time right away so its age
        # reflects when IT was staged, not when the original recording was
        # made. Belt and braces with ``sweep_staging`` also aging off
        # ``st_ctime`` (which ``copystat`` cannot set) rather than trusting
        # this alone.
        os.utime(temp_path, None)

        audio = FLAC(temp_path)
        for key, value in tags.items():
            audio[key] = value

        if cover_art_path and os.path.isfile(cover_art_path):
            picture = Picture()
            picture.type = 3  # front cover
            picture.mime = "image/png" if cover_art_path.lower().endswith(".png") else "image/jpeg"
            picture.desc = "Cover"
            with open(cover_art_path, "rb") as fh:
                picture.data = fh.read()
            audio.clear_pictures()
            audio.add_picture(picture)

        audio.save(padding=lambda _info: FLAC_PADDING_BYTES)

        # Always re-read: this proves the file we just wrote still parses as
        # FLAC. It is the one check that does not depend on the caller passing
        # anything, and a corrupt-but-parseable write is exactly what this
        # module exists to stop from being promoted.
        verify = FLAC(temp_path)

        if expected_duration is not None:
            actual = verify.info.length
            if not actual:
                raise RuntimeError(
                    "tagged copy reports no audio duration -- the write is empty or "
                    "unreadable, refusing to treat it as good"
                )
            if abs(actual - float(expected_duration)) > 1.0:
                raise RuntimeError(
                    f"tagged copy duration {actual:.1f}s does not match the expected "
                    f"{float(expected_duration):.1f}s"
                )

        # ``verify.info.length`` is a HEADER READ, not a measurement: it comes
        # from the FLAC STREAMINFO block, which shutil.copy2 carries over
        # verbatim from the original. A short write -- ENOSPC part-way, an
        # I/O error on the NAS, a disk that fills after ``_has_free_space``
        # passed -- produces a file whose header still claims the full
        # original duration while the audio payload itself is truncated. That
        # passes both checks above. Only a size comparison against the source
        # catches it -- but it must be a comparison of the AUDIO PAYLOAD, not
        # of total file size: once a source already carries the 64 KB padding
        # block this module adds, a healthy re-tag writes new metadata INTO
        # that padding and the total file size does not change at all, which
        # made a plain ``temp_size <= src_size`` check a false positive on
        # every re-tag of an already-tagged file. Walking to the start of the
        # audio frames and comparing what follows is the real invariant: the
        # staged copy must carry at least as many audio bytes as the source,
        # however its metadata happens to be sized.
        src_size = os.path.getsize(src)
        temp_size = os.path.getsize(temp_path)
        src_audio_offset = _audio_payload_offset(src)
        temp_audio_offset = _audio_payload_offset(temp_path)
        src_payload = src_size - src_audio_offset
        temp_payload = temp_size - temp_audio_offset
        if temp_payload < src_payload:
            raise RuntimeError(
                f"tagged copy's audio payload ({temp_payload} bytes, after a "
                f"{temp_audio_offset}-byte metadata header) is smaller than the "
                f"source's ({src_payload} bytes, after a {src_audio_offset}-byte "
                "metadata header) -- this means the audio payload was truncated "
                "during the write (e.g. ENOSPC or an I/O error), even though the "
                "header still parses and reports a valid duration; refusing to "
                "promote a short write over an irreplaceable original"
            )

        # Force the tagged copy's data to disk before the caller can promote
        # it over the original with an atomic rename. POSIX gives no
        # ordering guarantee between written data blocks and a later rename,
        # so without this a crash or power loss right after the rename can
        # leave the directory entry pointing at unwritten blocks with the
        # original already gone.
        with open(temp_path, "rb+") as fh:
            fh.flush()
            os.fsync(fh.fileno())

        return temp_path
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            logger.warning("could not clean up temp file %s", temp_path, exc_info=True)
        raise


def _fsync_dir(path: str) -> None:
    """Fsync the directory containing ``path`` so a promote survives a crash.

    A successful ``os.rename`` only guarantees the new directory entry is
    visible; POSIX gives no ordering guarantee between that and the entry
    actually reaching disk, so without this a crash or power loss right
    after the rename can lose the rename on some filesystems even though the
    renamed file's own data was already fsynced. Not every platform supports
    fsyncing a directory, so a refusal here degrades to a logged warning
    rather than failing an otherwise-successful promote.
    """
    directory = os.path.dirname(path) or "."
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        logger.warning(
            "could not fsync directory %s after promoting %s (platform may not "
            "support directory fsync)", directory, path, exc_info=True,
        )


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

    Any failure in this function -- a raising ``compute_file_hash`` (an I/O
    error reading the multi-gigabyte temp file), a raising ``mark_done``
    (``sqlite3.OperationalError: database is locked`` is a live failure mode
    while the running watcher holds its own connection to the same
    ``seen_files.db``), or a raising ``os.rename`` -- removes the temp file
    before re-raising. The staging dir is hidden from the watcher, so an
    orphan left behind here is invisible debris that nothing else will ever
    reclaim.
    """
    from app.services import file_watcher

    db = seen_db if seen_db is not None else file_watcher.open_seen_files_db()
    opened_here = seen_db is None
    try:
        try:
            new_hash = file_watcher.compute_file_hash(temp_path)
            db.mark_done(new_hash, final_path, file_type)
            os.rename(temp_path, final_path)
        except Exception:
            # The mark_done row above (if it already landed) is deliberately
            # NOT rolled back: it is keyed on a hash no file now has, so it
            # is inert, and a restored backup of the untagged original still
            # dedupes on its own (different) hash.
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                logger.warning("could not clean up temp file %s", temp_path, exc_info=True)
            raise

        _fsync_dir(final_path)
        logger.info(
            "promoted tagged file %s (hash %s registered first)", final_path, new_hash
        )
    finally:
        if opened_here:
            db.close()


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

    try:
        # Evaluated unconditionally -- including during a dry run. Both
        # tag_source_files and rename_source_files default to OFF, so a dry
        # run that never looked at the flag could report "would tag all N
        # mixes", green-light a real run, and have every mix come back
        # "disabled" with nothing done: a full pre-flight report and a real
        # run that silently does nothing look identical unless the dry run
        # checks the same flag the real run gates on.
        enabled = await _tagging_enabled()
        if not dry_run and not enabled:
            return {"status": "disabled", "actions": [], "reason": "tag_source_files is off"}

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
        cover_art_path = getattr(mix, "cover_art_path", None)
        exists = os.path.isfile(src)
        # Evaluated whenever the source exists -- including during a dry
        # run, for the same reason ``enabled`` is evaluated unconditionally
        # above: a dry run that skipped this check would report "would tag"
        # for a file a real run would then skip, overstating the work in
        # front of a 498 GB operation.
        skip_already_tagged = already_tagged(src, tags, cover_art_path) if exists else False
        has_space = _has_free_space(src) if exists else False
        actions.append({
            "path": src,
            "tags": tags,
            "cover_art": cover_art_path,
            "source_exists": exists,
            "sufficient_space": has_space,
            "enabled": enabled,
            "already_tagged": skip_already_tagged,
        })

        if dry_run:
            if not enabled:
                return {"status": "dry_run", "actions": actions,
                        "reason": "tag_source_files is off -- a real run would do nothing"}
            if not exists:
                return {"status": "dry_run", "actions": actions,
                        "reason": f"source missing: {src} -- would be skipped"}
            if skip_already_tagged:
                return {"status": "dry_run", "actions": actions,
                        "reason": "already tagged -- would be skipped"}
            if not has_space:
                return {"status": "dry_run", "actions": actions,
                        "reason": f"insufficient free space for {src} -- would be skipped"}
            return {"status": "dry_run", "actions": actions, "reason": "no changes made"}

        if not exists:
            return {"status": "skipped", "actions": actions,
                    "reason": f"{src} does not exist"}

        if skip_already_tagged:
            return {"status": "skipped", "actions": actions, "reason": "already tagged"}

        if not has_space:
            return {"status": "skipped", "actions": actions,
                    "reason": f"insufficient free space to rewrite {src} safely"}

        temp = write_tagged_copy(
            src, tags, cover_art_path,
            getattr(mix, "duration_seconds", None),
        )
        register_and_promote(temp, src, "audio")
        logger.info("tagged %s (%s)", src, reason)
        return {"status": "ok", "actions": actions, "reason": reason}

    except Exception as exc:  # never fatal -- see module docstring
        logger.exception("tagging failed for mix %s", mix_id)
        return {"status": "failed", "actions": actions, "reason": str(exc)}
