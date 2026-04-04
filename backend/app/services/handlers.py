"""Pipeline step handlers -- glue between the orchestrator and actual services."""

import logging
import os
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AppSettings, BrandSettings, Mix
from app.services.pipeline import PipelineOrchestrator, VideoNotReady

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_mix(mix_id: str, session: AsyncSession) -> Mix:
    """Load a Mix or raise if not found."""
    mix = await session.get(Mix, mix_id)
    if mix is None:
        raise RuntimeError(f"Mix {mix_id} not found")
    return mix


async def _get_brand_settings(session: AsyncSession) -> Optional[BrandSettings]:
    """Load brand settings (row id=1) if they exist."""
    return await session.get(BrandSettings, 1)


async def _get_app_settings(session: AsyncSession) -> Optional[AppSettings]:
    """Load app settings (row id=1) if they exist."""
    return await session.get(AppSettings, 1)


def _title_from_filename(audio_path: str) -> str:
    """Derive a human-friendly title from a filename."""
    stem = Path(audio_path).stem
    # Replace common separators with spaces
    for ch in ("_", "-", "."):
        stem = stem.replace(ch, " ")
    return stem.strip().title()


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

async def handle_analyze(mix_id: str, session: AsyncSession) -> Optional[dict]:
    """Run audio analysis and merge with CUE/DJCTL data if available."""
    mix = await _get_mix(mix_id, session)

    if not mix.audio_file_path:
        raise RuntimeError("No audio file path on mix")

    from app.services.audio_analyzer import AudioAnalyzer
    from app.services.djctl_integration import (
        find_cue_for_audio,
        merge_tracklists,
        parse_cue_file,
    )

    analyzer = AudioAnalyzer()
    result = await analyzer.analyze(mix.audio_file_path)

    # Try to merge with CUE-sheet tracklist
    cue_path = find_cue_for_audio(mix.audio_file_path)
    cue_tracks = parse_cue_file(cue_path) if cue_path else None

    merged = merge_tracklists(
        cue_tracks=cue_tracks,
        shazam_tracks=result.tracklist,
    )
    final_tracklist = merged.tracklist if merged.tracklist else result.tracklist

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
        "tracklist_source": merged.source,
        "duration_seconds": result.duration_seconds,
        "youtube_timestamp_offset": yt_offset,
    }


# ---------------------------------------------------------------------------
# generate_description
# ---------------------------------------------------------------------------

