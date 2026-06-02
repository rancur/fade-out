"""Multi-channel notification service: Discord webhooks, email, generic webhooks."""

import asyncio
import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.database import async_session_factory
from app.models import Notification

logger = logging.getLogger(__name__)

NOTIFICATION_TYPES = {
    "pipeline_started",
    "step_completed",
    "upload_complete",
    "error",
    "draft_ready",
    "upgrade_available",
}

# Rate limit: 1 notification per mix per step per channel within this window
RATE_LIMIT_SECONDS = 300

# Color codes for Discord embeds
DISCORD_COLORS: Dict[str, int] = {
    "pipeline_started": 0x3498DB,  # blue
    "step_completed": 0x2ECC71,  # green
    "upload_complete": 0x9B59B6,  # purple
    "error": 0xE74C3C,  # red
    "draft_ready": 0xF39C12,  # orange
    "upgrade_available": 0x1ABC9C,  # teal
}


class NotificationService:
    """Async notification service with rate limiting and multiple channels."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        # Rate limit tracker: (mix_id, step, channel) -> last_sent_timestamp
        self._rate_tracker: Dict[tuple, float] = {}

    async def start(self) -> None:
        """Start the background notification worker."""
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("Notification service started")

    async def stop(self) -> None:
        """Stop the worker."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Notification service stopped")

    async def notify(
        self,
        notification_type: str,
        mix_id: Optional[str] = None,
        title: str = "",
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Queue a notification for delivery."""
        if notification_type not in NOTIFICATION_TYPES:
            logger.warning("Unknown notification type: %s", notification_type)

        payload = {
            "type": notification_type,
            "mix_id": mix_id,
            "title": title or notification_type.replace("_", " ").title(),
            "message": message,
            "data": data or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._queue.put(payload)
        logger.debug("Notification queued: %s for mix %s", notification_type, mix_id)

    async def _worker(self) -> None:
        """Process queued notifications."""
        while self._running:
            try:
                payload = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._send_all_channels(payload)
            except Exception:
                logger.exception("Error sending notification: %s", payload.get("type"))

    async def _send_all_channels(self, payload: Dict[str, Any]) -> None:
        """Send to all configured channels."""
        tasks = []

        if settings.NOTIFICATION_DISCORD_WEBHOOK_URL:
            tasks.append(self._send_discord(payload))

        if settings.NOTIFICATION_EMAIL_SMTP_HOST and settings.NOTIFICATION_EMAIL_TO:
            tasks.append(self._send_email(payload))

        webhook_urls = settings.get_webhook_urls()
        for url in webhook_urls:
            tasks.append(self._send_webhook(url, payload))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Discord
    # ------------------------------------------------------------------

    async def _send_discord(self, payload: Dict[str, Any]) -> None:
        mix_id = payload.get("mix_id")
        ntype = payload.get("type", "")

        if not self._check_rate_limit(mix_id, ntype, "discord"):
            return

        color = DISCORD_COLORS.get(ntype, 0x95A5A6)
        data = payload.get("data", {})

        fields: List[Dict[str, Any]] = []
        if data.get("step"):
            fields.append({"name": "Step", "value": data["step"], "inline": True})
        if data.get("elapsed_seconds"):
            fields.append(
                {
                    "name": "Duration",
                    "value": f"{data['elapsed_seconds']:.1f}s",
                    "inline": True,
                }
            )
        if data.get("error"):
            fields.append(
                {"name": "Error", "value": data["error"][:1024], "inline": False}
            )

        # Add platform links if available
        for key in ("soundcloud_url", "youtube_url"):
            if data.get(key):
                fields.append(
                    {
                        "name": key.replace("_", " ").title(),
                        "value": data[key],
                        "inline": True,
                    }
                )

        embed: Dict[str, Any] = {
            "title": payload.get("title", "Fade-Out Notification"),
            "description": payload.get("message", "")[:4096],
            "color": color,
            "timestamp": payload.get("timestamp"),
            "footer": {"text": "Fade-Out Pipeline"},
        }
        if fields:
            embed["fields"] = fields
        if data.get("thumbnail_url"):
            embed["thumbnail"] = {"url": data["thumbnail_url"]}

        body = {"embeds": [embed]}

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                settings.NOTIFICATION_DISCORD_WEBHOOK_URL, json=body
            )
            if resp.status_code in (200, 204):
                await self._record(mix_id, ntype, "discord", payload.get("message", ""))
                logger.info("Discord notification sent: %s", ntype)
            else:
                logger.warning(
                    "Discord webhook returned %d: %s", resp.status_code, resp.text[:200]
                )

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------

    async def _send_email(self, payload: Dict[str, Any]) -> None:
        mix_id = payload.get("mix_id")
        ntype = payload.get("type", "")

        if not self._check_rate_limit(mix_id, ntype, "email"):
            return

        subject = f"[Fade-Out] {payload.get('title', ntype)}"
        html_body = self._build_email_html(payload)

        try:
            await asyncio.to_thread(self._smtp_send, subject, html_body)
            await self._record(mix_id, ntype, "email", payload.get("message", ""))
            logger.info("Email notification sent: %s", ntype)
        except Exception as exc:
            logger.error("Email send failed: %s", exc)

    def _smtp_send(self, subject: str, html_body: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["From"] = settings.NOTIFICATION_EMAIL_SMTP_USER
        msg["To"] = settings.NOTIFICATION_EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(
            settings.NOTIFICATION_EMAIL_SMTP_HOST, settings.NOTIFICATION_EMAIL_SMTP_PORT
        ) as server:
            server.starttls()
            server.login(
                settings.NOTIFICATION_EMAIL_SMTP_USER,
                settings.NOTIFICATION_EMAIL_SMTP_PASSWORD,
            )
            server.send_message(msg)

    def _build_email_html(self, payload: Dict[str, Any]) -> str:
        data = payload.get("data", {})
        ntype = payload.get("type", "")
        title = payload.get("title", "")
        message = payload.get("message", "")

        color = {
            "error": "#E74C3C",
            "upload_complete": "#9B59B6",
            "draft_ready": "#F39C12",
        }.get(ntype, "#3498DB")

        links_html = ""
        for key in ("soundcloud_url", "youtube_url"):
            if data.get(key):
                label = key.replace("_url", "").replace("_", " ").title()
                links_html += f'<p><a href="{data[key]}">{label}</a></p>'

        error_html = ""
        if data.get("error"):
            error_html = f'<div style="background:#FDECEA;padding:10px;border-radius:4px;margin:10px 0"><code>{data["error"]}</code></div>'

        return f"""
        <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
            <div style="background:{color};color:white;padding:20px;border-radius:8px 8px 0 0">
                <h2 style="margin:0">{title}</h2>
            </div>
            <div style="background:#F8F9FA;padding:20px;border-radius:0 0 8px 8px">
                <p>{message}</p>
                {error_html}
                {links_html}
                <hr style="border:none;border-top:1px solid #DDD;margin:20px 0">
                <p style="color:#999;font-size:12px">Fade-Out Pipeline | {payload.get("timestamp", "")}</p>
            </div>
        </div>
        """

    # ------------------------------------------------------------------
    # Generic webhook
    # ------------------------------------------------------------------

    async def _send_webhook(self, url: str, payload: Dict[str, Any]) -> None:
        mix_id = payload.get("mix_id")
        ntype = payload.get("type", "")

        if not self._check_rate_limit(mix_id, ntype, f"webhook:{url}"):
            return

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code < 300:
                    await self._record(
                        mix_id, ntype, "webhook", payload.get("message", "")
                    )
                    logger.info("Webhook notification sent to %s: %s", url, ntype)
                else:
                    logger.warning("Webhook %s returned %d", url, resp.status_code)
            except Exception as exc:
                logger.error("Webhook %s failed: %s", url, exc)

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _check_rate_limit(
        self, mix_id: Optional[str], ntype: str, channel: str
    ) -> bool:
        if not mix_id:
            return True
        key = (mix_id, ntype, channel)
        last = self._rate_tracker.get(key, 0)
        now = time.time()
        if now - last < RATE_LIMIT_SECONDS:
            logger.debug("Rate limited: %s/%s/%s", mix_id, ntype, channel)
            return False
        self._rate_tracker[key] = now
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _record(
        self, mix_id: Optional[str], ntype: str, channel: str, message: str
    ) -> None:
        try:
            async with async_session_factory() as session:
                notification = Notification(
                    mix_id=mix_id,
                    type=ntype,
                    channel=channel,
                    message=message[:2000],
                    sent=True,
                    sent_at=datetime.now(timezone.utc),
                )
                session.add(notification)
                await session.commit()
        except Exception:
            logger.exception("Failed to record notification")
