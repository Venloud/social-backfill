"""Checkpointed manual loop; consecutive hosted jobs resume one session."""
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
    return m.setdefault("publish_loops", {}).setdefault(run_id, {
        "target": target, "completed_ids": [], "fresh_ids": [],
        "active_old": None, "next_old_at": now, "started_at": mf.now(), "status": "running",
    })

def reconcile(m, session):
    vid = session.get("active_old")
    if vid and complete(m["videos"][vid]):
        if vid not in session["completed_ids"]:
            session["completed_ids"].append(vid)
        session["active_old"] = None
    if len(session["completed_ids"]) >= session["target"]:
        session["status"] = "completed"
    return session["status"] == "completed"

def choose_old(m, session, now):
    if session.get("active_old"):
        return session["active_old"]
    if now < session["next_old_at"]:
        return None
    for vid, entry in mf.ordered(m):
        if vid not in session["fresh_ids"] and not complete(entry) and retryable(entry):
            return vid
    return None

def run(state_dir, run_id, target=20, segment_seconds=20400, interval=4320,
        poll_seconds=300, verify_only=False):
    store = StateStore(state_dir.resolve())
    m = store.load()
    session = session_for(m, run_id, time.time(), target)
    reconcile(m, session)
    if verify_only:
        if session["status"] != "completed":
            log(f"INCOMPLETE: {len(session['completed_ids'])}/{session['target']} old videos; resume required")
            return 1
        log(f"LOOP COMPLETE: {len(session['completed_ids'])}/{session['target']} old videos")
        return 0
    store.push(m, "state: loop started or resumed")
    if session["status"] == "completed":
        log("Loop already completed; no uploads")
        return 0
    deadline = time.monotonic() + segment_seconds
    next_poll = 0.0
    while time.monotonic() < deadline:
        m = store.load()
        session = m["publish_loops"][run_id]
        if reconcile(m, session):
            store.push(m, "state: loop completed")
            log("LOOP COMPLETE")
            return 0
        now = time.time()
        poll_due = now >= next_poll
        if poll_due:
            previous = set(m.get("videos", {}))
            discover_readonly(m)
            session["fresh_ids"] = sorted(set(session["fresh_ids"]) | (set(m["videos"]) - previous))
            store.push(m, "state: loop discovery")
            next_poll = now + poll_seconds
            for vid, entry in mf.ordered(m):
                if vid in session["fresh_ids"] and retryable(entry):
                    log(f"NEW POST: {vid}; publishing unfinished destinations")
                    publish_one(["--state-dir", str(state_dir), "--video-id", vid])
            m = store.load()
            session = m["publish_loops"][run_id]
        vid = choose_old(m, session, time.time())
        if vid and (poll_due or session["active_old"] is None):
            if session["active_old"] is None:
                session["active_old"] = vid
                session["next_old_at"] = time.time() + interval
                store.push(m, f"state: loop reserves old video {vid}")
            if retryable(m["videos"][vid]):
                log(f"OLD POST: {vid}; count {len(session['completed_ids'])}/{session['target']}")
                publish_one(["--state-dir", str(state_dir), "--video-id", vid])
            m = store.load()
            session = m["publish_loops"][run_id]
            finished = reconcile(m, session)
            store.push(m, "state: loop progress")
            log(f"Loop count: {len(session['completed_ids'])}/{session['target']} old videos")
            if finished:
                log("LOOP COMPLETE")
                return 0
        wake = next_poll
        if session["active_old"] is None and session["next_old_at"] > time.time():
            wake = min(wake, session["next_old_at"])
        time.sleep(max(0.1, min(60, wake - time.time(), deadline - time.monotonic())))
    log("HANDOFF: loop still running; next job resumes the saved count and interval")
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", type=Path, required=True)
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    ap.add_argument("--target", type=int, default=20)
    ap.add_argument("--segment-seconds", type=int, default=20400)
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args(argv)
    if not a.run_id or a.target < 1 or a.segment_seconds < 1:
        ap.error("run-id, positive target and positive segment-seconds are required")
    return run(a.state_dir, a.run_id, a.target, a.segment_seconds, verify_only=a.verify_only)

if __name__ == "__main__":
    raise SystemExit(main())
