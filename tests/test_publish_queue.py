"""Regression tests for independent destination backfill selection (CI-safe).

These tests are pure state-selection regression tests: they never call TikTok, YouTube, Instagram, yt-dlp, or ffmpeg.
"""
import unittest

from pipeline import manifest as mf
from pipeline.publish_one import pick_jobs


def entry(created, youtube="pending", instagram="pending"):
    return {
        "source": {"platform": "tiktok", "video_id": created, "created_at": created, "url": "https://example.invalid"},
        "media": dict(mf.MEDIA_TEMPLATE),
        "destinations": {
            "youtube": {**mf.DEST_TEMPLATE, "status": youtube},
            "instagram": {**mf.DEST_TEMPLATE, "status": instagram},
        },
    }


class IndependentQueueTests(unittest.TestCase):
    def test_partial_failure_does_not_hold_healthy_platform_back(self):
        m = {"videos": {
            "old": entry("2026-01-01T00:00:00Z", youtube="failed", instagram="published"),
            "next": entry("2026-01-02T00:00:00Z"),
        }}
        jobs = pick_jobs(m)
        self.assertEqual(jobs["youtube"][0], "old")
        self.assertEqual(jobs["instagram"][0], "next")

    def test_published_destination_is_never_selected_again(self):
        m = {"videos": {
            "old": entry("2026-01-01T00:00:00Z", youtube="published", instagram="published"),
            "next": entry("2026-01-02T00:00:00Z"),
        }}
        jobs = pick_jobs(m)
        self.assertEqual(jobs["youtube"][0], "next")
        self.assertEqual(jobs["instagram"][0], "next")

    def test_stuck_destination_does_not_block_other_platform(self):
        m = {"videos": {
            "old": entry("2026-01-01T00:00:00Z", youtube="publishing", instagram="published"),
            "next": entry("2026-01-02T00:00:00Z"),
        }}
        jobs = pick_jobs(m)
        self.assertEqual(jobs["youtube"][0], "next")
        self.assertEqual(jobs["instagram"][0], "next")

    def test_daily_cap_blocks_new_historical_ids(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        m = {"videos": {
            "old": entry("2026-01-01T00:00:00Z", youtube="published", instagram="pending"),
            "next": entry("2026-01-02T00:00:00Z"),
        }}
        m["videos"]["old"]["destinations"]["youtube"]["published_at"] = today + "T08:00:00Z"
        jobs = pick_jobs(m, daily_limit=1)
        self.assertNotIn("youtube", jobs)
        self.assertEqual(jobs["instagram"][0], "old")

    def test_fresh_post_bypasses_daily_cap(self):
        from pipeline import manifest as mf
        m = {"videos": {
            "old": entry("2026-01-01T00:00:00Z"),
            "fresh": entry("2026-10-07T23:00:00Z"),
        }}
        m["videos"]["fresh"]["source"]["first_seen_at"] = mf.now()
        m["videos"]["fresh"]["source"]["fresh_priority"] = True
        jobs = pick_jobs(m, daily_limit=1)
        self.assertEqual(jobs["youtube"][0], "fresh")
        self.assertEqual(jobs["instagram"][0], "fresh")

    def test_quota_counts_distinct_ids_not_destinations(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        m = {"videos": {
            "old": entry("2026-01-01T00:00:00Z", youtube="published", instagram="published"),
            "next": entry("2026-01-02T00:00:00Z"),
        }}
        for d in m["videos"]["old"]["destinations"].values():
            d["published_at"] = today + "T08:00:00Z"
        jobs = pick_jobs(m, daily_limit=2)
        self.assertEqual(jobs["youtube"][0], "next")
        self.assertEqual(jobs["instagram"][0], "next")

    def test_platform_pacing_does_not_block_other_destination(self):
        import time
        m = {"videos": {"old": entry("2026-01-01T00:00:00Z")},
             "publish_pacing": {"youtube": {"next_attempt_at": time.time() + 3600}}}
        jobs = pick_jobs(m)
        self.assertNotIn("youtube", jobs)
        self.assertEqual(jobs["instagram"][0], "old")

    def test_existing_youtube_upload_limit_error_is_cooled_down(self):
        m = {"videos": {"old": entry("2026-01-01T00:00:00Z", youtube="failed")}}
        m["videos"]["old"]["destinations"]["youtube"].update(
            last_error="HTTP 400: uploadLimitExceeded", started_at=mf.now())
        jobs = pick_jobs(m)
        self.assertNotIn("youtube", jobs)
        self.assertEqual(jobs["instagram"][0], "old")

    def test_cooldown_expiry_allows_youtube_again(self):
        import time
        m = {"videos": {"old": entry("2026-01-01T00:00:00Z", youtube="failed")},
             "publish_pacing": {"youtube": {"next_attempt_at": time.time() - 1,
                                            "cooldown_until": time.time() - 1}}}
        jobs = pick_jobs(m)
        self.assertEqual(jobs["youtube"][0], "old")

    def test_failed_destination_is_retryable(self):
        m = {"videos": {"old": entry("2026-01-01T00:00:00Z", youtube="failed", instagram="published")}}
        jobs = pick_jobs(m)
        self.assertEqual(jobs["youtube"][0], "old")
        self.assertNotIn("instagram", jobs)


if __name__ == "__main__":
    unittest.main()
