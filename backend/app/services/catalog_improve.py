"""AI improve for the back catalog: keeper/generic title triage + draft proposals.

``run_improve`` (behind ``POST /api/catalog/improve``) works in two passes:

1. **Classify** every target mix title as ``keeper`` (unique/creative — locked
   via ``title_locked``, never proposed again) or ``generic`` (raid-train
   patterns, date-only titles, "DJ set" boilerplate). A cheap heuristic decides
   the obvious cases; the ambiguous middle ground goes to the LLM in one batch.
2. **Draft** for each generic mix: ONE click-optimized title and a refreshed
   full description that *retains the existing tracklist section* and the brand
   links block. Drafts land as ``MixProposal`` rows (``created_by="ai"``,
   status ``draft``) — platform ``both`` for shared values, split per-platform
   where the values differ.

Titles here obey exactly the same policy as the live pipeline (see
``description_generator``): one string per mix used identically on SoundCloud
and YouTube, shaped ``"<Evocative Hook> | <Genre> Mix"``, aiming for
``TITLE_TARGET_CHARS`` and never exceeding ``TITLE_MAX_CHARS``, always carrying
the searchable genre keyword. The shape/genre helpers are imported rather than
re-implemented so there is one policy, not two.

On top of that the back catalog needs a guard the fresh pipeline does not: its
existing titles are full of stream-era wording the owner has since retired
(series names, "Raid Train", the channel name as a prefix, bare dates). See
:data:`BANNED_TITLE_PATTERNS` — a drafted title matching any of them is
rejected, retried, and finally stripped deterministically.

All LLM traffic reuses the DescriptionGenerator client + AIUsage accounting.
"""

import difflib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import AppSettings, Mix, MixProposal
from app.services import activity_log

logger = logging.getLogger(__name__)

# Title shape/length policy is owned by description_generator (the live
# pipeline's module) and re-exported here so the back catalog cannot drift.
from app.services.description_generator import (  # noqa: E402
    TITLE_MAX_CHARS,
    TITLE_TARGET_CHARS,
    enforce_title_shape,
    genre_in_title,  # noqa: F401  — re-exported for callers and tests
    resolve_title_genre,
)

# Words the improve LLM has historically leaned on to the point of parody
# ("Odyssey" showed up in 15+ of 60 drafts). Banned outright in the prompt and
# enforced by the post-generation diversity guard.
OVERUSED_TITLE_WORDS = ["odyssey", "sonic", "journey", "voyage", "exploration"]

# ---------------------------------------------------------------------------
# Banned wording (back-catalog specific)
# ---------------------------------------------------------------------------
# The live catalog was built during the Twitch era, so its titles carry show
# names, raid-train labels, the channel name as a prefix, and stream dates.
# None of that belongs in a title any more: a title's job is an evocative hook
# plus the genre keyword, and calendar/series wording burns characters while
# actively dating the upload. Same ban-list pattern as OVERUSED_TITLE_WORDS,
# but these are phrases and structures rather than single words, so each entry
# pairs a human-readable reason with the pattern that detects it.
#
# Order matters for the deterministic strip: the specific series names run
# before the bare "Will See" channel prefix so "Will See Wednesdays" is removed
# as a series name rather than half-eaten by the prefix rule.
# Written-out ordinals and their numeric forms are used interchangeably in
# stream titles ("Second Saturdays" / "2nd Saturdays"), so a series name
# containing one should match either spelling.
_ORDINAL_ALIASES = {
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th",
    "fifth": "5th", "sixth": "6th", "seventh": "7th", "eighth": "8th",
    "ninth": "9th", "tenth": "10th",
}
_ORDINAL_ALIASES.update({v: k for k, v in _ORDINAL_ALIASES.items()})


def _word_pattern(word: str) -> str:
    """A word, plus its ordinal counterpart when it has one."""
    alias = _ORDINAL_ALIASES.get(word.lower())
    if alias:
        return f"(?:{re.escape(word)}|{re.escape(alias)})"
    return re.escape(word)


