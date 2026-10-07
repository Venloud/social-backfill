"""YouTube Shorts connection check + upload (adapted from kindled_iron/publish.py).
Secrets: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN (this repo's names)."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from pipeline.social.common import CONFIG, Fail, env, http, mask


def youtube_token() -> tuple[str, str]:
    cid, sec, ref = env("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    d = http("https://oauth2.googleapis.com/token", "POST",
             form={"client_id": cid, "client_secret": sec, "refresh_token": ref, "grant_type": "refresh_token"})
    if not d.get("access_token"):
        raise Fail("Google returned no access token")
    mask(d["access_token"])
    return d["access_token"], d.get("scope", "")


def youtube_check() -> dict:
    """A working refresh is the check. The channel name needs a read scope (youtube / youtube.readonly);
    with only youtube.upload the channel lookup is skipped."""
    tok, scope = youtube_token()
    can_upload = "youtube.upload" in scope or "auth/youtube " in scope + " "
    info = {"scope": " ".join(s.rsplit("/", 1)[-1] for s in scope.split()), "upload": can_upload}
    if "auth/youtube " in scope + " " or "youtube.readonly" in scope or "youtube.force-ssl" in scope:
        d = http("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                 headers={"Authorization": f"Bearer {tok}"})
        items = d.get("items") or []
        info["account"] = items[0]["snippet"]["title"] if items else "no channel on this account"
    else:
        info["account"] = "name hidden (token has upload scope only)"
    if not can_upload:
        raise Fail(f"refresh token does not grant youtube.upload (scopes: {info['scope']}) [{info['account']}]")
    return info


class UploadStarted(Fail):
    """The video bytes were (or may have been) sent: YouTube may hold a copy. Never retry blindly."""


def youtube_upload(video: Path, title: str, description: str, tags: list[str], privacy: str = "public") -> dict:
    """Resumable upload of one Short. Raises Fail before any byte is sent, UploadStarted after."""
    tok, _ = youtube_token()
    body = {"snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:15],
                        "categoryId": str(CONFIG.get("youtube_category", "22"))},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    init = http("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status", "POST",
                data=body, headers={"Authorization": f"Bearer {tok}", "X-Upload-Content-Type": "video/mp4",
                                    "X-Upload-Content-Length": str(video.stat().st_size)})
    loc = init["_headers"].get("Location") or init["_headers"].get("location")
    if not loc:
        raise Fail("YouTube gave no upload URL")
    try:
        req = urllib.request.Request(loc, data=video.read_bytes(), method="PUT",
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "video/mp4"})
        with urllib.request.urlopen(req, timeout=900) as r:
            d = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        raise UploadStarted(f"upload interrupted after sending bytes ({type(e).__name__}: {str(e)[:160]})") from None
    if not d.get("id"):
        raise UploadStarted("YouTube answered without a video id")
    return {"id": d["id"], "url": f"https://youtube.com/shorts/{d['id']}",
            "privacy": (d.get("status") or {}).get("privacyStatus"),
            "upload_status": (d.get("status") or {}).get("uploadStatus")}


def youtube_post(video: Path, post: dict, mode: str, chk: dict) -> dict:
    """Kindled Iron-style entry point (kept for reference; the backfill uses youtube_upload)."""
    tok, _ = youtube_token()
    title = post["title"][:90]
    if "#shorts" not in title.lower():
        title = (title + " #Shorts")[:100]
    privacy = "public" if mode == "live" else "private"
    body = {"snippet": {"title": title, "description": (post["caption"] + "\n\n" + post.get("description", "")).strip()[:4900],
                        "tags": [w.strip("#") for w in post["caption"].split() if w.startswith("#")][:15],
                        "categoryId": str(CONFIG.get("youtube_category", "22"))},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    init = http("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status", "POST",
                data=body, headers={"Authorization": f"Bearer {tok}", "X-Upload-Content-Type": "video/mp4",
                                    "X-Upload-Content-Length": str(video.stat().st_size)})
    loc = init["_headers"].get("Location") or init["_headers"].get("location")
    req = urllib.request.Request(loc, data=video.read_bytes(), method="PUT",
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "video/mp4"})
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read().decode())
    return {"mode": f"{privacy} Short, not made for kids", "id": d["id"], "url": f"https://youtube.com/shorts/{d['id']}"}
