"""Pre-publish duplicate guard: the same recording must never be posted twice.

Incident 2026-11-04: the original OBS-named video files of two mixes that had
been published months earlier reappeared in the watch directory. Ingest paired
each one with the (already renamed) FLAC of the original mix, created a brand
new mix, and the pipeline published both again under fresh AI titles, to
YouTube and SoundCloud. Nothing compared the new mix with what was already
live; the uniqueness registry only guarantees the *titles* differ, which is
exactly what made the copies look new.

This module answers one question before anything uploads: is this mix the same
recording as a mix that is already published or already queued? It compares
evidence, never titles alone:

* **same source file** — the new mix points at a file path another mix uses;
* **same video bytes** — MD5 of the first 10 MB plus the byte size (the same
  cheap key the Shorts pipeline dedupes on);
* **same audio** — MD5 of a short window of *decoded* PCM. Decoding makes it
  survive tag rewrites (``source_tagger`` writes metadata into the FLAC, which
  changes the file's bytes but not its audio) and renames;
* **same title stem and duration** — only for catalog rows that carry no file
  evidence at all (imported platform uploads), the leading title segment before
  ``:`` or ``|`` must match exactly and the durations must agree within
  :data:`TITLE_STEM_DURATION_TOLERANCE`.

Every rule also requires the durations to agree within
:data:`DURATION_TOLERANCE_SECONDS` when both are known. Duration on its own is
never a match: two different two-hour mixes can easily land within a few
seconds of each other.

A match marks the mix ``duplicate`` with ``metadata_json.duplicate_of``. That
is a quiet, terminal, non-error state: nothing is uploaded, nobody is paged and
the stuck-mix watchdog ignores it. ``metadata_json.allow_duplicate = true`` on
a mix is the deliberate override for a re-upload someone actually wants.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.database import async_session_factory
from app.models import Mix, PipelineStep

logger = logging.getLogger(__name__)

STATUS_DUPLICATE = "duplicate"
EVENT = "duplicate_skipped"

DURATION_TOLERANCE_SECONDS = 5.0
TITLE_STEM_DURATION_TOLERANCE = 2.0

HEAD_BYTES = 10 * 1024 * 1024
PCM_WINDOW_SECONDS = 30
PCM_OFFSET_SECONDS = 300
PCM_TIMEOUT_SECONDS = 120

# A mix in one of these states is published or on its way to being published,
# so a second copy of it must not go out.
_QUEUED_STATUSES = {
    "pending", "running", "analyzing", "generating", "uploading_soundcloud",
    "uploading_youtube", "verifying", "draft_review", "waiting", "interrupted",
    "partial", "completed", "imported",
}

_STEP_ROWS_TO_SKIP = ("pending", "blocked", "failed", "interrupted", "waiting")


@dataclass
class Fingerprint:
    duration: Optional[float] = None
    audio_path: Optional[str] = None
    video_path: Optional[str] = None
    audio_head: Optional[str] = None
    video_head: Optional[str] = None
    audio_pcm: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "duration": self.duration,
            "audio_head": self.audio_head,
            "video_head": self.video_head,
            "audio_pcm": self.audio_pcm,
        }

    @property
    def has_file_evidence(self) -> bool:
        return bool(
            self.audio_path or self.video_path or self.audio_head
            or self.video_head or self.audio_pcm
        )


@dataclass
class DuplicateMatch:
    duplicate_of: str
    title: str
    reasons: List[str] = field(default_factory=list)
    published: bool = False
    created_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "duplicate_of": self.duplicate_of,
            "title": self.title,
            "reasons": list(self.reasons),
            "published": self.published,
        }


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def _known_duration(value: Any) -> Optional[float]:
    try:
        d = float(value)
    except (TypeError, ValueError):
        return None
    # A failed analyze leaves 0.0 behind; that is "unknown", not "zero long".
    return d if d > 1.0 else None


def file_head_hash(path: Optional[str]) -> Optional[str]:
    """``md5(first 10 MB):size`` for an existing file, else None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        digest = hashlib.md5()
        with open(path, "rb") as fh:
            digest.update(fh.read(HEAD_BYTES))
        return f"{digest.hexdigest()}:{os.path.getsize(path)}"
    except OSError:
        logger.warning("Could not read %s for duplicate check", path, exc_info=True)
        return None


