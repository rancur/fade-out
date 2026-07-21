"""Pipeline step handlers -- glue between the orchestrator and actual services."""

import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory
from app.models import AppSettings, BrandSettings, Mix
from app.services import app_config
from app.services.pipeline import (
    PipelineOrchestrator,
    VideoNotReady,
    check_video_complete,
)

logger = logging.getLogger(__name__)

# YouTube Data API cost of one videos.insert, charged to the shared daily
# ledger. Key + cost MUST stay in sync with shorts_pipeline / catalog_apply.
YT_QUOTA_KEY = "catalog_yt_quota"
YT_UPLOAD_QUOTA_COST = 1600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_mix(mix_id: str, session: AsyncSession) -> Mix:
    """Load a Mix or raise if not found."""
    mix = await session.get(Mix, mix_id)
    if mix is None:
        raise RuntimeError(f"Mix {mix_id} not found")
    return mix


async def _emit_activity(level: str, event: str, message: str, **kwargs) -> None:
    """Best-effort activity-log emit — never breaks a pipeline step."""
    try:
        from app.services import activity_log

        await activity_log.log(level, event, message, **kwargs)
    except Exception:  # pragma: no cover - defensive
        logger.debug("activity emit failed for %s", event, exc_info=True)


async def _get_brand_settings(session: AsyncSession) -> Optional[BrandSettings]:
    """Load brand settings (row id=1) if they exist."""
    return await session.get(BrandSettings, 1)


async def _get_app_settings(session: AsyncSession) -> Optional[AppSettings]:
    """Load app settings (row id=1) if they exist."""
    return await session.get(AppSettings, 1)


async def _resolve_youtube_publish_mode(
    app_settings: Optional[AppSettings],
) -> str:
    """Effective publish mode for a YouTube mix upload.

    ``youtube_publish_mode`` (immediate | scheduled) resolved via app_config
    wins whenever it is explicitly set in settings_json. When it is not set,
    legacy ``premiere_mode`` values that change privacy (``instant`` / the
    ``unlisted`` safety flag) are honored so existing deployments keep their
    behavior; otherwise the schema default (scheduled) applies.

    A stored ``premiere`` value (written by builds that offered the mode
    before it was dropped — the Data API cannot create Premieres) is coerced
    to ``scheduled``, which is what it always did in practice.
    """
    publish_mode = await app_config.resolve("youtube_publish_mode")
    if publish_mode == "premiere":
        logger.info(
            "stored youtube_publish_mode 'premiere' is no longer supported; "
            "using 'scheduled'"
        )
        publish_mode = "scheduled"

    sj = (app_settings.settings_json or {}) if app_settings else {}
    if not sj.get("youtube_publish_mode"):
        legacy = settings.PREMIERE_MODE
        if app_settings is not None and app_settings.premiere_mode:
            legacy = app_settings.premiere_mode
        if legacy in ("instant", "unlisted"):
            publish_mode = legacy
    return publish_mode


def _sc_token_persister(app_settings: Optional[AppSettings]):
    """Callback that persists rotated SoundCloud tokens into app_settings.

    SoundCloud rotates the refresh token on every refresh — losing the new
    pair strands the app with an invalid_grant on the next refresh.

    Persists in its OWN short session, committed immediately: a token refresh
    fires at upload start, and flushing on the caller's long-lived pipeline
    session would hold sqlite's single write lock for the entire multi-GB
    upload. The row is re-fetched fresh so a concurrent settings_json write
    (e.g. the quota ledger) is never clobbered with a stale copy.
    """
    async def persist(access_token: str, refresh_token: Optional[str]) -> None:
        if app_settings is None:
            logger.warning("No app_settings row; refreshed SC tokens not persisted")
            return
        async with async_session_factory() as own_session:
            row = await own_session.get(AppSettings, 1)
            if row is None:
                logger.warning(
                    "No app_settings row; refreshed SC tokens not persisted"
                )
                return
            # Reassign the dict so SQLAlchemy sees the JSON column as changed
            sj = dict(row.settings_json or {})
            sj["soundcloud_access_token"] = access_token
            if refresh_token:
                sj["soundcloud_refresh_token"] = refresh_token
            row.settings_json = sj
            await own_session.commit()
        logger.info("Persisted rotated SoundCloud tokens to app_settings")

    return persist


async def _record_yt_upload_quota() -> None:
    """Charge one videos.insert (1600 units) to the shared daily quota ledger.

    The main pipeline's YouTube upload consumes the same daily API quota the
    shorts and catalog-apply jobs budget against, so it is charged to the same
    ``catalog_yt_quota`` ledger (same convention as
    ``shorts_pipeline._record_quota``). Record-only — the mix upload is never
    gated on the budget. Runs in its own short committed session.
    """
    async with async_session_factory() as session:
        row = await session.get(AppSettings, 1)
        if row is None:
            row = AppSettings(id=1)
            session.add(row)
        sj = dict(row.settings_json or {})
        quota = sj.get(YT_QUOTA_KEY) or {}
        today = date.today().isoformat()
        used = int(quota.get("used", 0)) if quota.get("date") == today else 0
        sj[YT_QUOTA_KEY] = {"date": today, "used": used + YT_UPLOAD_QUOTA_COST}
        row.settings_json = sj
        await session.commit()


def extract_youtube_video_id(url: str) -> str:
    """Extract the 11-char video ID from any common YouTube URL form.

    Handles ``watch?v=<id>`` (with extra query params in any order),
    ``youtu.be/<id>``, ``/embed/<id>``, and ``/shorts/<id>``. Falls back to the
    trailing path segment so a bare ID still round-trips.
    """
    if not url:
        return ""
    url = url.strip()

    # watch?v=<id> and any other ...?v=<id>&... form
    query_match = re.search(r"[?&]v=([^&#]+)", url)
    if query_match:
        return query_match.group(1)

    # youtu.be/<id>, /embed/<id>, /shorts/<id>, /v/<id>
    path_match = re.search(r"(?:youtu\.be/|/embed/|/shorts/|/v/)([^?&#/]+)", url)
    if path_match:
        return path_match.group(1)

    # Bare id or unknown form: take the last non-empty path/query segment.
    tail = url.split("/")[-1]
    return tail.split("?")[0].split("&")[0]


