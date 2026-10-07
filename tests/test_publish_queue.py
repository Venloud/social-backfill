"""Regression tests for independent destination backfill selection (CI-safe).

These tests are pure state-selection tests: they never call TikTok, YouTube, Instagram, yt-dlp, or ffmpeg.
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

    def test_failed_destination_is_retryable(self):
        m = {"videos": {"old": entry("2026-01-01T00:00:00Z", youtube="failed", instagram="published")}}
        jobs = pick_jobs(m)
        self.assertEqual(jobs["youtube"][0], "old")
        self.assertNotIn("instagram", jobs)


if __name__ == "__main__":
    unittest.main()
