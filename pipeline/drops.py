"""Phone drops: videos uploaded with docs/index.html land on the `drops` branch and jump ahead of TikTok backfill.

Branch layout (written by the page, read-only here):
    drops/<id>.mp4   the video
    drops/<id>.json  {"caption": "...", "created_at": "..."}
    drops/<id>.jpg   optional cover frame grabbed by the page

The workflow checks the branch out at .drops (or $DROPS_DIR). Each drop becomes one manifest entry
`drop-<id>` that goes through the normal write-ahead publishing for YouTube and Instagram.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
from pathlib import Path

from pipeline import manifest as mf

REPO = "Venloud/social-backfill"
BRANCH = "drops"
DROP_ID = re.compile(r"[0-9A-Za-z-]{1,64}")


def worktree() -> Path:
    return Path(os.environ.get("DROPS_DIR") or mf.ROOT / ".drops")


def is_drop(entry: dict) -> bool:
    return entry.get("source", {}).get("platform") == "drop"


def import_drops(m: dict, root: Path | None = None) -> int:
    """Add drops not yet in the manifest. Existing entries (and their publish state) are never touched."""
    folder = (root or worktree()) / "drops"
    if not folder.is_dir():
        return 0
    videos = m.setdefault("videos", {})
    added = 0
    for meta in sorted(folder.glob("*.json")):
        did = meta.stem
        vid = f"drop-{did}"
        if not DROP_ID.fullmatch(did) or vid in videos or not (folder / f"{did}.mp4").is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stamp = mf.now()
        has_cover = (folder / f"{did}.jpg").is_file()
        videos[vid] = {
            "source": {
                "platform": "drop", "account": "drop", "video_id": vid, "url": None,
                "file": f"drops/{did}.mp4", "cover": f"drops/{did}.jpg" if has_cover else None,
                "created_at": str(data.get("created_at") or stamp), "first_seen_at": stamp, "last_seen_at": stamp,
                "metadata": {
                    "video_description": str(data.get("caption") or ""),
                    "cover_image_url": (f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/drops/{did}.jpg"
                                        if has_cover else None),
                },
            },
            "media": copy.deepcopy(mf.MEDIA_TEMPLATE),
            "destinations": {d: copy.deepcopy(mf.DEST_TEMPLATE) for d in mf.DESTINATIONS},
        }
        added += 1
    return added


def copy_media(entry: dict, dest: Path) -> Path:
    src = worktree() / entry["source"]["file"]
    if not src.is_file():
        raise ValueError(f"drop file {entry['source']['file']} is not on this runner (drops branch not checked out?)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest


def cover_file(entry: dict) -> Path | None:
    rel = entry.get("source", {}).get("cover")
    path = worktree() / rel if rel else None
    return path if path and path.is_file() else None
