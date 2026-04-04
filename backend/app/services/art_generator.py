"""Image generation service for SoundCloud cover art and YouTube thumbnails."""

import logging
import os
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AIUsage, BrandSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand visual defaults
# ---------------------------------------------------------------------------

DEFAULT_VISUAL_STYLE = (
    "Chunky pixel art style, psychedelic nature scene, vibrant colors, "
    "eyes everywhere watching from foliage, retro game aesthetic, "
    "thick bold pixels, lush vegetation with hidden creatures, "
    "trippy color palette, low-resolution rendered at high-resolution, "
    "no text, no watermarks, no logos"
)

DEFAULT_COLOR_PALETTE = [
    "#FF6B35",  # desert orange
    "#7B2D8E",  # psychedelic purple
    "#1B998B",  # jungle teal
    "#F7DC6F",  # golden sand
    "#E74C3C",  # hot red
    "#2ECC71",  # neon green
    "#3498DB",  # sky blue
    "#E91E63",  # magenta
]

DEFAULT_MOTIFS = [
    "pixel art eyes",
    "chunky vegetation",
    "psychedelic colors",
    "retro game aesthetic",
    "desert landscape elements",
    "trippy patterns",
    "hidden faces in nature",
]

GENRE_VISUAL_MODIFIERS: Dict[str, str] = {
    "house": "warm sunset, terrace vibes, palm trees, golden hour lighting, disco ball reflections",
    "techno": "dark industrial, concrete textures, strobe lights, underground bunker, smoke machines",
    "drum and bass": "neon jungle, fast motion blur, lightning strikes, urban nightscape, graffiti walls",
    "trance": "cosmic nebula, aurora borealis, crystal formations, ethereal glow, starfield",
    "dubstep": "heavy bass waveforms, cracked earth, seismic energy, dark neon, bass face skull",
    "ambient": "misty mountains, still water, bioluminescent forest, fog, gentle moonlight",
    "breakbeat": "shattered glass, kaleidoscope, street art, broken beat visualizer, urban chaos",
}


