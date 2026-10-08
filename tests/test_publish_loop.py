"""Loop pacing and checkpoint tests; no network calls or real waits."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch
from pipeline import publish_loop as loop
from pipeline import manifest as mf

def entry(stamp):
    return {"source": {"created_at": stamp}, "destinations": {
        name: {**mf.DEST_TEMPLATE, "status": "pending"} for name in loop.DESTS}}

class LoopTests(unittest.TestCase):
    def simulate(self, target, interval=4320, segment=86400, inject=False, partial=False):
        clock = [1000.0]
        data = {"videos": {str(i): entry(f"2026-01-{i+1:02d}") for i in range(target)}}
        posts = []
        polls = []
        class Store:
            def __init__(self, path): pass
            def load(self): return copy.deepcopy(data)
            def push(self, m, message):
                data.clear()
                data.update(copy.deepcopy(m))
        def publish(args):
            vid = args[-1]
            posts.append((vid, clock[0]))
            data["videos"][vid]["destinations"]["youtube"]["status"] = "published"
            if not partial or len([v for v, _ in posts if v == vid]) > 1:
                data["videos"][vid]["destinations"]["instagram"]["status"] = "published"
            return 0
        def discover(m):
            polls.append(clock[0])
            if inject and clock[0] >= 1600 and "fresh" not in m["videos"]:
                m["videos"]["fresh"] = entry("2026-12-01")
        def sleep(seconds): clock[0] += seconds
        return clock, data, posts, polls, Store, publish, discover, sleep

    def execute(self, fixture, target, segment=86400, run_id="run", max_posts=None):
        clock, data, posts, polls, Store, publish, discover, sleep = fixture
        with patch.object(loop, "StateStore", Store), patch.object(loop, "publish_one", publish), \
             patch.object(loop, "discover_readonly", discover), patch.object(loop, "log"), \
             patch.object(loop.time, "time", side_effect=lambda: clock[0]), \
             patch.object(loop.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(loop.time, "sleep", side_effect=sleep):
            return loop.run(Path("."), run_id, target=target, segment_seconds=segment,
                            max_posts=max_posts or target, upload_margin=0)

    def test_twenty_old_videos_are_72_minutes_apart(self):
        f = self.simulate(20)
        self.assertEqual(self.execute(f, 20), 0)
        times = [t for _, t in f[2]]
        self.assertEqual(len(times), 20)
        self.assertTrue(all(b-a == 4320 for a,b in zip(times,times[1:])))
        self.assertEqual(f[1]["publish_loops"]["scheduled"]["status"], "completed")
        self.assertTrue(all(b-a == 300 for a,b in zip(f[3],f[3][1:])))

    def test_fresh_post_counts_toward_shared_quota(self):
        f = self.simulate(2, inject=True)
        self.execute(f, 2)
        self.assertIn(("fresh", 1600), f[2])
        self.assertEqual(f[1]["publish_loops"]["scheduled"]["completed_ids"], ["0", "fresh"])

    def test_segment_resumes_count_and_pacing(self):
        f = self.simulate(2)
        self.execute(f, 2, segment=600)
        self.assertEqual(f[1]["publish_loops"]["scheduled"]["status"], "running")
        self.execute(f, 2)
        self.assertEqual(f[2], [("0", 1000), ("1", 5320)])

    def test_partial_success_counts_only_after_both_destinations(self):
        f = self.simulate(1, partial=True)
        self.execute(f, 1, segment=200)
        self.assertEqual(f[1]["publish_loops"]["scheduled"]["completed_ids"], [])
        self.execute(f, 1)
        self.assertEqual(f[1]["publish_loops"]["scheduled"]["completed_ids"], ["0"])

    def test_five_per_run_shared_twenty_across_runs(self):
        f = self.simulate(20)
        for index in range(4):
            f[0][0] = 1000 + index * 21600
            self.execute(f, 20, segment=18900, run_id=str(index), max_posts=5)
            self.assertEqual(len(f[2]), (index + 1) * 5)
        session = f[1]["publish_loops"]["scheduled"]
        self.assertEqual(session["status"], "completed")
        self.assertEqual(len(session["completed_ids"]), 20)
        self.assertEqual(len(set(v for v, _ in f[2])), 20)

    def test_run_retry_keeps_five_post_limit(self):
        f = self.simulate(10)
        self.execute(f, 20, max_posts=5)
        self.execute(f, 20, max_posts=5)
        self.assertEqual(len(f[2]), 5)

    def test_new_post_postpones_old_slot(self):
        f = self.simulate(3, inject=True)
        self.execute(f, 3)
        self.assertEqual(f[2], [("0", 1000), ("fresh", 1600), ("1", 5920)])

    def test_quota_rollover_keeps_pending_and_pacing(self):
        m = {"videos": {}}
        s = loop.session_for(m, "one", 1000, 20)
        s["completed_ids"] = ["done"]
        s["active_old"] = "partial"
        s["next_old_at"] = 90000
        resumed = loop.session_for(m, "two", 87400, 20)
        self.assertEqual(resumed["completed_ids"], [])
        self.assertEqual(resumed["active_old"], "partial")
        self.assertEqual(resumed["next_old_at"], 90000)
