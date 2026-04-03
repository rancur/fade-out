"""Brand settings and AI preview endpoints."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import BrandSettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brand", tags=["brand"])


# --- Schemas ---

class BrandSettingsOut(BaseModel):
    id: int = 1
    brand_name: Optional[str] = "Will See"
    description_template: Optional[str] = None
    color_palette: Optional[list] = None
    visual_style: Optional[str] = None
    motifs: Optional[list] = None
    genre_visual_modifiers: Optional[dict] = None
    title_format: Optional[str] = None
    youtube_playlists: Optional[dict] = None
    soundcloud_links: Optional[str] = None
    youtube_links: Optional[str] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class BrandSettingsUpdate(BaseModel):
    brand_name: Optional[str] = None
    description_template: Optional[str] = None
    color_palette: Optional[list] = None
    visual_style: Optional[str] = None
    motifs: Optional[list] = None
    genre_visual_modifiers: Optional[dict] = None
    title_format: Optional[str] = None
    youtube_playlists: Optional[dict] = None
    soundcloud_links: Optional[str] = None
    youtube_links: Optional[str] = None


class PreviewDescriptionRequest(BaseModel):
    genres: List[str]
    vibes: List[str]
    tracklist: Optional[List[Dict[str, Any]]] = None
    title: str


class PreviewDescriptionResponse(BaseModel):
    soundcloud: str
    youtube: str


class PreviewArtRequest(BaseModel):
    genres: List[str]
    vibes: List[str]
    title: str


class PreviewArtResponse(BaseModel):
    prompt: str
    image_url: Optional[str] = None


# --- Helpers ---

async def _get_or_create_brand(db: AsyncSession) -> BrandSettings:
    """Get or create the singleton brand settings row."""
    result = await db.execute(select(BrandSettings).where(BrandSettings.id == 1))
    row = result.scalar_one_or_none()
    if not row:
        row = BrandSettings(id=1)
        db.add(row)
        await db.flush()
    return row


# --- Endpoints ---

@router.get("", response_model=BrandSettingsOut)
async def get_brand_settings(db: AsyncSession = Depends(get_db)):
    """Get brand settings, creating defaults if none exist."""
    row = await _get_or_create_brand(db)
    return BrandSettingsOut.model_validate(row)


@router.put("", response_model=BrandSettingsOut)
async def update_brand_settings(body: BrandSettingsUpdate, db: AsyncSession = Depends(get_db)):
    """Update brand settings."""
    row = await _get_or_create_brand(db)
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(row, key, value)
    await db.flush()
    return BrandSettingsOut.model_validate(row)


@router.post("/preview-description", response_model=PreviewDescriptionResponse)
async def preview_description(body: PreviewDescriptionRequest, db: AsyncSession = Depends(get_db)):
    """Generate a preview description using the description generator (not saved to any mix)."""
    from app.services.description_generator import DescriptionGenerator

    brand = await _get_or_create_brand(db)
    generator = DescriptionGenerator()

    try:
        soundcloud_desc = await generator.generate_soundcloud_description(
            mix_title=body.title,
            genres=body.genres,
            vibes=body.vibes,
            tracklist=body.tracklist,
            brand_settings=brand,
        )
        youtube_desc = await generator.generate_youtube_description(
            mix_title=body.title,
            genres=body.genres,
            vibes=body.vibes,
            tracklist=body.tracklist,
            brand_settings=brand,
        )
    except Exception as exc:
        logger.exception("Description preview failed")
        raise HTTPException(status_code=502, detail=f"AI generation failed: {exc}")

    return PreviewDescriptionResponse(soundcloud=soundcloud_desc, youtube=youtube_desc)


@router.post("/preview-art", response_model=PreviewArtResponse)
async def preview_art(body: PreviewArtRequest, db: AsyncSession = Depends(get_db)):
    """Generate a preview art prompt and optionally an image URL (not saved)."""
    from app.services.art_generator import ArtGenerator

    brand = await _get_or_create_brand(db)
    generator = ArtGenerator()

    # Build the prompt so the user can see what would be generated
    prompt = generator._build_prompt(body.genres, body.vibes, brand, aspect="square")

    # Attempt actual image generation if API keys are configured
    image_url: Optional[str] = None
    try:
        image_url = await generator._generate_with_fal(
            prompt, width=1400, height=1400,
        )
        if not image_url:
            image_url = await generator._generate_with_dalle(prompt, size="1024x1024")
    except Exception as exc:
        logger.warning("Art preview generation failed (returning prompt only): %s", exc)

    return PreviewArtResponse(prompt=prompt, image_url=image_url)