async def audio_pcm_hash(
    path: Optional[str], duration: Optional[float] = None
) -> Optional[str]:
    """MD5 of a decoded mono 8 kHz PCM window of ``path``, or None.

    The window starts at :data:`PCM_OFFSET_SECONDS` (or a third of the way in
    for a short file), so silence at the head of a recording does not make
    every mix look alike.
    """
    if not path or not os.path.isfile(path) or not shutil.which("ffmpeg"):
        return None
    offset = PCM_OFFSET_SECONDS
    if duration and duration < offset * 2:
        offset = max(0, int(duration / 3))
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-ss", str(offset), "-t",
        str(PCM_WINDOW_SECONDS), "-i", path, "-vn", "-ac", "1", "-ar", "8000",
        "-f", "s16le", "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), PCM_TIMEOUT_SECONDS)
    except (OSError, asyncio.TimeoutError):
        logger.warning("Audio hash failed for %s", path, exc_info=True)
        return None
    if proc.returncode != 0 or not out:
        return None
    return hashlib.md5(out).hexdigest()


async def fingerprint_mix(mix: Mix) -> Fingerprint:
    """Fingerprint a mix from its files, falling back to a stored fingerprint.

    The stored copy matters: once a published mix's source files are renamed
    or moved, the stored hashes are the only evidence left of what went out.
    """
    stored = ((mix.metadata_json or {}).get("fingerprint") or {}) if isinstance(
        mix.metadata_json, dict
    ) else {}
    duration = _known_duration(mix.duration_seconds) or _known_duration(
        stored.get("duration")
    )
    fp = Fingerprint(
        duration=duration,
        audio_path=mix.audio_file_path or None,
        video_path=mix.video_file_path or None,
    )
    fp.video_head = await asyncio.to_thread(file_head_hash, mix.video_file_path)
    fp.audio_head = await asyncio.to_thread(file_head_hash, mix.audio_file_path)
    fp.audio_pcm = await audio_pcm_hash(mix.audio_file_path, duration)
    fp.video_head = fp.video_head or stored.get("video_head")
    fp.audio_head = fp.audio_head or stored.get("audio_head")
    fp.audio_pcm = fp.audio_pcm or stored.get("audio_pcm")
    return fp


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _title_stem(title: Optional[str]) -> str:
    stem = re.split(r"[:|]", title or "", maxsplit=1)[0]
    stem = re.sub(r"[^a-z0-9 ]+", " ", stem.lower())
    return re.sub(r"\s+", " ", stem).strip()


def _is_published(mix: Mix) -> bool:
    return bool(
        mix.youtube_url or mix.soundcloud_url or mix.mixcloud_url
        or mix.youtube_video_id or mix.soundcloud_track_id
    )


def _platform_keys(mix: Mix) -> set:
    keys = {
        ("yt", mix.youtube_video_id), ("sc", mix.soundcloud_track_id),
        ("url", mix.youtube_url), ("url", mix.soundcloud_url),
        ("url", mix.mixcloud_url),
    }
    return {k for k in keys if k[1]}


def _is_mirror(mix: Mix, other: Mix) -> bool:
    """``other`` is a catalog row for the very upload ``mix`` made.

    Catalog import creates rows for platform uploads, including the ones the
    pipeline itself made. Those rows carry the same audio, so without this a
    retry of a partly published mix would match its own catalog record.
    """
    return bool(_platform_keys(mix) & _platform_keys(other))


def _is_candidate(mix: Mix) -> bool:
    if (mix.pipeline_status or "") == STATUS_DUPLICATE:
        return False
    return _is_published(mix) or (mix.pipeline_status or "pending") in _QUEUED_STATUSES


