"""Turn a TikTok caption into YouTube / Instagram text."""
from __future__ import annotations

import re

# Reach-bait tags that mean nothing off TikTok (#fyp, #foryou, #fypシ゚viral, #viral, #xyzbca ...).
FYP_TAG = re.compile(r"(?<!\S)#(?:fyp|foryou|fy\b|fypage|viral|xyzbca|trending|parati|pourtoi|tiktok|capcut)[^\s#]*",
                     re.IGNORECASE)


def caption_of(source: dict) -> str:
    md = source.get("metadata") or {}
    return (md.get("video_description") or md.get("title") or "").strip()


def clean_caption(caption: str) -> str:
    text = FYP_TAG.sub("", caption or "")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif out and not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def first_sentence(text: str, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    m = re.search(r"^(.+?[.!?…])(?:\s|$)", text)
    s = (m.group(1) if m else text).strip()
    if len(s) > limit:
        cut = s[:limit].rsplit(" ", 1)[0]
        s = (cut if len(cut) >= limit // 2 else s[:limit]).rstrip(" ,;:-")
    return s


def youtube_text(source: dict, video_id: str) -> tuple[str, str, list[str]]:
    """(title, description, tags). YouTube rejects '<' and '>' in titles/descriptions."""
    clean = clean_caption(caption_of(source)).replace("<", "").replace(">", "")
    first = first_sentence(clean.split("\n")[0] if clean else "")
    if not first:
        first = "Just me yapping"
    title = f"{first} #shorts"
    description = clean
    tags = [w.lstrip("#") for w in clean.split() if w.startswith("#") and len(w) > 1][:15]
    return title, description, tags


def instagram_text(source: dict) -> str:
    return clean_caption(caption_of(source))[:2200]
