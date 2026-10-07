"""TikTok connection check + upload (adapted from kindled_iron/publish.py).

There is ONE TikTok token path in this repo: pipeline/tiktok_auth.py (used by the Connect TikTok workflow).
Every call here starts with tiktok_auth.access_token(), which refreshes the 24 h access token and saves the new
access + refresh token + open id back into this repo's secrets.
"""
from __future__ import annotations

import time
import urllib.request
from pathlib import Path

from pipeline import tiktok_auth
from pipeline.social.common import Fail, NotConfigured, configured, http

TT = "https://open.tiktokapis.com/v2"
NEEDED = ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN")


def tiktok_refresh() -> dict:
    """EVERY TikTok call starts here. Returns the token answer (access_token, scope, expires...)."""
    if not configured(*NEEDED):
        raise NotConfigured("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REFRESH_TOKEN not set")
    try:
        return tiktok_auth.access_token()
    except tiktok_auth.TikTokAuthError as e:
        raise Fail(f"TikTok refresh failed ({e}): re-run the 'Connect TikTok' workflow") from None


def tiktok_check() -> dict:
    d = tiktok_refresh()
    tok = d["access_token"]
    info = {"scope": d.get("scope", ""), "refresh_expires_days": round(int(d.get("refresh_expires_in") or 0) / 86400, 1)}
    user = tiktok_auth.user_info(tok, "open_id,display_name")
    info["display_name"] = ((user.get("data") or {}).get("user") or {}).get("display_name", "")
    if "video.publish" in info["scope"]:
        # Read-only: returns the creator's username and the privacy levels allowed for posts.
        c = http(f"{TT}/post/publish/creator_info/query/", "POST", data={}, headers={"Authorization": f"Bearer {tok}"})
        if c.get("error", {}).get("code") not in (None, "ok"):
            raise Fail(f"TikTok creator info: {c['error']}")
        info.update({k: v for k, v in c.get("data", {}).items()
                     if k in ("creator_username", "creator_nickname", "privacy_level_options", "max_video_post_duration_sec")})
    info["account"] = ("@" + info["creator_username"]) if info.get("creator_username") else info.get("display_name") or "?"
    return info


def tiktok_post(video: Path, post: dict, mode: str, chk: dict) -> dict:
    """NOT CALLED in this phase. Direct post when allowed, else TikTok drafts (inbox)."""
    tok = tiktok_refresh()["access_token"]
    size = video.stat().st_size
    src = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    h = {"Authorization": f"Bearer {tok}"}
    used, note = "draft", ""
    init = None
    if mode == "live" and "video.publish" in chk.get("scope", ""):
        levels = chk.get("privacy_level_options") or []
        level = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in levels else (levels[0] if levels else "SELF_ONLY")
        try:
            init = http(f"{TT}/post/publish/video/init/", "POST", headers=h, data={
                "post_info": {"title": post["caption"][:2200], "privacy_level": level, "disable_duet": False,
                              "disable_comment": False, "disable_stitch": False, "video_cover_timestamp_ms": 1000},
                "source_info": src})
            if init.get("error", {}).get("code") not in (None, "ok"):
                raise Fail(str(init["error"]))
            used = "direct post" + ("" if level == "PUBLIC_TO_EVERYONE" else f" ({level}: the app is not audited for public posts)")
        except Fail as e:
            note = f"direct post refused ({str(e)[:160]}); sent to drafts instead"
            init = None
    elif mode == "live":
        note = "the app has no video.publish scope (direct post not allowed): sent to drafts"
    if init is None:
        init = http(f"{TT}/post/publish/inbox/video/init/", "POST", headers=h, data={"source_info": src})
        if init.get("error", {}).get("code") not in (None, "ok"):
            raise Fail(f"TikTok draft init: {init['error']}")
        used = "draft (TikTok inbox: open TikTok to finish the post)"
    data = init["data"]
    req = urllib.request.Request(data["upload_url"], data=video.read_bytes(), method="PUT",
                                 headers={"Content-Type": "video/mp4", "Content-Length": str(size),
                                          "Content-Range": f"bytes 0-{size - 1}/{size}"})
    with urllib.request.urlopen(req, timeout=600):
        pass
    status, st = "", {}
    for _ in range(30):
        st = http(f"{TT}/post/publish/status/fetch/", "POST", headers=h, data={"publish_id": data["publish_id"]})
        status = st.get("data", {}).get("status", "")
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"):
            break
        time.sleep(5)
    if status == "FAILED":
        raise Fail(f"TikTok processing failed: {st.get('data', {}).get('fail_reason')}")
    user = chk.get("creator_username", "")
    return {"mode": used, "id": data["publish_id"], "status": status, "note": note,
            "url": f"https://www.tiktok.com/@{user}" if user else ""}
