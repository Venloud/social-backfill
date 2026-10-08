"""Scheduled batches with shared quota and saved pacing."""
from __future__ import annotations
import argparse
import os
import time
from pathlib import Path
from pipeline import manifest as mf
from pipeline.publish_one import DESTS, dest, discover_readonly, main as publish_one, log
from pipeline.state_store import StateStore

def complete(entry):
    return all(dest(entry, name)["status"] == "published" for name in DESTS)

def retryable(entry):
    return any(dest(entry, name)["status"] in ("pending", "failed") for name in DESTS)

def session_for(m, run_id, now, target):
    session = m.setdefault("publish_loops", {}).setdefault("scheduled", {
        "target": target, "completed_ids": [], "fresh_ids": [],
        "active_old": None, "next_old_at": now, "window_started": now,
        "run_completed": {}, "status": "running",
    })
    if now >= session["window_started"] + 86400:
        session["completed_ids"] = []
        session["window_started"] = now
        session["run_completed"] = {}
    session["target"] = target
    session["run_completed"].setdefault(run_id, [])
    return session

def reconcile(m, session, run_id):
    vid = session.get("active_old")
    if vid and complete(m["videos"][vid]):
        if vid not in session["completed_ids"]:
            session["completed_ids"].append(vid)
            session["run_completed"][run_id].append(vid)
        session["active_old"] = None
    session["status"] = "completed" if len(session["completed_ids"]) >= session["target"] else "running"
    return session["status"] == "completed"

def choose_video(m, session, now):
    if session.get("active_old"):
        return session["active_old"]
    for vid, entry in mf.ordered(m):
        if vid in session["fresh_ids"] and not complete(entry) and retryable(entry):
            return vid
    if now < session["next_old_at"]:
        return None
    for vid, entry in mf.ordered(m):
        if not complete(entry) and retryable(entry):
            return vid
    return None

def run(state_dir, run_id, target=20, segment_seconds=18900, interval=4320,
        poll_seconds=300, max_posts=5, upload_margin=900):
    store = StateStore(state_dir.resolve())
    deadline = time.monotonic() + segment_seconds
    next_poll = 0.0
    while time.monotonic() < deadline:
        m = store.load()
        session = session_for(m, run_id, time.time(), target)
        finished = reconcile(m, session, run_id)
        store.push(m, "state: scheduled loop progress")
        count = len(session["run_completed"][run_id])
        if finished or count >= max_posts:
            log(f"BATCH ENDED: {count}/{max_posts} this run; {len(session['completed_ids'])}/{target} in the 24-hour window")
            return 0
        if deadline - time.monotonic() < upload_margin:
            break
        now = time.time()
        poll_due = now >= next_poll
        if poll_due:
            previous = set(m.get("videos", {}))
            discover_readonly(m)
            session["fresh_ids"] = sorted(set(session["fresh_ids"]) | (set(m["videos"]) - previous))
            store.push(m, "state: scheduled loop discovery")
            next_poll = now + poll_seconds
        vid = choose_video(m, session, time.time())
        if vid and (poll_due or session["active_old"] is None):
            if session["active_old"] is None:
                session["active_old"] = vid
                # New posts replace old slots and postpone the next old upload.
                session["next_old_at"] = time.time() + interval
                store.push(m, f"state: loop reserves video {vid}")
            if retryable(m["videos"][vid]):
                kind = "NEW" if vid in session["fresh_ids"] else "OLD"
                log(f"{kind} POST: {vid}; shared count {len(session['completed_ids'])}/{target}")
                publish_one(["--state-dir", str(state_dir), "--video-id", vid])
            m = store.load()
            session = m["publish_loops"]["scheduled"]
            reconcile(m, session, run_id)
            store.push(m, "state: scheduled upload checkpoint")
            continue
        wake = next_poll
        if session["active_old"] is None and session["next_old_at"] > time.time():
            wake = min(wake, session["next_old_at"])
        time.sleep(max(0.1, min(60, wake - time.time(), deadline - time.monotonic())))
    log("BATCH ENDED: saved progress; next scheduled run resumes. The 20-video window may still be incomplete.")
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", type=Path, required=True)
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    ap.add_argument("--target", type=int, default=20)
    ap.add_argument("--max-posts", type=int, default=5)
    ap.add_argument("--segment-seconds", type=int, default=18900)
    a = ap.parse_args(argv)
    if not a.run_id or min(a.target, a.max_posts, a.segment_seconds) < 1:
        ap.error("run-id and positive limits are required")
    return run(a.state_dir, a.run_id, a.target, a.segment_seconds, max_posts=a.max_posts)

if __name__ == "__main__":
    raise SystemExit(main())
