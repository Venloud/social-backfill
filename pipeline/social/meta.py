"""Instagram Reels, Threads and Facebook Page Reels: connection checks, token refresh and uploads
(adapted from kindled_iron/publish.py).

Instagram works with either token kind:
- Instagram Login token  -> graph.instagram.com (refresh: ig_refresh_token, no app secret needed)
- Facebook Login token   -> graph.facebook.com/<INSTAGRAM_USER_ID> (refresh: fb_exchange_token, needs
                            META_APP_ID + META_APP_SECRET)
Secrets: INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_USER_ID, optional META_APP_ID, META_APP_SECRET, THREADS_ACCESS_TOKEN,
FACEBOOK_PAGE_ID, FACEBOOK_PAGE_ACCESS_TOKEN.
"""
from __future__ import annotations

import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline.social.common import CONFIG, GRAPH_V, Fail, configured, env, http, save_secret

IG = f"https://graph.instagram.com/{GRAPH_V}"
FB = f"https://graph.facebook.com/{GRAPH_V}"
TH = "https://graph.threads.net/v1.0"


# ------------------------------------------------------------------ Instagram
def instagram_check() -> dict:
    tok, = env("INSTAGRAM_ACCESS_TOKEN")
    h = {"Authorization": f"Bearer {tok}"}
    try:
        d = http(f"{IG}/me?fields=user_id,username", headers=h)
        return {"id": d.get("user_id") or d.get("id"), "account": "@" + str(d.get("username")),
                "base": IG, "login": "instagram"}
    except Fail as ig_err:
        uid = os.environ.get("INSTAGRAM_USER_ID")
        if not uid:
            raise
        try:   # Facebook Login token: the IG professional account is read through graph.facebook.com
            d = http(f"{FB}/{urllib.parse.quote(uid)}?fields=id,username", headers=h)
        except Fail as fb_err:
            raise Fail(f"Instagram Login API: {str(ig_err)[:160]}; Facebook Graph: {str(fb_err)[:160]}") from None
        return {"id": d.get("id") or uid, "account": "@" + str(d.get("username")), "base": FB, "login": "facebook"}


def instagram_refresh(chk: dict) -> str:
    """Refresh the long-lived Instagram token and save it back. Returns a one-line status."""
    tok, = env("INSTAGRAM_ACCESS_TOKEN")
    warn = int(CONFIG.get("token_warn_days", 7))
    if chk.get("login") == "instagram":
        try:
            d = http(f"https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token"
                     f"&access_token={urllib.parse.quote(tok)}")
        except Fail as e:   # e.g. a token under 24 h old can't be refreshed yet
            return f"not refreshed this time ({str(e)[:120]})"
        if not d.get("access_token"):
            return "not refreshed (no token in the answer)"
        saved = save_secret("INSTAGRAM_ACCESS_TOKEN", d["access_token"])
        days = int(d.get("expires_in") or 0) / 86400
        return f"refreshed ({days:.0f} days){'' if saved else ', NOT saved'}"
    if not configured("META_APP_ID", "META_APP_SECRET"):
        return "automatic refresh needs META_APP_ID and META_APP_SECRET (Facebook Login token)"
    app, secret = os.environ["META_APP_ID"], os.environ["META_APP_SECRET"]
    info = http(f"{FB}/debug_token?input_token={urllib.parse.quote(tok)}"
                f"&access_token={urllib.parse.quote(app + '|' + secret)}").get("data", {})
    exp = int(info.get("expires_at") or 0)
    if not info.get("is_valid", False):
        raise Fail("INSTAGRAM_ACCESS_TOKEN is no longer valid: reconnect Instagram")
    if exp == 0:
        return "token does not expire"
    days = (exp - time.time()) / 86400
    if days > max(warn, 14):
        return f"valid for {days:.0f} more days (refresh starts at {max(warn, 14)} days left)"
    d = http(f"{FB}/oauth/access_token?" + urllib.parse.urlencode(
        {"grant_type": "fb_exchange_token", "client_id": app, "client_secret": secret, "fb_exchange_token": tok}))
    if not d.get("access_token"):
        return f"expires in {days:.0f} days and could not be refreshed"
    saved = save_secret("INSTAGRAM_ACCESS_TOKEN", d["access_token"])
    return f"refreshed (was {days:.0f} days left){'' if saved else ', NOT saved'}"


def _wait_container(url: str, tok: str, field: str = "status_code") -> None:
    for _ in range(60):
        d = http(f"{url}?fields={field}", headers={"Authorization": f"Bearer {tok}"})
        st = d.get(field) or d.get("status")
        if st in ("FINISHED", "PUBLISHED"):
            return
        if st in ("ERROR", "EXPIRED"):
            raise Fail(f"processing {st}: {d}")
        time.sleep(5)
    raise Fail("processing timed out")