def _title_from_filename(audio_path: str) -> str:
    """Derive a human-friendly title from a filename."""
    stem = Path(audio_path).stem
    # Replace common separators with spaces
    for ch in ("_", "-", "."):
        stem = stem.replace(ch, " ")
    return stem.strip().title()


def _ensure_tracklist_section(sc_desc: str, tracklist: list) -> str:
    """Guarantee the SoundCloud description carries a "Tracklist:" section.

    The LLM sometimes weaves track names into prose and skips the actual
    tracklist section entirely (observed live). The tracklist is the product —
    guarantee it, inserted before the brand-links block when present.
    """
    if not tracklist or "Tracklist:" in sc_desc:
        return sc_desc
    lines = "\n".join(
        f"{t.get('timestamp_formatted', '?')} {t.get('artist', '?')} - {t.get('title', '?')}"
        for t in tracklist
    )
    block = f"Tracklist:\n{lines}"
    marker = "\nTwitch: https://"
    if marker in sc_desc:
        sc_desc = sc_desc.replace(marker, f"\n{block}\n{marker}", 1)
    else:
        sc_desc = f"{sc_desc}\n\n{block}"
    logger.info("Appended missing tracklist section to SoundCloud description")
    return sc_desc


async def _detect_video_offset(
    video_path: str, flac_tracklist: list, analyzer
) -> float:
    """Detect the timestamp offset between the FLAC and the video.

    Shazams the first few minutes of the video to find where the first
    identified track starts, then calculates the difference from the FLAC
    tracklist's first track timestamp.
    """
    from app.services.audio_analyzer import TrackHit

    first_flac_track = flac_tracklist[0] if flac_tracklist else None
    if not first_flac_track:
        return 0.0

    first_flac_title = first_flac_track.get("title", "").lower() if isinstance(first_flac_track, dict) else first_flac_track.title.lower()
    first_flac_ts = first_flac_track.get("timestamp_seconds", 0) if isinstance(first_flac_track, dict) else first_flac_track.timestamp_seconds

    # Sample the video at 30s intervals for the first 10 minutes
    import librosa
    duration = librosa.get_duration(path=video_path)
    max_search = min(duration, 600)  # search first 10 min

    for offset in range(0, int(max_search), 30):
        try:
            # Get native sample rate for Shazam
            sr_native = librosa.get_samplerate(video_path)
            hit = await analyzer._shazam_segment(video_path, float(offset), sr_native)
            if hit and hit.title.lower() == first_flac_title:
                # Found the first track in the video
                video_ts = float(offset)
                detected_offset = video_ts - first_flac_ts
                logger.info(
                    "Video offset: first track '%s' at %.0fs in video vs %.0fs in FLAC = %.1fs offset",
                    hit.title, video_ts, first_flac_ts, detected_offset,
                )
                return detected_offset
        except Exception:
            continue

    logger.info("Could not auto-detect video offset (first track not found in video first 10min)")
    return 0.0


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

async def handle_detect(mix_id: str, session: AsyncSession) -> Optional[dict]:
    """Verify the audio file exists and read its duration via mutagen."""
    mix = await _get_mix(mix_id, session)

    if not mix.audio_file_path or not os.path.exists(mix.audio_file_path):
        raise FileNotFoundError(
            f"Audio file missing: {mix.audio_file_path!r}"
        )

    # Get duration from mutagen (lightweight -- no full decode)
    from mutagen import File as MutagenFile

    mf = MutagenFile(mix.audio_file_path)
    if mf is not None and mf.info is not None:
        mix.duration_seconds = mf.info.length
    else:
        logger.warning("mutagen could not read info for %s", mix.audio_file_path)

    # Check video file if set
    video_ok = True
    if mix.video_file_path:
        if not os.path.exists(mix.video_file_path):
            logger.warning(
                "Video file set but not found yet: %s", mix.video_file_path,
            )
            video_ok = False

    return {
        "audio_file": mix.audio_file_path,
        "duration_seconds": mix.duration_seconds,
        "video_file": mix.video_file_path,
        "video_present": video_ok,
    }


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

