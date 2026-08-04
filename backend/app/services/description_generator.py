"""OpenAI-powered description generator for SoundCloud and YouTube."""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import openai
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AIUsage, BrandSettings
from app.services.tracklist_utils import (
    ID_LABEL,
    build_youtube_chapters,
    format_timestamp,
    label_or_id,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model-aware token pricing (USD per 1,000,000 tokens: input, output)
# ---------------------------------------------------------------------------
# Longest key wins on prefix match, so dated snapshots like
# "gpt-4o-2024-08-06" resolve to the "gpt-4o" rate. Unknown models fall back to
# the gpt-4o rate rather than silently recording $0.
MODEL_PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4-turbo": (10.00, 30.00),
    "o1-mini": (1.10, 4.40),
    "o1": (15.00, 60.00),
    "o3-mini": (1.10, 4.40),
}
_DEFAULT_PRICING = (2.50, 10.00)  # gpt-4o


def price_for_model(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate OpenAI cost (USD) for a model, honoring ``OPENAI_MODEL`` changes.

    Falls back to the gpt-4o rate for unrecognized models. Matches dated model
    snapshots by longest-prefix (e.g. ``gpt-4o-2024-08-06`` -> ``gpt-4o``).
    """
    key = (model or "").lower()
    rates = MODEL_PRICING.get(key)
    if rates is None:
        for name in sorted(MODEL_PRICING, key=len, reverse=True):
            if key.startswith(name):
                rates = MODEL_PRICING[name]
                break
    if rates is None:
        rates = _DEFAULT_PRICING
    cost_in, cost_out = rates
    return (input_tokens * cost_in + output_tokens * cost_out) / 1_000_000


def _response_text(response: Any) -> str:
    """Extract stripped text content from a chat completion, or '' if empty.

    ``response.choices[0].message.content`` can be ``None`` (content filter or a
    length cutoff), which would make a bare ``.strip()`` raise. This returns an
    empty string in every degenerate case so callers can guard cleanly.
    """
    try:
        choice = response.choices[0]
    except (AttributeError, IndexError, TypeError):
        return ""
    content = getattr(getattr(choice, "message", None), "content", None)
    if not content:
        return ""
    return content.strip()

# ---------------------------------------------------------------------------
# Unified, genre-forward, click-optimized titles
# ---------------------------------------------------------------------------
# One canonical title per mix, published verbatim to BOTH SoundCloud and
# YouTube. Two separate generators used to produce two different strings for
# the same set, and a short-lived experiment (PR #30) forced a series /
# raid-train name plus the recording date in front of the creative hook. Both
# are gone: the owner wants the abstract creative title he trades on, carrying
# a searchable genre keyword, tuned for click-through.
#
# Shape:  "<Creative Hook> | <Genre> Mix"
#         e.g. "Mirage Reverberation | Tech House Mix"

# YouTube allows 100 characters but shows far fewer: mobile truncates around
# 50-60 and desktop search / suggested around 60-70 ("Viewers may only see
# part of your title" — YouTube Help, Thumbnail & title tips). SoundCloud's
# own cap is 100. So: aim for 60, never ship past 70, and everything that has
# to be read survives the cut on every surface.
TITLE_TARGET_CHARS = 60
TITLE_MAX_CHARS = 70

# Genre bucket -> (title keyword, spellings that already satisfy the rule).
# The bucket itself comes from ``thumbnail_design.resolve_genre_key`` — the
# codebase's existing genre classifier (it drives the thumbnail motifs) — so
# artwork and title always agree on what the set is, and there is exactly one
# genre-resolution path to maintain.
GENRE_TITLE_TERMS: Dict[str, tuple] = {
    "drum and bass": (
        "Drum & Bass",
        ("drum & bass", "drum and bass", "drum n bass", "dnb", "d&b", "jungle"),
    ),
    "dubstep": ("Dubstep", ("dubstep", "riddim", "brostep")),
    "deep house": ("Deep House", ("deep house",)),
    "tech house": ("Tech House", ("tech house",)),
    "house": ("House", ("house",)),
    "techno": ("Techno", ("techno",)),
    "trance": ("Trance", ("trance",)),
    "edm": ("EDM", ("edm", "big room")),
    "trap": ("Trap", ("trap",)),
    "garage": ("UK Garage", ("uk garage", "garage", "ukg", "2-step", "2step")),
    "organic": ("Melodic House", ("melodic", "organic house", "progressive")),
    "open format": ("Open Format", ("open format", "genre-fluid")),
}

_FALLBACK_GENRE_TERM = ("Open Format", ("open format", "genre-fluid"))


def resolve_title_genre(genres: Optional[List[str]]) -> str:
    """The searchable genre keyword that must appear in a mix's title.

    Delegates the actual classification to
    :func:`app.services.thumbnail_design.resolve_genre_key` (the existing
    genre-bucket classifier used for thumbnail motifs) and maps the resolved
    bucket to the term listeners actually search for — "Drum & Bass" rather
    than the internal ``drum and bass`` key, "UK Garage" rather than
    ``garage``.
    """
    from app.services.thumbnail_design import resolve_genre_key

    key = resolve_genre_key(genres)
    return GENRE_TITLE_TERMS.get(key, _FALLBACK_GENRE_TERM)[0]


def _genre_spellings(genre_term: str) -> tuple:
    """Accepted spellings for a resolved genre term (for presence checking)."""
    for term, spellings in GENRE_TITLE_TERMS.values():
        if term == genre_term:
            return spellings
    return (genre_term.lower(),)


def genre_in_title(title: str, genre_term: str) -> bool:
    """True if ``title`` already carries the genre keyword in any spelling."""
    low = (title or "").lower()
    return any(spelling in low for spelling in _genre_spellings(genre_term))


def _title_hook(title: str, genre_term: str) -> str:
    """The creative part of a title, with the trailing genre segment removed.

    Used when a title has to be disambiguated: the numeral belongs on the
    hook ("Neon Cactus II | House Mix"), not tacked onto the keyword.
    """
    head, sep, tail = (title or "").rpartition(" | ")
    if sep and genre_in_title(tail, genre_term):
        return head.strip()
    return (title or "").strip()


def enforce_title_shape(title: str, genre_term: str) -> str:
    """Guarantee the published title carries its genre and fits the cap.

    The prompt asks for ``"<Hook> | <Genre> Mix"``, but a model that drops the
    genre would cost the mix its single strongest discovery keyword, so the
    genre is appended deterministically when missing. The genre tail is never
    what gets trimmed — the creative hook gives up the characters, since the
    keyword is the part doing the search work.
    """
    title = (title or "").strip().strip('"').strip("'").strip()
    if genre_in_title(title, genre_term):
        return title[:TITLE_MAX_CHARS].rstrip() if len(title) > TITLE_MAX_CHARS else title

    for tail in (f" | {genre_term} Mix", f" | {genre_term}"):
        if len(title) + len(tail) <= TITLE_MAX_CHARS:
            return f"{title}{tail}"

    # Even the bare genre tail doesn't fit: trim the hook, never the keyword.
    tail = f" | {genre_term}"
    room = max(TITLE_MAX_CHARS - len(tail), 0)
    return f"{title[:room].rstrip()}{tail}"


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
    "Do not include any links or a link list; they are appended automatically."
)

YOUTUBE_LINK_INSTRUCTION = (
    "Do not include any links or a link list; they are appended automatically. "
    "Do NOT write a tracklist or chapter list yourself -- a validated chapter "
    "list with timestamps is appended automatically."
)

# Used when there are too few tracks for a valid chapter block (YouTube needs
# >=3 chapters), so the model should still surface whatever tracklist exists.
YOUTUBE_LINK_INSTRUCTION_TRACKLIST = (
    "Do not include any links or a link list; they are appended automatically. "
    "If a tracklist is provided, include it with timestamps starting at 0:00."
)

CREATIVE_TITLE_PROMPT = """\
Write ONE title for a DJ mix by "{brand_name}". This exact title is published
to BOTH SoundCloud and YouTube, so it has to do two jobs at once: sound like
art, and earn the click.

FORMAT (required):
"[Creative Hook] | {genre_term} Mix"
Examples of the shape:
- "Mirage Reverberation | Tech House Mix"
- "Four Decks and a Prayer | Drum & Bass Mix"
- "Neon Cactus After Dark | Melodic House Mix"

THE HOOK (the part before the pipe):
- Psychedelic, evocative, poetic — it should read like an album name, an art
  exhibition, or a fever dream, never like an SEO string
- Desert energy, late-night, otherworldly
- Concrete and specific beats vague and moody: an image someone can picture
  ("Silk and Static", "The Eye Opens at Midnight") outperforms an abstraction
  ("Sonic Journey", "Good Vibes")
- Carry one emotional or curiosity trigger — a tension, a place, a moment —
  but it must be TRUE to the set. Never promise something the audio does not
  deliver; misleading titles cost watch time and get demoted
- 2-5 words. It is the first thing read, so the most arresting word goes early

CLICK-THROUGH RULES (non-negotiable):
- Front-load the hook. Titles are truncated in search results, suggested
  videos, and on mobile — the most important words go near the beginning,
  because that is all many viewers will ever see
- The genre keyword "{genre_term}" MUST appear, spelled exactly like that. It
  is the term listeners search and browse by, and it is the single strongest
  reason this mix gets found at all
- Keep the WHOLE title at or under {target_chars} characters if you possibly
  can, and NEVER past {max_chars}
- Say one thing sharply rather than three things vaguely — one vivid, specific
  image beats a pile of mood words
- At most ONE power word ("Best", "Ultimate", "Fresh"). No ALL-CAPS words, no
  emojis, no "must hear", no fake hype
- The title must accurately represent the audio. A title that oversells gets
  clicks and then loses them, and low retention after a high click is exactly
  what buries a video
- No dates, no year, no series or show name, no "raid train", no "Twitch"
- Do not include the artist or brand name
- Do not write "live", "vol.", or a track count

TITLES ALREADY IN USE — every one of these is FORBIDDEN, and do not write
anything close to them:
{used_titles_block}

MIX DATA:
- Genre keyword to use: {genre_term}
- Raw filename: {filename}
- Detected genres: {genres}
- Vibes: {vibes}
{tracklist_hint}
{retry_feedback}
Return ONLY the title, nothing else.\
"""


class DescriptionGenerator:
    """Generates platform-specific descriptions and titles using OpenAI."""

    def __init__(self, db_settings_json: Optional[Dict[str, Any]] = None) -> None:
        sj = db_settings_json or {}
        api_key = sj.get("openai_api_key") or settings.OPENAI_API_KEY
        self._client = openai.AsyncOpenAI(api_key=api_key)
        # DB-configured model (Settings page) wins; env is the fallback.
        self._model = sj.get("llm_model") or settings.OPENAI_MODEL

    async def _create_completion(
        self, prompt: str, max_tokens: int, temperature: float
    ):
        """Call the chat API, guarding against empty content with one retry.

        Returns ``(response, text)``. Raises ``RuntimeError`` if the model still
        returns no usable content after a single retry (e.g. a content filter or
        a length cutoff that produced ``None`` content).
        """
        messages = [{"role": "user", "content": prompt}]
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = _response_text(response)
        if not text:
            logger.warning(
                "OpenAI returned empty content (model=%s); retrying once",
                self._model,
            )
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            text = _response_text(response)
            if not text:
                raise RuntimeError(
                    f"OpenAI returned no usable content for model "
                    f"{self._model!r} after retry"
                )
        return response, text

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
        video_duration_seconds: Optional[float] = None,
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

        # Build a guaranteed-valid YouTube chapter block (first stamp 0:00, >=3
        # chapters, >=10s apart). When we have enough tracks for real chapters,
        # append them deterministically and keep the model from writing its own
        # (unreliable) tracklist. Otherwise fall back to the model tracklist.
        # The video is the whole stream: the mix sits between a "starting
        # soon" pre-roll of exactly the measured offset and whatever trailed
        # after the last track. Both bookends fall out of numbers we already
        # have — no extra analysis.
        mix_end = (
            youtube_timestamp_offset + duration_seconds
            if duration_seconds
            else None
        )
        chapter_block = _format_chapter_block(
            adjusted_tracklist,
            lead_in_seconds=youtube_timestamp_offset,
            mix_end_seconds=mix_end,
            video_duration_seconds=video_duration_seconds,
        )

        return await self._generate_description(
            platform="YouTube",
            platform_link_instruction=(
                YOUTUBE_LINK_INSTRUCTION if chapter_block
                else YOUTUBE_LINK_INSTRUCTION_TRACKLIST
            ),
            links=settings.YOUTUBE_LINKS,
            mix_title=mix_title,
            genres=genres,
            vibes=vibes,
            tracklist=None if chapter_block else adjusted_tracklist,
            energy_profile=energy_profile,
            bpm_range=bpm_range,
            duration_seconds=duration_seconds,
            session=session,
            mix_id=mix_id,
            brand_settings=brand_settings,
            appended_block=chapter_block,
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
        """Generate THE title for a mix — the one string both platforms use.

        The result is written to ``mix.title`` (SoundCloud) and
        ``mix.title_youtube`` (YouTube) unchanged, so a mix is the same
        artifact wherever someone finds it. It always carries the resolved
        genre keyword (see :func:`resolve_title_genre`) and is capped at
        :data:`TITLE_MAX_CHARS` so nothing truncates in search or suggested.

        Uniqueness is system-enforced when a ``session`` is provided: recent
        already-used titles are fed into the prompt, every draft is checked
        against the ``used_creative`` registry (exact + fuzzy), collisions
        retry with feedback, a stubborn collision gets a deterministic
        volume-numeral suffix, and the accepted title is claimed.
        """
        from app.services import uniqueness

        genre_term = resolve_title_genre(genres)

        # Build a tracklist hint (first few artists for inspiration)
        tracklist_hint = ""
        if tracklist:
            artists = list({t.get("artist", "") for t in tracklist[:8] if t.get("artist")})
            if artists:
                tracklist_hint = f"- Key artists: {', '.join(artists[:6])}"

        used_titles: List[str] = []
        if session is not None:
            used_titles = await uniqueness.recent_values(
                session, uniqueness.KIND_TITLE, limit=40
            )

        title = ""
        rejected: List[str] = []
        for attempt in range(3):
            used_block = (
                "\n".join(f"- {t}" for t in [*used_titles, *rejected]) or "(none)"
            )
            feedback = ""
            if rejected:
                feedback = (
                    f'\nYOUR PREVIOUS ATTEMPT "{rejected[-1]}" WAS REJECTED: that '
                    "title is already used. Produce a COMPLETELY different title "
                    "— different words, different structure.\n"
                )
            prompt = CREATIVE_TITLE_PROMPT.format(
                brand_name=settings.BRAND_NAME,
                genre_term=genre_term,
                target_chars=TITLE_TARGET_CHARS,
                max_chars=TITLE_MAX_CHARS,
                filename=filename,
                genres=", ".join(genres),
                vibes=", ".join(vibes),
                tracklist_hint=tracklist_hint,
                used_titles_block=used_block,
                retry_feedback=feedback,
            )

            response, text = await self._create_completion(
                prompt, max_tokens=80, temperature=0.9,
            )

            # Genre keyword + length cap are guaranteed here, not hoped for,
            # so uniqueness is checked and claimed on the exact published
            # string.
            title = enforce_title_shape(text, genre_term)

            # Track usage
            if session and mix_id:
                await self._track_usage(
                    session, mix_id, "creative_title",
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                )

            if session is None or not await uniqueness.is_taken(
                session, uniqueness.KIND_TITLE, title, exclude_mix_id=mix_id
            ):
                break
            rejected.append(title)
            logger.info(
                "Creative title %r already taken (attempt %d); retrying",
                title, attempt + 1,
            )
        else:
            # Hard guarantee: deterministic volume-numeral suffix until free.
            # The numeral goes on the HOOK and the genre tail is re-applied
            # after, so a disambiguated title still reads "Hook II | House Mix"
            # and never loses its search keyword.
            base_hook = _title_hook(title, genre_term)
            for numeral in ("II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"):
                candidate = enforce_title_shape(
                    f"{base_hook} {numeral}", genre_term
                )
                # Exact-only: "Base II" must not fuzzy-collide with "Base".
                if not await uniqueness.is_taken(
                    session, uniqueness.KIND_TITLE, candidate,
                    exclude_mix_id=mix_id, fuzzy=False,
                ):
                    title = candidate
                    break
            else:
                suffix = (mix_id or "X")[:4].upper()
                title = enforce_title_shape(
                    f"{base_hook} {suffix}", genre_term
                )
            logger.warning(
                "Creative title kept colliding; accepted suffixed title %r", title
            )

        if session is not None:
            await uniqueness.claim(session, uniqueness.KIND_TITLE, title, mix_id)

        logger.info("Generated unified title: %s", title)
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
        appended_block: str = "",
    ) -> str:
        duration_str = _format_duration(duration_seconds)
        bpm_str = f"{bpm_range[0]:.0f}-{bpm_range[1]:.0f}" if bpm_range else "unknown"

        # Build tracklist section (unidentified tracks render as "ID - ID")
        tracklist_section = ""
        if tracklist:
            lines = ["Tracklist:"]
            for t in tracklist:
                ts = t.get("timestamp_formatted") or _seconds_to_timestamp(
                    t.get("timestamp_seconds", 0)
                )
                artist = label_or_id(t.get("artist", ""))
                title = label_or_id(t.get("title", ""))
                lines.append(f"  {ts} {artist} - {title}")
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

        response, description = await self._create_completion(
            prompt, max_tokens=2000, temperature=0.7,
        )

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
        # Drop stray social-link label lines. The model sometimes emits the
        # brand link labels without URLs (e.g. a "YouTube / Twitch / Website /
        # Shop" block) when it tries to satisfy an (old) link-list instruction
        # under the no-URLs rule. These appear either as one label per line:
        #     YouTube
        #     Twitch
        #     Website
        #     Shop
        # or several labels on a single line ("YouTube, Twitch, Website, Shop").
        # A line that is ONLY one of these brand labels (optionally repeated,
        # comma/space separated) is never legitimate description prose, so drop
        # it. The real links are appended separately below.
        bare_label_pattern = re.compile(
            r"^\s*(?:youtube|twitch|website|shop|soundcloud)"
            r"(?:\s*[,/|]?\s*(?:youtube|twitch|website|shop|soundcloud))*\s*$",
            re.IGNORECASE,
        )
        cleaned_lines = []
        for line in description.split("\n"):
            if url_line_pattern.match(line):
                continue
            if link_intro_pattern.match(line):
                continue
            if bare_label_pattern.match(line):
                continue
            cleaned_lines.append(line)
        description = "\n".join(cleaned_lines).rstrip()

        # Append a deterministic, validated chapter/tracklist block (YouTube),
        # guaranteeing correct 0:00-anchored chapter formatting rather than
        # trusting the model to reproduce timestamps.
        if appended_block:
            description = f"{description}\n\n{appended_block}"

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
        # Model-aware pricing so a changed OPENAI_MODEL is costed correctly.
        cost = price_for_model(self._model, input_tokens, output_tokens)

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

def _format_chapter_block(
    tracklist: Optional[List[Dict[str, Any]]],
    lead_in_seconds: float = 0.0,
    mix_end_seconds: Optional[float] = None,
    video_duration_seconds: Optional[float] = None,
) -> str:
    """Render a validated YouTube chapter block, or '' if too few chapters.

    Uses ``build_youtube_chapters`` to guarantee YouTube renders chapters (first
    stamp 0:00, >=3 chapters, >=10s apart) and to bookend the tracks with
    "Starting Soon" / "Stream Ended" chapters when the video has a pre-roll or
    trailing content. Unidentified tracks render as ``ID - ID``; marker rows
    render as just their label.
    """
    chapters = build_youtube_chapters(
        tracklist or [],
        lead_in_seconds=lead_in_seconds,
        mix_end_seconds=mix_end_seconds,
        video_duration_seconds=video_duration_seconds,
    )
    if not chapters:
        return ""

    lines = ["Tracklist:"]
    for chapter in chapters:
        ts = chapter.get("timestamp_formatted") or format_timestamp(
            chapter.get("timestamp_seconds", 0)
        )
        title = label_or_id(chapter.get("title", ""))
        raw_artist = str(chapter.get("artist", "")).strip()
        if raw_artist:
            lines.append(f"{ts} {label_or_id(raw_artist)} - {title}")
        else:
            # Marker rows (e.g. the synthetic Intro) have no artist.
            lines.append(f"{ts} {title}")
    return "\n".join(lines)


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