def instagram_post(video: Path, post: dict, mode: str, chk: dict, public_url: str | None) -> dict:
    """NOT CALLED in this phase. Reel by resumable upload (no public URL needed), URL fallback."""
    tok, = env("INSTAGRAM_ACCESS_TOKEN")
    h = {"Authorization": f"Bearer {tok}"}
    base, uid = chk["base"], chk["id"]
    try:
        c = http(f"{base}/{uid}/media", "POST", headers=h, form={"media_type": "REELS", "upload_type": "resumable",
                                                                 "caption": post["caption"], "share_to_feed": "true"})
        size = video.stat().st_size
        req = urllib.request.Request(f"https://rupload.facebook.com/ig-api-upload/{GRAPH_V}/{c['id']}", data=video.read_bytes(),
                                     method="POST", headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)})
        with urllib.request.urlopen(req, timeout=600):
            pass
        how = "resumable upload"
    except Exception as e:  # noqa: BLE001
        if not public_url:
            raise Fail(f"resumable upload failed ({str(e)[:160]}) and no public URL")
        c = http(f"{base}/{uid}/media", "POST", headers=h, form={"media_type": "REELS", "video_url": public_url,
                                                                 "caption": post["caption"], "share_to_feed": "true"})
        how = "temporary public URL"
    _wait_container(f"{base}/{c['id']}", tok)
    p = http(f"{base}/{uid}/media_publish", "POST", headers=h, form={"creation_id": c["id"]})
    link = http(f"{base}/{p['id']}?fields=permalink", headers=h).get("permalink", "")
    return {"mode": f"live Reel ({how})", "id": p["id"], "url": link}


class PublishAttempted(Fail):
    """media_publish was called (or may have been): the Reel may be live. Never retry blindly."""


def _container_status(base: str, cid: str, tok: str, timeout_s: int = 600) -> str:
    """Poll a container until FINISHED / ERROR / EXPIRED (or timeout). Returns the final status code."""
    deadline = time.time() + timeout_s
    st = ""
    while time.time() < deadline:
        d = http(f"{base}/{cid}?fields=status_code,status", headers={"Authorization": f"Bearer {tok}"})
        st = d.get("status_code") or ""
        if st in ("FINISHED", "PUBLISHED", "ERROR", "EXPIRED"):
            return st if st != "ERROR" else f"ERROR ({str(d.get('status', ''))[:160]})"
        time.sleep(5)
    return f"TIMEOUT ({st or 'no status'})"


def instagram_upload_container(video: Path, caption: str, chk: dict) -> tuple[str, str]:
    """Create a Reels container by resumable upload (bytes go straight to Meta, no public URL) and wait for
    processing. Returns (container_id, final_status). Nothing is published here."""
    tok, = env("INSTAGRAM_ACCESS_TOKEN")
    h = {"Authorization": f"Bearer {tok}"}
    base, uid = chk["base"], chk["id"]
    from pipeline.social.covers import instagram_cover_url
    cover_url = instagram_cover_url()
    c = http(f"{base}/{uid}/media", "POST", headers=h, form={"media_type": "REELS", "upload_type": "resumable",
                                                             "caption": caption, "share_to_feed": "true",
                                                             "cover_url": cover_url})
    cid = c.get("id")
    if not cid:
        raise Fail("Instagram returned no container id")
    uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{GRAPH_V}/{cid}"
    size = video.stat().st_size
    req = urllib.request.Request(uri, data=video.read_bytes(), method="POST",
                                 headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)})
    try:
        with urllib.request.urlopen(req, timeout=600):
            pass
    except Exception as e:  # noqa: BLE001
        raise Fail(f"resumable upload failed: {type(e).__name__}: {str(e)[:160]}") from None
    return cid, _container_status(base, cid, tok)


def instagram_publish_container(cid: str, chk: dict) -> dict:
    """Publish a FINISHED container and confirm the media id. Raises PublishAttempted on any failure."""
    tok, = env("INSTAGRAM_ACCESS_TOKEN")
    h = {"Authorization": f"Bearer {tok}"}
    base, uid = chk["base"], chk["id"]
    try:
        p = http(f"{base}/{uid}/media_publish", "POST", headers=h, form={"creation_id": cid})
    except Fail as e:
        raise PublishAttempted(f"media_publish failed: {str(e)[:200]}") from None
    mid = p.get("id")
    if not mid:
        raise PublishAttempted("media_publish answered without a media id")
    try:
        m = http(f"{base}/{mid}?fields=id,permalink,media_product_type,timestamp", headers=h)
    except Fail as e:
        raise PublishAttempted(f"published media {mid} could not be confirmed: {str(e)[:160]}") from None
    if str(m.get("id")) != str(mid):
        raise PublishAttempted(f"confirmation returned a different id for {mid}")
    return {"id": str(mid), "url": m.get("permalink", ""), "product_type": m.get("media_product_type"),
            "timestamp": m.get("timestamp")}


