"""Typed platform failures and the classifier that names a proximate cause.

Why this exists: on 2026-08-12 a dead SoundCloud refresh token was reported to
the operator as ``Page.wait_for_selector: Timeout 10000ms exceeded``. The
notification named a Playwright symptom, so whoever read it went hunting in the
browser automation while the real problem — ``invalid_grant`` on an OAuth
credential — sat untouched for two days.

Every failure that leaves this module carries three things the alert must be
able to say out loud: WHICH platform, WHICH credential (name only, never the
value), and WHETHER retrying can possibly help. An auth failure is not
retryable — burning three attempts on it only delays the operator seeing it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PLATFORMS = ("soundcloud", "youtube", "mixcloud")

# Fragments that mean "the credential is dead", not "the network hiccuped".
_AUTH_MARKERS = (
    "invalid_grant",
    "invalid_token",
    "unauthorized",
    "401",
    "token has been expired or revoked",
    "invalid_client",
    "unsupported_grant_type",
)


class PlatformAuthError(RuntimeError):
    """A platform rejected every credential path we have.

    ``credential`` names the env/setting key that needs re-authorizing. It is
    a NAME only — no value of a secret ever enters an exception message, a log
    line, or a notification body.
    """

    platform: str = "unknown"

    def __init__(
        self,
        message: str,
        platform: Optional[str] = None,
        credential: Optional[str] = None,
        attempts: Optional[List[str]] = None,
    ) -> None:
        if platform:
            self.platform = platform
        self.credential = credential
        self.attempts = attempts or []
        super().__init__(message)


def platform_for_step(step_name: str) -> Optional[str]:
    """The platform a pipeline step belongs to, or None for shared steps."""
    for platform in PLATFORMS:
        if step_name.endswith(platform):
            return platform
    return None


@dataclass
class FailureCause:
    """The proximate cause of a step failure, in operator language."""

    kind: str  # auth | missing_file | video_not_ready | network | quota | unknown
    summary: str
    detail: str
    retryable: bool
    platform: Optional[str] = None
    credential: Optional[str] = None
    remediation: Optional[str] = None
    chain: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _walk_causes(exc: BaseException, limit: int = 8) -> List[BaseException]:
    """The exception and its ``__cause__``/``__context__`` chain.

    The browser fallback raises its own error with the auth failure chained as
    ``__cause__``; classifying only the outermost exception is exactly the bug
    that produced the misleading email.
    """
    seen: List[BaseException] = []
    current: Optional[BaseException] = exc
    while current is not None and len(seen) < limit:
        if any(current is s for s in seen):
            break
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def _looks_like_auth(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


_CREDENTIAL_HINTS = {
    "soundcloud": "SOUNDCLOUD_REFRESH_TOKEN / SOUNDCLOUD_ACCESS_TOKEN",
    "youtube": "YOUTUBE_REFRESH_TOKEN",
    "mixcloud": "MIXCLOUD_ACCESS_TOKEN",
}

_REAUTH_HINTS = {
    "soundcloud": (
        "Re-authorize SoundCloud (OAuth). No retry will clear this; the stored "
        "grant has to be replaced."
    ),
    "youtube": (
        "Re-authorize the YouTube OAuth client and store a fresh refresh token."
    ),
    "mixcloud": "Re-issue the Mixcloud access token.",
}


def classify_failure(exc: BaseException, step_name: str = "") -> FailureCause:
    """Name the proximate cause of ``exc`` for the step that raised it.

    Never raises: an unrecognized error degrades to ``kind="unknown"`` with the
    original text preserved, because a wrong-but-confident label is worse than
    an honest "unknown".
    """
    try:
        step_platform = platform_for_step(step_name)
        chain = _walk_causes(exc)
        chain_text = [f"{type(e).__name__}: {e}" for e in chain]

        # 1. An explicit, typed auth failure anywhere in the chain wins. This
        #    is the case the 08-12 incident got wrong.
        for link in chain:
            if isinstance(link, PlatformAuthError):
                platform = link.platform or step_platform or "unknown"
                credential = link.credential or _CREDENTIAL_HINTS.get(platform)
                return FailureCause(
                    kind="auth",
                    summary=(
                        f"{platform} authorization failed — the stored credential was "
                        f"rejected ({'; '.join(link.attempts) if link.attempts else str(link)})"
                    ),
                    detail=str(link),
                    retryable=False,
                    platform=platform,
                    credential=credential,
                    remediation=_REAUTH_HINTS.get(platform),
                    chain=chain_text,
                )

        # 2. An untyped auth rejection (raw invalid_grant / 401 text).
        for link in chain:
            if _looks_like_auth(f"{type(link).__name__}: {link}"):
                platform = step_platform or "unknown"
                return FailureCause(
                    kind="auth",
                    summary=(
                        f"{platform} authorization failed — credential rejected "
                        f"({type(link).__name__})"
                    ),
                    detail=str(link),
                    retryable=False,
                    platform=platform,
                    credential=_CREDENTIAL_HINTS.get(platform),
                    remediation=_REAUTH_HINTS.get(platform),
                    chain=chain_text,
                )

        head = chain[0] if chain else exc
        name = type(head).__name__

        if name == "_VideoNotReady" or name == "VideoNotReady":
            return FailureCause(
                kind="video_not_ready",
                summary=f"video source never became complete: {head}",
                detail=str(head),
                retryable=True,
                platform=step_platform,
                remediation="Check the OBS→NAS recording sync.",
                chain=chain_text,
            )

        if isinstance(head, FileNotFoundError):
            return FailureCause(
                kind="missing_file",
                summary=f"source file missing: {head}",
                detail=str(head),
                retryable=False,
                platform=step_platform,
                remediation="The source recording is not where the mix record says it is.",
                chain=chain_text,
            )

        lowered = str(head).lower()
        if "quota" in lowered or "ratelimit" in lowered or "rate limit" in lowered:
            return FailureCause(
                kind="quota",
                summary=f"{step_platform or 'platform'} quota/rate limit hit",
                detail=str(head),
                retryable=True,
                platform=step_platform,
                remediation="Wait for the quota window to reset.",
                chain=chain_text,
            )

        if any(
            marker in lowered
            for marker in ("timeout", "timed out", "connection", "temporarily unavailable", "502", "503")
        ):
            return FailureCause(
                kind="network",
                summary=f"transport failure during {step_name or 'step'}: {name}",
                detail=str(head),
                retryable=True,
                platform=step_platform,
                chain=chain_text,
            )

        return FailureCause(
            kind="unknown",
            summary=f"{step_name or 'step'} failed: {name}: {head}",
            detail=str(head),
            retryable=True,
            platform=step_platform,
            chain=chain_text,
        )
    except Exception:  # pragma: no cover - the classifier must never mask a failure
        logger.exception("classify_failure itself failed for step %s", step_name)
        return FailureCause(
            kind="unknown",
            summary=str(exc),
            detail=str(exc),
            retryable=True,
            platform=None,
        )
