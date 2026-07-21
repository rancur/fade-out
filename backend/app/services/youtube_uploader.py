"""YouTube Data API v3 uploader with OAuth2, publish scheduling, and playlist management."""

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

DAY_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2,
    "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}

# Verified 2026-07: the YouTube Data/Live Streaming APIs cannot create a
# Premiere or convert an uploaded video into one (liveBroadcasts only bind
# to liveStreams; feature request issuetracker.google.com/issues/414284069
# is open). Premieres can only be enabled in YouTube Studio, so fade-out
# offers immediate | scheduled only. Publish modes stored as "premiere" by
# older builds are coerced to "scheduled" in ``_resolve_publish``.
LEGACY_PREMIERE_MODE = "premiere"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]


class YouTubeUploader:
    """Upload videos to YouTube via the Data API v3."""

    def __init__(
        self, db_settings_json: Optional[Dict] = None, mix_id: Optional[str] = None
    ) -> None:
        sj = db_settings_json or {}
        self._db_settings = sj
        self._mix_id = mix_id  # for activity-log attribution (optional)
        self._credentials: Optional[Credentials] = None
        self._youtube = None

    async def _activity(self, level: str, event: str, message: str, **kwargs) -> None:
        """Best-effort activity-log emit — never breaks an upload."""
        try:
            from app.services import activity_log

            await activity_log.log(
                level, event, message, mix_id=self._mix_id, platform="youtube", **kwargs
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("activity emit failed for %s", event, exc_info=True)

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
        progress_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Upload a video to YouTube.

        Returns: {"video_id": str, "video_url": str, "playlist_id": Optional[str]}
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        mode = premiere_mode or settings.PREMIERE_MODE
        youtube = self._get_service()

        # Determine privacy and scheduled time
        privacy, scheduled_at = await self._resolve_publish(mode)

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
            logger.info("Scheduling publish for %s", scheduled_at.isoformat())

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

        # The resumable upload runs in a worker thread; capture the loop so
        # chunk progress can be bridged back to the async progress callback.
        loop = asyncio.get_running_loop() if progress_cb else None
        response = await asyncio.to_thread(
            self._resumable_upload, insert_request, progress_cb, loop
        )
        video_id = response["id"]
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info("Video uploaded: %s (%s)", video_url, video_id)
        if progress_cb:
            # The final chunk returns with no status object, so 100% would
            # otherwise never be reported.
            try:
                await progress_cb(100, "upload complete")
            except Exception:  # pragma: no cover - progress must never break IO
                pass

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

    async def upload_short(
        self,
        file_path: str,
        title: str,
        description: str,
        tags: List[str],
    ) -> Dict[str, Any]:
        """Upload a vertical clip as a YouTube Short.

        A Short is a plain ``videos.insert`` (1600 quota units) — YouTube
        classifies it as a Short from the video itself (vertical/square and
        <= 3 minutes; the limit moved from 60s to 3 min on 2024-10-15), not
        from any API flag. No custom thumbnail is set: the Shorts player uses
        a frame from the video. Uploaded public immediately — Shorts get
        their distribution from the feed's test-audience phase in the first
        hours, so premiere scheduling adds nothing for them.

        Returns {"video_id": str, "video_url": str} (a /shorts/ URL).
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Short file not found: {file_path}")

        youtube = self._get_service()
        body: Dict[str, Any] = {
            "snippet": {
                "title": title[:100],  # YouTube max 100 chars
                "description": description[:5000],
                "tags": tags[:500],
                "categoryId": CATEGORY_MUSIC,
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(
            file_path,
            mimetype="video/mp4",
            resumable=True,
            chunksize=10 * 1024 * 1024,
        )

        import asyncio

        insert_request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )
        response = await asyncio.to_thread(self._resumable_upload, insert_request)
        video_id = response["id"]
        video_url = f"https://www.youtube.com/shorts/{video_id}"
        logger.info("Short uploaded: %s (%s)", video_url, video_id)
        return {"video_id": video_id, "video_url": video_url}

    def _resumable_upload(self, request, progress_cb=None, loop=None) -> dict:
        """Execute a resumable upload, handling retries.

        Runs in a worker thread; per-chunk percent is forwarded to the async
        ``progress_cb`` (when given) via ``run_coroutine_threadsafe`` on the
        captured event loop.
        """
        response = None
        retries = 0
        max_retries = 5

        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    logger.info("YouTube upload progress: %d%%", progress)
                    if progress_cb is not None and loop is not None:
                        try:
                            import asyncio

                            asyncio.run_coroutine_threadsafe(
                                progress_cb(progress, f"uploading {progress}%"), loop
                            )
                        except Exception:  # pragma: no cover - progress must never break IO
                            pass
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

    async def _resolve_publish(self, mode: str) -> tuple[str, Optional[datetime]]:
        """Privacy status + optional publishAt for a publish mode.

        Modes (new ``youtube_publish_mode`` enum + legacy ``premiere_mode``
        values):

        - ``immediate`` / legacy ``instant`` → public on insert
        - legacy ``unlisted`` → unlisted on insert
        - ``scheduled`` → private + publishAt at the configured
          premiere_day/premiere_hour_utc slot

        ``premiere`` is no longer a supported mode (the Data API cannot create
        Premieres). Values still stored in older databases are coerced to
        ``scheduled`` so the pipeline keeps running.
        """
        if mode == LEGACY_PREMIERE_MODE:
            logger.info(
                "publish mode 'premiere' is no longer supported (Data API "
                "cannot create Premieres); treating as 'scheduled'"
            )
            mode = "scheduled"
        if mode in ("immediate", "instant"):
            return "public", None
        if mode == "unlisted":
            return "unlisted", None
        if mode == "scheduled":
            return "private", await self._scheduled_publish_time()
        return "private", None

    async def _scheduled_publish_time(self) -> datetime:
        """Next occurrence of the configured premiere_day/premiere_hour_utc.

        Resolved via app_config (DB over env). Falls back to the legacy
        optimal-slot heuristic when the configured values are unusable.
        """
        from app.services import app_config

        try:
            day = str(await app_config.resolve("premiere_day") or "").lower()
            hour = int(await app_config.resolve("premiere_hour_utc"))
            day_index = DAY_INDEX[day]
            if not 0 <= hour <= 23:
                raise ValueError(f"premiere_hour_utc out of range: {hour}")
        except Exception:
            logger.warning(
                "premiere_day/premiere_hour_utc unusable; using optimal-slot "
                "fallback", exc_info=True,
            )
            return self._calculate_optimal_time()

        now_utc = datetime.now(timezone.utc)
        candidate = now_utc.replace(
            hour=hour, minute=0, second=0, microsecond=0
        ) + timedelta(days=(day_index - now_utc.weekday()) % 7)
        if candidate <= now_utc:
            candidate += timedelta(days=7)
        logger.info(
            "Scheduled publish time: %s UTC (%s %02d:00)",
            candidate.isoformat(), day, hour,
        )
        return candidate

    def _resolve_privacy(self, mode: str) -> tuple[str, Optional[datetime]]:
        """Legacy sync resolver (pre-``youtube_publish_mode``); kept for
        compatibility. ``upload()`` uses ``_resolve_publish``."""
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

        from app.services import app_config

        prefix = await app_config.resolve("youtube_default_playlist_prefix")
        playlist_name = f"{prefix} | {genre.title()} Mixes"

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
    # Description update (cross-linking)
    # ------------------------------------------------------------------

    async def update_description(
        self, video_id: str, description: str, title: Optional[str] = None
    ) -> bool:
        """Replace a video's description via ``videos.update`` (best-effort).

        ``videos.update`` replaces the whole ``snippet`` part, so the current
        snippet is fetched first and only the description (and optionally title)
        is changed -- otherwise categoryId/title would be wiped. Uses the
        existing OAuth token (needs the youtube manage scope, already requested).
        """
        import asyncio
        youtube = self._get_service()

        resp = await asyncio.to_thread(
            youtube.videos().list(part="snippet", id=video_id).execute
        )
        items = resp.get("items", [])
        if not items:
            raise RuntimeError(f"YouTube video {video_id} not found for update")

        snippet = items[0]["snippet"]
        snippet["description"] = description[:5000]
        if title:
            snippet["title"] = title[:100]

        await asyncio.to_thread(
            youtube.videos().update(
                part="snippet",
                body={"id": video_id, "snippet": snippet},
            ).execute
        )
        logger.info("Updated YouTube description for video %s", video_id)
        await self._activity(
            "info", "description_updated",
            f"YouTube description updated for video {video_id}"
            + (f" (title: {title[:60]!r})" if title else ""),
            context={"video_id": video_id, "title_changed": bool(title)},
        )
        return True

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

    # ==================================================================
    # Catalog listing / editing APIs (Stage C — back-catalog management)
    # ==================================================================
    # Everything below is additive: read the channel's full upload list and
    # push targeted edits to already-published videos. Raw API resources are
    # returned as-is; normalization lives in services/catalog_sync.py.

    async def list_all_uploads(self) -> List[Dict[str, Any]]:
        """Return every uploaded video on the authed channel (raw resources).

        channels.list(mine=true) -> uploads playlist id -> playlistItems.list
        paginated (50/page) -> videos.list batched (50/call) with
        part=snippet,contentDetails,status.
        """
        import asyncio

        youtube = self._get_service()

        channels = await asyncio.to_thread(
            youtube.channels().list(part="contentDetails", mine=True).execute
        )
        items = channels.get("items", [])
        if not items:
            return []
        uploads_playlist = (
            items[0]
            .get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads")
        )
        if not uploads_playlist:
            return []

        video_ids: List[str] = []
        page_token: Optional[str] = None
        while True:
            resp = await asyncio.to_thread(
                youtube.playlistItems()
                .list(
                    part="contentDetails",
                    playlistId=uploads_playlist,
                    maxResults=50,
                    pageToken=page_token,
                )
                .execute
            )
            for item in resp.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if vid:
                    video_ids.append(vid)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        videos: List[Dict[str, Any]] = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            resp = await asyncio.to_thread(
                youtube.videos()
                .list(part="snippet,contentDetails,status", id=",".join(batch))
                .execute
            )
            videos.extend(resp.get("items", []))
        return videos

    async def update_video_fields(
        self,
        video_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        """Mutate only the given snippet fields via ``videos.update``.

        The current snippet is fetched first so untouched fields
        (categoryId, other metadata) survive the whole-part replacement.
        Raises on any failure — the apply worker records the error.
        """
        import asyncio

        youtube = self._get_service()
        resp = await asyncio.to_thread(
            youtube.videos().list(part="snippet", id=video_id).execute
        )
        items = resp.get("items", [])
        if not items:
            raise RuntimeError(f"YouTube video {video_id} not found for update")

        snippet = items[0]["snippet"]
        if title is not None:
            snippet["title"] = title[:100]
        if description is not None:
            snippet["description"] = description[:5000]
        if tags is not None:
            snippet["tags"] = tags[:500]

        await asyncio.to_thread(
            youtube.videos()
            .update(part="snippet", body={"id": video_id, "snippet": snippet})
            .execute
        )
        logger.info("Updated YouTube video fields for %s", video_id)

    async def set_thumbnail(self, video_id: str, thumbnail_path: str) -> None:
        """Set a custom thumbnail, raising on failure (unlike _set_thumbnail)."""
        import asyncio

        if not os.path.exists(thumbnail_path):
            raise FileNotFoundError(f"Thumbnail not found: {thumbnail_path}")
        youtube = self._get_service()
        media = MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
        await asyncio.to_thread(
            youtube.thumbnails().set(videoId=video_id, media_body=media).execute
        )
        logger.info("Thumbnail set for video %s", video_id)

    async def list_playlists(self) -> List[Dict[str, Any]]:
        """Return the channel's playlists (raw resources, paginated)."""
        import asyncio

        youtube = self._get_service()
        playlists: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            resp = await asyncio.to_thread(
                youtube.playlists()
                .list(part="snippet", mine=True, maxResults=50, pageToken=page_token)
                .execute
            )
            playlists.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return playlists

    async def create_playlist(
        self, title: str, description: str = "", privacy: str = "public"
    ) -> str:
        """Create a playlist and return its id (raises on failure).

        Costs 50 quota units — callers budget it like any other catalog write.
        """
        import asyncio

        youtube = self._get_service()
        resp = await asyncio.to_thread(
            youtube.playlists()
            .insert(
                part="snippet,status",
                body={
                    "snippet": {"title": title, "description": description},
                    "status": {"privacyStatus": privacy},
                },
            )
            .execute
        )
        playlist_id = resp["id"]
        logger.info("Created playlist: %s (%s)", title, playlist_id)
        return playlist_id

    async def list_playlist_video_ids(self, playlist_id: str) -> List[str]:
        """Video ids currently in a playlist (paginated; cheap 1-unit reads)."""
        import asyncio

        youtube = self._get_service()
        video_ids: List[str] = []
        page_token: Optional[str] = None
        while True:
            resp = await asyncio.to_thread(
                youtube.playlistItems()
                .list(
                    part="contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                )
                .execute
            )
            for item in resp.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if vid:
                    video_ids.append(vid)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return video_ids

    async def add_video_to_playlist(self, playlist_id: str, video_id: str) -> None:
        """Insert a video into a playlist (raises on failure)."""
        import asyncio

        youtube = self._get_service()
        await asyncio.to_thread(
            youtube.playlistItems()
            .insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    },
                },
            )
            .execute
        )
        logger.info("Added video %s to playlist %s", video_id, playlist_id)

    async def remove_video_from_playlist(self, playlist_id: str, video_id: str) -> None:
        """Delete a video's playlistItem rows from a playlist (raises on failure)."""
        import asyncio

        youtube = self._get_service()
        resp = await asyncio.to_thread(
            youtube.playlistItems()
            .list(part="id", playlistId=playlist_id, videoId=video_id, maxResults=50)
            .execute
        )
        for item in resp.get("items", []):
            await asyncio.to_thread(
                youtube.playlistItems().delete(id=item["id"]).execute
            )
        logger.info("Removed video %s from playlist %s", video_id, playlist_id)
