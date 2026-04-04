"""YouTube Data API v3 uploader with OAuth2, premiere scheduling, and playlist management."""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.config import settings

logger = logging.getLogger(__name__)

# Arizona/Phoenix is UTC-7 year-round (no DST)
PHOENIX_UTC_OFFSET = timedelta(hours=-7)
CATEGORY_MUSIC = "10"

# Preferred premiere slots (Phoenix local time)
PREMIERE_SLOTS = [
    ("friday", 16),    # Fri 4 PM Phoenix = 7 PM EST
    ("saturday", 10),  # Sat 10 AM Phoenix
    ("thursday", 17),  # Thu 5 PM Phoenix
]

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]


class YouTubeUploader:
    """Upload videos to YouTube via the Data API v3."""

    def __init__(self, db_settings_json: Optional[Dict] = None) -> None:
        sj = db_settings_json or {}
        self._db_settings = sj
        self._credentials: Optional[Credentials] = None
        self._youtube = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_credentials(self) -> Credentials:
        """Build OAuth2 credentials from refresh token."""
        if self._credentials and self._credentials.valid:
            return self._credentials

        sj = self._db_settings
        refresh_token = sj.get("youtube_refresh_token") or settings.YOUTUBE_REFRESH_TOKEN
        client_id = sj.get("youtube_client_id") or settings.YOUTUBE_CLIENT_ID
        client_secret = sj.get("youtube_client_secret") or settings.YOUTUBE_CLIENT_SECRET

        if not refresh_token:
            raise RuntimeError("YOUTUBE_REFRESH_TOKEN not configured")

        self._credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        self._credentials.refresh(Request())
        return self._credentials

    def _get_service(self):
        """Get or create the YouTube API service client."""
        if self._youtube is None:
            creds = self._get_credentials()
            self._youtube = build("youtube", "v3", credentials=creds)
        return self._youtube

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def upload(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: List[str],
        thumbnail_path: Optional[str] = None,
        premiere_mode: Optional[str] = None,
        genre_for_playlist: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a video to YouTube.

        Returns: {"video_id": str, "video_url": str, "playlist_id": Optional[str]}
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        mode = premiere_mode or settings.PREMIERE_MODE
        youtube = self._get_service()

        # Determine privacy and scheduled time
        privacy, scheduled_at = self._resolve_privacy(mode)

        body: Dict[str, Any] = {
            "snippet": {
                "title": title[:100],  # YouTube max 100 chars
                "description": description[:5000],
                "tags": tags[:500],  # YouTube max 500 tags
                "categoryId": CATEGORY_MUSIC,
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        if scheduled_at and privacy == "private":
            body["status"]["publishAt"] = scheduled_at.isoformat()
            body["status"]["privacyStatus"] = "private"
            logger.info("Scheduling premiere for %s", scheduled_at.isoformat())

        # Upload video
        media = MediaFileUpload(
            video_path,
            mimetype="video/*",
            resumable=True,
            chunksize=10 * 1024 * 1024,  # 10 MB chunks
        )

        import asyncio
        insert_request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = await asyncio.to_thread(self._resumable_upload, insert_request)
        video_id = response["id"]
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info("Video uploaded: %s (%s)", video_url, video_id)

        # Set thumbnail
        if thumbnail_path and os.path.exists(thumbnail_path):
            await self._set_thumbnail(youtube, video_id, thumbnail_path)

        # Playlist management
        playlist_id = None
        if genre_for_playlist:
            playlist_id = await self._manage_playlist(youtube, video_id, genre_for_playlist)

        return {
            "video_id": video_id,
            "video_url": video_url,
            "playlist_id": playlist_id,
        }

    def _resumable_upload(self, request) -> dict:
        """Execute a resumable upload, handling retries."""
        response = None
        retries = 0
        max_retries = 5

        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    logger.info("YouTube upload progress: %d%%", progress)
            except Exception as exc:
                retries += 1
                if retries > max_retries:
                    raise RuntimeError(f"YouTube upload failed after {max_retries} retries: {exc}")
                logger.warning("Upload chunk failed (retry %d/%d): %s", retries, max_retries, exc)
                import time
                time.sleep(2 ** retries)

        return response

    # ------------------------------------------------------------------
    # Thumbnail
    # ------------------------------------------------------------------

    async def _set_thumbnail(self, youtube, video_id: str, thumbnail_path: str) -> None:
        """Set a custom thumbnail for the video."""
        import asyncio
        try:
            media = MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
            await asyncio.to_thread(
                youtube.thumbnails().set(videoId=video_id, media_body=media).execute
            )
            logger.info("Custom thumbnail set for video %s", video_id)
        except Exception as exc:
            logger.error("Failed to set thumbnail for %s: %s", video_id, exc)

    # ------------------------------------------------------------------
    # Premiere scheduling
    # ------------------------------------------------------------------

    def _resolve_privacy(self, mode: str) -> tuple[str, Optional[datetime]]:
        """Determine privacy status and optional publish time."""
        if mode == "instant":
            return "public", None
        elif mode == "unlisted":
            return "unlisted", None
        elif mode == "scheduled":
            scheduled_at = self._calculate_optimal_time()
            return "private", scheduled_at
        else:
            return "private", None

    def _calculate_optimal_time(self) -> datetime:
        """Calculate the next optimal premiere time within 24 hours."""
        now_utc = datetime.now(timezone.utc)
        now_phoenix = now_utc + PHOENIX_UTC_OFFSET

        candidates: List[datetime] = []

        for day_name, hour in PREMIERE_SLOTS:
            # Calculate next occurrence of this day+hour
            day_index = {
                "monday": 0, "tuesday": 1, "wednesday": 2,
                "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            }[day_name]

            current_day = now_phoenix.weekday()
            days_ahead = (day_index - current_day) % 7
            candidate_phoenix = now_phoenix.replace(
                hour=hour, minute=0, second=0, microsecond=0
            ) + timedelta(days=days_ahead)

            # If it's today but already past, go to next week
            if candidate_phoenix <= now_phoenix:
                candidate_phoenix += timedelta(days=7)

            candidates.append(candidate_phoenix)

        # Filter to within 7 days (we'll pick the earliest reasonable one)
        candidates.sort()

        # Prefer within 24 hours
        within_24h = [c for c in candidates if (c - now_phoenix).total_seconds() <= 86400]
        chosen_phoenix = within_24h[0] if within_24h else candidates[0]

        # Convert back to UTC
        chosen_utc = chosen_phoenix - PHOENIX_UTC_OFFSET
        chosen_utc = chosen_utc.replace(tzinfo=timezone.utc)

        logger.info(
            "Optimal premiere time: %s Phoenix (%s UTC)",
            chosen_phoenix.strftime("%A %I:%M %p"),
            chosen_utc.isoformat(),
        )
        return chosen_utc

    # ------------------------------------------------------------------
    # Playlist management
    # ------------------------------------------------------------------

    async def _manage_playlist(
        self, youtube, video_id: str, genre: str
    ) -> Optional[str]:
        """Add video to appropriate genre playlist, creating if needed."""
        import asyncio
        playlist_name = f"{settings.YOUTUBE_DEFAULT_PLAYLIST_PREFIX} | {genre.title()} Mixes"

        try:
            # Search existing playlists
            playlists = await asyncio.to_thread(
                youtube.playlists().list(
                    part="snippet",
                    mine=True,
                    maxResults=50,
                ).execute
            )

            playlist_id = None
            for pl in playlists.get("items", []):
                if pl["snippet"]["title"].lower() == playlist_name.lower():
                    playlist_id = pl["id"]
                    break

            # Create if not found
            if not playlist_id:
                create_resp = await asyncio.to_thread(
                    youtube.playlists().insert(
                        part="snippet,status",
                        body={
                            "snippet": {
                                "title": playlist_name,
                                "description": f"{genre.title()} DJ mixes by {settings.BRAND_NAME}",
                            },
                            "status": {"privacyStatus": "public"},
                        },
                    ).execute
                )
                playlist_id = create_resp["id"]
                logger.info("Created playlist: %s (%s)", playlist_name, playlist_id)

            # Add video to playlist
            await asyncio.to_thread(
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {
                                "kind": "youtube#video",
                                "videoId": video_id,
                            },
                        },
                    },
                ).execute
            )
            logger.info("Added video %s to playlist %s", video_id, playlist_id)
            return playlist_id

        except Exception as exc:
            logger.error("Playlist management failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def verify_upload(self, video_id: str) -> Dict[str, Any]:
        """Check video status via the API."""
        import asyncio
        youtube = self._get_service()

        try:
            response = await asyncio.to_thread(
                youtube.videos().list(
                    part="status,snippet,processingDetails",
                    id=video_id,
                ).execute
            )

            items = response.get("items", [])
            if not items:
                return {"status": "not_found", "video_id": video_id}

            video = items[0]
            upload_status = video.get("status", {}).get("uploadStatus", "unknown")
            privacy = video.get("status", {}).get("privacyStatus", "unknown")
            processing = video.get("processingDetails", {}).get("processingStatus", "unknown")

            result = {
                "status": upload_status,
                "privacy": privacy,
                "processing": processing,
                "video_id": video_id,
                "title": video.get("snippet", {}).get("title", ""),
            }
            logger.info("YouTube verification for %s: %s", video_id, result)
            return result

        except Exception as exc:
            logger.error("YouTube verification failed for %s: %s", video_id, exc)
            return {"status": "error", "error": str(exc), "video_id": video_id}