def _series_pattern(name: str) -> "re.Pattern[str]":
    """Match a series name tolerantly.

    Any run of whitespace/dashes between words, ordinal spellings treated as
    equivalent, and singular/plural treated as the same series — so a name
    configured either way ("Will See Wednesdays" or "Will See Wednesday")
    matches both spellings in a title.
    """
    words = name.split()
    if words:
        # Normalise the final word to its stem so the optional "s" below
        # covers both spellings regardless of how the name was configured.
        last = words[-1]
        if len(last) > 1 and last[-1].lower() == "s" and last.lower() not in _ORDINAL_ALIASES:
            words[-1] = last[:-1]
    body = r"\s*[-–—]?\s*".join(_word_pattern(w) for w in words)
    return re.compile(rf"\b{body}s?\b", re.IGNORECASE)


def _channel_prefix_pattern(name: str) -> "re.Pattern[str]":
    """Match the channel name only where it is used as a leading prefix."""
    body = r"\s+".join(re.escape(w) for w in name.split())
    return re.compile(rf"^\s*{body}\s*[|\-–—:]+\s*", re.IGNORECASE)


def _build_banned_title_patterns() -> List[Tuple[str, "re.Pattern[str]"]]:
    """Assemble the ban list: configured series/channel names first, then the
    universal markers.

    Order matters for the deterministic strip: the specific series names run
    before the bare channel prefix, so a series name is removed whole rather
    than half-eaten by the prefix rule.
    """
    patterns: List[Tuple[str, "re.Pattern[str]"]] = []

    for name in settings.catalog_series_names:
        patterns.append((f'contains the series name "{name}"', _series_pattern(name)))

    patterns.append(('contains "Raid Train"', re.compile(r"\braid[\s\-_]*trains?\b", re.IGNORECASE)))

    channel = settings.CATALOG_CHANNEL_NAME.strip()
    if channel:
        patterns.append((
            f'starts with the channel name "{channel}" as a prefix',
            _channel_prefix_pattern(channel),
        ))

    patterns.append((
        "contains a date",
        re.compile(
            r"\(?\b(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
            r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b\)?"
        ),
    ))
    return patterns


BANNED_TITLE_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = _build_banned_title_patterns()

# Left over when a banned phrase is the entire creative half of a title
# ("Will See Wednesdays | House Mix" -> "" | House Mix"). Neutral, on-brand,
# and only ever reached after the LLM has already failed twice.
BANNED_STRIP_FALLBACK_HOOK = "Late Transmission"

# Separator/punctuation debris left behind by a strip.
_SEP_RUN_RE = re.compile(r"\s*[|\-–—:]\s*(?:[|\-–—:]\s*)+")
_SEP_EDGE_RE = re.compile(r"^[\s|\-–—:,]+|[\s|\-–—:,]+$")


def banned_wording_problem(title: str) -> Optional[str]:
    """Why ``title`` violates the retired-wording ban, or None when it's clean."""
    t = title or ""
    for reason, pattern in BANNED_TITLE_PATTERNS:
        if pattern.search(t):
            return reason
    return None


def strip_banned_wording(title: str) -> str:
    """Remove every banned phrase from ``title`` and tidy the debris.

    Deterministic last resort for when the LLM will not stop volunteering the
    old stream wording. The result can be empty (the banned phrase *was* the
    title) — callers substitute :data:`BANNED_STRIP_FALLBACK_HOOK`.
    """
    out = title or ""
    for _, pattern in BANNED_TITLE_PATTERNS:
        out = pattern.sub(" ", out)
    out = _SEP_RUN_RE.sub(" | ", out)
    out = _SEP_EDGE_RE.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def sanitize_title(title: str, genre_term: str) -> str:
    """Banned wording removed, then the shared shape/genre/length policy
    re-applied — the genre keyword survives even when the hook is gutted."""
    hook = strip_banned_wording(title) or BANNED_STRIP_FALLBACK_HOOK
    return enforce_title_shape(hook, genre_term)

# A drafted title this difflib-similar to any already-used title triggers a
# retry (and, failing that, an activity warning).
TITLE_SIMILARITY_MAX = 0.8

# How many already-used titles to show the LLM (most recent last -> tail).
MAX_USED_TITLES_IN_PROMPT = 40

# ---------------------------------------------------------------------------
# Heuristic title classifier
# ---------------------------------------------------------------------------