def compare(
    new: Fingerprint, new_title: Optional[str], other: Fingerprint,
    other_title: Optional[str],
) -> List[str]:
    """Reasons ``new`` and ``other`` are the same recording; empty if not."""
    durations_known = new.duration is not None and other.duration is not None
    if durations_known and abs(new.duration - other.duration) > DURATION_TOLERANCE_SECONDS:
        return []

    reasons: List[str] = []
    if new.audio_path and new.audio_path == other.audio_path:
        reasons.append("same audio file")
    if new.video_path and new.video_path == other.video_path:
        reasons.append("same video file")
    if new.video_head and new.video_head == other.video_head:
        reasons.append("identical video bytes")
    if new.audio_head and new.audio_head == other.audio_head:
        reasons.append("identical audio bytes")
    if new.audio_pcm and new.audio_pcm == other.audio_pcm:
        reasons.append("identical decoded audio")

    if (
        not reasons
        and not other.has_file_evidence
        and durations_known
        and abs(new.duration - other.duration) <= TITLE_STEM_DURATION_TOLERANCE
    ):
        stem = _title_stem(new_title)
        if stem and stem == _title_stem(other_title):
            reasons.append("same title and duration as a catalog upload")
    return reasons


async def find_duplicate(mix_id: str) -> Optional[DuplicateMatch]:
    """The published-or-queued mix that ``mix_id`` duplicates, or None.

    Also stores the mix's own fingerprint so later mixes can be compared with
    it after its files are renamed or removed.
    """
    async with async_session_factory() as session:
        mix = await session.get(Mix, mix_id)
        if mix is None:
            return None
        meta = mix.metadata_json if isinstance(mix.metadata_json, dict) else {}
        if meta.get("allow_duplicate"):
            return None
        others = (
            await session.execute(select(Mix).where(Mix.id != mix_id))
        ).scalars().all()

        fp = await fingerprint_mix(mix)
        meta = dict(meta)
        meta["fingerprint"] = fp.to_dict()
        mix.metadata_json = meta
        await session.commit()

        matches: List[DuplicateMatch] = []
        for other in others:
            if not _is_candidate(other) or _is_mirror(mix, other):
                continue
            # Cheap prefilter: decoding audio for a mix of a different length
            # can only ever produce a non-match.
            other_duration = _known_duration(other.duration_seconds)
            if (
                fp.duration is not None and other_duration is not None
                and abs(fp.duration - other_duration) > DURATION_TOLERANCE_SECONDS
            ):
                continue
            other_fp = await fingerprint_mix(other)
            reasons = compare(fp, mix.title, other_fp, other.title)
            if reasons:
                matches.append(DuplicateMatch(
                    duplicate_of=other.id,
                    title=other.title,
                    reasons=reasons,
                    published=_is_published(other),
                    created_at=other.created_at,
                ))

    if not matches:
        return None
    # Prefer a mix that is already live, then the oldest.
    def key(m: DuplicateMatch):
        created = m.created_at or datetime.min
        return (not m.published, created.replace(tzinfo=None))

    return sorted(matches, key=key)[0]


async def mark_duplicate(mix_id: str, match: DuplicateMatch, *, stage: str) -> None:
    """Park ``mix_id`` as a duplicate. Quiet by design: info log, no alert."""
    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        mix = await session.get(Mix, mix_id)
        if mix is None:
            return
        meta = dict(mix.metadata_json or {}) if isinstance(mix.metadata_json, dict) else {}
        meta["duplicate_of"] = match.duplicate_of
        meta["duplicate_evidence"] = {
            **match.to_dict(), "stage": stage, "marked_at": now.isoformat(),
        }
        mix.metadata_json = meta
        mix.pipeline_status = STATUS_DUPLICATE
        mix.pipeline_error = None
        rows = (
            await session.execute(
                select(PipelineStep).where(PipelineStep.mix_id == mix_id)
            )
        ).scalars().all()
        for row in rows:
            if row.status in _STEP_ROWS_TO_SKIP:
                row.status = "skipped"
                row.error = f"duplicate of {match.duplicate_of}"
                row.completed_at = now
        await session.commit()

    try:
        from app.services import activity_log

        await activity_log.info(
            EVENT,
            f"Skipped: same recording as \"{match.title}\" "
            f"({', '.join(match.reasons)})",
            mix_id=mix_id,
            stage=stage,
            context=match.to_dict(),
        )
    except Exception:  # pragma: no cover - logging must not block the skip
        logger.exception("Could not log duplicate skip for %s", mix_id)
    logger.info("Mix %s is a duplicate of %s: %s", mix_id, match.duplicate_of, match.reasons)
