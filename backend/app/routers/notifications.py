"""Notification listing, testing, and settings endpoints."""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AppSettings, Notification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


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
    discord_webhook_url: Optional[str] = None
    email_smtp_host: Optional[str] = None
    email_smtp_port: Optional[int] = None
    email_smtp_user: Optional[str] = None
    email_smtp_password: Optional[str] = None
    email_to: Optional[str] = None
    webhook_urls: Optional[str] = None  # comma-separated


class NotificationSettingsOut(BaseModel):
    discord_webhook_url: Optional[str] = None
    email_smtp_host: Optional[str] = None
    email_smtp_port: Optional[int] = None
    email_smtp_user: Optional[str] = None
    email_to: Optional[str] = None
    webhook_urls: Optional[str] = None

    class Config:
        from_attributes = True


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


def _extract_notification_settings(app_settings: AppSettings) -> dict:
    """Extract notification-related fields from settings_json."""
    sj = app_settings.settings_json or {}
    return {
        "discord_webhook_url": sj.get("notification_discord_webhook_url"),
        "email_smtp_host": sj.get("notification_email_smtp_host"),
        "email_smtp_port": sj.get("notification_email_smtp_port"),
        "email_smtp_user": sj.get("notification_email_smtp_user"),
        "email_to": sj.get("notification_email_to"),
        "webhook_urls": sj.get("notification_webhook_urls"),
    }


# --- Endpoints ---


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    type: Optional[str] = Query(None, description="Filter by notification type"),
    channel: Optional[str] = Query(
        None, description="Filter by channel (discord/email/webhook)"
    ),
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


@router.post("/test", response_model=TestNotificationResponse)
async def test_notification(body: TestNotificationRequest):
    """Send a test notification to the specified channel."""
    from app.services.notification_service import NotificationService

    valid_channels = {"discord", "email", "webhook"}
    if body.channel not in valid_channels:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid channel '{body.channel}'. Must be one of: {', '.join(sorted(valid_channels))}",
        )

    service = NotificationService()
    try:
        await service.notify(
            notification_type="info",
            title="Test Notification",
            message=body.message or "This is a test notification from Fade-Out.",
        )
        # The notification service queues and sends asynchronously.
        # For a direct test, attempt immediate delivery.
        await service._send_all_channels(
            {
                "type": "info",
                "mix_id": None,
                "title": "Test Notification",
                "message": body.message or "This is a test notification from Fade-Out.",
                "data": {},
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        return TestNotificationResponse(
            success=True,
            channel=body.channel,
            detail=f"Test notification sent to {body.channel}.",
        )
    except Exception as exc:
        logger.exception("Test notification failed for channel %s", body.channel)
        return TestNotificationResponse(
            success=False,
            channel=body.channel,
            detail=f"Failed to send test notification: {exc}",
        )


@router.put("/settings", response_model=NotificationSettingsOut)
async def update_notification_settings(
    body: NotificationSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update notification configuration stored in AppSettings.settings_json."""
    app_settings = await _get_or_create_app_settings(db)

    sj = dict(app_settings.settings_json or {})

    update_data = body.model_dump(exclude_unset=True)
    field_map = {
        "discord_webhook_url": "notification_discord_webhook_url",
        "email_smtp_host": "notification_email_smtp_host",
        "email_smtp_port": "notification_email_smtp_port",
        "email_smtp_user": "notification_email_smtp_user",
        "email_smtp_password": "notification_email_smtp_password",
        "email_to": "notification_email_to",
        "webhook_urls": "notification_webhook_urls",
    }

    for field_name, json_key in field_map.items():
        if field_name in update_data:
            sj[json_key] = update_data[field_name]

    app_settings.settings_json = sj
    await db.flush()

    # Return settings without the password
    return NotificationSettingsOut(
        discord_webhook_url=sj.get("notification_discord_webhook_url"),
        email_smtp_host=sj.get("notification_email_smtp_host"),
        email_smtp_port=sj.get("notification_email_smtp_port"),
        email_smtp_user=sj.get("notification_email_smtp_user"),
        email_to=sj.get("notification_email_to"),
        webhook_urls=sj.get("notification_webhook_urls"),
    )


@router.get("/settings", response_model=NotificationSettingsOut)
async def get_notification_settings(db: AsyncSession = Depends(get_db)):
    """Get current notification configuration."""
    app_settings = await _get_or_create_app_settings(db)
    data = _extract_notification_settings(app_settings)
    return NotificationSettingsOut(**data)
