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
import uuid
from typing import Any, Dict, List, Optional

from app.services import source_renamer
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
        # passes both checks above. Only a raw size comparison against the
        # source catches it: the tagged copy is the same audio plus 64 KB of
        # padding plus any cover art minus a small old tag block, so it must
        # always come out larger than the source. Anything smaller than the
        # source is definitionally a truncated write.
        src_size = os.path.getsize(src)
        temp_size = os.path.getsize(temp_path)
        if temp_size <= src_size:
            raise RuntimeError(
                f"tagged copy ({temp_size} bytes) is not larger than the source "
                f"({src_size} bytes) -- this means the audio payload was "
                "truncated during the write (e.g. ENOSPC or an I/O error), even "
                "though the header still parses and reports a valid duration; "
                "refusing to promote a short write over an irreplaceable original"
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
    opened_here = seen_db is None
    try:
        new_hash = file_watcher.compute_file_hash(temp_path)
        db.mark_done(new_hash, final_path, file_type)
        try:
            os.rename(temp_path, final_path)
        except Exception:
            # The staging dir is hidden from the watcher, so an orphan here is
            # invisible debris that nothing else will ever reclaim. The
            # mark_done row above is deliberately NOT rolled back: it is keyed
            # on a hash no file now has, so it is inert.
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                logger.warning("could not clean up temp file %s", temp_path, exc_info=True)
            raise
        logger.info(
            "promoted tagged file %s (hash %s registered first)", final_path, new_hash
        )
    finally:
        if opened_here:
            db.close()


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

    try:
        if not dry_run and not await _tagging_enabled():
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
        exists = os.path.isfile(src)
        has_space = _has_free_space(src) if exists else False
        actions.append({
            "path": src,
            "tags": tags,
            "cover_art": getattr(mix, "cover_art_path", None),
            "source_exists": exists,
            "sufficient_space": has_space,
        })

        if dry_run:
            if not exists:
                return {"status": "dry_run", "actions": actions,
                        "reason": f"source missing: {src} -- would be skipped"}
            if not has_space:
                return {"status": "dry_run", "actions": actions,
                        "reason": f"insufficient free space for {src} -- would be skipped"}
            return {"status": "dry_run", "actions": actions, "reason": "no changes made"}

        if not exists:
            return {"status": "skipped", "actions": actions,
                    "reason": f"{src} does not exist"}

        if not has_space:
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
