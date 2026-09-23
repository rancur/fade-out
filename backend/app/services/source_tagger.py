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

    If ``expected_duration`` is None, verification is skipped; production always
    passes a real duration to catch corruption. Tests may pass None to accept
    any duration.
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

        # Verify by re-reading. A truncated or corrupt write that still parses
        # would otherwise be promoted over a good recording.
        if expected_duration is not None:
            verify = FLAC(temp_path)
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
        return temp_path
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            logger.warning("could not clean up temp file %s", temp_path, exc_info=True)
        raise
