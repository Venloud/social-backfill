"""Backfill Publish: advance each destination independently without duplicating successful posts.

    python -m pipeline.publish_one --state-dir .state [--discover] [--dry-run]

Rules:
- Each destination has its own oldest-first queue. One run may therefore handle different source videos for YouTube and Instagram.
- A destination on "publishing" (a crashed run) is never retried automatically, but it does NOT block the other platform.
- Published destinations are never uploaded again. Failed destinations are retryable without holding healthy platforms back.
- Write-ahead: mark "publishing" and push the state BEFORE each upload; mark "published" (remote id, URL,
  timestamp) and push AFTER. If the state can't be pushed, nothing is uploaded.
- Never refreshes or writes TikTok secrets: --discover reads video.list with the stored access token only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import subprocess
import sys
from pathlib import Path

from pipeline import captions, manifest as mf, media
from pipeline.state_store import StatePushError, StateStore

DESTS = ("youtube", "instagram")


def log(msg: str) -> None:
    print(msg, flush=True)


def discover_readonly(m: dict) -> None:
    """Add new TikTok videos using the STORED access token (no refresh, no secret writes)."""
    from pipeline import tiktok_auth, tiktok_discovery
    tok = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
    if not tok:
        log("Discovery skipped: TIKTOK_ACCESS_TOKEN not available; using the manifest as it is")
        return
    tiktok_auth.mask(tok)
    try:
        videos = tiktok_discovery.list_videos(tok)
    except tiktok_auth.TikTokAuthError as e:
        log(f"Discovery skipped ({str(e)[:160]}); using the manifest as it is. "
            "Run 'TikTok Backfill Test' or 'Platform Connection Check' to refresh the TikTok token.")
        return
    old_ids = set(m.get("videos", {}))
    c = mf.merge(m, videos)
    for vid in set(m["videos"]) - old_ids:
        m["videos"][vid]["source"]["fresh_priority"] = True
    log(f"Discovery: {len(videos)} videos on TikTok, {c['added']} new, {c['total']} in the manifest")


def dest(entry: dict, name: str) -> dict:
    d = entry.setdefault("destinations", {}).setdefault(name, dict(mf.DEST_TEMPLATE))
    for k, v in mf.DEST_TEMPLATE.items():
        d.setdefault(k, v)
    d.setdefault("url", None)
    return d


def _day(iso: str | None) -> str | None:
    return iso[:10] if iso else None


def _fresh(entry: dict, day: str) -> bool:
    """Newly discovered posts take priority for 24 hours; after that they join normal backfill."""
    source = entry.get("source", {})
    if not source.get("fresh_priority"):
        return False
    seen = source.get("first_seen_at")
    if not seen:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(seen.replace("Z", "+00:00"))
        return 0 <= age.total_seconds() < 86400
    except ValueError:
        return False


def pick_jobs(m: dict, daily_limit: int | None = None) -> dict[str, tuple[str, dict]]:
    """One eligible item per destination, fresh first, independent historical queues.

    Daily historical quota is a global count of distinct source TikTok ids with a successful
    destination publish today. A video already counted today can finish its other destinations
    even after the quota is reached. Fresh posts are exempt from the historical quota.
    """
    jobs: dict[str, tuple[str, dict]] = {}
    ordered = mf.ordered(m)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counted = {
        vid for vid, entry in ordered
        if not _fresh(entry, day) and any(
            d.get("status") == "published" and _day(d.get("published_at")) == day
            for d in entry.get("destinations", {}).values()
        )
    }
    reserved: set[str] = set()
    for name in DESTS:
        fresh = [(vid, entry) for vid, entry in ordered if _fresh(entry, day)]
        historical = [(vid, entry) for vid, entry in ordered if not _fresh(entry, day)]
        for vid, entry in fresh + historical:
            d = dest(entry, name)
            if d["status"] == "published":
                continue
            if d["status"] == "publishing":
                log(f"STUCK {name}, skipped: {vid} is still 'publishing'. Verify on {name} before retry.")
                continue
            if daily_limit is not None and not _fresh(entry, day):
                if vid not in counted and vid not in reserved and len(counted | reserved) >= daily_limit:
                    continue
                reserved.add(vid)
            jobs[name] = (vid, entry)
            break
    log(f"Historical daily quota (UTC): {len(counted)} completed source IDs, "
        f"{len(reserved - counted)} reserved this run / {daily_limit if daily_limit is not None else 'unlimited'}")
    for vid, entry in ordered:
        ds = {n: dest(entry, n) for n in DESTS}
        if all(d["status"] == "published" for d in ds.values()):
            log(f"already posted: {vid} -> {ds['youtube'].get('url')} / {ds['instagram'].get('url')}")
    return jobs

def to_h264(src: Path) -> Path:
    out = src.with_name(src.stem + ".h264.mp4")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c:v", "libx264", "-preset", "medium",
                        "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high", "-c:a", "aac", "-b:a", "128k",
                        "-ar", "44100", "-movflags", "+faststart", str(out)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg H.264 encode failed: {r.stderr.strip()[:200]}")
    media.probe(out)
    return out


def post_youtube(vid: str, entry: dict, video: Path) -> dict:
    from pipeline.social import youtube
    title, description, tags = captions.youtube_text(entry["source"], vid)
    log(f"YouTube title: {title}")
    from pipeline.social.covers import tiktok_cover
    try:
        cover = tiktok_cover(entry, vid)
        log(f"TikTok original cover downloaded: {cover.name}")
    except Exception as exc:
        log(f"WARNING TikTok cover unavailable for {vid}: {media.sanitize(exc)}")
        cover = None
    r = youtube.youtube_upload(video, title, description, tags, privacy="public", cover_path=cover)
    if r.get("privacy") != "public":
        log(f"WARNING: YouTube reports privacy '{r.get('privacy')}' (an unverified Google Cloud project "
            "forces API uploads to private until the API audit passes)")
    return {"remote_id": r["id"], "url": r["url"], "privacy": r.get("privacy"), "format": "original MP4", "cover": r.get("cover")}


def post_instagram(vid: str, entry: dict, video: Path, info: dict) -> dict:
    from pipeline.social import meta
    chk = meta.instagram_check()
    from pipeline.social.covers import tiktok_cover
    cover_path = tiktok_cover(entry, vid)
    cover_url = (entry.get("source", {}).get("metadata") or {}).get("cover_image_url")
    if not isinstance(cover_url, str) or not cover_url.startswith("https://"):
        raise meta.Fail(f"No original TikTok cover URL for {vid}; refusing to reuse a generic cover")
    log(f"Instagram: verified matching TikTok cover saved as {cover_path.name}")
    caption = captions.instagram_text(entry["source"])
    codec = info.get("video_codec")
    attempts = [(f"{codec} (original)", video)]
    cid = None
    for label, path in attempts:
        log(f"Instagram: uploading {label} by resumable upload")
        cid, status = meta.instagram_upload_container(path, caption, chk, cover_url=cover_url)
        log(f"Instagram container {cid}: {status}")
        if status == "FINISHED":
            fmt = label
            break
        if codec != "h264" and len(attempts) == 1:
            log("Instagram rejected the original; re-encoding to H.264 + AAC for Instagram only")
            attempts.append(("h264/aac (re-encoded)", to_h264(video)))
        cid = None
    if not cid:
        raise meta.Fail("Instagram could not process the video in any format")
    r = meta.instagram_publish_container(cid, chk)       # PublishAttempted from here on
    return {"remote_id": r["id"], "url": r["url"], "format": fmt, "product_type": r.get("product_type"),
            "cover": {"status": "requested", "asset": str(cover_path.relative_to(mf.ROOT)), "source": "tiktok_original"}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", type=Path, required=True, help="worktree of the state branch")
    ap.add_argument("--discover", action="store_true", help="read-only discovery with the stored TikTok token")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--daily-limit", type=int, default=None,
                    help="Maximum distinct historical source TikToks per UTC day; fresh posts exempt")
    ap.add_argument("--video-id", help="Publish only this source id to unfinished destinations")
    a = ap.parse_args(argv)

    store = StateStore(a.state_dir.resolve())
    m = store.load()
    if a.discover:
        discover_readonly(m)
        if not a.dry_run:
            store.push(m, "state: discovery")
    if not m.get("videos"):
        log("FAIL: the manifest on the state branch is empty (run with --discover)")
        return 1

    if a.daily_limit is not None and a.daily_limit < 1:
        ap.error("--daily-limit must be positive")
    if a.video_id:
        entry = m["videos"].get(a.video_id)
        if entry is None:
            ap.error("Unknown --video-id")
        jobs = {name: (a.video_id, entry) for name in DESTS
                if dest(entry, name)["status"] in ("pending", "failed")}
    else:
        jobs = pick_jobs(m, daily_limit=a.daily_limit)
    if not jobs:
        log("Nothing to post: every destination is published or waiting on a manual check")
        return 0

    summary = ", ".join(f"{name}={vid}" for name, (vid, _entry) in jobs.items())
    log(f"Queue: {summary}")
    if a.dry_run:
        log("Dry run: nothing downloaded or posted")
        return 0

    # A run may need two different source videos. Cache acquisition by TikTok id so when both
    # destinations need the same video we still download/validate it only once.
    acquired: dict[str, tuple[Path, dict]] = {}
    failures = 0

    for name in DESTS:
        if name not in jobs:
            log(f"{name}: no eligible backlog item")
            continue
        vid, entry = jobs[name]
        d = dest(entry, name)
        log(f"{name}: selected {vid} ({entry['source'].get('created_at')})")

        if vid not in acquired:
            result = media.acquire(entry)
            store.push(m, f"state: media {vid} {entry['media']['status']}")
            if entry["media"]["status"] != "ready":
                log(f"{name}: FAIL media for {vid}: {entry['media'].get('last_error')}")
                failures += 1
                continue
            info = entry["media"]["info"]
            video = mf.ROOT / entry["media"]["path"]
            acquired[vid] = (video, info)
            log(f"Media {result}: {info['width']}x{info['height']} {info['video_codec']} {info['duration']} s, "
                f"audio {'YES' if info['audio'] else 'NO'}")
        video, info = acquired[vid]

        d.update(status="publishing", attempts=int(d.get("attempts") or 0) + 1, last_error=None,
                 started_at=mf.now())
        try:
            store.push(m, f"state: {vid} {name} publishing")
        except StatePushError as e:
            log(f"FAIL: could not record 'publishing' for {name} ({e}); NOT uploading")
            if a.video_id:
                raise
            return 1
        try:
            r = post_youtube(vid, entry, video) if name == "youtube" else post_instagram(vid, entry, video, info)
        except Exception as e:  # noqa: BLE001
            from pipeline.social.meta import PublishAttempted
            from pipeline.social.youtube import UploadStarted
            msg = media.sanitize(e)
            if isinstance(e, (UploadStarted, PublishAttempted)):
                d.update(last_error=f"needs manual check: {msg}")       # stays 'publishing'
                log(f"{name}: UNCERTAIN, left on 'publishing' for a manual check: {msg}")
            else:
                d.update(status="failed", last_error=msg)
                log(f"{name}: FAILED before anything went live (retryable): {msg}")
            failures += 1
            store.push(m, f"state: {vid} {name} {d['status']}")
            continue
        d.update(status="published", remote_id=r["remote_id"], url=r["url"], published_at=mf.now(),
                 last_error=None, format=r.get("format"))
        if r.get("cover"):
            d["cover"] = r["cover"]
            if r["cover"]["status"] != "set":
                log(f"WARNING {name}: cover failed, video remains published: {r['cover'].get('error')}")
        if r.get("privacy"):
            d["privacy"] = r["privacy"]
        store.push(m, f"state: {vid} {name} published")
        log(f"{name}: PUBLISHED {r['url']} (id {r['remote_id']}, format {r.get('format')})")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
