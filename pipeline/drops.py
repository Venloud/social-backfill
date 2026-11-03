"""Phone drops: videos uploaded with docs/index.html land on the `drops` branch and jump ahead of TikTok backfill.

Branch layout (written by the page, read-only here):
    drops/<id>.mp4   the video
    drops/<id>.json  {"caption": "...", "created_at": "...", "parts": N (only for split videos)}
    drops/<id>.part000.mp4 ...  a video too big for one GitHub request, split in order; rejoined here
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
        if not DROP_ID.fullmatch(did) or vid in videos:
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        parts = data.get("parts")
        if isinstance(parts, int) and not isinstance(parts, bool) and 0 < parts <= 100:
            files = [f"drops/{did}.part{i:03d}.mp4" for i in range(parts)]
        else:
            files = [f"drops/{did}.mp4"]
        if not all((folder.parent / f).is_file() for f in files):
            continue
        stamp = mf.now()
        has_cover = (folder / f"{did}.jpg").is_file()
        videos[vid] = {
            "source": {
                "platform": "drop", "account": "drop", "video_id": vid, "url": None,
                "file": files[0] if len(files) == 1 else None, "parts": files if len(files) > 1 else None, "cover": f"drops/{did}.jpg" if has_cover else None,
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
    files = entry["source"].get("parts") or [entry["source"]["file"]]
    srcs = [worktree() / f for f in files]
    for src, rel in zip(srcs, files):
        if not src.is_file():
            raise ValueError(f"drop file {rel} is not on this runner (drops branch not checked out?)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:   # split uploads are rejoined in order into the original video
        for src in srcs:
            with open(src, "rb") as part:
                shutil.copyfileobj(part, out)
    return dest


def cover_file(entry: dict) -> Path | None:
    rel = entry.get("source", {}).get("cover")
    path = worktree() / rel if rel else None
    return path if path and path.is_file() else None
