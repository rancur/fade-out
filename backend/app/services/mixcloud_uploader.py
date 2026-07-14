"""Mixcloud uploader using the official Mixcloud API.

Mirrors the SoundCloud/YouTube uploader shape (``upload`` / ``verify_upload`` /
``update_description``) so the pipeline can treat all three platforms the same
way. The whole path is gated behind ``MIXCLOUD_ENABLED`` upstream in the
handler -- this class only performs work when it is given a real access token.

Mixcloud's upload API (https://www.mixcloud.com/developers/#uploading) accepts a
multipart ``POST /upload/`` with an ``mp3`` file part plus ``name``,
``description``, ``tags-N-tag`` and an optional ``picture`` part, authenticated
by an OAuth ``access_token`` query parameter. Editing an existing cloudcast uses
``POST /<user>/<slug>/edit/`` with the same field shape.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MIXCLOUD_API_BASE = "https://api.mixcloud.com"
MIXCLOUD_UPLOAD_URL = f"{MIXCLOUD_API_BASE}/upload/"
MIXCLOUD_WEB_BASE = "https://www.mixcloud.com"

UPLOAD_TIMEOUT = 600  # 10 minutes for large audio files
# Mixcloud rejects lossless/oversize audio; it expects mp3/m4a up to ~4GB but in
# practice we cap defensively and let the API surface its own limit otherwise.
MAX_TAGS = 5  # Mixcloud only stores the first 5 tags


class MixcloudUploader:
    """Upload mixes to Mixcloud via the official API (OAuth access token)."""

    def __init__(self, db_settings_json: Optional[Dict[str, Any]] = None) -> None:
        sj = db_settings_json or {}
        self._access_token: str = (
            sj.get("mixcloud_access_token") or settings.MIXCLOUD_ACCESS_TOKEN
        )
        self._client_id = sj.get("mixcloud_client_id") or settings.MIXCLOUD_CLIENT_ID
        self._client_secret = (
            sj.get("mixcloud_client_secret") or settings.MIXCLOUD_CLIENT_SECRET
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_token(self) -> str:
        if not self._access_token:
            raise RuntimeError(
                "No Mixcloud access token. Set MIXCLOUD_ACCESS_TOKEN (or the "
                "mixcloud_access_token app setting) after completing the OAuth flow."
            )
        return self._access_token

    @staticmethod
    def _tag_fields(tags: List[str]) -> List[tuple]:
        """Build Mixcloud's positional ``tags-N-tag`` multipart fields."""
        fields: List[tuple] = []
        for i, tag in enumerate(tags[:MAX_TAGS]):
            tag = (tag or "").strip()
            if tag:
                fields.append((f"tags-{i}-tag", (None, tag)))
        return fields

    @staticmethod
    def _key_from_result(data: Dict[str, Any]) -> Optional[str]:
        """Pull the cloudcast key (``/user/slug/``) out of an upload response."""
        result = data.get("result", {})
        # Newer responses expose the created object under "result" with a "key".
        key = result.get("key") or data.get("key")
        return key

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def upload(
        self,
        audio_path: str,
        title: str,
        description: str,
        tags: List[str],
        cover_art_path: Optional[str] = None,
    ) -> str:
        """Upload a cloudcast to Mixcloud. Returns the public cloudcast URL."""
        token = self._require_token()

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        mime = "audio/mpeg"
        if audio_path.endswith(".m4a"):
            mime = "audio/mp4"
        elif audio_path.endswith(".flac"):
            # Mixcloud does not accept FLAC; surface a clear error rather than a
            # confusing API 4xx so the pipeline logs are actionable.
            raise ValueError(
                "Mixcloud does not accept FLAC uploads; provide an mp3/m4a render."
            )

        logger.info("Uploading to Mixcloud API: '%s'", title)

        files: List[tuple] = [
            ("mp3", (os.path.basename(audio_path), open(audio_path, "rb"), mime)),
            ("name", (None, title)),
            ("description", (None, description or "")),
        ]
        files.extend(self._tag_fields(tags))

        if cover_art_path and os.path.exists(cover_art_path):
            pic_mime = "image/png" if cover_art_path.endswith(".png") else "image/jpeg"
            files.append(
                ("picture", (os.path.basename(cover_art_path), open(cover_art_path, "rb"), pic_mime))
            )

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(UPLOAD_TIMEOUT, connect=30)
            ) as client:
                resp = await client.post(
                    MIXCLOUD_UPLOAD_URL,
                    params={"access_token": token},
                    files=files,
                )
        finally:
            for _name, tup in files:
                handle = tup[1]
                if hasattr(handle, "close"):
                    try:
                        handle.close()
                    except Exception:
                        pass

        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Mixcloud upload failed ({resp.status_code}): {resp.text[:500]}"
            )

        data = resp.json()
        key = self._key_from_result(data)
        if key:
            url = f"{MIXCLOUD_WEB_BASE}{key}"
            logger.info("Mixcloud upload successful: %s", url)
            return url

        logger.info("Mixcloud upload succeeded but returned no key: %s", data)
        return MIXCLOUD_WEB_BASE

    # ------------------------------------------------------------------
    # Description update (cross-linking)
    # ------------------------------------------------------------------

    async def update_description(self, cloudcast_url: str, description: str) -> bool:
        """Update a cloudcast's description via its ``/edit/`` endpoint (best-effort)."""
        token = self._require_token()
        key = self._key_from_url(cloudcast_url)
        if not key:
            raise RuntimeError(f"Could not derive cloudcast key from {cloudcast_url}")

        edit_url = f"{MIXCLOUD_API_BASE}{key}edit/"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                edit_url,
                params={"access_token": token},
                data={"description": description},
            )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Mixcloud description update failed ({resp.status_code}): {resp.text[:300]}"
            )
        logger.info("Updated Mixcloud description for %s", cloudcast_url)
        return True

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def verify_upload(self, cloudcast_url: str) -> bool:
        """Verify a cloudcast exists and is retrievable via the public API."""
        key = self._key_from_url(cloudcast_url)
        if not key:
            logger.warning("Mixcloud verify: could not parse key from %s", cloudcast_url)
            return False

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(f"{MIXCLOUD_API_BASE}{key}")
            if resp.status_code == 200:
                logger.info("Mixcloud upload verified via API: %s", cloudcast_url)
                return True

        logger.warning("Mixcloud verification failed for %s", cloudcast_url)
        return False

    @staticmethod
    def _key_from_url(cloudcast_url: str) -> Optional[str]:
        """Turn a public/API cloudcast URL into an API key path (``/user/slug/``)."""
        if not cloudcast_url:
            return None
        url = cloudcast_url.strip()
        for prefix in (MIXCLOUD_WEB_BASE, MIXCLOUD_API_BASE, "https://mixcloud.com"):
            if url.startswith(prefix):
                url = url[len(prefix):]
                break
        if not url.startswith("/"):
            url = "/" + url
        if not url.endswith("/"):
            url += "/"
        return url
