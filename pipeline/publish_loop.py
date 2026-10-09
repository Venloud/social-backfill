"""Scheduled batches with shared quota and saved pacing."""
from __future__ import annotations
import argparse
import os
import time
from pathlib import Path
from pipeline import manifest as mf
from pipeline.publish_one import DESTS, dest, discover_readonly, main as publish_one, log
from pipeline.state_store import StateStore

def published_in_window(entry, started):
    """A source counts once when either destination was published in this window."""
    from datetime import datetime
    for name in DESTS:
        d = dest(entry, name)
        if d["status"] != "published" or not d.get("published_at"):
            continue
        try:
            if datetime.fromisoformat(d["published_at"].replace("Z", "+00:00")).timestamp() >= started:
                return True
        except ValueError:
            continue
    return False


def session_for(m, run_id, now, target):
    session = m.setdefault("publish_loops", {}).setdefault("scheduled", {})
    session.setdefault("window_started", now)
    if now >= session["window_started"] + 86400:
        session["window_started"] = now
        session["completed_ids"] = []
        session["run_completed"] = {}
        session["fresh_ids"] = []
    session.setdefault("completed_ids", [])
    session.setdefault("fresh_ids", [])
    session.setdefault("run_completed", {})
    session.setdefault("next_old_at", now)
    session["target"] = target
    session["run_completed"].setdefault(run_id, [])
    # Migrate the old single-video reservation, which could remain locked forever
    # when one platform had no eligible backlog item.
    session.pop("active_old", None)
    return session


def reconcile(m, session, run_id):
    for vid, entry in mf.ordered(m):
        if published_in_window(entry, session["window_started"]):
            if vid not in session["completed_ids"]:
                session["completed_ids"].append(vid)
                session["run_completed"][run_id].append(vid)
    session["status"] = "completed" if len(session["completed_ids"]) >= session["target"] else "running"
    return session["status"] == "completed"


def run(state_dir, run_id, target=20, segment_seconds=18900, interval=4320,
        poll_seconds=300, max_posts=5, upload_margin=120):
    store = StateStore(state_dir.resolve())
    deadline = time.monotonic() + segment_seconds
    log(f'SCHEDULER: segment={segment_seconds}s, upload_margin={upload_margin}s, interval={interval}s')
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
            log(f'SKIP: remaining run time below upload margin ({upload_margin}s)')
            break
        now = time.time()
        if now >= next_poll:
            previous = set(m.get("videos", {}))
            discover_readonly(m)
            session["fresh_ids"] = sorted(set(session["fresh_ids"]) | (set(m["videos"]) - previous))
            store.push(m, "state: scheduled loop discovery")
            next_poll = now + poll_seconds
        if now >= session["next_old_at"]:
            # Let publish_one select independent queues per platform. Do not pin
            # the entire scheduler to a source whose Instagram side is blocked.
            log(f"POST SLOT: {count}/{max_posts} this run; independent destination queues")
            result = publish_one(["--state-dir", str(state_dir), "--daily-limit", str(target)])
            m = store.load()
            session = session_for(m, run_id, time.time(), target)
            reconcile(m, session, run_id)
            session["next_old_at"] = time.time() + interval
            store.push(m, "state: scheduled upload checkpoint")
            log(f"POST SLOT RESULT: {'success' if result == 0 else 'partial failure'}; "
                f"next slot in {interval // 60} minutes; "
                f"{len(session['run_completed'][run_id])}/{max_posts} newly completed")
            continue
        wait = max(0, session["next_old_at"] - now)
        log(f"WAIT: next publishing slot in {int(wait)} seconds; "
            f"{count}/{max_posts} newly completed this run")
        time.sleep(max(0.1, min(60, wait, deadline - time.monotonic())))
    log("BATCH ENDED: saved progress; next scheduled run resumes.")
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", type=Path, required=True)
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    ap.add_argument("--target", type=int, default=20)
    ap.add_argument("--max-posts", type=int, default=5)
    ap.add_argument("--segment-seconds", type=int, default=18900)
    ap.add_argument("--interval-seconds", type=int, default=780)
    a = ap.parse_args(argv)
    if not a.run_id or min(a.target, a.max_posts, a.segment_seconds) < 1:
        ap.error("run-id and positive limits are required")
    return run(a.state_dir, a.run_id, a.target, a.segment_seconds, interval=a.interval_seconds, max_posts=a.max_posts)

if __name__ == "__main__":
    raise SystemExit(main())
