"""Uniqueness registry: the system-enforced "EVERYTHING unique" ledger.

Every creative value the app commits — mix titles, thumbnail hook texts, scene
descriptors — is claimed in the ``used_creative`` table so it can never be
produced again. Prompt engineering asks the LLM to be different; this module
*enforces* it: generators check :func:`is_taken` before accepting a value,
retry with context when it collides, and :func:`claim` the winner.

Comparison model (kept deliberately O(N)-simple):

* **Exact** — normalized values (lowercase, punctuation stripped, whitespace
  collapsed; titles additionally drop a leading series prefix so
  "Will See Wednesdays: X" vs "Will See Wednesdays: Y" compares X vs Y) are
  matched via an indexed equality lookup over the whole ledger.
* **Fuzzy** — difflib ratio against the most recent :data:`RECENT_WINDOW`
  values of the same kind. Thresholds per kind in
  :data:`SIMILARITY_THRESHOLDS`; hooks are exact-match only (2-3 word strings
  make ratio comparisons absurdly aggressive).

Regeneration flow: :func:`release_for_mix` drops a mix's own previous claims
first so a mix never collides with itself when its art or title is redone.
"""

import difflib
import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func as sa_func, select

from app.models import UsedCreative

logger = logging.getLogger(__name__)

KIND_TITLE = "title"
KIND_HOOK = "hook"
KIND_SCENE = "scene"
KINDS = (KIND_TITLE, KIND_HOOK, KIND_SCENE)

# difflib ratio at/above which a value counts as taken. ``None`` = exact only.
SIMILARITY_THRESHOLDS: Dict[str, Optional[float]] = {
    KIND_TITLE: 0.8,
    KIND_HOOK: None,  # 2-3 word hooks: similarity is far too aggressive
    KIND_SCENE: 0.75,
}

# How many recent rows of a kind the fuzzy pass scans.
RECENT_WINDOW = 500

# Series prefixes are shared branding, not creative content: the uniqueness
# comparison for titles runs on the post-prefix part. MUST stay in sync with
# the inlined copy in migration 0006 (seed backfill).
_SERIES_PREFIX_RE = re.compile(
    r"^\s*(?:will\s*see\s*wednesdays?|(?:2nd|second)\s*saturdays?)\s*[:|\-–—]+\s*",
    re.IGNORECASE,
)

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def normalize(kind: str, value: str) -> str:
    """The comparison key for a creative value: casefolded, punctuation
    stripped, whitespace collapsed; titles also lose a leading series prefix."""
    v = (value or "").strip()
    if kind == KIND_TITLE:
        v = _SERIES_PREFIX_RE.sub("", v)
    v = v.lower()
    v = _PUNCT_RE.sub(" ", v)
    return _WS_RE.sub(" ", v).strip()


async def is_taken(
    session,
    kind: str,
    value: str,
    exclude_mix_id: Optional[str] = None,
    fuzzy: bool = True,
) -> bool:
    """Whether ``value`` is already claimed for ``kind``.

    Exact normalized match over the whole ledger, then (for kinds with a
    threshold, unless ``fuzzy=False`` — used by deterministic suffix
    fallbacks, where "Base II" must not fuzzy-match "Base") a difflib pass
    over the most recent :data:`RECENT_WINDOW` rows. ``exclude_mix_id``
    ignores the mix's own claims (regeneration).
    """
    norm = normalize(kind, value)
    if not norm:
        return False

    exact = select(UsedCreative.id).where(
        UsedCreative.kind == kind,
        UsedCreative.value_normalized == norm,
    )
    if exclude_mix_id:
        exact = exact.where(
            (UsedCreative.mix_id.is_(None)) | (UsedCreative.mix_id != exclude_mix_id)
        )
    if (await session.execute(exact.limit(1))).first() is not None:
        return True

    threshold = SIMILARITY_THRESHOLDS.get(kind)
    if threshold is None or not fuzzy:
        return False

    recent = select(UsedCreative.value_normalized).where(UsedCreative.kind == kind)
    if exclude_mix_id:
        recent = recent.where(
            (UsedCreative.mix_id.is_(None)) | (UsedCreative.mix_id != exclude_mix_id)
        )
    rows = (
        (await session.execute(recent.order_by(UsedCreative.id.desc()).limit(RECENT_WINDOW)))
        .scalars()
        .all()
    )
    for used in rows:
        if difflib.SequenceMatcher(None, norm, used).ratio() >= threshold:
            return True
    return False


async def claim(
    session, kind: str, value: str, mix_id: Optional[str] = None
) -> Optional[UsedCreative]:
    """Register ``value`` as used. Idempotent: an existing exact-normalized row
    is returned untouched (except a null ``mix_id`` adopting the claimant), so
    re-applying a proposal or re-running a step never duplicates rows."""
    norm = normalize(kind, value)
    if not norm:
        return None
    existing = (
        await session.execute(
            select(UsedCreative)
            .where(
                UsedCreative.kind == kind,
                UsedCreative.value_normalized == norm,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.mix_id is None and mix_id:
            existing.mix_id = mix_id
        return existing
    row = UsedCreative(
        kind=kind, value_normalized=norm, value_raw=value, mix_id=mix_id
    )
    session.add(row)
    await session.flush()
    return row


async def release_for_mix(session, mix_id: str, kind: Optional[str] = None) -> int:
    """Drop a mix's own claims (optionally one kind) so regeneration can
    re-claim without colliding with the mix's previous values."""
    if not mix_id:
        return 0
    stmt = delete(UsedCreative).where(UsedCreative.mix_id == mix_id)
    if kind:
        stmt = stmt.where(UsedCreative.kind == kind)
    result = await session.execute(stmt)
    return result.rowcount or 0


async def recent_values(session, kind: str, limit: int = 40) -> List[str]:
    """The most recent raw values of a kind, oldest first (prompt context)."""
    rows = (
        (
            await session.execute(
                select(UsedCreative.value_raw)
                .where(UsedCreative.kind == kind)
                .order_by(UsedCreative.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


async def stats(session) -> Dict[str, Any]:
    """Cheap observability: per-kind counts + the latest claims."""
    counts_rows = (
        await session.execute(
            select(UsedCreative.kind, sa_func.count()).group_by(UsedCreative.kind)
        )
    ).all()
    counts = {kind: 0 for kind in KINDS}
    counts.update({kind: n for kind, n in counts_rows})

    recent_rows = (
        (
            await session.execute(
                select(UsedCreative)
                .order_by(UsedCreative.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "recent": [
            {
                "kind": r.kind,
                "value": r.value_raw,
                "mix_id": r.mix_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in recent_rows
        ],
    }
