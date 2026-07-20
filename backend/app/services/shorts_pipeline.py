"""YouTube Shorts pipeline: watch vertical clips, track-ID, optimize, upload.

Will records vertical clips via OBS Backtrack into a Google-Drive-synced
folder mounted READ-ONLY at ``SHORTS_WATCH_PATH`` (files named
``Backtrack YYYY-MM-DD HH-MM-SS.mp4`` with a same-recording ``.mkv`` sibling
that must be ignored). Every new stable ``.mp4``:

1. **Detect** — watchdog + stability polling (same machinery as the mix
   watcher); dedupe by first-10MB MD5 against the ``shorts`` table.
2. **Analyze** — ffprobe for duration/resolution. Must be vertical (h > w)
   and <= ``SHORTS_MAX_DURATION_SECONDS`` (Shorts accept up to 3 minutes
   since 2024-10-15; longer uploads become regular videos). Then Shazam the
   middle of the clip (mirrors ``audio_analyzer``'s segment approach) for a
   track credit — may legitimately come back empty on originals/blends.
3. **Metadata** — LLM generates a hook-first title, a 2-3 line description
   with hashtags, and a tag list, following the Shorts-algorithm playbook
   encoded in ``SHORTS_METADATA_PROMPT`` below.
4. **Upload** — ``videos.insert`` (1600 quota units) through the shared
   ``catalog_yt_quota`` daily ledger, capped at ``SHORTS_DAILY_UPLOAD_CAP``
   per day so the backlog drains a few clips per day, oldest first. When the
   cap/budget is hit the short parks as ``queued`` and the periodic drain
   pass picks it up on a later day.

Backlog import: ``scan()`` ingests every not-yet-seen file in the folder.
Because some of the existing clips are already on the channel, the scan runs
a ONE-TO-ONE dedupe pass against catalog mixes with duration <= 185s (synced
YouTube uploads): pairs are scored ``duration_diff + 0.5 * day_diff`` (both
gates required — see the DEDUPE_* constants) and assigned greedily, each
channel video claiming at most one clip and vice versa. A matched clip
becomes ``skipped`` with the existing ``youtube_video_id`` linked (the user
can un-skip to force an upload); a re-scan re-derives the links, so a
better-scoring clip can take a video id from an earlier weaker match.

---------------------------------------------------------------------------
YouTube Shorts algorithm playbook (researched 2026-07) — encoded in the
prompt + post-processing below. Sources:

* riverside.com/blog/youtube-shorts-algorithm — Shorts are first shown to a
  small test audience and ranked on swipe-away rate, watch-through rate,
  engagement and replay rate; the hook in the first seconds decides whether
  distribution expands. Titles must create immediate clarity or curiosity.
* socialync.io/blog/youtube-shorts-algorithm-2026 — retention thresholds:
  ~65% watch-through for sub-30s clips, ~50% for 30-60s; the 25-40s range is
  the algorithm's sweet spot; publish 3-5 Shorts weekly (our default cap of
  3/day drains the backlog well above that floor without flooding).
* hashtagtools.io/blog/youtube-shorts-hashtags-title-vs-description-2026 —
  ~3 relevant hashtags is the 2026 working range; more than 15 hashtags on a
  video makes YouTube ignore ALL of them; #shorts is no longer required for
  classification (that's dimensions + duration) but still links the video
  into the Shorts collection, so keep it.
* howmanywords.app/blog/youtube-character-limits — titles allow 100 chars but
  the Shorts feed/shelf truncates around 40: front-load the hook.
* miraflow.ai/blog/youtube-shorts-best-practices-2026-complete-guide — title
  keywords now matter for the dedicated Shorts search carousel (Jan 2026
  search redesign), so include searchable track/genre terms, not just vibes.
* shortsync.app/resources/youtube-shorts-upload-requirements-2026 — 9:16
  vertical (1080x1920) up to 3 minutes; vertical or square qualifies.
---------------------------------------------------------------------------
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shazamio import Shazam
from sqlalchemy import func as sa_func, select
from watchdog.observers import Observer

from app.config import settings
from app.database import async_session_factory
from app.models import AIUsage, AppSettings, BrandSettings, Mix, Short
from app.services import activity_log
from app.services.description_generator import DescriptionGenerator, price_for_model
from app.services.file_watcher import (
    _compute_file_hash,
    _StabilityTracker,
    _WatchHandler,
)
from app.services.youtube_uploader import YouTubeUploader

logger = logging.getLogger(__name__)

# videos.insert costs 1600 units (YouTube Data API quota table) regardless of
# file size. Tracked in the SAME daily ledger as catalog writes so shorts,
# catalog apply, and playlist organization never collectively blow the budget.
YT_SHORT_UPLOAD_COST = 1600
QUOTA_KEY = "catalog_yt_quota"  # shared with app.services.catalog_apply

LAST_SCAN_KEY = "shorts_last_scan"

# Seconds of audio Shazam gets from the middle of the clip (mirrors the mix
# analyzer's SEGMENT_DURATION; the middle avoids intro stingers/outros).
SHAZAM_CLIP_SECONDS = 12
SHAZAM_TRANSIENT_RETRIES = 2
SHAZAM_RETRY_BACKOFF = 1.5

# Catalog dedupe: a backlog clip counts as "already on the channel" only when
# a catalog mix with a YouTube id matches BOTH gates:
#   * duration within DEDUPE_DURATION_TOLERANCE (absorbs container/rounding
#     differences between the local mp4 and what YouTube reports), AND
#   * |published_at - recording date| within DEDUPE_MAX_DAY_DIFF days (both
#     dates are required — a clip with no parseable recording date or a mix
#     with no published date never dedupe-matches).
# Pair quality is scored ``duration_diff + DEDUPE_DAY_WEIGHT * day_diff``
# (lower = better) and assignment is strictly ONE-TO-ONE: each channel video
# claims at most one clip and vice versa, best score first. The per-clip
# duration±date test alone once matched 52 of 89 backlog clips onto just 3
# distinct video ids (dozens of similar-length Backtrack clips all "matched"
# the same few uploads) — hence the one-to-one greedy assignment.
DEDUPE_DURATION_TOLERANCE = 2.5
DEDUPE_MAX_DAY_DIFF = 5.0
DEDUPE_DAY_WEIGHT = 0.5
# Only mixes at/below this duration are Short-shaped candidates (185 gives 5s
# of slack over the 180s Shorts ceiling for YouTube's rounded durations).
DEDUPE_MAX_MIX_DURATION = 185.0

MIN_SHORT_FILE_BYTES = 1024 * 1024  # 1 MB floor — same rationale as the mix watcher

# "Backtrack 2026-05-14 21-03-22.mp4" -> recording datetime
_FILENAME_DATE_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})"
)

VALID_STATUSES = {
    "detected", "analyzing", "ready", "queued",
    "uploading", "uploaded", "failed", "skipped",
}


# ---------------------------------------------------------------------------
# Metadata prompt (see the algorithm playbook + sources in the module docstring)
# ---------------------------------------------------------------------------

SHORTS_METADATA_PROMPT = """\
You write YouTube Shorts metadata for DJ "{brand_name}" — vertical clips of
live DJ sets (desert energy, four decks, genre-fluid electronic music).