# Mostly universal markers. The "dj … see live/set" entry is a leftover from
# the original author's channel and is harmless for everyone else (it simply
# never matches); the configurable ban list in BANNED_TITLE_PATTERNS is where
# per-channel wording belongs.
GENERIC_PATTERNS = [
    re.compile(r"raid.?train", re.IGNORECASE),
    re.compile(r"^dj (will )?see (live|set|stream)", re.IGNORECASE),
    re.compile(r"\btwitch\b", re.IGNORECASE),
    re.compile(r"\buntitled\b", re.IGNORECASE),
]

_DATE_TOKEN_RE = re.compile(
    r"\b(\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}|\d{4}|\d{1,2}(st|nd|rd|th)?|"
    r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|"
    r"sep(t|tember)?|oct(ober)?|nov(ember)?|dec(ember)?|"
    r"mon(day)?|tue(s|sday)?|wed(nesday)?|thu(rs|rsday)?|fri(day)?|"
    r"sat(urday)?|sun(day)?)\b",
    re.IGNORECASE,
)


def _is_date_only(title: str) -> bool:
    """True when the title is nothing but date tokens (a stream dump name)."""
    stripped = re.sub(r"[^\w\s]", " ", title or "")
    remainder = _DATE_TOKEN_RE.sub(" ", stripped)
    remainder = re.sub(r"\b(live|stream|set|mix|recording|vod)\b", " ", remainder, flags=re.IGNORECASE)
    return not remainder.strip()


def classify_title_heuristic(title: str) -> str:
    """Classify a title: "generic", "keeper", or "unknown" (needs the LLM).

    Generic markers (raid-train / DJ-see-live / twitch / untitled / date-only)
    win outright. A clearly creative title — several words, no digits, none of
    the generic markers — is a keeper. Everything else is the LLM's call.
    """
    t = (title or "").strip()
    if not t:
        return "generic"
    # Retired stream-era wording is generic by definition, however creative the
    # rest of the title is — the owner wants it gone from every title.
    if banned_wording_problem(t):
        return "generic"
    for pattern in GENERIC_PATTERNS:
        if pattern.search(t):
            return "generic"
    if _is_date_only(t):
        return "generic"
    words = t.split()
    if len(words) >= 3 and not any(ch.isdigit() for ch in t):
        return "keeper"
    return "unknown"


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

CLASSIFY_PROMPT = """\
You are triaging DJ mix titles for the brand "{brand_name}". For each numbered
title decide: "keeper" (a unique, creative, artistic mix title worth keeping
exactly as-is) or "generic" (stream-dump boilerplate: dates, raid trains,
"live set", platform names, or otherwise unmemorable filler).

{titles_block}

Respond with ONLY a JSON array like
[{{"n": 1, "class": "keeper"}}, {{"n": 2, "class": "generic"}}]\
"""

