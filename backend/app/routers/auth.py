"""Connection status and token management for SoundCloud and YouTube."""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AppSettings

logger = logging.getLogger("fadeout.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class ConnectionStatus(BaseModel):
    connected: bool
    username: Optional[str] = None
    channel_name: Optional[str] = None
    error: Optional[str] = None


class TokenUpdate(BaseModel):
    token: str


async def _get_settings(db: AsyncSession) -> AppSettings:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    if not row:
        row = AppSettings(id=1, settings_json={})
        db.add(row)
        await db.flush()
    return row


# ---------------------------------------------------------------------------
# SoundCloud — uses client_id + client_secret + OAuth access token
# ---------------------------------------------------------------------------

@router.get("/soundcloud/status", response_model=ConnectionStatus)
async def soundcloud_status(db: AsyncSession = Depends(get_db)):
    """Check SoundCloud connection by testing stored credentials."""
    row = await _get_settings(db)
    sj = row.settings_json or {}
    access_token = sj.get("soundcloud_access_token") or settings.SOUNDCLOUD_ACCESS_TOKEN

    if not access_token:
        # Try getting a token via client credentials if we have client_id/secret
        if settings.SOUNDCLOUD_CLIENT_ID and settings.SOUNDCLOUD_CLIENT_SECRET:
            return ConnectionStatus(
                connected=False,
                error="Client credentials configured but no access token. Use the SoundCloud OAuth Playground or POST /api/auth/soundcloud/token to set one.",
            )
        return ConnectionStatus(connected=False, error="No SoundCloud credentials configured")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.soundcloud.com/me",
                headers={"Authorization": f"OAuth {access_token}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return ConnectionStatus(
                connected=True,
                username=data.get("username") or data.get("permalink"),
            )
        return ConnectionStatus(connected=False, error=f"Token invalid (HTTP {resp.status_code})")
    except Exception as exc:
        return ConnectionStatus(connected=False, error=str(exc))


@router.post("/soundcloud/token", response_model=ConnectionStatus)
async def set_soundcloud_token(body: TokenUpdate, db: AsyncSession = Depends(get_db)):
    """Store a SoundCloud OAuth access token (obtained manually or via OAuth Playground)."""
    row = await _get_settings(db)
    sj = dict(row.settings_json or {})
    sj["soundcloud_access_token"] = body.token
    row.settings_json = sj
    await db.flush()

    # Verify it works
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.soundcloud.com/me",
                headers={"Authorization": f"OAuth {body.token}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return ConnectionStatus(
                connected=True,
                username=data.get("username") or data.get("permalink"),
            )
        return ConnectionStatus(connected=False, error=f"Token rejected (HTTP {resp.status_code})")
    except Exception as exc:
        return ConnectionStatus(connected=False, error=str(exc))


@router.post("/soundcloud/authenticate", response_model=ConnectionStatus)
async def soundcloud_password_auth(db: AsyncSession = Depends(get_db)):
    """Attempt to get a SoundCloud token via password grant using configured credentials."""
    if not all([settings.SOUNDCLOUD_CLIENT_ID, settings.SOUNDCLOUD_CLIENT_SECRET,
                settings.SOUNDCLOUD_EMAIL, settings.SOUNDCLOUD_PASSWORD]):
        return ConnectionStatus(
            connected=False,
            error="Need SOUNDCLOUD_CLIENT_ID, CLIENT_SECRET, EMAIL, and PASSWORD in .env",
        )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.soundcloud.com/oauth2/token",
                data={
                    "grant_type": "password",
                    "client_id": settings.SOUNDCLOUD_CLIENT_ID,
                    "client_secret": settings.SOUNDCLOUD_CLIENT_SECRET,
                    "username": settings.SOUNDCLOUD_EMAIL,
                    "password": settings.SOUNDCLOUD_PASSWORD,
                },
            )

        if resp.status_code == 200:
            token_data = resp.json()
            access_token = token_data.get("access_token", "")

            row = await _get_settings(db)
            sj = dict(row.settings_json or {})
            sj["soundcloud_access_token"] = access_token
            if token_data.get("refresh_token"):
                sj["soundcloud_refresh_token"] = token_data["refresh_token"]
            row.settings_json = sj
            await db.flush()

            # Get username
            me_resp = await httpx.AsyncClient(timeout=10).get(
                "https://api.soundcloud.com/me",
                headers={"Authorization": f"OAuth {access_token}"},
            )
            username = None
            if me_resp.status_code == 200:
                username = me_resp.json().get("username")

            return ConnectionStatus(connected=True, username=username)

        return ConnectionStatus(connected=False, error=f"Auth failed: {resp.text[:200]}")
    except Exception as exc:
        return ConnectionStatus(connected=False, error=str(exc))


# ---------------------------------------------------------------------------
# YouTube — uses API key or OAuth refresh token
# ---------------------------------------------------------------------------

@router.get("/youtube/status", response_model=ConnectionStatus)
async def youtube_status(db: AsyncSession = Depends(get_db)):
    """Check YouTube connection by testing stored credentials."""
    row = await _get_settings(db)
    sj = row.settings_json or {}

    refresh_token = sj.get("youtube_refresh_token") or settings.YOUTUBE_REFRESH_TOKEN

    if not refresh_token:
        return ConnectionStatus(
            connected=False,
            error="No YouTube refresh token. Get one from Google OAuth Playground (https://developers.google.com/oauthplayground) and POST it to /api/auth/youtube/token",
        )

    if not settings.YOUTUBE_CLIENT_ID or not settings.YOUTUBE_CLIENT_SECRET:
        return ConnectionStatus(
            connected=False,
            error="YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set in .env",
        )

    # Exchange refresh token for access token
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

        if resp.status_code != 200:
            return ConnectionStatus(connected=False, error=f"Token refresh failed: {resp.text[:200]}")

        access_token = resp.json().get("access_token")

        # Test with YouTube API
        async with httpx.AsyncClient(timeout=10) as client:
            yt_resp = await client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if yt_resp.status_code == 200:
            items = yt_resp.json().get("items", [])
            if items:
                channel_name = items[0].get("snippet", {}).get("title")
                return ConnectionStatus(connected=True, channel_name=channel_name)
            return ConnectionStatus(connected=True, channel_name="(no channel found)")

        return ConnectionStatus(connected=False, error=f"YouTube API error: {yt_resp.status_code}")
    except Exception as exc:
        return ConnectionStatus(connected=False, error=str(exc))


@router.post("/youtube/token", response_model=ConnectionStatus)
async def set_youtube_token(body: TokenUpdate, db: AsyncSession = Depends(get_db)):
    """Store a YouTube OAuth refresh token.

    Get one from Google OAuth Playground:
    1. Go to https://developers.google.com/oauthplayground
    2. Click the gear icon, check 'Use your own OAuth credentials'
    3. Enter your YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET
    4. In Step 1, authorize: https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube
    5. In Step 2, click 'Exchange authorization code for tokens'
    6. Copy the refresh_token and paste it here
    """
    row = await _get_settings(db)
    sj = dict(row.settings_json or {})
    sj["youtube_refresh_token"] = body.token
    row.settings_json = sj
    await db.flush()

    # Verify by checking status
    return await youtube_status(db=db)
