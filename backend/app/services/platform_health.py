"""Credential health for every configured publish target.

``/api/health`` used to answer ``{"status":"ok"}`` unconditionally — it proved
the process was listening and nothing else. On 2026-08-12 it kept saying "ok"
for two days while the SoundCloud OAuth grant was dead and mixes were failing
to publish. A 200 that does not assert the underlying condition is a false
green.

So health now probes the SAME auth path an upload takes:

* SoundCloud — ``_ensure_access_token()``: the stored access token is tested
  against ``/me`` and, if stale, the refresh grant is exercised. That is
  exactly what fails with ``invalid_grant`` when the grant is dead.
* YouTube — the OAuth refresh is actually performed.
* Mixcloud — only when enabled; the token is exercised against ``/me``.

Results are cached and refreshed by a background loop, so the endpoint itself
never blocks on the network. A result older than ``STALE_AFTER_SECONDS`` is
reported as ``unknown``, and unknown is never treated as healthy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.database import async_session_factory
from app.models import AppSettings

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 600  # 10 minutes
STALE_AFTER_SECONDS = 1800  # 3 missed refreshes -> we no longer know

PLATFORMS = ("soundcloud", "youtube", "mixcloud")

# States, in plain language:
#   ok             — credentials were exercised and accepted
#   dead           — credentials were exercised and REJECTED (operator action)
#   not_configured — nothing configured for this platform; nothing to assert
#   unknown        — never probed, probe errored, or the result went stale
OK = "ok"
DEAD = "dead"
NOT_CONFIGURED = "not_configured"
UNKNOWN = "unknown"

HEALTHY_STATES = (OK, NOT_CONFIGURED)


@dataclass
class CredentialState:
    platform: str
    state: str = UNKNOWN
    detail: str = "not probed yet"
    credential: Optional[str] = None
    checked_at: Optional[str] = None
    checked_monotonic: float = field(default=0.0, repr=False)

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = now if now is not None else time.monotonic()
        state = self.state
        detail = self.detail
        # A stale result is not a passing result.
        if (
            state in (OK, DEAD)
            and self.checked_monotonic
            and now - self.checked_monotonic > STALE_AFTER_SECONDS
        ):
            state = UNKNOWN
            detail = (
                f"last probe was {int(now - self.checked_monotonic)}s ago "
                "(stale — the health refresher is not running)"
            )
        out = asdict(self)
        out.pop("checked_monotonic", None)
        out["state"] = state
        out["detail"] = detail
        out["healthy"] = state in HEALTHY_STATES
        return out


class PlatformHealth:
    """Cached credential probes, refreshed in the background."""

    def __init__(self) -> None:
        self._states: Dict[str, CredentialState] = {
            p: CredentialState(platform=p) for p in PLATFORMS
        }
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Platform credential health started (every %ds)", REFRESH_INTERVAL_SECONDS
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.refresh_all()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Platform credential refresh failed")
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

    # -- probing --------------------------------------------------------

    async def refresh_all(self) -> Dict[str, Dict[str, Any]]:
        async with self._lock:
            sj = await self._settings_json()
            for platform in PLATFORMS:
                probe = getattr(self, f"_probe_{platform}")
                try:
                    state = await probe(sj)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("%s credential probe errored: %s", platform, exc)
                    state = CredentialState(
                        platform=platform,
                        state=UNKNOWN,
                        detail=f"probe error: {exc}",
                    )
                # A deferred probe returns the EXISTING state object. Do not
                # re-stamp it: that would keep a skipped check looking fresh
                # forever, which is the false green in miniature.
                if state is not self._states.get(platform):
                    previous = self._states.get(platform)
                    state.checked_at = datetime.now(timezone.utc).isoformat()
                    state.checked_monotonic = time.monotonic()
                    self._states[platform] = state
                    await self._announce_if_newly_dead(previous, state)
        return self.snapshot()

    @staticmethod
    async def _announce_if_newly_dead(
        previous: Optional[CredentialState], current: CredentialState
    ) -> None:
        """Page once when a credential goes dead, on the transition only.

        The probe loop re-checks every few minutes, so notifying on state
        rather than on transition would emit the same page indefinitely --
        one dead SoundCloud grant produced 855 silent retries over six days
        and not a single notification, because nothing here ever spoke to
        the notification service at all.

        Deliberately one-directional: recovery is not announced. An all-clear
        that nobody asked for is noise, and the health endpoint already shows
        the current state for anyone who wants to look.
        """
        if current.state != DEAD:
            return
        if previous is not None and previous.state == DEAD:
            return  # already reported; don't re-page every probe

        try:
            from app.services.notification_service import get_notification_service

            detail = current.detail or "credential rejected"
            credential = current.credential or "(credential not identified)"
            await get_notification_service().notify(
                "credential_dead",
                title=f"{current.platform} credentials need re-authorisation",
                message=(
                    f"{current.platform} authorisation is no longer accepted, so "
                    f"nothing will publish there until it is renewed.\n\n"
                    f"Cause: {detail}\n"
                    f"Credential: {credential}\n\n"
                    f"This is not retryable -- the stored grant has to be replaced."
                ),
                data={
                    "platform": current.platform,
                    "state": current.state,
                    "previous_state": previous.state if previous else None,
                    "detail": detail,
                    "credential": credential,
                },
            )
        except Exception:  # pragma: no cover - never let alerting break probing
            logger.exception(
                "Failed to send credential_dead notification for %s", current.platform
            )

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        now = time.monotonic()
        return {p: s.to_dict(now) for p, s in self._states.items()}

    def overall_ok(self) -> bool:
        """True only when every platform is ok or genuinely not configured."""
        return all(s["healthy"] for s in self.snapshot().values())

    def unhealthy(self) -> Dict[str, Dict[str, Any]]:
        return {p: s for p, s in self.snapshot().items() if not s["healthy"]}

    # -- per-platform ---------------------------------------------------

    @staticmethod
    async def _settings_json() -> Dict[str, Any]:
        try:
            async with async_session_factory() as session:
                row = await session.get(AppSettings, 1)
                return dict(row.settings_json or {}) if row else {}
        except Exception:  # pragma: no cover - DB down is reported elsewhere
            return {}

    @staticmethod
    async def _upload_in_flight(platform: str) -> bool:
        """Is an upload for this platform running right now?

        Probing mid-upload is not worth the risk of a concurrent token
        rotation, so the probe stands down and the previous result stands (and
        ages toward ``unknown`` on its own if this persists).
        """
        from sqlalchemy import select

        from app.models import PipelineStep

        try:
            async with async_session_factory() as session:
                row = (
                    await session.execute(
                        select(PipelineStep.id).where(
                            PipelineStep.step_name == f"upload_{platform}",
                            PipelineStep.status == "running",
                        ).limit(1)
                    )
                ).first()
            return row is not None
        except Exception:  # pragma: no cover - defensive
            return False

    async def _probe_soundcloud(self, sj: Dict[str, Any]) -> CredentialState:
        from app.services.platform_errors import PlatformAuthError
        from app.services.soundcloud_uploader import SoundCloudUploader

        if await self._upload_in_flight("soundcloud"):
            return self._states.get("soundcloud") or CredentialState(
                platform="soundcloud", state=UNKNOWN,
                detail="upload in flight — probe deferred",
            )

        has_any = any((
            sj.get("soundcloud_access_token"), settings.SOUNDCLOUD_ACCESS_TOKEN,
            sj.get("soundcloud_refresh_token"), settings.SOUNDCLOUD_REFRESH_TOKEN,
            settings.SOUNDCLOUD_EMAIL and settings.SOUNDCLOUD_PASSWORD,
        ))
        if not has_any:
            return CredentialState(
                platform="soundcloud", state=NOT_CONFIGURED,
                detail="no SoundCloud credentials configured",
            )

        # The persister is NOT optional here. SoundCloud rotates the refresh
        # token on every refresh, so a probe that refreshes without persisting
        # the new pair would invalidate the credential it is checking and
        # strand the next real upload with invalid_grant. The probe uses the
        # exact same persistence path an upload does.
        from app.services.handlers import _sc_token_persister

        async with async_session_factory() as session:
            app_settings = await session.get(AppSettings, 1)
        uploader = SoundCloudUploader(
            db_settings_json=sj,
            on_tokens_refreshed=_sc_token_persister(app_settings),
            # The probe runs every 10 minutes; it must not write an identical
            # activity entry each sweep. /api/health is where this is reported.
            emit_activity=False,
        )
        try:
            await uploader._ensure_access_token()
        except PlatformAuthError as exc:
            return CredentialState(
                platform="soundcloud", state=DEAD,
                detail=str(exc), credential=exc.credential,
            )
        except Exception as exc:
            return CredentialState(
                platform="soundcloud", state=UNKNOWN,
                detail=f"probe could not reach SoundCloud: {exc}",
            )
        return CredentialState(
            platform="soundcloud", state=OK,
            detail="access token accepted by /me",
        )

    async def _probe_youtube(self, sj: Dict[str, Any]) -> CredentialState:
        from app.services.platform_errors import PlatformAuthError
        from app.services.youtube_uploader import YouTubeUploader

        if not (sj.get("youtube_refresh_token") or settings.YOUTUBE_REFRESH_TOKEN):
            return CredentialState(
                platform="youtube", state=NOT_CONFIGURED,
                detail="no YouTube refresh token configured",
            )

        uploader = YouTubeUploader(db_settings_json=sj)
        try:
            # Synchronous google-auth refresh — off the event loop.
            await asyncio.to_thread(uploader._get_credentials)
        except PlatformAuthError as exc:
            return CredentialState(
                platform="youtube", state=DEAD,
                detail=str(exc), credential=exc.credential,
            )
        except Exception as exc:
            return CredentialState(
                platform="youtube", state=UNKNOWN,
                detail=f"probe could not reach Google OAuth: {exc}",
            )
        return CredentialState(
            platform="youtube", state=OK, detail="OAuth refresh grant accepted",
        )

    async def _probe_mixcloud(self, sj: Dict[str, Any]) -> CredentialState:
        enabled = settings.MIXCLOUD_ENABLED or bool(sj.get("mixcloud_enabled"))
        token = sj.get("mixcloud_access_token") or settings.MIXCLOUD_ACCESS_TOKEN
        if not enabled or not token:
            return CredentialState(
                platform="mixcloud", state=NOT_CONFIGURED,
                detail="Mixcloud disabled or no access token configured",
            )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.mixcloud.com/me/",
                    params={"access_token": token},
                )
        except Exception as exc:
            return CredentialState(
                platform="mixcloud", state=UNKNOWN,
                detail=f"probe could not reach Mixcloud: {exc}",
            )
        if resp.status_code == 200:
            return CredentialState(
                platform="mixcloud", state=OK, detail="access token accepted by /me/",
            )
        if resp.status_code in (401, 403):
            return CredentialState(
                platform="mixcloud", state=DEAD,
                detail=f"/me/ returned {resp.status_code}",
                credential="MIXCLOUD_ACCESS_TOKEN",
            )
        return CredentialState(
            platform="mixcloud", state=UNKNOWN,
            detail=f"/me/ returned {resp.status_code}",
        )


_service: Optional[PlatformHealth] = None


def get_platform_health() -> PlatformHealth:
    global _service
    if _service is None:
        _service = PlatformHealth()
    return _service
