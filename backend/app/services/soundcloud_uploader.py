"""SoundCloud uploader using the official API with Playwright fallback."""

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright

from app.config import settings

logger = logging.getLogger(__name__)

SOUNDCLOUD_API_BASE = "https://api.soundcloud.com"
SOUNDCLOUD_AUTH_URL = "https://api.soundcloud.com/oauth2/token"
SOUNDCLOUD_WEB_BASE = "https://soundcloud.com"

BROWSER_PROFILE_DIR = "/data/playwright-profile"
VIEWPORT = {"width": 1920, "height": 1080}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

UPLOAD_TIMEOUT = 3600  # large uploads on home upstream need well over 10 min

# api.soundcloud.com/tracks rejects large bodies with an empty 413 (observed
# live at 2.9GB despite the web uploader's documented 4GB cap). Anything over
# this gets transcoded to 320kbps MP3 first — SoundCloud re-encodes for
# streaming anyway, and downloads are disabled.
API_MAX_UPLOAD_BYTES = 450 * 1024 * 1024
TRANSCODE_BITRATE = "320k"


def format_bytes(n: float) -> str:
    """Human-readable byte count, e.g. 1.2 GB / 340 MB / 12 KB."""
    for unit, div in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= div:
            value = n / div
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
    return f"{int(n)} B"


def parse_ffmpeg_progress_line(line: str, duration_seconds: float) -> Optional[int]:
    """Parse one line of ffmpeg ``-progress pipe:1`` output into a percent.

    Returns the transcode percent (0-100) when the line carries
    ``out_time_ms=`` (microseconds despite the name), else None.
    """
    line = line.strip()
    if not line.startswith("out_time_ms=") or duration_seconds <= 0:
        return None
    try:
        out_us = int(line.split("=", 1)[1])
    except ValueError:
        return None
    if out_us < 0:
        return None
    return min(100, int(out_us / 1_000_000 / duration_seconds * 100))


class CountingReader:
    """File wrapper that counts bytes read and reports (bytes_sent, total).

    Used to derive live upload progress: httpx streams the multipart body by
    calling ``read()`` in chunks, so bytes read == bytes handed to the socket
    buffer. ``on_bytes`` is a SYNC callable — the caller bridges to async.
    Delegates everything else (fileno/seek/tell/...) to the underlying file so
    httpx can still stat it for Content-Length.
    """

    def __init__(self, fileobj, total: int, on_bytes: Optional[Callable[[int, int], None]] = None):
        self._f = fileobj
        self._total = total
        self._sent = 0
        self._on_bytes = on_bytes

    @property
    def bytes_sent(self) -> int:
        return self._sent

    def read(self, size: int = -1) -> bytes:
        data = self._f.read(size)
        if data:
            self._sent += len(data)
            if self._on_bytes:
                try:
                    self._on_bytes(self._sent, self._total)
                except Exception:  # pragma: no cover - progress must never break IO
                    pass
        return data

    def seek(self, offset: int, whence: int = 0):
        result = self._f.seek(offset, whence)
        if offset == 0 and whence == 0:
            self._sent = 0  # httpx may rewind and re-send (e.g. on redirect)
        return result

    def __getattr__(self, name):
        return getattr(self._f, name)


