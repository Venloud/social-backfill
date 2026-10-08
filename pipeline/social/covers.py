"""Default cover shared by Instagram Reels and YouTube Shorts."""
from __future__ import annotations

import os
import io
import urllib.request
from PIL import Image
import urllib.parse
from pathlib import Path

from pipeline.social.common import Fail, http, mask

def tiktok_cover(entry: dict, video_id: str) -> Path:
    """Download and validate the original TikTok cover for the matching post.

    TikTok Display API cover_image_url is preferred; yt-dlp thumbnail metadata is
    the fallback. Do not silently replace a missing original with a generic cover.
    """
    if not video_id.isdigit() or str(entry.get("source", {}).get("video_id")) != video_id:
        raise Fail("Cover video id does not match the source post")
    path = Path(__file__).resolve().parents[2] / "media" / "covers" / f"{video_id}.jpg"
    if path.exists():
        try:
            with Image.open(path) as im:
                im.verify()
            return path
        except Exception:
            path.unlink(missing_ok=True)
    metadata = entry.get("source", {}).get("metadata") or {}
    urls = [metadata.get("cover_image_url")]
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "noplaylist": True, "socket_timeout": 20}) as ydl:
            info = ydl.extract_info(entry["source"]["url"], download=False) or {}
        if str(info.get("id", video_id)) == video_id:
            urls += [t.get("url") for t in reversed(info.get("thumbnails") or [])]
            urls.append(info.get("thumbnail"))
    except Exception:
        pass
    for url in dict.fromkeys(u for u in urls if isinstance(u, str) and u.startswith("https://")):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                continue
            with Image.open(io.BytesIO(raw)) as im:
                if im.width < 100 or im.height < 100:
                    continue
                rgb = im.convert("RGB")
                path.parent.mkdir(parents=True, exist_ok=True)
                rgb.save(path, "JPEG", quality=94)
            return path
        except Exception:
            continue
    raise Fail(f"Unable to retrieve TikTok's original cover for {video_id}")


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


def youtube_cover(video_id: str, token: str, cover_path: Path | None = None) -> dict:
    """Set the cover on an existing video, never upload another video."""
    data = http(
        "https://www.googleapis.com/upload/youtube/v3/thumbnails/set?"
        + urllib.parse.urlencode({"videoId": video_id, "uploadType": "media"}),
        "POST", data=(cover_path or COVER_PATH).read_bytes(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
    )
    if not data.get("items"):
        raise Fail("YouTube returned no thumbnail confirmation")
    return {"status": "set", "asset": str(cover_path) if cover_path else "assets/default-cover.jpg"}