async def analyze_audio_with_cue(
    audio_path: str,
    mix_id: Optional[str] = None,
    progress_cb=None,
    analyzer=None,
):
    """Analyze an audio file and merge with a date-matched CUE session.

    Shared core of the pipeline ``analyze`` step and the catalog tracklist
    backfill. The CUE must match the audio's filename date AND contain a
    session whose timestamps actually fit this recording — otherwise recent
    unrelated DJCTL plays would override the fingerprinted tracklist. No valid
    CUE -> fingerprint-only. The confidence merge then weights the
    authoritative CUE timestamps/names above the Shazam fallback: CUE wins any
    overlap, Shazam only fills gaps (and fills CUE "Track N" placeholders),
    and weak Shazam guesses collapse to "ID - ID" rather than a wrong name.

    Returns ``(result, final_tracklist, source)`` where ``result`` is the
    :class:`AnalysisResult`, ``final_tracklist`` the cleaned merged tracklist,
    and ``source`` the merge's winning source ("cue"/"merged"/"shazam"/...).
    """
    from app.services.audio_analyzer import AudioAnalyzer
    from app.services.confidence_merge import DetectionSource, merge_detections
    from app.services.djctl_integration import find_cue_for_audio, select_cue_tracks
    from app.services.tracklist_utils import clean_tracklist

    analyzer = analyzer or AudioAnalyzer()
    result = await analyzer.analyze(audio_path, progress_cb=progress_cb)

    cue_tracks = None
    cue_path = find_cue_for_audio(audio_path)
    if cue_path:
        cue_name = os.path.basename(cue_path)
        await _emit_activity(
            "info", "cue_matched",
            f"CUE sheet matched by recording date: {cue_name}",
            mix_id=mix_id, filename=cue_name, stage="analyze",
        )
        cue_tracks = select_cue_tracks(cue_path, result.duration_seconds)
        if cue_tracks is None:
            logger.warning(
                "CUE %s matched by date but no session fits mix duration %.0fs; "
                "using fingerprint-only tracklist",
                cue_path, result.duration_seconds or 0.0,
            )
            await _emit_activity(
                "warn", "cue_rejected",
                f"CUE {cue_name} rejected: no recording session fits the mix duration "
                f"({result.duration_seconds or 0.0:.0f}s) — using fingerprint-only tracklist",
                mix_id=mix_id, filename=cue_name, stage="analyze",
            )
        else:
            span = cue_tracks[-1].timestamp_seconds if cue_tracks else 0.0
            await _emit_activity(
                "info", "cue_session_selected",
                f"CUE session selected from {cue_name}: {len(cue_tracks)} tracks "
                f"spanning {span:.0f}s (authoritative over fingerprints)",
                mix_id=mix_id, filename=cue_name, stage="analyze",
                context={"tracks": len(cue_tracks), "span_seconds": round(span, 1)},
            )
    else:
        await _emit_activity(
            "info", "cue_none",
            "No same-date CUE sheet found — tracklist will be fingerprint-only",
            mix_id=mix_id, stage="analyze",
        )

    detection_sources = []
    if cue_tracks:
        detection_sources.append(
            DetectionSource("cue", [t.to_dict() for t in cue_tracks])
        )
    if result.tracklist:
        detection_sources.append(DetectionSource("shazam", result.tracklist))

    merged = merge_detections(
        detection_sources,
        name_confidence_threshold=float(
            await app_config.resolve("detection_name_confidence_threshold")
        ),
    )

    final_tracklist = merged.tracklist if merged.tracklist else result.tracklist
    # Drop recognition noise (Unknown - Unknown) and consecutive duplicates, and
    # normalize timestamps before this list drives descriptions + chapters.
    final_tracklist = clean_tracklist(final_tracklist)
    return result, final_tracklist, merged.source


async def handle_analyze(
    mix_id: str, session: AsyncSession, progress_cb=None, **_kwargs
) -> Optional[dict]:
    """Run audio analysis and merge with CUE/DJCTL data if available."""
    mix = await _get_mix(mix_id, session)

    if not mix.audio_file_path:
        raise RuntimeError("No audio file path on mix")

    from app.services.audio_analyzer import AudioAnalyzer

    analyzer = AudioAnalyzer()
    result, final_tracklist, merged_source = await analyze_audio_with_cue(
        mix.audio_file_path, mix_id=mix_id, progress_cb=progress_cb,
        analyzer=analyzer,
    )

    mix.genres = result.genres
    mix.vibes = result.vibes
    mix.energy_profile = result.energy_profile
    mix.tracklist = final_tracklist
    mix.duration_seconds = result.duration_seconds

    # Auto-detect YouTube timestamp offset if video file exists
    # The FLAC is trimmed but the video stream isn't — find where the first
    # track starts in the video vs the FLAC to calculate the offset.
    yt_offset = 0.0
    if mix.video_file_path and os.path.exists(mix.video_file_path) and final_tracklist:
        try:
            yt_offset = await _detect_video_offset(
                mix.video_file_path, final_tracklist, analyzer
            )
            logger.info("Auto-detected YouTube timestamp offset: %.1fs", yt_offset)
        except Exception as exc:
            logger.warning("Video offset detection failed, defaulting to 0: %s", exc)
    mix.youtube_timestamp_offset = yt_offset

    return {
        "genres": result.genres,
        "vibes": result.vibes,
        "bpm_range": list(result.bpm_range),
        "tracks_found": len(final_tracklist),
        "tracklist_source": merged_source,
        "duration_seconds": result.duration_seconds,
        "youtube_timestamp_offset": yt_offset,
    }


# ---------------------------------------------------------------------------
# generate_description
# ---------------------------------------------------------------------------

