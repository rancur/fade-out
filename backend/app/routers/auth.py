"""OAuth onboarding endpoints for SoundCloud and YouTube."""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AppSettings

logger = logging.getLogger("fadeout.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class OAuthStatus(BaseModel):
    connected: bool
    username: Optional[str] = None
    channel_name: Optional[str] = None


async def _get_or_create_settings(db: AsyncSession) -> AppSettings:
    """Get or create the singleton settings row."""
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    if not row:
        row = AppSettings(id=1, settings_json={})
        db.add(row)
        await db.flush()
    return row


def _build_redirect_uri(request: Request, provider: str) -> str:
    """Build the OAuth callback URI from the current request host."""
    return f"{request.url.scheme}://{request.headers.get('host', 'localhost')}/api/auth/{provider}/callback"


# ---------------------------------------------------------------------------
# SoundCloud OAuth
# ---------------------------------------------------------------------------


@router.get("/soundcloud")
async def soundcloud_auth(request: Request):
    """Redirect user to SoundCloud OAuth authorize page."""
    redirect_uri = _build_redirect_uri(request, "soundcloud")
    params = {
        "client_id": settings.SOUNDCLOUD_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "non-expiring",
    }
    qs = "&".join(f"{k}={httpx.URL('', params={k: v}).params[k]}" for k, v in params.items())
    authorize_url = f"https://api.soundcloud.com/connect?{qs}"
    return RedirectResponse(url=authorize_url)


@router.get("/soundcloud/callback")
async def soundcloud_callback(
    request: Request,
    code: str,
    db: AsyncSession = Depends(get_db),
):
    """Exchange SoundCloud auth code for tokens and store them."""
    redirect_uri = _build_redirect_uri(request, "soundcloud")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            "https://api.soundcloud.com/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.SOUNDCLOUD_CLIENT_ID,
                "client_secret": settings.SOUNDCLOUD_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )

    if token_resp.status_code != 200:
        logger.error("SoundCloud token exchange failed: %s %s", token_resp.status_code, token_resp.text)
        return RedirectResponse(url="/settings?auth=soundcloud&error=token_exchange_failed")

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    row = await _get_or_create_settings(db)
    sj = dict(row.settings_json or {})
    sj["soundcloud_access_token"] = access_token
    sj["soundcloud_refresh_token"] = refresh_token
    row.settings_json = sj
    await db.flush()

    logger.info("SoundCloud OAuth tokens stored successfully.")
    return RedirectResponse(url="/settings?auth=soundcloud&success=1")


@router.get("/soundcloud/status", response_model=OAuthStatus)
async def soundcloud_status(db: AsyncSession = Depends(get_db)):
    """Check if SoundCloud is connected by testing the stored token."""
    row = await _get_or_create_settings(db)
    sj = row.settings_json or {}
    access_token = sj.get("soundcloud_access_token") or settings.SOUNDCLOUD_ACCESS_TOKEN

    if not access_token:
        return OAuthStatus(connected=False)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.soundcloud.com/me",
                headers={"Authorization": f"OAuth {access_token}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return OAuthStatus(connected=True, username=data.get("username") or data.get("permalink"))
    except Exception:
        logger.warning("SoundCloud status check failed", exc_info=True)

    return OAuthStatus(connected=False)


# ---------------------------------------------------------------------------
# YouTube OAuth (Google)
# ---------------------------------------------------------------------------

_YOUTUBE_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube"


@router.get("/youtube")
async def youtube_auth(request: Request):
    """Redirect user to Google OAuth authorize page for YouTube."""
    redirect_uri = _build_redirect_uri(request, "youtube")
    params = {
        "client_id": settings.YOUTUBE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _YOUTUBE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    qs = "&".join(f"{k}={httpx.URL('', params={k: v}).params[k]}" for k, v in params.items())
    authorize_url = f"https://accounts.google.com/o/oauth2/v2/auth?{qs}"
    return RedirectResponse(url=authorize_url)


@router.get("/youtube/callback")
async def youtube_callback(
    request: Request,
    code: str,
    db: AsyncSession = Depends(get_db),
):
    """Exchange Google auth code for tokens and store the refresh token."""
    redirect_uri = _build_redirect_uri(request, "youtube")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.YOUTUBE_CLIENT_ID,
                "client_secret": settings.YOUTUBE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )

    if token_resp.status_code != 200:
        logger.error("YouTube token exchange failed: %s %s", token_resp.status_code, token_resp.text)
        return RedirectResponse(url="/settings?auth=youtube&error=token_exchange_failed")

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    row = await _get_or_create_settings(db)
    sj = dict(row.settings_json or {})
    sj["youtube_access_token"] = access_token
    if refresh_token:
        sj["youtube_refresh_token"] = refresh_token
    row.settings_json = sj
    await db.flush()

    logger.info("YouTube OAuth tokens stored successfully.")
    return RedirectResponse(url="/settings?auth=youtube&success=1")


@router.get("/youtube/status", response_model=OAuthStatus)
async def youtube_status(db: AsyncSession = Depends(get_db)):
    """Check if YouTube is connected by fetching channel info with stored token."""
    row = await _get_or_create_settings(db)
    sj = row.settings_json or {}

    # Try DB-stored token first, fall back to env config
    access_token = sj.get("youtube_access_token")
    refresh_token = sj.get("youtube_refresh_token") or settings.YOUTUBE_REFRESH_TOKEN

    # If we only have a refresh token, exchange it for an access token
    if not access_token and refresh_token:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": settings.YOUTUBE_CLIENT_ID,
                        "client_secret": settings.YOUTUBE_CLIENT_SECRET,
                        "refresh_token": refresh_token,
                    },
                )
            if resp.status_code == 200:
                access_token = resp.json().get("access_token")
        except Exception:
            logger.warning("YouTube token refresh failed", exc_info=True)

    if not access_token:
        return OAuthStatus(connected=False)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            if items:
                channel_name = items[0].get("snippet", {}).get("title")
                return OAuthStatus(connected=True, channel_name=channel_name)
    except Exception:
        logger.warning("YouTube status check failed", exc_info=True)

    return OAuthStatus(connected=False)
