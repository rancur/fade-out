"""Connection status and token management for SoundCloud and YouTube."""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AppSettings

logger = logging.getLogger("fadeout.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

YOUTUBE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.readonly",
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SoundCloudStatus(BaseModel):
    connected: bool
    username: Optional[str] = None
    error: Optional[str] = None


class YouTubeStatus(BaseModel):
    connected: bool
    channel_name: Optional[str] = None
    can_upload: bool = False
    error: Optional[str] = None


class ServiceConfigured(BaseModel):
    configured: bool


class AllStatus(BaseModel):
    soundcloud: SoundCloudStatus
    youtube: YouTubeStatus
    openai: ServiceConfigured
    fal: ServiceConfigured


class TokenUpdate(BaseModel):
    token: str


class CodeExchange(BaseModel):
    code: str
    redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob"


class OAuthURL(BaseModel):
    url: str
    redirect_uri: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_settings(db: AsyncSession) -> AppSettings:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    if not row:
        row = AppSettings(id=1, settings_json={})
        db.add(row)
        await db.flush()
    return row


async def _get_soundcloud_status(db: AsyncSession) -> SoundCloudStatus:
    """Internal helper to check SoundCloud status."""
    row = await _get_settings(db)
    sj = row.settings_json or {}
    access_token = sj.get("soundcloud_access_token") or settings.SOUNDCLOUD_ACCESS_TOKEN

    if not access_token:
        if settings.SOUNDCLOUD_CLIENT_ID and settings.SOUNDCLOUD_CLIENT_SECRET:
            return SoundCloudStatus(
                connected=False,
                error="Client credentials configured but no access token. Use Auto Connect or paste a token.",
            )
        return SoundCloudStatus(connected=False, error="No SoundCloud credentials configured")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.soundcloud.com/me",
                headers={"Authorization": f"OAuth {access_token}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return SoundCloudStatus(
                connected=True,
                username=data.get("username") or data.get("permalink"),
            )
        return SoundCloudStatus(connected=False, error=f"Token invalid (HTTP {resp.status_code})")
    except Exception as exc:
        return SoundCloudStatus(connected=False, error=str(exc))


async def _get_youtube_status(db: AsyncSession) -> YouTubeStatus:
    """Internal helper to check YouTube status."""
    row = await _get_settings(db)
    sj = row.settings_json or {}

    refresh_token = sj.get("youtube_refresh_token") or settings.YOUTUBE_REFRESH_TOKEN
    has_refresh_token = bool(refresh_token)

    # If we have a refresh token + client creds, try OAuth-based check
    if has_refresh_token and settings.YOUTUBE_CLIENT_ID and settings.YOUTUBE_CLIENT_SECRET:
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
                return YouTubeStatus(
                    connected=False,
                    can_upload=False,
                    error=f"Token refresh failed: {resp.text[:200]}",
                )

            access_token = resp.json().get("access_token")

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
                    return YouTubeStatus(connected=True, channel_name=channel_name, can_upload=True)
                return YouTubeStatus(connected=True, channel_name="(no channel found)", can_upload=True)

            return YouTubeStatus(
                connected=False,
                can_upload=False,
                error=f"YouTube API error: {yt_resp.status_code}",
            )
        except Exception as exc:
            return YouTubeStatus(connected=False, can_upload=False, error=str(exc))

    # Fall back to API key (read-only)
    if settings.YOUTUBE_API_KEY:
        try:
            params = {"part": "snippet", "key": settings.YOUTUBE_API_KEY}
            if settings.YOUTUBE_CHANNEL_ID:
                params["id"] = settings.YOUTUBE_CHANNEL_ID
            else:
                # Without a channel ID we can't query by mine=true with an API key
                return YouTubeStatus(
                    connected=True,
                    channel_name="(API key set, no channel ID)",
                    can_upload=False,
                    error="API key provides read-only access. Set up OAuth to enable uploads.",
                )

            async with httpx.AsyncClient(timeout=10) as client:
                yt_resp = await client.get(
                    "https://www.googleapis.com/youtube/v3/channels",
                    params=params,
                )

            if yt_resp.status_code == 200:
                items = yt_resp.json().get("items", [])
                if items:
                    channel_name = items[0].get("snippet", {}).get("title")
                    return YouTubeStatus(
                        connected=True,
                        channel_name=channel_name,
                        can_upload=False,
                        error="Read-only via API key. Set up OAuth to enable uploads.",
                    )
            return YouTubeStatus(
                connected=True,
                channel_name=None,
                can_upload=False,
                error="API key set but channel lookup failed. Set up OAuth to enable uploads.",
            )
        except Exception as exc:
            return YouTubeStatus(connected=False, can_upload=False, error=str(exc))

    # Nothing configured
    return YouTubeStatus(
        connected=False,
        can_upload=False,
        error="No YouTube credentials configured. Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET to .env, then authorize via the setup wizard.",
    )


# ---------------------------------------------------------------------------
# General Status
# ---------------------------------------------------------------------------

@router.get("/status", response_model=AllStatus)
async def all_status(db: AsyncSession = Depends(get_db)):
    """Returns status of ALL connections in one call."""
    sc = await _get_soundcloud_status(db)
    yt = await _get_youtube_status(db)

    return AllStatus(
        soundcloud=sc,
        youtube=yt,
        openai=ServiceConfigured(configured=bool(settings.OPENAI_API_KEY)),
        fal=ServiceConfigured(configured=bool(settings.FAL_API_KEY)),
    )


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

@router.get("/soundcloud/status", response_model=SoundCloudStatus)
async def soundcloud_status(db: AsyncSession = Depends(get_db)):
    """Check SoundCloud connection by testing stored credentials."""
    return await _get_soundcloud_status(db)


@router.post("/soundcloud/token", response_model=SoundCloudStatus)
async def set_soundcloud_token(body: TokenUpdate, db: AsyncSession = Depends(get_db)):
    """Store a SoundCloud OAuth access token (obtained manually)."""
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
            return SoundCloudStatus(
                connected=True,
                username=data.get("username") or data.get("permalink"),
            )
        return SoundCloudStatus(connected=False, error=f"Token rejected (HTTP {resp.status_code})")
    except Exception as exc:
        return SoundCloudStatus(connected=False, error=str(exc))


@router.get("/soundcloud/oauth-url", response_model=OAuthURL)
async def soundcloud_oauth_url(
    redirect_uri: str = Query(default="https://soundcloud.com"),
):
    """Generate the SoundCloud OAuth authorization URL."""
    if not settings.SOUNDCLOUD_CLIENT_ID:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="SOUNDCLOUD_CLIENT_ID not set in .env")

    from urllib.parse import urlencode

    params = {
        "client_id": settings.SOUNDCLOUD_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    url = f"https://api.soundcloud.com/connect?{urlencode(params)}"
    return OAuthURL(url=url, redirect_uri=redirect_uri)


@router.post("/soundcloud/exchange-code", response_model=SoundCloudStatus)
async def soundcloud_exchange_code(body: CodeExchange, db: AsyncSession = Depends(get_db)):
    """Exchange a SoundCloud authorization code for tokens."""
    if not settings.SOUNDCLOUD_CLIENT_ID or not settings.SOUNDCLOUD_CLIENT_SECRET:
        return SoundCloudStatus(
            connected=False,
            error="SOUNDCLOUD_CLIENT_ID and SOUNDCLOUD_CLIENT_SECRET must be set in .env",
        )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.soundcloud.com/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": body.code,
                    "client_id": settings.SOUNDCLOUD_CLIENT_ID,
                    "client_secret": settings.SOUNDCLOUD_CLIENT_SECRET,
                    "redirect_uri": body.redirect_uri,
                },
            )

        if resp.status_code != 200:
            return SoundCloudStatus(
                connected=False,
                error=f"Code exchange failed: {resp.text[:200]}",
            )

        token_data = resp.json()
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")

        # Save tokens
        row = await _get_settings(db)
        sj = dict(row.settings_json or {})
        sj["soundcloud_access_token"] = access_token
        if refresh_token:
            sj["soundcloud_refresh_token"] = refresh_token
        row.settings_json = sj
        await db.flush()

        # Get username
        async with httpx.AsyncClient(timeout=10) as me_client:
            me_resp = await me_client.get(
                "https://api.soundcloud.com/me",
                headers={"Authorization": f"OAuth {access_token}"},
            )
        username = None
        if me_resp.status_code == 200:
            username = me_resp.json().get("username")

        return SoundCloudStatus(connected=True, username=username)
    except Exception as exc:
        return SoundCloudStatus(connected=False, error=str(exc))


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

