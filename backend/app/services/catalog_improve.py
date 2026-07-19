"""AI improve for the back catalog: keeper/generic title triage + draft proposals.

``run_improve`` (behind ``POST /api/catalog/improve``) works in two passes:

1. **Classify** every target mix title as ``keeper`` (unique/creative — locked
   via ``title_locked``, never proposed again) or ``generic`` (raid-train
   patterns, date-only titles, "DJ set" boilerplate). A cheap heuristic decides
   the obvious cases; the ambiguous middle ground goes to the LLM in one batch.
2. **Draft** for each generic mix: a click-optimized title (<=70 chars, genre +
   hook, no clickbait lies) and a refreshed full description that *retains the
   existing tracklist section* and the brand links block. Drafts land as
   ``MixProposal`` rows (``created_by="ai"``, status ``draft``) — platform
   ``both`` for shared values, split per-platform where the values differ.

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

TITLE_MAX_CHARS = 70

# Words the improve LLM has historically leaned on to the point of parody
# ("Odyssey" showed up in 15+ of 60 drafts). Banned outright in the prompt and
# enforced by the post-generation diversity guard.
OVERUSED_TITLE_WORDS = ["odyssey", "sonic", "journey", "voyage", "exploration"]

# A drafted title this difflib-similar to any already-used title triggers a
# retry (and, failing that, an activity warning).
TITLE_SIMILARITY_MAX = 0.8

# How many already-used titles to show the LLM (most recent last -> tail).
MAX_USED_TITLES_IN_PROMPT = 40

# ---------------------------------------------------------------------------
# Heuristic title classifier
# ---------------------------------------------------------------------------

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
- Click-optimized but honest: genre keywords people search for + a hook
- <= {title_max} characters
- No clickbait lies, no emojis, no dates
- Do not include the words "raid train", "twitch", or "stream"
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
    recent_used = (used_titles or [])[-MAX_USED_TITLES_IN_PROMPT:]
    used_titles_block = (
        "\n".join(f"- {t}" for t in recent_used) if recent_used else "(none)"
    )
    prompt = IMPROVE_PROMPT.format(
        brand_name=settings.BRAND_NAME,
        title_max=TITLE_MAX_CHARS,
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
        if len(title) > TITLE_MAX_CHARS:
            title = title[: TITLE_MAX_CHARS - 3].rstrip() + "..."
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

    Fails on any banned OVERUSED_TITLE_WORDS (word-boundary, case-insensitive)
    or on >= TITLE_SIMILARITY_MAX difflib similarity to an already-used title.
    """
    for word in OVERUSED_TITLE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", title, re.IGNORECASE):
            return f'contains the overused word "{word}"'
    lowered = title.strip().lower()
    for used in used_titles:
        ratio = difflib.SequenceMatcher(None, lowered, used.strip().lower()).ratio()
        if ratio >= TITLE_SIMILARITY_MAX:
            return f'is too similar to the existing title "{used}"'
    return None


async def draft_with_diversity_guard(
    mix: Mix, generator, session, used_titles: List[str]
) -> Optional[Dict[str, Any]]:
    """draft_improvement_llm + the diversity guard: retry once on a banned-word
    or too-similar title; if the retry is still bad (or fails), keep the best
    draft we have but log an activity warning."""
    draft = await draft_improvement_llm(
        mix, generator, session, used_titles=used_titles
    )
    if not draft:
        return None
    problem = title_diversity_problem(draft["title"], used_titles)
    if not problem:
        return draft

    feedback = (
        f'YOUR PREVIOUS ATTEMPT "{draft["title"]}" WAS REJECTED: it {problem}. '
        "Too similar, be different: produce a COMPLETELY different title — "
        "different words, different structure."
    )
    retry = await draft_improvement_llm(
        mix, generator, session, used_titles=used_titles, retry_feedback=feedback
    )
    if retry:
        retry_problem = title_diversity_problem(retry["title"], used_titles)
        if not retry_problem:
            return retry
        draft, problem = retry, retry_problem

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


def _draft_proposals_for_mix(mix: Mix, draft: Dict[str, Any]) -> List[MixProposal]:
    """Turn an LLM draft into proposal rows for the platforms the mix is on."""
    platforms = _mix_platforms(mix)
    if not platforms:
        return []
    proposals: List[MixProposal] = []

    # Title: one shared value -> a single row ("both" when on both platforms).
    title_platform = "both" if len(platforms) == 2 else platforms[0]
    proposals.append(
        MixProposal(
            mix_id=mix.id,
            platform=title_platform,
            field="title",
            current_value=mix.title_youtube if platforms == ["youtube"] else mix.title,
            proposed_value=draft["title"],
            status="draft",
            created_by="ai",
        )
    )

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
        "proposals_drafted": 0, "skipped": 0,
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
        else:  # "all_generic" / None -> every unlocked mix present on a platform
            query = query.where(Mix.title_locked.is_(False))
        mixes = list((await session.execute(query)).scalars().all())
        mixes = [m for m in mixes if _mix_platforms(m)]

        generator = DescriptionGenerator(sj)

        # Pass 1: heuristic classify, batch the unknowns through the LLM.
        classes: Dict[str, str] = {}
        unknown: List[Mix] = []
        for mix in mixes:
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
            for proposal in _draft_proposals_for_mix(mix, draft):
                session.add(proposal)
                summary["proposals_drafted"] += 1
            await session.commit()
        await session.commit()

    await activity_log.info(
        "catalog_improve",
        (
            f"AI improve finished: {summary['keepers_locked']} keeper titles locked, "
            f"{summary['proposals_drafted']} proposals drafted for "
            f"{summary['generic']} generic mixes."
        ),
        context=summary,
    )
    return summary
