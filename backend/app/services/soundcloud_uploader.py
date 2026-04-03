"""SoundCloud uploader using the official API with Playwright fallback."""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

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

UPLOAD_TIMEOUT = 600  # 10 minutes for large FLAC files


class SoundCloudUploader:
    """Upload mixes to SoundCloud via API (preferred) or browser automation (fallback)."""

    def __init__(self) -> None:
        self._client_id = settings.SOUNDCLOUD_CLIENT_ID
        self._client_secret = settings.SOUNDCLOUD_CLIENT_SECRET
        self._access_token: Optional[str] = settings.SOUNDCLOUD_ACCESS_TOKEN
        self._refresh_token: Optional[str] = settings.SOUNDCLOUD_REFRESH_TOKEN
        self._email = settings.SOUNDCLOUD_EMAIL
        self._password = settings.SOUNDCLOUD_PASSWORD
        self._playwright = None
        self._browser = None

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
                return self._access_token
            logger.warning("Token refresh failed: %s", resp.text)
            return None

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
    ) -> str:
        """Upload a track to SoundCloud. Returns the track permalink URL."""
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        # Try API upload first
        if self._client_id and self._client_secret:
            try:
                return await self._api_upload(
                    audio_path, title, description, genre, tags, cover_art_path,
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
    ) -> str:
        """Upload via the official SoundCloud API."""
        token = await self._ensure_access_token()

        # Format tags: space-separated, multi-word tags in quotes
        formatted_tags = []
        for tag in tags[:30]:
            if " " in tag:
                formatted_tags.append(f'"{tag}"')
            else:
                formatted_tags.append(tag)
        tag_list = " ".join(formatted_tags)

        # Build multipart upload
        file_size = os.path.getsize(audio_path)
        if file_size > 500 * 1024 * 1024:  # 500MB API limit
            raise ValueError(f"File too large for API upload ({file_size / 1024 / 1024:.0f}MB > 500MB)")

        logger.info("Uploading to SoundCloud API: '%s' (%d MB)", title, file_size // (1024 * 1024))

        async with httpx.AsyncClient(timeout=httpx.Timeout(UPLOAD_TIMEOUT, connect=30)) as client:
            # Prepare multipart files and data
            files: Dict[str, Any] = {
                "track[asset_data]": (
                    os.path.basename(audio_path),
                    open(audio_path, "rb"),
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
