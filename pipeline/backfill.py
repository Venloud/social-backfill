"""Backfill CLI: DISCOVER (TikTok video.list) -> CHECKPOINT (manifest) -> DOWNLOAD (yt-dlp) -> VALIDATE (ffprobe).

    python -m pipeline.backfill discover [--dry-run]
    python -m pipeline.backfill download --limit 1 [--video-id ID] [--force] [--dry-run]
    python -m pipeline.backfill status

Nothing is published. Output is safe: ids, dates, statuses and media facts only, never tokens.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline import manifest as mf

KNOWN_TEST_IDS = ("7693970155247799565", "7693941087366434062", "7693935976359513358",
                  "7693931336926530829", "7693930008787586318")   # test data only, never used for discovery


def cmd_discover(a) -> int:
    from pipeline import tiktok_auth, tiktok_discovery
    try:
        found = tiktok_discovery.discover()["videos"]
    except tiktok_auth.TikTokAuthError as e:
        print(f"FAIL: TikTok discovery: {e}")
        return 1
    m = mf.load(a.manifest)
    counts = mf.merge(m, found)
    print(f"TikTok discovery: PASS ({len(found)} public videos returned by video.list)")
    for v in sorted(found, key=lambda v: int(v.get("create_time") or 0)):
        print(f"  {v['id']}  {mf.iso(v.get('create_time'))}  {v.get('duration', '?')} s")
    known = [i for i in KNOWN_TEST_IDS if i in m["videos"]]
    print(f"Known test videos present: {len(known)}/{len(KNOWN_TEST_IDS)}")
    print(f"Manifest: {counts['added']} added, {counts['updated']} refreshed, {counts['total']} total")
    if a.dry_run:
        print("Dry run: manifest NOT written")
    else:
        mf.save(m, a.manifest)
        print(f"Manifest written: {a.manifest.relative_to(mf.ROOT) if a.manifest.is_relative_to(mf.ROOT) else a.manifest}")
    return 0


def cmd_download(a) -> int:
    from pipeline import media
    m = mf.load(a.manifest)
    if not m["videos"]:
        print("FAIL: manifest is empty; run 'discover' first")
        return 1
    if a.video_id:
        if a.video_id not in m["videos"]:
            print(f"FAIL: {a.video_id} is not in the manifest (only discovered videos can be downloaded)")
            return 1
        todo = [(a.video_id, m["videos"][a.video_id])]
    else:
        todo = [(vid, e) for vid, e in mf.ordered(m)
                if a.force or not (e["media"].get("status") == "ready" and media.final_path(vid).exists())]
    todo = todo[: a.limit]
    if not todo:
        print("Nothing to download")
        return 0
    failures = 0
    for vid, entry in todo:
        if a.dry_run:
            print(f"Would download {vid} ({entry['source'].get('created_at')})")
            continue
        result = media.acquire(entry, force=a.force)
        mf.save(m, a.manifest)       # checkpoint after every video
        info = entry["media"].get("info") or {}
        if result == "failed":
            failures += 1
            print(f"{vid}: FAIL (attempt {entry['media']['attempts']}): {entry['media']['last_error']}")
        else:
            print(f"{vid}: {result.upper()} {entry['media']['path']} | {info.get('container')} "
                  f"{info.get('width')}x{info.get('height')} {info.get('video_codec')} "
                  f"{info.get('duration')} s | audio {'YES' if info.get('audio') else 'NO'} | "
                  f"{info.get('size_bytes')} bytes")
    return 1 if failures else 0


def cmd_status(a) -> int:
    m = mf.load(a.manifest)
    vids = mf.ordered(m)
    ready = sum(1 for _, e in vids if e["media"].get("status") == "ready")
    print(f"Manifest: {len(vids)} videos, {ready} media ready, updated {m.get('updated_at')}")
    for vid, e in vids:
        d = e.get("destinations", {})
        print(f"  {vid}  {e['source'].get('created_at')}  media={e['media'].get('status')}"
              f"(attempts {e['media'].get('attempts', 0)})  "
              + "  ".join(f"{k}={v.get('status')}" for k, v in sorted(d.items())))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=mf.DEFAULT_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="list public TikTok videos and sync the manifest")
    d.add_argument("--dry-run", action="store_true")
    dl = sub.add_parser("download", help="download + validate media for discovered videos")
    dl.add_argument("--limit", type=int, default=1)
    dl.add_argument("--video-id")
    dl.add_argument("--force", action="store_true")
    dl.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="show manifest status")
    a = ap.parse_args(argv)
    a.manifest = a.manifest.resolve()
    return {"discover": cmd_discover, "download": cmd_download, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