IMPROVE_PROMPT = """\
You are refreshing the metadata of an ALREADY-PUBLISHED DJ mix by the brand
"{brand_name}". The current title is generic stream-dump boilerplate; write it
a proper release identity.

RULES FOR THE TITLE:
- ONE title only. It is published verbatim on BOTH SoundCloud and YouTube —
  do not write platform variants.
- SHAPE: "<Evocative Hook> | {genre_term} Mix". The hook is abstract and
  atmospheric; the genre keyword is what people actually search for.
- The genre keyword "{genre_term}" MUST appear.
- Aim for {title_target} characters, NEVER exceed {title_max}.
- Click-optimized but honest: no clickbait lies, no emojis
- BANNED WORDINGS — never include any of these, in any casing:
  * the series/show names "Will See Wednesdays", "Second Saturdays",
    "2nd Saturdays", or ANY other series, show, or residency name
  * "Raid Train", "raid train", "twitch", or "stream"
  * the channel name "Will See" as a prefix (no "Will See | ...",
    no "Will See - ...") — the brand is already on the channel
  * ANY date in any format: "2026-07-20", "07-20-2026", "(2026-07-20)",
    "May 3rd", years, weekday names
- BANNED WORDS (overused across this catalog — never use them in any casing
  or form): {banned_words}
- Vary the title STRUCTURE — do NOT default to "Adjective Noun: Subtitle".
  Rotate between forms: imperative hooks ("Lock the Groove In"), place/time
  references ("3AM in the Warehouse"), genre slang, "artist | descriptor"
  forms, questions, single striking phrases

TITLES ALREADY IN USE — do NOT reuse their patterns or key words, and do not
write anything close to them:
{used_titles_block}

RULES FOR THE DESCRIPTION:
- Confident, psychedelic, cool; no emojis, no fake hype
- Plain text only, no markdown
- 2-4 short paragraphs about the mix (genres, energy, vibe)
- Do NOT write a tracklist — it is re-appended automatically afterward
- Do NOT include any links or a link list — links are appended automatically

RULES FOR THE TAGS (search discoverability):
- "tags_youtube": 15-20 search tags — genres and subgenres, notable artists
  from the current description's tracklist, "{brand_name}", "dj mix",
  "dj set", the series/show name if any, and the year if known
- "tags_soundcloud": up to 30 tags, same approach, SoundCloud-style
- Plain lowercase strings, no "#" characters, no duplicates

MIX DATA:
- Current title: {current_title}
- Genres: {genres}
- Duration: {duration}
- Current description (for context, may contain a tracklist you must NOT copy):
{current_description}
{retry_feedback}
Respond with ONLY a JSON object:
{{"title": "...", "description": "...",
 "tags_youtube": ["..."], "tags_soundcloud": ["..."]}}\
"""

# Tag caps: YouTube search tags stay ~15-20; SoundCloud's tag_list caps at 30.
YT_TAGS_MAX = 20
SC_TAGS_MAX = 30


def _strip_json_fences(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)


def _clean_tags(raw: Any, cap: int) -> List[str]:
    """Normalize an LLM tags list: strings only, '#' stripped, case-insensitive
    dedupe, order preserved, capped. Anything malformed -> []."""
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw:
        tag = str(item).strip().lstrip("#").strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# Description assembly helpers
# ---------------------------------------------------------------------------

def extract_tracklist_block(description: str) -> str:
    """Pull the ``Tracklist:`` section (through its timestamped lines) out of an
    existing platform description, or '' when absent."""
    if not description:
        return ""
    lines = description.split("\n")
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower().startswith("tracklist")),
        None,
    )
    if start is None:
        return ""
    block = [lines[start]]
    ts_re = re.compile(r"^\s*(\d+:)?\d{1,2}:\d{2}\s")
    for ln in lines[start + 1 :]:
        if not ln.strip():
            break
        if ts_re.match(ln) or ln.startswith("  "):
            block.append(ln)
        else:
            break
    return "\n".join(block) if len(block) > 1 else ""


def build_platform_description(body: str, tracklist_block: str, links: str) -> str:
    """Assemble body + retained tracklist + brand links, in that order."""
    parts = [body.rstrip()]
    if tracklist_block:
        parts.append(tracklist_block)
    if links:
        parts.append(links)
    return "\n\n".join(p for p in parts if p)


def _format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "unknown"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m"


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

async def classify_titles_llm(
    titles: List[str], generator, session=None
) -> List[str]:
    """Batch-classify the heuristic's "unknown" titles. Failure -> all keeper
    (never draft changes for a title we could not confidently call generic)."""
    if not titles:
        return []
    titles_block = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1))
    prompt = CLASSIFY_PROMPT.format(
        brand_name=settings.BRAND_NAME, titles_block=titles_block
    )
    try:
        response, text = await generator._create_completion(
            prompt, max_tokens=20 * len(titles) + 40, temperature=0.0
        )
        if session is not None:
            await generator._track_usage(
                session, None, "catalog_title_classify",
                response.usage.prompt_tokens, response.usage.completion_tokens,
            )
        data = json.loads(_strip_json_fences(text))
        out = ["keeper"] * len(titles)
        for entry in data:
            idx = int(entry.get("n", 0)) - 1
            if 0 <= idx < len(titles):
                out[idx] = "generic" if entry.get("class") == "generic" else "keeper"
        return out
    except Exception as exc:
        logger.warning("LLM title classify failed; defaulting to keeper: %s", exc)
        return ["keeper"] * len(titles)