async def handle_generate_description(
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Generate descriptions, YouTube title, and tags.

    Lock hygiene: the creative-title draw flushes AIUsage + used_creative rows
    (sqlite's single write lock is taken at first flush and held to COMMIT),
    so it runs in its OWN short session committed before the remaining LLM
    calls — otherwise the lock would be held across ~10-40s of OpenAI awaits,
    starving every other writer past the 30s busy timeout. The follow-up LLM
    calls track usage in a short session committed after each call, and the
    caller's pipeline session only receives the cheap final field updates.
    """
    mix = await _get_mix(mix_id, session)
    brand = await _get_brand_settings(session)

    genres = mix.genres or ["electronic"]
    vibes = mix.vibes or ["mixed"]
    tracklist = mix.tracklist or []
    energy_profile = mix.energy_profile or []
    duration = mix.duration_seconds or 0.0

    # Derive a base title if none exists
    if not mix.title or mix.title == "Untitled":
        mix.title = _title_from_filename(mix.audio_file_path or "mix")

    # BPM range from energy profile
    bpm_range = None
    if energy_profile:
        bpms = [p.get("bpm", 0) for p in energy_profile if p.get("bpm")]
        if bpms:
            bpm_range = [min(bpms), max(bpms)]

    from app.services.description_generator import (
        DescriptionGenerator,
        derive_title_identity,
    )
    from app.services.tag_generator import TagGenerator

    app_settings = await _get_app_settings(session)
    sj = (app_settings.settings_json or {}) if app_settings else {}
    desc_gen = DescriptionGenerator(db_settings_json=sj)
    tag_gen = TagGenerator()

    # Generate a creative SoundCloud title. Its uniqueness claim + usage rows
    # are committed in a short session of their own before any further await.
    #
    # Series / raid-train episodes MUST stay recognizable (live incident
    # 2026-07-20: a raid-train file shipped under a fully abstract title and
    # the owner could not find his own uploads): the identity derived from the
    # source filename becomes a mandatory prefix on both platform titles, and
    # only the hook after it is creative.
    raw_filename = Path(mix.audio_file_path).stem if mix.audio_file_path else "mix"
    identity = derive_title_identity(raw_filename)
    if identity:
        logger.info("Source file carries series identity: %s", identity)
    async with async_session_factory() as title_session:
        creative_title = await desc_gen.generate_creative_title(
            genres=genres,
            vibes=vibes,
            tracklist=tracklist,
            filename=raw_filename,
            session=title_session,
            mix_id=mix_id,
            identity=identity,
        )
        await title_session.commit()
    mix.title = creative_title

    # Generate all content. Usage rows land in a short session committed right
    # after each call, so no dirty session is held across an LLM await.
    async with async_session_factory() as usage_session:
        sc_desc = await desc_gen.generate_soundcloud_description(
            mix_title=mix.title,
            genres=genres,
            vibes=vibes,
            tracklist=tracklist,
            energy_profile=energy_profile,
            bpm_range=bpm_range,
            duration_seconds=duration,
            session=usage_session,
            mix_id=mix_id,
            brand_settings=brand,
        )
        await usage_session.commit()

        yt_offset = mix.youtube_timestamp_offset or 0.0
        yt_desc = await desc_gen.generate_youtube_description(
            mix_title=mix.title,
            genres=genres,
            vibes=vibes,
            tracklist=tracklist,
            energy_profile=energy_profile,
            bpm_range=bpm_range,
            duration_seconds=duration,
            session=usage_session,
            mix_id=mix_id,
            brand_settings=brand,
            youtube_timestamp_offset=yt_offset,
        )
        await usage_session.commit()

        yt_title = await desc_gen.generate_youtube_title(
            genres=genres,
            vibes=vibes,
            bpm_range=bpm_range,
            duration_seconds=duration,
            session=usage_session,
            mix_id=mix_id,
            identity=identity,
        )
        await usage_session.commit()

    tags = tag_gen.generate(
        genres=genres,
        vibes=vibes,
        tracklist=tracklist,
    )

    # The LLM sometimes weaves track names into prose and skips the actual
    # tracklist section. The tracklist is the product — guarantee it.
    sc_desc = _ensure_tracklist_section(sc_desc, tracklist)

    mix.description_soundcloud = sc_desc
    mix.description_youtube = yt_desc
    mix.title_youtube = yt_title
    mix.tags = tags

    return {
        "soundcloud_desc_length": len(sc_desc),
        "youtube_desc_length": len(yt_desc),
        "youtube_title": yt_title,
        "tag_count": len(tags),
    }


# ---------------------------------------------------------------------------
# generate_art
# ---------------------------------------------------------------------------

async def handle_generate_art(
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Generate cover art and YouTube thumbnail.

    Lock hygiene: the uniqueness design draw DELETEs the mix's previous claims
    and flushes new ones — sqlite's single write lock is taken at the first
    flush and held to COMMIT — while each fal.ai generation can poll for up to
    5 minutes. Holding the pipeline session across the generations starved
    every other writer (shorts, backfill, activity log, catalog apply) past
    the 30s busy timeout. So the design phase runs in its OWN short session
    committed before any fal call, each generation's AIUsage row is committed
    in a short session right after the call, and the caller's pipeline session
    only receives the cheap final path updates.
    """
    mix = await _get_mix(mix_id, session)
    brand = await _get_brand_settings(session)

    genres = mix.genres or ["electronic"]
    vibes = mix.vibes or ["mixed"]

    cover_dir = settings.OUTPUT_COVER_ART_PATH
    thumb_dir = settings.OUTPUT_THUMBNAILS_PATH
    os.makedirs(cover_dir, exist_ok=True)
    os.makedirs(thumb_dir, exist_ok=True)

    cover_path = os.path.join(cover_dir, f"{mix_id}.jpg")
    thumb_path = os.path.join(thumb_dir, f"{mix_id}.jpg")

    from app.services import thumbnail_design
    from app.services.art_generator import ArtGenerator

    app_settings_art = await _get_app_settings(session)
    sj_art = (app_settings_art.settings_json or {}) if app_settings_art else {}
    art_gen = ArtGenerator(db_settings_json=sj_art)

    # Uniqueness engine: a per-mix hook + varied scene, enforced unique by the
    # registry (releases this mix's own previous claims first) so no two
    # thumbnails ever share hook text or a near-identical scene. Runs in its
    # own short session, committed BEFORE any fal call; the Mix is re-fetched
    # inside that session (never cross-attach ORM objects between sessions).
    async with async_session_factory() as design_session:
        design_mix = await _get_mix(mix_id, design_session)
        design = await thumbnail_design.unique_design_for_mix(
            design_mix, design_session, sj_art
        )
        await design_session.commit()

    # The generations run with no dirty session held: each call's AIUsage row
    # is added after the image comes back and committed immediately.
    async with async_session_factory() as art_session:
        cover_result = await art_gen.generate_cover_art(
            mix_title=mix.title,
            genres=genres,
            vibes=vibes,
            output_path=cover_path,
            session=art_session,
            mix_id=mix_id,
            brand_settings=brand,
            hook_text=design["hook"],
            scene_text=design["scene"],
        )
        await art_session.commit()

        thumb_result = await art_gen.generate_youtube_thumbnail(
            mix_title=mix.title_youtube or mix.title,
            genres=genres,
            vibes=vibes,
            output_path=thumb_path,
            cover_art_path=cover_path,
            session=art_session,
            mix_id=mix_id,
            brand_settings=brand,
            video_file_path=mix.video_file_path,
            energy_profile=mix.energy_profile or [],
            duration_seconds=mix.duration_seconds or 0.0,
            hook_text=design["hook"],
            scene_text=design["scene"],
        )
        await art_session.commit()

    mix.cover_art_path = cover_result
    mix.thumbnail_path = thumb_result

    return {
        "cover_art_path": cover_result,
        "thumbnail_path": thumb_result,
        "hook_text": design["hook"].replace("\n", " "),
    }


# ---------------------------------------------------------------------------
# upload_soundcloud
# ---------------------------------------------------------------------------

async def handle_upload_soundcloud(
    mix_id: str, session: AsyncSession, progress_cb=None, **_kwargs
) -> Optional[dict]:
    """Upload the mix to SoundCloud."""
    mix = await _get_mix(mix_id, session)

    # Idempotency: never double-upload. If this mix already has a SoundCloud URL
    # (e.g. this is a retry/re-run after a later step failed), skip re-uploading
    # and reuse the existing track.
    if mix.soundcloud_url:
        logger.info("SoundCloud already uploaded for mix %s (%s); skipping.", mix_id, mix.soundcloud_url)
        await _emit_activity(
            "info", "upload_skipped",
            f"SoundCloud upload skipped — already uploaded: {mix.soundcloud_url}",
            mix_id=mix_id, platform="soundcloud",
        )
        return {"skipped": True, "reason": "already uploaded", "soundcloud_url": mix.soundcloud_url}

    # Check for an access token in AppSettings or env config
    app_settings = await _get_app_settings(session)
    has_token = bool(settings.SOUNDCLOUD_ACCESS_TOKEN)
    if not has_token and app_settings and app_settings.settings_json:
        has_token = bool(app_settings.settings_json.get("soundcloud_access_token"))

    sj = (app_settings.settings_json or {}) if app_settings else {}
    has_credentials = bool(
        (sj.get("soundcloud_client_id") or settings.SOUNDCLOUD_CLIENT_ID)
        and (sj.get("soundcloud_client_secret") or settings.SOUNDCLOUD_CLIENT_SECRET)
    )
    has_browser_auth = bool(settings.SOUNDCLOUD_EMAIL and settings.SOUNDCLOUD_PASSWORD)

    if not has_token and not has_credentials and not has_browser_auth:
        logger.warning(
            "SoundCloud not configured -- skipping upload for mix %s", mix_id,
        )
        return {"skipped": True, "reason": "No SoundCloud credentials configured"}

    if not mix.audio_file_path or not os.path.exists(mix.audio_file_path):
        raise FileNotFoundError(f"Audio file missing: {mix.audio_file_path!r}")

    from app.services.soundcloud_uploader import SoundCloudUploader
    from app.services.tag_generator import TagGenerator

    tag_gen = TagGenerator()
    genres = mix.genres or ["electronic"]
    vibes = mix.vibes or ["mixed"]
    tags = mix.tags or tag_gen.generate(genres=genres, vibes=vibes, tracklist=mix.tracklist)
    genre_label = tag_gen.get_primary_genre_tag(genres)

    sj_sc = (app_settings.settings_json or {}) if app_settings else {}
    uploader = SoundCloudUploader(
        db_settings_json=sj_sc,
        on_tokens_refreshed=_sc_token_persister(app_settings),
        mix_id=mix_id,
    )
    await _emit_activity(
        "info", "upload_attempt", f"Uploading to SoundCloud: {mix.title}",
        mix_id=mix_id, platform="soundcloud",
    )
    permalink = await uploader.upload(
        audio_path=mix.audio_file_path,
        title=mix.title,
        description=mix.description_soundcloud or "",
        genre=genre_label,
        tags=tags,
        cover_art_path=mix.cover_art_path,
        progress_cb=progress_cb,
    )

    mix.soundcloud_url = permalink
    await _emit_activity(
        "info", "upload_result", f"SoundCloud upload succeeded: {permalink}",
        mix_id=mix_id, platform="soundcloud", context={"url": permalink},
    )
    return {"soundcloud_url": permalink}


# ---------------------------------------------------------------------------
# verify_soundcloud
# ---------------------------------------------------------------------------

async def handle_verify_soundcloud(
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Verify the SoundCloud upload is accessible."""
    mix = await _get_mix(mix_id, session)

    if not mix.soundcloud_url:
        logger.info("No SoundCloud URL for mix %s -- skipping verification", mix_id)
        return {"skipped": True, "reason": "No SoundCloud URL (upload was skipped)"}

    from app.services.soundcloud_uploader import SoundCloudUploader

    app_settings_v = await _get_app_settings(session)
    sj_v = (app_settings_v.settings_json or {}) if app_settings_v else {}
    uploader = SoundCloudUploader(
        db_settings_json=sj_v,
        on_tokens_refreshed=_sc_token_persister(app_settings_v),
    )
    verified = await uploader.verify_upload(mix.soundcloud_url)

    if not verified:
        raise RuntimeError(
            f"SoundCloud verification failed for {mix.soundcloud_url}"
        )

    return {"verified": True, "url": mix.soundcloud_url}


# ---------------------------------------------------------------------------
# upload_youtube
# ---------------------------------------------------------------------------

async def handle_upload_youtube(
    mix_id: str, session: AsyncSession, progress_cb=None, **_kwargs
) -> Optional[dict]:
    """Upload the video to YouTube."""
    mix = await _get_mix(mix_id, session)

    # Idempotency: never double-upload. If a YouTube URL/video id is already
    # recorded for this mix, reuse it instead of uploading the video again.
    if mix.youtube_url:
        existing_id = extract_youtube_video_id(mix.youtube_url)
        logger.info("YouTube already uploaded for mix %s (%s); skipping.", mix_id, mix.youtube_url)
        await _emit_activity(
            "info", "upload_skipped",
            f"YouTube upload skipped — already uploaded: {mix.youtube_url}",
            mix_id=mix_id, platform="youtube",
        )
        return {"skipped": True, "reason": "already uploaded", "youtube_url": mix.youtube_url, "video_id": existing_id}

    # Check for refresh token
    app_settings = await _get_app_settings(session)
    has_token = bool(settings.YOUTUBE_REFRESH_TOKEN)
    if not has_token and app_settings and app_settings.settings_json:
        has_token = bool(app_settings.settings_json.get("youtube_refresh_token"))

    if not has_token:
        logger.warning(
            "YouTube not configured -- skipping upload for mix %s", mix_id,
        )
        return {"skipped": True, "reason": "No YouTube refresh token configured"}

    # Video file is required for YouTube
    if not mix.video_file_path or not os.path.exists(mix.video_file_path):
        raise VideoNotReady(
            f"Video file not available: {mix.video_file_path!r}"
        )

    # Completeness gate: existing is NOT enough. A stalled OBS→NAS sync
    # leaves a partial file (prod incident: an MKV holding 10m40s of a
    # ~115-minute set, ffprobe duration=N/A, "File ended prematurely") and
    # this handler would have uploaded it. Never upload a partial video —
    # raise VideoNotReady so the orchestrator's 5-minute poll re-checks until
    # the sync finishes, or the wait ceiling fails the step with this same
    # reason. The probe is subprocess-only and nothing has been written on
    # this session yet, so no write transaction spans it.
    complete, incomplete_reason = await check_video_complete(
        mix.video_file_path, mix.duration_seconds
    )
    if not complete:
        await _emit_activity(
            "warn", "video_incomplete",
            f"YouTube upload waiting on video sync: {incomplete_reason}",
            mix_id=mix_id, platform="youtube",
            context={"video_file_path": mix.video_file_path},
        )
        raise VideoNotReady(incomplete_reason)

    from app.services.tag_generator import TagGenerator
    from app.services.youtube_uploader import YouTubeUploader

    tag_gen = TagGenerator()
    genres = mix.genres or ["electronic"]
    tags = mix.tags or tag_gen.generate(genres=genres, vibes=mix.vibes or [])

    # Effective publish mode (youtube_publish_mode, with legacy premiere_mode
    # instant/unlisted honored when the new key is unset).
    publish_mode = await _resolve_youtube_publish_mode(app_settings)

    sj_yt = (app_settings.settings_json or {}) if app_settings else {}
    uploader = YouTubeUploader(db_settings_json=sj_yt, mix_id=mix_id)
    await _emit_activity(
        "info", "upload_attempt",
        f"Uploading to YouTube ({publish_mode}): {mix.title_youtube or mix.title}",
        mix_id=mix_id, platform="youtube", context={"publish_mode": publish_mode},
    )
    result = await uploader.upload(
        video_path=mix.video_file_path,
        title=mix.title_youtube or mix.title,
        description=mix.description_youtube or "",
        tags=tag_gen.format_for_youtube(tags),
        thumbnail_path=mix.thumbnail_path,
        premiere_mode=publish_mode,
        genre_for_playlist=genres[0] if genres else None,
        progress_cb=progress_cb,
    )

    mix.youtube_url = result["video_url"]
    if result.get("playlist_id"):
        mix.youtube_playlist_id = result["playlist_id"]

    # Charge the videos.insert to the shared daily quota ledger so shorts /
    # catalog-apply budgeting sees the real spend. Best-effort: a ledger
    # failure must never fail an upload that already succeeded.
    try:
        await _record_yt_upload_quota()
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "Failed to record YouTube upload quota for mix %s", mix_id,
            exc_info=True,
        )

    await _emit_activity(
        "info", "upload_result", f"YouTube upload succeeded: {result['video_url']}",
        mix_id=mix_id, platform="youtube",
        context={"url": result["video_url"], "video_id": result.get("video_id")},
    )
    return {
        "youtube_url": result["video_url"],
        "video_id": result["video_id"],
        "playlist_id": result.get("playlist_id"),
    }


# ---------------------------------------------------------------------------
# verify_youtube
# ---------------------------------------------------------------------------

async def handle_verify_youtube(
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Verify the YouTube upload processed successfully."""
    mix = await _get_mix(mix_id, session)

    if not mix.youtube_url:
        logger.info("No YouTube URL for mix %s -- skipping verification", mix_id)
        return {"skipped": True, "reason": "No YouTube URL (upload was skipped)"}

    from app.services.youtube_uploader import YouTubeUploader

    # Extract video ID from URL (handles watch?v=, youtu.be, shorts, embed)
    video_id = extract_youtube_video_id(mix.youtube_url)

    app_settings_yv = await _get_app_settings(session)
    sj_yv = (app_settings_yv.settings_json or {}) if app_settings_yv else {}
    uploader = YouTubeUploader(db_settings_json=sj_yv)
    status = await uploader.verify_upload(video_id)

    if status.get("status") == "not_found":
        raise RuntimeError(f"YouTube video {video_id} not found")
    if status.get("status") == "error":
        raise RuntimeError(f"YouTube verification error: {status.get('error')}")

    return {
        "verified": True,
        "upload_status": status.get("status"),
        "processing": status.get("processing"),
        "video_id": video_id,
    }


# ---------------------------------------------------------------------------
# upload_mixcloud
# ---------------------------------------------------------------------------

async def handle_upload_mixcloud(
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Upload the mix to Mixcloud (gated behind MIXCLOUD_ENABLED, default OFF)."""
    mix = await _get_mix(mix_id, session)

    # Idempotency: never double-upload.
    if mix.mixcloud_url:
        logger.info("Mixcloud already uploaded for mix %s (%s); skipping.", mix_id, mix.mixcloud_url)
        await _emit_activity(
            "info", "upload_skipped",
            f"Mixcloud upload skipped — already uploaded: {mix.mixcloud_url}",
            mix_id=mix_id, platform="mixcloud",
        )
        return {"skipped": True, "reason": "already uploaded", "mixcloud_url": mix.mixcloud_url}

    app_settings = await _get_app_settings(session)
    sj = (app_settings.settings_json or {}) if app_settings else {}

    enabled = settings.MIXCLOUD_ENABLED or bool(sj.get("mixcloud_enabled"))
    if not enabled:
        logger.info("Mixcloud disabled -- skipping upload for mix %s", mix_id)
        return {"skipped": True, "reason": "Mixcloud disabled (MIXCLOUD_ENABLED=false)"}

    has_token = bool(settings.MIXCLOUD_ACCESS_TOKEN or sj.get("mixcloud_access_token"))
    if not has_token:
        logger.warning(
            "Mixcloud enabled but no access token -- skipping upload for mix %s", mix_id,
        )
        return {"skipped": True, "reason": "No Mixcloud access token configured"}

    if not mix.audio_file_path or not os.path.exists(mix.audio_file_path):
        raise FileNotFoundError(f"Audio file missing: {mix.audio_file_path!r}")

    from app.services.mixcloud_uploader import MixcloudUploader
    from app.services.tag_generator import TagGenerator

    tag_gen = TagGenerator()
    genres = mix.genres or ["electronic"]
    vibes = mix.vibes or ["mixed"]
    tags = mix.tags or tag_gen.generate(genres=genres, vibes=vibes, tracklist=mix.tracklist)

    uploader = MixcloudUploader(db_settings_json=sj)
    url = await uploader.upload(
        audio_path=mix.audio_file_path,
        title=mix.title,
        description=mix.description_soundcloud or "",
        tags=tags,
        cover_art_path=mix.cover_art_path,
    )

    mix.mixcloud_url = url
    return {"mixcloud_url": url}


# ---------------------------------------------------------------------------
# verify_mixcloud
# ---------------------------------------------------------------------------

async def handle_verify_mixcloud(
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Verify the Mixcloud upload is accessible."""
    mix = await _get_mix(mix_id, session)

    if not mix.mixcloud_url:
        logger.info("No Mixcloud URL for mix %s -- skipping verification", mix_id)
        return {"skipped": True, "reason": "No Mixcloud URL (upload was skipped)"}

    from app.services.mixcloud_uploader import MixcloudUploader

    app_settings_v = await _get_app_settings(session)
    sj_v = (app_settings_v.settings_json or {}) if app_settings_v else {}
    uploader = MixcloudUploader(db_settings_json=sj_v)
    verified = await uploader.verify_upload(mix.mixcloud_url)

    if not verified:
        raise RuntimeError(f"Mixcloud verification failed for {mix.mixcloud_url}")

    return {"verified": True, "url": mix.mixcloud_url}


# ---------------------------------------------------------------------------
# cross_link
# ---------------------------------------------------------------------------

async def handle_cross_link(
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Update descriptions on each platform to include the other platform's URL."""
    mix = await _get_mix(mix_id, session)

    sc_url = mix.soundcloud_url
    yt_url = mix.youtube_url

    updated = []
    pushed = []

    if not (sc_url and yt_url):
        logger.info(
            "Cross-link: skipping for mix %s (sc=%s, yt=%s)",
            mix_id,
            bool(sc_url),
            bool(yt_url),
        )
        return {
            "soundcloud_url": sc_url,
            "youtube_url": yt_url,
            "updated_descriptions": updated,
            "pushed_platforms": pushed,
        }

    # 1. Update the locally-stored descriptions to include the reciprocal link.
    if mix.description_soundcloud and yt_url not in mix.description_soundcloud:
        mix.description_soundcloud += f"\n\nWatch on YouTube: {yt_url}"
        updated.append("soundcloud_description")

    if mix.description_youtube and sc_url not in mix.description_youtube:
        mix.description_youtube += f"\n\nListen on SoundCloud: {sc_url}"
        updated.append("youtube_description")

    # Mixcloud is optional (gated + default OFF); only weave it in when present.
    mc_url = getattr(mix, "mixcloud_url", None)
    if mc_url:
        if mix.description_soundcloud and mc_url not in mix.description_soundcloud:
            mix.description_soundcloud += f"\n\nOn Mixcloud: {mc_url}"
            if "soundcloud_description" not in updated:
                updated.append("soundcloud_description")
        if mix.description_youtube and mc_url not in mix.description_youtube:
            mix.description_youtube += f"\n\nOn Mixcloud: {mc_url}"
            if "youtube_description" not in updated:
                updated.append("youtube_description")

    if updated:
        logger.info(
            "Cross-linked descriptions for mix %s (local update): %s",
            mix_id,
            ", ".join(updated),
        )

    # 2. Push the updated descriptions to the LIVE platforms (on by default;
    # CROSS_LINK_PUSH_ENABLED=false opts out). Without the push the
    # cross-links only ever landed in the DB and the published descriptions
    # never carried them. Each push is best-effort: a failure is logged loudly
    # but never fails the pipeline's final step. Uses existing OAuth tokens.
    if await app_config.resolve("cross_link_push_enabled"):
        app_settings = await _get_app_settings(session)
        sj = (app_settings.settings_json or {}) if app_settings else {}

        if mix.description_youtube:
            try:
                from app.services.youtube_uploader import YouTubeUploader

                video_id = extract_youtube_video_id(yt_url)
                yt_uploader = YouTubeUploader(db_settings_json=sj, mix_id=mix_id)
                await yt_uploader.update_description(video_id, mix.description_youtube)
                pushed.append("youtube")
                await _emit_activity(
                    "info", "cross_link_pushed",
                    f"Cross-link pushed to YouTube description ({video_id})",
                    mix_id=mix_id, platform="youtube", stage="cross_link",
                )
            except Exception as exc:
                logger.error("Cross-link YouTube push failed for mix %s: %s", mix_id, exc)
                await _emit_activity(
                    "warn", "cross_link_failed",
                    f"Cross-link push to YouTube failed: {exc}",
                    mix_id=mix_id, platform="youtube", stage="cross_link",
                )

        if mix.description_soundcloud:
            try:
                from app.services.soundcloud_uploader import SoundCloudUploader

                sc_uploader = SoundCloudUploader(
                    db_settings_json=sj,
                    on_tokens_refreshed=_sc_token_persister(app_settings),
                    mix_id=mix_id,
                )
                await sc_uploader.update_description(sc_url, mix.description_soundcloud)
                pushed.append("soundcloud")
                await _emit_activity(
                    "info", "cross_link_pushed",
                    f"Cross-link pushed to SoundCloud description ({sc_url})",
                    mix_id=mix_id, platform="soundcloud", stage="cross_link",
                )
            except Exception as exc:
                logger.error("Cross-link SoundCloud push failed for mix %s: %s", mix_id, exc)
                await _emit_activity(
                    "warn", "cross_link_failed",
                    f"Cross-link push to SoundCloud failed: {exc}",
                    mix_id=mix_id, platform="soundcloud", stage="cross_link",
                )

        if mc_url and mix.description_soundcloud:
            try:
                from app.services.mixcloud_uploader import MixcloudUploader

                mc_uploader = MixcloudUploader(db_settings_json=sj)
                await mc_uploader.update_description(mc_url, mix.description_soundcloud)
                pushed.append("mixcloud")
            except Exception as exc:
                logger.error("Cross-link Mixcloud push failed for mix %s: %s", mix_id, exc)

        if pushed:
            logger.info("Cross-link pushed to live platforms for mix %s: %s", mix_id, ", ".join(pushed))
    else:
        logger.info(
            "Cross-link push disabled (CROSS_LINK_PUSH_ENABLED=false); local descriptions updated only for mix %s",
            mix_id,
        )

    return {
        "soundcloud_url": sc_url,
        "youtube_url": yt_url,
        "mixcloud_url": mc_url,
        "updated_descriptions": updated,
        "pushed_platforms": pushed,
    }


# ---------------------------------------------------------------------------
# reread_tracklist (on-demand, not a pipeline step)
# ---------------------------------------------------------------------------

async def handle_reread_tracklist(mix_id: str, session: AsyncSession) -> Optional[dict]:
    """Re-detect the tracklist and patch platform descriptions in place.

    Re-runs analyze + generate_description, then updates the YouTube and
    SoundCloud descriptions via their APIs — no re-upload. Published titles
    are preserved so live URLs/branding don't churn.
    """
    mix = await _get_mix(mix_id, session)
    if not mix.audio_file_path or not os.path.exists(mix.audio_file_path):
        raise FileNotFoundError(f"Audio file missing: {mix.audio_file_path!r}")

    prev_title = mix.title
    prev_yt_title = mix.title_youtube

    logger.info("Re-detecting tracklist from audio for mix %s", mix_id)

    analyze_result = await handle_analyze(mix_id, session)
    await handle_generate_description(mix_id, session)

    # Keep the already-published titles stable — including inside the
    # regenerated description prose, which references the transient
    # creative title the generator just invented.
    transient_title = mix.title
    if prev_title and prev_title != "Untitled":
        mix.title = prev_title
        if transient_title and transient_title != prev_title:
            if mix.description_soundcloud:
                mix.description_soundcloud = mix.description_soundcloud.replace(
                    transient_title, prev_title
                )
            if mix.description_youtube:
                mix.description_youtube = mix.description_youtube.replace(
                    transient_title, prev_title
                )
    if prev_yt_title:
        mix.title_youtube = prev_yt_title

    app_settings = await _get_app_settings(session)
    sj = (app_settings.settings_json or {}) if app_settings else {}

    updated = {"youtube": False, "soundcloud": False}
    errors = {}

    if mix.youtube_url and mix.description_youtube:
        try:
            from app.services.youtube_uploader import YouTubeUploader

            video_id = extract_youtube_video_id(mix.youtube_url)
            yt = YouTubeUploader(db_settings_json=sj)
            await yt.update_description(video_id, mix.description_youtube)
            updated["youtube"] = True
        except Exception as exc:
            logger.error("YouTube description update failed: %s", exc)
            errors["youtube"] = str(exc)

    if mix.soundcloud_url and mix.description_soundcloud:
        try:
            from app.services.soundcloud_uploader import SoundCloudUploader

            sc = SoundCloudUploader(
                db_settings_json=sj,
                on_tokens_refreshed=_sc_token_persister(app_settings),
            )
            await sc.update_description(mix.soundcloud_url, mix.description_soundcloud)
            updated["soundcloud"] = True
        except Exception as exc:
            logger.error("SoundCloud description update failed: %s", exc)
            errors["soundcloud"] = str(exc)

    logger.info(
        "Tracklist re-read for mix %s: %s tracks (%s); platform descriptions updated: %s; errors: %s",
        mix_id,
        analyze_result.get("tracks_found", 0),
        analyze_result.get("tracklist_source", "?"),
        updated,
        errors or "none",
    )

    return {
        "tracks_found": analyze_result.get("tracks_found"),
        "tracklist_source": analyze_result.get("tracklist_source"),
        "descriptions_updated": updated,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_all_handlers(orchestrator: PipelineOrchestrator) -> None:
    """Register every step handler on the given orchestrator instance."""
    orchestrator.register_handler("detect", handle_detect)
    orchestrator.register_handler("analyze", handle_analyze)
    orchestrator.register_handler("generate_description", handle_generate_description)
    orchestrator.register_handler("generate_art", handle_generate_art)
    orchestrator.register_handler("upload_soundcloud", handle_upload_soundcloud)
    orchestrator.register_handler("verify_soundcloud", handle_verify_soundcloud)
    orchestrator.register_handler("upload_youtube", handle_upload_youtube)
    orchestrator.register_handler("verify_youtube", handle_verify_youtube)
    orchestrator.register_handler("upload_mixcloud", handle_upload_mixcloud)
    orchestrator.register_handler("verify_mixcloud", handle_verify_mixcloud)
    orchestrator.register_handler("cross_link", handle_cross_link)

    logger.info("All pipeline handlers registered")
