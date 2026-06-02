"""Connection status and token management for SoundCloud and YouTube."""

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
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

# Credentials that can be managed from the web UI
MANAGED_CREDENTIALS = [
    "soundcloud_client_id",
    "soundcloud_client_secret",
    "youtube_client_id",
    "youtube_client_secret",
    "youtube_api_key",
    "openai_api_key",
    "fal_api_key",
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


class CredentialInfo(BaseModel):
    name: str
    is_set: bool
    source: Optional[str] = None  # "db", "env", or None
    masked_value: Optional[str] = None


class CredentialsResponse(BaseModel):
    credentials: List[CredentialInfo]


class CredentialsUpdate(BaseModel):
    credentials: Dict[str, str]


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


def _get_credential(name: str, settings_json: Optional[Dict[str, Any]]) -> str:
    """Get a credential value, checking DB settings_json first, then env vars.

    Args:
        name: lowercase credential name (e.g. "soundcloud_client_id")
        settings_json: the settings_json dict from AppSettings
    Returns:
        The credential value, or empty string if not found.
    """
    # Check DB first
    if settings_json:
        val = settings_json.get(name)
        if val:
            return str(val)
    # Fall back to env var via pydantic settings
    return getattr(settings, name.upper(), "") or ""


def _mask_value(value: str) -> str:
    """Mask a credential value for display, showing first few and last few chars."""
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "..." + value[-2:]
    return value[:6] + "..." + value[-4:]


async def _get_soundcloud_status(db: AsyncSession) -> SoundCloudStatus:
    """Internal helper to check SoundCloud status."""
    row = await _get_settings(db)
    sj = row.settings_json or {}
    access_token = sj.get("soundcloud_access_token") or settings.SOUNDCLOUD_ACCESS_TOKEN
    client_id = _get_credential("soundcloud_client_id", sj)
    client_secret = _get_credential("soundcloud_client_secret", sj)

    if not access_token:
        if client_id and client_secret:
            return SoundCloudStatus(
                connected=False,
                error="Client credentials configured but no access token. Use Auto Connect or paste a token.",
            )
        return SoundCloudStatus(
            connected=False, error="No SoundCloud credentials configured"
        )

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
        return SoundCloudStatus(
            connected=False, error=f"Token invalid (HTTP {resp.status_code})"
        )
    except Exception as exc:
        return SoundCloudStatus(connected=False, error=str(exc))


async def _get_youtube_status(db: AsyncSession) -> YouTubeStatus:
    """Internal helper to check YouTube status."""
    row = await _get_settings(db)
    sj = row.settings_json or {}

    refresh_token = sj.get("youtube_refresh_token") or settings.YOUTUBE_REFRESH_TOKEN
    has_refresh_token = bool(refresh_token)
    yt_client_id = _get_credential("youtube_client_id", sj)
    yt_client_secret = _get_credential("youtube_client_secret", sj)
    yt_api_key = _get_credential("youtube_api_key", sj)

    # If we have a refresh token + client creds, try OAuth-based check
    if has_refresh_token and yt_client_id and yt_client_secret:
        try:
            async with httpx.AsyncClient(timeout=10) as http_client:
                resp = await http_client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": yt_client_id,
                        "client_secret": yt_client_secret,
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

            async with httpx.AsyncClient(timeout=10) as http_client:
                yt_resp = await http_client.get(
                    "https://www.googleapis.com/youtube/v3/channels",
                    params={"part": "snippet", "mine": "true"},
                    headers={"Authorization": f"Bearer {access_token}"},
                )

            if yt_resp.status_code == 200:
                items = yt_resp.json().get("items", [])
                if items:
                    channel_name = items[0].get("snippet", {}).get("title")
                    return YouTubeStatus(
                        connected=True, channel_name=channel_name, can_upload=True
                    )
                return YouTubeStatus(
                    connected=True, channel_name="(no channel found)", can_upload=True
                )

            return YouTubeStatus(
                connected=False,
                can_upload=False,
                error=f"YouTube API error: {yt_resp.status_code}",
            )
        except Exception as exc:
            return YouTubeStatus(connected=False, can_upload=False, error=str(exc))

    # Fall back to API key (read-only)
    if yt_api_key:
        try:
            params: Dict[str, str] = {"part": "snippet", "key": yt_api_key}
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

            async with httpx.AsyncClient(timeout=10) as http_client:
                yt_resp = await http_client.get(
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
        error="No YouTube credentials configured. Add client ID and secret via Settings, then authorize.",
    )


# ---------------------------------------------------------------------------
# General Status
# ---------------------------------------------------------------------------


@router.get("/status", response_model=AllStatus)
async def all_status(db: AsyncSession = Depends(get_db)):
    """Returns status of ALL connections in one call."""
    row = await _get_settings(db)
    sj = row.settings_json or {}

    sc = await _get_soundcloud_status(db)
    yt = await _get_youtube_status(db)

    openai_key = _get_credential("openai_api_key", sj)
    fal_key = _get_credential("fal_api_key", sj)

    return AllStatus(
        soundcloud=sc,
        youtube=yt,
        openai=ServiceConfigured(configured=bool(openai_key)),
        fal=ServiceConfigured(configured=bool(fal_key)),
    )


# ---------------------------------------------------------------------------
# Credentials Management (Web UI)
# ---------------------------------------------------------------------------


@router.get("/credentials", response_model=CredentialsResponse)
async def get_credentials(db: AsyncSession = Depends(get_db)):
    """Return which credentials are configured (with masked values)."""
    row = await _get_settings(db)
    sj = row.settings_json or {}

    result: List[CredentialInfo] = []
    for name in MANAGED_CREDENTIALS:
        db_val = sj.get(name, "")
        env_val = getattr(settings, name.upper(), "") or ""

        if db_val:
            result.append(
                CredentialInfo(
                    name=name,
                    is_set=True,
                    source="db",
                    masked_value=_mask_value(str(db_val)),
                )
            )
        elif env_val:
            result.append(
                CredentialInfo(
                    name=name,
                    is_set=True,
                    source="env",
                    masked_value=_mask_value(env_val),
                )
            )
        else:
            result.append(
                CredentialInfo(
                    name=name,
                    is_set=False,
                    source=None,
                    masked_value=None,
                )
            )

    return CredentialsResponse(credentials=result)


@router.put("/credentials", response_model=CredentialsResponse)
async def update_credentials(
    body: CredentialsUpdate, db: AsyncSession = Depends(get_db)
):
    """Save credential values to AppSettings.settings_json."""
    row = await _get_settings(db)
    sj = dict(row.settings_json or {})

    for name, value in body.credentials.items():
        if name not in MANAGED_CREDENTIALS:
            raise HTTPException(status_code=400, detail=f"Unknown credential: {name}")
        if value:
            sj[name] = value
        # If empty string, remove from DB (fall back to env)
        elif name in sj:
            del sj[name]

    row.settings_json = sj
    await db.flush()
    await db.commit()

    # Return updated state
    return await get_credentials(db)


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
        async with httpx.AsyncClient(timeout=10) as http_client:
            resp = await http_client.get(
                "https://api.soundcloud.com/me",
                headers={"Authorization": f"OAuth {body.token}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return SoundCloudStatus(
                connected=True,
                username=data.get("username") or data.get("permalink"),
            )
        return SoundCloudStatus(
            connected=False, error=f"Token rejected (HTTP {resp.status_code})"
        )
    except Exception as exc:
        return SoundCloudStatus(connected=False, error=str(exc))


@router.get("/soundcloud/oauth-url", response_model=OAuthURL)
async def soundcloud_oauth_url(
    request: Request,
    redirect_uri: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Generate the SoundCloud OAuth authorization URL."""
    row = await _get_settings(db)
    sj = row.settings_json or {}
    client_id = _get_credential("soundcloud_client_id", sj)

    if not client_id:
        raise HTTPException(
            status_code=400,
            detail="SoundCloud Client ID not configured. Add it in Settings.",
        )

    # Build callback URI dynamically from request host
    if not redirect_uri:
        redirect_uri = f"http://{request.headers.get('host', 'localhost:8500')}/api/auth/soundcloud/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    url = f"https://api.soundcloud.com/connect?{urlencode(params)}"
    return OAuthURL(url=url, redirect_uri=redirect_uri)


@router.get("/soundcloud/callback")
async def soundcloud_callback(
    request: Request,
    code: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """OAuth callback for SoundCloud. Exchanges code for tokens and redirects to settings page."""
    if error:
        return RedirectResponse(url=f"/settings?auth=soundcloud&error={error}")

    if not code:
        return RedirectResponse(url="/settings?auth=soundcloud&error=no_code_received")

    row = await _get_settings(db)
    sj = row.settings_json or {}
    client_id = _get_credential("soundcloud_client_id", sj)
    client_secret = _get_credential("soundcloud_client_secret", sj)

    if not client_id or not client_secret:
        return RedirectResponse(
            url="/settings?auth=soundcloud&error=missing_client_credentials"
        )

    # The redirect_uri used here must match what was used to generate the auth URL
    callback_uri = f"http://{request.headers.get('host', 'localhost:8500')}/api/auth/soundcloud/callback"

    try:
        async with httpx.AsyncClient(timeout=15) as http_client:
            resp = await http_client.post(
                "https://api.soundcloud.com/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": callback_uri,
                },
            )

        if resp.status_code != 200:
            logger.error("SoundCloud code exchange failed: %s", resp.text[:300])
            return RedirectResponse(
                url="/settings?auth=soundcloud&error=code_exchange_failed"
            )

        token_data = resp.json()
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")

        # Save tokens
        sj = dict(row.settings_json or {})
        sj["soundcloud_access_token"] = access_token
        if refresh_token:
            sj["soundcloud_refresh_token"] = refresh_token
        row.settings_json = sj
        await db.flush()
        await db.commit()

        return RedirectResponse(url="/settings?auth=soundcloud&success=1")

    except Exception as exc:
        logger.exception("SoundCloud callback error")
        return RedirectResponse(url=f"/settings?auth=soundcloud&error={str(exc)[:100]}")


@router.post("/soundcloud/exchange-code", response_model=SoundCloudStatus)
async def soundcloud_exchange_code(
    body: CodeExchange, db: AsyncSession = Depends(get_db)
):
    """Exchange a SoundCloud authorization code for tokens."""
    row = await _get_settings(db)
    sj = row.settings_json or {}
    client_id = _get_credential("soundcloud_client_id", sj)
    client_secret = _get_credential("soundcloud_client_secret", sj)

    if not client_id or not client_secret:
        return SoundCloudStatus(
            connected=False,
            error="SoundCloud Client ID and Secret must be configured in Settings.",
        )

    try:
        async with httpx.AsyncClient(timeout=15) as http_client:
            resp = await http_client.post(
                "https://api.soundcloud.com/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": body.code,
                    "client_id": client_id,
                    "client_secret": client_secret,
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
    request: Request,
    redirect_uri: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Generate the Google OAuth authorization URL for the user to visit."""
    row = await _get_settings(db)
    sj = row.settings_json or {}
    yt_client_id = _get_credential("youtube_client_id", sj)

    if not yt_client_id:
        raise HTTPException(
            status_code=400,
            detail="YouTube Client ID not configured. Add it in Settings.",
        )

    # Build redirect URI from the actual request host.
    # For Web Application OAuth clients, the redirect URI must be registered in Google Console.
    # For Desktop Application clients, Google only allows http://localhost or http://127.0.0.1.
    if not redirect_uri:
        host = request.headers.get("host", "localhost:8500")
        scheme = request.headers.get("x-forwarded-proto", "http")
        redirect_uri = f"{scheme}://{host}/api/auth/youtube/callback"

    params = {
        "client_id": yt_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return OAuthURL(url=url, redirect_uri=redirect_uri)


@router.get("/youtube/callback")
async def youtube_callback(
    request: Request,
    code: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """OAuth callback for YouTube/Google. Exchanges code for tokens and redirects to settings page."""
    if error:
        return RedirectResponse(url=f"/settings?auth=youtube&error={error}")

    if not code:
        return RedirectResponse(url="/settings?auth=youtube&error=no_code_received")

    row = await _get_settings(db)
    sj = row.settings_json or {}
    yt_client_id = _get_credential("youtube_client_id", sj)
    yt_client_secret = _get_credential("youtube_client_secret", sj)

    if not yt_client_id or not yt_client_secret:
        return RedirectResponse(
            url="/settings?auth=youtube&error=missing_client_credentials"
        )

    # Must match the redirect_uri used in the auth URL
    host = request.headers.get("host", "localhost:8500")
    scheme = request.headers.get("x-forwarded-proto", "http")
    callback_uri = f"{scheme}://{host}/api/auth/youtube/callback"

    try:
        async with httpx.AsyncClient(timeout=15) as http_client:
            resp = await http_client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": yt_client_id,
                    "client_secret": yt_client_secret,
                    "redirect_uri": callback_uri,
                },
            )

        if resp.status_code != 200:
            logger.error("YouTube code exchange failed: %s", resp.text[:300])
            return RedirectResponse(
                url="/settings?auth=youtube&error=code_exchange_failed"
            )

        token_data = resp.json()
        refresh_token = token_data.get("refresh_token")

        if not refresh_token:
            return RedirectResponse(
                url="/settings?auth=youtube&error=no_refresh_token_returned"
            )

        # Save the refresh token
        sj = dict(row.settings_json or {})
        sj["youtube_refresh_token"] = refresh_token
        row.settings_json = sj
        await db.flush()
        await db.commit()

        return RedirectResponse(url="/settings?auth=youtube&success=1")

    except Exception as exc:
        logger.exception("YouTube callback error")
        return RedirectResponse(url=f"/settings?auth=youtube&error={str(exc)[:100]}")


@router.post("/youtube/exchange-code", response_model=YouTubeStatus)
async def youtube_exchange_code(body: CodeExchange, db: AsyncSession = Depends(get_db)):
    """Exchange an authorization code for access + refresh tokens, then save them."""
    row = await _get_settings(db)
    sj = row.settings_json or {}
    yt_client_id = _get_credential("youtube_client_id", sj)
    yt_client_secret = _get_credential("youtube_client_secret", sj)

    if not yt_client_id or not yt_client_secret:
        return YouTubeStatus(
            connected=False,
            can_upload=False,
            error="YouTube Client ID and Secret must be configured in Settings.",
        )

    try:
        async with httpx.AsyncClient(timeout=15) as http_client:
            resp = await http_client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "authorization_code",
                    "code": body.code,
                    "client_id": yt_client_id,
                    "client_secret": yt_client_secret,
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
        sj = dict(row.settings_json or {})
        sj["youtube_refresh_token"] = refresh_token
        row.settings_json = sj
        await db.flush()

        # Test the access token to get channel info
        async with httpx.AsyncClient(timeout=10) as http_client:
            yt_resp = await http_client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if yt_resp.status_code == 200:
            items = yt_resp.json().get("items", [])
            if items:
                channel_name = items[0].get("snippet", {}).get("title")
                return YouTubeStatus(
                    connected=True, channel_name=channel_name, can_upload=True
                )
            return YouTubeStatus(
                connected=True, channel_name="(no channel found)", can_upload=True
            )

        # Token exchange succeeded but channel query failed — still save the token
        return YouTubeStatus(
            connected=True,
            can_upload=True,
            error=f"Token saved but channel info unavailable (HTTP {yt_resp.status_code})",
        )

    except Exception as exc:
        return YouTubeStatus(connected=False, can_upload=False, error=str(exc))
