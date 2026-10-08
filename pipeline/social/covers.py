"""Default cover shared by Instagram Reels and YouTube Shorts."""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

from pipeline.social.common import Fail, http, mask

COVER_PATH = Path(__file__).resolve().parents[2] / "assets" / "default-cover.jpg"


def instagram_cover_url() -> str:
    """Get a fresh signed URL for only the cover in this private repository."""
    token = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or repo.lower() != "venloud/social-backfill":
        raise Fail("Instagram cover needs GH_TOKEN and GITHUB_REPOSITORY=Venloud/social-backfill")
    ref = os.environ.get("GITHUB_SHA") or "main"
    data = http(
        f"https://api.github.com/repos/{repo}/contents/assets/default-cover.jpg?"
        + urllib.parse.urlencode({"ref": ref}),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    url = data.get("download_url") or ""
    if not url.startswith("https://raw.githubusercontent.com/"):
        raise Fail("GitHub did not return a downloadable cover URL")
    mask(url)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    for value in query.get("token", []):
        mask(value)
    return url


def youtube_cover(video_id: str, token: str) -> dict:
    """Set the cover on an existing video, never upload another video."""
    data = http(
        "https://www.googleapis.com/upload/youtube/v3/thumbnails/set?"
        + urllib.parse.urlencode({"videoId": video_id, "uploadType": "media"}),
        "POST", data=COVER_PATH.read_bytes(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
    )
    if not data.get("items"):
        raise Fail("YouTube returned no thumbnail confirmation")
    return {"status": "set", "asset": "assets/default-cover.jpg"}