async def handle_generate_description(
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Generate descriptions, YouTube title, and tags."""
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

    from app.services.description_generator import DescriptionGenerator
    from app.services.tag_generator import TagGenerator

    app_settings = await _get_app_settings(session)
    sj = (app_settings.settings_json or {}) if app_settings else {}
    desc_gen = DescriptionGenerator(db_settings_json=sj)
    tag_gen = TagGenerator()

    # Generate a creative SoundCloud title
    raw_filename = Path(mix.audio_file_path).stem if mix.audio_file_path else "mix"
    creative_title = await desc_gen.generate_creative_title(
        genres=genres,
        vibes=vibes,
        tracklist=tracklist,
        filename=raw_filename,
        session=session,
        mix_id=mix_id,
    )
    mix.title = creative_title

    # Generate all content
    sc_desc = await desc_gen.generate_soundcloud_description(
        mix_title=mix.title,
        genres=genres,
        vibes=vibes,
        tracklist=tracklist,
        energy_profile=energy_profile,
        bpm_range=bpm_range,
        duration_seconds=duration,
        session=session,
        mix_id=mix_id,
        brand_settings=brand,
    )

    yt_offset = mix.youtube_timestamp_offset or 0.0
    yt_desc = await desc_gen.generate_youtube_description(
        mix_title=mix.title,
        genres=genres,
        vibes=vibes,
        tracklist=tracklist,
        energy_profile=energy_profile,
        bpm_range=bpm_range,
        duration_seconds=duration,
        session=session,
        mix_id=mix_id,
        brand_settings=brand,
        youtube_timestamp_offset=yt_offset,
    )

    yt_title = await desc_gen.generate_youtube_title(
        genres=genres,
        vibes=vibes,
        bpm_range=bpm_range,
        duration_seconds=duration,
        session=session,
        mix_id=mix_id,
    )

    tags = tag_gen.generate(
        genres=genres,
        vibes=vibes,
        tracklist=tracklist,
    )

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
    """Generate cover art and YouTube thumbnail."""
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

    from app.services.art_generator import ArtGenerator

    app_settings_art = await _get_app_settings(session)
    sj_art = (app_settings_art.settings_json or {}) if app_settings_art else {}
    art_gen = ArtGenerator(db_settings_json=sj_art)

    cover_result = await art_gen.generate_cover_art(
        mix_title=mix.title,
        genres=genres,
        vibes=vibes,
        output_path=cover_path,
        session=session,
        mix_id=mix_id,
        brand_settings=brand,
    )

    thumb_result = await art_gen.generate_youtube_thumbnail(
        mix_title=mix.title_youtube or mix.title,
        genres=genres,
        vibes=vibes,
        output_path=thumb_path,
        cover_art_path=cover_path,
        session=session,
        mix_id=mix_id,
        brand_settings=brand,
    )

    mix.cover_art_path = cover_result
    mix.thumbnail_path = thumb_result

    return {
        "cover_art_path": cover_result,
        "thumbnail_path": thumb_result,
    }


# ---------------------------------------------------------------------------
# upload_soundcloud
# ---------------------------------------------------------------------------

async def handle_upload_soundcloud(
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Upload the mix to SoundCloud."""
    mix = await _get_mix(mix_id, session)

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
    uploader = SoundCloudUploader(db_settings_json=sj_sc)
    permalink = await uploader.upload(
        audio_path=mix.audio_file_path,
        title=mix.title,
        description=mix.description_soundcloud or "",
        genre=genre_label,
        tags=tags,
        cover_art_path=mix.cover_art_path,
    )

    mix.soundcloud_url = permalink
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
    uploader = SoundCloudUploader(db_settings_json=sj_v)
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
    mix_id: str, session: AsyncSession
) -> Optional[dict]:
    """Upload the video to YouTube."""
    mix = await _get_mix(mix_id, session)

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

    from app.services.tag_generator import TagGenerator
    from app.services.youtube_uploader import YouTubeUploader

    tag_gen = TagGenerator()
    genres = mix.genres or ["electronic"]
    tags = mix.tags or tag_gen.generate(genres=genres, vibes=mix.vibes or [])

    # Determine premiere mode from app settings or config
    premiere_mode = settings.PREMIERE_MODE
    if app_settings and app_settings.premiere_mode:
        premiere_mode = app_settings.premiere_mode

    sj_yt = (app_settings.settings_json or {}) if app_settings else {}
    uploader = YouTubeUploader(db_settings_json=sj_yt)
    result = await uploader.upload(
        video_path=mix.video_file_path,
        title=mix.title_youtube or mix.title,
        description=mix.description_youtube or "",
        tags=tag_gen.format_for_youtube(tags),
        thumbnail_path=mix.thumbnail_path,
        premiere_mode=premiere_mode,
        genre_for_playlist=genres[0] if genres else None,
    )

    mix.youtube_url = result["video_url"]
    if result.get("playlist_id"):
        mix.youtube_playlist_id = result["playlist_id"]

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

    # Extract video ID from URL
    video_id = mix.youtube_url.split("v=")[-1].split("&")[0]

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

    # For now, log the intent. Full cross-linking requires API calls to update
    # existing descriptions. The generated descriptions already contain
    # placeholder link sections, so this is a best-effort step.

    if sc_url and yt_url:
        # Update SoundCloud description to include YouTube link
        if mix.description_soundcloud and yt_url not in mix.description_soundcloud:
            mix.description_soundcloud += f"\n\nWatch on YouTube: {yt_url}"
            updated.append("soundcloud_description")

        # Update YouTube description to include SoundCloud link
        if mix.description_youtube and sc_url not in mix.description_youtube:
            mix.description_youtube += f"\n\nListen on SoundCloud: {sc_url}"
            updated.append("youtube_description")

        # Note: actually pushing these updates to the platforms would require
        # additional API calls (SoundCloud PUT /tracks/:id, YouTube videos.update).
        # That can be wired in later once the basic flow is stable.
        if updated:
            logger.info(
                "Cross-linked descriptions for mix %s (local update): %s",
                mix_id,
                ", ".join(updated),
            )
    else:
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
    orchestrator.register_handler("cross_link", handle_cross_link)

    logger.info("All pipeline handlers registered")
