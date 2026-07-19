"""Notification listing, testing, and settings endpoints.

Settings live in ``AppSettings.settings_json`` under ``notification_*`` keys —
the single config source the NotificationService reads (env vars are first-boot
fallback only). The SMTP password is write-only: GET returns ``has_password``.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AppSettings, Notification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

VALID_CHANNELS = {"discord", "email", "webhook"}
VALID_LEVELS = {"info", "warn", "error"}


# --- Schemas ---

class NotificationOut(BaseModel):
    id: int
    mix_id: Optional[str] = None
    type: Optional[str] = None
    channel: Optional[str] = None
    message: Optional[str] = None
    sent: bool = False
    sent_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class NotificationListResponse(BaseModel):
    items: List[NotificationOut]
    total: int
    page: int
    page_size: int


class TestNotificationRequest(BaseModel):
    channel: str  # discord, email, webhook
    message: Optional[str] = "This is a test notification from Fade-Out."


class TestNotificationResponse(BaseModel):
    success: bool
    channel: str
    detail: str


class NotificationSettingsUpdate(BaseModel):
    """PUT body. Every field optional — only provided fields are written."""

    discord_webhook_url: Optional[str] = None
    email_smtp_host: Optional[str] = None
    email_smtp_port: Optional[int] = None
    email_smtp_user: Optional[str] = None
    email_smtp_password: Optional[str] = None  # write-only, never returned
    email_from: Optional[str] = None
    email_to: Optional[str] = None
    email_smtp_secure: Optional[bool] = None
    webhook_urls: Optional[str] = None  # comma-separated
    events: Optional[Dict[str, bool]] = None  # event-type -> enabled
    min_level: Optional[str] = None  # info | warn | error


class NotificationSettingsOut(BaseModel):
    discord_webhook_url: Optional[str] = None
    email_smtp_host: Optional[str] = None
    email_smtp_port: Optional[int] = None
    email_smtp_user: Optional[str] = None
    has_password: bool = False  # password itself is never returned
    email_from: Optional[str] = None
    email_to: Optional[str] = None
    email_smtp_secure: bool = True
    webhook_urls: Optional[str] = None
    events: Dict[str, bool] = {}
    min_level: str = "info"


# --- Helpers ---

async def _get_or_create_app_settings(db: AsyncSession) -> AppSettings:
    """Get or create the singleton app settings row."""
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    if not row:
        row = AppSettings(id=1)
        db.add(row)
        await db.flush()
    return row


def _settings_out(settings_json: Optional[dict]) -> NotificationSettingsOut:
    """Build the API view of the EFFECTIVE config (DB first, env fallback)."""
    from app.services.notification_service import resolve_config

    cfg = resolve_config(settings_json)
    webhook_urls = cfg["webhook_urls"]
    return NotificationSettingsOut(
        discord_webhook_url=cfg["discord_webhook_url"],
        email_smtp_host=cfg["email_smtp_host"],
        email_smtp_port=cfg["email_smtp_port"],
        email_smtp_user=cfg["email_smtp_user"],
        has_password=bool(cfg["email_smtp_password"]),
        email_from=cfg["email_from"],
        email_to=cfg["email_to"],
        email_smtp_secure=cfg["email_secure"],
        webhook_urls=",".join(webhook_urls) if webhook_urls else None,
        events=cfg["events"],
        min_level=cfg["min_level"],
    )


# --- Endpoints ---

@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    type: Optional[str] = Query(None, description="Filter by notification type"),
    channel: Optional[str] = Query(None, description="Filter by channel (discord/email/webhook)"),
    db: AsyncSession = Depends(get_db),
):
    """List notification records with pagination and optional filters."""
    query = select(Notification)
    count_query = select(sa_func.count()).select_from(Notification)

    if type:
        query = query.where(Notification.type == type)
        count_query = count_query.where(Notification.type == type)
    if channel:
        query = query.where(Notification.channel == channel)
        count_query = count_query.where(Notification.channel == channel)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(Notification.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    rows = result.scalars().all()

    return NotificationListResponse(
        items=[NotificationOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/test/{channel}", response_model=TestNotificationResponse)
async def test_notification_channel(channel: str):
    """Send a test notification to one channel using the CURRENT saved settings."""
    from app.services.notification_service import get_notification_service

    if channel not in VALID_CHANNELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid channel '{channel}'. Must be one of: {', '.join(sorted(VALID_CHANNELS))}",
        )

    service = get_notification_service()
    success, detail = await service.send_test(channel)
    return TestNotificationResponse(success=success, channel=channel, detail=detail)


@router.post("/test", response_model=TestNotificationResponse)
async def test_notification(body: TestNotificationRequest):
    """Legacy test endpoint (channel in body). Delegates to the saved-settings path."""
    from app.services.notification_service import get_notification_service

    if body.channel not in VALID_CHANNELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid channel '{body.channel}'. Must be one of: {', '.join(sorted(VALID_CHANNELS))}",
        )

    service = get_notification_service()
    success, detail = await service.send_test(body.channel, message=body.message)
    return TestNotificationResponse(success=success, channel=body.channel, detail=detail)


@router.put("/settings", response_model=NotificationSettingsOut)
async def update_notification_settings(
    body: NotificationSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update notification configuration stored in AppSettings.settings_json.

    Only fields present in the request are written; the SMTP password is
    write-only (omit it to keep the stored one).
    """
    if body.min_level is not None and body.min_level not in VALID_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid min_level '{body.min_level}'. Must be one of: {', '.join(sorted(VALID_LEVELS))}",
        )

    app_settings = await _get_or_create_app_settings(db)

    sj = dict(app_settings.settings_json or {})

    update_data = body.model_dump(exclude_unset=True)
    field_map = {
        "discord_webhook_url": "notification_discord_webhook_url",
        "email_smtp_host": "notification_email_smtp_host",
        "email_smtp_port": "notification_email_smtp_port",
        "email_smtp_user": "notification_email_smtp_user",
        "email_smtp_password": "notification_email_smtp_password",
        "email_from": "notification_email_from",
        "email_to": "notification_email_to",
        "email_smtp_secure": "notification_email_smtp_secure",
        "webhook_urls": "notification_webhook_urls",
        "events": "notification_events",
        "min_level": "notification_min_level",
    }

    for field_name, json_key in field_map.items():
        if field_name in update_data:
            sj[json_key] = update_data[field_name]

    app_settings.settings_json = sj
    await db.flush()
    await db.commit()

    # Saved settings apply immediately (the service caches config for 60s).
    try:
        from app.services.notification_service import get_notification_service

        get_notification_service().invalidate_config_cache()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not invalidate notification config cache", exc_info=True)

    return _settings_out(sj)


@router.get("/settings", response_model=NotificationSettingsOut)
async def get_notification_settings(db: AsyncSession = Depends(get_db)):
    """Get the effective notification configuration (password never included)."""
    app_settings = await _get_or_create_app_settings(db)
    return _settings_out(app_settings.settings_json)
