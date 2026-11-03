"""Phone drops jump ahead of TikTok backfill (CI-safe: no network, no ffmpeg)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from pipeline import drops, manifest as mf
from pipeline.publish_one import pick_jobs


def tiktok(created, account="primary"):
    return {
        "source": {"platform": "tiktok", "account": account, "video_id": created, "created_at": created,
                   "url": "https://example.invalid"},
        "media": dict(mf.MEDIA_TEMPLATE),
        "destinations": {"youtube": dict(mf.DEST_TEMPLATE), "instagram": dict(mf.DEST_TEMPLATE)},
    }


def write_drop(root: Path, did: str, caption="hello #fyp", cover=True):
    folder = root / "drops"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{did}.mp4").write_bytes(b"x")
    (folder / f"{did}.json").write_text(json.dumps({"caption": caption, "created_at": "2026-11-02T10:00:00Z"}))
    if cover:
        (folder / f"{did}.jpg").write_bytes(b"x")


class DropTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_adds_entry_once_and_keeps_state(self):
        write_drop(self.root, "20261102-a")
        m = {"videos": {}}
        self.assertEqual(drops.import_drops(m, self.root), 1)
        entry = m["videos"]["drop-20261102-a"]
        self.assertEqual(entry["source"]["metadata"]["video_description"], "hello #fyp")
        self.assertTrue(entry["source"]["metadata"]["cover_image_url"].startswith("https://raw.githubusercontent.com/"))
        entry["destinations"]["instagram"]["status"] = "published"
        self.assertEqual(drops.import_drops(m, self.root), 0)
        self.assertEqual(m["videos"]["drop-20261102-a"]["destinations"]["instagram"]["status"], "published")

    def test_incomplete_or_unsafe_drops_are_ignored(self):
        folder = self.root / "drops"
        folder.mkdir()
        (folder / "nofile.json").write_text("{}")          # caption without video
        (folder / "bad id.json").write_text("{}")
        (folder / "bad id.mp4").write_bytes(b"x")
        m = {"videos": {}}
        self.assertEqual(drops.import_drops(m, self.root), 0)

    def test_drop_leads_both_queues_ahead_of_older_tiktok(self):
        write_drop(self.root, "20261102-a", cover=False)
        m = {"videos": {"old": tiktok("2020-01-01T00:00:00Z")}}
        drops.import_drops(m, self.root)
        self.assertIsNone(m["videos"]["drop-20261102-a"]["source"]["metadata"]["cover_image_url"])
        jobs = pick_jobs(m)
        self.assertEqual(jobs["youtube"][0], "drop-20261102-a")
        self.assertEqual(jobs["instagram"][0], "drop-20261102-a")

    def test_backfill_resumes_after_drop_is_posted(self):
        write_drop(self.root, "20261102-a")
        m = {"videos": {"old": tiktok("2020-01-01T00:00:00Z")}}
        drops.import_drops(m, self.root)
        for d in m["videos"]["drop-20261102-a"]["destinations"].values():
            d["status"] = "published"
        jobs = pick_jobs(m)
        self.assertEqual(jobs["instagram"][0], "old")

    def test_split_video_is_imported_only_when_complete_and_rejoined_in_order(self):
        folder = self.root / "drops"
        folder.mkdir()
        (folder / "big.json").write_text(json.dumps({"caption": "big", "parts": 3}))
        (folder / "big.part000.mp4").write_bytes(b"aa")
        (folder / "big.part001.mp4").write_bytes(b"bb")
        m = {"videos": {}}
        self.assertEqual(drops.import_drops(m, self.root), 0)      # last part still missing
        (folder / "big.part002.mp4").write_bytes(b"cc")
        self.assertEqual(drops.import_drops(m, self.root), 1)
        entry = m["videos"]["drop-big"]
        os.environ["DROPS_DIR"] = str(self.root)
        try:
            out = drops.copy_media(entry, self.root / "out" / "drop-big.mp4")
        finally:
            del os.environ["DROPS_DIR"]
        self.assertEqual(out.read_bytes(), b"aabbcc")

    def test_cancelled_drop_is_never_picked_but_published_side_is_kept(self):
        write_drop(self.root, "20261102-a")
        m = {"videos": {"old": tiktok("2020-01-01T00:00:00Z")}}
        drops.import_drops(m, self.root)
        m["videos"]["drop-20261102-a"]["destinations"]["youtube"]["status"] = "published"
        (self.root / "drops" / "20261102-a.json").unlink()      # Cancel button
        self.assertEqual(drops.import_drops(m, self.root), 0)
        ds = m["videos"]["drop-20261102-a"]["destinations"]
        self.assertEqual(ds["instagram"]["status"], "cancelled")
        self.assertEqual(ds["youtube"]["status"], "published")
        jobs = pick_jobs(m)
        self.assertEqual(jobs["instagram"][0], "old")

    def test_missing_drops_checkout_cancels_nothing(self):
        write_drop(self.root, "20261102-a")
        m = {"videos": {}}
        drops.import_drops(m, self.root)
        drops.import_drops(m, self.root / "not-checked-out")
        self.assertEqual(m["videos"]["drop-20261102-a"]["destinations"]["instagram"]["status"], "pending")

    def test_held_drop_waits_until_released(self):
        write_drop(self.root, "20261102-a")
        meta = self.root / "drops" / "20261102-a.json"
        meta.write_text(json.dumps({"caption": "x", "hold": True}))
        m = {"videos": {}}
        self.assertEqual(drops.import_drops(m, self.root), 0)
        meta.write_text(json.dumps({"caption": "x", "hold": False}))     # Publish now
        self.assertEqual(drops.import_drops(m, self.root), 1)

    def test_drops_bypass_historical_daily_quota(self):
        write_drop(self.root, "20261102-a")
        m = {"videos": {"old": tiktok("2020-01-01T00:00:00Z")}}
        drops.import_drops(m, self.root)
        jobs = pick_jobs(m, daily_limit=1)
        self.assertEqual(jobs["instagram"][0], "drop-20261102-a")


if __name__ == "__main__":
    unittest.main()
