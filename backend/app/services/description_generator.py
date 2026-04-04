"""OpenAI-powered description generator for SoundCloud and YouTube."""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import openai
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AIUsage, BrandSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default prompt template
# ---------------------------------------------------------------------------

DEFAULT_DESCRIPTION_PROMPT = """\
You are a copywriter for the DJ brand "{brand_name}". Write a description for a DJ mix upload.

BRAND VOICE:
- Confident, psychedelic, cool
- No emojis, no fake hype, no clickbait
- Desert energy, genre-fluid, four decks, improvised feel
- Speak like someone who just got back from a late-night session in the desert
- Let the music speak -- don't oversell

GENRE TONE ADAPTATION:
- If house: warm, groovy, late-night terrace vibes
- If drum and bass: relentless, rolling, jungle fever
- If techno: industrial, hypnotic, warehouse darkness
- If trance: euphoric, melodic, otherworldly journey
- If dubstep: heavy, filthy, bass-face mandatory
- If ambient: meditative, expansive, cosmic drift
- If mixed/multi-genre: genre-fluid, unpredictable, four-deck chaos

ENERGY MATCHING:
- Match the energy of the description to the energy of the mix
- Chill mix = chill description, chaotic mix = chaotic description

RULES:
- Start with the full mix title in the first sentence
- Clearly mention the genres
- Under 5000 characters total
- No code blocks, backticks, or indentation
- No markdown formatting
- Plain text only
- If a tracklist is provided, include it with timestamps
- Do NOT include any links, URLs, or references to links in the description body. No "Find us on", "Explore more", "Follow on", "Catch us on", or similar link introduction phrases. Links are appended automatically and separately. Do not write placeholder links like [example.com] either.
- Do NOT include a sign-off line like "— Will See" or any closing signature

MIX DATA:
- Title: {mix_title}
- Genres: {genres}
- Vibes: {vibes}
- Duration: {duration}
- BPM Range: {bpm_range}
- Energy Profile Summary: {energy_summary}
{tracklist_section}

PLATFORM: {platform}
Write for {platform}. {platform_link_instruction}

{custom_template}

Write the description now. Plain text, no markdown.\
"""

SOUNDCLOUD_LINK_INSTRUCTION = (
    "At the bottom, include links to: YouTube, Twitch, Website, Shop. "
    "Do NOT include a SoundCloud link (they are already on SoundCloud)."
)

YOUTUBE_LINK_INSTRUCTION = (
    "At the bottom, include links to: SoundCloud, Twitch, Website, Shop. "
    "Do NOT include a YouTube link (they are already on YouTube). "
    "Format the tracklist as YouTube chapters with timestamps starting at 0:00."
)

CREATIVE_TITLE_PROMPT = """\
Generate a creative, artistic title for a DJ mix by "{brand_name}" for SoundCloud.

VIBE:
- Psychedelic, evocative, poetic
- Should feel like a DJ mix name, NOT an SEO title
- Desert energy, late-night, otherworldly
- Think album names, art exhibitions, fever dreams
- Examples of the vibe: "Desert Frequencies Vol. III", "Four Decks and a Prayer", "Neon Cactus After Dark", "The Eye Opens at Midnight", "Silk and Static", "Phantom Groove Theory"

RULES:
- Under 60 characters
- No dates
- Do not include "DJ mix", "set", or "live" in the title
- Incorporate the genres and vibes naturally but artistically
- Do NOT just list genres -- weave them into something evocative
- One title only, no alternatives, no explanation

MIX DATA:
- Raw filename: {filename}
- Genres: {genres}
- Vibes: {vibes}
{tracklist_hint}

Return ONLY the title, nothing else.\
"""

YOUTUBE_TITLE_PROMPT = """\
Generate a YouTube title for a DJ mix upload.

RULES:
- Format: "{brand_name} | [Genre descriptor] [Mix Type] | [Vibe/Hook]"
- Under 60 characters when possible
- Artist name first, genre keywords for discovery
- No dates in title
- Use pipe separator |
- Make it SEO-friendly: include genre keywords people actually search for

MIX DATA:
- Genres: {genres}
- Vibes: {vibes}
- BPM Range: {bpm_range}
- Duration: {duration}

Return ONLY the title, nothing else.\
"""


