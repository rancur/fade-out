"""Rename the source audio/video on disk to match a mix's generated title.

fade-out invents a click-optimized title at the ``generate_description`` step
and publishes it everywhere, but the recordings on the NAS keep whatever OBS or
the DJ software called them. The archive on disk therefore never matches the
published catalog. This module closes that gap: once a run has fully completed
(or a new title is applied from the catalog) it renames the source files to
``YYYY-MM-DD <title>.<ext>`` and updates ``Mix.audio_file_path`` /
``Mix.video_file_path`` to follow.

**This is the only code in the backend that writes to the watch folders.**
Everything else opens the source read-only (see ``file_watcher``). That makes
the safety rails, not the ``os.rename`` call, the substance of this module:

* **Opt-in.** Nothing happens unless the ``rename_source_files`` setting is on
  AND the bind mounts are ``:rw``. Either one missing is a clean, logged no-op.
* **Allowlisted.** Only files inside ``WATCH_AUDIO_PATH`` / ``WATCH_VIDEO_PATH``
  are eligible. ``POST /api/mixes`` accepts a caller-supplied
  ``audio_file_path``, so without this rail the feature would be an
  arbitrary-path rename primitive.
* **Never destructive.** An occupied target name is suffixed ``(2)``…``(9)`` and
  then refused. POSIX ``os.rename`` silently clobbers, and the thing it would
  clobber is a multi-gigabyte recording.
* **Never fatal.** Every failure mode — read-only mount, permissions, a file
  that vanished — is caught, classified, and logged. A rename can not fail a
  pipeline run.

**The date prefix is load-bearing, not decoration.** Three existing matchers key
off a date token in the filename: ``ingest._pairing_key`` / ``_find_sibling``,
and ``catalog_backfill.extract_filename_date`` / ``match_local_audio``. The
token is taken from the ORIGINAL filename and the SAME token is applied to both
files, so audio and video come out with byte-identical stems — which actually
upgrades ``_find_sibling`` from its fuzzy same-date branch to its exact-stem
branch.
"""

import asyncio
import errno
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.database import async_session_factory
from app.models import Mix

logger = logging.getLogger(__name__)


# Per-component filename ceiling on ext4 and NTFS alike. It is a BYTE budget,
# not a character count -- a title full of multibyte characters hits it roughly
# three times sooner than its length suggests.
MAX_NAME_BYTES = 255
# Held back from the budget so the " (2)" collision suffix can always be
# appended without re-truncating and changing the stem.
COLLISION_RESERVE_BYTES = 8
# Never truncate the title below this, however long the date prefix runs.
MIN_STEM_BYTES = 32
# Collision suffixes tried before giving up: "name (2).flac" ... "name (9).flac".
MAX_COLLISION_ATTEMPTS = 9

# ext4 forbids only "/" and NUL, but these files are read back over SMB from
# macOS and Windows, so sanitize to the strictest common set. Characters that
# carry meaning are mapped rather than dropped, so "Neon Drift | House Mix"
# reads as "Neon Drift - House Mix" instead of "Neon Drift  House Mix".
_ILLEGAL_MAP = {
    ":": " -",
    "/": "-",
    "\\": "-",
    "|": "-",
    '"': "'",
    "<": "",
    ">": "",
    "?": "",
    "*": "",
}

# Unicode categories dropped outright: control, format, surrogate, private-use,
# unassigned, and "other symbol" (So = emoji). Emoji are legal on NTFS and SMB
# but hostile to shell globbing, rsync and backup tooling, and a DJ mix filename
# gains nothing from them.
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "So"})

# Windows/SMB device names. Unreachable once a date prefix is present; handled
# for the no-date branch.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_WHITESPACE_RE = re.compile(r"\s+")

# Serializes the check-and-rename across concurrently completing pipelines
# (MAX_CONCURRENT_PIPELINES defaults to 2) so two mixes can never both see the
# same target name as free.
_RENAME_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Pure helpers -- no I/O, no DB. The bulk of the correctness surface lives here.
# ---------------------------------------------------------------------------