# ------------------------------------------------------------------ Threads
def threads_check() -> dict:
    tok, = env("THREADS_ACCESS_TOKEN")
    d = http(f"{TH}/me?fields=id,username", headers={"Authorization": f"Bearer {tok}"})
    return {"id": d.get("id"), "account": "@" + str(d.get("username"))}


def threads_refresh() -> str:
    tok, = env("THREADS_ACCESS_TOKEN")
    try:
        d = http(f"https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token"
                 f"&access_token={urllib.parse.quote(tok)}")
    except Fail as e:
        return f"not refreshed this time ({str(e)[:120]})"
    if not d.get("access_token"):
        return "not refreshed"
    saved = save_secret("THREADS_ACCESS_TOKEN", d["access_token"])
    return f"refreshed ({int(d.get('expires_in') or 0) / 86400:.0f} days){'' if saved else ', NOT saved'}"


def threads_post(video: Path, post: dict, mode: str, chk: dict, public_url: str | None) -> dict:
    """NOT CALLED in this phase."""
    if not public_url:
        raise Fail("Threads needs a public video URL (none was made)")
    tok, = env("THREADS_ACCESS_TOKEN")
    h = {"Authorization": f"Bearer {tok}"}
    c = http(f"{TH}/me/threads", "POST", headers=h, form={"media_type": "VIDEO", "video_url": public_url,
                                                          "text": post.get("threads", post["caption"])[:500]})
    _wait_container(f"{TH}/{c['id']}", tok, "status")
    p = http(f"{TH}/me/threads_publish", "POST", headers=h, form={"creation_id": c["id"]})
    link = http(f"{TH}/{p['id']}?fields=permalink", headers=h).get("permalink", "")
    return {"mode": "live post (video fetched from a temporary public URL)", "id": p["id"], "url": link}


# ------------------------------------------------------------------ Facebook Page Reels
def facebook_check() -> dict:
    pid, tok = env("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN")
    d = http(f"{FB}/{urllib.parse.quote(pid)}?fields=id,name", headers={"Authorization": f"Bearer {tok}"})
    return {"id": d.get("id"), "account": d.get("name")}


def facebook_token_status() -> str:
    tok, = env("FACEBOOK_PAGE_ACCESS_TOKEN")
    app = (f"{os.environ['META_APP_ID']}|{os.environ['META_APP_SECRET']}"
           if configured("META_APP_ID", "META_APP_SECRET") else tok)
    d = http(f"{FB}/debug_token?input_token={urllib.parse.quote(tok)}&access_token={urllib.parse.quote(app)}")
    info = d.get("data", {})
    if not info.get("is_valid", True):
        raise Fail("FACEBOOK_PAGE_ACCESS_TOKEN is no longer valid: run 'Meta Token Refresh'")
    exp = int(info.get("expires_at") or 0)
    return "never expires" if exp == 0 else f"expires {time.strftime('%Y-%m-%d', time.gmtime(exp))}: run 'Meta Token Refresh'"


def facebook_post(video: Path, post: dict, mode: str, chk: dict) -> dict:
    """NOT CALLED in this phase."""
    pid, tok = env("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN")
    h = {"Authorization": f"Bearer {tok}"}
    s = http(f"{FB}/{pid}/video_reels", "POST", headers=h, form={"upload_phase": "start"})
    size = video.stat().st_size
    req = urllib.request.Request(f"https://rupload.facebook.com/video-upload/{GRAPH_V}/{s['video_id']}", data=video.read_bytes(),
                                 method="POST", headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)})
    with urllib.request.urlopen(req, timeout=600):
        pass
    state = "PUBLISHED" if mode == "live" else "DRAFT"
    http(f"{FB}/{pid}/video_reels", "POST", headers=h, form={"upload_phase": "finish", "video_id": s["video_id"],
                                                             "video_state": state, "description": post["caption"]})
    return {"mode": f"Page Reel ({state.lower()})", "id": s["video_id"], "url": f"https://www.facebook.com/reel/{s['video_id']}"}


