"""Persistent checkpoint manifest, one entry per TikTok video keyed by the stable TikTok video id.

Discovery only adds new entries and refreshes source metadata. It never resets destination status and never
resets media status (except a 'ready' entry whose file is gone, which media.py handles at download time).
Writes are atomic (temp file + os.replace).
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "state" / "manifest.json"
SCHEMA_VERSION = 1
DESTINATIONS = ("youtube", "instagram", "snapchat")

MEDIA_TEMPLATE = {"status": "pending", "path": None, "downloaded_at": None, "validated": False,
                  "last_error": None, "attempts": 0, "info": None}
DEST_TEMPLATE = {"status": "pending", "remote_id": None, "published_at": None, "attempts": 0, "last_error": None}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: Path = DEFAULT_PATH) -> dict:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("videos", {})
        return data
    return {"schema_version": SCHEMA_VERSION, "updated_at": None, "videos": {}}


def save(manifest: dict, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["updated_at"] = now()
    fd, tmp = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def canonical_url(video: dict) -> str | None:
    """Public post URL for yt-dlp: the API's share_url without tracking parameters."""
    share = video.get("share_url")
    return share.split("?", 1)[0] if share else None


def iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def merge(manifest: dict, videos: list[dict]) -> dict:
    """Add new videos, refresh metadata of known ones. Returns counts {'added', 'updated', 'total'}."""
    added = updated = 0
    stamp = now()
    entries = manifest.setdefault("videos", {})
    for v in videos:
        vid = str(v["id"])
        source = {"platform": "tiktok", "video_id": vid, "url": canonical_url(v),
                  "created_at": iso(v.get("create_time")), "metadata": {k: x for k, x in v.items() if k != "id"},
                  "last_seen_at": stamp}
        entry = entries.get(vid)
        if entry is None:
            source["first_seen_at"] = stamp
            entries[vid] = {"source": source, "media": copy.deepcopy(MEDIA_TEMPLATE),
                            "destinations": {d: copy.deepcopy(DEST_TEMPLATE) for d in DESTINATIONS}}
            added += 1
            continue
        old = entry.setdefault("source", {})
        source["first_seen_at"] = old.get("first_seen_at") or stamp
        if not source["url"]:
            source["url"] = old.get("url")      # keep a known URL if TikTok omitted it this time
        old.update(source)
        # Fill only MISSING keys; existing media/destination state is never overwritten.
        media = entry.setdefault("media", {})
        for k, val in MEDIA_TEMPLATE.items():
            media.setdefault(k, copy.deepcopy(val))
        dests = entry.setdefault("destinations", {})
        for d in DESTINATIONS:
            slot = dests.setdefault(d, copy.deepcopy(DEST_TEMPLATE))
            for k, val in DEST_TEMPLATE.items():
                slot.setdefault(k, val)
        updated += 1
    return {"added": added, "updated": updated, "total": len(entries)}


def ordered(manifest: dict) -> list[tuple[str, dict]]:
    """Oldest first (backfill order); ties broken by id."""
    return sorted(manifest.get("videos", {}).items(),
                  key=lambda kv: (kv[1].get("source", {}).get("created_at") or "", kv[0]))
