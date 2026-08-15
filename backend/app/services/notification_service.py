"""Multi-channel notification service: Discord webhooks, email, generic webhooks.

Configuration comes from ``AppSettings.settings_json`` (the ``notification_*``
keys written by ``PUT /api/notifications/settings``), with env settings used as
fallback ONLY for keys absent from the DB. The DB read is cached for 60s and
uses its own session — the service runs outside any request scope.
"""

import asyncio
import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.database import async_session_factory
from app.models import AppSettings, Notification

logger = logging.getLogger(__name__)

NOTIFICATION_TYPES = {
    "pipeline_started",
    "step_completed",
    "upload_complete",
    "error",
    "draft_ready",
    "upgrade_available",
    # One platform leg failed while others may still have published, and the
    # mix finished only partly live. Both are actionable on their own.
    "platform_failed",
    "publish_incomplete",
    "stuck_mix",
    "deployment_stale",
}

# Per-event-type default toggles when notification_events is not configured.
# Terminal/actionable events on, per-step chatter off.
DEFAULT_EVENT_TOGGLES: Dict[str, bool] = {
    "pipeline_started": False,
    "step_completed": False,
    "upload_complete": True,
    "error": True,
    "draft_ready": True,
    "upgrade_available": True,
    "platform_failed": True,
    "publish_incomplete": True,
    "stuck_mix": True,
    "deployment_stale": True,
}

# Severity of each notification type, checked against notification_min_level.
TYPE_LEVELS: Dict[str, str] = {
    "error": "error",
    "platform_failed": "error",
    "publish_incomplete": "error",
    "stuck_mix": "warn",
    "deployment_stale": "warn",
}
LEVEL_RANK = {"info": 0, "warn": 1, "error": 2}

# Rate limit: 1 notification per mix per step per channel within this window
RATE_LIMIT_SECONDS = 300

CONFIG_CACHE_SECONDS = 60

# Color codes for Discord embeds
DISCORD_COLORS: Dict[str, int] = {
    "pipeline_started": 0x3498DB,   # blue
    "step_completed": 0x2ECC71,     # green
    "upload_complete": 0x9B59B6,    # purple
    "error": 0xE74C3C,              # red
    "draft_ready": 0xF39C12,        # orange
    "upgrade_available": 0x1ABC9C,  # teal
    "platform_failed": 0xE74C3C,    # red
    "publish_incomplete": 0xE67E22, # dark orange
    "stuck_mix": 0xE67E22,          # dark orange
    "deployment_stale": 0xE67E22,   # dark orange
}


