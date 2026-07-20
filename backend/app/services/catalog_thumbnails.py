"""Back-catalog thumbnail regeneration in the Will-approved brand design.

``run_regen_thumbnails`` (behind ``POST /api/catalog/regen-thumbnails``)
re-renders platform art for cataloged mixes with the design system in
:mod:`thumbnail_design`:

* a new-style 1280x720 YouTube thumbnail for every target mix that has a
  ``youtube_video_id``;
* a new-style 1400x1400 SoundCloud cover for every target with a
  ``soundcloud_track_id``.

Files land in the standard ``/output`` art paths and each successful render
becomes an APPROVED ``field="thumbnail"`` :class:`MixProposal` — the existing
apply worker pushes them to the platforms (thumbnails.set / artwork_data).
The apply worker is NOT auto-run.

Targets: an explicit mix-id list, ``"all"`` (every cataloged mix on a
platform), or ``"raid-trains"`` (mixes whose title matches the raid-train
pattern). Progress lands in ``catalog_regen_thumbs`` activity events; the run
summary persists under ``AppSettings.settings_json["catalog_last_regen_thumbs"]``.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import BrandSettings, Mix, MixProposal
from app.services import activity_log, thumbnail_design
from app.services.art_generator import ArtGenerator

logger = logging.getLogger(__name__)

LAST_REGEN_THUMBS_KEY = "catalog_last_regen_thumbs"

RAID_TRAIN_RE = re.compile(r"raid.?train", re.IGNORECASE)


# Factory hook (monkeypatchable in tests).
def get_art_generator(db_settings_json: Optional[Dict[str, Any]]) -> ArtGenerator:
    return ArtGenerator(db_settings_json)


async def _unique_design(mix, session, sj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Per-mix unique hook + scene via the uniqueness engine, committed before
    the long-running fal generation so sqlite's write lock is never held across
    that await. Never fails the regen — None falls back to motif defaults."""
    try:
        design = await thumbnail_design.unique_design_for_mix(mix, session, sj)
        await session.commit()
        return design
    except Exception:  # pragma: no cover - defensive
        logger.exception("Unique design generation failed for mix %s", mix.id)
        return None


def _on_platform(mix: Mix) -> bool:
    return bool(
        mix.youtube_video_id or mix.youtube_url
        or mix.soundcloud_track_id or mix.soundcloud_url
    )


async def _has_open_thumbnail_proposal(session, mix_id: str, platform: str) -> bool:
    result = await session.execute(
        select(MixProposal.id).where(
            MixProposal.mix_id == mix_id,
            MixProposal.field == "thumbnail",
            MixProposal.platform == platform,
            MixProposal.status.in_(["draft", "approved", "applying"]),
        )
    )
    return result.first() is not None