def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Trim ``value`` to at most ``max_bytes`` UTF-8 bytes, on a clean boundary.

    Slicing the encoded bytes can land mid-codepoint; ``errors="ignore"`` on the
    way back is what discards that partial tail. A word break is preferred when
    one falls in the last 40% of the budget, so a truncated title reads as a
    short title rather than a corrupted one.
    """
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    cut = raw[:max_bytes].decode("utf-8", "ignore")
    idx = cut.rfind(" ")
    if idx >= int(len(cut) * 0.6):
        cut = cut[:idx]
    return cut.rstrip(" .-")


def sanitize_title_for_filename(title: str, *, max_bytes: int = 200) -> str:
    """Turn a mix title into one safe filename component. Never returns "".

    Order matters here. NFC normalization comes first because macOS hands back
    decomposed (NFD) strings over SMB while Linux and Windows produce composed
    ones, and ``ingest._find_sibling`` compares stems with ``==``. The trailing
    ``.``/`` `` strip comes last, AFTER truncation, because SMB and Windows
    physically cannot store a name ending in either and truncation can create
    one.
    """
    text = unicodedata.normalize("NFC", title or "")

    out: List[str] = []
    for ch in text:
        if ch in _ILLEGAL_MAP:
            out.append(_ILLEGAL_MAP[ch])
        elif unicodedata.category(ch) in _DROP_CATEGORIES:
            # A space, not nothing: a tab or newline between two words has to
            # stay a word boundary, or "A\tB" comes out as "AB". The collapse
            # below removes the surplus.
            out.append(" ")
        else:
            out.append(ch)
    text = "".join(out)

    # Collapses the double space the ":" -> " -" mapping creates, and folds any
    # tab/newline a pasted title dragged in.
    text = _WHITESPACE_RE.sub(" ", text).strip()
    # No hidden dotfiles, and no leading dash that reads as a CLI flag.
    text = text.lstrip(". -")
    text = _truncate_utf8(text, max_bytes)
    text = text.rstrip(" .")

    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text = f"_{text}"

    return text or "mix"


def resolve_date_token(
    *filenames: str,
    source: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> Optional[str]:
    """The ``YYYY-MM-DD`` token to prefix, or None to omit the prefix.

    Prefers a date already present in one of the original filenames, reusing
    ``catalog_backfill.extract_filename_date`` so a ``MM-DD-YYYY`` original is
    normalized on the way through -- which strictly improves pairing, since
    ``ingest._extract_date`` only understands the ISO form.

    Falling back to ``created_at`` is only honest for pipeline mixes, which are
    ingested the same day they are recorded. An imported back-catalog mix has a
    ``created_at`` of the catalog-sync run, so stamping it on would invent a
    date that ``catalog_backfill.match_local_audio`` then scores against real
    publish dates. Those get no prefix at all.
    """
    from app.services.catalog_backfill import extract_filename_date

    for name in filenames:
        if not name:
            continue
        found = extract_filename_date(os.path.basename(name))
        if found:
            return found.isoformat()

    if source == "pipeline" and created_at is not None:
        value = created_at.date() if isinstance(created_at, datetime) else created_at
        if isinstance(value, date):
            return value.isoformat()
    return None


def build_target_name(title: str, date_token: Optional[str], ext: str) -> str:
    """Assemble the final ``YYYY-MM-DD <title><ext>`` filename component."""
    prefix = f"{date_token} " if date_token else ""
    ext = ext.lower()
    budget = (
        MAX_NAME_BYTES
        - len(prefix.encode("utf-8"))
        - len(ext.encode("utf-8"))
        - COLLISION_RESERVE_BYTES
    )
    stem = sanitize_title_for_filename(title, max_bytes=max(budget, MIN_STEM_BYTES))
    return f"{prefix}{stem}{ext}"


def allowed_roots() -> List[str]:
    """Directories this module may rename inside. Everything else is refused.

    Deliberately just the two watch folders. ``CATALOG_EXTRA_AUDIO_PATHS`` is a
    read-only back-catalog archive the operator never signed up to have mutated
    (and a backfilled mix really does get its ``audio_file_path`` from there);
    ``SHORTS_WATCH_PATH`` and ``DJCTL_CUE_PATH`` belong to other features.
    """
    roots: List[str] = []
    for path in (settings.WATCH_AUDIO_PATH, settings.WATCH_VIDEO_PATH):
        if path:
            roots.append(os.path.realpath(path))
    return roots


def is_within_allowed_roots(path: str, roots: Optional[List[str]] = None) -> bool:
    """True when ``path`` sits inside one of the allowed roots.

    Uses ``commonpath`` rather than ``str.startswith``: the latter happily
    accepts ``/watch/audio-archive`` for the root ``/watch/audio``. The parent
    directory is resolved with ``realpath`` so a symlinked mount still matches,
    while a symlinked *file* is refused separately in the executor.
    """
    if not path:
        return False
    roots = allowed_roots() if roots is None else roots
    parent = os.path.realpath(os.path.dirname(path))
    for root in roots:
        try:
            if os.path.commonpath([root, parent]) == root:
                return True
        except ValueError:  # different drives, or relative vs absolute
            continue
    return False


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class _RenamePlan:
    kind: str      # "audio" | "video"
    field: str     # Mix column to update
    current: str
    target: str


# errno -> stable reason code. Codes are asserted on in tests and surfaced in
# activity-log context, so they are part of this module's contract.
_ERRNO_REASONS = {
    errno.EROFS: "read_only_fs",
    errno.EACCES: "permission_denied",
    errno.EPERM: "permission_denied",
    errno.ENOENT: "not_found",
    errno.EXDEV: "cross_device",
    errno.ENAMETOOLONG: "name_too_long",
}

# Refusals worth a warn-level activity event; the rest are routine.
NOTABLE_SKIP_REASONS = frozenset(
    {"read_only_fs", "permission_denied", "collision", "outside_allowed_roots",
     "cross_device", "name_too_long", "os_error"}
)


def _build_plans(
    title: str,
    source: Optional[str],
    created_at: Optional[datetime],
    paths: Dict[str, Optional[str]],
) -> Tuple[List[_RenamePlan], List[Dict[str, Any]], Dict[str, str]]:
    """Plan both renames. Returns ``(plans, refusals, adoptions)``.

    ``adoptions`` covers the self-heal case: the recorded path is gone but the
    name we would have produced is sitting right there in the same directory.
    That happens after a crash between the rename and the commit, after a manual
    rename by the operator, and on a re-run following a partial failure. Taking
    the DB pointer to it costs one ``lexists`` and removes the feature's worst
    failure mode.
    """
    roots = allowed_roots()
    refusals: List[Dict[str, Any]] = []
    adoptions: Dict[str, str] = {}
    plans: List[_RenamePlan] = []

    token = resolve_date_token(
        paths.get("audio") or "",
        paths.get("video") or "",
        source=source,
        created_at=created_at,
    )

    for kind, field in (("audio", "audio_file_path"), ("video", "video_file_path")):
        current = paths.get(kind)
        if not current:
            continue
        if not is_within_allowed_roots(current, roots):
            refusals.append(
                {"kind": kind, "reason": "outside_allowed_roots", "path": current}
            )
            continue

        target_name = build_target_name(title, token, os.path.splitext(current)[1])
        target = os.path.join(os.path.dirname(current), target_name)

        if not os.path.lexists(current) and os.path.lexists(target):
            adoptions[field] = target
            refusals.append(
                {"kind": kind, "reason": "adopted", "path": current, "target": target}
            )
            continue

        plans.append(_RenamePlan(kind=kind, field=field, current=current, target=target))

    return plans, refusals, adoptions


def _collision_candidate(target: str, attempt: int) -> str:
    if attempt == 1:
        return target
    stem, ext = os.path.splitext(target)
    return f"{stem} ({attempt}){ext}"


# ---------------------------------------------------------------------------
# Blocking execution -- one thread hop for the whole mix
# ---------------------------------------------------------------------------

def _execute_one(plan: _RenamePlan) -> Dict[str, Any]:
    if not os.path.lexists(plan.current):
        return {"status": "skipped", "reason": "not_found"}

    # A symlink is refused rather than followed: renaming the link leaves the DB
    # pointing at an alias whose name now lies about its target, and renaming
    # the target mutates a file outside the allowlist we just checked.
    if os.path.islink(plan.current):
        return {"status": "skipped", "reason": "symlink"}

    if os.path.basename(plan.current) == os.path.basename(plan.target):
        return {"status": "unchanged"}

    for attempt in range(1, MAX_COLLISION_ATTEMPTS + 1):
        candidate = _collision_candidate(plan.target, attempt)
        if not os.path.lexists(candidate):
            os.rename(plan.current, candidate)
            return {"status": "renamed", "to": candidate}
        # On a case-insensitive filesystem (macOS dev, Synology over SMB) a
        # case-only rename sees its own target as occupied. Without this the
        # result would be "set (2).flac" instead of the intended "set.flac".
        if os.path.samefile(plan.current, candidate):
            return {"status": "unchanged"}

    return {"status": "skipped", "reason": "collision", "target": plan.target}


def _execute_plan(plans: List[_RenamePlan]) -> List[Dict[str, Any]]:
    """Every blocking filesystem call for one mix. Never raises."""
    outcomes: List[Dict[str, Any]] = []
    for plan in plans:
        try:
            outcomes.append(_execute_one(plan))
        except OSError as exc:
            reason = _ERRNO_REASONS.get(exc.errno, "os_error")
            if reason == "name_too_long":
                # Belt-and-braces against a share with a sub-255 ceiling: retry
                # once at the smallest stem we are willing to produce.
                try:
                    stem_ext = os.path.splitext(plan.target)
                    short = os.path.join(
                        os.path.dirname(plan.target),
                        _truncate_utf8(os.path.basename(stem_ext[0]), MIN_STEM_BYTES)
                        + stem_ext[1],
                    )
                    os.rename(plan.current, short)
                    outcomes.append({"status": "renamed", "to": short})
                    continue
                except OSError:
                    pass
            logger.warning(
                "Source rename failed (%s) for %s -> %s: %s",
                reason, plan.current, plan.target, exc,
            )
            outcomes.append({"status": "error", "reason": reason, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unexpected error renaming %s", plan.current)
            outcomes.append({"status": "error", "reason": "os_error", "error": str(exc)})
    return outcomes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def rename_sources_for_mix(
    mix_id: str, *, reason: str, dry_run: bool = False
) -> Dict[str, Any]:
    """Rename this mix's sources to match its title and persist the new paths.

    Best-effort by contract: this never raises and never fails a pipeline run.
    ``dry_run`` reports the plan without touching disk (and ignores the setting,
    so it works as a preview before the feature is switched on).

    The session discipline matters. SQLite takes its single write lock at the
    first flush and holds it to COMMIT, and an ``os.rename`` against a sleeping
    NAS can block for seconds -- so the read session is closed before any I/O,
    the I/O happens in one ``to_thread`` hop, and a second short session commits
    only the columns that actually moved.
    """
    result: Dict[str, Any] = {
        "mix_id": mix_id,
        "reason": reason,
        "dry_run": dry_run,
        "renamed": [],
        "skipped": [],
        "errors": [],
    }
    try:
        from app.services import app_config

        if not dry_run and not bool(await app_config.resolve("rename_source_files")):
            result["skipped"].append({"reason": "disabled"})
            return result

        async with async_session_factory() as session:
            mix = await session.get(Mix, mix_id)
            if mix is None:
                result["skipped"].append({"reason": "mix_missing"})
                return result
            title = mix.title
            source = mix.source
            created_at = mix.created_at
            paths = {"audio": mix.audio_file_path, "video": mix.video_file_path}

        if not title or title == "Untitled":
            result["skipped"].append({"reason": "no_title"})
            return result

        plans, refusals, adoptions = _build_plans(title, source, created_at, paths)
        result["skipped"].extend(refusals)

        if dry_run:
            result["planned"] = [
                {"kind": p.kind, "from": p.current, "to": p.target} for p in plans
            ]
            return result

        outcomes: List[Dict[str, Any]] = []
        if plans:
            async with _RENAME_LOCK:
                outcomes = await asyncio.to_thread(_execute_plan, plans)

        moved: Dict[str, str] = dict(adoptions)
        for plan, outcome in zip(plans, outcomes):
            status = outcome.get("status")
            if status == "renamed":
                moved[plan.field] = outcome["to"]
                result["renamed"].append(
                    {"kind": plan.kind, "from": plan.current, "to": outcome["to"]}
                )
            elif status == "error":
                result["errors"].append(
                    {
                        "kind": plan.kind,
                        "reason": outcome.get("reason"),
                        "path": plan.current,
                        "error": outcome.get("error"),
                    }
                )
            else:
                entry = {"kind": plan.kind, "reason": outcome.get("reason", "unchanged")}
                if outcome.get("target"):
                    entry["target"] = outcome["target"]
                result["skipped"].append(entry)

        # A partial outcome is committed as-is: each column independently tracks
        # what is actually on disk. Rolling the audio rename back would need a
        # second rename that can fail in exactly the same way, leaving a worse
        # mess than the honest partial state. The next invocation retries.
        if moved:
            async with async_session_factory() as session:
                mix = await session.get(Mix, mix_id)
                if mix is not None:
                    for field, new_path in moved.items():
                        setattr(mix, field, new_path)
                    await session.commit()

        _sync_seen_files(result["renamed"])
        await _log_outcome(mix_id, reason, result)
    except Exception:  # pragma: no cover - defensive; callers must never see this
        logger.exception("Source rename failed for mix %s", mix_id)
    return result


def _sync_seen_files(renamed: List[Dict[str, Any]]) -> None:
    """Keep the watcher's seen-files record pointing at the new names.

    Cosmetic: dedupe is keyed on the file's content hash, which a rename does
    not touch, so a stale row here can never cause a re-ingest. Best-effort --
    the watcher is not running under pytest, and it must not matter if it isn't.
    """
    if not renamed:
        return
    try:
        from app.services.file_watcher import get_active_watcher

        watcher = get_active_watcher()
        if watcher is None:
            return
        for item in renamed:
            watcher.note_renamed(item["from"], item["to"])
    except Exception:  # pragma: no cover - never worth failing a rename over
        logger.debug("Could not update seen-files paths after rename", exc_info=True)


async def _log_outcome(mix_id: str, reason: str, result: Dict[str, Any]) -> None:
    from app.services import activity_log

    renamed = result["renamed"]
    notable = [
        item for item in result["skipped"] + result["errors"]
        if item.get("reason") in NOTABLE_SKIP_REASONS
    ]

    if renamed:
        audio = next((r for r in renamed if r["kind"] == "audio"), renamed[0])
        summary = ", ".join(
            f"{r['kind']} → {os.path.basename(r['to'])}" for r in renamed
        )
        await activity_log.info(
            "source_files_renamed",
            f"Renamed source files to match title: {summary}",
            mix_id=mix_id,
            filename=os.path.basename(audio["to"]),
            stage=reason,
            context=result,
        )
    if notable:
        detail = ", ".join(f"{i.get('kind', 'source')}: {i['reason']}" for i in notable)
        await activity_log.warn(
            "source_rename_skipped",
            f"Source rename skipped ({detail})",
            mix_id=mix_id,
            stage=reason,
            context=result,
        )
