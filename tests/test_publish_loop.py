"""Scheduled backfill unit tests, no network, uploads, or real waiting.

The scheduled workflow makes one publishing attempt per invocation; the cron
provides pacing. These tests do not assume the retired per-video pinning system.
"""
import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from pipeline import publish_loop as loop
from pipeline import manifest as mf


def entry(stamp):
    return {"source": {"created_at": stamp}, "destinations": {
        name: {**mf.DEST_TEMPLATE, "status": "pending"} for name in loop.DESTS}}


class LoopTests(unittest.TestCase):
    def fixture(self, n=3, failure=False):
        clock = [1000.0]
        data = {"videos": {str(i): entry(f"2026-01-{i+1:02d}") for i in range(n)}}
        attempts = []
        pushes = []

        class Store:
            def __init__(self, path):
                pass
            def load(self):
                return copy.deepcopy(data)
            def push(self, m, message):
                pushes.append(message)
                data.clear()
                data.update(copy.deepcopy(m))

        def publish(args):
            self.assertEqual(args[-2:], ["--daily-limit", str(data["publish_loops"]["scheduled"]["target"])])
            attempts.append((clock[0], tuple(args)))
            if failure:
                return 1
            for vid, item in sorted(data["videos"].items()):
                if item["destinations"]["youtube"]["status"] == "published":
                    continue
                timestamp = datetime.fromtimestamp(clock[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                for name in loop.DESTS:
                    item["destinations"][name].update(status="published", published_at=timestamp)
                break
            return 0

        def sleep(seconds):
            clock[0] += seconds

        return clock, data, attempts, pushes, Store, publish, sleep

    def execute(self, fixture, *, run_id="run", target=20, max_posts=1, segment=600,
                interval=780, upload_margin=120):
        clock, data, attempts, pushes, Store, publish, sleep = fixture
        with patch.object(loop, "StateStore", Store), patch.object(loop, "publish_one", publish), \
             patch.object(loop, "discover_readonly", lambda m: None), patch.object(loop, "log"), \
             patch.object(loop.time, "time", side_effect=lambda: clock[0]), \
             patch.object(loop.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(loop.time, "sleep", side_effect=sleep):
            return loop.run(Path("."), run_id, target=target, segment_seconds=segment,
                            interval=interval, max_posts=max_posts, upload_margin=upload_margin)

    def test_single_post_ignores_legacy_saved_delay(self):
        f = self.fixture()
        f[1]["publish_loops"] = {"scheduled": {"window_started": 1000.0, "next_old_at": 6000.0}}
        self.assertEqual(self.execute(f), 0)
        self.assertEqual(len(f[2]), 1)
        self.assertEqual(f[2][0][0], 1000.0)

    def test_single_post_does_not_retry_failure_in_same_run(self):
        f = self.fixture(failure=True)
        self.assertEqual(self.execute(f), 1)
        self.assertEqual(len(f[2]), 1)
        self.assertEqual(f[1]["publish_loops"]["scheduled"]["status"], "running")

    def test_consecutive_scheduled_invocations_publish_one_each(self):
        f = self.fixture()
        for i in range(3):
            f[0][0] = 1000 + i * 780
            self.assertEqual(self.execute(f, run_id=str(i)), 0)
            self.assertEqual(len(f[2]), i + 1)
        s = f[1]["publish_loops"]["scheduled"]
        self.assertEqual(s["completed_ids"], ["0", "1", "2"])
        self.assertEqual(s["status"], "running")

    def test_shared_target_blocks_additional_attempts(self):
        f = self.fixture()
        # The fake clock is in 1970 but the real queue uses the UTC calendar day.
        # Model its exhausted eligibility separately from the loop's rolling window.
        def queued(m, daily_limit):
            completed = m.get("publish_loops", {}).get("scheduled", {}).get("completed_ids", [])
            return {} if len(completed) >= daily_limit else {"youtube": ("0", {})}
        with patch.object(loop, "pick_jobs", side_effect=queued):
            for i in range(3):
                f[0][0] = 1000 + i * 780
                self.assertEqual(self.execute(f, run_id=str(i), target=2), 0)
        self.assertEqual(len(f[2]), 2)
        self.assertEqual(f[1]["publish_loops"]["scheduled"]["status"], "completed")
        self.assertEqual(f[1]["videos"]["2"]["destinations"]["instagram"]["status"], "pending")

    def test_quota_reached_still_allows_youtube_to_catch_up(self):
        f = self.fixture(n=2)
        f[1]["videos"]["0"]["destinations"]["instagram"].update(
            status="published", published_at="1970-01-01T00:20:00Z")
        with patch.object(loop, "pick_jobs", return_value={"youtube": ("0", {})}):
            self.assertEqual(self.execute(f, run_id="catchup", target=1), 0)
        self.assertEqual(len(f[2]), 1)

    def test_single_post_waits_when_all_destinations_are_paced(self):
        f = self.fixture()
        f[1]["publish_pacing"] = {
            "youtube": {"next_attempt_at": 2000.0},
            "instagram": {"next_attempt_at": 2000.0},
        }
        self.assertEqual(self.execute(f), 0)
        self.assertEqual(len(f[2]), 0)

    def test_quota_window_rollover_allows_pending_video(self):
        f = self.fixture()
        for i in range(2):
            f[0][0] = 1000 + i * 780
            self.execute(f, run_id=str(i), target=2)
        f[0][0] = 1000 + 86401
        self.assertEqual(self.execute(f, run_id="new-day", target=2), 0)
        self.assertEqual(len(f[2]), 3)
        self.assertEqual(f[1]["publish_loops"]["scheduled"]["completed_ids"], ["2"])

    def test_reconcile_counts_two_destinations_as_one_source(self):
        m = {"videos": {"7": entry("2026-01-01")}}
        for d in m["videos"]["7"]["destinations"].values():
            d.update(status="published", published_at="1970-01-01T00:30:00Z")
        s = loop.session_for(m, "run", 1000, 20)
        self.assertFalse(loop.reconcile(m, s, "run"))
        self.assertEqual(s["completed_ids"], ["7"])

    def test_reconcile_excludes_publishes_outside_window(self):
        m = {"videos": {"7": entry("2026-01-01")}}
        m["videos"]["7"]["destinations"]["instagram"].update(
            status="published", published_at="1970-01-01T00:10:00Z")
        s = loop.session_for(m, "run", 1000, 20)
        loop.reconcile(m, s, "run")
        self.assertEqual(s["completed_ids"], [])

    def test_multi_post_mode_retains_legacy_pacing(self):
        f = self.fixture(n=2)
        self.assertEqual(self.execute(f, max_posts=2, segment=10000,
                                      interval=4320, upload_margin=0), 0)
        self.assertEqual([t for t, _ in f[2]], [1000.0, 5320.0])

    def test_too_short_window_does_not_attempt_upload(self):
        f = self.fixture()
        self.assertEqual(self.execute(f, segment=100, upload_margin=120), 0)
        self.assertEqual(f[2], [])