class DescriptionGenerator:
    """Generates platform-specific descriptions and titles using OpenAI."""

    def __init__(self, db_settings_json: Optional[Dict[str, Any]] = None) -> None:
        sj = db_settings_json or {}
        api_key = sj.get("openai_api_key") or settings.OPENAI_API_KEY
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self._model = settings.OPENAI_MODEL

    async def generate_soundcloud_description(
        self,
        mix_title: str,
        genres: List[str],
        vibes: List[str],
        tracklist: Optional[List[Dict[str, Any]]] = None,
        energy_profile: Optional[List[Dict[str, Any]]] = None,
        bpm_range: Optional[List[float]] = None,
        duration_seconds: float = 0.0,
        session: Optional[AsyncSession] = None,
        mix_id: Optional[str] = None,
        brand_settings: Optional[BrandSettings] = None,
    ) -> str:
        """Generate a SoundCloud description."""
        return await self._generate_description(
            platform="SoundCloud",
            platform_link_instruction=SOUNDCLOUD_LINK_INSTRUCTION,
            links=settings.SOUNDCLOUD_LINKS,
            mix_title=mix_title,
            genres=genres,
            vibes=vibes,
            tracklist=tracklist,
            energy_profile=energy_profile,
            bpm_range=bpm_range,
            duration_seconds=duration_seconds,
            session=session,
            mix_id=mix_id,
            brand_settings=brand_settings,
        )

    async def generate_youtube_description(
        self,
        mix_title: str,
        genres: List[str],
        vibes: List[str],
        tracklist: Optional[List[Dict[str, Any]]] = None,
        energy_profile: Optional[List[Dict[str, Any]]] = None,
        bpm_range: Optional[List[float]] = None,
        duration_seconds: float = 0.0,
        session: Optional[AsyncSession] = None,
        mix_id: Optional[str] = None,
        brand_settings: Optional[BrandSettings] = None,
        youtube_timestamp_offset: float = 0.0,
    ) -> str:
        """Generate a YouTube description with chapter timestamps.

        youtube_timestamp_offset: seconds to ADD to FLAC timestamps for YT chapters.
        Positive = video starts before the FLAC (extra intro in stream).
        Negative = video starts after the FLAC.
        """
        # Apply offset to tracklist timestamps for YouTube chapters
        adjusted_tracklist = tracklist
        if tracklist and youtube_timestamp_offset != 0.0:
            adjusted_tracklist = []
            for t in tracklist:
                adjusted = dict(t)
                ts = t.get("timestamp_seconds", 0) + youtube_timestamp_offset
                ts = max(0, ts)  # don't go negative
                adjusted["timestamp_seconds"] = ts
                adjusted["timestamp_formatted"] = _seconds_to_timestamp(ts)
                adjusted_tracklist.append(adjusted)

        return await self._generate_description(
            platform="YouTube",
            platform_link_instruction=YOUTUBE_LINK_INSTRUCTION,
            links=settings.YOUTUBE_LINKS,
            mix_title=mix_title,
            genres=genres,
            vibes=vibes,
            tracklist=adjusted_tracklist,
            energy_profile=energy_profile,
            bpm_range=bpm_range,
            duration_seconds=duration_seconds,
            session=session,
            mix_id=mix_id,
            brand_settings=brand_settings,
        )

    async def generate_creative_title(
        self,
        genres: List[str],
        vibes: List[str],
        tracklist: Optional[List[Dict[str, Any]]] = None,
        filename: str = "",
        session: Optional[AsyncSession] = None,
        mix_id: Optional[str] = None,
    ) -> str:
        """Generate a creative, artistic mix title for SoundCloud."""
        # Build a tracklist hint (first few artists for inspiration)
        tracklist_hint = ""
        if tracklist:
            artists = list({t.get("artist", "") for t in tracklist[:8] if t.get("artist")})
            if artists:
                tracklist_hint = f"- Key artists: {', '.join(artists[:6])}"

        prompt = CREATIVE_TITLE_PROMPT.format(
            brand_name=settings.BRAND_NAME,
            filename=filename,
            genres=", ".join(genres),
            vibes=", ".join(vibes),
            tracklist_hint=tracklist_hint,
        )

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.9,
        )

        title = response.choices[0].message.content.strip().strip('"').strip("'")

        # Enforce 60-char limit
        if len(title) > 60:
            title = title[:57] + "..."

        # Track usage
        if session and mix_id:
            await self._track_usage(
                session, mix_id, "creative_title",
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
            )

        logger.info("Generated creative title: %s", title)
        return title

    async def generate_youtube_title(
        self,
        genres: List[str],
        vibes: List[str],
        bpm_range: Optional[List[float]] = None,
        duration_seconds: float = 0.0,
        session: Optional[AsyncSession] = None,
        mix_id: Optional[str] = None,
    ) -> str:
        """Generate an SEO-optimized YouTube title."""
        duration_str = _format_duration(duration_seconds)
        bpm_str = f"{bpm_range[0]:.0f}-{bpm_range[1]:.0f}" if bpm_range else "unknown"

        prompt = YOUTUBE_TITLE_PROMPT.format(
            brand_name=settings.BRAND_NAME,
            genres=", ".join(genres),
            vibes=", ".join(vibes),
            bpm_range=bpm_str,
            duration=duration_str,
        )

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.8,
        )

        title = response.choices[0].message.content.strip().strip('"').strip("'")

        # Track usage
        if session and mix_id:
            await self._track_usage(
                session, mix_id, "youtube_title",
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
            )

        logger.info("Generated YouTube title: %s", title)
        return title

    async def _generate_description(
        self,
        platform: str,
        platform_link_instruction: str,
        links: str,
        mix_title: str,
        genres: List[str],
        vibes: List[str],
        tracklist: Optional[List[Dict[str, Any]]],
        energy_profile: Optional[List[Dict[str, Any]]],
        bpm_range: Optional[List[float]],
        duration_seconds: float,
        session: Optional[AsyncSession],
        mix_id: Optional[str],
        brand_settings: Optional[BrandSettings],
    ) -> str:
        duration_str = _format_duration(duration_seconds)
        bpm_str = f"{bpm_range[0]:.0f}-{bpm_range[1]:.0f}" if bpm_range else "unknown"

        # Build tracklist section
        tracklist_section = ""
        if tracklist:
            lines = ["Tracklist:"]
            for t in tracklist:
                ts = t.get("timestamp_formatted") or _seconds_to_timestamp(
                    t.get("timestamp_seconds", 0)
                )
                lines.append(f"  {ts} {t.get('artist', 'Unknown')} - {t.get('title', 'Unknown')}")
            tracklist_section = "\n".join(lines)

        # Energy summary
        energy_summary = "not available"
        if energy_profile:
            rms_vals = [p.get("rms", 0) for p in energy_profile]
            if rms_vals:
                max_rms = max(rms_vals)
                if max_rms > 0:
                    normalized = [v / max_rms for v in rms_vals]
                    avg = sum(normalized) / len(normalized)
                    if avg > 0.7:
                        energy_summary = "high energy throughout"
                    elif avg > 0.4:
                        energy_summary = "moderate energy with peaks"
                    else:
                        energy_summary = "chill and laid-back"

        # Custom template from brand settings
        custom_template = ""
        if brand_settings and brand_settings.description_template:
            custom_template = f"ADDITIONAL BRAND TEMPLATE:\n{brand_settings.description_template}"

        prompt = DEFAULT_DESCRIPTION_PROMPT.format(
            brand_name=settings.BRAND_NAME,
            mix_title=mix_title,
            genres=", ".join(genres),
            vibes=", ".join(vibes),
            duration=duration_str,
            bpm_range=bpm_str,
            energy_summary=energy_summary,
            tracklist_section=tracklist_section,
            platform=platform,
            platform_link_instruction=platform_link_instruction,
            custom_template=custom_template,
        )

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.7,
        )

        description = response.choices[0].message.content.strip()

        # Strip any lines that look like URLs, bracketed placeholder links,
        # or link introduction phrases ("Find us on:", "Explore more:", etc.)
        url_line_pattern = re.compile(
            r"^\s*(\[?\s*https?://\S+\s*\]?|"       # lines starting with a URL or [url]
            r"\[?\s*\w+\.\w+\S*\s*\]?)\s*$|"        # lines that are just a domain like [example.com]
            r"^\s*\w+:\s*https?://\S+\s*$|"          # lines like "YouTube: https://..."
            r"^\s*\w+:\s*\[?\s*\w+\.\w+\S*\s*\]?$", # lines like "Website: [example.com]"
            re.IGNORECASE,
        )
        link_intro_pattern = re.compile(
            r"^\s*(find\s+us|explore\s+more|follow|catch\s+us|connect\s+with|check\s+us|"
            r"listen\s+on|watch\s+on|more\s+from|stay\s+connected|join\s+us|links|"
            r"--+\s*will\s+see|—\s*will\s+see)\b",
            re.IGNORECASE,
        )
        cleaned_lines = []
        for line in description.split("\n"):
            if url_line_pattern.match(line):
                continue
            if link_intro_pattern.match(line):
                continue
            cleaned_lines.append(line)
        description = "\n".join(cleaned_lines).rstrip()

        # Always append the real links
        if links:
            description += f"\n\n{links}"

        # Track usage
        if session and mix_id:
            await self._track_usage(
                session, mix_id, f"{platform.lower()}_description",
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
            )

        logger.info("Generated %s description (%d chars) for mix %s", platform, len(description), mix_id)
        return description

    async def _track_usage(
        self,
        session: AsyncSession,
        mix_id: str,
        operation: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Record AI token usage and estimated cost."""
        # Rough pricing for gpt-4o (as of 2025)
        cost_per_input_token = 2.50 / 1_000_000
        cost_per_output_token = 10.00 / 1_000_000
        cost = (input_tokens * cost_per_input_token) + (output_tokens * cost_per_output_token)

        usage = AIUsage(
            mix_id=mix_id,
            provider="openai",
            model=self._model,
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
        )
        session.add(usage)
        logger.debug(
            "AI usage: %s %d/%d tokens, $%.4f",
            operation, input_tokens, output_tokens, cost,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_duration(seconds: float) -> str:
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def _seconds_to_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
