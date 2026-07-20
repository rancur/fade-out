"""Image generation service for SoundCloud cover art and YouTube thumbnails."""

import asyncio
import logging
import os
import subprocess
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AIUsage, BrandSettings
from app.services import thumbnail_design

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand visual defaults
# ---------------------------------------------------------------------------

# Brand base is the pixel-art / psychedelic / all-seeing-eyes aesthetic Will
# likes -- but GENRE-NEUTRAL. The old default hard-coded a "psychedelic nature
# scene ... eyes in foliage ... lush vegetation", which forced EVERY mix into a
# palm-tree/jungle landscape (wrong for bass music). The concrete environment
# now comes from GENRE_VISUAL_MODIFIERS so a DnB set reads as a dark neon
# rave -- not a tropical lagoon.
DEFAULT_VISUAL_STYLE = (
    "Chunky pixel art style, psychedelic and vibrant, glowing neon colors, "
    "eyes everywhere watching, retro game aesthetic, thick bold pixels, "
    "trippy color palette, high-energy electronic music artwork, "
    "low-resolution rendered at high-resolution, no text, no watermarks, no logos"
)

DEFAULT_COLOR_PALETTE = [
    "#FF6B35",  # hot orange
    "#7B2D8E",  # psychedelic purple
    "#1B998B",  # electric teal
    "#F7DC6F",  # golden
    "#E74C3C",  # hot red
    "#2ECC71",  # neon green
    "#3498DB",  # electric blue
    "#E91E63",  # magenta
]

DEFAULT_MOTIFS = [
    "pixel art eyes",
    "psychedelic colors",
    "retro game aesthetic",
    "glowing neon",
    "trippy patterns",
    "bold geometric shapes",
]

GENRE_VISUAL_MODIFIERS: Dict[str, str] = {
    "house": "warm sunset, rooftop terrace vibes, golden hour lighting, disco ball reflections, dancing crowd",
    "techno": "dark industrial, concrete textures, strobe lights, underground bunker, smoke machines",
    "drum and bass": "dark neon cityscape at night, glowing subwoofers and bass bins, laser grids, fast motion blur, lightning arcs, electric blue purple and green, futuristic rave energy",
    "trance": "cosmic nebula, aurora borealis, crystal formations, ethereal glow, starfield",
    "dubstep": "massive bass waveforms, cracked concrete, seismic shockwaves, glitch distortion, dark aggressive neon, bass-face skull",
    "trap": "gritty urban night, purple and gold haze, heavy 808 sub-bass energy, neon signs, smoky trap house",
    "garage": "sleek UK garage club, chrome and neon, shuffling dancefloor lights, deep blues and magenta",
    "ambient": "misty mountains, still water, bioluminescent glow, fog, gentle moonlight",
    "breakbeat": "shattered glass, kaleidoscope, street art, broken-beat visualizer, vivid neon, urban chaos",
    "electronic": "glowing synthwave grid, neon geometric shapes, laser lights, futuristic club energy, vibrant electric colors",
}


