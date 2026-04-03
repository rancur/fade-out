"""Playwright-based SoundCloud uploader with persistent browser context."""

import asyncio
import logging
import os
from typing import List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from app.config import settings

logger = logging.getLogger(__name__)

BROWSER_PROFILE_DIR = "/data/playwright-profile"
SOUNDCLOUD_BASE = "https://soundcloud.com"
UPLOAD_URL = f"{SOUNDCLOUD_BASE}/upload"
LOGIN_URL = f"{SOUNDCLOUD_BASE}/signin"

# Stealth-ish defaults
VIEWPORT = {"width": 1920, "height": 1080}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

UPLOAD_TIMEOUT_MS = 600_000  # 10 minutes for large FLAC files
PROCESSING_TIMEOUT_MS = 900_000  # 15 minutes for server-side processing


class SoundCloudUploader:
    """Upload mixes to SoundCloud using browser automation."""

    def __init__(self) -> None:
        self._email = settings.SOUNDCLOUD_EMAIL
        self._password = settings.SOUNDCLOUD_PASSWORD
        self._playwright = None
        self._browser = None
        self._context: Optional[BrowserContext] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch browser with persistent context."""
        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            headless=True,
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/Phoenix",
            accept_downloads=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        logger.info("SoundCloud browser context started")

    async def stop(self) -> None:
        """Close browser."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("SoundCloud browser context stopped")

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def ensure_logged_in(self) -> None:
        """Check if logged in, attempt login if not."""
        page = await self._new_page()
        try:
            await page.goto(SOUNDCLOUD_BASE, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)

            # Check for user avatar or profile indicator
            logged_in = await page.query_selector('[aria-label="Your profile"]') is not None
            if not logged_in:
                logged_in = await page.query_selector('.header__userNavButton') is not None

            if logged_in:
                logger.info("Already logged in to SoundCloud")
                return

            logger.info("Not logged in, performing login flow")
            await self._login(page)
        finally:
            await page.close()

    async def _login(self, page: Page) -> None:
        """Perform the SoundCloud login flow."""
        if not self._email or not self._password:
            raise RuntimeError("SoundCloud credentials not configured")

        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        # Click the "Sign in with email" or "Continue with email" button if present
        email_btn = await page.query_selector('button:has-text("email")')
        if email_btn:
            await email_btn.click()
            await page.wait_for_timeout(1000)

        # Fill email
        email_input = await page.wait_for_selector(
            'input[type="email"], input[name="email"], input[id="email"]',
            timeout=10_000,
        )
        await email_input.fill(self._email)
        await page.wait_for_timeout(500)

        # Fill password
        password_input = await page.wait_for_selector(
            'input[type="password"], input[name="password"]',
            timeout=10_000,
        )
        await password_input.fill(self._password)
        await page.wait_for_timeout(500)

        # Submit
        submit = await page.query_selector(
            'button[type="submit"], button:has-text("Sign in"), button:has-text("Log in")'
        )
        if submit:
            await submit.click()
        else:
            await password_input.press("Enter")

        # Wait for navigation to indicate successful login
        try:
            await page.wait_for_url(f"{SOUNDCLOUD_BASE}/**", timeout=15_000)
            logger.info("SoundCloud login successful")
        except Exception:
            # Check for error messages
            error_el = await page.query_selector('.formControl__validationMessage, .loginForm__error')
            error_text = await error_el.inner_text() if error_el else "unknown error"
            raise RuntimeError(f"SoundCloud login failed: {error_text}")

    # ------------------------------------------------------------------
    # Upload
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
        """Upload a track to SoundCloud. Returns the track URL."""
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        await self.ensure_logged_in()
        page = await self._new_page()

        try:
            return await self._do_upload(
                page, audio_path, title, description, genre, tags, cover_art_path,
            )
        except Exception:
            # Save debug screenshot
            try:
                await page.screenshot(path="/data/soundcloud-upload-error.png")
                logger.error("Error screenshot saved to /data/soundcloud-upload-error.png")
            except Exception:
                pass
            raise
        finally:
            await page.close()

    async def _do_upload(
        self,
        page: Page,
        audio_path: str,
        title: str,
        description: str,
        genre: str,
        tags: List[str],
        cover_art_path: Optional[str],
    ) -> str:
        # Navigate to upload page
        await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        # Upload file via the file input
        file_input = await page.wait_for_selector(
            'input[type="file"][accept*="audio"], input[type="file"]',
            timeout=15_000,
        )
        await file_input.set_input_files(audio_path)
        logger.info("Audio file selected for upload: %s", audio_path)

        # Wait for upload to begin (progress or form fields to appear)
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
        logger.info("Title set: %s", title)

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
            logger.info("Description filled (%d chars)", len(description))

        # Set genre from dropdown
        await self._set_genre(page, genre)

        # Set tags
        await self._set_tags(page, tags)

        # Upload cover art
        if cover_art_path and os.path.exists(cover_art_path):
            await self._upload_cover_art(page, cover_art_path)

        # Set visibility to Public
        await self._set_public(page)

        # Click Save
        save_btn = await page.query_selector(
            'button:has-text("Save"), button[type="submit"]:has-text("Save"), '
            'button:has-text("Publish")'
        )
        if save_btn:
            await save_btn.click()
            logger.info("Save/Publish button clicked")
        else:
            logger.warning("Could not find Save button, attempting form submit")
            await page.keyboard.press("Enter")

        # Wait for processing/redirect
        track_url = await self._wait_for_track_url(page)
        logger.info("Upload complete. Track URL: %s", track_url)
        return track_url

    async def _set_genre(self, page: Page, genre: str) -> None:
        """Select genre from dropdown."""
        genre_select = await page.query_selector(
            'select[name="genre"], [class*="genreSelect"], [class*="genre"] select'
        )
        if genre_select:
            try:
                await genre_select.select_option(label=genre)
                logger.info("Genre set: %s", genre)
                return
            except Exception:
                pass

        # Try clicking a genre dropdown button
        genre_btn = await page.query_selector(
            'button[class*="genre"], [class*="genreDropdown"], '
            '[data-testid*="genre"]'
        )
        if genre_btn:
            await genre_btn.click()
            await page.wait_for_timeout(500)
            option = await page.query_selector(f'li:has-text("{genre}"), [role="option"]:has-text("{genre}")')
            if option:
                await option.click()
                logger.info("Genre selected from dropdown: %s", genre)
                return

        logger.warning("Could not set genre: %s", genre)

    async def _set_tags(self, page: Page, tags: List[str]) -> None:
        """Enter tags into the tag input."""
        tag_input = await page.query_selector(
            'input[name="tag_list"], input[class*="tagInput"], '
            'input[placeholder*="tag"], input[placeholder*="Tag"]'
        )
        if not tag_input:
            logger.warning("Tag input not found")
            return

        # SoundCloud expects space-separated tags, multi-word in quotes
        formatted_tags: List[str] = []
        for tag in tags[:30]:  # max 30
            if " " in tag:
                formatted_tags.append(f'"{tag}"')
            else:
                formatted_tags.append(tag)

        tag_string = " ".join(formatted_tags)
        await tag_input.fill(tag_string)
        await page.wait_for_timeout(300)
        await tag_input.press("Enter")
        logger.info("Tags set: %d tags", len(tags[:30]))

    async def _upload_cover_art(self, page: Page, cover_art_path: str) -> None:
        """Upload cover art image."""
        # Look for the artwork upload area
        art_input = await page.query_selector(
            'input[type="file"][accept*="image"], '
            '[class*="artwork"] input[type="file"], '
            '[class*="coverArt"] input[type="file"]'
        )
        if art_input:
            await art_input.set_input_files(cover_art_path)
            logger.info("Cover art uploaded: %s", cover_art_path)
            await page.wait_for_timeout(2000)
        else:
            # Try clicking an artwork button to reveal the input
            art_btn = await page.query_selector(
                'button[class*="artwork"], [class*="artwork"] button, '
                '[class*="coverArt"], [data-testid*="artwork"]'
            )
            if art_btn:
                await art_btn.click()
                await page.wait_for_timeout(1000)
                art_input = await page.query_selector('input[type="file"][accept*="image"]')
                if art_input:
                    await art_input.set_input_files(cover_art_path)
                    logger.info("Cover art uploaded via button: %s", cover_art_path)
                    await page.wait_for_timeout(2000)
                    return
            logger.warning("Could not find cover art upload input")

    async def _set_public(self, page: Page) -> None:
        """Set the track visibility to Public."""
        # Try radio button
        public_radio = await page.query_selector(
            'input[type="radio"][value="public"], '
            'label:has-text("Public") input[type="radio"]'
        )
        if public_radio:
            await public_radio.check()
            logger.info("Visibility set to Public (radio)")
            return

        # Try label click
        public_label = await page.query_selector(
            'label:has-text("Public"), [class*="privacy"] label:has-text("Public")'
        )
        if public_label:
            await public_label.click()
            logger.info("Visibility set to Public (label)")
            return

        logger.debug("No explicit privacy toggle found, assuming default is fine")

    async def _wait_for_track_url(self, page: Page) -> str:
        """Wait for upload to finish and extract the track URL."""
        # Wait for redirect to the track page or success message
        try:
            await page.wait_for_url(
                f"{SOUNDCLOUD_BASE}/*/**",
                timeout=PROCESSING_TIMEOUT_MS,
            )
            url = page.url
            if "/upload" not in url and SOUNDCLOUD_BASE in url:
                return url
        except Exception:
            pass

        # Alternative: look for a link to the uploaded track
        track_link = await page.query_selector(
            'a[href*="soundcloud.com/"][class*="trackTitle"], '
            'a[href*="soundcloud.com/"]:has-text("Go to your track"), '
            '[class*="success"] a[href*="soundcloud.com/"]'
        )
        if track_link:
            href = await track_link.get_attribute("href")
            if href:
                if href.startswith("/"):
                    return f"{SOUNDCLOUD_BASE}{href}"
                return href

        # Last resort: use current URL
        current = page.url
        if "/upload" not in current:
            return current

        raise RuntimeError("Could not determine uploaded track URL after processing")

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def verify_upload(self, track_url: str) -> bool:
        """Navigate to the track URL and verify it loads."""
        page = await self._new_page()
        try:
            resp = await page.goto(track_url, wait_until="domcontentloaded", timeout=30_000)
            if resp and resp.status == 200:
                # Check for the play button or waveform as indicators of a valid track
                play_btn = await page.query_selector(
                    'button[class*="play"], [class*="waveform"], [class*="soundPlayer"]'
                )
                if play_btn:
                    logger.info("SoundCloud upload verified: %s", track_url)
                    return True
                # Even without play button, 200 status is good
                logger.info("SoundCloud page loaded (200): %s", track_url)
                return True
            logger.warning("SoundCloud verification returned status %s for %s", resp.status if resp else "None", track_url)
            return False
        except Exception as exc:
            logger.error("SoundCloud verification failed for %s: %s", track_url, exc)
            return False
        finally:
            await page.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _new_page(self) -> Page:
        if not self._browser:
            await self.start()
        return await self._browser.new_page()