async def run_regen_thumbnails(
    mix_ids: Union[List[str], str, None],
    force_unique: bool = True,
) -> Dict[str, Any]:
    """Regenerate brand-design art for the targeted mixes. Returns the summary.

    ``force_unique`` (default on) routes every render through the uniqueness
    engine: a per-mix LLM hook and hash-varied scene, both enforced unique by
    the ``used_creative`` registry (the mix's own previous claims are released
    first). ``False`` restores the legacy fixed per-genre motif hook/scene.
    """
    from app.services.catalog_sync import _get_settings_row

    started_at = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "status": "running",
        "errors": [],
        "targeted": 0,
        "generated_youtube": 0,
        "generated_soundcloud": 0,
        "proposals_created": 0,
        "skipped": 0,
        "failed": 0,
    }
    await activity_log.info(
        "catalog_regen_thumbs", "Brand thumbnail regeneration started.",
    )

    try:
        async with async_session_factory() as session:
            settings_row = await _get_settings_row(session)
            sj = dict(settings_row.settings_json or {})

            brand = (
                await session.execute(select(BrandSettings).where(BrandSettings.id == 1))
            ).scalar_one_or_none()

            query = select(Mix)
            if isinstance(mix_ids, list):
                query = query.where(Mix.id.in_(mix_ids))
            rows = list((await session.execute(query)).scalars().all())
            targets = [m for m in rows if _on_platform(m)]
            if mix_ids == "raid-trains":
                targets = [m for m in targets if RAID_TRAIN_RE.search(m.title or "")]
            target_ids = [m.id for m in targets]
            await session.commit()
        summary["targeted"] = len(target_ids)

        generator = get_art_generator(sj)
        os.makedirs(settings.OUTPUT_THUMBNAILS_PATH, exist_ok=True)
        os.makedirs(settings.OUTPUT_COVER_ART_PATH, exist_ok=True)

        total = len(target_ids)
        for n, mix_id in enumerate(target_ids, start=1):
            async with async_session_factory() as session:
                mix = await session.get(Mix, mix_id)
                if mix is None:
                    continue
                genres = mix.genres or ["electronic"]
                vibes = mix.vibes or []

                design: Optional[Dict[str, Any]] = None
                if force_unique:
                    design = await _unique_design(mix, session, sj)
                hook_text = design["hook"] if design else None
                scene_text = design["scene"] if design else None

                made_any = False
                try:
                    if mix.youtube_video_id:
                        if await _has_open_thumbnail_proposal(session, mix.id, "youtube"):
                            summary["skipped"] += 1
                        else:
                            thumb_path = os.path.join(
                                settings.OUTPUT_THUMBNAILS_PATH, f"{mix.id}.jpg"
                            )
                            await generator.generate_youtube_thumbnail(
                                mix_title=mix.title_youtube or mix.title,
                                genres=genres,
                                vibes=vibes,
                                output_path=thumb_path,
                                session=session,
                                mix_id=mix.id,
                                brand_settings=brand,
                                hook_text=hook_text,
                                scene_text=scene_text,
                            )
                            session.add(
                                MixProposal(
                                    mix_id=mix.id,
                                    platform="youtube",
                                    field="thumbnail",
                                    current_value=mix.thumbnail_path,
                                    proposed_value=thumb_path,
                                    status="approved",
                                    created_by="ai",
                                )
                            )
                            summary["generated_youtube"] += 1
                            summary["proposals_created"] += 1
                            made_any = True
                            # Commit before the next long-running generate so
                            # sqlite's write lock is never held across an await
                            # on fal (autoflush from the SC proposal check
                            # would otherwise flush these pending rows first).
                            await session.commit()

                    if mix.soundcloud_track_id:
                        if await _has_open_thumbnail_proposal(
                            session, mix.id, "soundcloud"
                        ):
                            summary["skipped"] += 1
                        else:
                            cover_path = os.path.join(
                                settings.OUTPUT_COVER_ART_PATH, f"{mix.id}.jpg"
                            )
                            await generator.generate_cover_art(
                                mix_title=mix.title,
                                genres=genres,
                                vibes=vibes,
                                output_path=cover_path,
                                session=session,
                                mix_id=mix.id,
                                brand_settings=brand,
                                hook_text=hook_text,
                                scene_text=scene_text,
                            )
                            session.add(
                                MixProposal(
                                    mix_id=mix.id,
                                    platform="soundcloud",
                                    field="thumbnail",
                                    current_value=mix.cover_art_path,
                                    proposed_value=cover_path,
                                    status="approved",
                                    created_by="ai",
                                )
                            )
                            summary["generated_soundcloud"] += 1
                            summary["proposals_created"] += 1
                            made_any = True
                except Exception as exc:
                    logger.exception("Thumbnail regen failed for mix %s", mix.id)
                    summary["failed"] += 1
                    summary["errors"].append(f"{mix.title}: {exc}")
                    await session.commit()
                    await activity_log.error(
                        "catalog_regen_thumbs",
                        f"Thumbnail regen failed for {mix.title}: {exc}",
                        mix_id=mix.id,
                    )
                    continue

                await session.commit()
                if made_any:
                    await activity_log.info(
                        "catalog_regen_thumbs",
                        f"Regenerated brand art {n}/{total}: {mix.title}",
                        mix_id=mix.id,
                        context={"n": n, "total": total},
                    )

        summary["status"] = "ok" if not summary["errors"] else "partial"
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("Thumbnail regeneration failed")
        summary["status"] = "failed"
        summary["errors"].append(str(exc))

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()

    try:
        async with async_session_factory() as session:
            settings_row = await _get_settings_row(session)
            merged = dict(settings_row.settings_json or {})
            merged[LAST_REGEN_THUMBS_KEY] = summary
            settings_row.settings_json = merged
            await session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to persist thumbnail regen summary")

    level = activity_log.info if summary["status"] == "ok" else activity_log.warn
    await level(
        "catalog_regen_thumbs",
        (
            f"Brand thumbnail regen {summary['status']}: "
            f"{summary['generated_youtube']} YouTube + "
            f"{summary['generated_soundcloud']} SoundCloud renders, "
            f"{summary['proposals_created']} approved thumbnail proposals, "
            f"{summary['failed']} failed."
        ),
        context=summary,
    )
    return summary
