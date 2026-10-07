"""MEDIA ACQUISITION with yt-dlp (Python API) + ffprobe validation.

The manifest (from TikTok video.list) decides WHAT to download; yt-dlp fetches exactly that one post URL
(no profile scraping, no playlists, no cookies). Files are named media/<tiktok video id>.mp4.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pipeline.manifest import ROOT, now

MEDIA_DIR = ROOT / "media"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SECRETISH = re.compile(r"(access_token|refresh_token|client_secret|authorization|cookie)[=:]\s*\S+", re.I)


def sanitize(msg: str) -> str:
    msg = SECRETISH.sub(r"\1=<redacted>", ANSI.sub("", str(msg)))
    return " ".join(msg.split())[:400]


def probe(path: Path) -> dict:
    """Validate with ffprobe. Raises ValueError with a readable reason; returns safe metadata."""
    if not path.exists():
        raise ValueError("file does not exist")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("file is empty")
    r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise ValueError(f"ffprobe cannot read the container: {r.stderr.strip()[:200]}")
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if not video:
        raise ValueError("no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or video.get("duration") or 0)
    except ValueError:
        duration = 0.0
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if duration <= 0:
        raise ValueError("duration is 0")
    if width <= 0 or height <= 0:
        raise ValueError(f"bad frame size {width}x{height}")
    return {"container": fmt.get("format_name"), "duration": round(duration, 3), "width": width, "height": height,
            "video_codec": video.get("codec_name"), "audio": bool(audio),
            "audio_codec": audio.get("codec_name") if audio else None, "size_bytes": size}


def final_path(video_id: str) -> Path:
    return MEDIA_DIR / f"{video_id}.mp4"


def download(url: str, video_id: str) -> Path:
    """Download ONE post with yt-dlp into a temp dir, then move it to media/<id>.mp4."""
    import yt_dlp  # imported lazily so discovery/status work without it

    if not re.fullmatch(r"\d+", video_id):
        raise ValueError("TikTok video id must be numeric")
    if not url or video_id not in url:
        raise ValueError("post URL is missing or does not belong to this video id")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"dl-{video_id}-") as tmp:
        opts = {
            "outtmpl": str(Path(tmp) / f"{video_id}.%(ext)s"),
            # Best MP4 with audio as delivered (no re-encode), never TikTok's watermarked "download"
            # rendition; merge/remux to MP4 with ffmpeg only if needed.
            "format": ("b[ext=mp4][vcodec!=none][acodec!=none][format_id!=download]"
                       "/bv*[ext=mp4]+ba[ext=m4a]/b[format_id!=download]/bv*+ba/b"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "playlist_items": "1",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 3,
            "socket_timeout": 30,
            "cachedir": False,
            "writeinfojson": False,
            "writethumbnail": False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        got = str((info or {}).get("id") or "")
        if got and got != video_id:
            raise ValueError(f"yt-dlp returned a different post ({got})")
        files = sorted(Path(tmp).glob(f"{video_id}.*"), key=lambda p: p.suffix != ".mp4")
        files = [f for f in files if f.suffix not in (".part", ".ytdl", ".json")]
        if not files:
            raise ValueError("yt-dlp finished but produced no file")
        src = files[0]
        if src.suffix != ".mp4":   # last resort remux (stream copy, no re-encode)
            mp4 = src.with_suffix(".mp4")
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c", "copy", str(mp4)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise ValueError(f"ffmpeg remux failed: {r.stderr.strip()[:200]}")
            src = mp4
        dest = final_path(video_id)
        shutil.move(str(src), dest)
    return dest


def acquire(entry: dict, force: bool = False) -> str:
    """Make sure this manifest entry has a validated local MP4. Updates entry['media'] in place.
    Returns 'skipped' (valid file already there), 'ready' or 'failed'."""
    vid = entry["source"]["video_id"]
    media = entry["media"]
    path = final_path(vid)
    if not force and path.exists():
        try:
            info = probe(path)
            media.update(status="ready", path=str(path.relative_to(ROOT)), validated=True, last_error=None, info=info)
            return "skipped"
        except ValueError:
            pass   # broken leftover: download again
    if media.get("status") == "ready" and not path.exists():
        media.update(status="pending", validated=False)   # file lived on another runner
    media["attempts"] = int(media.get("attempts") or 0) + 1
    media["status"] = "downloading"
    try:
        path = download(entry["source"].get("url"), vid)
        media["downloaded_at"] = now()
        media["info"] = probe(path)
    except Exception as e:  # noqa: BLE001 - recorded in the manifest, retryable
        media.update(status="failed", validated=False, last_error=sanitize(e), info=None)
        path.unlink(missing_ok=True)   # never leave an unvalidated file behind
        return "failed"
    media.update(status="ready", validated=True, path=str(path.relative_to(ROOT)), last_error=None)
    return "ready"