class ArtGenerator:
    """Generates cover art (1400x1400) and YouTube thumbnails (1920x1080)."""

    def __init__(self) -> None:
        self._fal_api_key = settings.FAL_API_KEY
        self._fal_model = settings.FAL_MODEL
        self._openai_api_key = settings.OPENAI_API_KEY

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_cover_art(
        self,
        mix_title: str,
        genres: List[str],
        vibes: List[str],
        output_path: str,
        session: Optional[AsyncSession] = None,
        mix_id: Optional[str] = None,
        brand_settings: Optional[BrandSettings] = None,
    ) -> str:
        """Generate 1400x1400 SoundCloud cover art. Returns the saved file path."""
        prompt = self._build_prompt(genres, vibes, brand_settings, aspect="square")
        logger.info("Generating cover art for '%s': %s", mix_title, prompt[:120])

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        image_url = await self._generate_with_fal(
            prompt, width=1400, height=1400, session=session, mix_id=mix_id,
        )
        if not image_url:
            logger.warning("fal.ai failed, falling back to DALL-E")
            image_url = await self._generate_with_dalle(
                prompt, size="1024x1024", session=session, mix_id=mix_id,
            )

        if not image_url:
            raise RuntimeError("All image generation providers failed")

        await self._download_and_save(image_url, output_path, resize=(1400, 1400))
        logger.info("Cover art saved to %s", output_path)
        return output_path

    async def generate_youtube_thumbnail(
        self,
        mix_title: str,
        genres: List[str],
        vibes: List[str],
        output_path: str,
        cover_art_path: Optional[str] = None,
        session: Optional[AsyncSession] = None,
        mix_id: Optional[str] = None,
        brand_settings: Optional[BrandSettings] = None,
    ) -> str:
        """Generate 1920x1080 YouTube thumbnail. Returns the saved file path."""
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        if cover_art_path and os.path.exists(cover_art_path):
            # Extend the cover art to 16:9
            thumbnail_url = await self._generate_wide_variant(
                genres, vibes, brand_settings, session, mix_id,
            )
            if thumbnail_url:
                await self._download_and_save(thumbnail_url, output_path, resize=(1920, 1080))
            else:
                # Fallback: letterbox the cover art
                self._letterbox_cover(cover_art_path, output_path)
        else:
            # Generate fresh wide image
            prompt = self._build_prompt(genres, vibes, brand_settings, aspect="wide")
            image_url = await self._generate_with_fal(
                prompt, width=1920, height=1080, session=session, mix_id=mix_id,
            )
            if not image_url:
                image_url = await self._generate_with_dalle(
                    prompt, size="1792x1024", session=session, mix_id=mix_id,
                )
            if not image_url:
                raise RuntimeError("All providers failed for thumbnail generation")
            await self._download_and_save(image_url, output_path, resize=(1920, 1080))

        # Overlay text
        self._overlay_text(output_path, mix_title, genres)
        logger.info("YouTube thumbnail saved to %s", output_path)
        return output_path

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        genres: List[str],
        vibes: List[str],
        brand_settings: Optional[BrandSettings],
        aspect: str = "square",
    ) -> str:
        # Base style
        style = DEFAULT_VISUAL_STYLE
        if brand_settings and brand_settings.visual_style:
            style = brand_settings.visual_style

        # Color palette
        palette = DEFAULT_COLOR_PALETTE
        if brand_settings and brand_settings.color_palette:
            palette = brand_settings.color_palette

        # Motifs
        motifs = DEFAULT_MOTIFS
        if brand_settings and brand_settings.motifs:
            motifs = brand_settings.motifs

        # Genre modifiers
        genre_mods_map = GENRE_VISUAL_MODIFIERS
        if brand_settings and brand_settings.genre_visual_modifiers:
            genre_mods_map = {**GENRE_VISUAL_MODIFIERS, **brand_settings.genre_visual_modifiers}

        genre_modifier_parts: List[str] = []
        for g in genres:
            g_lower = g.lower()
            if g_lower in genre_mods_map:
                genre_modifier_parts.append(genre_mods_map[g_lower])

        # Compose
        parts = [style]
        if genre_modifier_parts:
            parts.append(", ".join(genre_modifier_parts))
        if vibes:
            parts.append(f"mood: {', '.join(vibes)}")
        parts.append(f"color palette inspiration: {', '.join(palette[:5])}")
        parts.append(f"motifs: {', '.join(motifs[:5])}")

        if aspect == "wide":
            parts.append("wide cinematic composition, 16:9 aspect ratio, panoramic scene")
        else:
            parts.append("square composition, centered subject")

        return ", ".join(parts)

    # ------------------------------------------------------------------
    # fal.ai provider
    # ------------------------------------------------------------------

    async def _generate_with_fal(
        self,
        prompt: str,
        width: int,
        height: int,
        session: Optional[AsyncSession] = None,
        mix_id: Optional[str] = None,
    ) -> Optional[str]:
        if not self._fal_api_key:
            logger.warning("FAL_API_KEY not set, skipping fal.ai")
            return None

        url = f"https://queue.fal.run/{self._fal_model}"
        headers = {
            "Authorization": f"Key {self._fal_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": prompt,
            "image_size": {"width": width, "height": height},
            "num_images": 1,
            "safety_tolerance": "5",
        }

        async with httpx.AsyncClient(timeout=120) as client:
            # Submit to queue
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            result = resp.json()

            # fal.ai queue API: may return request_id for polling or direct result
            if "request_id" in result:
                # Use URLs from the response (they handle model path correctly)
                status_url = result.get("status_url", f"https://queue.fal.run/{self._fal_model}/requests/{result['request_id']}/status")
                result_url = result.get("response_url", f"https://queue.fal.run/{self._fal_model}/requests/{result['request_id']}")

                # Poll for completion
                import asyncio
                for attempt in range(60):  # up to 5 minutes
                    await asyncio.sleep(5)
                    try:
                        status_resp = await client.get(status_url, headers=headers)
                        if status_resp.status_code != 200 or not status_resp.text.strip():
                            logger.debug("fal.ai status poll %d: HTTP %s (empty or error)", attempt, status_resp.status_code)
                            continue
                        status_data = status_resp.json()
                    except Exception as poll_exc:
                        logger.debug("fal.ai status poll %d error: %s", attempt, poll_exc)
                        continue

                    status_val = status_data.get("status", "").upper()
                    if status_val == "COMPLETED":
                        result_resp = await client.get(result_url, headers=headers)
                        result = result_resp.json()
                        break
                    elif status_val in ("FAILED", "CANCELLED"):
                        logger.error("fal.ai generation failed: %s", status_data)
                        return None
                    # IN_QUEUE or IN_PROGRESS — keep polling
                else:
                    logger.error("fal.ai generation timed out after 60 polls")
                    return None

            images = result.get("images") or result.get("output", {}).get("images", [])
            if not images:
                logger.error("No images returned from fal.ai: %s", result)
                return None

            image_url = images[0].get("url") or images[0]

        # Track cost (fal.ai Flux Pro ~$0.05 per image)
        if session and mix_id:
            usage = AIUsage(
                mix_id=mix_id,
                provider="fal",
                model=self._fal_model,
                operation="image_generation",
                cost_usd=0.05,
            )
            session.add(usage)

        return image_url

    # ------------------------------------------------------------------
    # DALL-E fallback
    # ------------------------------------------------------------------

    async def _generate_with_dalle(
        self,
        prompt: str,
        size: str = "1024x1024",
        session: Optional[AsyncSession] = None,
        mix_id: Optional[str] = None,
    ) -> Optional[str]:
        if not self._openai_api_key:
            logger.warning("OPENAI_API_KEY not set, skipping DALL-E fallback")
            return None

        try:
            import openai
            client = openai.AsyncOpenAI(api_key=self._openai_api_key)
            response = await client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size=size,
                quality="standard",
                n=1,
            )
            image_url = response.data[0].url

            # Track cost (DALL-E 3 standard ~$0.04 for 1024x1024)
            cost = 0.08 if "1792" in size else 0.04
            if session and mix_id:
                usage = AIUsage(
                    mix_id=mix_id,
                    provider="openai",
                    model="dall-e-3",
                    operation="image_generation",
                    cost_usd=cost,
                )
                session.add(usage)

            return image_url
        except Exception as exc:
            logger.error("DALL-E generation failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Wide variant for thumbnail
    # ------------------------------------------------------------------

    async def _generate_wide_variant(
        self,
        genres: List[str],
        vibes: List[str],
        brand_settings: Optional[BrandSettings],
        session: Optional[AsyncSession],
        mix_id: Optional[str],
    ) -> Optional[str]:
        prompt = self._build_prompt(genres, vibes, brand_settings, aspect="wide")
        url = await self._generate_with_fal(
            prompt, width=1920, height=1080, session=session, mix_id=mix_id,
        )
        if not url:
            url = await self._generate_with_dalle(
                prompt, size="1792x1024", session=session, mix_id=mix_id,
            )
        return url

    # ------------------------------------------------------------------
    # Image utilities
    # ------------------------------------------------------------------

    async def _download_and_save(
        self, url: str, output_path: str, resize: Optional[Tuple[int, int]] = None
    ) -> None:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content

        img = Image.open(BytesIO(data))
        if resize:
            img = img.resize(resize, Image.LANCZOS)
        img.save(output_path, quality=95)

    def _letterbox_cover(self, cover_path: str, output_path: str) -> None:
        """Create a 1920x1080 thumbnail by centering the square art on a blurred background."""
        cover = Image.open(cover_path).convert("RGB")

        # Create blurred background
        bg = cover.resize((1920, 1080), Image.LANCZOS)
        try:
            from PIL import ImageFilter
            bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
        except Exception:
            pass

        # Darken the background
        from PIL import ImageEnhance
        enhancer = ImageEnhance.Brightness(bg)
        bg = enhancer.enhance(0.4)

        # Paste centered cover
        cover_resized = cover.resize((1080, 1080), Image.LANCZOS)
        x_offset = (1920 - 1080) // 2
        bg.paste(cover_resized, (x_offset, 0))
        bg.save(output_path, quality=95)

    def _overlay_text(
        self, image_path: str, title: str, genres: List[str]
    ) -> None:
        """Overlay title and genre text on the thumbnail in pixel-art style."""
        img = Image.open(image_path).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Try to load a pixel/bitmap font, fall back to default
        title_font = self._get_font(size=64)
        genre_font = self._get_font(size=36)

        # Semi-transparent overlay at bottom
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle(
            [(0, img.height - 200), (img.width, img.height)],
            fill=(0, 0, 0, 160),
        )
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Title text
        title_y = img.height - 170
        self._draw_text_with_shadow(draw, title, (60, title_y), title_font, fill="white")

        # Genre text
        genre_text = " / ".join(g.title() for g in genres[:3])
        genre_y = img.height - 80
        self._draw_text_with_shadow(draw, genre_text, (60, genre_y), genre_font, fill="#FF6B35")

        img.save(image_path, quality=95)

    def _draw_text_with_shadow(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        position: Tuple[int, int],
        font: Any,
        fill: str = "white",
        shadow_color: str = "black",
        shadow_offset: int = 3,
    ) -> None:
        x, y = position
        # Shadow
        draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=shadow_color)
        # Main text
        draw.text((x, y), text, font=font, fill=fill)

    def _get_font(self, size: int) -> Any:
        """Try to load a good font, fall back to default."""
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    continue
        try:
            return ImageFont.truetype("arial.ttf", size)
        except Exception:
            return ImageFont.load_default()
