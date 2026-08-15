"""Self-upgrade mechanism: checks GitHub releases, backs up settings, signals restart."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.database import async_session_factory
from app.models import AppSettings, BrandSettings, Mix

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
BACKUP_DIR = "/data/backups"
CURRENT_VERSION_FILE = "/app/VERSION"

# How long a successful release check stays authoritative, and how long before
# never-checked/failed state is loud rather than quiet.
DEPLOYMENT_CHECK_INTERVAL_SECONDS = 6 * 3600
DEPLOYMENT_STALE_CHECK_SECONDS = 26 * 3600


def _get_current_version() -> str:
    """The version of the code that is actually running.

    Previously this read ``/app/VERSION`` — a file the image does not contain —
    and fell back to ``0.0.0``. That fallback is why a container built on
    2026-07-21 reported ``current_version 0.0.0`` and
    ``update_available false`` for weeks: 0.0.0 is not a version anyone
    notices, and the comparison never ran. The version now ships inside the
    code, so it cannot go missing from the image.
    """
    if os.path.exists(CURRENT_VERSION_FILE):
        try:
            text = Path(CURRENT_VERSION_FILE).read_text().strip()
            if text:
                return text
        except OSError:  # pragma: no cover - unreadable file
            pass
    from app.version import __version__

    return __version__


def _parse_semver(version: str) -> Tuple[int, int, int]:
    """Parse a semver string like 'v1.2.3' or '1.2.3' into (major, minor, patch)."""
    v = version.lstrip("vV").strip()
    parts = v.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2].split("-")[0]) if len(parts) > 2 else 0
        return (major, minor, patch)
    except (ValueError, IndexError):
        return (0, 0, 0)


def _is_newer(remote: str, local: str) -> bool:
    """Return True if remote version is newer than local."""
    return _parse_semver(remote) > _parse_semver(local)


class _DeploymentStatus:
    """Cached answer to "is the running container the current build?".

    Deliberately three-valued. ``state`` is ``ok`` (checked, comparison
    valid), ``error`` (the check failed) or ``unknown`` (never checked / the
    last success has gone stale). ``stale`` is only ever True on a real
    comparison — a failed check reports ``unknown``, never "up to date".
    """

    def __init__(self) -> None:
        self.state: str = "unknown"
        self.latest_version: Optional[str] = None
        self.error: Optional[str] = None
        self.checked_at: Optional[str] = None
        self.checked_monotonic: float = 0.0

    def record_success(self, latest: Optional[str]) -> None:
        self.state = "ok"
        self.latest_version = latest
        self.error = None
        self.checked_at = datetime.now(timezone.utc).isoformat()
        self.checked_monotonic = time.monotonic()

    def record_error(self, error: str) -> None:
        self.state = "error"
        self.error = error
        self.checked_at = datetime.now(timezone.utc).isoformat()
        self.checked_monotonic = time.monotonic()

    def as_dict(self) -> Dict[str, Any]:
        current = _get_current_version()
        state = self.state
        if state == "ok" and self.checked_monotonic and (
            time.monotonic() - self.checked_monotonic > DEPLOYMENT_STALE_CHECK_SECONDS
        ):
            state = "unknown"

        stale: Optional[bool] = None
        behind_by: Optional[str] = None
        comparison: Optional[str] = None
        if state == "ok":
            if self.latest_version:
                stale = _is_newer(self.latest_version, current)
                if stale:
                    behind_by = f"{current} -> {self.latest_version}"
                comparison = "compared against the latest GitHub release"
            else:
                # A repo with no published releases is a KNOWN answer with
                # nothing to compare against — not an unknown one.
                stale = False
                comparison = "no releases published; nothing to compare against"

        from app.version import build_info

        return {
            "current_version": current,
            "latest_version": self.latest_version if state == "ok" else None,
            "state": state,
            "stale": stale,
            "behind_by": behind_by,
            "comparison": comparison,
            "error": self.error if state == "error" else None,
            "checked_at": self.checked_at,
            "build": build_info(),
            # Healthy means: we KNOW the running build is current. Unknown is
            # not healthy — that assumption is what let a July image run into
            # the middle of August unnoticed.
            "healthy": state == "ok" and stale is False,
        }


deployment_status = _DeploymentStatus()


def _scrub(text: str) -> str:
    """Never let a credential reach a log line, an error field, or the API.

    Error strings are rendered into ``/api/health`` and into the activity log,
    both of which are read by humans and shipped around. httpx does not put
    request headers in its messages today, but "today" is not a guarantee worth
    betting a token on.
    """
    token = settings.GITHUB_TOKEN
    if token and token in text:
        text = text.replace(token, "***")
    return text


async def _token_can_see_repo(client: httpx.AsyncClient, repo: str,
                              headers: Dict[str, str]) -> Tuple[bool, str]:
    """Does the configured credential actually have this repo in view?

    ``GET /repos/{owner}/{repo}`` answers 404 — not 403 — for a private repo the
    caller cannot read, which is the whole reason this probe exists.
    """
    resp = await client.get(f"{GITHUB_API_BASE}/repos/{repo}", headers=headers)
    if resp.status_code == 200:
        return True, ""
    if resp.status_code == 404:
        return False, (
            f"GITHUB_TOKEN authenticates but cannot see {repo} — a token "
            "without access to this private repo is indistinguishable from a "
            "repo with no releases; grant it read access to the repository"
        )
    if resp.status_code in (401, 403):
        return False, (
            f"GITHUB_TOKEN rejected on {repo} (HTTP {resp.status_code}) — "
            "expired, revoked, or missing SSO authorization"
        )
    return False, f"repo lookup for {repo} returned HTTP {resp.status_code}"


async def refresh_deployment_status() -> Dict[str, Any]:
    """Ask GitHub for the newest release and update the cached comparison."""
    repo = settings.GITHUB_REPO
    url = f"{GITHUB_API_BASE}/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = settings.GITHUB_TOKEN
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                if not token:
                    # Unauthenticated 404 is ambiguous — "no releases" and
                    # "private repo" look identical from here. Saying "up to
                    # date" on that evidence is exactly the false green this
                    # check exists to remove.
                    deployment_status.record_error(
                        "releases API returned 404 unauthenticated — cannot "
                        "tell 'no releases' from 'private repo'; set "
                        "GITHUB_TOKEN"
                    )
                else:
                    # Authenticated 404 is STILL ambiguous. GitHub hides
                    # private repos a credential cannot read behind 404 rather
                    # than 403, so "no releases published" and "this token has
                    # no access to this repo" arrive as the same response. The
                    # first reading marks the deployment healthy; the second is
                    # a check that has silently stopped checking. Only a repo
                    # the token can demonstrably see turns 404 into an answer.
                    visible, why = await _token_can_see_repo(
                        client, repo, headers
                    )
                    if visible:
                        deployment_status.record_success(None)
                    else:
                        deployment_status.record_error(why)
            elif resp.status_code in (401, 403):
                deployment_status.record_error(
                    f"releases API returned HTTP {resp.status_code} — "
                    "GITHUB_TOKEN is missing, expired, rejected, or rate "
                    "limited; the running build cannot be compared"
                )
            else:
                resp.raise_for_status()
                deployment_status.record_success(resp.json().get("tag_name"))
    except Exception as exc:
        logger.warning("Deployment version check failed: %s", _scrub(str(exc)))
        deployment_status.record_error(_scrub(str(exc)))

    snapshot = deployment_status.as_dict()
    if snapshot["stale"]:
        logger.warning(
            "STALE DEPLOYMENT: running %s, latest release %s",
            snapshot["current_version"], snapshot["latest_version"],
        )
        try:
            from app.services import activity_log

            await activity_log.warn(
                "deployment_stale",
                f"Running version {snapshot['current_version']} but "
                f"{snapshot['latest_version']} has been released "
                f"(built {snapshot['build'].get('built_at') or 'unknown'}).",
                context={k: v for k, v in snapshot.items() if k != "build"},
            )
        except Exception:  # pragma: no cover - defensive
            pass
    return snapshot


async def deployment_watch_loop() -> None:
    """Background loop keeping the deployment freshness answer current."""
    while True:
        try:
            await refresh_deployment_status()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Deployment watch iteration failed")
        await asyncio.sleep(DEPLOYMENT_CHECK_INTERVAL_SECONDS)


class UpgradeService:
    """Handles version checking, backups, and upgrade orchestration."""

    def __init__(self) -> None:
        self._repo = settings.GITHUB_REPO
        self._check_interval_hours = settings.AUTO_UPGRADE_CHECK_INTERVAL_HOURS
        self._auto_enabled = settings.AUTO_UPGRADE_ENABLED
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start periodic upgrade checking."""
        if not self._auto_enabled:
            logger.info("Auto-upgrade disabled")
            return
        self._running = True
        self._task = asyncio.create_task(self._periodic_check())
        logger.info(
            "Upgrade service started: checking %s every %dh",
            self._repo, self._check_interval_hours,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Upgrade service stopped")

    async def _periodic_check(self) -> None:
        while self._running:
            try:
                await self.check_and_upgrade()
            except Exception:
                logger.exception("Upgrade check failed")
            await asyncio.sleep(self._check_interval_hours * 3600)

    # ------------------------------------------------------------------
    # Version check
    # ------------------------------------------------------------------

    async def check_for_update(self) -> Optional[Dict[str, Any]]:
        """Check GitHub releases for a newer version.

        Returns release info dict if update available, None otherwise.
        """
        url = f"{GITHUB_API_BASE}/repos/{self._repo}/releases/latest"
        # Same credential as the freshness check. Without it a private repo
        # answers 404 to every query, this returns None forever, and the
        # auto-upgrade loop quietly never upgrades anything.
        headers = {"Accept": "application/vnd.github.v3+json"}
        if settings.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                logger.debug("No releases visible for %s", self._repo)
                return None
            resp.raise_for_status()
            release = resp.json()

        remote_version = release.get("tag_name", "0.0.0")
        local_version = _get_current_version()

        if _is_newer(remote_version, local_version):
            logger.info(
                "Update available: %s -> %s",
                local_version, remote_version,
            )
            return {
                "current_version": local_version,
                "latest_version": remote_version,
                "release_name": release.get("name", ""),
                "release_notes": release.get("body", ""),
                "html_url": release.get("html_url", ""),
                "published_at": release.get("published_at", ""),
            }

        logger.debug("Already up to date: %s", local_version)
        return None

    async def check_and_upgrade(self) -> bool:
        """Check for update and perform upgrade if available."""
        update_info = await self.check_for_update()
        if not update_info:
            return False

        logger.info("Starting upgrade to %s", update_info["latest_version"])

        # Backup first
        backup_path = await self.backup_settings()
        logger.info("Settings backed up to %s", backup_path)

        # Perform upgrade
        success = await self._perform_upgrade(update_info["latest_version"])
        if success:
            logger.info("Upgrade to %s completed, signaling restart", update_info["latest_version"])
            await self._signal_restart()
        else:
            logger.error("Upgrade failed")

        return success

    # ------------------------------------------------------------------
    # Upgrade execution
    # ------------------------------------------------------------------

    async def _perform_upgrade(self, version: str) -> bool:
        """Pull latest Docker image or git pull."""
        # Try Docker pull first
        docker_image = f"ghcr.io/{self._repo}:{version}"
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["docker", "pull", docker_image],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                logger.info("Docker image pulled: %s", docker_image)
                return True
            logger.debug("Docker pull failed: %s", result.stderr)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("Docker not available or timed out")

        # Fall back to git pull
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "pull", "origin", "main"],
                capture_output=True, text=True, timeout=60,
                cwd="/app" if os.path.isdir("/app/.git") else ".",
            )
            if result.returncode == 0:
                logger.info("Git pull completed: %s", result.stdout.strip())
                return True
            logger.error("Git pull failed: %s", result.stderr)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.error("Git pull error: %s", exc)

        return False

    async def _signal_restart(self) -> None:
        """Signal for container/process restart."""
        # Write a restart flag that a supervisor can watch
        restart_flag = "/data/.restart-requested"
        Path(restart_flag).write_text(
            datetime.now(timezone.utc).isoformat()
        )
        logger.info("Restart flag written to %s", restart_flag)

        # Also try sending SIGHUP to PID 1 (works in Docker)
        try:
            os.kill(1, 1)  # SIGHUP
        except (OSError, PermissionError):
            pass

    # ------------------------------------------------------------------
    # Backup / Restore
    # ------------------------------------------------------------------

    async def backup_settings(self) -> str:
        """Export database state to a JSON backup file."""
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"fadeout_backup_{timestamp}.json")

        backup_data: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": _get_current_version(),
            "mixes": [],
            "app_settings": None,
            "brand_settings": None,
        }

        async with async_session_factory() as session:
            # Export mixes
            from sqlalchemy import select
            result = await session.execute(select(Mix))
            mixes = result.scalars().all()
            for mix in mixes:
                backup_data["mixes"].append({
                    "id": mix.id,
                    "title": mix.title,
                    "audio_file_path": mix.audio_file_path,
                    "video_file_path": mix.video_file_path,
                    "duration_seconds": mix.duration_seconds,
                    "genres": mix.genres,
                    "vibes": mix.vibes,
                    "tracklist": mix.tracklist,
                    "description_soundcloud": mix.description_soundcloud,
                    "description_youtube": mix.description_youtube,
                    "title_youtube": mix.title_youtube,
                    "tags": mix.tags,
                    "cover_art_path": mix.cover_art_path,
                    "thumbnail_path": mix.thumbnail_path,
                    "soundcloud_url": mix.soundcloud_url,
                    "youtube_url": mix.youtube_url,
                    "pipeline_status": mix.pipeline_status,
                    "metadata_json": mix.metadata_json,
                })

            # Export app settings
            app_settings = await session.get(AppSettings, 1)
            if app_settings:
                backup_data["app_settings"] = {
                    "image_gen_provider": app_settings.image_gen_provider,
                    "image_gen_model": app_settings.image_gen_model,
                    "llm_provider": app_settings.llm_provider,
                    "llm_model": app_settings.llm_model,
                    "premiere_mode": app_settings.premiere_mode,
                    "premiere_hour_utc": app_settings.premiere_hour_utc,
                    "premiere_day": app_settings.premiere_day,
                    "draft_mode": app_settings.draft_mode,
                    "auto_upgrade": app_settings.auto_upgrade,
                    "settings_json": app_settings.settings_json,
                }

            # Export brand settings
            brand_settings = await session.get(BrandSettings, 1)
            if brand_settings:
                backup_data["brand_settings"] = {
                    "brand_name": brand_settings.brand_name,
                    "description_template": brand_settings.description_template,
                    "color_palette": brand_settings.color_palette,
                    "visual_style": brand_settings.visual_style,
                    "motifs": brand_settings.motifs,
                    "genre_visual_modifiers": brand_settings.genre_visual_modifiers,
                    "title_format": brand_settings.title_format,
                    "youtube_playlists": brand_settings.youtube_playlists,
                    "soundcloud_links": brand_settings.soundcloud_links,
                    "youtube_links": brand_settings.youtube_links,
                }

        with open(backup_path, "w") as f:
            json.dump(backup_data, f, indent=2, default=str)

        logger.info("Backup saved: %s (%d mixes)", backup_path, len(backup_data["mixes"]))

        # Prune old backups (keep last 10)
        self._prune_backups()

        return backup_path

    async def restore_from_backup(self, backup_path: str) -> bool:
        """Restore settings from a backup JSON file."""
        if not os.path.exists(backup_path):
            logger.error("Backup file not found: %s", backup_path)
            return False

        with open(backup_path, "r") as f:
            backup_data = json.load(f)

        async with async_session_factory() as session:
            # Restore app settings
            if backup_data.get("app_settings"):
                app_settings = await session.get(AppSettings, 1)
                if not app_settings:
                    app_settings = AppSettings(id=1)
                    session.add(app_settings)
                for key, value in backup_data["app_settings"].items():
                    if hasattr(app_settings, key):
                        setattr(app_settings, key, value)

            # Restore brand settings
            if backup_data.get("brand_settings"):
                brand_settings = await session.get(BrandSettings, 1)
                if not brand_settings:
                    brand_settings = BrandSettings(id=1)
                    session.add(brand_settings)
                for key, value in backup_data["brand_settings"].items():
                    if hasattr(brand_settings, key):
                        setattr(brand_settings, key, value)

            await session.commit()

        logger.info("Settings restored from %s", backup_path)
        return True

    def _prune_backups(self, keep: int = 10) -> None:
        """Remove old backup files, keeping the most recent N."""
        if not os.path.isdir(BACKUP_DIR):
            return
        backups = sorted(
            [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if f.endswith(".json")],
            key=os.path.getmtime,
            reverse=True,
        )
        for old in backups[keep:]:
            try:
                os.remove(old)
                logger.debug("Pruned old backup: %s", old)
            except OSError:
                pass

    def list_backups(self) -> List[Dict[str, Any]]:
        """List available backups."""
        if not os.path.isdir(BACKUP_DIR):
            return []
        backups = []
        for fname in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(BACKUP_DIR, fname)
            try:
                stat = os.stat(fpath)
                backups.append({
                    "filename": fname,
                    "path": fpath,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                })
            except OSError:
                continue
        return backups