class ArtGenerator:
    """Generates cover art (1400x1400) and YouTube thumbnails (1920x1080)."""

    def __init__(self, db_settings_json: Optional[Dict[str, Any]] = None) -> None:
        sj = db_settings_json or {}
        self._fal_api_key = sj.get("fal_api_key") or settings.FAL_API_KEY
        self._fal_model = settings.FAL_MODEL
        self._openai_api_key = sj.get("openai_api_key") or settings.OPENAI_API_KEY

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
        hook_text: Optional[str] = None,
        scene_text: Optional[str] = None,
    ) -> str:
        """Generate 1400x1400 SoundCloud cover art (brand design system).

        The scene comes from the Will-approved genre motif (desert-psychedelic
        focal subject + eye, square composition with the upper third kept for
        text) and the deterministic pixel-font overlay is composed on top with
        the text centered in the upper third. ``hook_text`` overrides the
        motif's default <=3-word hook and ``scene_text`` overrides the motif's
        base scene (per-mix uniqueness engine). Returns the saved file path.
        """
        genre_key = thumbnail_design.resolve_genre_key(genres)
        motif = thumbnail_design.GENRE_MOTIFS[genre_key]
        hook = hook_text or str(motif["hook"])
        prompt = thumbnail_design.build_scene_prompt(
            genre_key, brand_settings, aspect="square", scene_override=scene_text
        )
        logger.info("Generating cover art for '%s' (%s): %s",
                    mix_title, genre_key, prompt[:120])

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
        thumbnail_design.compose_thumbnail(
            output_path, hook, motif["accent"], output_path, size=(1400, 1400),
        )
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
        video_file_path: Optional[str] = None,
        energy_profile: Optional[List[Dict[str, Any]]] = None,
        duration_seconds: float = 0.0,
        hook_text: Optional[str] = None,
        scene_text: Optional[str] = None,
    ) -> str:
        """Generate a 1280x720 YouTube thumbnail. Returns the saved file path.

        Strategy, in order:
          1. PRIMARY -- the Will-approved brand design system
             (:mod:`thumbnail_design`): a fal-generated (-> DALL-E fallback)
             desert-psychedelic scene picked by the mix's genre motif, with
             the deterministic pixel-font hook overlay composed on top.
          2. FALLBACK -- a frame grabbed from the paired video at a peak-energy
             timestamp, if the AI providers fail.
          3. FALLBACK -- letterboxed cover art as a last resort.
        The brand overlay (``compose_thumbnail``) runs in every case;
        ``hook_text`` overrides the motif's default <=3-word hook and
        ``scene_text`` overrides the motif's base scene (per-mix uniqueness).
        """
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        genre_key = thumbnail_design.resolve_genre_key(genres)
        motif = thumbnail_design.GENRE_MOTIFS[genre_key]
        hook = hook_text or str(motif["hook"])
        accent = motif["accent"]

        # 1. Primary: brand design system scene + overlay.
        prompt = thumbnail_design.build_scene_prompt(
            genre_key, brand_settings, aspect="wide", scene_override=scene_text
        )
        image_url = await self._generate_with_fal(
            prompt, width=1280, height=720, session=session, mix_id=mix_id,
        )
        if not image_url:
            image_url = await self._generate_with_dalle(
                prompt, size="1792x1024", session=session, mix_id=mix_id,
            )
        if image_url:
            await self._download_and_save(image_url, output_path, resize=(1280, 720))
            thumbnail_design.compose_thumbnail(
                output_path, hook, accent, output_path, size=(1280, 720),
            )
            logger.info("YouTube thumbnail (brand design) saved to %s", output_path)
            return output_path

        # 2. Fallback: frame grabbed from the paired video.
        if video_file_path and os.path.exists(video_file_path):
            ts = self._peak_energy_timestamp(energy_profile, duration_seconds)
            frame = await asyncio.to_thread(
                self._grab_video_frame, video_file_path, output_path, ts
            )
            if frame:
                thumbnail_design.compose_thumbnail(
                    output_path, hook, accent, output_path, size=(1280, 720),
                )
                logger.info(
                    "YouTube thumbnail from video frame at %.0fs saved to %s (AI fallback)",
                    ts, output_path,
                )
                return output_path

        # 3. Fallback: letterbox the cover art.
        if cover_art_path and os.path.exists(cover_art_path):
            self._letterbox_cover(cover_art_path, output_path)
            thumbnail_design.compose_thumbnail(
                output_path, hook, accent, output_path, size=(1280, 720),
            )
            logger.info("YouTube thumbnail (letterboxed cover) saved to %s", output_path)
            return output_path

        raise RuntimeError("All thumbnail generation strategies failed")

    # ------------------------------------------------------------------
    # Video-frame thumbnail helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _peak_energy_timestamp(
        energy_profile: Optional[List[Dict[str, Any]]], duration_seconds: float
    ) -> float:
        """Pick a lively timestamp to grab a frame from.

        Prefers the highest-RMS sampled point from the analyzer's energy curve;
        otherwise a sensible point ~35% in (past the intro), min 90s.
        """
        if energy_profile:
            try:
                peak = max(energy_profile, key=lambda p: p.get("rms", 0) or 0)
                ts = peak.get("timestamp_seconds")
                if ts:
                    return float(ts)
            except (ValueError, TypeError):
                pass
        if duration_seconds and duration_seconds > 240:
            return duration_seconds * 0.35
        return 90.0

    @staticmethod
    def _grab_video_frame(
        video_path: str, output_path: str, timestamp: float
    ) -> Optional[str]:
        """Extract a single 1920x1080 frame from the video at ``timestamp``.

        Scales-to-cover and centre-crops to 16:9 so any source aspect fills the
        thumbnail. Returns the output path on success, else ``None``.
        """
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(max(0.0, timestamp)),
            "-i", video_path,
            "-frames:v", "1",
            "-vf",
            "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
            "-q:v", "2",
            output_path,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("ffmpeg frame grab error: %s", exc)
            return None
        if result.returncode != 0:
            logger.warning("ffmpeg frame grab failed (rc=%s): %s",
                           result.returncode, (result.stderr or "")[-300:])
            return None
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.warning("ffmpeg produced no frame at %.0fs", timestamp)
            return None
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
        """Overlay title and genre text on the thumbnail with auto-scaling."""
        img = Image.open(image_path).convert("RGB")
        max_text_width = img.width - 120  # 60px margin on each side

        # Auto-scale title font to fit within the image width
        title_font = self._get_font(size=64)
        title_size = 64
        while title_size > 28:
            bbox = title_font.getbbox(title) if hasattr(title_font, 'getbbox') else (0, 0, title_size * len(title) * 0.6, title_size)
            text_width = bbox[2] - bbox[0] if bbox else title_size * len(title) * 0.6
            if text_width <= max_text_width:
                break
            title_size -= 4
            title_font = self._get_font(size=title_size)

        genre_font = self._get_font(size=36)
        genre_text = " / ".join(g.title() for g in genres[:3])

        # Calculate overlay height based on font sizes
        overlay_height = title_size + 60 + 50  # title + genre + padding
        overlay_top = img.height - overlay_height

        # Semi-transparent overlay at bottom
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle(
            [(0, overlay_top), (img.width, img.height)],
            fill=(0, 0, 0, 170),
        )
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Title text
        title_y = overlay_top + 20
        self._draw_text_with_shadow(draw, title, (60, title_y), title_font, fill="white")

        # Genre text
        genre_y = title_y + title_size + 15
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