Return STRICT JSON only, no code fences, exactly this shape:
{{"title": "...", "description": "...", "tags": ["...", "..."]}}

TITLE RULES (Shorts algorithm):
- Max 80 characters. The Shorts feed truncates titles around 40 characters,
  so the HOOK must live in the first 40 characters — open with curiosity or
  energy, never with the brand name.
- Shorts now rank in YouTube search (dedicated Shorts carousel), so include
  searchable keywords: the track credit "{track_credit_hint}" when one is
  given, and a genre word (house, techno, DnB, trance...).
- Include exactly one fitting emoji.
- No ALL-CAPS spam, no clickbait lies, no hashtags in the title.

DESCRIPTION RULES:
- 2-3 short lines: what's happening in the clip + the track credit line
  ("Track: Artist - Title") when a track is identified.
- End with ONE hashtag line: #shorts #dj #<genre> plus 2-3 extra
  discoverability hashtags (5-6 hashtags total — never more; past 15
  YouTube ignores them all). #shorts must be included.
- No links or URLs (brand links are appended automatically). No sign-off.

TAGS: 8-12 plain tags (no # prefix): genre keywords, "dj set", "dj mix
shorts", track artist/title when identified, brand name.

UNIQUENESS: recent Shorts titles already used — do NOT repeat their
structure or wording:
{recent_titles}

CLIP DATA:
- Recorded: {recorded_at}
- Duration: {duration_seconds:.0f}s vertical clip from a live stream
- Identified track: {track_credit}

Return ONLY the JSON object.\
"""


def parse_recording_date(file_path: str) -> Optional[datetime]:
    """Parse the recording datetime out of a Backtrack filename, or None."""
    m = _FILENAME_DATE_RE.search(os.path.basename(file_path))
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def _mix_published_at(mix: Mix) -> Optional[datetime]:
    """Naive-UTC YouTube published_at from a mix's catalog metadata, or None."""
    raw = (
        ((mix.metadata_json or {}).get("catalog") or {})
        .get("youtube", {})
        .get("published_at")
    )
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None


def _match_score(
    clip_duration: float, recorded_at: Optional[datetime], mix: Mix
) -> Optional[float]:
    """Score a (clip, channel-video) dedupe pair; None = not a valid pair.

    Both gates are required: duration diff <= DEDUPE_DURATION_TOLERANCE and
    |published - recorded| <= DEDUPE_MAX_DAY_DIFF (so both dates must exist).
    Lower score = better match: duration_diff + DEDUPE_DAY_WEIGHT * day_diff.
    """
    if mix.duration_seconds is None:
        return None
    duration_diff = abs(mix.duration_seconds - clip_duration)
    if duration_diff > DEDUPE_DURATION_TOLERANCE:
        return None
    if recorded_at is None:
        return None
    published = _mix_published_at(mix)
    if published is None:
        return None
    day_diff = abs((published - recorded_at).total_seconds()) / 86400.0
    if day_diff > DEDUPE_MAX_DAY_DIFF:
        return None
    return duration_diff + DEDUPE_DAY_WEIGHT * day_diff


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe helpers (monkeypatched in tests)
# ---------------------------------------------------------------------------


async def ffprobe_clip(file_path: str) -> Dict[str, Any]:
    """Return {duration, width, height} for a video file via ffprobe.

    ffmpeg/ffprobe is present in the container image (used elsewhere for
    transcoding). Raises RuntimeError when ffprobe fails.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {file_path}: {err.decode(errors='replace')[:500]}"
        )
    data = json.loads(out.decode(errors="replace") or "{}")
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    width = height = 0
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            break
    return {"duration": duration, "width": width, "height": height}


async def _extract_middle_audio(file_path: str, duration: float) -> Optional[str]:
    """Extract SHAZAM_CLIP_SECONDS of mono WAV from the clip's middle.

    Returns a temp-file path (caller unlinks) or None on failure. The source
    is only ever read — the watch mount is read-only by design.
    """
    offset = max(0.0, duration / 2 - SHAZAM_CLIP_SECONDS / 2)
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{offset:.2f}", "-t", str(SHAZAM_CLIP_SECONDS),
        "-i", file_path, "-vn", "-ac", "1", "-ar", "44100", wav_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.debug(
            "ffmpeg audio extract failed for %s: %s",
            file_path, err.decode(errors="replace")[:300],
        )
        try:
            os.unlink(wav_path)
        except OSError:
            pass
        return None
    return wav_path


async def identify_track(
    file_path: str, duration: float
) -> Tuple[Optional[str], Optional[str]]:
    """Shazam the middle of the clip. Returns (artist, title) or (None, None).

    Mirrors ``audio_analyzer._shazam_wav``: a clean no-match returns
    immediately; transient errors (rate limit, network) retry with backoff.
    An unidentified clip is a legitimate outcome (originals, deep blends).
    """
    wav_path = await _extract_middle_audio(file_path, duration)
    if wav_path is None:
        return None, None
    try:
        result = None
        for attempt in range(SHAZAM_TRANSIENT_RETRIES + 1):
            try:
                result = await Shazam().recognize(wav_path)
                break
            except Exception as exc:
                if attempt < SHAZAM_TRANSIENT_RETRIES:
                    await asyncio.sleep(SHAZAM_RETRY_BACKOFF * (attempt + 1))
                    continue
                logger.debug("Shazam failed for %s after retries: %s", file_path, exc)
                return None, None
        matches = (result or {}).get("matches") or []
        track_info = (result or {}).get("track") or {}
        if not matches or not track_info:
            return None, None
        return track_info.get("subtitle"), track_info.get("title")
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Factory hooks (monkeypatchable in tests)
# ---------------------------------------------------------------------------


def get_youtube_uploader(db_settings_json: Optional[Dict[str, Any]]) -> YouTubeUploader:
    return YouTubeUploader(db_settings_json)


def get_description_generator(
    db_settings_json: Optional[Dict[str, Any]],
) -> DescriptionGenerator:
    return DescriptionGenerator(db_settings_json)


# ---------------------------------------------------------------------------
# Metadata generation + enforcement
# ---------------------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _enforce_title(title: str) -> str:
    """Hard 80-char ceiling (YouTube allows 100; the prompt asks for <=80 so
    the visible ~40-char feed window plus keywords never risk truncated
    mid-word garbage)."""
    title = (title or "").strip().strip('"').strip("'")
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    return title


def _enforce_hashtags(description: str) -> str:
    """Guarantee #shorts is present and cap total hashtags at 15.

    #shorts still links the video into the Shorts collection (classification
    itself is by dimensions+duration), and >15 hashtags makes YouTube ignore
    every hashtag on the video — so trim from the end rather than risk that.
    """
    if not any(
        m.group().lower() == "#shorts" for m in re.finditer(r"#\w+", description)
    ):
        description = (
            description.rstrip()
            + ("\n\n" if description.strip() else "")
            + "#shorts #dj"
        )
    matches = list(re.finditer(r"#\w+", description))
    excess = len(matches) - 15
    if excess > 0:
        # Drop the trailing hashtags, but never #shorts itself.
        removable = [m for m in matches if m.group().lower() != "#shorts"]
        to_remove = sorted(
            (m.start(), m.end()) for m in removable[len(removable) - excess:]
        )
        parts: List[str] = []
        last = 0
        for start, end in to_remove:
            parts.append(description[last:start])
            last = end
        parts.append(description[last:])
        description = re.sub(r"[ \t]{2,}", " ", "".join(parts))
        description = re.sub(r"[ \t]+\n", "\n", description).rstrip()
    return description


async def generate_short_metadata(
    short: Short,
    recent_titles: List[str],
    brand: Optional[BrandSettings],
    db_settings_json: Optional[Dict[str, Any]],
    session=None,
) -> Dict[str, Any]:
    """LLM-generate {title, description, tags} for a short.

    Reuses DescriptionGenerator's guarded completion call; the brand link
    block (Twitch/SoundCloud/site/shop) is appended deterministically after
    the model output, same as full-mix descriptions.
    """
    track_credit = (
        f"{short.track_artist} - {short.track_title}"
        if short.track_artist and short.track_title
        else "none identified (do not invent one)"
    )
    recorded = parse_recording_date(short.file_path)
    prompt = SHORTS_METADATA_PROMPT.format(
        brand_name=(brand.brand_name if brand and brand.brand_name else settings.BRAND_NAME),
        track_credit_hint=(
            f"{short.track_artist} - {short.track_title}"
            if short.track_artist and short.track_title else "genre keywords"
        ),
        recent_titles=(
            "\n".join(f"- {t}" for t in recent_titles[:20]) if recent_titles else "- (none yet)"
        ),
        recorded_at=recorded.strftime("%Y-%m-%d") if recorded else "unknown",
        duration_seconds=short.duration_seconds or 0.0,
        track_credit=track_credit,
    )

    generator = get_description_generator(db_settings_json)
    response, text = await generator._create_completion(
        prompt, max_tokens=600, temperature=0.8
    )

    try:
        payload = json.loads(_strip_code_fences(text))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Shorts metadata LLM returned non-JSON: {text[:200]!r}") from exc

    title = _enforce_title(str(payload.get("title") or ""))
    description = str(payload.get("description") or "").strip()
    tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()][:15]
    if not title or not description:
        raise RuntimeError("Shorts metadata LLM returned empty title/description")

    description = _enforce_hashtags(description)

    # Brand links appended deterministically (never trusted to the model).
    links = (brand.youtube_links if brand and brand.youtube_links else settings.YOUTUBE_LINKS)
    if links:
        description = f"{description}\n\n{links}"
    description = description[:5000]

    # Record token usage/cost (shorts have no mix row; mix_id stays NULL).
    if session is not None and getattr(response, "usage", None) is not None:
        session.add(
            AIUsage(
                mix_id=None,
                provider="openai",
                model=generator._model,
                operation="shorts_metadata",
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                cost_usd=round(
                    price_for_model(
                        generator._model,
                        response.usage.prompt_tokens,
                        response.usage.completion_tokens,
                    ),
                    6,
                ),
            )
        )

    return {"title": title, "description": description, "tags": tags}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_short(short: Short) -> Dict[str, Any]:
    return {
        "id": short.id,
        "file_path": short.file_path,
        "filename": os.path.basename(short.file_path or ""),
        "file_hash": short.file_hash,
        "title": short.title,
        "description": short.description,
        "tags": short.tags,
        "track_artist": short.track_artist,
        "track_title": short.track_title,
        "duration_seconds": short.duration_seconds,
        "width": short.width,
        "height": short.height,
        "youtube_video_id": short.youtube_video_id,
        "youtube_url": short.youtube_url,
        "status": short.status,
        "error": short.error,
        "detected_at": short.detected_at.isoformat() if short.detected_at else None,
        "uploaded_at": short.uploaded_at.isoformat() if short.uploaded_at else None,
        "metadata_json": short.metadata_json,
    }


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ShortsService:
    """Watcher + pipeline + quota-aware uploader for YouTube Shorts."""

    def __init__(self, watch_path: Optional[str] = None) -> None:
        self._watch_path = watch_path or settings.SHORTS_WATCH_PATH
        self._observer: Optional[Observer] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._tracker = _StabilityTracker(
            settings.FILE_STABLE_SECONDS, settings.STABILITY_CONFIRMATIONS
        )
        self._running = False
        self._scan_lock = asyncio.Lock()
        self._drain_lock = asyncio.Lock()
        self._last_drain = 0.0

    # ------------------------------------------------------------------
    # Watcher lifecycle
    # ------------------------------------------------------------------

    @property
    def watcher_active(self) -> bool:
        return self._running

    async def start_watcher(self) -> bool:
        """Arm the folder watcher. No-op (False) when disabled or unmounted.

        Only ``.mp4`` is watched — the ``.mkv`` sibling OBS writes for the
        same recording never enters the tracker. Pre-existing files are NOT
        swept on start (the 89-file backlog is imported explicitly via
        ``scan()``); the watcher reacts to new drops only, mirroring
        WATCH_INGEST_EXISTING_ON_START=False semantics.
        """
        if not settings.SHORTS_ENABLED:
            logger.info("Shorts watcher disabled (SHORTS_ENABLED=false)")
            return False
        if not os.path.isdir(self._watch_path):
            logger.info(
                "Shorts watcher not armed: %s does not exist", self._watch_path
            )
            return False

        loop = asyncio.get_running_loop()
        self._observer = Observer()
        self._observer.schedule(
            _WatchHandler({".mp4"}, "shorts", self._tracker, loop),
            self._watch_path,
            recursive=False,
        )
        self._observer.start()
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Shorts watcher started: %s", self._watch_path)
        await activity_log.info(
            "shorts_watcher_started",
            f"Shorts watcher armed on {self._watch_path}",
            platform="youtube",
        )
        return True

    async def stop_watcher(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    async def _poll_loop(self) -> None:
        import time as _time

        while self._running:
            await asyncio.sleep(10)
            try:
                await self._check_stable_files()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Shorts stability check failed")
            # Periodic drain: quota/cap resets daily, so retry queued shorts.
            if _time.time() - self._last_drain >= settings.SHORTS_DRAIN_INTERVAL_SECONDS:
                self._last_drain = _time.time()
                try:
                    await self.drain_queue()
                except Exception:  # pragma: no cover - defensive
                    logger.exception("Shorts drain pass failed")

    async def _check_stable_files(self) -> None:
        for path in list(self._tracker.tracked_paths):
            if not os.path.exists(path):
                self._tracker.remove(path)
                continue
            self._tracker.update(path)
            if not self._tracker.is_stable(path):
                continue
            size = self._tracker.size(path)
            if size is not None and size < MIN_SHORT_FILE_BYTES:
                logger.warning("Skipping undersized shorts file (%s bytes): %s", size, path)
                self._tracker.remove(path)
                continue
            self._tracker.remove(path)
            try:
                short_id = await self.ingest_file(path)
            except Exception:
                logger.exception("Shorts ingest failed for %s", path)
                continue
            if short_id:
                # Process in the background; failures land on the row itself.
                asyncio.create_task(self._process_safe(short_id))

    async def _process_safe(self, short_id: str) -> None:
        try:
            await self.process_short(short_id)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Shorts processing crashed for %s", short_id)

    # ------------------------------------------------------------------
    # Ingest + scan
    # ------------------------------------------------------------------

    async def ingest_file(self, path: str) -> Optional[str]:
        """Create a ``detected`` row for a file, or None when already known.

        Dedupe key is the first-10MB MD5 (same as the mix watcher) checked
        against the shorts table itself — re-drops, renames, and re-scans of
        the same recording never create a second row.
        """
        file_hash = await asyncio.to_thread(_compute_file_hash, path)
        async with async_session_factory() as session:
            existing = (
                await session.execute(select(Short).where(Short.file_hash == file_hash))
            ).scalar_one_or_none()
            if existing:
                logger.info("Shorts: already known file %s (hash=%s)", path, file_hash)
                return None
            short = Short(
                file_path=path,
                file_hash=file_hash,
                status="detected",
                detected_at=datetime.now(timezone.utc),
            )
            session.add(short)
            await session.commit()
            short_id = short.id
        await activity_log.info(
            "shorts_detected",
            f"New vertical clip detected: {os.path.basename(path)}",
            filename=os.path.basename(path),
            platform="youtube",
        )
        return short_id

    async def scan(self) -> Dict[str, Any]:
        """Backlog import: ingest + process every not-yet-seen .mp4 in the folder.

        Three phases, so the catalog dedupe sees the WHOLE backlog at once:

        1. Ingest + analyze (ffprobe/eligibility) every new file, oldest first
           (the Backtrack filename embeds the recording timestamp, so a name
           sort is a date sort).
        2. One-to-one catalog dedupe across all analyzed clips — including
           previously catalog-skipped rows, whose links are re-derived so a
           better-scoring new clip can claim a video id (the loser re-enters
           the pipeline). Per-clip matching here once linked 52 clips to only
           3 distinct video ids; see :meth:`dedupe_backlog`.
        3. Track-ID + metadata + upload for everything left unmatched.
           Auto-upload respects the daily cap, so a large backlog lands
           mostly ``queued`` and drains over the following days.

        Summary persists in AppSettings.settings_json[LAST_SCAN_KEY].
        """
        async with self._scan_lock:
            summary: Dict[str, Any] = {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "running",
                "files_seen": 0,
                "already_known": 0,
                "ingested": 0,
                "catalog_matched": 0,
                "requeued": 0,
                "errors": [],
            }
            await self._persist_scan_summary(summary)
            await activity_log.info(
                "shorts_scan", f"Shorts backlog scan started on {self._watch_path}",
                platform="youtube",
            )
            try:
                files = sorted(
                    p for p in Path(self._watch_path).glob("*.mp4") if p.is_file()
                )
            except OSError as exc:
                summary["status"] = "failed"
                summary["errors"].append(str(exc))
                summary["finished_at"] = datetime.now(timezone.utc).isoformat()
                await self._persist_scan_summary(summary)
                return summary

            # Phase 1: ingest + analyze (no dedupe, no upload yet).
            for f in files:
                summary["files_seen"] += 1
                try:
                    short_id = await self.ingest_file(str(f))
                except Exception as exc:
                    summary["errors"].append(f"{f.name}: {exc}")
                    continue
                if short_id is None:
                    summary["already_known"] += 1
                    continue
                summary["ingested"] += 1
                try:
                    await self._analyze_short(short_id, leave_pending=True)
                except Exception as exc:
                    summary["errors"].append(f"{f.name}: {exc}")
                await self._persist_scan_summary(summary)

            # Phase 2: global one-to-one dedupe against the channel catalog.
            try:
                dedupe = await self.dedupe_backlog()
                summary["catalog_matched"] = dedupe["linked"]
                summary["requeued"] = dedupe["unlinked"]
            except Exception as exc:
                summary["errors"].append(f"dedupe: {exc}")
            await self._persist_scan_summary(summary)

            # Phase 3: finish every analyzed-but-unmatched clip (status
            # ``detected`` with probe data), oldest first — including rows a
            # better clip just unlinked in phase 2.
            async with async_session_factory() as session:
                pending_ids = (
                    (
                        await session.execute(
                            select(Short.id)
                            .where(
                                Short.status == "detected",
                                Short.duration_seconds.isnot(None),
                            )
                            .order_by(Short.detected_at.asc(), Short.id.asc())
                        )
                    )
                    .scalars()
                    .all()
                )
            for short_id in pending_ids:
                try:
                    await self._finish_short(short_id)
                except Exception as exc:
                    summary["errors"].append(f"{short_id}: {exc}")
                await self._persist_scan_summary(summary)

            summary["status"] = "ok" if not summary["errors"] else "partial"
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            await self._persist_scan_summary(summary)
            await activity_log.info(
                "shorts_scan",
                (
                    f"Shorts scan finished: {summary['ingested']} ingested, "
                    f"{summary['already_known']} already known, "
                    f"{summary['catalog_matched']} matched to existing channel "
                    f"uploads, {summary['requeued']} requeued."
                ),
                platform="youtube",
                context=summary,
            )
            return summary

    async def _persist_scan_summary(self, summary: Dict[str, Any]) -> None:
        async with async_session_factory() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.id == 1))
            ).scalar_one_or_none()
            if row is None:
                row = AppSettings(id=1)
                session.add(row)
            merged = dict(row.settings_json or {})
            merged[LAST_SCAN_KEY] = summary
            row.settings_json = merged
            await session.commit()

    # ------------------------------------------------------------------
    # Analyze + metadata
    # ------------------------------------------------------------------

    async def _load_short(self, session, short_id: str) -> Short:
        short = (
            await session.execute(select(Short).where(Short.id == short_id))
        ).scalar_one_or_none()
        if short is None:
            raise RuntimeError(f"Short {short_id} not found")
        return short

    async def process_short(self, short_id: str, auto_upload: bool = True) -> str:
        """Run analyze -> catalog dedupe -> track ID -> metadata -> (upload).

        Returns the resulting status string. This is the single-clip path
        (watcher drops, un-skip); the backlog ``scan()`` runs the same steps
        but batches the dedupe across all clips at once.
        """
        probe = await self._analyze_short(short_id)
        if probe is None:
            # Terminal in analysis (skipped for shape/length, or failed).
            async with async_session_factory() as session:
                short = await self._load_short(session, short_id)
                return short.status

        # Catalog dedupe for one clip: one-to-one is enforced by excluding
        # every video id already claimed by any other short row, so a burst
        # of similar-length clips can never all link the same channel video.
        async with async_session_factory() as session:
            short = await self._load_short(session, short_id)
            matched = await self._match_catalog(session, short)
            if matched is not None:
                short.youtube_video_id = matched.youtube_video_id
                short.youtube_url = matched.youtube_url or (
                    f"https://www.youtube.com/shorts/{matched.youtube_video_id}"
                )
                short.metadata_json = {
                    **(short.metadata_json or {}),
                    "catalog_match": {"mix_id": matched.id, "mix_title": matched.title},
                }
                return await self._skip(
                    session, short,
                    f"already on channel (matched catalog mix '{matched.title}')",
                    event="shorts_skipped",
                )

        return await self._finish_short(short_id, auto_upload=auto_upload)

    async def _analyze_short(
        self, short_id: str, leave_pending: bool = False
    ) -> Optional[Dict[str, Any]]:
        """ffprobe + eligibility gates. Returns the probe dict, or None when
        the clip is terminal (skipped for shape/length, or failed).

        ``leave_pending=True`` (scan phase 1) parks an eligible clip back in
        ``detected`` so the batch dedupe + finish phases pick it up later.

        Session discipline (same pattern as catalog_playlists._save_placement
        and catalog_backfill._backfill_one): sqlite holds its single write
        lock from the first flushed write until COMMIT, so a session is NEVER
        held across the ffprobe subprocess; each phase opens its own
        short-lived session and commits immediately.
        """
        # Claim the row (short session, commit, close).
        async with async_session_factory() as session:
            short = await self._load_short(session, short_id)
            short.status = "analyzing"
            short.error = None
            await session.commit()
            file_path = short.file_path

        # Probe — no session held across the subprocess.
        try:
            probe = await ffprobe_clip(file_path)
        except Exception as exc:
            async with async_session_factory() as session:
                short = await self._load_short(session, short_id)
                await self._fail(session, short, f"ffprobe failed: {exc}")
                return None

        # Store probe results + eligibility gates.
        async with async_session_factory() as session:
            short = await self._load_short(session, short_id)
            short.duration_seconds = probe["duration"]
            short.width = probe["width"]
            short.height = probe["height"]

            # Shorts eligibility: vertical (h > w) and <= 180s. The 3-minute
            # ceiling has applied since 2024-10-15; a longer or landscape file
            # would upload as a regular video, so it is skipped, not failed.
            if probe["height"] <= probe["width"]:
                await self._skip(
                    session, short,
                    f"not vertical ({probe['width']}x{probe['height']})",
                )
                return None
            if probe["duration"] <= 0 or probe["duration"] > settings.SHORTS_MAX_DURATION_SECONDS:
                await self._skip(
                    session, short,
                    f"duration {probe['duration']:.0f}s outside the Shorts limit "
                    f"({settings.SHORTS_MAX_DURATION_SECONDS:.0f}s max)",
                )
                return None

            if leave_pending:
                short.status = "detected"  # analyzed; awaiting dedupe + finish
            await session.commit()
        return probe

    async def _finish_short(self, short_id: str, auto_upload: bool = True) -> str:
        """Track ID + metadata + (upload) for an already-analyzed clip."""
        async with async_session_factory() as session:
            short = await self._load_short(session, short_id)
            short.status = "analyzing"
            await session.commit()
            file_path = short.file_path
            duration = short.duration_seconds or 0.0

        # Track ID — ffmpeg extract + Shazam with NO session open.
        artist, title = await identify_track(file_path, duration)

        # Persist track ID, metadata, status, optional upload. The LLM/upload
        # awaits below run with all prior writes committed (only a WAL-safe
        # read transaction may be open across them).
        async with async_session_factory() as session:
            short = await self._load_short(session, short_id)
            short.track_artist = artist
            short.track_title = title
            await session.commit()

            try:
                await self._generate_metadata(session, short)
            except Exception as exc:
                return await self._fail(session, short, f"metadata generation failed: {exc}")

            short.status = "ready"
            await session.commit()
            await activity_log.info(
                "shorts_ready",
                f"Short ready: {short.title!r}"
                + (f" (track: {artist} - {title})" if artist and title else " (no track ID)"),
                filename=os.path.basename(short.file_path),
                platform="youtube",
            )

            if auto_upload:
                allowed, info = await self._can_upload(session)
                if allowed:
                    await self._upload(session, short)
                else:
                    short.status = "queued"
                    await session.commit()
                    await activity_log.info(
                        "shorts_queued",
                        f"Short queued (daily cap/quota): {short.title!r} — {info}",
                        filename=os.path.basename(short.file_path),
                        platform="youtube",
                    )
            return short.status

    async def _generate_metadata(self, session, short: Short) -> None:
        # Recent titles feed the uniqueness rule so consecutive clips from the
        # same session don't all get the same hook.
        recent = (
            (
                await session.execute(
                    select(Short.title)
                    .where(Short.title.isnot(None), Short.id != short.id)
                    .order_by(Short.detected_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        brand = (
            await session.execute(select(BrandSettings).where(BrandSettings.id == 1))
        ).scalar_one_or_none()
        app_row = (
            await session.execute(select(AppSettings).where(AppSettings.id == 1))
        ).scalar_one_or_none()
        sj = dict(app_row.settings_json or {}) if app_row else {}

        meta = await generate_short_metadata(short, list(recent), brand, sj, session)
        short.title = meta["title"]
        short.description = meta["description"]
        short.tags = meta["tags"]
        await session.commit()

    async def regenerate_metadata(self, short_id: str) -> Dict[str, Any]:
        """Re-run the LLM metadata step for an already-analyzed short."""
        async with async_session_factory() as session:
            short = (
                await session.execute(select(Short).where(Short.id == short_id))
            ).scalar_one_or_none()
            if short is None:
                raise RuntimeError(f"Short {short_id} not found")
            await self._generate_metadata(session, short)
            if short.status in ("failed", "detected", "analyzing"):
                short.status = "ready"
                short.error = None
                await session.commit()
            await activity_log.info(
                "shorts_metadata_regenerated",
                f"Short metadata regenerated: {short.title!r}",
                filename=os.path.basename(short.file_path),
                platform="youtube",
            )
            return serialize_short(short)

    async def _short_shaped_mixes(self, session) -> List[Mix]:
        """Channel uploads that could be Shorts (YouTube id + duration <= 185s)."""
        return list(
            (
                await session.execute(
                    select(Mix).where(
                        Mix.youtube_video_id.isnot(None),
                        Mix.duration_seconds.isnot(None),
                        Mix.duration_seconds <= DEDUPE_MAX_MIX_DURATION,
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _match_catalog(self, session, short: Short) -> Optional[Mix]:
        """Best available catalog match for ONE clip, one-to-one enforced.

        Every video id already held by any other short row — an earlier
        catalog-dedupe link OR one of our own uploads — is off the table, so
        this path can never pile a second clip onto the same channel video.
        Among the remaining candidates the lowest ``_match_score`` wins
        (duration diff + 0.5x day diff; both gates required).
        """
        claimed = set(
            (
                await session.execute(
                    select(Short.youtube_video_id).where(
                        Short.youtube_video_id.isnot(None),
                        Short.id != short.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        recorded_at = parse_recording_date(short.file_path)
        best: Optional[Mix] = None
        best_score: Optional[float] = None
        for mix in await self._short_shaped_mixes(session):
            if mix.youtube_video_id in claimed:
                continue
            score = _match_score(short.duration_seconds or 0.0, recorded_at, mix)
            if score is not None and (best_score is None or score < best_score):
                best, best_score = mix, score
        return best

    async def dedupe_backlog(self) -> Dict[str, int]:
        """Rebuild clip <-> channel-video links, strictly one-to-one.

        Pool: every analyzed clip still awaiting processing (``detected``
        with probe data) plus every previously catalog-skipped clip — the
        latter are re-derived, so a better-scoring clip can claim a video id
        away from an earlier weaker match, and the loser drops back to
        ``detected`` for normal processing.

        Assignment is greedy, best (lowest) score first; ties prefer an
        existing link, then older clips, so re-scans are idempotent. Video
        ids held by shorts OUTSIDE the pool (our own uploads, manual links)
        are never up for grabs. Mirrors catalog_match's never-double-assign
        rule: each video id claims at most one clip and vice versa. (The old
        per-clip match linked 52 of 89 backlog clips to just 3 distinct
        video ids — dozens of similar-length clips matching the same few
        uploads.)
        """
        linked = unlinked = 0
        async with async_session_factory() as session:
            all_shorts = list(
                (await session.execute(select(Short))).scalars().all()
            )

            def _is_catalog_skip(s: Short) -> bool:
                return s.status == "skipped" and bool(
                    (s.metadata_json or {}).get("catalog_match")
                )

            pool = [
                s for s in all_shorts
                if s.duration_seconds is not None
                and (s.status == "detected" or _is_catalog_skip(s))
            ]
            pool_ids = {s.id for s in pool}
            claimed_elsewhere = {
                s.youtube_video_id
                for s in all_shorts
                if s.youtube_video_id and s.id not in pool_ids
            }
            candidates = [
                m for m in await self._short_shaped_mixes(session)
                if m.youtube_video_id not in claimed_elsewhere
            ]

            # All valid (clip, video) pairs, scored. Sort key: score, then
            # keep-existing-link, then clip age/id — deterministic, so a
            # re-scan with unchanged inputs reproduces the same assignment.
            pairs: List[tuple] = []
            for clip in pool:
                recorded_at = parse_recording_date(clip.file_path)
                for mix in candidates:
                    score = _match_score(clip.duration_seconds, recorded_at, mix)
                    if score is None:
                        continue
                    keeps_existing = (
                        0 if clip.youtube_video_id == mix.youtube_video_id else 1
                    )
                    pairs.append(
                        (
                            score,
                            keeps_existing,
                            str(clip.detected_at or ""),
                            clip.id,
                            mix.youtube_video_id,
                            clip,
                            mix,
                        )
                    )
            pairs.sort(key=lambda p: p[:5])

            taken_clips: set = set()
            taken_videos: set = set()
            assignment: Dict[str, Mix] = {}
            for score, _, _, clip_id, video_id, clip, mix in pairs:
                if clip_id in taken_clips or video_id in taken_videos:
                    continue
                taken_clips.add(clip_id)
                taken_videos.add(video_id)
                assignment[clip_id] = mix

            for clip in pool:
                mix = assignment.get(clip.id)
                if mix is not None:
                    clip.status = "skipped"
                    clip.youtube_video_id = mix.youtube_video_id
                    clip.youtube_url = mix.youtube_url or (
                        f"https://www.youtube.com/shorts/{mix.youtube_video_id}"
                    )
                    clip.error = (
                        f"already on channel (matched catalog mix '{mix.title}')"
                    )
                    clip.metadata_json = {
                        **(clip.metadata_json or {}),
                        "catalog_match": {"mix_id": mix.id, "mix_title": mix.title},
                    }
                    linked += 1
                elif _is_catalog_skip(clip):
                    # Lost its link (a better clip claimed the video, or the
                    # old link no longer passes the gates) — back into the
                    # pipeline for normal processing.
                    clip.status = "detected"
                    clip.youtube_video_id = None
                    clip.youtube_url = None
                    clip.error = None
                    md = dict(clip.metadata_json or {})
                    md.pop("catalog_match", None)
                    clip.metadata_json = md or None
                    unlinked += 1
            await session.commit()

        if linked or unlinked:
            await activity_log.info(
                "shorts_dedupe",
                (
                    f"Shorts catalog dedupe: {linked} clips linked one-to-one "
                    f"to existing channel uploads, {unlinked} requeued."
                ),
                platform="youtube",
                context={"linked": linked, "unlinked": unlinked},
            )
        return {"linked": linked, "unlinked": unlinked}

    # ------------------------------------------------------------------
    # Upload + quota
    # ------------------------------------------------------------------

    async def _quota_state(self, session) -> Tuple[int, int, Dict[str, Any]]:
        """Return (used_today, budget, settings_json) from the shared ledger."""
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == 1))
        ).scalar_one_or_none()
        sj = dict(row.settings_json or {}) if row else {}
        quota = sj.get(QUOTA_KEY) or {}
        today = date.today().isoformat()
        used = int(quota.get("used", 0)) if quota.get("date") == today else 0
        return used, settings.YOUTUBE_DAILY_QUOTA_BUDGET, sj

    async def _uploads_today(self, session) -> int:
        midnight = datetime.combine(date.today(), dt_time.min)
        return (
            await session.execute(
                select(sa_func.count())
                .select_from(Short)
                .where(Short.status == "uploaded", Short.uploaded_at >= midnight)
            )
        ).scalar() or 0

    async def _can_upload(
        self, session, ignore_cap: bool = False
    ) -> Tuple[bool, str]:
        """Cap + shared-quota gate for one more videos.insert (1600 units)."""
        uploaded_today = await self._uploads_today(session)
        if not ignore_cap and uploaded_today >= settings.SHORTS_DAILY_UPLOAD_CAP:
            return False, (
                f"daily cap reached ({uploaded_today}/{settings.SHORTS_DAILY_UPLOAD_CAP})"
            )
        used, budget, _ = await self._quota_state(session)
        if used + YT_SHORT_UPLOAD_COST > budget:
            return False, f"YouTube quota budget reached ({used}/{budget} units)"
        return True, "ok"

    async def _record_quota(self, session) -> None:
        """Charge one upload (1600 units) to the shared daily ledger.

        A videos.insert consumes quota whether or not it succeeds, so this is
        recorded before the API call — same convention as catalog_apply.
        """
        row = (
            await session.execute(select(AppSettings).where(AppSettings.id == 1))
        ).scalar_one_or_none()
        if row is None:
            row = AppSettings(id=1)
            session.add(row)
        sj = dict(row.settings_json or {})
        quota = sj.get(QUOTA_KEY) or {}
        today = date.today().isoformat()
        used = int(quota.get("used", 0)) if quota.get("date") == today else 0
        sj[QUOTA_KEY] = {"date": today, "used": used + YT_SHORT_UPLOAD_COST}
        row.settings_json = sj
        await session.commit()

    async def _upload(self, session, short: Short) -> None:
        short.status = "uploading"
        await session.commit()
        await activity_log.info(
            "shorts_uploading",
            f"Uploading Short: {short.title!r}",
            filename=os.path.basename(short.file_path),
            platform="youtube",
        )

        await self._record_quota(session)
        _, _, sj = await self._quota_state(session)
        uploader = get_youtube_uploader(sj)
        try:
            result = await uploader.upload_short(
                short.file_path,
                title=short.title or os.path.basename(short.file_path),
                description=short.description or "",
                tags=list(short.tags or []),
            )
        except Exception as exc:
            short.status = "failed"
            short.error = str(exc)[:2000]
            await session.commit()
            await activity_log.error(
                "shorts_failed",
                f"Short upload failed: {short.title!r} — {exc}",
                filename=os.path.basename(short.file_path),
                platform="youtube",
            )
            return

        short.youtube_video_id = result["video_id"]
        short.youtube_url = result["video_url"]
        short.status = "uploaded"
        short.uploaded_at = datetime.now(timezone.utc)
        short.error = None
        await session.commit()
        await activity_log.info(
            "shorts_uploaded",
            f"Short uploaded: {short.title!r} -> {short.youtube_url}",
            filename=os.path.basename(short.file_path),
            platform="youtube",
            context={"video_id": short.youtube_video_id},
        )

    async def upload_short_by_id(self, short_id: str, manual: bool = False) -> str:
        """Upload one short now. Manual triggers bypass the daily cap (the
        user asked for it) but never the shared quota budget."""
        async with async_session_factory() as session:
            short = (
                await session.execute(select(Short).where(Short.id == short_id))
            ).scalar_one_or_none()
            if short is None:
                raise RuntimeError(f"Short {short_id} not found")
            if short.status in ("uploaded", "uploading"):
                return short.status
            if not short.title or not short.description:
                raise RuntimeError("Short has no metadata yet — analyze/regenerate first")
            allowed, info = await self._can_upload(session, ignore_cap=manual)
            if not allowed:
                short.status = "queued"
                await session.commit()
                await activity_log.info(
                    "shorts_queued",
                    f"Short queued: {short.title!r} — {info}",
                    filename=os.path.basename(short.file_path),
                    platform="youtube",
                )
                return short.status
            await self._upload(session, short)
            return short.status

    async def drain_queue(self) -> Dict[str, Any]:
        """Upload queued shorts, OLDEST FIRST, while cap + quota allow.

        Steady state this pushes SHORTS_DAILY_UPLOAD_CAP per day until the
        backlog is gone.
        """
        async with self._drain_lock:
            summary = {"uploaded": 0, "remaining": 0}
            async with async_session_factory() as session:
                while True:
                    allowed, _ = await self._can_upload(session)
                    if not allowed:
                        break
                    short = (
                        await session.execute(
                            select(Short)
                            .where(Short.status == "queued")
                            .order_by(Short.detected_at.asc(), Short.id.asc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if short is None:
                        break
                    await self._upload(session, short)
                    if short.status == "uploaded":
                        summary["uploaded"] += 1
                summary["remaining"] = (
                    await session.execute(
                        select(sa_func.count())
                        .select_from(Short)
                        .where(Short.status == "queued")
                    )
                ).scalar() or 0
            return summary

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def stats(self) -> Dict[str, Any]:
        async with async_session_factory() as session:
            uploaded_today = await self._uploads_today(session)
            used, budget, _ = await self._quota_state(session)
            counts_rows = (
                await session.execute(
                    select(Short.status, sa_func.count()).group_by(Short.status)
                )
            ).all()
            counts = {status: n for status, n in counts_rows}
        return {
            "uploaded_today": uploaded_today,
            "daily_cap": settings.SHORTS_DAILY_UPLOAD_CAP,
            "cap_reached": uploaded_today >= settings.SHORTS_DAILY_UPLOAD_CAP,
            "queued": counts.get("queued", 0),
            "quota_used": used,
            "quota_budget": budget,
            "status_counts": counts,
            "watcher_active": self.watcher_active,
            "watch_path": self._watch_path,
        }

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    async def _skip(
        self, session, short: Short, reason: str, event: str = "shorts_skipped"
    ) -> str:
        short.status = "skipped"
        short.error = reason
        await session.commit()
        await activity_log.info(
            event,
            f"Short skipped ({reason}): {os.path.basename(short.file_path)}",
            filename=os.path.basename(short.file_path),
            platform="youtube",
        )
        return "skipped"

    async def _fail(self, session, short: Short, reason: str) -> str:
        short.status = "failed"
        short.error = reason[:2000]
        await session.commit()
        await activity_log.error(
            "shorts_failed",
            f"Short failed ({os.path.basename(short.file_path)}): {reason}",
            filename=os.path.basename(short.file_path),
            platform="youtube",
        )
        return "failed"


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_service: Optional[ShortsService] = None


def get_shorts_service() -> ShortsService:
    global _service
    if _service is None:
        _service = ShortsService()
    return _service
