"""TikTok DISCOVERY: enumerate the authorized account's public videos with the official video.list API.

Discovery decides WHAT exists; media acquisition (pipeline/media.py, yt-dlp) is a separate step.
The token is refreshed once at the start of a discovery run (tiktok_auth.access_token(), saves the rotated
tokens back to this repo's secrets); one run takes seconds, the access token lasts 24 h.
"""
from __future__ import annotations

import time

from pipeline import tiktok_auth

# Video Object fields the Display API documents for video.list (only fields TikTok returns are stored).
FIELDS = ("id,create_time,title,video_description,duration,height,width,cover_image_url,share_url,"
          "embed_link,like_count,comment_count,share_count,view_count")
PAGE_SIZE = 20          # API maximum
MAX_PAGES = 500         # hard stop against a cursor that never ends


def list_videos(access_token: str, max_pages: int = MAX_PAGES) -> list[dict]:
    videos, cursor, seen = [], None, set()
    for _ in range(max_pages):
        body = {"max_count": PAGE_SIZE}
        if cursor is not None:
            body["cursor"] = cursor
        data = tiktok_auth._request(f"{tiktok_auth.VIDEO_LIST_URL}?fields={FIELDS}",
                                    headers={"Authorization": f"Bearer {access_token}"}, json_body=body)
        problem = tiktok_auth._api_error(data)
        if problem:
            raise tiktok_auth.TikTokAuthError(f"video.list failed: {problem}")
        page = data.get("data") or {}
        for v in page.get("videos") or []:
            vid = str(v.get("id") or "")
            if vid and vid not in seen:
                seen.add(vid)
                videos.append({k: val for k, val in v.items() if val not in (None, "")} | {"id": vid})
        if not page.get("has_more"):
            break
        new_cursor = page.get("cursor")
        if new_cursor is None or new_cursor == cursor:
            break
        cursor = new_cursor
        time.sleep(0.5)
    return videos


def discover() -> dict:
    """Refresh the token, then return {'videos': [...], 'open_id': ...}."""
    tok = tiktok_auth.access_token()
    return {"videos": list_videos(tok["access_token"]), "open_id": tok.get("open_id")}