class SoundCloudUploader:
    """Upload mixes to SoundCloud via API (preferred) or browser automation (fallback)."""

    def __init__(
        self,
        db_settings_json: Optional[Dict[str, Any]] = None,
        on_tokens_refreshed: Optional[Any] = None,
        mix_id: Optional[str] = None,
    ) -> None:
        # on_tokens_refreshed: async callback (access_token, refresh_token) invoked
        # after a successful refresh/grant. SoundCloud ROTATES refresh tokens on
        # every use, so the new pair must be persisted or the next refresh gets
        # invalid_grant and the upload falls into the flaky browser path.
        sj = db_settings_json or {}
        self._on_tokens_refreshed = on_tokens_refreshed
        self._mix_id = mix_id  # for activity-log attribution (optional)
        self._client_id = sj.get("soundcloud_client_id") or settings.SOUNDCLOUD_CLIENT_ID
        self._client_secret = sj.get("soundcloud_client_secret") or settings.SOUNDCLOUD_CLIENT_SECRET
        self._access_token: Optional[str] = sj.get("soundcloud_access_token") or settings.SOUNDCLOUD_ACCESS_TOKEN
        self._refresh_token: Optional[str] = sj.get("soundcloud_refresh_token") or settings.SOUNDCLOUD_REFRESH_TOKEN
        self._email = settings.SOUNDCLOUD_EMAIL
        self._password = settings.SOUNDCLOUD_PASSWORD
        self._playwright = None
        self._browser = None

    async def _activity(self, level: str, event: str, message: str, **kwargs) -> None:
        """Best-effort activity-log emit — never breaks an upload."""
        try:
            from app.services import activity_log

            await activity_log.log(
                level, event, message, mix_id=self._mix_id, platform="soundcloud", **kwargs
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("activity emit failed for %s", event, exc_info=True)

    # ------------------------------------------------------------------
    # OAuth Token Management
    # ------------------------------------------------------------------

    async def _ensure_access_token(self) -> str:
        """Get a valid access token, refreshing or obtaining one if needed."""
        if self._access_token:
            # Test if token is still valid
            if await self._test_token(self._access_token):
                return self._access_token

        # Try refreshing with refresh_token
        if self._refresh_token:
            token = await self._refresh_access_token()
            if token:
                return token

        # Try client credentials + user password grant
        if self._email and self._password and self._client_id and self._client_secret:
            token = await self._password_grant()
            if token:
                return token

        raise RuntimeError(
            "No valid SoundCloud access token. Set SOUNDCLOUD_ACCESS_TOKEN "
            "or provide CLIENT_ID + CLIENT_SECRET + EMAIL + PASSWORD for OAuth."
        )

    async def _test_token(self, token: str) -> bool:
        """Test if an access token is valid."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{SOUNDCLOUD_API_BASE}/me",
                headers={"Authorization": f"OAuth {token}", "Accept": "application/json"},
            )
            return resp.status_code == 200

    async def _refresh_access_token(self) -> Optional[str]:
        """Refresh the access token using the refresh token."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                SOUNDCLOUD_AUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                self._access_token = data["access_token"]
                self._refresh_token = data.get("refresh_token", self._refresh_token)
                logger.info("SoundCloud access token refreshed")
                await self._persist_tokens()
                return self._access_token
            logger.warning("Token refresh failed: %s", resp.text)
            await self._activity(
                "warn", "sc_token_refresh_failed",
                f"SoundCloud token refresh failed ({resp.status_code})",
            )
            return None

    async def _persist_tokens(self) -> None:
        """Persist rotated tokens via the callback (best-effort)."""
        if not self._on_tokens_refreshed:
            await self._activity(
                "info", "sc_token_refreshed",
                "SoundCloud access token refreshed (no persister attached)",
            )
            return
        try:
            await self._on_tokens_refreshed(self._access_token, self._refresh_token)
            await self._activity(
                "info", "sc_token_refreshed",
                "SoundCloud access token refreshed and persisted (refresh token rotated)",
            )
        except Exception as exc:
            logger.error("Failed to persist refreshed SoundCloud tokens: %s", exc)
            await self._activity(
                "error", "sc_token_persist_failed",
                f"SoundCloud token refreshed but persisting the rotated pair failed: {exc}",
            )

    async def _password_grant(self) -> Optional[str]:
        """Obtain access token via password grant (resource owner credentials)."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                SOUNDCLOUD_AUTH_URL,
                data={
                    "grant_type": "password",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "username": self._email,
                    "password": self._password,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                self._access_token = data["access_token"]
                self._refresh_token = data.get("refresh_token")
                logger.info("SoundCloud access token obtained via password grant")
                await self._persist_tokens()
                return self._access_token
            logger.warning("Password grant failed: %s", resp.text)
            return None

    # ------------------------------------------------------------------
    # API Upload (Primary)
    # ------------------------------------------------------------------

    async def upload(
        self,
        audio_path: str,
        title: str,
        description: str,
        genre: str,
        tags: List[str],
        cover_art_path: Optional[str] = None,
        progress_cb: Optional[Callable] = None,
    ) -> str:
        """Upload a track to SoundCloud. Returns the track permalink URL.

        ``progress_cb`` is an optional ``async (percent, detail)`` callable —
        it receives transcode progress ("transcoding 42%") and then upload
        progress ("1.2 GB / 2.9 GB") while the multipart body streams out.
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        # Try API upload first
        if self._client_id and self._client_secret:
            try:
                return await self._api_upload(
                    audio_path, title, description, genre, tags, cover_art_path,
                    progress_cb=progress_cb,
                )
            except Exception as exc:
                logger.warning("API upload failed, falling back to browser: %s", exc)

        # Fall back to Playwright browser automation
        return await self._browser_upload(
            audio_path, title, description, genre, tags, cover_art_path,
        )

    async def _api_upload(
        self,
        audio_path: str,
        title: str,
        description: str,
        genre: str,
        tags: List[str],
        cover_art_path: Optional[str],
        progress_cb: Optional[Callable] = None,
    ) -> str:
        """Upload via the official SoundCloud API."""
        token = await self._ensure_access_token()

        transcoded: Optional[str] = None
        if os.path.getsize(audio_path) > API_MAX_UPLOAD_BYTES:
            transcoded = await self._transcode_for_api(audio_path, progress_cb=progress_cb)
            audio_path = transcoded
        try:
            return await self._api_upload_inner(
                audio_path, title, description, genre, tags, cover_art_path, token,
                progress_cb=progress_cb,
            )
        finally:
            if transcoded:
                try:
                    os.unlink(transcoded)
                except OSError:
                    pass

    @staticmethod
    def _source_duration_seconds(audio_path: str) -> float:
        """Best-effort duration of the source file (for transcode percent)."""
        try:
            from mutagen import File as MutagenFile

            mf = MutagenFile(audio_path)
            if mf is not None and mf.info is not None:
                return float(mf.info.length or 0.0)
        except Exception:  # pragma: no cover - defensive
            pass
        return 0.0

    async def _transcode_for_api(
        self, audio_path: str, progress_cb: Optional[Callable] = None
    ) -> str:
        """Transcode an oversized master to 320kbps MP3 for the API upload.

        Progress is parsed from ffmpeg ``-progress pipe:1`` output
        (out_time vs source duration) and reported as "transcoding N%".
        """
        import tempfile

        out = os.path.join(
            tempfile.gettempdir(),
            os.path.splitext(os.path.basename(audio_path))[0] + ".sc-upload.mp3",
        )
        source_size = os.path.getsize(audio_path)
        logger.info(
            "Audio too large for SoundCloud API (%.0f MB); transcoding to %s MP3",
            source_size / 1048576, TRANSCODE_BITRATE,
        )
        await self._activity(
            "info", "sc_transcode_started",
            f"Transcoding {os.path.basename(audio_path)} "
            f"({format_bytes(source_size)}) to {TRANSCODE_BITRATE} MP3 for the SoundCloud API",
            filename=os.path.basename(audio_path),
        )
        duration = self._source_duration_seconds(audio_path)

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", audio_path,
            "-codec:a", "libmp3lame", "-b:a", TRANSCODE_BITRATE,
            "-map_metadata", "0", "-id3v2_version", "3",
            "-nostats", "-progress", "pipe:1",
            out,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _drain_stderr() -> bytes:
            return await proc.stderr.read()

        stderr_task = asyncio.create_task(_drain_stderr())

        # Stream ffmpeg's key=value progress lines as they arrive.
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            pct = parse_ffmpeg_progress_line(line.decode(errors="replace"), duration)
            if pct is not None and progress_cb:
                try:
                    await progress_cb(pct, f"transcoding {pct}%")
                except Exception:  # pragma: no cover - progress must never break IO
                    pass

        await proc.wait()
        stderr = await stderr_task
        if proc.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(
                f"ffmpeg transcode failed (rc={proc.returncode}): "
                f"{(stderr or b'')[-400:].decode(errors='replace')}"
            )
        out_size = os.path.getsize(out)
        logger.info("Transcoded to %s (%.0f MB)", out, out_size / 1048576)
        await self._activity(
            "info", "sc_transcode_finished",
            f"Transcode finished: {format_bytes(source_size)} → {format_bytes(out_size)} MP3",
            filename=os.path.basename(out),
            context={"source_bytes": source_size, "output_bytes": out_size},
        )
        return out

    async def _api_upload_inner(
        self,
        audio_path: str,
        title: str,
        description: str,
        genre: str,
        tags: List[str],
        cover_art_path: Optional[str],
        token: str,
        progress_cb: Optional[Callable] = None,
    ) -> str:

        # Format tags: space-separated, multi-word tags in quotes
        formatted_tags = []
        for tag in tags[:30]:
            if " " in tag:
                formatted_tags.append(f'"{tag}"')
            else:
                formatted_tags.append(tag)
        tag_list = " ".join(formatted_tags)

        # Build multipart upload (oversized masters were already transcoded)
        file_size = os.path.getsize(audio_path)

        logger.info("Uploading to SoundCloud API: '%s' (%d MB)", title, file_size // (1024 * 1024))

        # Wrap the audio file in a counting reader so bytes-on-the-wire drive
        # live progress ("1.2 GB / 2.9 GB"). The reader's callback is sync;
        # bridge to the async progress_cb via a fire-and-forget task, locally
        # throttled to ~1/s (the orchestrator throttles again downstream).
        audio_file: Any = open(audio_path, "rb")
        if progress_cb is not None:
            loop = asyncio.get_running_loop()
            throttle = {"last": 0.0}

            def _on_bytes(sent: int, total: int) -> None:
                now = time.monotonic()
                if sent < total and (now - throttle["last"]) < 1.0:
                    return
                throttle["last"] = now
                pct = int(sent * 100 / total) if total else None
                detail = f"{format_bytes(sent)} / {format_bytes(total)}"
                try:
                    loop.create_task(progress_cb(pct, detail))
                except Exception:  # pragma: no cover - progress must never break IO
                    pass

            audio_file = CountingReader(audio_file, file_size, _on_bytes)

        async with httpx.AsyncClient(timeout=httpx.Timeout(UPLOAD_TIMEOUT, connect=30)) as client:
            # Prepare multipart files and data
            files: Dict[str, Any] = {
                "track[asset_data]": (
                    os.path.basename(audio_path),
                    audio_file,
                    "audio/flac" if audio_path.endswith(".flac") else "audio/mpeg",
                ),
            }

            if cover_art_path and os.path.exists(cover_art_path):
                files["track[artwork_data]"] = (
                    os.path.basename(cover_art_path),
                    open(cover_art_path, "rb"),
                    "image/jpeg" if cover_art_path.endswith(".jpg") else "image/png",
                )

            data = {
                "track[title]": title,
                "track[description]": description,
                "track[genre]": genre,
                "track[tag_list]": tag_list,
                "track[sharing]": "public",
                "track[downloadable]": "false",
                "track[license]": "all-rights-reserved",
            }

            resp = await client.post(
                f"{SOUNDCLOUD_API_BASE}/tracks",
                headers={
                    "Authorization": f"OAuth {token}",
                    "Accept": "application/json; charset=utf-8",
                },
                data=data,
                files=files,
            )

            # Close file handles
            for key, val in files.items():
                if hasattr(val[1], "close"):
                    val[1].close()

            if resp.status_code in (200, 201):
                track_data = resp.json()
                permalink = track_data.get("permalink_url", "")
                track_id = track_data.get("id", "")
                logger.info(
                    "SoundCloud API upload successful: id=%s url=%s",
                    track_id, permalink,
                )
                return permalink
            else:
                raise RuntimeError(
                    f"SoundCloud API upload failed ({resp.status_code}): {resp.text}"
                )

    # ------------------------------------------------------------------
    # Browser Upload (Fallback)
    # ------------------------------------------------------------------

    async def _browser_upload(
        self,
        audio_path: str,
        title: str,
        description: str,
        genre: str,
        tags: List[str],
        cover_art_path: Optional[str],
    ) -> str:
        """Upload via Playwright browser automation (fallback when API fails)."""
        await self._ensure_browser()
        await self._ensure_logged_in()
        page = await self._browser.new_page()

        try:
            return await self._do_browser_upload(
                page, audio_path, title, description, genre, tags, cover_art_path,
            )
        except Exception:
            try:
                await page.screenshot(path="/data/soundcloud-upload-error.png")
                logger.error("Error screenshot saved to /data/soundcloud-upload-error.png")
            except Exception:
                pass
            raise
        finally:
            await page.close()

    async def _ensure_browser(self) -> None:
        """Start browser if not running."""
        if self._browser:
            return
        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            headless=True,
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/Phoenix",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

    async def _ensure_logged_in(self) -> None:
        """Check if logged in via browser, attempt login if not."""
        page = await self._browser.new_page()
        try:
            await page.goto(SOUNDCLOUD_WEB_BASE, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)
            logged_in = await page.query_selector('[aria-label="Your profile"]') is not None
            if not logged_in:
                logged_in = await page.query_selector('.header__userNavButton') is not None
            if logged_in:
                logger.info("Already logged in to SoundCloud (browser)")
                return
            await self._browser_login(page)
        finally:
            await page.close()

    async def _browser_login(self, page: Page) -> None:
        """Perform browser login flow."""
        if not self._email or not self._password:
            raise RuntimeError("SoundCloud credentials not configured for browser login")

        await page.goto(f"{SOUNDCLOUD_WEB_BASE}/signin", wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        email_btn = await page.query_selector('button:has-text("email")')
        if email_btn:
            await email_btn.click()
            await page.wait_for_timeout(1000)

        email_input = await page.wait_for_selector(
            'input[type="email"], input[name="email"], input[id="email"]', timeout=10_000,
        )
        await email_input.fill(self._email)
        await page.wait_for_timeout(500)

        password_input = await page.wait_for_selector(
            'input[type="password"], input[name="password"]', timeout=10_000,
        )
        await password_input.fill(self._password)
        await page.wait_for_timeout(500)

        submit = await page.query_selector(
            'button[type="submit"], button:has-text("Sign in"), button:has-text("Log in")'
        )
        if submit:
            await submit.click()
        else:
            await password_input.press("Enter")

        try:
            await page.wait_for_url(f"{SOUNDCLOUD_WEB_BASE}/**", timeout=15_000)
            logger.info("SoundCloud browser login successful")
        except Exception:
            error_el = await page.query_selector('.formControl__validationMessage, .loginForm__error')
            error_text = await error_el.inner_text() if error_el else "unknown error"
            raise RuntimeError(f"SoundCloud browser login failed: {error_text}")

    async def _do_browser_upload(
        self,
        page: Page,
        audio_path: str,
        title: str,
        description: str,
        genre: str,
        tags: List[str],
        cover_art_path: Optional[str],
    ) -> str:
        """Execute the full browser upload flow."""
        upload_url = f"{SOUNDCLOUD_WEB_BASE}/upload"
        await page.goto(upload_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        # Upload file
        file_input = await page.wait_for_selector(
            'input[type="file"][accept*="audio"], input[type="file"]', timeout=15_000,
        )
        await file_input.set_input_files(audio_path)
        logger.info("Audio file selected: %s", audio_path)

        await page.wait_for_selector(
            '.uploadProgress, .soundForm, [class*="upload"], [class*="trackForm"]',
            timeout=30_000,
        )
        await page.wait_for_timeout(3000)

        # Fill title
        title_input = await page.wait_for_selector(
            'input[name="title"], input[id*="title"], input[class*="titleInput"]',
            timeout=15_000,
        )
        await title_input.fill("")
        await title_input.fill(title)

        # Fill description
        desc_input = await page.query_selector(
            'textarea[name="description"], textarea[id*="description"], '
            'textarea[class*="description"], [contenteditable][class*="description"]'
        )
        if desc_input:
            tag_name = await desc_input.evaluate("el => el.tagName")
            if tag_name.lower() == "textarea":
                await desc_input.fill(description)
            else:
                await desc_input.click()
                await page.keyboard.select_all()
                await page.keyboard.type(description)

        # Set genre
        genre_select = await page.query_selector(
            'select[name="genre"], [class*="genreSelect"], [class*="genre"] select'
        )
        if genre_select:
            try:
                await genre_select.select_option(label=genre)
            except Exception:
                pass

        # Set tags
        tag_input = await page.query_selector(
            'input[name="tag_list"], input[class*="tagInput"], '
            'input[placeholder*="tag"], input[placeholder*="Tag"]'
        )
        if tag_input:
            formatted = []
            for tag in tags[:30]:
                formatted.append(f'"{tag}"' if " " in tag else tag)
            await tag_input.fill(" ".join(formatted))
            await page.wait_for_timeout(300)
            await tag_input.press("Enter")

        # Upload cover art
        if cover_art_path and os.path.exists(cover_art_path):
            art_input = await page.query_selector(
                'input[type="file"][accept*="image"], '
                '[class*="artwork"] input[type="file"]'
            )
            if art_input:
                await art_input.set_input_files(cover_art_path)
                await page.wait_for_timeout(2000)

        # Set public
        public_radio = await page.query_selector(
            'input[type="radio"][value="public"], '
            'label:has-text("Public") input[type="radio"]'
        )
        if public_radio:
            await public_radio.check()

        # Save
        save_btn = await page.query_selector(
            'button:has-text("Save"), button[type="submit"]:has-text("Save"), '
            'button:has-text("Publish")'
        )
        if save_btn:
            await save_btn.click()
        else:
            await page.keyboard.press("Enter")

        # Wait for track URL
        try:
            await page.wait_for_url(
                f"{SOUNDCLOUD_WEB_BASE}/*/**", timeout=900_000,
            )
            url = page.url
            if "/upload" not in url:
                return url
        except Exception:
            pass

        track_link = await page.query_selector(
            'a[href*="soundcloud.com/"]:has-text("Go to your track"), '
            '[class*="success"] a'
        )
        if track_link:
            href = await track_link.get_attribute("href")
            if href:
                return href if href.startswith("http") else f"{SOUNDCLOUD_WEB_BASE}{href}"

        current = page.url
        if "/upload" not in current:
            return current

        raise RuntimeError("Could not determine uploaded track URL")

    # ------------------------------------------------------------------
    # Description update (cross-linking)
    # ------------------------------------------------------------------

    async def update_description(self, track_url: str, description: str) -> bool:
        """Update a track's description via ``PUT /tracks/:id`` (best-effort).

        Resolves the permalink URL to a track id, then PUTs the new description.
        Uses the existing OAuth access token -- no new credentials required.
        """
        token = await self._ensure_access_token()

        async with httpx.AsyncClient(timeout=30) as client:
            resolved = await client.get(
                f"{SOUNDCLOUD_API_BASE}/resolve",
                params={"url": track_url},
                headers={"Authorization": f"OAuth {token}", "Accept": "application/json"},
                follow_redirects=True,
            )
            if resolved.status_code != 200:
                raise RuntimeError(
                    f"SoundCloud resolve failed ({resolved.status_code}) for {track_url}"
                )
            track_id = resolved.json().get("id")
            if not track_id:
                raise RuntimeError(f"SoundCloud resolve returned no track id for {track_url}")

            put = await client.put(
                f"{SOUNDCLOUD_API_BASE}/tracks/{track_id}",
                headers={"Authorization": f"OAuth {token}", "Accept": "application/json"},
                data={"track[description]": description},
            )
            if put.status_code not in (200, 201):
                raise RuntimeError(
                    f"SoundCloud description update failed ({put.status_code}): {put.text}"
                )

        logger.info("Updated SoundCloud description for track %s (%s)", track_id, track_url)
        await self._activity(
            "info", "description_updated",
            f"SoundCloud description updated: {track_url}",
            context={"track_id": track_id},
        )
        return True

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def verify_upload(self, track_url: str) -> bool:
        """Verify an upload exists and is accessible."""
        # Try API verification first
        if self._access_token:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    # Resolve the track URL to get track data
                    resp = await client.get(
                        f"{SOUNDCLOUD_API_BASE}/resolve",
                        params={"url": track_url},
                        headers={
                            "Authorization": f"OAuth {self._access_token}",
                            "Accept": "application/json",
                        },
                        follow_redirects=True,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("kind") == "track":
                            logger.info("SoundCloud upload verified via API: %s", track_url)
                            return True
            except Exception as exc:
                logger.debug("API verification failed, trying HTTP: %s", exc)

        # Fallback: HTTP HEAD/GET check
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(track_url)
            if resp.status_code == 200:
                logger.info("SoundCloud upload verified via HTTP: %s", track_url)
                return True

        logger.warning("SoundCloud verification failed for %s", track_url)
        return False

    # ==================================================================
    # Catalog listing / editing APIs (Stage C — back-catalog management)
    # ==================================================================
    # Additive section: enumerate the authed user's full track list and push
    # targeted edits to already-published tracks. Raw API resources are
    # returned as-is; normalization lives in services/catalog_sync.py.

    async def list_all_tracks(self) -> List[Dict[str, Any]]:
        """Return every track owned by the authed user (raw track resources).

        GET /me/tracks with linked_partitioning=1&limit=200, following
        ``next_href`` until exhausted.
        """
        token = await self._ensure_access_token()
        tracks: List[Dict[str, Any]] = []
        url: Optional[str] = f"{SOUNDCLOUD_API_BASE}/me/tracks"
        params: Optional[Dict[str, Any]] = {"linked_partitioning": 1, "limit": 200}

        async with httpx.AsyncClient(timeout=60) as client:
            while url:
                resp = await client.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"OAuth {token}",
                        "Accept": "application/json",
                    },
                )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"SoundCloud track listing failed ({resp.status_code}): {resp.text}"
                    )
                data = resp.json()
                if isinstance(data, dict):
                    tracks.extend(data.get("collection", []))
                    url = data.get("next_href")
                else:  # non-partitioned plain-list response
                    tracks.extend(data)
                    url = None
                params = None  # next_href already carries the query string
        return tracks

    async def update_track_fields(
        self,
        track_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        artwork_path: Optional[str] = None,
    ) -> None:
        """Update only the given fields via ``PUT /tracks/:id``.

        Tags are formatted the same way as at upload time (space-separated,
        multi-word tags quoted). Artwork goes up as ``track[artwork_data]``
        multipart. Raises on any failure — the apply worker records the error.
        """
        token = await self._ensure_access_token()

        data: Dict[str, Any] = {}
        if title is not None:
            data["track[title]"] = title
        if description is not None:
            data["track[description]"] = description
        if tags is not None:
            formatted = [f'"{t}"' if " " in t else t for t in tags[:30]]
            data["track[tag_list]"] = " ".join(formatted)

        files: Dict[str, Any] = {}
        if artwork_path:
            if not os.path.exists(artwork_path):
                raise FileNotFoundError(f"Artwork not found: {artwork_path}")
            files["track[artwork_data]"] = (
                os.path.basename(artwork_path),
                open(artwork_path, "rb"),
                "image/jpeg" if artwork_path.endswith(".jpg") else "image/png",
            )

        if not data and not files:
            return

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.put(
                    f"{SOUNDCLOUD_API_BASE}/tracks/{track_id}",
                    headers={
                        "Authorization": f"OAuth {token}",
                        "Accept": "application/json",
                    },
                    data=data,
                    files=files or None,
                )
                if resp.status_code not in (200, 201):
                    raise RuntimeError(
                        f"SoundCloud track update failed ({resp.status_code}): {resp.text}"
                    )
        finally:
            for _, val in files.items():
                if hasattr(val[1], "close"):
                    val[1].close()
        logger.info("Updated SoundCloud track %s fields: %s", track_id, sorted(data))

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Close browser if running."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("SoundCloud uploader stopped")