async def draft_improvement_llm(
    mix: Mix,
    generator,
    session=None,
    used_titles: Optional[List[str]] = None,
    retry_feedback: str = "",
    genre_term: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Draft {"title", "description", "tags_youtube", "tags_soundcloud"} for a
    generic mix, or None on failure. The tags lists (search-discoverability
    riders) are optional — an LLM response without them still yields a valid
    title/description draft.

    ``used_titles`` (existing mix titles + open/applied title proposals + drafts
    already produced this run) is injected into the prompt so the LLM stops
    recycling the same words and patterns; ``retry_feedback`` carries the
    diversity guard's "too similar, be different" addendum on a retry.
    """
    current_description = (
        mix.description_youtube or mix.description_soundcloud or ""
    )
    genre_term = genre_term or resolve_title_genre(mix.genres)
    recent_used = (used_titles or [])[-MAX_USED_TITLES_IN_PROMPT:]
    used_titles_block = (
        "\n".join(f"- {t}" for t in recent_used) if recent_used else "(none)"
    )
    prompt = IMPROVE_PROMPT.format(
        brand_name=settings.BRAND_NAME,
        title_max=TITLE_MAX_CHARS,
        title_target=TITLE_TARGET_CHARS,
        genre_term=genre_term,
        banned_words=", ".join(OVERUSED_TITLE_WORDS),
        used_titles_block=used_titles_block,
        current_title=mix.title,
        genres=", ".join(mix.genres or []) or "electronic",
        duration=_format_duration(mix.duration_seconds),
        current_description=current_description[:3000] or "(none)",
        retry_feedback=f"\n{retry_feedback}\n" if retry_feedback else "",
    )
    try:
        response, text = await generator._create_completion(
            prompt, max_tokens=1500, temperature=0.7
        )
        if session is not None:
            await generator._track_usage(
                session, mix.id, "catalog_improve_draft",
                response.usage.prompt_tokens, response.usage.completion_tokens,
            )
        data = json.loads(_strip_json_fences(text))
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        if not title or not description:
            return None
        # Shared policy: genre keyword guaranteed, hook (never the keyword)
        # gives up characters when the cap is tight.
        title = enforce_title_shape(title, genre_term)
        return {
            "title": title,
            "description": description,
            "tags_youtube": _clean_tags(data.get("tags_youtube"), YT_TAGS_MAX),
            "tags_soundcloud": _clean_tags(data.get("tags_soundcloud"), SC_TAGS_MAX),
        }
    except Exception as exc:
        logger.warning("LLM improve draft failed for mix %s: %s", mix.id, exc)
        return None


# ---------------------------------------------------------------------------
# Title diversity guard
# ---------------------------------------------------------------------------

def title_diversity_problem(title: str, used_titles: List[str]) -> Optional[str]:
    """Why a drafted title fails the diversity bar, or None when it's fine.

    Fails on any retired stream-era wording (:data:`BANNED_TITLE_PATTERNS`),
    any banned OVERUSED_TITLE_WORDS (word-boundary, case-insensitive), or on
    >= TITLE_SIMILARITY_MAX difflib similarity to an already-used title.
    """
    banned = banned_wording_problem(title)
    if banned:
        return banned
    for word in OVERUSED_TITLE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", title, re.IGNORECASE):
            return f'contains the overused word "{word}"'
    lowered = title.strip().lower()
    for used in used_titles:
        ratio = difflib.SequenceMatcher(None, lowered, used.strip().lower()).ratio()
        if ratio >= TITLE_SIMILARITY_MAX:
            return f'is too similar to the existing title "{used}"'
    return None


async def _title_problem(
    session, title: str, used_titles: List[str], mix_id: Optional[str] = None
) -> Optional[str]:
    """The in-run diversity guard plus the persistent uniqueness registry:
    why a drafted title can't be accepted, or None when it's clear."""
    problem = title_diversity_problem(title, used_titles)
    if problem:
        return problem
    if session is not None:
        from app.services import uniqueness

        if await uniqueness.is_taken(
            session, uniqueness.KIND_TITLE, title, exclude_mix_id=mix_id
        ):
            return "is already claimed in the uniqueness registry"
    return None


async def draft_with_diversity_guard(
    mix: Mix, generator, session, used_titles: List[str]
) -> Optional[Dict[str, Any]]:
    """draft_improvement_llm + the diversity guard: retry once on a banned
    wording, banned word, or too-similar title. If the retry is still bad (or
    fails), keep the best draft we have — but banned wording is never merely
    warned about: it is stripped deterministically before the draft is
    returned, so retired series/date wording can never reach a proposal."""
    genre_term = resolve_title_genre(mix.genres)
    draft = await draft_improvement_llm(
        mix, generator, session, used_titles=used_titles, genre_term=genre_term
    )
    if not draft:
        return None
    problem = await _title_problem(session, draft["title"], used_titles, mix.id)
    if not problem:
        return draft

    feedback = (
        f'YOUR PREVIOUS ATTEMPT "{draft["title"]}" WAS REJECTED: it {problem}. '
        "Too similar, be different: produce a COMPLETELY different title — "
        "different words, different structure."
    )
    retry = await draft_improvement_llm(
        mix, generator, session, used_titles=used_titles,
        retry_feedback=feedback, genre_term=genre_term,
    )
    if retry:
        retry_problem = await _title_problem(
            session, retry["title"], used_titles, mix.id
        )
        if not retry_problem:
            return retry
        draft, problem = retry, retry_problem

    # Two strikes. Banned wording is a hard requirement, not a preference, so
    # remove it by hand rather than shipping the draft as-is.
    if banned_wording_problem(draft["title"]):
        original = draft["title"]
        draft["title"] = sanitize_title(original, genre_term)
        await activity_log.warn(
            "catalog_improve",
            (
                f'Banned wording guard: stripped "{original}" -> '
                f'"{draft["title"]}" for mix {mix.id} after retry — it {problem}.'
            ),
            mix_id=mix.id,
            context={
                "original_title": original,
                "title": draft["title"],
                "problem": problem,
            },
        )
        return draft

    await activity_log.warn(
        "catalog_improve",
        (
            f'Diversity guard: kept title "{draft["title"]}" for mix {mix.id} '
            f"after retry — it still {problem}."
        ),
        mix_id=mix.id,
        context={"title": draft["title"], "problem": problem},
    )
    return draft


# ---------------------------------------------------------------------------
# Proposal creation
# ---------------------------------------------------------------------------

def _mix_platforms(mix: Mix) -> List[str]:
    platforms = []
    if mix.youtube_video_id or mix.youtube_url:
        platforms.append("youtube")
    if mix.soundcloud_track_id or mix.soundcloud_url:
        platforms.append("soundcloud")
    return platforms


async def _has_open_proposal(session, mix_id: str, field: str) -> bool:
    result = await session.execute(
        select(MixProposal.id).where(
            MixProposal.mix_id == mix_id,
            MixProposal.field == field,
            MixProposal.status.in_(["draft", "approved", "applying"]),
        )
    )
    return result.first() is not None


def _title_proposal(
    mix: Mix, title: str, platforms: List[str]
) -> MixProposal:
    """A single title proposal carrying ``title`` for every platform the mix is
    on. ``platform="both"`` is the whole point: one row, one string, applied
    identically to SoundCloud and YouTube."""
    return MixProposal(
        mix_id=mix.id,
        platform="both" if len(platforms) == 2 else platforms[0],
        field="title",
        current_value=mix.title_youtube if platforms == ["youtube"] else mix.title,
        proposed_value=title,
        status="draft",
        created_by="ai",
    )


def titles_diverge(mix: Mix) -> bool:
    """True when the mix carries a different title on YouTube than everywhere
    else. Nothing generates these any more, but the imported back catalog is
    full of them (SoundCloud title vs. the title the video was uploaded with),
    and a divergent pair is a title problem even when both halves read well."""
    yt = (mix.title_youtube or "").strip()
    return bool(yt) and yt != (mix.title or "").strip()


def mix_title_has_banned_wording(mix: Mix) -> Optional[str]:
    """The retired-wording problem in either of a mix's title fields, if any."""
    for value in (mix.title, mix.title_youtube):
        problem = banned_wording_problem(value or "")
        if problem:
            return problem
    return None


def _draft_proposals_for_mix(mix: Mix, draft: Dict[str, Any]) -> List[MixProposal]:
    """Turn an LLM draft into proposal rows for the platforms the mix is on."""
    platforms = _mix_platforms(mix)
    if not platforms:
        return []
    proposals: List[MixProposal] = []

    # Title: ONE string for the mix, never a per-platform variant. When the mix
    # is on both platforms this is a single ``platform="both"`` row, which
    # catalog_apply fans out to the YouTube video AND the SoundCloud track with
    # the identical value — that is what keeps the two platforms from drifting.
    proposals.append(_title_proposal(mix, draft["title"], platforms))

    # Description: assembled per platform (tracklist + links differ); rows are
    # split per platform when values differ, merged into "both" when identical.
    per_platform: Dict[str, Tuple[str, Optional[str]]] = {}
    if "youtube" in platforms:
        assembled = build_platform_description(
            draft["description"],
            extract_tracklist_block(mix.description_youtube or ""),
            settings.YOUTUBE_LINKS,
        )
        per_platform["youtube"] = (assembled, mix.description_youtube)
    if "soundcloud" in platforms:
        assembled = build_platform_description(
            draft["description"],
            extract_tracklist_block(mix.description_soundcloud or ""),
            settings.SOUNDCLOUD_LINKS,
        )
        per_platform["soundcloud"] = (assembled, mix.description_soundcloud)

    values = {v[0] for v in per_platform.values()}
    if len(per_platform) == 2 and len(values) == 1:
        proposed = next(iter(values))
        proposals.append(
            MixProposal(
                mix_id=mix.id,
                platform="both",
                field="description",
                current_value=mix.description_youtube or mix.description_soundcloud,
                proposed_value=proposed,
                status="draft",
                created_by="ai",
            )
        )
    else:
        for platform, (proposed, current) in per_platform.items():
            proposals.append(
                MixProposal(
                    mix_id=mix.id,
                    platform=platform,
                    field="description",
                    current_value=current,
                    proposed_value=proposed,
                    status="draft",
                    created_by="ai",
                )
            )

    # Tags rider: per-platform discoverability tag proposals (JSON-encoded,
    # like every structured proposed_value). Only when the LLM produced them.
    current_tags = json.dumps(mix.tags or [])
    for platform, key in (("youtube", "tags_youtube"), ("soundcloud", "tags_soundcloud")):
        tags = draft.get(key) or []
        if platform in platforms and tags:
            proposals.append(
                MixProposal(
                    mix_id=mix.id,
                    platform=platform,
                    field="tags",
                    current_value=current_tags,
                    proposed_value=json.dumps(tags),
                    status="draft",
                    created_by="ai",
                )
            )
    return proposals


async def _fetch_used_titles(session) -> List[str]:
    """All titles already claimed: every mix title (oldest first, so the most
    recent land at the tail the prompt shows) plus every open or applied title
    proposal value. Case-insensitively deduped, order preserved."""
    mix_titles = (
        (await session.execute(select(Mix.title).order_by(Mix.created_at.asc())))
        .scalars()
        .all()
    )
    proposal_titles = (
        (
            await session.execute(
                select(MixProposal.proposed_value).where(
                    MixProposal.field == "title",
                    MixProposal.status.in_(
                        ["draft", "approved", "applying", "applied"]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    seen = set()
    out: List[str] = []
    for title in [*mix_titles, *proposal_titles]:
        t = (title or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_improve(mix_ids: Optional[Any] = None) -> Dict[str, Any]:
    """Classify + draft improvements. ``mix_ids`` is a list of mix ids or the
    literal ``"all_generic"`` (default) for every unlocked cataloged mix."""
    from app.services.description_generator import DescriptionGenerator

    summary: Dict[str, Any] = {
        "classified": 0, "keepers_locked": 0, "generic": 0,
        "proposals_drafted": 0, "skipped": 0, "divergent_unified": 0,
    }

    async with async_session_factory() as session:
        settings_result = await session.execute(
            select(AppSettings).where(AppSettings.id == 1)
        )
        settings_row = settings_result.scalar_one_or_none()
        sj = dict(settings_row.settings_json or {}) if settings_row else {}

        query = select(Mix)
        if isinstance(mix_ids, list):
            query = query.where(Mix.id.in_(mix_ids))
        mixes = list((await session.execute(query)).scalars().all())
        mixes = [m for m in mixes if _mix_platforms(m)]
        if not isinstance(mix_ids, list):
            # "all_generic" / None -> every unlocked mix, PLUS locked ones whose
            # title is still wrong in a way locking was never meant to bless:
            # retired series/date wording, or a YouTube title that disagrees
            # with the SoundCloud one.
            mixes = [
                m
                for m in mixes
                if not m.title_locked
                or mix_title_has_banned_wording(m)
                or titles_diverge(m)
            ]

        generator = DescriptionGenerator(sj)

        # Pass 1: heuristic classify, batch the unknowns through the LLM.
        classes: Dict[str, str] = {}
        unknown: List[Mix] = []
        for mix in mixes:
            banned = mix_title_has_banned_wording(mix)
            if banned:
                # Overrides both the lock and the LLM: this title must change.
                classes[mix.id] = "generic"
                mix.title_locked = False
                continue
            if mix.title_locked:
                classes[mix.id] = "keeper"
                continue
            verdict = classify_title_heuristic(mix.title)
            if verdict == "unknown":
                unknown.append(mix)
            else:
                classes[mix.id] = verdict
        if unknown:
            llm_verdicts = await classify_titles_llm(
                [m.title for m in unknown], generator, session
            )
            for mix, verdict in zip(unknown, llm_verdicts):
                classes[mix.id] = verdict
        summary["classified"] = len(mixes)

        # Keepers: lock the title; never propose title changes.
        for mix in mixes:
            if classes.get(mix.id) == "keeper" and not mix.title_locked:
                mix.title_locked = True
                summary["keepers_locked"] += 1
        await session.commit()

        # Pass 2: draft proposals for generics. ``used_titles`` starts from the
        # DB (all mix titles + open/applied title proposals) and accumulates
        # every title drafted in this run so later drafts can't repeat them.
        used_titles = await _fetch_used_titles(session)
        for mix in mixes:
            if classes.get(mix.id) != "generic":
                continue
            summary["generic"] += 1
            if await _has_open_proposal(session, mix.id, "title"):
                summary["skipped"] += 1
                continue
            draft = await draft_with_diversity_guard(
                mix, generator, session, used_titles
            )
            if not draft:
                summary["skipped"] += 1
                continue
            used_titles.append(draft["title"])
            # Hard uniqueness: claim the accepted title in the registry so no
            # other flow (pipeline, series, later improve runs) can reuse it.
            from app.services import uniqueness

            await uniqueness.claim(
                session, uniqueness.KIND_TITLE, draft["title"], mix.id
            )
            for proposal in _draft_proposals_for_mix(mix, draft):
                session.add(proposal)
                summary["proposals_drafted"] += 1
            await session.commit()

        # Pass 3: unify keepers that diverge across platforms. Their wording is
        # fine — they just carry a different string on YouTube than everywhere
        # else, which is a data problem, not a creative one. No LLM: adopt the
        # canonical ``mix.title`` (shape-enforced so it still carries the genre
        # keyword) for both platforms.
        for mix in mixes:
            if classes.get(mix.id) != "keeper" or not titles_diverge(mix):
                continue
            if await _has_open_proposal(session, mix.id, "title"):
                summary["skipped"] += 1
                continue
            unified = enforce_title_shape(
                mix.title or "", resolve_title_genre(mix.genres)
            )
            session.add(_title_proposal(mix, unified, _mix_platforms(mix)))
            summary["proposals_drafted"] += 1
            summary["divergent_unified"] += 1
            await session.commit()

        await session.commit()

    await activity_log.info(
        "catalog_improve",
        (
            f"AI improve finished: {summary['keepers_locked']} keeper titles locked, "
            f"{summary['proposals_drafted']} proposals drafted for "
            f"{summary['generic']} generic mixes "
            f"({summary['divergent_unified']} platform-divergent titles unified)."
        ),
        context=summary,
    )
    return summary