def _split_urls(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [u.strip() for u in value.split(",") if u.strip()]


def resolve_config(settings_json: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve effective notification config from DB settings_json + env.

    DB keys win whenever present (even if empty/None — an explicit DB write
    can disable a channel); env values fill in ONLY for absent keys. Empty
    env strings resolve to None so "unconfigured" is uniform.
    """
    sj = settings_json or {}

    def pick(key: str, env_value: Any) -> Any:
        if key in sj:
            return sj[key]
        return env_value if env_value not in ("", None) else None

    events = dict(DEFAULT_EVENT_TOGGLES)
    raw_events = sj.get("notification_events")
    if isinstance(raw_events, dict):
        for k, v in raw_events.items():
            events[k] = bool(v)

    min_level = sj.get("notification_min_level") or "info"
    if min_level not in LEVEL_RANK:
        min_level = "info"

    port = pick("notification_email_smtp_port", settings.NOTIFICATION_EMAIL_SMTP_PORT)
    try:
        port = int(port) if port is not None else 587
    except (TypeError, ValueError):
        port = 587

    smtp_user = pick("notification_email_smtp_user", settings.NOTIFICATION_EMAIL_SMTP_USER)
    webhook_raw = pick("notification_webhook_urls", settings.NOTIFICATION_WEBHOOK_URLS)

    return {
        "discord_webhook_url": pick(
            "notification_discord_webhook_url", settings.NOTIFICATION_DISCORD_WEBHOOK_URL
        ),
        "email_smtp_host": pick(
            "notification_email_smtp_host", settings.NOTIFICATION_EMAIL_SMTP_HOST
        ),
        "email_smtp_port": port,
        "email_smtp_user": smtp_user,
        "email_smtp_password": pick(
            "notification_email_smtp_password", settings.NOTIFICATION_EMAIL_SMTP_PASSWORD
        ),
        "email_from": pick("notification_email_from", None) or smtp_user,
        "email_to": pick("notification_email_to", settings.NOTIFICATION_EMAIL_TO),
        "email_secure": bool(sj.get("notification_email_smtp_secure", True)),
        "webhook_urls": webhook_raw if isinstance(webhook_raw, list) else _split_urls(webhook_raw),
        "events": events,
        "min_level": min_level,
    }


class NotificationService:
    """Async notification service with rate limiting and multiple channels."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        # Rate limit tracker: (mix_id, step, channel) -> last_sent_timestamp
        self._rate_tracker: Dict[tuple, float] = {}
        # Config cache (60s TTL)
        self._config_cache: Optional[Dict[str, Any]] = None
        self._config_cached_at: float = 0.0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def invalidate_config_cache(self) -> None:
        """Drop the cached config so the next send re-reads the DB."""
        self._config_cache = None
        self._config_cached_at = 0.0

    async def get_config(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Effective config: DB settings_json first, env fallback. Cached 60s."""
        now = time.monotonic()
        if (
            not force_refresh
            and self._config_cache is not None
            and (now - self._config_cached_at) < CONFIG_CACHE_SECONDS
        ):
            return self._config_cache

        sj: Dict[str, Any] = {}
        try:
            async with async_session_factory() as session:
                row = await session.get(AppSettings, 1)
                if row is not None:
                    sj = row.settings_json or {}
        except Exception:
            logger.exception("Failed to read notification config from DB; using env fallback")

        self._config_cache = resolve_config(sj)
        self._config_cached_at = now
        return self._config_cache

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Queueing / dispatch
    # ------------------------------------------------------------------

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

    async def handle_orchestrator_event(
        self, event_type: str, mix_id: Optional[str], data: Optional[dict] = None
    ) -> None:
        """Orchestrator listener: translate pipeline events into notifications.

        Per-event toggles + min-level from the saved settings are applied at
        send time (config may change while an event sits in the queue).
        """
        data = data or {}
        if event_type == "pipeline_started":
            await self.notify("pipeline_started", mix_id=mix_id,
                              message="Pipeline started.", data=data)
        elif event_type == "step_completed":
            step = data.get("step", "?")
            elapsed = data.get("elapsed_seconds")
            msg = f"Step {step} completed"
            if elapsed is not None:
                msg += f" in {elapsed:.1f}s"
            await self.notify("step_completed", mix_id=mix_id, message=msg + ".", data=data)
        elif event_type == "upload_complete":
            await self.notify("upload_complete", mix_id=mix_id,
                              message="Mix finished: all uploads complete.", data=data)
        elif event_type == "error":
            # Name the proximate cause, not just the step. "Mix failed at step
            # upload_soundcloud" plus a Playwright timeout is what sent the
            # 08-12 reader to the browser automation instead of to the dead
            # OAuth grant.
            cause = data.get("cause") or {}
            headline = cause.get("summary") or data.get("error") or "failed"
            await self.notify(
                "error", mix_id=mix_id,
                title=self._failure_title(data, cause),
                message=f"Mix failed at step {data.get('step')}: {headline}",
                data=data,
            )
        elif event_type == "platform_failed":
            cause = data.get("cause") or {}
            platform = data.get("platform", "platform")
            headline = cause.get("summary") or data.get("error") or "failed"
            await self.notify(
                "platform_failed", mix_id=mix_id,
                title=self._failure_title(data, cause),
                message=(
                    f"{platform} did not publish ({data.get('step')}): {headline} "
                    "Other platforms were attempted independently."
                ),
                data=data,
            )
        elif event_type == "publish_incomplete":
            published = data.get("published") or []
            failed = data.get("failed") or []
            await self.notify(
                "publish_incomplete", mix_id=mix_id,
                title=(
                    f"Partial publish — {', '.join(published)} live, "
                    f"{', '.join(failed)} not"
                    if published else "Publish failed on every target"
                ),
                message=data.get("error", "Publish incomplete."),
                data=data,
            )
        elif event_type == "draft_ready":
            await self.notify("draft_ready", mix_id=mix_id,
                              message="Mix is ready for draft review.", data=data)
        # step_progress and anything else: intentionally not notified.

    @staticmethod
    def _rate_key(payload: Dict[str, Any]) -> str:
        """Rate-limit bucket for a payload.

        Per (mix, type, platform, step): two DIFFERENT platforms failing on
        the same mix are two different facts, and collapsing them into one
        bucket would hide the second one entirely.
        """
        data = payload.get("data") or {}
        cause = data.get("cause") or {}
        parts = [
            payload.get("type", ""),
            str(cause.get("platform") or data.get("platform") or ""),
            str(data.get("step") or ""),
        ]
        return ":".join(p for p in parts if p)

    @staticmethod
    def _failure_title(data: Dict[str, Any], cause: Dict[str, Any]) -> str:
        """Subject line that states the KIND of failure and where it is.

        An auth failure has to read as an auth failure at a glance, in the
        subject, before anyone opens the mail.
        """
        platform = cause.get("platform") or data.get("platform")
        kind = cause.get("kind", "error")
        labels = {
            "auth": "Authorization failed",
            "quota": "Quota/rate limit",
            "missing_file": "Source file missing",
            "video_not_ready": "Video source incomplete",
            "network": "Network failure",
            "unknown": "Pipeline error",
        }
        label = labels.get(kind, "Pipeline error")
        if platform:
            return f"{label} — {platform}"
        return label

    def _passes_filters(self, ntype: str, cfg: Dict[str, Any]) -> bool:
        if not cfg["events"].get(ntype, True):
            logger.debug("Notification type %s disabled by settings", ntype)
            return False
        type_level = TYPE_LEVELS.get(ntype, "info")
        if LEVEL_RANK[type_level] < LEVEL_RANK[cfg["min_level"]]:
            logger.debug(
                "Notification type %s (%s) below min level %s",
                ntype, type_level, cfg["min_level"],
            )
            return False
        return True

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
        """Send to all configured channels, honoring toggles + min level."""
        cfg = await self.get_config()

        if not self._passes_filters(payload.get("type", ""), cfg):
            return

        tasks = []
        if cfg["discord_webhook_url"]:
            tasks.append(self._send_discord(payload, cfg))

        if cfg["email_smtp_host"] and cfg["email_to"]:
            tasks.append(self._send_email(payload, cfg))

        for url in cfg["webhook_urls"]:
            tasks.append(self._send_webhook(url, payload, cfg))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Test sends
    # ------------------------------------------------------------------

    async def send_test(self, channel: str, message: Optional[str] = None) -> Tuple[bool, str]:
        """Send a test notification to one channel using the CURRENT saved
        settings. Bypasses event toggles/min-level (a test should always try).
        """
        cfg = await self.get_config(force_refresh=True)
        payload = {
            "type": "test",
            "mix_id": None,
            "title": "Test Notification",
            "message": message or "This is a test notification from Fade-Out.",
            "data": {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            if channel == "discord":
                if not cfg["discord_webhook_url"]:
                    return False, "Discord webhook URL is not configured."
                ok = await self._send_discord(payload, cfg)
            elif channel == "email":
                if not (cfg["email_smtp_host"] and cfg["email_to"]):
                    return False, "Email SMTP host / recipient are not configured."
                ok = await self._send_email(payload, cfg)
            elif channel == "webhook":
                if not cfg["webhook_urls"]:
                    return False, "No webhook URLs are configured."
                results = await asyncio.gather(
                    *[self._send_webhook(u, payload, cfg) for u in cfg["webhook_urls"]],
                    return_exceptions=True,
                )
                ok = any(r is True for r in results)
            else:
                return False, f"Unknown channel: {channel}"
        except Exception as exc:
            logger.exception("Test notification failed for channel %s", channel)
            return False, f"Failed to send test notification: {exc}"

        if ok:
            return True, f"Test notification sent to {channel}."
        return False, f"Test notification to {channel} failed (see logs / activity feed)."

    # ------------------------------------------------------------------
    # Discord
    # ------------------------------------------------------------------

    async def _send_discord(self, payload: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
        mix_id = payload.get("mix_id")
        ntype = payload.get("type", "")

        if not self._check_rate_limit(mix_id, self._rate_key(payload), "discord"):
            return False

        color = DISCORD_COLORS.get(ntype, 0x95A5A6)
        data = payload.get("data", {})

        fields: List[Dict[str, Any]] = []
        if data.get("step"):
            fields.append({"name": "Step", "value": data["step"], "inline": True})
        if data.get("elapsed_seconds"):
            fields.append({"name": "Duration", "value": f"{data['elapsed_seconds']:.1f}s", "inline": True})
        cause = data.get("cause") or {}
        if cause:
            fields.append({
                "name": "Cause",
                "value": str(cause.get("summary") or data.get("error", ""))[:1024],
                "inline": False,
            })
            fields.append({"name": "Type", "value": str(cause.get("kind", "unknown")), "inline": True})
            if cause.get("platform"):
                fields.append({"name": "Platform", "value": str(cause["platform"]), "inline": True})
            if cause.get("credential"):
                # Key name only. Never a value.
                fields.append({"name": "Credential", "value": str(cause["credential"]), "inline": True})
        elif data.get("error"):
            fields.append({"name": "Error", "value": str(data["error"])[:1024], "inline": False})

        # Add platform links if available
        for key in ("soundcloud_url", "youtube_url"):
            if data.get(key):
                fields.append({"name": key.replace("_", " ").title(), "value": data[key], "inline": True})

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

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(cfg["discord_webhook_url"], json=body)
        except Exception as exc:
            logger.error("Discord webhook failed: %s", exc)
            await self._record(mix_id, ntype, "discord", payload.get("message", ""),
                               sent=False, detail=str(exc))
            return False

        if resp.status_code in (200, 204):
            await self._record(mix_id, ntype, "discord", payload.get("message", ""), sent=True)
            logger.info("Discord notification sent: %s", ntype)
            return True

        logger.warning("Discord webhook returned %d: %s", resp.status_code, resp.text[:200])
        await self._record(mix_id, ntype, "discord", payload.get("message", ""),
                           sent=False, detail=f"HTTP {resp.status_code}")
        return False

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------

    async def _send_email(self, payload: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
        mix_id = payload.get("mix_id")
        ntype = payload.get("type", "")

        if not self._check_rate_limit(mix_id, self._rate_key(payload), "email"):
            return False

        subject = f"[Fade-Out] {payload.get('title', ntype)}"
        html_body = self._build_email_html(payload)

        try:
            await asyncio.to_thread(self._smtp_send, subject, html_body, cfg)
            await self._record(mix_id, ntype, "email", payload.get("message", ""), sent=True)
            logger.info("Email notification sent: %s", ntype)
            return True
        except Exception as exc:
            logger.error("Email send failed: %s", exc)
            await self._record(mix_id, ntype, "email", payload.get("message", ""),
                               sent=False, detail=str(exc))
            return False

    def _smtp_send(self, subject: str, html_body: str, cfg: Dict[str, Any]) -> None:
        msg = MIMEMultipart("alternative")
        msg["From"] = cfg["email_from"] or cfg["email_smtp_user"] or ""
        msg["To"] = cfg["email_to"]
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        host = cfg["email_smtp_host"]
        port = cfg["email_smtp_port"]
        secure = cfg["email_secure"]

        if secure and port == 465:
            server_cls: Any = smtplib.SMTP_SSL
        else:
            server_cls = smtplib.SMTP

        with server_cls(host, port) as server:
            if secure and server_cls is smtplib.SMTP:
                server.starttls()
            if cfg["email_smtp_user"] and cfg["email_smtp_password"]:
                server.login(cfg["email_smtp_user"], cfg["email_smtp_password"])
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

        # --- Cause block -------------------------------------------------
        # The failure mail's job is to point at the thing that is actually
        # broken. It names the kind of failure, the platform, and the
        # credential KEY that needs attention — never a credential value.
        cause = data.get("cause") or {}
        cause_html = ""
        if cause:
            rows = [
                ("Cause", cause.get("summary") or data.get("error", "")),
                ("Type", cause.get("kind", "unknown")),
                ("Platform", cause.get("platform") or data.get("platform") or "—"),
                ("Credential", cause.get("credential") or "—"),
                ("Retryable", "no — operator action required"
                              if cause.get("retryable") is False else "yes"),
            ]
            if cause.get("remediation"):
                rows.append(("Fix", cause["remediation"]))
            row_html = "".join(
                f'<tr><td style="padding:4px 10px 4px 0;color:#666;'
                f'vertical-align:top;white-space:nowrap">{escape(str(k))}</td>'
                f'<td style="padding:4px 0"><strong>{escape(str(v))}</strong></td></tr>'
                for k, v in rows
            )
            cause_html = (
                '<table style="margin:12px 0;border-collapse:collapse;font-size:13px">'
                f"{row_html}</table>"
            )

        legs_html = ""
        legs = data.get("legs") or {}
        if legs:
            items = "".join(
                f"<li><strong>{escape(str(name))}</strong>: {escape(str(info.get('status')))}"
                + (f" — {escape(str(info.get('error')))}" if info.get("error") else "")
                + (f' — <a href="{escape(str(info["url"]))}">link</a>' if info.get("url") else "")
                + "</li>"
                for name, info in legs.items()
            )
            legs_html = f'<ul style="font-size:13px;padding-left:18px">{items}</ul>'

        error_html = ""
        raw_error = data.get("error")
        if raw_error and raw_error != cause.get("summary"):
            error_html = (
                '<div style="background:#FDECEA;padding:10px;border-radius:4px;'
                f'margin:10px 0"><code>{escape(str(raw_error))}</code></div>'
            )

        return f"""
        <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
            <div style="background:{color};color:white;padding:20px;border-radius:8px 8px 0 0">
                <h2 style="margin:0">{title}</h2>
            </div>
            <div style="background:#F8F9FA;padding:20px;border-radius:0 0 8px 8px">
                <p>{message}</p>
                {cause_html}
                {legs_html}
                {error_html}
                {links_html}
                <hr style="border:none;border-top:1px solid #DDD;margin:20px 0">
                <p style="color:#999;font-size:12px">Fade-Out Pipeline | {payload.get('timestamp', '')}</p>
            </div>
        </div>
        """

    # ------------------------------------------------------------------
    # Generic webhook
    # ------------------------------------------------------------------

    async def _send_webhook(
        self, url: str, payload: Dict[str, Any], cfg: Dict[str, Any]
    ) -> bool:
        mix_id = payload.get("mix_id")
        ntype = payload.get("type", "")

        if not self._check_rate_limit(mix_id, self._rate_key(payload), f"webhook:{url}"):
            return False

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code < 300:
                await self._record(mix_id, ntype, "webhook", payload.get("message", ""), sent=True)
                logger.info("Webhook notification sent to %s: %s", url, ntype)
                return True
            logger.warning("Webhook %s returned %d", url, resp.status_code)
            await self._record(mix_id, ntype, "webhook", payload.get("message", ""),
                               sent=False, detail=f"HTTP {resp.status_code}")
            return False
        except Exception as exc:
            logger.error("Webhook %s failed: %s", url, exc)
            await self._record(mix_id, ntype, "webhook", payload.get("message", ""),
                               sent=False, detail=str(exc))
            return False

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _check_rate_limit(self, mix_id: Optional[str], ntype: str, channel: str) -> bool:
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
    # Persistence + activity mirror
    # ------------------------------------------------------------------

    async def _record(
        self,
        mix_id: Optional[str],
        ntype: str,
        channel: str,
        message: str,
        sent: bool = True,
        detail: Optional[str] = None,
    ) -> None:
        """Record the delivery attempt in history AND the activity feed."""
        try:
            async with async_session_factory() as session:
                notification = Notification(
                    mix_id=mix_id,
                    type=ntype,
                    channel=channel,
                    message=message[:2000],
                    sent=sent,
                    sent_at=datetime.now(timezone.utc) if sent else None,
                )
                session.add(notification)
                await session.commit()
        except Exception:
            logger.exception("Failed to record notification")

        # Mirror every delivery attempt into the activity feed so History
        # rows tie back to the running event log.
        try:
            from app.services import activity_log

            if sent:
                await activity_log.info(
                    "notification_sent",
                    f"Notification sent via {channel}: {ntype} — {message[:200]}",
                    mix_id=mix_id,
                    context={"channel": channel, "type": ntype},
                )
            else:
                await activity_log.warn(
                    "notification_failed",
                    f"Notification via {channel} failed: {ntype}"
                    + (f" — {detail}" if detail else ""),
                    mix_id=mix_id,
                    context={"channel": channel, "type": ntype, "detail": detail},
                )
        except Exception:  # pragma: no cover - defensive
            logger.debug("activity mirror failed for notification", exc_info=True)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    """Process-wide NotificationService singleton (created lazily)."""
    global _service
    if _service is None:
        _service = NotificationService()
    return _service