@router.get("/youtube/status", response_model=YouTubeStatus)
async def youtube_status(db: AsyncSession = Depends(get_db)):
    """Check YouTube connection. can_upload is true only with a valid refresh token."""
    return await _get_youtube_status(db)


@router.post("/youtube/token", response_model=YouTubeStatus)
async def set_youtube_token(body: TokenUpdate, db: AsyncSession = Depends(get_db)):
    """Store a YouTube OAuth refresh token (e.g. from Google OAuth Playground)."""
    row = await _get_settings(db)
    sj = dict(row.settings_json or {})
    sj["youtube_refresh_token"] = body.token
    row.settings_json = sj
    await db.flush()

    return await _get_youtube_status(db)


@router.get("/youtube/oauth-url", response_model=OAuthURL)
async def youtube_oauth_url(
    redirect_uri: str = Query(default="urn:ietf:wg:oauth:2.0:oob"),
):
    """Generate the Google OAuth authorization URL for the user to visit."""
    if not settings.YOUTUBE_CLIENT_ID:
        raise httpx.HTTPStatusError(
            "YOUTUBE_CLIENT_ID not set in .env",
            request=None,  # type: ignore[arg-type]
            response=None,  # type: ignore[arg-type]
        )

    from urllib.parse import urlencode

    params = {
        "client_id": settings.YOUTUBE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return OAuthURL(url=url, redirect_uri=redirect_uri)


@router.post("/youtube/exchange-code", response_model=YouTubeStatus)
async def youtube_exchange_code(body: CodeExchange, db: AsyncSession = Depends(get_db)):
    """Exchange an authorization code for access + refresh tokens, then save them."""
    if not settings.YOUTUBE_CLIENT_ID or not settings.YOUTUBE_CLIENT_SECRET:
        return YouTubeStatus(
            connected=False,
            can_upload=False,
            error="YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set in .env",
        )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "authorization_code",
                    "code": body.code,
                    "client_id": settings.YOUTUBE_CLIENT_ID,
                    "client_secret": settings.YOUTUBE_CLIENT_SECRET,
                    "redirect_uri": body.redirect_uri,
                },
            )

        if resp.status_code != 200:
            error_detail = resp.json().get("error_description", resp.text[:200])
            return YouTubeStatus(
                connected=False,
                can_upload=False,
                error=f"Code exchange failed: {error_detail}",
            )

        token_data = resp.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")

        if not refresh_token:
            return YouTubeStatus(
                connected=False,
                can_upload=False,
                error="No refresh_token returned. Make sure you used prompt=consent and access_type=offline. Try revoking app access at myaccount.google.com/permissions and re-authorizing.",
            )

        # Save the refresh token
        row = await _get_settings(db)
        sj = dict(row.settings_json or {})
        sj["youtube_refresh_token"] = refresh_token
        row.settings_json = sj
        await db.flush()

        # Test the access token to get channel info
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
                return YouTubeStatus(connected=True, channel_name=channel_name, can_upload=True)
            return YouTubeStatus(connected=True, channel_name="(no channel found)", can_upload=True)

        # Token exchange succeeded but channel query failed — still save the token
        return YouTubeStatus(
            connected=True,
            can_upload=True,
            error=f"Token saved but channel info unavailable (HTTP {yt_resp.status_code})",
        )

    except Exception as exc:
        return YouTubeStatus(connected=False, can_upload=False, error=str(exc))
