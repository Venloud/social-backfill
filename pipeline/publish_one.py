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
import time
import subprocess
import sys
from pathlib import Path

from pipeline import captions, manifest as mf, media
from pipeline.state_store import StatePushError, StateStore

DESTS = ("youtube", "instagram")

# Conservative platform-specific pacing; checked on every scheduled invocation.
# These limits are not YouTube's unpublished channel quota.
PLATFORM_INTERVAL_SECONDS = {"youtube": 6 * 3600, "instagram": 3600}
YOUTUBE_LIMIT_COOLDOWN_SECONDS = 24 * 3600


def _timestamp(value):
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def platform_due(m: dict, name: str, now: float | None = None) -> tuple[bool, float]:
    """Persist scheduling across runs; migrate an existing YouTube upload-limit failure."""
    now = time.time() if now is None else now
    state = m.get("publish_pacing", {}).get(name, {})
    due_at = max(float(state.get("next_attempt_at") or 0),
                 float(state.get("cooldown_until") or 0))
    if name == "youtube":
        # Existing state predates the cooldown field. Don't hammer YouTube on deployment.
        for entry in m.get("videos", {}).values():
            d = entry.get("destinations", {}).get("youtube", {})
            if "uploadlimitexceeded" in str(d.get("last_error") or "").lower():
                due_at = max(due_at, _timestamp(d.get("started_at")) + YOUTUBE_LIMIT_COOLDOWN_SECONDS)
    return now >= due_at, due_at


def log(msg: str) -> None:
    print(msg, flush=True)


def discover_readonly(m: dict) -> None:
    """Read both authorized TikTok sources and merge into one destination queue.

    OAuth refresh happens in the workflow, not here. A failed source must not
    stop discovery or posting from the other source.
    """
    from pipeline import tiktok_auth, tiktok_discovery
    old_ids = set(m.get("videos", {}))
    for account, token_env in (("primary", "TIKTOK_ACCESS_TOKEN"),
                               ("secondary", "TIKTOK_ACCESS_TOKEN_2")):
        tok = os.environ.get(token_env, "")
        if not tok:
            log(f"Discovery skipped for {account}: no refreshed TikTok token")
            continue
        tiktok_auth.mask(tok)
        try:
            videos = tiktok_discovery.list_videos(tok)
            counts = mf.merge(m, videos, source_account=account)
        except (tiktok_auth.TikTokAuthError, ValueError) as e:
            log(f"Discovery failed for {account} ({str(e)[:160]}); keeping saved manifest")
            continue
        for vid in set(m["videos"]) - old_ids:
            if m["videos"][vid]["source"].get("account") == account:
                m["videos"][vid]["source"]["fresh_priority"] = True
        log(f"Discovery {account}: {len(videos)} public videos, {counts['added']} new, "
            f"{counts['total']} in shared manifest")
    log(f"Discovery complete: {len(set(m.get('videos', {})) - old_ids)} new total")


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
        if name == "youtube":
            # Scheduled uploads are limited to four published Shorts per UTC day.
            # Manual youtube-only tests remain explicitly operator-controlled.
            posted_today = sum(
                dest(entry, "youtube")["status"] == "published"
                and _day(dest(entry, "youtube").get("published_at")) == day
                for _, entry in ordered
            )
            if posted_today >= 4:
                log(f"youtube: daily publishing cap reached ({posted_today}/4 UTC); Instagram continues")
                continue
        due, due_at = platform_due(m, name)
        if not due:
            log(f"{name}: paused by pacing/cooldown; next eligible at "
                f"{datetime.fromtimestamp(due_at, timezone.utc).isoformat()}")
            continue
        fresh = [(vid, entry) for vid, entry in ordered if _fresh(entry, day)]
        historical = [(vid, entry) for vid, entry in ordered if not _fresh(entry, day)]
        # Alternate sources when both have eligible videos while keeping
        # oldest-first order within each account and prioritizing fresh posts.
        last_source = m.get("publish_pacing", {}).get(name, {}).get("last_source_account")
        if last_source in ("primary", "secondary"):
            fresh = sorted(fresh, key=lambda item: item[1].get("source", {}).get("account", "primary") == last_source)
            historical = sorted(historical, key=lambda item: item[1].get("source", {}).get("account", "primary") == last_source)
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
    ap.add_argument("--youtube-only", action="store_true", help="Manual YouTube-only upload for --video-id")
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
        jobs = {name: (a.video_id, entry) for name in (("youtube",) if a.youtube_only else DESTS)
                if dest(entry, name)["status"] in ("pending", "failed")}
    elif a.youtube_only:
        # Manual test: choose the next eligible YouTube item without scheduled pacing.
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ordered = mf.ordered(m)
        fresh = [(vid, entry) for vid, entry in ordered if _fresh(entry, day)]
        historical = [(vid, entry) for vid, entry in ordered if not _fresh(entry, day)]
        last_source = m.get("publish_pacing", {}).get("youtube", {}).get("last_source_account")
        if last_source in ("primary", "secondary"):
            fresh.sort(key=lambda item: item[1].get("source", {}).get("account", "primary") == last_source)
            historical.sort(key=lambda item: item[1].get("source", {}).get("account", "primary") == last_source)
        jobs = {}
        for vid, entry in fresh + historical:
            if dest(entry, "youtube")["status"] in ("pending", "failed"):
                jobs["youtube"] = (vid, entry)
                break
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
        # Checkpoint platform pacing and source rotation before attempting upload.
        pacing = m.setdefault("publish_pacing", {}).setdefault(name, {})
        pacing["next_attempt_at"] = time.time() + PLATFORM_INTERVAL_SECONDS[name]
        pacing["last_source_account"] = entry.get("source", {}).get("account", "primary")
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
            if name == "youtube" and "uploadlimitexceeded" in msg.lower():
                m.setdefault("publish_pacing", {}).setdefault("youtube", {})["cooldown_until"] = (
                    time.time() + YOUTUBE_LIMIT_COOLDOWN_SECONDS
                )
                log("YouTube: upload limit reached, pausing new YouTube attempts for 24 hours; "
                    "Instagram continues independently")
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
